"""Per-connection v2 client state and the snapshot/ACK handoff — ADR-0014 D8/D11 (runtime).

The hub is the minimal half of D11 this card needs: one
:class:`InherentClient` per accepted socket holding the snapshot staging,
the awaiting-ACK record, the five-second adoption deadline, the live
frontier (via its :class:`ClientLane`), and one sender task.  Bounded
queues, byte windows, ephemeral coalescing and ACK batching are the
per-client-flow-control card's extension of this module.

The handoff follows D8 to the letter and none of it runs inside the
sequencer actor: :meth:`InherentClient._run_snapshot` asks the sequencer for
a checkpoint at ``H`` (one synchronous command), builds the plan through the
presenter, records it as awaiting ACK, transmits begin / pages / end through
the sender, waits for the exact ACK outside the actor, and only then posts
the second command that folds ``(H, B]`` and switches the lane live.

:func:`start_inherent_view` is the one call ``serve_inherent`` makes: it
resolves ``realtime.inherent.v2_sequencer.enabled``, downgrades once with a
warning when ``realtime.enabled`` is off, and returns either the wiring the
v2 route needs or None, in which case nothing here is constructed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from jarvis.runtime.inherent_view_sequencer import (
    CatchUpBudgetExceededError,
    ClientLane,
    InherentViewSequencer,
    SnapshotStaging,
)
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface.inherent_presenter import build_snapshot_plan
from jarvis.surface.inherent_protocol import (
    VIEW_SCHEMA_VERSION,
    ClientEnvelope,
    ServerEnvelope,
    TransportAckPayload,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from jarvis.runtime import JarvisRuntime

LOGGER = logging.getLogger("jarvis.runtime.inherent_hub")

SNAPSHOT_ADOPTION_DEADLINE_S: Final[float] = 5.0
"""D11 rule 9: no ACK for the active snapshot within this closes the client."""

_CLOSE_PROTOCOL_ERROR: Final[int] = 1002
_CLOSE_RESYNC_REQUIRED: Final[int] = 1008
_ACK_MESSAGE_TYPE: Final[str] = "transport.ack"


class InherentClient:
    """One v2 connection from hello to close.

    The server route owns the socket and its receive loop; it forwards every
    vetted post-hello frame to :meth:`on_frame` and calls :meth:`detach` when
    the socket is gone.  This object owns everything the connection sends.
    """

    def __init__(  # noqa: PLR0913 — every identity and callable the connection binds.
        self,
        *,
        sequencer: InherentViewSequencer,
        connection_id: str,
        send_text: Callable[[str], Awaitable[None]],
        close: Callable[[int, str], Awaitable[None]],
        log_epoch: str,
        boot_id: str,
        adoption_deadline_s: float,
        clock: Callable[[], float],
    ) -> None:
        """Bind one accepted socket; nothing runs until :meth:`start`."""
        self._sequencer = sequencer
        self.connection_id = connection_id
        self._send_text = send_text
        self._close_socket = close
        self._log_epoch = log_epoch
        self._boot_id = boot_id
        self._adoption_deadline_s = adoption_deadline_s
        self._clock = clock
        self._lane = ClientLane(connection_id=connection_id, enqueue=self._enqueue)
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._awaiting: SnapshotStaging | None = None
        self._ack_event = asyncio.Event()
        self._last_sent_cursor = 0
        self._last_acked_cursor = 0
        self._closed = False
        self._sender: asyncio.Task[None] | None = None
        self._snapshot: asyncio.Task[None] | None = None

    # --- observable state ---------------------------------------------------

    @property
    def last_sent_cursor(self) -> int:
        """Highest durable cursor handed to the sender (D11 rule 5)."""
        return self._last_sent_cursor

    @property
    def last_acked_cursor(self) -> int:
        """Highest cursor the client has cumulatively ACKed (D11 rule 5)."""
        return self._last_acked_cursor

    @property
    def live_frontier(self) -> int | None:
        """``B`` once the catch-up is enqueued; None while snapshotting."""
        return self._lane.live_frontier

    @property
    def closed(self) -> bool:
        """Whether this connection has been closed or detached."""
        return self._closed

    # --- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Start the sender and the snapshot handoff."""
        self._sender = asyncio.create_task(
            self._run_sender(), name=f"inherent_v2_sender:{self.connection_id}",
        )
        self._snapshot = asyncio.create_task(
            self._run_snapshot(), name=f"inherent_v2_snapshot:{self.connection_id}",
        )

    async def on_frame(self, envelope: ClientEnvelope) -> None:
        """Route one vetted post-hello client frame (D11 rule 7 for ACKs).

        Frames other than ``transport.ack`` belong to later cards and are
        ignored here, exactly as card 1 ignored every post-hello frame.
        """
        if self._closed or envelope.message_type != _ACK_MESSAGE_TYPE:
            return
        fault = self._apply_ack(envelope)
        if fault is not None:
            await self._fail("protocol_error", fault)

    def _apply_ack(self, envelope: ClientEnvelope) -> str | None:
        """Apply one ACK; return the protocol fault that rejects it, if any."""
        if envelope.connection_id != self.connection_id:
            return "ack for another connection"
        try:
            ack = TransportAckPayload.model_validate(envelope.payload)
        except ValidationError:
            return "malformed transport.ack"
        awaiting = self._awaiting
        if awaiting is not None:
            expected = (awaiting.snapshot_id, awaiting.through_cursor)
            if (ack.snapshot_id, ack.through_cursor) != expected:
                return "ack names another snapshot or cursor"
            self._ack_event.set()
        elif ack.snapshot_id is not None:
            return "snapshot ack with no active snapshot"
        elif ack.through_cursor > self._last_sent_cursor:
            return "ack past last sent cursor"
        else:
            # A regressing or repeated cumulative ACK carries nothing new (rule 7).
            self._last_acked_cursor = max(self._last_acked_cursor, ack.through_cursor)
        return None

    async def detach(self) -> None:
        """Release the lane and stop the tasks; the socket is already gone."""
        self._closed = True
        self._sequencer.detach(self._lane)
        if self._snapshot is not None:
            self._snapshot.cancel()
        self._queue.put_nowait(None)
        for task in (self._snapshot, self._sender):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    # --- internals --------------------------------------------------------

    def _enqueue(self, frame: str, cursor: int) -> None:
        self._last_sent_cursor = max(self._last_sent_cursor, cursor)
        self._queue.put_nowait(frame)

    def _protocol_frame(
        self, message_type: str, message_id: str, payload: Mapping[str, Any],
    ) -> str:
        return ServerEnvelope(
            protocol_version=2,
            message_type=message_type,
            message_id=message_id,
            delivery_class="protocol",
            connection_id=self.connection_id,
            log_epoch=self._log_epoch,
            boot_id=self._boot_id,
            sent_at_ms=int(self._clock() * 1000),
            payload=dict(payload),
        ).model_dump_json()

    async def _run_snapshot(self) -> None:
        staging = self._sequencer.begin_snapshot(self._lane)
        plan = build_snapshot_plan(
            staging.checkpoint,
            snapshot_id=staging.snapshot_id,
            view_schema_version=VIEW_SCHEMA_VERSION,
            encode=self._protocol_frame,
        )
        self._awaiting = staging
        for frame in plan.frames:
            self._queue.put_nowait(frame)
        record_realtime_trace(
            "inherent_v2_snapshot_sent",
            connection_id=self.connection_id,
            snapshot_id=staging.snapshot_id,
            through_cursor=staging.through_cursor,
            pages=len(plan.page_frames),
        )
        try:
            await asyncio.wait_for(self._ack_event.wait(), self._adoption_deadline_s)
        except TimeoutError:
            await self._fail("resync_required", "no snapshot ack within the adoption deadline")
            return
        self._awaiting = None
        self._last_sent_cursor = self._last_acked_cursor = staging.through_cursor
        try:
            replayed = self._sequencer.complete_snapshot(self._lane, staging)
        except CatchUpBudgetExceededError as exc:
            await self._fail("resync_required", str(exc))
            return
        record_realtime_trace(
            "inherent_v2_client_live",
            connection_id=self.connection_id,
            snapshot_id=staging.snapshot_id,
            live_frontier=self._lane.live_frontier or 0,
            catch_up_frames=replayed,
        )

    async def _run_sender(self) -> None:
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            try:
                await self._send_text(frame)
            except Exception:  # noqa: BLE001 — a dead socket ends this connection only.
                LOGGER.warning("inherent v2 send failed for %s; closing", self.connection_id)
                await self._fail("send_failed", "send failed")
                return

    async def _fail(self, reason: str, detail: str) -> None:
        if self._closed:
            return
        self._closed = True
        self._sequencer.detach(self._lane)
        LOGGER.warning("inherent v2 client %s closed: %s (%s)", self.connection_id, reason, detail)
        record_realtime_trace(
            "inherent_v2_client_closed",
            connection_id=self.connection_id,
            reason=reason,
        )
        code = _CLOSE_PROTOCOL_ERROR if reason == "protocol_error" else _CLOSE_RESYNC_REQUIRED
        with contextlib.suppress(Exception):
            await self._close_socket(code, reason)
        self._queue.put_nowait(None)


class InherentHub:
    """Creates and starts one :class:`InherentClient` per accepted v2 socket."""

    def __init__(
        self,
        sequencer: InherentViewSequencer,
        *,
        log_epoch: str,
        boot_id: str,
        adoption_deadline_s: float = SNAPSHOT_ADOPTION_DEADLINE_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind the sequencer and the identities every frame carries."""
        self._sequencer = sequencer
        self._log_epoch = log_epoch
        self._boot_id = boot_id
        self._adoption_deadline_s = adoption_deadline_s
        self._clock = clock

    async def attach(
        self,
        connection_id: str,
        send_text: Callable[[str], Awaitable[None]],
        close: Callable[[int, str], Awaitable[None]],
    ) -> InherentClient:
        """Take over a hello-completed socket and start its snapshot handoff."""
        client = InherentClient(
            sequencer=self._sequencer,
            connection_id=connection_id,
            send_text=send_text,
            close=close,
            log_epoch=self._log_epoch,
            boot_id=self._boot_id,
            adoption_deadline_s=self._adoption_deadline_s,
            clock=self._clock,
        )
        client.start()
        return client


@dataclass(frozen=True)
class InherentViewWiring:
    """What ``serve_inherent`` plugs into the v2 route and its watcher list."""

    hub: InherentHub
    sequencer: InherentViewSequencer
    tasks: tuple[asyncio.Task[None], ...]


def inherent_v2_sequencer_enabled(config: Mapping[str, Any]) -> bool:
    """Resolve ``realtime.inherent.v2_sequencer.enabled`` with its parent rule.

    The switch additionally requires ``realtime.enabled``; an invalid
    combination downgrades once, with one warning, to the v1-only wire.
    """
    realtime = config.get("realtime")
    if not isinstance(realtime, Mapping):
        return False
    inherent = realtime.get("inherent")
    block = inherent.get("v2_sequencer") if isinstance(inherent, Mapping) else None
    requested = isinstance(block, Mapping) and block.get("enabled") is True
    if not requested:
        return False
    if realtime.get("enabled") is not True:
        LOGGER.warning(
            "realtime.inherent.v2_sequencer downgraded (realtime_parent_disabled): "
            "requested enabled=True; effective enabled=False",
        )
        record_realtime_trace(
            "inherent_v2_sequencer_downgraded",
            reason="realtime_parent_disabled",
        )
        return False
    return True


async def start_inherent_view(
    runtime: JarvisRuntime,
    *,
    boot_id: str,
    log_epoch: str,
    poll_interval_s: float,
) -> InherentViewWiring | None:
    """Construct and start the sequencer and hub when the flag is on.

    Args:
        runtime: The assembled runtime; its connection and bus are used.
        boot_id: This boot's id, shared with the D7 hello.
        log_epoch: The log's epoch, shared with the D7 hello.
        poll_interval_s: The v1 watcher cadence, reused as the recovery timer.

    Returns:
        The wiring, or None when the flag (or its parent) is off.
    """
    if not inherent_v2_sequencer_enabled(runtime.config):
        return None
    sequencer = InherentViewSequencer(
        runtime.conn,
        log_epoch=log_epoch,
        boot_id=boot_id,
        recovery_interval_s=poll_interval_s,
    )
    tasks = await sequencer.start(bus=runtime.committed_event_bus)
    hub = InherentHub(sequencer, log_epoch=log_epoch, boot_id=boot_id)
    return InherentViewWiring(hub=hub, sequencer=sequencer, tasks=tasks)


__all__ = [
    "SNAPSHOT_ADOPTION_DEADLINE_S",
    "InherentClient",
    "InherentHub",
    "InherentViewWiring",
    "inherent_v2_sequencer_enabled",
    "start_inherent_view",
]
