"""The media owner's ring sink: what goes into the player's ring and the rows recorded about it."""

from __future__ import annotations

import asyncio
import hashlib
import math
from typing import TYPE_CHECKING

from jarvis.surface.tts_silence import SilenceTrimConfig, SilenceTrimmer
from jarvis.surface.voice_ledger import AcceptedSamples, StalePlaybackGeneration, settle_boundaries

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.surface.voice_media.config import StreamingMediaConfig
    from jarvis.surface.voice_media.journal import PlaybackJournal
    from jarvis.surface.voice_media.response import ActiveResponse
    from jarvis.surface.voice_tts import AudioStreamPlayer, TTSAudioChunk


class RingSink:
    """Writes into the player's ring and records the rows about it; actor thread only (ADR 0378).

    It holds no mutable state of its own: the player, the config and the journal are given, and
    what it changes is the player's ring, the journal's rows and the fields of the
    ``ActiveResponse`` it is handed. Every call runs on the actor's loop. The owner decides which
    response is the active one, through ``current``; the sink says how its audio is written.
    """

    def __init__(
        self,
        *,
        player: AudioStreamPlayer,
        config: StreamingMediaConfig,
        journal: PlaybackJournal,
        current: Callable[[], ActiveResponse | None],
    ) -> None:
        """Write into ``player``'s ring and record the rows through ``journal``.

        ``current`` answers which response is the owner's active one: a write stops once the
        response it was given is no longer it.
        """
        self._player = player
        self._config = config
        self._journal = journal
        self._current = current

    def prepare_segment(
        self, active: ActiveResponse, sequence: int, text: str, segment_hash: str,
    ) -> None:
        """Record the segment about to be synthesized; the cursor maps onto these rows."""
        self._journal.segment_prepared(
            payload={
                "session_id": active.lease.session_id,
                "response_id": active.response.response_id,
                "turn_id": active.response.turn_id,
                "playback_generation_id": active.lease.playback_generation_id,
                "sequence": sequence,
                "speech_text": text,
                "speech_text_hash": hashlib.sha256(text.encode()).hexdigest(),
                "segment_hash": segment_hash,
                "source_chunk_event_uid": active.response.chunks[sequence].source_event_uid,
            },
            source_event_id=active.activation_event_uid,
            correlation={"turn_id": active.response.turn_id},
        )
        active.prepared_text += text

    def new_silence_trimmer(self) -> SilenceTrimmer | None:
        """ADR 0165: one trimmer per playback generation, or none when switched off."""
        config = self._config
        if not config.trim_silence:
            return None
        return SilenceTrimmer(
            sample_rate_hz=config.canonical_sample_rate_hz,
            config=SilenceTrimConfig(
                threshold_db=config.trim_threshold_db,
                window_ms=config.trim_window_ms,
                preroll_ms=config.trim_preroll_ms,
                fade_ms=config.trim_fade_ms,
                max_cut_ms=config.trim_max_cut_ms,
                clause_pause_ms=config.trim_clause_pause_ms,
                sentence_pause_ms=config.trim_sentence_pause_ms,
            ),
        )

    def record_alignment(
        self, active: ActiveResponse, events: list[TTSAudioChunk], *, segment_start: int,
        segment_samples: int, trimmed: int = 0,
    ) -> None:
        """Persist exact text/sample mappings before allowing a word checkpoint.

        Provider times count from the start of the provider's audio; the written
        segment starts ``trimmed`` samples later (its leading silence dropped).
        Called once the segment's audio is complete: whether the provider's
        timing is plausible can only be told against the audio it came with
        (``settle_boundaries``).
        """
        rate = self._config.canonical_sample_rate_hz
        boundaries = settle_boundaries(
            tuple(
                (end, segment_start + max(0, math.ceil(ms * rate / 1000) - trimmed))
                for event in events for end, ms in event.word_boundaries
            ),
            segment_start=segment_start, segment_end=segment_start + segment_samples,
        )
        if not boundaries:
            return
        self._journal.playback_alignment(
            payload={
                "session_id": active.lease.session_id,
                "response_id": active.response.response_id,
                "turn_id": active.response.turn_id,
                "playback_generation_id": active.lease.playback_generation_id,
                "sequence": events[0].sequence,
                "speech_text_hash": hashlib.sha256(
                    (events[0].timing_text or "").encode(),
                ).hexdigest(),
                "word_boundaries": [list(boundary) for boundary in boundaries],
            },
            source_event_id=active.activation_event_uid,
            correlation={"turn_id": active.response.turn_id},
        )
        self._player.align_generation_segment(
            expected_playback_generation_id=active.lease.playback_generation_id,
            sequence=events[0].sequence, boundaries=boundaries,
        )

    async def await_segments(
        self,
        active: ActiveResponse,
        segments: list[tuple[int, str, str]],
    ) -> bool:
        """Extend ``segments`` with newly durable ones; False once the buffer is complete."""
        response = active.response
        last = segments[-1][0] if segments else -1
        while self._current() is active:
            if response.tagged:
                return True
            response.updated.clear()
            fresh = [item for item in response.speech_segments() if item[0] > last]
            if fresh:
                segments.extend(fresh)
                return True
            if response.emitted:
                return False
            await response.updated.wait()
        return False

    async def write_all(
        self,
        active: ActiveResponse,
        pcm: bytes,
        *,
        sequence: int,
    ) -> int:
        """Write ``pcm`` into the ring until it is all taken; the samples accepted."""
        offset_samples = 0
        samples = memoryview(pcm)
        total_samples = len(samples) // 4
        while offset_samples < total_samples:
            if self._current() is not active:
                return offset_samples
            result = self._player.write_generation(
                samples[offset_samples * 4 :].tobytes(),
                expected_playback_generation_id=(active.lease.playback_generation_id),
                segment_sequence=sequence,
            )
            if isinstance(result, StalePlaybackGeneration):
                return offset_samples
            if isinstance(result, AcceptedSamples):
                offset_samples += result.sample_count
                continue
            self._player.poll_presentation()
            await asyncio.sleep(self._config.ring_retry_s)
        return offset_samples
