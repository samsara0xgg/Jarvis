"""The media owner's playback lane: the responses it schedules, plays and ends, one at a time."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections import deque
from typing import TYPE_CHECKING

from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface.voice_ledger import ForegroundBusy, StalePlaybackGeneration
from jarvis.surface.voice_media.contract import MediaSubmitOutcome
from jarvis.surface.voice_media.registry import ActivePlaybackRegistry
from jarvis.surface.voice_media.response import (
    CHANNEL_TAG_RE,
    ActiveResponse,
    ResponseBuffer,
    ResponseChunk,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from jarvis.shared import Event
    from jarvis.surface.voice_ducking import SystemAudioDucker
    from jarvis.surface.voice_ledger import OutputTimelineSnapshot
    from jarvis.surface.voice_media.config import StreamingMediaConfig
    from jarvis.surface.voice_media.gain import OutputGain
    from jarvis.surface.voice_media.journal import PlaybackJournal
    from jarvis.surface.voice_media.power import PowerController
    from jarvis.surface.voice_media.response import MediaCommand
    from jarvis.surface.voice_media.say_fallback import SayFallback
    from jarvis.surface.voice_media.shutdown_clock import ShutdownClock
    from jarvis.surface.voice_media.spare import SpareSessions
    from jarvis.surface.voice_media.streamer import ResponseStreamer
    from jarvis.surface.voice_tts import AudioStreamPlayer

LOGGER = logging.getLogger(__name__)

_RESPONSE_TERMINAL_TYPES = frozenset({"response.cancelled", "response.failed"})
_RESPONSE_EVENT_TYPES = frozenset(
    {"surface.response_open", "surface.response_chunk", "surface.response_emitted"}
    | _RESPONSE_TERMINAL_TYPES,
)
_ANSWERED_GROUPS_LIMIT = 256
"""How many recent groups whose answer opened the media owner remembers."""

_TTS_SILENT_CHANNELS = frozenset({"queue_review", "silent_log", "badge_card"})


def _response_id(event: Event) -> str | None:
    value = event.payload.get("response_id")
    return value if isinstance(value, str) else None


class PlaybackLane:
    """The only writer of the playback lane's fields; the actor's thread only (ADR 0383).

    The registry, the event cursor, the responses, the queue behind the active one, the active,
    held and parked responses, the terminal debt and the isolation flag are written here and
    nowhere else, and every call runs on the actor's loop. Two reads come from another thread,
    as they did when the owner held the fields: ``isolated``, which the power controller's helper
    reads, and ``queued_after_drain``, which a test reads while the actor runs. Both are one
    attribute read. The owner keeps the thread, the queue of commands and the admission flags,
    and gives the lane a callable for each one it sets or clears; ``interrupt_active`` and
    ``suspend_for_sleep`` are the owner's to call, in the owner's order, at shutdown and sleep.
    """

    def __init__(  # noqa: PLR0913 - explicit ownership dependencies
        self,
        *,
        player: AudioStreamPlayer,
        config: StreamingMediaConfig,
        clock: ShutdownClock,
        journal: PlaybackJournal,
        gain: OutputGain,
        spare: SpareSessions,
        say_fallback: SayFallback,
        streamer: ResponseStreamer,
        power: PowerController,
        broadcaster: object | None,
        ducker: SystemAudioDucker | None,
        foreground_decision: Callable[[str, int, str, int], str] | None,
        silent_intent_channels: frozenset[str],
        speaks_turn: Callable[[sqlite3.Connection, str], bool] | None,
        boot_high_water_id: int,
        stop_accepting: Callable[[], None],
        set_output_active: Callable[[], None],
        clear_output_active: Callable[[], None],
        request_shutdown_deadline: Callable[[float], None],
    ) -> None:
        """Play through ``player`` and ``streamer``, recording through ``journal``.

        ``power`` is read for the suspended and shutdown states; ``foreground_decision`` is
        L3's verdict on a cross-group candidate (ADR 0008 D8); ``silent_intent_channels`` and
        ``speaks_turn`` (ADR 0209) say which turns the actor never speaks. The lane starts at
        ``boot_high_water_id``, the one replay boundary of this boot. ``stop_accepting``,
        ``set_output_active``, ``clear_output_active`` and ``request_shutdown_deadline`` write
        the owner's admission flag, its output bit and its shutdown deadline.
        """
        self._player = player
        self._config = config
        self._clock = clock
        self._journal = journal
        self._gain = gain
        self._spare = spare
        self._say_fallback = say_fallback
        self._streamer = streamer
        self._power = power
        self._broadcaster = broadcaster
        self._ducker = ducker
        self._foreground_decision = foreground_decision
        self._silent_intent_channels = silent_intent_channels
        self._speaks_turn = speaks_turn
        self._stop_accepting = stop_accepting
        self._set_output_active = set_output_active
        self._clear_output_active = clear_output_active
        self._request_shutdown_deadline = request_shutdown_deadline
        self._registry = ActivePlaybackRegistry(
            boot_high_water_id=boot_high_water_id,
        )
        self._lane_isolated = False
        self._terminal_debt: ActiveResponse | None = None
        self._event_cursor = boot_high_water_id
        self._responses: dict[str, ResponseBuffer] = {}
        self._after_drain: deque[ResponseBuffer] = deque()
        self._active: ActiveResponse | None = None
        # ADR 0053: while Allen's words are coming in no answer starts; it
        # parks here. A turn that ever started playing is never dropped.
        self._held = False
        # ADR 0054: the same parking while the audio devices are re-read.
        self._device_held = False
        self._parked: deque[ResponseBuffer] = deque()
        # ponytail: only grows, one short id per spoken turn; trim it if a
        # daemon ever runs for months.
        self._started_turns: set[str] = set()
        # ADR 0121: groups whose answer (any phase but commentary) has opened. A
        # wait line of such a group that has not started is dropped, never played
        # behind the answer. Insertion-ordered so the oldest can be trimmed.
        self._answered_groups: dict[str, None] = {}

    @property
    def active(self) -> ActiveResponse | None:
        """The response now on the player, if any; read-only for the owner and its helpers."""
        return self._active

    @property
    def isolated(self) -> bool:
        """True once the lane failed closed and will speak nothing again; any thread may read it."""
        return self._lane_isolated

    @property
    def boot_id(self) -> str:
        """The id this boot's playback sessions carry."""
        return self._registry.boot_id

    @property
    def boot_high_water_id(self) -> int:
        """The exact replay cutoff captured before the actor was built."""
        return self._registry.boot_high_water_id

    @property
    def queued_after_drain(self) -> int:
        """How many responses wait behind the active one; any thread may read it."""
        return len(self._after_drain)

    async def suspend_for_sleep(self) -> bool:
        """End the active response and drop the queued ones for a sleep; ``False`` if it stays."""
        if self._active is not None and not await self.interrupt_active(
            reason="system_sleep",
        ):
            return False
        self.discard_queued()
        self._responses.clear()
        return True

    def discard_queued(self) -> None:
        """Retire every response queued behind the active one, unsaid and unrecorded."""
        while self._after_drain:
            response = self._after_drain.popleft()
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)

    async def handle_command(
        self,
        command: MediaCommand,
    ) -> MediaSubmitOutcome:
        """Drain the committed rows through ``command``'s and answer what became of its own."""
        if command.row_id <= self._registry.boot_high_water_id:
            return MediaSubmitOutcome(
                "historical",
                command.event.event_uid,
                response_id=_response_id(command.event),
            )
        if command.row_id <= self._event_cursor:
            return MediaSubmitOutcome(
                "duplicate" if self._registry.has_seen(command.event.event_uid) else "invalid",
                command.event.event_uid,
                response_id=_response_id(command.event),
                detail="committed row already crossed by ordered media drain",
            )
        target_outcome: MediaSubmitOutcome | None = None
        while self._event_cursor < command.row_id:
            drained = False
            for row_id, event in self._journal.event_rows_through(
                self._event_cursor,
                command.row_id,
                self._config.event_drain_batch,
            ):
                drained = True
                self._event_cursor = row_id
                if event.type not in _RESPONSE_EVENT_TYPES:
                    continue
                outcome = await self._handle_event(row_id=row_id, event=event)
                if row_id == command.row_id:
                    if event.event_uid != command.event.event_uid:
                        return MediaSubmitOutcome(
                            "invalid",
                            command.event.event_uid,
                            detail="row id/event uid mismatch at media boundary",
                        )
                    target_outcome = outcome
            if not drained:
                break
        if target_outcome is not None:
            return target_outcome
        return MediaSubmitOutcome(
            "invalid",
            command.event.event_uid,
            response_id=_response_id(command.event),
            detail="hint row was absent or not a response event",
        )

    async def stop_foreground_output(self, response_id: str | None, reason: str) -> str:
        """Actor-owned stop of one named (or, for ``None``, the current) response's output."""
        active = self._active
        waiting = None if response_id is None else self._responses.get(response_id)
        if waiting is not None and (active is None or active.response is not waiting):
            # Named but not audible yet: queued behind another answer, parked
            # while Allen talks (ADR 0053) or still buffering. The surface
            # already shows it, so a stop there means it is never said.
            self._unschedule(waiting)
            self._responses.pop(waiting.response_id, None)
            self._registry.terminalize(waiting.response_id)
            self._record_dropped(waiting, reason=reason)
            self._broadcast_spoken(waiting, event_type="dropped")
            record_realtime_trace(
                "media_stop_foreground_waiting", response_id=waiting.response_id, reason=reason,
            )
            return "applied"
        if (
            active is None
            or (response_id is not None and active.response.response_id != response_id)
            or active.terminal_commit_pending
        ):
            if response_id is None:
                # The caller faded her out before asking; nothing is cut.
                self._gain.restore()
            record_realtime_trace(
                "media_stop_foreground_stale",
                response_id=response_id,
                active_response_id=None if active is None else active.response.response_id,
                terminal_commit_pending=active is not None and active.terminal_commit_pending,
            )
            return "stale"
        try:
            await self._gain.fade_out()
            if not await self.interrupt_active(reason=reason):
                return "uncertain"
        finally:
            self._gain.restore()
        # ADR-0006 D4: a user stop covers the queued speech of that group
        # too, and `_release_active(start_successor=False)` never advances
        # the lane, so an unpurged successor would sit unstartable.
        self._purge_after_drain()
        return "applied"

    async def pause(self, *, paused: bool) -> None:
        """Fade her out and hold the generation, or let it go on and fade her back in."""
        if paused:
            await self._gain.fade_out()
            self._player.pause_generation(paused=True)
            return
        self._player.pause_generation(paused=False)
        self._gain.restore()

    async def hold_output(self, *, held: bool) -> None:
        """Hold or release answer starts while Allen is talking (ADR 0053)."""
        self._held = held
        if held:
            await self._spare.prewarm()
        if held or self._device_held:
            return
        parked, self._parked = self._parked, deque()
        for response in parked:
            await self._schedule_response(response)

    async def hold_for_devices(self, *, held: bool) -> bool:
        """Hold or release answer starts around a device refresh; answers whether nothing plays."""
        self._device_held = held
        if not held and not self._held:
            parked, self._parked = self._parked, deque()
            for response in parked:
                await self._schedule_response(response)
        return self._active is None

    async def drop_unspoken(self, turn_ids: frozenset[str]) -> frozenset[str]:
        """Drop the responses of ``turn_ids`` that never started; answer the turns dropped."""
        dropped = turn_ids - self._started_turns
        for response in list(self._responses.values()):
            if response.turn_id in dropped:
                self._unschedule(response)
                self._responses.pop(response.response_id, None)
                self._registry.terminalize(response.response_id)
        record_realtime_trace("media_unspoken_dropped", turns=",".join(sorted(dropped)))
        return dropped

    async def _handle_event(  # noqa: C901, PLR0911 - closed three-event dispatch
        self,
        *,
        row_id: int,
        event: Event,
    ) -> MediaSubmitOutcome:
        outcome = self._registry.classify(row_id=row_id, event=event)
        if outcome.status != "accepted":
            return outcome
        payload = event.payload
        response_id = str(payload["response_id"])
        if self._power.suspended:
            self._registry.terminalize(response_id)
            self._responses.pop(response_id, None)
            return MediaSubmitOutcome(
                "closed",
                event.event_uid,
                response_id,
                "system power transition suspended output",
            )
        if event.type == "surface.response_open":
            # The drain replays every row up to the submitted one, including those of a
            # turn the watcher refused to submit, so the verdict is read here too: by
            # where the turn was submitted from, which the open header never carries.
            if payload.get("attention_channel") in _TTS_SILENT_CHANNELS or (
                self._silent_intent_channels
                and self._journal.intent_channel(str(payload.get("turn_id", "")))
                in self._silent_intent_channels
            ) or (
                self._speaks_turn is not None
                and not self._journal.speaks(str(payload.get("turn_id", "")), self._speaks_turn)
            ):
                self._registry.terminalize(response_id)
                return outcome
            response_group_raw = payload.get("response_group_id")
            if not isinstance(response_group_raw, str) or not response_group_raw:
                self._registry.terminalize(response_id)
                return MediaSubmitOutcome(
                    "invalid",
                    event.event_uid,
                    response_id,
                    "streaming response open requires stable response_group_id",
                )
            opened = ResponseBuffer(
                row_id=row_id,
                source_event_id=event.event_uid,
                response_id=response_id,
                response_group_id=response_group_raw,
                turn_id=str(payload.get("turn_id", "")),
                phase=str(payload.get("phase", "final")),
                channel=str(payload.get("channel", "both")),
                gate_mode=str(payload.get("required_gate_mode", "sentence")),
                stream=payload.get("kind") == "stream",
            )
            if not self._admit_open(opened):
                return outcome
            self._responses[response_id] = opened
            # Only a stream open's chunks are single gate-permitted segments.
            self._responses[response_id].incremental = (
                self._config.speak_from_segments and self._responses[response_id].stream
            )
            return outcome
        response = self._responses.get(response_id)
        if response is None:
            return MediaSubmitOutcome("unregistered", event.event_uid, response_id)
        if event.type == "surface.response_chunk":
            sequence = int(payload["sequence"])
            text = str(payload.get("text", ""))
            prospective = sum(
                len(item.raw_text.encode()) for item in response.chunks.values()
            )
            prospective += len(text.encode())
            if prospective > self._config.response_text_bytes:
                self._registry.terminalize(response_id)
                self._responses.pop(response_id, None)
                return MediaSubmitOutcome(
                    "overloaded",
                    event.event_uid,
                    response_id,
                    "bounded response text budget exceeded",
                )
            segment_hash_raw = payload.get("segment_hash")
            segment_hash = (
                segment_hash_raw
                if isinstance(segment_hash_raw, str) and segment_hash_raw
                else hashlib.sha256(text.encode()).hexdigest()
            )
            response.chunks[sequence] = ResponseChunk(
                sequence=sequence,
                raw_text=text,
                segment_hash=segment_hash,
                source_event_uid=event.event_uid,
            )
            await self._chunk_buffered(response, text)
            return outcome
        if event.type == "surface.response_emitted":
            await self._response_emitted(response, event)
        else:
            await self._response_terminal(response, event)
        return outcome

    async def _response_terminal(self, response: ResponseBuffer, event: Event) -> None:
        """L3 ended the run: stop an active playback, or drop a buffered one."""
        reason = event.type.replace(".", "_")
        active = self._active
        if active is not None and active.response is response:
            if active.terminal_commit_pending:
                # A media-side terminal is already in flight and owns this
                # response's whole exit: it will release the lease, advance the
                # queue and clear `_output_active` itself. Re-entering
                # `_interrupt_active` would cancel `_play_response` inside its
                # own `finally`, skipping `leave_output()` and leaving the
                # system output lease held, then emit a contradictory `spoken`
                # frame once the CAS answers `AlreadyTerminal`. Clearing
                # `_output_active` here is just as wrong: audio is still
                # playing until that terminal lands, and `is_speaking()` feeds
                # the wake listener.
                return
            await self.interrupt_active(reason=reason)
            self._purge_after_drain()
            self._clear_output_active()
            return
        self._unschedule(response)
        self._responses.pop(response.response_id, None)
        self._registry.terminalize(response.response_id)
        self._record_dropped(response, reason=reason)
        self._broadcast_spoken(response, event_type="dropped")

    async def _response_emitted(self, response: ResponseBuffer, event: Event) -> None:
        """Complete the buffer; a scheduled response finishes its own segment loop."""
        response.emitted = True
        response.source_event_id = event.event_uid
        response.append_voice_suffix(
            event.payload.get("voice_text"),
            source_event_uid=event.event_uid,
        )
        response.updated.set()
        if not response.scheduled:
            await self._schedule_response(response)

    async def _chunk_buffered(self, response: ResponseBuffer, text: str) -> None:
        """Wake the segment loop; a tag ends incremental speaking for this response."""
        response.updated.set()
        if response.stream and CHANNEL_TAG_RE.search(text) is not None:
            active = self._active
            if active is not None and active.response is response:
                response.tagged = True
            else:
                response.incremental = False
                self._unschedule(response)
        if response.incremental and not response.scheduled and response.speech_text():
            response.scheduled = True
            await self._schedule_response(response)

    def _purge_after_drain(self) -> None:
        """Drop every queued successor a foreground grant has displaced."""
        while self._after_drain:
            old = self._after_drain.popleft()
            self._registry.terminalize(old.response_id)
            self._responses.pop(old.response_id, None)
            # Its text may already be on screen: the surface ends its turn on `spoken`.
            self._record_dropped(old, reason="displaced")
            self._broadcast_spoken(old, event_type="dropped")

    def _admit_open(self, opened: ResponseBuffer) -> bool:
        """Apply ADR 0121 to a response's open row; ``False`` when it is dropped.

        A wait line whose group's answer is already out would queue behind the
        answer and be heard after all of it, so it never starts. An answer's open
        drops the group's wait lines that have not started.
        """
        if opened.phase != "commentary":
            self._answer_opened(opened.response_group_id)
            return True
        if opened.response_group_id not in self._answered_groups:
            return True
        self._registry.terminalize(opened.response_id)
        self._record_dropped(opened, reason="answer_started")
        return False

    def _answer_opened(self, group_id: str) -> None:
        """Note that this group's answer opened and drop its unplayed wait lines.

        A line already playing is left to finish: interrupting it would also
        purge the answer queued behind it.
        """
        self._answered_groups[group_id] = None
        while len(self._answered_groups) > _ANSWERED_GROUPS_LIMIT:
            del self._answered_groups[next(iter(self._answered_groups))]
        for line in list(self._responses.values()):
            if (
                line.phase == "commentary"
                and line.response_group_id == group_id
                and not (self._active is not None and self._active.response is line)
            ):
                self._unschedule(line)
                self._responses.pop(line.response_id, None)
                self._registry.terminalize(line.response_id)
                self._record_dropped(line, reason="answer_started")

    def _unschedule(self, response: ResponseBuffer) -> None:
        response.scheduled = False
        if response in self._after_drain:
            self._after_drain.remove(response)
        if response in self._parked:
            self._parked.remove(response)

    async def _schedule_response(  # noqa: C901, PLR0911 - explicit lane disposition table
        self,
        response: ResponseBuffer,
    ) -> None:
        speech = response.speech_text()
        if not speech:
            self._responses.pop(response.response_id, None)
            self._registry.terminalize(response.response_id)
            self._broadcast_spoken(response)
            return
        active = self._active
        if active is None:
            self._start_response(response)
            return
        if (
            self._foreground_decision is not None
            and active.response.response_group_id != response.response_group_id
        ):
            # ADR-0008 D8: a cross-group candidate takes the lane only when
            # L3 policy selects it; arriving is not permission.
            verdict = self._foreground_decision(
                active.response.response_group_id,
                active.response.row_id,
                response.response_group_id,
                response.row_id,
            )
            if verdict == "enqueue_after_drain":
                # ADR 0107: another turn's answer
                # waits until she has finished, and is not lost for arriving second.
                self._enqueue_after_drain(response)
                return
            if verdict != "supersede":
                self._registry.terminalize(response.response_id)
                self._responses.pop(response.response_id, None)
                record_realtime_trace(
                    "media_foreground_declined",
                    response_id=response.response_id,
                    response_group_id=response.response_group_id,
                    incumbent_response_id=active.response.response_id,
                    verdict=verdict,
                    terminal_commit_pending=active.terminal_commit_pending,
                )
                return
            if active.terminal_commit_pending:
                # A tombstoned incumbent is draining, not occupying: the
                # player released its lease at complete/interrupt_generation,
                # so the winner may take the lane and only has to wait for the
                # terminal debt below. D8 displaces a GROUP, so the grant
                # discards that group's queued continuation exactly as the
                # live-incumbent branch does -- otherwise which meaning of
                # "supersede" applies would turn on sub-millisecond timing.
                purged = len(self._after_drain)
                self._purge_after_drain()
                record_realtime_trace(
                    "media_foreground_draining_lane_granted",
                    response_id=response.response_id,
                    response_group_id=response.response_group_id,
                    incumbent_response_id=active.response.response_id,
                    purged_lane_depth=purged,
                )
        if active.terminal_commit_pending:
            if len(self._after_drain) >= self._config.response_lane_capacity:
                self._registry.terminalize(response.response_id)
                self._responses.pop(response.response_id, None)
                return
            # A generation whose PCM eligibility is already tombstoned still
            # owns the lane until its terminal is durable. Queue every
            # successor behind that debt; never reinterpret a late foreground
            # event as permission to mint a generation concurrently.
            self._after_drain.append(response)
            record_realtime_trace(
                "media_terminal_debt_wait_enqueued",
                response_id=response.response_id,
                response_group_id=response.response_group_id,
                lane_depth=len(self._after_drain),
            )
            return
        if active.response.response_group_id == response.response_group_id:
            self._enqueue_after_drain(response, behind=active.response.response_group_id)
            return
        if not await self.interrupt_active(reason="foreground_superseded"):
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)
            return
        self._purge_after_drain()
        self._start_response(response)

    def _enqueue_after_drain(self, response: ResponseBuffer, *, behind: str | None = None) -> None:
        """Queue ``response`` for the lane; ``behind`` keeps that group's answer together.

        The playing answer's own continuation goes ahead of another turn's
        answer already waiting, so a turn is said whole before the next.
        """
        if len(self._after_drain) >= self._config.response_lane_capacity:
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)
            record_realtime_trace(
                "media_after_drain_overflow",
                response_id=response.response_id,
                response_group_id=response.response_group_id,
                capacity=self._config.response_lane_capacity,
            )
            return
        index = next(
            (
                position
                for position, queued in enumerate(self._after_drain)
                if behind is not None and queued.response_group_id != behind
            ),
            len(self._after_drain),
        )
        self._after_drain.insert(index, response)
        record_realtime_trace(
            "media_after_drain_enqueued",
            response_id=response.response_id,
            response_group_id=response.response_group_id,
            phase=response.phase,
            lane_depth=len(self._after_drain),
        )

    def _start_response(self, response: ResponseBuffer) -> None:
        if (
            self._power.suspended
            or self._lane_isolated
            or self._power.is_shutdown_requested()
        ):
            self._registry.terminalize(response.response_id)
            self._responses.pop(response.response_id, None)
            return
        if self._held or self._device_held:
            # ADR 0053: Allen is talking; the answer starts when he stops.
            # ADR 0054: or the player is closed while the devices are re-read.
            self._parked.append(response)
            self._clear_output_active()
            return
        self._started_turns.add(response.turn_id)
        result = self._player.activate_generation(
            session_id=self._registry.boot_id,
            response_id=response.response_id,
            response_group_id=response.response_group_id,
            turn_id=response.turn_id,
        )
        if isinstance(result, ForegroundBusy):
            msg = "media owner/player foreground state diverged"
            raise RuntimeError(msg)  # noqa: TRY004 - runtime state, not caller type
        active = ActiveResponse(
            response=response,
            lease=result,
            starvation_gaps_at_start=self._player.starvation_gaps,
            host_underflows_at_start=self._player.underflow_count,
        )
        self._active = active
        self._set_output_active()
        active.task = asyncio.create_task(
            self._play_response(active),
            name=f"media-response-{response.response_id}",
        )
        active.presentation_task = asyncio.create_task(
            self._presentation_owner(active),
            name=f"media-presentation-{response.response_id}",
        )
        active.task.add_done_callback(self._response_task_done)
        record_realtime_trace(
            "tts_request_started",
            response_id=response.response_id,
            response_group_id=response.response_group_id,
            playback_generation_id=result.playback_generation_id,
            phase=response.phase,
            measurement_semantics="media_owner_started_provider_request",
        )

    async def _play_response(  # noqa: C901, PLR0912 - bounded lifecycle cleanup FSM
        self,
        active: ActiveResponse,
    ) -> None:
        response = active.response
        live = response.incremental and not response.emitted
        segments = response.speech_segments()
        committed = segments[0][1] if live and segments else response.speech_text()
        speech_hash = hashlib.sha256(committed.encode()).hexdigest()
        if self._ducker is not None:
            self._ducker.enter_output()
            active.output_lease = True
        try:
            activation = self._journal.playback_started(
                payload={
                    "session_id": active.lease.session_id,
                    "response_id": response.response_id,
                    "turn_id": response.turn_id,
                    "playback_generation_id": active.lease.playback_generation_id,
                    "phase": response.phase,
                    "channel": response.channel,
                    "speech_text_hash": speech_hash,
                    "estimated_output_latency_ns": (
                        self._player.estimated_output_latency_ns
                    ),
                    **({"incremental": True} if live else {}),
                },
                source_event_id=response.source_event_id,
                correlation={"turn_id": response.turn_id},
            )
            active.activation_event_uid = activation.event_uid
            async with asyncio.timeout(self._config.response_timeout_s) as budget:
                outcome = await self._streamer.stream_with_prefix_fallback(
                    active,
                    segments,
                    budget=budget,
                    live=live,
                )
                if outcome.fail_reason is not None:
                    await self._fail_active(
                        active,
                        reason=outcome.fail_reason,
                        retryable=outcome.fail_retryable,
                    )
                if active.advance_after_cleanup or self._active is not active:
                    return
                if not outcome.completed:
                    await self._fail_active(
                        active,
                        reason="tts_fallback_unavailable",
                        retryable=True,
                    )
                    return
                if live:
                    speech_hash = hashlib.sha256(active.prepared_text.encode()).hexdigest()
                await self._drain_and_complete(active, segment_hash=speech_hash)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            LOGGER.warning(
                "streaming TTS response %s exceeded response_timeout_s=%.3f for one segment",
                response.response_id,
                self._config.response_timeout_s,
            )
            await self._fail_active(active, reason="tts_response_timeout", retryable=True)
        except Exception as exc:  # noqa: BLE001 - provider adapters fail heterogeneously
            LOGGER.warning("streaming TTS response failed: %r", exc)
            await self._fail_active(
                active,
                reason=f"tts_provider_error:{type(exc).__name__}",
                retryable=True,
            )
        finally:
            session = active.session
            active.session = None
            if session is not None:
                close_task = asyncio.create_task(session.close())
                await self._clock.wait_task_bounded(close_task, timeout_s=0.5)
            # Every exit path retains and drains fallback process ownership;
            # an exceptional ``wait()`` must not let a still-live ``say``
            # overlap the next generation.
            await self._say_fallback.cancel(active)
            if active.output_lease and self._ducker is not None:
                active.output_lease = False
                with contextlib.suppress(Exception):
                    self._ducker.leave_output()
            if active.advance_after_cleanup:
                active.advance_after_cleanup = False
                if self._active is active:
                    self._active = None
                self._advance_after_drain()

    async def _presentation_owner(self, active: ActiveResponse) -> None:
        """Persist whole-segment heard checkpoints while provider I/O continues."""
        try:
            while self._active is active and not self._lane_isolated:
                snapshot = self._player.poll_generation(
                    active.lease.playback_generation_id,
                )
                if isinstance(snapshot, StalePlaybackGeneration):
                    return
                await self.checkpoint_if_advanced(active, snapshot)
                self._broadcast_position(active, snapshot)
                await asyncio.sleep(self._config.presentation_poll_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "presentation checkpoint owner failed: response_id=%s generation=%d",
                active.response.response_id,
                active.lease.playback_generation_id,
            )

    def isolate_fallback_cleanup(self, active: ActiveResponse) -> None:
        """Fail closed when exact subprocess quiescence cannot be established."""
        self._lane_isolated = True
        self._stop_accepting()
        active.advance_after_cleanup = False
        record_realtime_trace(
            "media_fallback_cleanup_isolated",
            response_id=active.response.response_id,
            playback_generation_id=active.lease.playback_generation_id,
        )
        # No terminal was being attempted and no exception reached here: the
        # janitor simply could not prove the `say` subprocess was quiescent.
        self._emit_lane_isolated(
            active,
            terminal_type=None,
            error_type=None,
            isolation_reason="fallback_cleanup_unproven",
        )
        self._request_shutdown_deadline(self._config.shutdown_timeout_s)

    async def _drain_and_complete(
        self,
        active: ActiveResponse,
        *,
        segment_hash: str,
    ) -> None:
        lease = active.lease
        software_drain_recorded = False
        while self._active is active:
            snapshot = self._player.poll_generation(lease.playback_generation_id)
            if isinstance(snapshot, StalePlaybackGeneration):
                await self._fail_active(
                    active,
                    reason="playback_ledger_lost_before_completion",
                    retryable=True,
                )
                return
            await self.checkpoint_if_advanced(active, snapshot)
            if snapshot.software_drained and not software_drain_recorded:
                software_drain_recorded = True
                record_realtime_trace(
                    "tts_software_drain",
                    response_id=active.response.response_id,
                    playback_generation_id=lease.playback_generation_id,
                    submitted_samples=snapshot.submitted_samples,
                    measurement_semantics=("generation_ring_empty_not_physical_dac_completion"),
                )
            if snapshot.fully_presented:
                record_realtime_trace(
                    "tts_estimated_audible",
                    response_id=active.response.response_id,
                    playback_generation_id=lease.playback_generation_id,
                    estimated_audible_samples=snapshot.estimated_audible_samples,
                    cursor_quality=snapshot.cursor_quality,
                    measurement_semantics=(
                        "full_conservative_callback_plus_output_latency_horizon_not_loopback"
                    ),
                )
                active.terminal_commit_pending = True
                completed = self._player.complete_generation(
                    expected_playback_generation_id=lease.playback_generation_id,
                )
                if isinstance(completed, StalePlaybackGeneration):
                    await self._fail_active(
                        active,
                        reason="playback_generation_lost_before_completion",
                        retryable=True,
                    )
                    return
                durable = await self._commit_terminal_durable(
                    active,
                    event_type="surface.playback_completed",
                    snapshot=completed,
                    reason=None,
                    speech_text_hash=segment_hash,
                    retryable=None,
                )
                if durable:
                    await self._release_active(
                        active,
                        event_type="surface.playback_completed",
                        start_successor=True,
                    )
                return
            await asyncio.sleep(self._config.presentation_poll_s)

    async def _fail_active(
        self,
        active: ActiveResponse,
        *,
        reason: str,
        retryable: bool,
    ) -> None:
        # An interrupt already owns this response's terminal: a response task that
        # sees the tombstone while the interrupt settles must not race it to a failure.
        if self._active is not active or active.terminal_commit_pending:
            return
        active.terminal_commit_pending = True
        snapshot = await self._interrupt_snapshot(active)
        if snapshot is None:
            self._isolate_terminal_debt(
                active,
                event_type="surface.playback_failed",
                error=RuntimeError("callback publication did not settle"),
                isolation_reason="callback_publication_unsettled",
            )
            return
        durable = await self._commit_terminal_durable(
            active,
            event_type="surface.playback_failed",
            snapshot=snapshot,
            reason=reason,
            speech_text_hash=None,
            retryable=retryable,
        )
        if durable:
            await self._release_active(
                active,
                event_type="surface.playback_failed",
                start_successor=True,
            )

    async def interrupt_active(self, *, reason: str) -> bool:
        """Tombstone, cancel and terminalize the active response; ``False`` if the lane isolated."""
        active = self._active
        if active is None:
            return not self._lane_isolated
        active.terminal_commit_pending = True
        # Generation tombstone is the first irreversible publication.  Only
        # after it lands do we cancel network/fallback work.
        snapshot = await self._interrupt_snapshot(active)
        if snapshot is not None:
            # The caption stops now, not after the terminal commit.
            self._broadcast_position(active, snapshot, stopped=True)
        await self._cancel_active_io(active, reason=reason)
        if snapshot is None:
            self._isolate_terminal_debt(
                active,
                event_type="surface.playback_interrupted",
                error=RuntimeError("callback publication did not settle"),
                isolation_reason="callback_publication_unsettled",
            )
            return False
        durable = await self._commit_terminal_durable(
            active,
            event_type="surface.playback_interrupted",
            snapshot=snapshot,
            reason=reason,
            speech_text_hash=None,
            retryable=None,
        )
        if not durable:
            return False
        await self._release_active(
            active,
            event_type="surface.playback_interrupted",
            start_successor=False,
        )
        return True

    async def _cancel_active_io(self, active: ActiveResponse, *, reason: str) -> None:
        task = active.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        session = active.session
        if session is not None:
            abort_task = asyncio.create_task(session.abort(reason))
            await self._clock.wait_task_bounded(abort_task, timeout_s=0.5)
        await self._say_fallback.cancel(active)
        if task is not None and task is not asyncio.current_task():
            await self._clock.wait_task_bounded(task, timeout_s=0.5)

    async def checkpoint_if_advanced(
        self,
        active: ActiveResponse,
        snapshot: OutputTimelineSnapshot,
    ) -> None:
        """Persist the heard position of ``active`` when it moved, retrying a failed append."""
        sequence = snapshot.heard_through_sequence
        if not snapshot.heard_text or snapshot.heard_text == active.last_checkpoint_text:
            return
        for attempt in range(1, self._config.checkpoint_retry_attempts + 1):
            if self._journal.checkpoint_recorded(
                response_id=active.response.response_id,
                playback_generation_id=active.lease.playback_generation_id,
                session_id=active.lease.session_id,
                heard_text_hash=snapshot.heard_text_hash,
            ):
                active.last_checkpoint_text = snapshot.heard_text
                return
            try:
                self._journal.playback_checkpoint(
                    payload={
                        "session_id": active.lease.session_id,
                        "response_id": active.response.response_id,
                        "turn_id": active.response.turn_id,
                        "playback_generation_id": active.lease.playback_generation_id,
                        "heard_through_sequence": sequence,
                        "submitted_samples": snapshot.submitted_samples,
                        "estimated_audible_samples": snapshot.estimated_audible_samples,
                        "heard_partial_sequence": snapshot.heard_partial_sequence,
                        "heard_partial_text_end": snapshot.heard_partial_text_end,
                        "heard_text_hash": snapshot.heard_text_hash,
                        "heard_text": snapshot.heard_text,
                        "cursor_quality": snapshot.cursor_quality,
                    },
                    source_event_id=active.activation_event_uid or active.response.source_event_id,
                    correlation={"turn_id": active.response.turn_id},
                )
            except Exception:
                LOGGER.exception(
                    "playback checkpoint append failed: response_id=%s generation=%d attempt=%d",
                    active.response.response_id,
                    active.lease.playback_generation_id,
                    attempt,
                )
                if attempt < self._config.checkpoint_retry_attempts:
                    await asyncio.sleep(self._config.durability_retry_s)
                continue
            active.last_checkpoint_text = snapshot.heard_text
            return

    async def _commit_terminal_durable(  # noqa: PLR0913 - mirrors terminal payload owner
        self,
        active: ActiveResponse,
        *,
        event_type: str,
        snapshot: OutputTimelineSnapshot,
        reason: str | None,
        speech_text_hash: str | None,
        retryable: bool | None,
    ) -> bool:
        """Block successor minting on bounded retry of the Wave 1 terminal CAS."""
        last_error: BaseException | None = None
        for attempt in range(1, self._config.terminal_retry_attempts + 1):
            try:
                self._journal.commit_terminal(
                    active,
                    event_type=event_type,
                    snapshot=snapshot,
                    reason=reason,
                    speech_text_hash=speech_text_hash,
                    retryable=retryable,
                    starvation_gaps=(
                        self._player.starvation_gaps - active.starvation_gaps_at_start
                    ),
                    host_underflows=(
                        self._player.underflow_count - active.host_underflows_at_start
                    ),
                    tail_ramp_samples=self._player.tail_ramp_samples,
                )
                self._registry.terminalize(active.response.response_id)
            except Exception as exc:
                last_error = exc
                LOGGER.exception(
                    "playback terminal append failed: type=%s response_id=%s "
                    "generation=%d attempt=%d",
                    event_type,
                    active.response.response_id,
                    active.lease.playback_generation_id,
                    attempt,
                )
                if attempt < self._config.terminal_retry_attempts:
                    delay = self._clock.remaining_s(self._config.durability_retry_s)
                    if delay <= 0:
                        break
                    await asyncio.sleep(delay)
                continue
            return True
        self._isolate_terminal_debt(
            active,
            event_type=event_type,
            error=last_error or RuntimeError("terminal append exhausted retries"),
            isolation_reason="terminal_append_exhausted",
        )
        return False

    async def _interrupt_snapshot(
        self,
        active: ActiveResponse,
    ) -> OutputTimelineSnapshot | None:
        """Tombstone first, then settle the callback publication linearization."""
        self._player.interrupt_generation(
            expected_playback_generation_id=active.lease.playback_generation_id,
        )
        deadline = asyncio.get_running_loop().time() + self._clock.remaining_s(0.5)
        while asyncio.get_running_loop().time() < deadline:
            settled = self._player.settle_interrupted_generation(
                expected_playback_generation_id=active.lease.playback_generation_id,
            )
            if settled is None:
                await asyncio.sleep(0)
                continue
            return None if isinstance(settled, StalePlaybackGeneration) else settled
        return None

    def _response_task_done(self, task: asyncio.Task[None]) -> None:
        """Fail closed if an unexpected task exception escaped lifecycle cleanup."""
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        LOGGER.error(
            "media response task escaped: %r",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        active = self._active
        if active is None or active.task is not task:
            return
        if active.terminal_commit_pending:
            self._isolate_terminal_debt(
                active,
                event_type="surface.playback_failed",
                error=error,
                isolation_reason="task_escaped",
            )
            return
        recovery = asyncio.create_task(
            self._fail_active(
                active,
                reason=f"media_owner_task_error:{type(error).__name__}",
                retryable=False,
            ),
            name=f"media-task-recovery-{active.response.response_id}",
        )
        recovery.add_done_callback(self._log_recovery_error)

    @staticmethod
    def _log_recovery_error(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOGGER.error("media task recovery escaped: %r", error)

    async def _release_active(
        self,
        active: ActiveResponse,
        *,
        event_type: str,
        start_successor: bool,
    ) -> None:
        """Retire/broadcast only after durable terminal commit."""
        if self._active is not active:
            return
        presentation = active.presentation_task
        if presentation is not None and presentation is not asyncio.current_task():
            presentation.cancel()
            await self._clock.wait_task_bounded(presentation, timeout_s=0.25)
        self._responses.pop(active.response.response_id, None)
        self._player.retire_generation(active.lease.playback_generation_id)
        self._broadcast_spoken(active.response, event_type=event_type)
        if start_successor and asyncio.current_task() is active.task:
            # Keep the durable-but-cleaning response as the lane owner. New
            # deliveries queue behind its terminal_commit_pending marker until
            # provider/process cleanup completes in `_play_response.finally`.
            active.advance_after_cleanup = True
            return
        self._active = None
        if not start_successor:
            self._clear_output_active()
            return
        self._advance_after_drain()

    def _advance_after_drain(self) -> None:
        if self._power.suspended or self._lane_isolated or self._active is not None:
            return
        if self._after_drain:
            self._start_response(self._after_drain.popleft())
        else:
            self._clear_output_active()

    def _record_dropped(self, response: ResponseBuffer, *, reason: str) -> None:
        """Leave the durable fact that none of ``response`` was spoken.

        Best effort: letting the answer go must not fail on its record.
        """
        try:
            self._journal.speech_dropped(
                payload={
                    "response_id": response.response_id,
                    "turn_id": response.turn_id,
                    "reason": reason,
                },
                source_event_id=response.source_event_id,
                correlation={"turn_id": response.turn_id},
            )
        except Exception:  # noqa: BLE001 - see docstring
            LOGGER.warning(
                "could not record dropped speech %s", response.response_id, exc_info=True,
            )

    def _broadcast_position(
        self,
        active: ActiveResponse,
        snapshot: OutputTimelineSnapshot,
        *,
        stopped: bool = False,
    ) -> None:
        """Tell the screen where her voice is, when that changed (ADR 0112).

        Held (a soft barge-in being judged) or ``stopped``, the caption stops
        with her; nothing is said after a stop, which is the last report.
        """
        callback = getattr(self._broadcaster, "broadcast_voice_sync", None)
        if not callable(callback) or active.position_stopped:
            return
        active.position_stopped = stopped
        held = stopped or self._player.generation_held(active.lease.playback_generation_id) is True
        position = (snapshot.played_letters, snapshot.playing_letters, held)
        if position == active.last_position or not isinstance(position[0], int):
            return
        active.last_position = position
        with contextlib.suppress(Exception):
            callback(
                "playing",
                turn_id=active.response.turn_id,
                played=position[0],
                ahead=position[1],
                held=held,
                **self._wait_line_mark(active.response),
            )

    @staticmethod
    def _wait_line_mark(response: ResponseBuffer) -> dict[str, str]:
        """``response_phase`` for a wait line's voice ops, nothing for an answer (ADR 0121)."""
        return {"response_phase": "commentary"} if response.phase == "commentary" else {}

    def _broadcast_spoken(
        self, response: ResponseBuffer, *, event_type: str = "no_speech",
    ) -> None:
        callback = getattr(self._broadcaster, "broadcast_voice_sync", None)
        if callable(callback):
            with contextlib.suppress(Exception):
                callback(
                    "spoken",
                    turn_id=response.turn_id,
                    output_outcome=event_type.removeprefix("surface.playback_"),
                    **self._wait_line_mark(response),
                )

    def _emit_lane_isolated(
        self,
        active: ActiveResponse,
        *,
        terminal_type: str | None,
        error_type: str | None,
        isolation_reason: str,
    ) -> None:
        """Append the durable record that this lane will speak nothing again.

        Best effort by construction: ``_lane_isolated`` is never reset and no
        supervisor recreates the owner, so the process is deaf until restart.
        Isolation must never be prevented by its own record-keeping failing.
        """
        try:
            self._journal.lane_isolated(
                payload={
                    "session_id": active.lease.session_id,
                    "response_id": active.response.response_id,
                    "turn_id": active.response.turn_id,
                    "playback_generation_id": active.lease.playback_generation_id,
                    "terminal_type": terminal_type,
                    "error_type": error_type,
                    "isolation_reason": isolation_reason,
                },
                source_event_id=(
                    active.activation_event_uid or active.response.source_event_id
                ),
                correlation={"turn_id": active.response.turn_id},
            )
        except Exception:
            LOGGER.exception(
                "media lane isolation row could not be appended: "
                "response_id=%s generation=%d reason=%s",
                active.response.response_id,
                active.lease.playback_generation_id,
                isolation_reason,
            )

    def _isolate_terminal_debt(
        self,
        active: ActiveResponse,
        *,
        event_type: str,
        error: BaseException,
        isolation_reason: str,
    ) -> None:
        """Fail closed: retain the ledger and forbid every successor generation."""
        self._lane_isolated = True
        self._terminal_debt = active
        self._stop_accepting()
        self._registry.terminalize(active.response.response_id)
        if self._active is active:
            self._active = None
        if active.presentation_task is not None:
            active.presentation_task.cancel()
        while self._after_drain:
            queued = self._after_drain.popleft()
            self._registry.terminalize(queued.response_id)
        self._responses.clear()
        self._clear_output_active()
        record_realtime_trace(
            "media_terminal_debt_isolated",
            response_id=active.response.response_id,
            playback_generation_id=active.lease.playback_generation_id,
            terminal_type=event_type,
            error_type=type(error).__name__,
        )
        self._emit_lane_isolated(
            active,
            terminal_type=event_type,
            error_type=type(error).__name__,
            isolation_reason=isolation_reason,
        )
        self._request_shutdown_deadline(self._config.shutdown_timeout_s)
