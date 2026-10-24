"""The watchers that carry a finished response out: to the app, the speaker and the phone."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.llm import failure_reason
from jarvis.decision.pre_route import TYPED_CHANNEL
from jarvis.runtime.daemon.polling import DEFAULT_POLL_INTERVAL_S, WATCHER_RETRY_S
from jarvis.runtime.phone_voice import UnplayedAnswers
from jarvis.runtime.tool_status import EVENT_TYPES as TOOL_STATUS_EVENT_TYPES
from jarvis.runtime.tool_status import ToolStatus
from jarvis.shared import Event, lang
from jarvis.shared.steps import EVENT_TYPES as STEP_EVENT_TYPES
from jarvis.shared.steps import StepFold
from jarvis.state import reactions
from jarvis.state.event_log import (
    PHONE_VOICE_CHANNEL,
    emit_event,
    fetch_events_after,
    latest_id,
    open_runtime_event_log,
    recent_turns_of_device,
    turn_intent_channel,
    turn_origin,
)
from jarvis.state.turn_steps import answer_lists, answer_steps
from jarvis.surface import terminal_voice

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime
    from jarvis.surface.inherent_output import InherentBroadcaster
    from jarvis.surface.voice_media.pipeline import StreamingTTSPipeline

LOGGER = logging.getLogger(__name__)


# --- ADR-0009 D4 "System turns are silent, enforced" ------------------------
#
# The daemon streams EVERY turn (``streaming_enabled=True``), so both
# response consumers below see every turn's open/chunk/emitted triple
# regardless of where L3 routed it. Step 8 gives them the L3 verdict:
# ``attention_channel`` now rides the ``surface.response_open`` header
# (ADR-0009 §4 registry amendment), which is the one event that arrives
# BEFORE any chunk — the only place a turn can be dropped before it is
# spoken.
#
# TTS suppression set. A supervisor-sweep orphan closure at 3am drives a
# system turn whose Limitation routes to ``queue_review`` (the ADR-0002
# Limitation-routing amendment); ``silent_log`` is the other non-speaking
# verdict ``attention_policy`` can return. Neither channel lists a voice
# surface in ``ATTENTION_CHANNEL_TO_SURFACES``, so feeding their chunks
# to TTS contradicts the routing table and spec §3.2.5 安静优先.
#
# ``gpt_live`` is the odd one out: it is an INTENT channel (where the turn was
# submitted from), not an attention channel, and it is matched against a
# different payload field — see :func:`drop_for_silent_channel`. ADR-0016 D8:
# while a Live session owns the speaker, the local chain must not synthesize
# that turn at all. It is deliberately absent from the broadcaster set below,
# because Resonance still shows the full answer. ``cli_stdin`` (a turn typed
# into the talk field) is the same kind of label and is silent for the same
# reason: ADR 0181, only words he spoke are answered aloud. ``phone_voice`` (ADR 0209) is
# spoken, but by the phone's own media actor: the Mac's speaker and a terminal's leave it alone.
_TEXT_ONLY_CHANNEL: Final = "text_only"
"""ADR 0269: the label a spoken turn takes once he leaves voice mode before its answer starts."""

TTS_SILENT_CHANNELS: frozenset[str] = frozenset(
    {
        "queue_review", "silent_log", "badge_card", "gpt_live", TYPED_CHANNEL,
        PHONE_VOICE_CHANNEL, _TEXT_ONLY_CHANNEL,
    },
)

# WS-broadcaster suppression set — deliberately NARROWER than the TTS
# set, and this asymmetry is load-bearing:
#
# - ``silent_log`` maps to ``()`` in ``ATTENTION_CHANNEL_TO_SURFACES``:
#   no physical surface at all. Dropping its envelopes is exactly what
#   aligns the wire with the routing table.
# - ``queue_review`` maps to ``("cli_stdout",)`` — it is a TEXT channel:
#   the verdict for reconciliation terminals and needs-review worker
#   reports (jarvis/decision/gates.py ``attention_policy``; an ordinary
#   user utterance defaults to ``voice_notify``). The daemon passes
#   ``available_surfaces=frozenset()``, so this WS is the substitute for
#   that ``cli_stdout``. Suppressing it would blank the Inherent text
#   surface for those turns and hang ADR-0009 D2's forwarding CLI until
#   its 120s timeout (exit 4).
#
# "Silent" in D4 means no audio, not no text: the queue_review turn
# still lands on the card so Allen can read it when he comes back —
# which is what "queue for review" means.
_BROADCAST_SILENT_CHANNELS: frozenset[str] = frozenset({"silent_log"})

_WAIT_LINES_REMEMBERED: Final = 256
"""How many wait-line response ids the response watcher keeps before it starts over."""

# The rows that close a response: its render, or its run's terminal.
_RESPONSE_TERMINAL_TYPES: frozenset[str] = frozenset(
    {"surface.response_emitted", "response.cancelled", "response.failed"},
)

# The rows the response watcher forwards: the three Step-2 Inherent response
# event types, plus the two ends that produce no answer. They share ONE cursor:
# asyncio's wakeup order does NOT guarantee that sibling watchers (one per type)
# would fire in L2-insertion order, and the row id orders them — D16 race
# elimination by construction.
_RESPONSE_WATCHER_TYPES: Final[tuple[str, ...]] = (
    "surface.response_open",
    "surface.response_chunk",
    "surface.response_emitted",
    "turn.failed",
    "response.cancelled",
)


def drop_for_silent_channel(  # noqa: PLR0913 - two verdict sources, one bookkeeping set
    event: Event,
    *,
    turn_id: str,
    silent_turns: set[str],
    silent_channels: frozenset[str],
    consumer: str,
    intent_channel: str | None = None,
) -> bool:
    """True when ``event`` belongs to a turn ``consumer`` must not deliver.

    ADR-0009 D4. Shared by :func:`response_watcher` and
    :func:`tts_watcher`, which differ only in their ``silent_channels``
    set (see the two constants' comments for why the sets differ).

    The L3 ``attention_channel`` verdict rides the ``surface.response_open``
    header only (ADR-0009 §4), so a suppressed turn id is remembered
    across its chunks and forgotten when its ``surface.response_emitted``
    row (or the run's ``response.cancelled`` / ``response.failed``
    terminal) arrives — the whole open/chunk*/emitted triple is dropped or
    none of it is.

    ``intent_channel`` is the second, parallel source of that verdict: the
    caller's :func:`turn_intent_channel` lookup, supplied on the open only.
    Either label matching ``silent_channels`` suppresses the turn, so a
    consumer can silence a turn by its L3 routing verdict (ADR-0009 D4) or by
    where it was submitted from (ADR-0016 D8) through one mechanism. Callers
    that pass nothing keep the header-only behaviour exactly.

    A missing (or non-string) ``attention_channel`` is deliberately NOT
    silent: the pre-Step-8 behaviour — deliver and speak — stays the
    default, so an emitter that predates the field (legacy rows, direct
    ``emit_event`` callers) never loses its output. Suppression is
    opt-in by an explicit channel label.
    """
    if event.type == "surface.response_open":
        channel = next(
            (
                label
                for label in (event.payload.get("attention_channel"), intent_channel)
                if isinstance(label, str) and label in silent_channels
            ),
            None,
        )
        if channel is None:
            return False
        silent_turns.add(turn_id)
        LOGGER.info(
            "%s: turn_id=%s routed to %r — suppressed for this consumer (ADR-0009 D4).",
            consumer,
            turn_id,
            channel,
        )
        return True
    if turn_id not in silent_turns:
        return False
    if event.type in {"surface.response_emitted", "response.cancelled", "response.failed"}:
        silent_turns.discard(turn_id)
    return True


_TURN_DEVICES_REMEMBERED: Final = 4096


def turn_devices(conn: sqlite3.Connection) -> Callable[[str], str | None]:
    """``turn_id -> device`` for the broadcaster (ADR 0222), read once per turn on the loop thread.

    The device is :func:`~jarvis.state.event_log.turn_origin`'s: the name the turn's opening row
    was written under, ``None`` for a turn with no opening row. That row precedes every row of the
    turn's answer, so a turn read once is read for good.
    """
    known: dict[str, str | None] = {}

    def device(turn_id: str) -> str | None:
        if turn_id not in known:
            if len(known) >= _TURN_DEVICES_REMEMBERED:
                known.clear()
            known[turn_id] = turn_origin(conn, turn_id)[1]
        return known[turn_id]

    return device


def log_watcher_death(task: asyncio.Task[None]) -> None:
    """A daemon-lifetime task that ends with an exception is an outage until restart; log it."""
    if not task.cancelled() and task.exception() is not None:
        LOGGER.error("%s stopped; restart the daemon", task.get_name(), exc_info=task.exception())


def emit_turn_failed(
    conn: sqlite3.Connection,
    *,
    intent_event: Event,
    exc: BaseException,
) -> None:
    """Emit the watcher-level ``turn.failed`` audit event (ADR-0003 D9 F3).

    ``trigger_event_id`` is the originating ``surface.user_intent``
    event's ``event_uid`` so a future replay can join the failure back
    to the request that produced it; ``reason`` is what the desktop
    shows (a missing key, a refused key, no quota, ...).

    Never raises: its callers are the catch-alls of the daemon's input loops,
    and a failed write here must not end the loop that called it.
    """
    turn_id = intent_event.payload.get("turn_id", "<unknown>")
    try:
        emit_event(
            conn,
            type="turn.failed",
            payload={
                "turn_id": str(turn_id),
                "exception_repr": repr(exc),
                "trigger_event_id": intent_event.event_uid,
                "reason": failure_reason(exc),
            },
            # ADR-0016 D5: correlated so a Live delegation's lookup by turn finds it.
            correlation={"turn_id": str(turn_id)},
            ts_epoch_ms=int(time.time() * 1000),
        )
    except Exception:
        LOGGER.exception("turn.failed could not be written for turn_id=%s", turn_id)


async def response_watcher(
    runtime: JarvisRuntime,
    broadcaster: InherentBroadcaster,
    *,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    voiced: bool = True,
) -> None:
    """Background task: forward every new response event to the broadcaster.

    Single watcher with ONE cursor (``WHERE type IN
    ('surface.response_open', 'surface.response_chunk',
    'surface.response_emitted', 'turn.failed', 'response.cancelled')
    ORDER BY id``) so the event types are dispatched in their
    L2-insertion order. Per ADR-0003
    Step 2 D16: three sibling watchers would race against asyncio's
    wakeup order and could send ``op:append`` before ``op:open``;
    single-cursor + monotonic SQLite row id eliminates the race by
    construction.

    Dispatch by ``event.type``:

    - ``surface.response_open``    -> :meth:`InherentBroadcaster.broadcast_open`
    - ``surface.response_chunk``   -> :meth:`InherentBroadcaster.broadcast_chunk`
    - ``surface.response_emitted`` -> :meth:`InherentBroadcaster.broadcast_done`
    - ``turn.failed`` / ``response.cancelled`` -> ``broadcast_op("failed" | "cancelled")``

    The broadcaster handles per-envelope wire translation and the F4 /
    F5 failure modes (per-client send failure isolation + no-clients
    warning).

    Cursor anchoring mirrors :func:`_intent_pump_watcher` — events
    that landed BEFORE this watcher started are not replayed.

    Channel filter (ADR-0009 D4): a turn whose ``surface.response_open``
    header declares a channel in :data:`_BROADCAST_SILENT_CHANNELS` is
    dropped whole — open, chunks and done — so the wire never carries a
    turn the L3 routing table gives no surface to. The turn id is
    remembered from the open until its ``emitted`` closes it, because
    only the open header carries the channel. See the constant's
    comment for why this set is narrower than the TTS one.

    ``voiced=False`` (no speech pipeline this boot) follows every
    ``done`` with ``voice spoken {output_outcome: "no_voice"}``: a surface
    ends a turn's speaking face on ``spoken``, and nothing else would send it.
    A turn typed on :data:`TYPED_CHANNEL` (ADR 0181), or spoken and then left to
    answer in text (ADR 0269), is ended with
    ``suppressed`` instead, as :func:`tts_watcher` ends it where a speaker
    exists: ``no_voice`` is the word a voice terminal drops, since its own
    player will say ``spoken``, and that player is never given a typed turn.
    Every other channel keeps ``no_voice``, a phone's included.
    """
    after_id = latest_id(runtime.conn)
    silent_turns: set[str] = set()
    typed_turns: set[str] = set()  # the turns no player is given, as the TTS watcher drops them
    wait_lines: set[str] = set()  # response ids of wait lines, whose cancel is not the turn's
    LOGGER.info("response_watcher started (after_id=%d)", after_id)
    try:
        while True:
            try:
                new_events = fetch_events_after(
                    runtime.conn,
                    after_id=after_id,
                    event_types=_RESPONSE_WATCHER_TYPES,
                )
                for row_id, ev in new_events:
                    after_id = max(after_id, row_id)
                    turn_id = str(ev.payload.get("turn_id", ""))
                    if drop_for_silent_channel(
                        ev,
                        turn_id=turn_id,
                        silent_turns=silent_turns,
                        silent_channels=_BROADCAST_SILENT_CHANNELS,
                        consumer="response_watcher",
                    ):
                        continue
                    typed = drop_for_silent_channel(
                        ev,
                        turn_id=turn_id,
                        silent_turns=typed_turns,
                        silent_channels=frozenset({TYPED_CHANNEL, _TEXT_ONLY_CHANNEL}),
                        consumer="response_watcher (speech)",
                        intent_channel=(
                            _TEXT_ONLY_CHANNEL
                            if runtime.response_runs is not None
                            and runtime.response_runs.turn_text_only(turn_id)
                            else turn_intent_channel(runtime.conn, turn_id)
                        )
                        if ev.type == "surface.response_open"
                        else None,
                    )
                    response_id = ev.payload.get("response_id")
                    is_line = ev.payload.get("phase") == "commentary"
                    if ev.type == "surface.response_open" and is_line:
                        if len(wait_lines) > _WAIT_LINES_REMEMBERED:
                            wait_lines.clear()
                        wait_lines.add(str(response_id))
                    elif ev.type == "response.cancelled" and response_id in wait_lines:
                        continue  # a wait line taken back says nothing about the turn (ADR 0121)
                    await _broadcast_response_event(
                        broadcaster, ev, turn_id=turn_id, voiced=voiced, typed=typed,
                    )
            except Exception:
                LOGGER.exception("response_watcher: poll failed at after_id=%d; retrying", after_id)
                await asyncio.sleep(WATCHER_RETRY_S)
                continue
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        LOGGER.info("response_watcher cancelled")
        raise


async def _broadcast_response_event(
    broadcaster: InherentBroadcaster,
    ev: Event,
    *,
    turn_id: str,
    voiced: bool,
    typed: bool,
) -> None:
    """Put one response row on the wire; see :func:`response_watcher`."""
    if ev.type == "surface.response_open":
        await broadcaster.broadcast_open(ev)
    elif ev.type == "surface.response_chunk":
        await broadcaster.broadcast_chunk(ev)
    elif ev.type == "surface.response_emitted":
        await broadcaster.broadcast_done(ev)
        if not voiced:
            await broadcaster.broadcast_voice(
                "spoken", turn_id=turn_id, output_outcome="suppressed" if typed else "no_voice",
            )
    elif ev.type == "turn.failed":
        reason = str(ev.payload.get("reason") or "error")
        await broadcaster.broadcast_op(
            "failed", turn_id=turn_id, reason=reason, message=lang.t(f"failure.{reason}"),
        )
    else:  # response.cancelled
        await broadcaster.broadcast_op("cancelled", turn_id=turn_id)


async def tool_status_watcher(
    runtime: JarvisRuntime,
    broadcaster: InherentBroadcaster,
    *,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: tell the surface which tool is really running (ADR 0115).

    One cursor over the action and turn-end rows, anchored like
    :func:`response_watcher`. Whenever the line to show changes, one
    ``{"op": "tool", "payload": {"turn_id", "label"}}`` envelope goes out; an
    empty label clears it, and goes out only once that turn is over (the line
    stays between two tools, so the surface never flickers). The label is fixed
    text from the language table.
    """
    after_id = latest_id(runtime.conn)
    status = ToolStatus()
    sent = ("", "")
    LOGGER.info("tool_status_watcher started (after_id=%d)", after_id)
    try:
        while True:
            try:
                for row_id, ev in fetch_events_after(
                    runtime.conn, after_id=after_id, event_types=TOOL_STATUS_EVENT_TYPES,
                ):
                    after_id = max(after_id, row_id)
                    status.feed(ev, time.monotonic())
                shown = status.shown(time.monotonic())
                if shown:
                    turn_id, label = shown.turn_id, lang.t(shown.key)
                elif status.over(sent[0]):
                    turn_id, label = sent[0], ""  # the clear goes out once the turn is over
                else:
                    turn_id, label = sent  # between two tools the line stays
                if (turn_id, label) != sent:
                    sent = (turn_id, label)
                    if broadcaster.has_clients:
                        await broadcaster.broadcast_op("tool", turn_id=turn_id, label=label)
            except Exception:
                LOGGER.exception("tool_status_watcher: poll failed at %d; retrying", after_id)
                await asyncio.sleep(WATCHER_RETRY_S)
                continue
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        LOGGER.info("tool_status_watcher cancelled")
        raise


async def tts_watcher(  # noqa: C901 - ordered durable dispatch FSM
    *,
    conn: sqlite3.Connection,
    pipeline: StreamingTTSPipeline,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    broadcaster: InherentBroadcaster | None = None,
    text_only: Callable[[str], bool] | None = None,
) -> None:
    """Background task: feed every surface.response_* row to the streaming media owner.

    Single cursor per ADR-0005 §5.3 + §4.3 row ``tts_watcher``. Runs
    in parallel with :func:`response_watcher`; both are read-only polls on
    the same event stream so the D16 race-elimination argument that motivates
    a single in-order cursor for the broadcaster applies here verbatim — TTS
    must observe ``open`` before ``chunk`` and ``chunk`` before ``emitted``,
    which the single-cursor + monotonic SQLite row id gives us by construction.

    Every row of ``surface.response_open`` / ``surface.response_chunk`` /
    ``surface.response_emitted`` / ``response.cancelled`` / ``response.failed`` goes to
    :meth:`~jarvis.surface.voice_media.pipeline.StreamingTTSPipeline.submit_event`, which only
    enqueues owned work, so dispatch stays on the event-loop thread without blocking.
    When the media owner reports ``overloaded`` the cursor stays on that row and the
    Event Log, the durable queue, delivers it again.

    Per-event dispatch is wrapped in a catch-all: a misbehaving TTS pipeline
    (e.g. a MiniMax WebSocket drop) must NOT crash the watcher because that would
    stall every subsequent turn. We log a warning and retry the row; the surface
    broadcaster keeps running on the parallel cursor, so the UI is unaffected.

    Channel filter (ADR-0009 D4 — "System turns are silent, enforced"):
    a turn whose ``surface.response_open`` header declares a channel in
    :data:`TTS_SILENT_CHANNELS`, or which was submitted on one (ADR-0016
    D8 — a ``gpt_live`` delegation), never reaches the pipeline at all —
    its chunks are dropped, and its ``emitted`` only clears the bookkeeping.
    Both labels are read once, at the open, so the suppressed turn ids are
    remembered until their ``emitted`` row closes them. This is a filter, not a
    switch: a ``voice_notify`` turn streams exactly as before. The row that closes a
    suppressed turn is announced as ``voice spoken {output_outcome:
    "suppressed"}`` on ``broadcaster``: the surface shows the turn's text
    with a speaking face until ``spoken``, which no player would send.

    Cancellation: re-raises :class:`asyncio.CancelledError` so the daemon
    shutdown path (Task 19) can await the watcher cleanly.
    """
    after_id = pipeline.boot_high_water_event_log_id
    silent_turns: set[str] = set()
    LOGGER.info("tts_watcher started (after_id=%d)", after_id)
    try:
        while True:
            try:
                new_events = fetch_events_after(
                    conn,
                    after_id=after_id,
                    event_types=(
                        "surface.response_open",
                        "surface.response_chunk",
                        "surface.response_emitted",
                        "response.cancelled",
                        "response.failed",
                    ),
                )
            except Exception:
                # A failed read must not end speech until a restart; retry from the same row.
                LOGGER.exception("tts_watcher: poll failed at after_id=%d; retrying", after_id)
                await asyncio.sleep(WATCHER_RETRY_S)
                continue
            for row_id, ev in new_events:
                advance_cursor = True
                try:
                    turn_id = str(ev.payload.get("turn_id", ""))
                    if drop_for_silent_channel(
                        ev,
                        turn_id=turn_id,
                        silent_turns=silent_turns,
                        silent_channels=TTS_SILENT_CHANNELS,
                        consumer="tts_watcher",
                        intent_channel=(
                            _TEXT_ONLY_CHANNEL
                            if text_only is not None and text_only(turn_id)
                            else turn_intent_channel(conn, turn_id)
                        )
                        if ev.type == "surface.response_open"
                        else None,
                    ):
                        if broadcaster is not None and ev.type in _RESPONSE_TERMINAL_TYPES:
                            await broadcaster.broadcast_voice(
                                "spoken", turn_id=turn_id, output_outcome="suppressed",
                            )
                        after_id = max(after_id, row_id)
                        continue
                    outcome = await pipeline.submit_event(
                        row_id=row_id,
                        event=ev,
                        origin="watcher",
                    )
                    if outcome.status == "overloaded":
                        # The Event Log is the durable queue. Retain the
                        # cursor so even the final committed row is retried
                        # after bounded media capacity becomes available.
                        advance_cursor = False
                        break
                except Exception as exc:  # noqa: BLE001 — log + retry the row; TTS must not crash watcher.
                    advance_cursor = False
                    LOGGER.warning(
                        "tts_watcher: dispatch raised on %s turn_id=%s: %r",
                        ev.type,
                        ev.payload.get("turn_id"),
                        exc,
                    )
                finally:
                    if advance_cursor:
                        after_id = max(after_id, row_id)
                if not advance_cursor:
                    break
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        LOGGER.info("tts_watcher cancelled")
        raise


async def phone_ends_watcher(
    conn: sqlite3.Connection,
    owner_boot: Callable[[str], int | None],
    *,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: end the phone voice answers that no actor plays (ADR 0222).

    One cursor over the response rows, anchored at the log's end like the other watchers: an
    answer from before this boot is the boot's reconciliation, not news. Each row goes to
    :class:`~jarvis.runtime.phone_voice.UnplayedAnswers`, which writes a
    ``surface.speech_dropped`` for an answer that ends while its device's actor does not hold
    it. A row that cannot be taken is retried from the same place.
    """
    ends = UnplayedAnswers(owner_boot)
    after_id = latest_id(conn)
    LOGGER.info("phone_ends_watcher started (after_id=%d)", after_id)
    try:
        while True:
            try:
                for row_id, event in fetch_events_after(
                    conn, after_id=after_id, event_types=terminal_voice.RESPONSE_ROW_TYPES,
                ):
                    ends.observe(conn, row_id, event)
                    after_id = max(after_id, row_id)
            except Exception:
                LOGGER.exception("phone_ends_watcher: failed at after_id=%d; retrying", after_id)
                await asyncio.sleep(WATCHER_RETRY_S)
                continue
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        LOGGER.info("phone_ends_watcher cancelled")
        raise


class _SpokenCursor:
    """One voice terminal's place in the log: the answer rows after it that are meant to be spoken.

    A turn on a silent channel is dropped here, as :func:`tts_watcher` drops it for the local
    speaker, so a terminal never hears what this daemon would not.
    """

    def __init__(
        self, conn: sqlite3.Connection, after: int, text_only: Callable[[str], bool] | None,
    ) -> None:
        self._conn = conn
        self._after = after
        self._text_only = text_only
        self._silent: set[str] = set()

    def poll(self) -> terminal_voice.RowBatch:
        rows = fetch_events_after(
            self._conn, after_id=self._after, event_types=terminal_voice.RESPONSE_ROW_TYPES,
        )
        spoken: list[tuple[int, Event]] = []
        for row_id, event in rows:
            self._after = max(self._after, row_id)
            turn_id = str(event.payload.get("turn_id", ""))
            if not drop_for_silent_channel(
                event,
                turn_id=turn_id,
                silent_turns=self._silent,
                silent_channels=TTS_SILENT_CHANNELS,
                consumer="terminal_speech",
                intent_channel=(
                    _TEXT_ONLY_CHANNEL
                    if self._text_only is not None and self._text_only(turn_id)
                    else turn_intent_channel(self._conn, turn_id)
                )
                if event.type == "surface.response_open"
                else None,
            ):
                spoken.append((row_id, event))
        return self._after, spoken


class SpokenRows:
    """The brain's answer rows, for :class:`terminal_voice.BrainVoice` (ADR 0172)."""

    def __init__(
        self, conn: sqlite3.Connection, text_only: Callable[[str], bool] | None = None,
    ) -> None:
        """Read the answer rows from ``conn``; ``text_only`` says which turns are not spoken."""
        self._conn = conn
        self._text_only = text_only

    def high_water(self) -> int:
        """The newest row id: a terminal starts its cursor here, so it hears nothing older."""
        return latest_id(self._conn)

    def cursor(self, after: int) -> _SpokenCursor:
        """A cursor over the spoken answer rows after row ``after``."""
        return _SpokenCursor(self._conn, after, self._text_only)


class _PhoneCursor:
    """One phone's place in the log: the answer rows of the turns it opened (ADR 0209).

    A turn is the device's when its opening row (``surface.user_intent`` or
    ``utterance.received``) was written under the device's name. ``spoken_only`` keeps those
    it spoke, on ``phone_voice``: the phone's media actor is fed from such a cursor, the
    phone's ``row`` stream from one without. The stream also carries the turn's question cards
    (ADR 0215); the actor's rows stay the answer's.
    """

    _REMEMBERED = 4096

    def __init__(
        self, conn: sqlite3.Connection, after: int, device: str, *, spoken_only: bool,
    ) -> None:
        self._conn = conn
        self._after = after
        self._device = device
        self._spoken_only = spoken_only
        self._turns: dict[str, bool] = {}

    def mine(self, turn_id: str) -> bool:
        known = self._turns.get(turn_id)
        if known is None:
            channel, node = turn_origin(self._conn, turn_id)
            known = node == self._device and (
                not self._spoken_only or channel == PHONE_VOICE_CHANNEL
            )
            if len(self._turns) >= self._REMEMBERED:
                self._turns.clear()
            self._turns[turn_id] = known
        return known

    def poll(self) -> terminal_voice.RowBatch:
        rows = fetch_events_after(
            self._conn, after_id=self._after,
            event_types=(
                terminal_voice.RESPONSE_ROW_TYPES if self._spoken_only
                else terminal_voice.PHONE_ROW_TYPES
            ),
        )
        mine: list[tuple[int, Event]] = []
        for row_id, event in rows:
            self._after = max(self._after, row_id)
            if self.mine(str(event.payload.get("turn_id", ""))):
                mine.append((row_id, event))
        return self._after, mine


class _PhoneStepCursor:
    """One phone's place in the action rows: the ``step`` frames of the turns it opened (ADR 0231).

    The proposal of an action names its turn, so only a proposal is checked against the device
    (:meth:`_PhoneCursor.mine`); the fold ignores every later row of an action it never kept.
    """

    def __init__(self, conn: sqlite3.Connection, after: int, device: str) -> None:
        self._after = after
        self._conn = conn
        self._fold = StepFold()
        self._own = _PhoneCursor(conn, after, device, spoken_only=False)

    def poll(self) -> tuple[int, list[tuple[int, dict[str, Any]]]]:
        frames: list[tuple[int, dict[str, Any]]] = []
        for row_id, event in fetch_events_after(
            self._conn, after_id=self._after, event_types=STEP_EVENT_TYPES,
        ):
            self._after = max(self._after, row_id)
            if event.type == "action.proposed" and not self._own.mine(
                str(event.payload.get("turn_id") or (event.correlation or {}).get("turn_id") or ""),
            ):
                continue
            frames.extend((row_id, step.frame()) for step in self._fold.feed(event))
        return self._after, frames


class _PhoneReactionCursor:
    """The reaction events: every phone's, whichever device opened the turn (ADR 0257)."""

    def __init__(self, conn: sqlite3.Connection, after: int) -> None:
        self._conn = conn
        self._after = after

    def poll(self) -> tuple[int, list[tuple[int, dict[str, Any]]]]:
        frames: list[tuple[int, dict[str, Any]]] = []
        for row_id, event in fetch_events_after(
            self._conn, after_id=self._after, event_types=(reactions.EVENT,),
        ):
            self._after = max(self._after, row_id)
            frames.append((row_id, {
                "type": "reaction", "target": event.payload["target"],
                "by": event.payload["by"], "emoji": event.payload["emoji"],
            }))
        return self._after, frames


class PhoneRows:
    """The host's answer rows by device, for :mod:`jarvis.surface.phone_link` (ADR 0209)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Read the answer rows from ``conn``."""
        self._conn = conn

    def high_water(self) -> int:
        """The newest row id: a phone starts its cursor here, so it gets nothing older."""
        return latest_id(self._conn)

    def cursor(self, device: str, after: int, *, spoken_only: bool = False) -> _PhoneCursor:
        """A cursor over the answer rows of the turns ``device`` opened, after row ``after``."""
        return _PhoneCursor(self._conn, after, device, spoken_only=spoken_only)

    def steps(self, device: str, after: int) -> _PhoneStepCursor:
        """A cursor over the ``step`` frames of the turns ``device`` opened, after row ``after``."""
        return _PhoneStepCursor(self._conn, after, device)

    def reactions(self, after: int) -> _PhoneReactionCursor:
        """A cursor over every phone's reaction events after row ``after``."""
        return _PhoneReactionCursor(self._conn, after)

    def recent_turns(self, device: str, limit: int) -> list[str]:
        """The last ``limit`` turns ``device`` opened, oldest first."""
        return recent_turns_of_device(self._conn, device, limit)


def add_steps(event_log: Path, rows: list[dict[str, object]]) -> None:
    """ADR 0231: put the steps of the turn on each of her answers in a page of the conversation.

    Her answer's record id is its ``surface.response_emitted`` row's uid, whose turn ran the
    tools. A log that cannot be read leaves the page without steps, never without rows.
    """
    ids = [str(row["id"]) for row in rows if row["source"] == "jarvis"]
    if not ids:
        return
    conn = open_runtime_event_log(event_log)
    try:
        found = answer_steps(conn, ids)
    except sqlite3.Error:
        LOGGER.warning("conversation: the steps of its answers could not be read", exc_info=True)
        return
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    for row in rows:
        row.update(found.get(str(row["id"]), {}))


def add_places_and_links(event_log: Path, rows: list[dict[str, object]]) -> None:
    """ADR 0279, 0391: put her answer's places and links on its row, so history redraws them.

    ``places`` and ``links`` are what the answer's ``surface.response_emitted`` carried; a row
    gets each key only when it had some. A log that cannot be read leaves the page without them,
    never without rows.
    """
    ids = [str(row["id"]) for row in rows if row["source"] == "jarvis"]
    if not ids:
        return
    conn = open_runtime_event_log(event_log)
    try:
        found = answer_lists(conn, ids)
    except sqlite3.Error:
        LOGGER.warning(
            "conversation: the places and links of its answers could not be read", exc_info=True,
        )
        return
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    for row in rows:
        row.update(found.get(str(row["id"]), {}))


def add_reactions(event_log: Path, rows: list[dict[str, object]]) -> None:
    """ADR 0257: put the reactions now on each message in a page of the conversation.

    A message is his or her row; every one gets ``reactions`` (``{allen?, jarvis?}``, empty when
    none). A log that cannot be read leaves the page without the field, never without rows.
    """
    ids = [str(row["id"]) for row in rows if row["source"] in ("allen", "jarvis")]
    if not ids:
        return
    conn = open_runtime_event_log(event_log)
    try:
        found = reactions.current(conn, ids)
    except sqlite3.Error:
        LOGGER.warning("conversation: its messages' reactions could not be read", exc_info=True)
        return
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    for row in rows:
        if row["source"] in ("allen", "jarvis"):
            row["reactions"] = found.get(str(row["id"]), {})
