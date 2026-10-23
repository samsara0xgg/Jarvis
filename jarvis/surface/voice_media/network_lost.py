"""The media owner's spoken cut-off line: synthesized ahead of time, kept in memory and on disk."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import TYPE_CHECKING

from jarvis.shared import lang
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface.voice_media.pcm import SegmentResampler
from jarvis.surface.voice_tts import TTSAudioChunk, TTSResponseSegment

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.surface.voice_media.config import StreamingMediaConfig
    from jarvis.surface.voice_media.contract import StreamingTTSProvider
    from jarvis.surface.voice_media.journal import PlaybackJournal
    from jarvis.surface.voice_media.shutdown_clock import ShutdownClock
    from jarvis.surface.voice_tts import TTSSession

LOGGER = logging.getLogger(__name__)

# The cut-off line is synthesized ahead of time as a response of its own; this
# id never reaches the Event Log's response rows, only tts.usage_observed.
_NETWORK_LOST_RESPONSE_ID = "network-lost-line"
_NETWORK_LOST_PREFETCH_TIMEOUT_S = 20.0
# A kept copy of the line is trusted this long, so a changed voice shows up within a month.
_NETWORK_LOST_KEEP_S = 30 * 24 * 3600.0


class NetworkLostCue:
    """The only writer of the cut-off line's audio; the actor's thread only (ADR 0378).

    It creates its task on the actor's loop and every call runs there. The owner decides when
    the line may be prefetched, through ``admitting``; the cue says how it is synthesized,
    kept and read.
    """

    def __init__(
        self,
        *,
        provider: StreamingTTSProvider,
        config: StreamingMediaConfig,
        clock: ShutdownClock,
        journal: PlaybackJournal,
        admitting: Callable[[], bool],
    ) -> None:
        """Synthesize the line with ``provider``, bounded by ``clock``, its usage in ``journal``.

        ``admitting`` answers whether the owner is awake and accepting.
        """
        self._provider = provider
        self._config = config
        self._clock = clock
        self._journal = journal
        self._admitting = admitting
        # Canonical PCM of the spoken "tts.network_lost" line, per language, in memory.
        self._network_lost_pcm: dict[str, bytes] = {}
        self._network_lost_task: asyncio.Task[None] | None = None

    def line(self, code: str) -> bytes | None:
        """The canonical PCM of the spoken line in ``code``, or ``None`` until it is cached."""
        return self._network_lost_pcm.get(code)

    def prefetch(self) -> None:
        """Once the network has just worked, have the cut-off line synthesized in the background."""
        if (
            not self._config.prefetch_network_lost_line
            or lang.language() in self._network_lost_pcm
            or self._network_lost_task is not None
            or not self._admitting()
        ):
            return
        self._network_lost_task = asyncio.create_task(
            self._synthesize_network_lost_line(lang.language()),
            name="tts-network-lost-prefetch",
        )

    async def close(self) -> None:
        """Shutdown: cancel a synthesis still running and wait for it, bounded."""
        if self._network_lost_task is not None:
            self._network_lost_task.cancel()
            await self._clock.wait_task_bounded(self._network_lost_task, timeout_s=0.5)

    async def _synthesize_network_lost_line(self, code: lang.Language) -> None:
        """Own session, no player, no response rows: a failure only leaves the tones."""
        session: TTSSession | None = None
        try:
            kept = await asyncio.to_thread(self._read_kept_network_lost_line, code)
            if kept is not None:
                self._network_lost_pcm[code] = kept
                record_realtime_trace("tts_network_lost_line_cached", language=code, source="disk")
                return
            session = self._provider.create_tts_session(
                endpoint_index=0,
                language=code,
                idle_close_s=self._config.session_idle_close_s,
                command_queue_capacity=self._config.session_command_capacity,
                audio_queue_capacity=self._config.session_audio_capacity,
            )
            parts: list[bytes] = []
            async with asyncio.timeout(_NETWORK_LOST_PREFETCH_TIMEOUT_S):
                await session.open(_NETWORK_LOST_RESPONSE_ID, 0)
                await session.send(
                    TTSResponseSegment(
                        response_id=_NETWORK_LOST_RESPONSE_ID,
                        playback_generation_id=0,
                        sequence=0,
                        text=lang.t("tts.network_lost", lang=code),
                    ),
                )
                resampler: SegmentResampler | None = None
                async for event in session.audio_events():
                    if isinstance(event, TTSAudioChunk):
                        if event.pcm:
                            resampler = resampler or SegmentResampler(
                                input_rate_hz=event.sample_rate_hz,
                                output_rate_hz=self._config.canonical_sample_rate_hz,
                            )
                            parts.append(resampler.feed(event.pcm))
                        continue
                    if resampler is not None:
                        parts.append(resampler.finish())
                    self._journal.tts_usage(
                        event.usage, response_id=_NETWORK_LOST_RESPONSE_ID, sequence=0,
                    )
                    break
            if parts:
                self._network_lost_pcm[code] = b"".join(parts)
                record_realtime_trace(
                    "tts_network_lost_line_cached",
                    language=code,
                    pcm_bytes=len(self._network_lost_pcm[code]),
                )
                await asyncio.to_thread(
                    self._keep_network_lost_line, code, self._network_lost_pcm[code],
                )
        except Exception:  # noqa: BLE001 - the tones remain the fallback
            LOGGER.debug("network-lost line was not synthesized", exc_info=True)
        finally:
            self._network_lost_task = None
            if session is not None:
                await self._clock.wait_task_bounded(
                    asyncio.create_task(session.close()), timeout_s=0.5,
                )

    def _network_lost_file(self, code: lang.Language) -> Path | None:
        """The kept audio of the line: named by its text and rate, so an edit starts afresh."""
        directory = self._config.network_lost_cache_dir
        if directory is None:
            return None
        key = f"{lang.t('tts.network_lost', lang=code)}|{self._config.canonical_sample_rate_hz}"
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        return directory / f"network-lost-{code}-{digest}.pcm"

    def _read_kept_network_lost_line(self, code: lang.Language) -> bytes | None:
        """The line's audio from an earlier run, or ``None`` when absent, stale or unreadable."""
        path = self._network_lost_file(code)
        try:
            if path is None or time.time() - path.stat().st_mtime > _NETWORK_LOST_KEEP_S:
                return None
            return path.read_bytes() or None
        except OSError:
            return None

    def _keep_network_lost_line(self, code: lang.Language, pcm: bytes) -> None:
        """Keep the line's audio for the next run; a disk that refuses only costs a call then."""
        path = self._network_lost_file(code)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".tmp")
            partial.write_bytes(pcm)
            partial.replace(path)
        except OSError:
            LOGGER.debug("network-lost line was not kept on disk", exc_info=True)
