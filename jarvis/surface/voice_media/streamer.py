"""The media owner's response streamer: one response's segments into the player."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jarvis.shared import lang
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface.voice_ledger import StalePlaybackGeneration
from jarvis.surface.voice_media.pcm import SegmentResampler, cut_off_cue, head_dropped
from jarvis.surface.voice_tts import TTSAudioChunk, TTSResponseSegment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from jarvis.surface.voice_ledger import OutputTimelineSnapshot
    from jarvis.surface.voice_media.config import StreamingMediaConfig
    from jarvis.surface.voice_media.contract import StreamingTTSProvider
    from jarvis.surface.voice_media.journal import PlaybackJournal
    from jarvis.surface.voice_media.network_lost import NetworkLostCue
    from jarvis.surface.voice_media.response import ActiveResponse
    from jarvis.surface.voice_media.ring_sink import RingSink
    from jarvis.surface.voice_media.say_fallback import SayFallback
    from jarvis.surface.voice_media.shutdown_clock import ShutdownClock
    from jarvis.surface.voice_media.spare import SpareSessions
    from jarvis.surface.voice_tts import AudioStreamPlayer, TTSSegmentFinished, TTSSession


@dataclass(frozen=True, slots=True)
class StreamOutcome:
    """How one response's streaming ended, for the owner to act on (ADR 0383).

    ``completed`` is whether every segment reached the player. When ``fail_reason`` is set the
    streamer has stopped at the point where it used to fail the active response itself; the
    owner fails it with that reason and ``fail_retryable``, then carries on as it did.
    """

    completed: bool
    fail_reason: str | None = None
    fail_retryable: bool = False


_COMPLETED = StreamOutcome(completed=True)
_NOT_COMPLETED = StreamOutcome(completed=False)


class ResponseStreamer:
    """Streams one response's segments into the player; actor thread only (ADR 0383).

    It holds no mutable state of its own: the provider, the player and the other collaborators
    are given, and what it changes is the player's generation, the sessions and the fields of the
    ``ActiveResponse`` it is handed. Every call runs on the actor's loop. The owner decides which
    response is the active one, through ``current``; which language the last answer spoke, through
    ``set_answer_language``; and how a checkpoint is written, through ``checkpoint``. It never ends
    a response: where it used to fail the active one it returns a ``StreamOutcome`` that says so.
    """

    def __init__(  # noqa: PLR0913 - explicit ownership dependencies
        self,
        *,
        provider: StreamingTTSProvider,
        player: AudioStreamPlayer,
        config: StreamingMediaConfig,
        clock: ShutdownClock,
        journal: PlaybackJournal,
        ring: RingSink,
        say_fallback: SayFallback,
        spare: SpareSessions,
        network_lost: NetworkLostCue,
        current: Callable[[], ActiveResponse | None],
        set_answer_language: Callable[[lang.Language], None],
        checkpoint: Callable[[ActiveResponse, OutputTimelineSnapshot], Awaitable[None]],
    ) -> None:
        """Stream through ``provider`` into ``player``, bounded by ``config`` and ``clock``.

        ``current`` answers which response is the owner's active one; ``set_answer_language``
        records the language of the answer's first segment for the next spare;
        ``checkpoint`` persists the heard position of a response.
        """
        self._provider = provider
        self._player = player
        self._config = config
        self._clock = clock
        self._journal = journal
        self._ring = ring
        self._say_fallback = say_fallback
        self._spare = spare
        self._network_lost = network_lost
        self._current = current
        self._set_answer_language = set_answer_language
        self._checkpoint = checkpoint

    async def stream_with_prefix_fallback(  # noqa: C901, PLR0911, PLR0912, PLR0915 - bounded endpoint FSM
        self,
        active: ActiveResponse,
        segments: list[tuple[int, str, str]],
        *,
        budget: asyncio.Timeout,
        live: bool,
    ) -> StreamOutcome:
        """Walk ``segments``; while ``live`` the list grows until the buffer is emitted."""
        lease = active.lease
        accepted_total = 0
        trimmer = self._ring.new_silence_trimmer()
        last_error: BaseException | None = None
        endpoint_index = 0
        session: TTSSession | None = None
        iterator: AsyncIterator[TTSAudioChunk | TTSSegmentFinished] | None = None
        provider_first = False
        segment_index = 0
        while True:
            if segment_index >= len(segments) and not (
                live and await self._ring.await_segments(active, segments)
            ):
                break
            if live and active.response.tagged:
                return StreamOutcome(
                    completed=False, fail_reason="stream_chunk_tagged", fail_retryable=False,
                )
            sequence, text, segment_hash = segments[segment_index]
            # Opened after this segment's wait and closed at the next segment's
            # reschedule, so the bound covers one segment's provider I/O plus its
            # backpressured playback - plus the drain, for the final segment -
            # never the whole generation, whose length is deliberately unbounded.
            budget.reschedule(
                asyncio.get_running_loop().time() + self._config.response_timeout_s,
            )
            self._ring.prepare_segment(active, sequence, text, segment_hash)
            opened = self._player.begin_generation_segment(
                expected_playback_generation_id=lease.playback_generation_id,
                sequence=sequence,
                text=text,
                segment_hash=segment_hash,
            )
            if isinstance(opened, StalePlaybackGeneration):
                return _NOT_COMPLETED
            segment_start = accepted_total
            accepted_segment = 0
            while endpoint_index < self._provider.streaming_candidate_count:
                if self._current() is not active:
                    return _NOT_COMPLETED
                try:
                    if session is None:
                        language = lang.text_language(text)
                        self._set_answer_language(language)
                        session = (
                            await self._spare.take(language) if endpoint_index == 0 else None
                        ) or self._provider.create_tts_session(
                            endpoint_index=endpoint_index,
                            language=language,
                            idle_close_s=self._config.session_idle_close_s,
                            command_queue_capacity=self._config.session_command_capacity,
                            audio_queue_capacity=self._config.session_audio_capacity,
                        )
                        active.session = session
                        await session.open(
                            active.response.response_id,
                            lease.playback_generation_id,
                        )
                        iterator = session.audio_events().__aiter__()
                    await session.send(
                        TTSResponseSegment(
                            response_id=active.response.response_id,
                            playback_generation_id=lease.playback_generation_id,
                            sequence=sequence,
                            text=text,
                        ),
                    )
                    resampler: SegmentResampler | None = None
                    pending_alignment: list[TTSAudioChunk] = []
                    if trimmer is not None:
                        trimmer.begin_segment(text)
                    while True:
                        if iterator is None:  # pragma: no cover - session invariant
                            msg = "opened TTS session has no audio iterator"
                            raise RuntimeError(msg)  # noqa: TRY301 - provider failure lane
                        event = await iterator.__anext__()
                        if event.sequence != sequence:
                            msg = (
                                f"provider segment sequence {event.sequence} does not "
                                f"match active sequence {sequence}"
                            )
                            raise RuntimeError(msg)  # noqa: TRY301 - provider failure lane
                        if isinstance(event, TTSAudioChunk):
                            if event.word_boundaries and event.timing_text == text:
                                pending_alignment.append(event)
                            if not event.pcm:
                                continue
                            if resampler is None:
                                resampler = SegmentResampler(
                                    input_rate_hz=event.sample_rate_hz,
                                    output_rate_hz=self._config.canonical_sample_rate_hz,
                                )
                            if not provider_first:
                                provider_first = True
                                record_realtime_trace(
                                    "tts_provider_first_pcm_received",
                                    response_id=active.response.response_id,
                                    playback_generation_id=lease.playback_generation_id,
                                    endpoint_index=endpoint_index,
                                    pcm_bytes=len(event.pcm),
                                    measurement_semantics=(
                                        "first_nonempty_provider_pcm_event_received_by_media_owner"
                                    ),
                                )
                            canonical = resampler.feed(event.pcm)
                            if trimmer is not None:
                                canonical = trimmer.feed(canonical)
                            written = await self._ring.write_all(
                                active,
                                canonical,
                                sequence=sequence,
                            )
                            accepted_segment += written
                            accepted_total += written
                            continue
                        tail = resampler.finish() if resampler is not None else b""
                        if trimmer is not None:
                            tail = trimmer.feed(tail) + trimmer.end_segment()
                        if tail:
                            written = await self._ring.write_all(active, tail, sequence=sequence)
                            accepted_segment += written
                            accepted_total += written
                        if accepted_segment and pending_alignment:
                            self._ring.record_alignment(
                                active, pending_alignment, segment_start=segment_start,
                                segment_samples=accepted_segment, trimmed=head_dropped(trimmer),
                            )
                        finished = self._player.finish_generation_segment(
                            expected_playback_generation_id=lease.playback_generation_id,
                            sequence=sequence,
                        )
                        if isinstance(finished, StalePlaybackGeneration):
                            return _NOT_COMPLETED
                        record_realtime_trace(
                            "tts_provider_segment_final",
                            response_id=active.response.response_id,
                            playback_generation_id=lease.playback_generation_id,
                            segment_sequence=sequence,
                            accepted_segment_samples=accepted_segment,
                            usage_present=event.usage is not None,
                            measurement_semantics=(
                                "provider_segment_final_received_not_playback_complete"
                            ),
                        )
                        self._journal.tts_usage(
                            event.usage,
                            response_id=active.response.response_id,
                            sequence=sequence,
                        )
                        self._network_lost.prefetch()
                        break
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - provider transport boundary
                    last_error = exc
                    if session is not None:
                        close_task = asyncio.create_task(session.close())
                        await self._clock.wait_task_bounded(close_task, timeout_s=0.5)
                    session = None
                    iterator = None
                    active.session = None
                    if accepted_segment > 0:
                        # ADR-0006 D5: a segment heard in part is never said
                        # again. What she said plays out, and a cue tells Allen
                        # her voice broke rather than leaving him in silence.
                        await self._play_cut_off_cue(active, sequence=sequence)
                        return StreamOutcome(
                            completed=False,
                            fail_reason="partial_tts_provider_failure",
                            fail_retryable=False,
                        )
                    record_realtime_trace(
                        "tts_prefix_safe_endpoint_fallback",
                        response_id=active.response.response_id,
                        playback_generation_id=lease.playback_generation_id,
                        segment_sequence=sequence,
                        failed_endpoint_index=endpoint_index,
                        accepted_samples=0,
                        error_type=type(exc).__name__,
                    )
                    endpoint_index += 1
            else:
                if accepted_total > 0:
                    # The same break between two sentences: no endpoint can say
                    # the next one, and waiting for the whole text to hand it to
                    # `say` waits on a model that is offline too (Wi-Fi off,
                    # 2026-10-01: silence until Allen spoke).
                    await self._play_cut_off_cue(active, sequence=sequence)
                    return StreamOutcome(
                        completed=False,
                        fail_reason="partial_tts_provider_failure",
                        fail_retryable=False,
                    )
                if self._config.enable_macos_say_fallback:
                    while live and await self._ring.await_segments(active, segments):
                        if active.response.tagged:
                            break
                        budget.reschedule(
                            asyncio.get_running_loop().time()
                            + self._config.response_timeout_s,
                        )
                    if live and active.response.tagged:
                        return StreamOutcome(
                            completed=False,
                            fail_reason="stream_chunk_tagged",
                            fail_retryable=False,
                        )
                    remaining = segments[segment_index:]
                    return StreamOutcome(
                        completed=await self._say_fallback.run(active, remaining, budget=budget),
                    )
                if last_error is not None:
                    raise last_error
                return _NOT_COMPLETED
            segment_index += 1
        if not segments:
            return _NOT_COMPLETED
        record_realtime_trace(
            "tts_provider_final",
            response_id=active.response.response_id,
            playback_generation_id=lease.playback_generation_id,
            accepted_samples=accepted_total,
            measurement_semantics="final_provider_segment_received_not_playback_complete",
        )
        if session is not None:
            with contextlib.suppress(Exception):
                await session.finish()
            active.session = None
        return _COMPLETED

    async def _play_cut_off_cue(self, active: ActiveResponse, *, sequence: int) -> None:
        """Play what was accepted to its end, then her spoken line or tones, before the terminal."""
        line = self._network_lost.line(lang.language())
        cue = line or cut_off_cue(self._config.canonical_sample_rate_hz)
        await self._ring.write_all(active, cue, sequence=sequence)
        record_realtime_trace(
            "tts_cut_off_cue",
            response_id=active.response.response_id,
            playback_generation_id=active.lease.playback_generation_id,
            segment_sequence=sequence,
            cue="spoken_line" if line else "tones",
        )
        while self._current() is active:
            snapshot = self._player.poll_generation(active.lease.playback_generation_id)
            if isinstance(snapshot, StalePlaybackGeneration):
                return
            await self._checkpoint(active, snapshot)
            if snapshot.estimated_audible_samples >= snapshot.accepted_samples:
                return
            await asyncio.sleep(self._config.presentation_poll_s)
