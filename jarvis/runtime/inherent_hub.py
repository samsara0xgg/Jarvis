"""Per-connection v2 client state, snapshot handoff and flow control — ADR-0014 D8/D11 (runtime).

One :class:`InherentClient` per accepted socket holds the snapshot staging,
the awaiting-ACK record, the five-second adoption deadline, the live
frontier (via its :class:`ClientLane`), the three bounded D11 lanes and
one sender task.  The lanes are what keep a slow or silent client from
growing a heap queue without limit or stalling anyone else:

- a control lane of ``control_frames`` protocol frames (the
  ``server.resync_required`` notice and the rule-12 ``ephemeral.clear``);
- a durable/snapshot lane bounded by ``durable_frames`` and
  ``durable_bytes`` of encoded UTF-8, which never drops or reorders (rule 1)
  and closes the client with ``client_backpressure`` when it would overflow
  (rule 3);
- an ephemeral coalescing map of ``ephemeral_keys`` latest values, whose
  33rd key evicts the oldest only behind an ordered clear (rule 12).

The sender serves control, then durable, then ephemeral, with at most
``control_fairness`` consecutive control frames while a durable frame is
ready (rule 10).  Every durable frame handed to the socket enters the
unacked window (rule 5); no ACK progress for ``ack_stall_s`` while that
window is non-empty closes the client with ``ack_stalled`` (rule 9).  Every
close affects exactly one client (rule 4): the sequencer, its other lanes
and the loop are never blocked on a socket.

The handoff follows D8 to the letter and none of it runs inside the
sequencer actor: :meth:`InherentClient._run_snapshot` asks the sequencer for
a checkpoint at ``H`` (one synchronous command), builds the plan through the
presenter, records it as awaiting ACK, transmits begin / pages / end through
the sender, waits for the exact ACK outside the actor, and only then posts
the second command that folds ``(H, B]`` and switches the lane live.

No production ephemeral producer exists yet: :meth:`InherentClient.enqueue_ephemeral`
is the hub's contract for the partial-transcript and progress cards and is
exercised only by tests today.

:func:`start_inherent_view` is the one call ``serve_inherent`` makes: it
resolves ``realtime.inherent.v2_sequencer.enabled`` and its ``flow_control``
block, downgrades once with a warning when ``realtime.enabled`` is off, and
returns either the wiring the v2 route needs or None, in which case nothing
here is constructed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, Final, Literal

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
    from jarvis.surface.inherent_server import V2Session

LOGGER = logging.getLogger("jarvis.runtime.inherent_hub")

SNAPSHOT_ADOPTION_DEADLINE_S: Final[float] = 5.0
"""D11 rule 9: no ACK for the active snapshot within this closes the client."""

ACK_STALL_POLL_S: Final[float] = 0.5
"""How often the rule-9 watchdog reads the injected clock; not a D11 limit."""

_CLOSE_PROTOCOL_ERROR: Final[int] = 1002
_CLOSE_RESYNC_REQUIRED: Final[int] = 1008
_ACK_MESSAGE_TYPE: Final[str] = "transport.ack"
_RESYNC_MESSAGE_TYPE: Final[str] = "server.resync_required"
_EPHEMERAL_MESSAGE_TYPE: Final[str] = "ephemeral"
_NOTICE_FLUSH_GRACE_S: Final[float] = 0.25
"""Best effort for rule 3: how long the closer lets the sender flush the control lane."""

_Lane = Literal["control", "durable", "ephemeral"]


@dataclass(frozen=True)
class FlowControlLimits:
    """The D11 limits; ``realtime.inherent.v2_sequencer.flow_control`` overrides any key.

    ``ack_batch_messages`` and ``ack_batch_ms`` are the client's cadence (rule
    8); the daemon records them beside the limits they make safe but never
    negotiates them.
    """

    control_frames: int = 32
    durable_frames: int = 256
    durable_bytes: int = 1_048_576
    ephemeral_keys: int = 32
    ack_batch_messages: int = 25
    ack_batch_ms: int = 100
    ack_stall_s: float = 5.0
    control_fairness: int = 8


@dataclass
class _EphemeralSlot:
    """One key's latest value; ``pending`` until the sender encodes it."""

    kind: str
    message_id: str
    payload: dict[str, Any]
    pending: bool = True


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
        limits: FlowControlLimits,
        ack_stall_poll_s: float,
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
        self._limits = limits
        self._ack_stall_poll_s = ack_stall_poll_s
        self._lane = ClientLane(
            connection_id=connection_id, enqueue=self._enqueue, fail=self._fail_from_lane,
        )
        self._control: deque[str | _EphemeralSlot] = deque()
        self._durable: deque[tuple[str, int, int]] = deque()
        self._durable_bytes = 0
        self._ephemeral: OrderedDict[str, _EphemeralSlot] = OrderedDict()
        self._ephemeral_sequence = 0
        self._window: deque[tuple[int, int]] = deque()
        self._window_bytes = 0
        self._ack_progress_at = clock()
        self._wake = asyncio.Event()
        self._awaiting: SnapshotStaging | None = None
        self._ack_event = asyncio.Event()
        self._last_sent_cursor = 0
        self._last_acked_cursor = 0
        self._closed = False
        self._sender: asyncio.Task[None] | None = None
        self._snapshot: asyncio.Task[None] | None = None
        self._closer: asyncio.Task[None] | None = None

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
    def unacked_frames(self) -> int:
        """Durable/snapshot frames handed to the socket and not yet ACKed (rule 5)."""
        return len(self._window)

    @property
    def unacked_bytes(self) -> int:
        """Encoded bytes of :attr:`unacked_frames` (rule 5)."""
        return self._window_bytes

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
        elif not (self._last_acked_cursor <= ack.through_cursor <= self._last_sent_cursor):
            return "ack outside [last acked, last sent]"
        elif ack.through_cursor > self._last_acked_cursor:
            # Rule 7: only an exact duplicate of the last cumulative ACK is
            # tolerated; it carries nothing new.
            self._last_acked_cursor = ack.through_cursor
            self._release_window(ack.through_cursor)
        return None

    def enqueue_ephemeral(self, key: str, kind: str, payload: Mapping[str, Any]) -> None:
        """Coalesce one ephemeral update by key (D11 rules 2 and 12).

        A known key is replaced in place.  A 33rd distinct key evicts the
        oldest one, and an ``ephemeral.clear`` naming it travels on the
        control lane — ahead of every ephemeral frame — so the client never
        keeps stale state; a control lane that cannot take the clear closes
        the client instead.  The ``ephemeral_sequence`` is assigned when the
        sender encodes the frame, so it increases in wire order.
        """
        if self._closed:
            return
        slot = self._ephemeral.get(key)
        if slot is not None:
            slot.kind, slot.payload, slot.pending = kind, dict(payload), True
        else:
            if len(self._ephemeral) >= self._limits.ephemeral_keys:
                evicted, _ = self._ephemeral.popitem(last=False)
                clear = _EphemeralSlot("ephemeral.clear", _new_message_id(), {"key": evicted})
                if not self._enqueue_control(clear):
                    return
            self._ephemeral[key] = _EphemeralSlot(kind, _new_message_id(), dict(payload))
        self._wake.set()

    async def detach(self) -> None:
        """Release the lane and stop the tasks; the socket is already gone."""
        self._closed = True
        self._sequencer.detach(self._lane)
        if self._snapshot is not None:
            self._snapshot.cancel()
        self._wake.set()
        for task in (self._snapshot, self._sender, self._closer):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    # --- lanes ------------------------------------------------------------

    def _enqueue(self, frame: str, cursor: int) -> None:
        """The durable/snapshot lane's only entry; rule 3 on overflow."""
        if self._closed:
            return
        size = len(frame.encode("utf-8"))
        if (
            len(self._durable) >= self._limits.durable_frames
            or self._durable_bytes + size > self._limits.durable_bytes
        ):
            self._fail_now(
                "client_backpressure",
                f"durable lane at {len(self._durable)} frames / {self._durable_bytes} bytes",
                notify=True,
            )
            return
        self._last_sent_cursor = max(self._last_sent_cursor, cursor)
        self._durable.append((frame, cursor, size))
        self._durable_bytes += size
        self._wake.set()

    def _enqueue_control(self, frame: str | _EphemeralSlot) -> bool:
        if len(self._control) >= self._limits.control_frames:
            self._fail_now("control_overflow", "control lane full", notify=False)
            return False
        self._control.append(frame)
        self._wake.set()
        return True

    def _release_window(self, through_cursor: int) -> None:
        while self._window and self._window[0][0] <= through_cursor:
            self._window_bytes -= self._window.popleft()[1]
        self._ack_progress_at = self._clock()

    def _next_frame(self, control_streak: int) -> tuple[_Lane, str] | None:
        if self._closed:
            # After a close request only the best-effort notice still leaves.
            if not self._control:
                return None
            return ("control", self._encode_control(self._control.popleft()))
        if self._control and (
            control_streak < self._limits.control_fairness or not self._durable
        ):
            return ("control", self._encode_control(self._control.popleft()))
        if self._durable:
            frame, cursor, size = self._durable.popleft()
            self._durable_bytes -= size
            if not self._window:
                self._ack_progress_at = self._clock()
            self._window.append((cursor, size))
            self._window_bytes += size
            return ("durable", frame)
        for slot in self._ephemeral.values():
            if slot.pending:
                slot.pending = False
                return ("ephemeral", self._ephemeral_frame(slot))
        return None

    def _encode_control(self, item: str | _EphemeralSlot) -> str:
        return item if isinstance(item, str) else self._ephemeral_frame(item)

    def _ephemeral_frame(self, slot: _EphemeralSlot) -> str:
        self._ephemeral_sequence += 1
        return ServerEnvelope(
            protocol_version=2,
            message_type=_EPHEMERAL_MESSAGE_TYPE,
            message_id=slot.message_id,
            delivery_class="ephemeral",
            connection_id=self.connection_id,
            log_epoch=self._log_epoch,
            boot_id=self._boot_id,
            ephemeral_sequence=self._ephemeral_sequence,
            sent_at_ms=int(self._clock() * 1000),
            payload={"kind": slot.kind, **slot.payload},
        ).model_dump_json()

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

    # --- tasks ------------------------------------------------------------

    async def _run_snapshot(self) -> None:
        try:
            if await self._handoff():
                await self._watch_acks()
        except asyncio.CancelledError:
            raise
        except Exception:  # One client's fault closes that client only.
            LOGGER.exception("inherent v2 snapshot path for %s raised; closing", self.connection_id)
            await self._fail("internal_error", "snapshot path raised")

    async def _handoff(self) -> bool:
        """D8 steps 1-6; True once the lane is live."""
        staging = self._sequencer.begin_snapshot(self._lane)
        plan = build_snapshot_plan(
            staging.checkpoint,
            snapshot_id=staging.snapshot_id,
            view_schema_version=VIEW_SCHEMA_VERSION,
            encode=self._protocol_frame,
        )
        self._awaiting = staging
        for frame in plan.frames:
            self._enqueue(frame, staging.through_cursor)
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
            return False
        self._awaiting = None
        self._last_sent_cursor = self._last_acked_cursor = staging.through_cursor
        self._release_window(staging.through_cursor)
        try:
            replayed = self._sequencer.complete_snapshot(self._lane, staging)
        except CatchUpBudgetExceededError as exc:
            await self._fail("catch_up_budget", str(exc))
            return False
        record_realtime_trace(
            "inherent_v2_client_live",
            connection_id=self.connection_id,
            snapshot_id=staging.snapshot_id,
            live_frontier=self._lane.live_frontier or 0,
            catch_up_frames=replayed,
        )
        return not self._closed

    async def _watch_acks(self) -> None:
        """D11 rule 9 on the injected clock: a non-empty window must make ACK progress."""
        while not self._closed:
            await asyncio.sleep(self._ack_stall_poll_s)
            stalled_for = self._clock() - self._ack_progress_at
            if self._window and stalled_for >= self._limits.ack_stall_s:
                await self._fail(
                    "ack_stalled",
                    f"{len(self._window)} frames / {self._window_bytes} bytes unacked "
                    f"for {stalled_for:.1f}s",
                )
                return

    async def _run_sender(self) -> None:
        control_streak = 0
        while True:
            item = self._next_frame(control_streak)
            if item is None:
                if self._closed:
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            lane, frame = item
            control_streak = control_streak + 1 if lane == "control" else 0
            try:
                await self._send_text(frame)
            except Exception:  # noqa: BLE001 — a dead socket ends this connection only.
                LOGGER.warning("inherent v2 send failed for %s; closing", self.connection_id)
                self._fail_now("send_failed", "send failed", notify=False)
                return

    # --- closing ------------------------------------------------------------

    def _fail_from_lane(self, detail: str) -> None:
        self._fail_now("internal_error", detail, notify=False)

    def _fail_now(self, reason: str, detail: str, *, notify: bool) -> bool:
        """Close this client once, synchronously; the socket close runs as a task.

        Safe inside the sequencer actor (no await).  ``notify`` queues the
        rule-3 ``server.resync_required`` notice, which the sender flushes
        best-effort before the closer closes the socket.
        """
        if self._closed:
            return False
        self._closed = True
        self._sequencer.detach(self._lane)
        LOGGER.warning("inherent v2 client %s closed: %s (%s)", self.connection_id, reason, detail)
        record_realtime_trace(
            "inherent_v2_client_closed",
            connection_id=self.connection_id,
            reason=reason,
        )
        if notify:
            # The lane stays bounded: on a closing client the notice matters
            # more than a queued ephemeral.clear, so it replaces the oldest
            # control frame rather than growing past control_frames.
            if len(self._control) >= self._limits.control_frames:
                self._control.popleft()
            self._control.append(
                self._protocol_frame(_RESYNC_MESSAGE_TYPE, _new_message_id(), {"reason": reason}),
            )
        code = _CLOSE_PROTOCOL_ERROR if reason == "protocol_error" else _CLOSE_RESYNC_REQUIRED
        self._wake.set()
        self._closer = asyncio.create_task(
            self._close_after_flush(code, reason), name=f"inherent_v2_closer:{self.connection_id}",
        )
        return True

    async def _fail(self, reason: str, detail: str, *, notify: bool = False) -> None:
        if self._fail_now(reason, detail, notify=notify) and self._closer is not None:
            await self._closer

    async def _close_after_flush(self, code: int, reason: str) -> None:
        sender = self._sender
        if sender is not None and sender is not asyncio.current_task():
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(sender), _NOTICE_FLUSH_GRACE_S)
        with contextlib.suppress(Exception):
            await self._close_socket(code, reason)


def _new_message_id() -> str:
    return "R" + uuid.uuid4().hex


class InherentHub:
    """Creates and starts one :class:`InherentClient` per accepted v2 socket."""

    def __init__(  # noqa: PLR0913 — the identities and every injectable limit.
        self,
        sequencer: InherentViewSequencer,
        *,
        log_epoch: str,
        boot_id: str,
        adoption_deadline_s: float = SNAPSHOT_ADOPTION_DEADLINE_S,
        clock: Callable[[], float] = time.time,
        limits: FlowControlLimits | None = None,
        ack_stall_poll_s: float = ACK_STALL_POLL_S,
    ) -> None:
        """Bind the sequencer, the identities every frame carries and the D11 limits."""
        self._sequencer = sequencer
        self._log_epoch = log_epoch
        self._boot_id = boot_id
        self._adoption_deadline_s = adoption_deadline_s
        self._clock = clock
        self._limits = limits or FlowControlLimits()
        self._ack_stall_poll_s = ack_stall_poll_s

    async def attach(self, session: V2Session) -> InherentClient:
        """Take over a hello-completed socket and start its snapshot handoff."""
        client = InherentClient(
            sequencer=self._sequencer,
            connection_id=session.connection_id,
            send_text=session.send_text,
            close=session.close,
            log_epoch=self._log_epoch,
            boot_id=self._boot_id,
            adoption_deadline_s=self._adoption_deadline_s,
            clock=self._clock,
            limits=self._limits,
            ack_stall_poll_s=self._ack_stall_poll_s,
        )
        client.start()
        return client


@dataclass(frozen=True)
class InherentViewWiring:
    """What ``serve_inherent`` plugs into the v2 route and its watcher list."""

    hub: InherentHub
    sequencer: InherentViewSequencer
    tasks: tuple[asyncio.Task[None], ...]

    @property
    def attach_client(self) -> Callable[[V2Session], Awaitable[InherentClient]]:
        """The ``InherentV2Deps.attach_client`` binding."""
        return self.hub.attach


def _v2_sequencer_block(config: Mapping[str, Any]) -> Mapping[str, Any] | None:
    realtime = config.get("realtime")
    inherent = realtime.get("inherent") if isinstance(realtime, Mapping) else None
    block = inherent.get("v2_sequencer") if isinstance(inherent, Mapping) else None
    return block if isinstance(block, Mapping) else None


def inherent_v2_sequencer_enabled(config: Mapping[str, Any]) -> bool:
    """Resolve ``realtime.inherent.v2_sequencer.enabled`` with its parent rule.

    The switch additionally requires ``realtime.enabled``; an invalid
    combination downgrades once, with one warning, to the v1-only wire.
    """
    block = _v2_sequencer_block(config)
    if block is None or block.get("enabled") is not True:
        return False
    realtime = config["realtime"]
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


def inherent_flow_control_limits(config: Mapping[str, Any]) -> FlowControlLimits:
    """Resolve ``realtime.inherent.v2_sequencer.flow_control``; absent keys keep D11's defaults."""
    block = _v2_sequencer_block(config)
    flow = block.get("flow_control") if block is not None else None
    if not isinstance(flow, Mapping):
        return FlowControlLimits()
    names = [f.name for f in fields(FlowControlLimits)]
    return FlowControlLimits(**{name: flow[name] for name in names if name in flow})


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
    hub = InherentHub(
        sequencer,
        log_epoch=log_epoch,
        boot_id=boot_id,
        limits=inherent_flow_control_limits(runtime.config),
    )
    return InherentViewWiring(hub=hub, sequencer=sequencer, tasks=tasks)


__all__ = [
    "ACK_STALL_POLL_S",
    "SNAPSHOT_ADOPTION_DEADLINE_S",
    "FlowControlLimits",
    "InherentClient",
    "InherentHub",
    "InherentViewWiring",
    "inherent_flow_control_limits",
    "inherent_v2_sequencer_enabled",
    "start_inherent_view",
]
