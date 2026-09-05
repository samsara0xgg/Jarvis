"""The one ordered Inherent view sequencer — ADR-0014 D8/D9 (runtime).

``InherentViewSequencer`` is the only producer of durable v2 panel deltas.
It owns exactly what D9 assigns to the runtime — lifecycle, the scan
watermark, wake/drain, and client orchestration — and delegates the fold to
:class:`jarvis.state.inherent_view.InherentView` and the wire shape to
:mod:`jarvis.surface.inherent_presenter`.

The wake is level-triggered and cannot be lost (D9):

1. :meth:`notify` (the committed-bus subscriber, any thread) and the
   recovery timer both set one ``asyncio.Event``; neither carries a payload.
2. :meth:`drain` clears the flag, captures ``H = MAX(events.id)``, folds
   every relevant row ``scan_cursor < id <= H`` ascending, and only then sets
   ``scan_cursor = H`` — so the watermark advances across irrelevant rows too,
   and a coalesced or lost wake changes nothing but latency.
3. It repeats at once when the flag was set again or a fresh MAX exceeds the
   cursor.

Everything that touches the watermark, the global view or a client's
frontier runs synchronously on the loop thread: :meth:`drain`,
:meth:`begin_snapshot` and :meth:`complete_snapshot` contain no ``await``,
which is what makes them the D8 "actor commands".  Socket sends and the
ACK wait live in :mod:`jarvis.runtime.inherent_hub`, outside this actor.

Production wake today is the recovery timer at the v1 watcher cadence:
nothing publishes ``surface.response_*`` on the committed bus yet, and this
module deliberately does not change that (the render path is lane A's).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.inherent_view import InherentView, InherentViewCheckpoint
from jarvis.surface.inherent_presenter import delta_payload
from jarvis.surface.inherent_protocol import ServerEnvelope

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from jarvis.shared import Event
    from jarvis.state.committed_event_bus import CommittedEventBus

LOGGER = logging.getLogger("jarvis.runtime.inherent_view_sequencer")

DEFAULT_RECOVERY_INTERVAL_S: Final[float] = 0.01
CATCH_UP_FRAME_BUDGET: Final[int] = 256
"""D8 step 6: a post-ACK catch-up longer than this closes the client instead."""
CATCH_UP_BYTE_BUDGET: Final[int] = 1_048_576
"""D8 step 6: the same for the catch-up's total encoded UTF-8 bytes."""

_SELECT_HIGH_WATER_SQL: Final[str] = "SELECT COALESCE(MAX(id), 0) FROM events"
# The three Inherent-relevant types plus the D21 input row, bounded above so a
# drain projects exactly the rows its captured H covers (D9 step 2).  Static
# literal, no placeholders for the type list — same posture as the v1 watcher's
# SELECT.  ``surface.user_intent`` is fed to the fold for its
# ``turn_id -> source_client_request_id`` map only: the fold returns None for
# it, so it produces no frame and the wire is unchanged.
_SELECT_RESPONSE_ROWS_SQL: Final[str] = (
    "SELECT id, event_uid, type, ts_epoch_ms, payload_json FROM events "
    "WHERE id > ? AND id <= ? AND type IN ("
    "'surface.response_open', 'surface.response_chunk', 'surface.response_emitted', "
    "'surface.user_intent'"
    ") ORDER BY id ASC"
)


@dataclass
class ClientLane:
    """What the sequencer knows about one connection.

    ``live_frontier`` is None while the client is snapshotting or awaiting
    ACK — no durable delta reaches it (D8 step 4).  Once set to ``B`` by
    :meth:`InherentViewSequencer.complete_snapshot`, only rows with
    ``cursor > B`` fan out live; everything at or below came by catch-up.
    """

    connection_id: str
    enqueue: Callable[[str, int], None]
    #: Called with a detail when fan-out to this lane raised; the hub closes it.
    fail: Callable[[str], None] = field(default=lambda _detail: None)
    live_frontier: int | None = None


@dataclass(frozen=True)
class SnapshotStaging:
    """D8 step 1: the attempt token, its high-water ``H`` and the checkpoint at ``H``."""

    snapshot_id: str
    through_cursor: int
    checkpoint: InherentViewCheckpoint


class CatchUpBudgetExceededError(Exception):
    """A catch-up would exceed the frame or byte budget; never send part of it."""


class InherentViewSequencer:
    """Level-triggered drain of the Event Log into ordered ``view.delta`` frames."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        log_epoch: str,
        boot_id: str,
        recovery_interval_s: float = DEFAULT_RECOVERY_INTERVAL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind the loop-thread connection and the identities every frame carries.

        Args:
            conn: The daemon's Event Log connection; read only on the loop thread.
            log_epoch: The log's lineage id, stamped on every frame.
            boot_id: This process's boot id, stamped on every frame.
            recovery_interval_s: Cadence of the timer that sets the wake flag.
            clock: Wall-clock seconds for ``sent_at_ms``; injectable for tests.
        """
        self._conn = conn
        self._log_epoch = log_epoch
        self._boot_id = boot_id
        self._recovery_interval_s = recovery_interval_s
        self._clock = clock
        self._view = InherentView()
        self._scan_cursor = 0
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lanes: dict[str, ClientLane] = {}

    @property
    def scan_cursor(self) -> int:
        """The watermark: every row at or below it has been projected."""
        return self._scan_cursor

    @property
    def wake_pending(self) -> bool:
        """Whether a wake is set and not yet drained."""
        return self._wake.is_set()

    # --- wake ---------------------------------------------------------------

    def notify(self, event: Event) -> None:
        """Committed-bus subscriber: set the flag from any thread, carry nothing."""
        _ = event
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._wake.set)

    def wake(self) -> None:
        """Set the flag from the loop thread (the recovery timer's path)."""
        self._wake.set()

    # --- actor commands (synchronous on the loop thread) ------------------

    def drain(self) -> int:
        """Project every row up to the current high-water; return rows projected.

        Runs until neither a re-set flag nor a fresh ``MAX(events.id)`` is
        pending, so a wake that arrives mid-drain is never lost (D9 step 5).
        """
        projected = 0
        while True:
            self._wake.clear()
            high = self._high_water()
            if high <= self._scan_cursor:
                if not self._wake.is_set():
                    return projected
                continue
            for cursor, event_uid, event_type, ts_epoch_ms, payload in self._rows(
                self._scan_cursor, high,
            ):
                transition = self._view.fold(
                    cursor=cursor,
                    event_uid=event_uid,
                    event_type=event_type,
                    ts_epoch_ms=ts_epoch_ms,
                    payload=payload,
                )
                if transition is None:
                    continue
                projected += 1
                frame_payload = delta_payload(transition)
                # A copy: a lane that hits backpressure detaches itself mid-loop.
                for lane in list(self._lanes.values()):
                    if lane.live_frontier is None or cursor <= lane.live_frontier:
                        continue
                    try:
                        frame = self._durable_frame(lane, cursor, event_uid, frame_payload)
                        lane.enqueue(frame, cursor)
                    except Exception:  # One lane's fault never stops the producer.
                        LOGGER.exception("inherent v2 fan-out to %s raised", lane.connection_id)
                        self.detach(lane)
                        lane.fail("fan-out raised")
            self._scan_cursor = high

    def begin_snapshot(self, lane: ClientLane) -> SnapshotStaging:
        """D8 step 1: drain through ``H``, register the lane snapshotting, checkpoint at ``H``."""
        self.drain()
        lane.live_frontier = None
        self._lanes[lane.connection_id] = lane
        high = self._scan_cursor
        return SnapshotStaging(
            snapshot_id="S" + uuid.uuid4().hex,
            through_cursor=high,
            checkpoint=self._view.checkpoint(through_cursor=high),
        )

    def complete_snapshot(self, lane: ClientLane, staging: SnapshotStaging) -> int:
        """D8 steps 5-6: fold ``(H, B]`` onto the client's checkpoint, enqueue, go live.

        ``B`` is captured here; the rows are re-read from the durable log
        because nothing was buffered for this client.  The catch-up is
        enqueued in full before ``live_frontier`` becomes ``B``, all without
        yielding, so no drain can interleave a later row ahead of it.

        Args:
            lane: The client whose snapshot ACK was verified.
            staging: What :meth:`begin_snapshot` returned for it.

        Returns:
            The number of catch-up frames enqueued.

        Raises:
            CatchUpBudgetExceededError: The replay would exceed the frame or byte budget.
        """
        if self._lanes.get(lane.connection_id) is not lane:
            return 0
        high = self._high_water()
        replay = InherentView.from_checkpoint(staging.checkpoint)
        frames: list[tuple[str, int]] = []
        total_bytes = 0
        for cursor, event_uid, event_type, ts_epoch_ms, payload in self._rows(
            staging.through_cursor, high,
        ):
            transition = replay.fold(
                cursor=cursor,
                event_uid=event_uid,
                event_type=event_type,
                ts_epoch_ms=ts_epoch_ms,
                payload=payload,
            )
            if transition is None:
                continue
            frame = self._durable_frame(lane, cursor, event_uid, delta_payload(transition))
            total_bytes += len(frame.encode("utf-8"))
            if len(frames) >= CATCH_UP_FRAME_BUDGET or total_bytes > CATCH_UP_BYTE_BUDGET:
                msg = (
                    f"catch-up for {lane.connection_id} exceeds the budget "
                    f"({CATCH_UP_FRAME_BUDGET} frames / {CATCH_UP_BYTE_BUDGET} bytes) "
                    f"between cursors {staging.through_cursor} and {high}"
                )
                raise CatchUpBudgetExceededError(msg)
            frames.append((frame, cursor))
        for frame, cursor in frames:
            lane.enqueue(frame, cursor)
        lane.live_frontier = high
        return len(frames)

    def detach(self, lane: ClientLane) -> None:
        """Forget a lane; idempotent."""
        if self._lanes.get(lane.connection_id) is lane:
            del self._lanes[lane.connection_id]

    # --- lifecycle ------------------------------------------------------------

    async def start(self, *, bus: CommittedEventBus | None) -> tuple[asyncio.Task[None], ...]:
        """Drain through the current high-water, then run the drain loop and timer.

        The startup drain completes before this returns, so no v2 client is
        accepted against an unprojected log (D9).

        Args:
            bus: The committed-event bus to subscribe the wake to, when present.

        Returns:
            The two background tasks the caller owns and cancels at shutdown.
        """
        self._loop = asyncio.get_running_loop()
        projected = self.drain()
        LOGGER.info(
            "inherent_view_sequencer started (scan_cursor=%d, projected=%d, bus=%s)",
            self._scan_cursor,
            projected,
            bus is not None,
        )
        unsubscribe = bus.subscribe(self.notify) if bus is not None else None
        return (
            asyncio.create_task(self._run(unsubscribe), name="inherent_view_sequencer"),
            asyncio.create_task(self._recovery_timer(), name="inherent_view_recovery_timer"),
        )

    async def _run(self, unsubscribe: Callable[[], None] | None) -> None:
        try:
            while True:
                await self._wake.wait()
                self.drain()
        except asyncio.CancelledError:
            LOGGER.info("inherent_view_sequencer cancelled")
            raise
        except Exception:
            LOGGER.exception("inherent_view_sequencer stopped on an unrecoverable error")
            raise
        finally:
            if unsubscribe is not None:
                unsubscribe()

    async def _recovery_timer(self) -> None:
        while True:
            await asyncio.sleep(self._recovery_interval_s)
            self._wake.set()

    # --- internals --------------------------------------------------------

    def _high_water(self) -> int:
        row = self._conn.execute(_SELECT_HIGH_WATER_SQL).fetchone()
        return 0 if row is None else int(row[0])

    def _rows(
        self, after_id: int, through_id: int,
    ) -> list[tuple[int, str, str, int, dict[str, Any]]]:
        cursor = self._conn.execute(_SELECT_RESPONSE_ROWS_SQL, (after_id, through_id))
        return [
            (int(id_), str(event_uid), str(type_), int(ts_epoch_ms), json.loads(payload_json))
            for id_, event_uid, type_, ts_epoch_ms, payload_json in cursor
        ]

    def _durable_frame(
        self,
        lane: ClientLane,
        cursor: int,
        event_uid: str,
        payload: dict[str, Any],
    ) -> str:
        return ServerEnvelope(
            protocol_version=2,
            message_type="view.delta",
            message_id=event_uid,
            delivery_class="durable",
            connection_id=lane.connection_id,
            log_epoch=self._log_epoch,
            boot_id=self._boot_id,
            event_cursor=cursor,
            sent_at_ms=int(self._clock() * 1000),
            payload=payload,
        ).model_dump_json()


__all__ = [
    "CATCH_UP_BYTE_BUDGET",
    "CATCH_UP_FRAME_BUDGET",
    "DEFAULT_RECOVERY_INTERVAL_S",
    "CatchUpBudgetExceededError",
    "ClientLane",
    "InherentViewSequencer",
    "SnapshotStaging",
]
