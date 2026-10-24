"""The media owner's macOS ``say`` fallback and the janitors that reap its subprocesses."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis.shared import lang
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface.voice_ledger import StalePlaybackGeneration
from jarvis.surface.voice_media.pcm import SegmentResampler

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.surface.voice_media.config import StreamingMediaConfig
    from jarvis.surface.voice_media.response import ActiveResponse
    from jarvis.surface.voice_media.ring_sink import RingSink
    from jarvis.surface.voice_media.shutdown_clock import ShutdownClock
    from jarvis.surface.voice_tts import AudioStreamPlayer

LOGGER = logging.getLogger(__name__)

# The rate the say fallback renders at; the player resamples it like provider PCM.
_SAY_RATE_HZ = 24_000


class SayFallback:
    """The only writer of the fallback janitors; the actor's thread only (ADR 0383).

    It creates its subprocesses and janitor tasks on the actor's loop and every call runs there.
    The handles of one response's ``say`` live on its ``ActiveResponse``. The owner decides which
    response is the active one, through ``current``, and what an unproven cleanup does to the
    lane, through ``isolate``; the fallback says how ``say`` is run, cancelled and reaped.
    """

    def __init__(  # noqa: PLR0913 - explicit ownership dependencies
        self,
        *,
        player: AudioStreamPlayer,
        config: StreamingMediaConfig,
        clock: ShutdownClock,
        ring: RingSink,
        current: Callable[[], ActiveResponse | None],
        isolate: Callable[[ActiveResponse], None],
    ) -> None:
        """Say through ``player`` and ``ring``, reaped within ``clock``'s deadline.

        ``current`` answers which response is the owner's active one; ``isolate`` fails the lane
        closed for a response whose ``say`` could not be proven quiescent.
        """
        self._player = player
        self._config = config
        self._clock = clock
        self._ring = ring
        self._current = current
        self._isolate = isolate
        self._fallback_janitors: set[asyncio.Task[bool]] = set()

    def janitors_pending(self) -> bool:
        """Whether a janitor is still reaping a ``say`` subprocess or its late spawn."""
        return bool(self._fallback_janitors)

    async def drain(self) -> None:
        """Shutdown: wait until every janitor is done, however late its spawn returns."""
        while self._fallback_janitors:
            # Shutdown owns late-spawn debt too. Waiting here may make the
            # caller observe degraded bounded close, but the daemon actor
            # remains the sole janitor and will finish once the spawn returns.
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self._fallback_janitors)),
                return_exceptions=True,
            )

    async def run(
        self,
        active: ActiveResponse,
        segments: list[tuple[int, str, str]],
        *,
        budget: asyncio.Timeout,
    ) -> bool:
        """Say ``segments`` with macOS ``say``, through the player where it can.

        Each segment is rendered to a file and played like provider audio, so
        the echo canceller hears it, the soft barge-in can lower it and the
        cursor knows what was heard. Played straight to the speakers, as
        before, when rendering fails. The first segment is already open.
        """
        active.provider_label = "macos_say"
        record_realtime_trace(
            "tts_macos_say_started",
            response_id=active.response.response_id,
            playback_generation_id=active.lease.playback_generation_id,
            cursor_quality="rendered",
        )
        for index, (sequence, text, segment_hash) in enumerate(segments):
            budget.reschedule(
                asyncio.get_running_loop().time() + self._config.response_timeout_s,
            )
            if index > 0:
                self._ring.prepare_segment(active, sequence, text, segment_hash)
                opened = self._player.begin_generation_segment(
                    expected_playback_generation_id=active.lease.playback_generation_id,
                    sequence=sequence,
                    text=text,
                    segment_hash=segment_hash,
                )
                if isinstance(opened, StalePlaybackGeneration):
                    return False
            rendered = await self._render_macos_say(active, text)
            if self._current() is not active:
                return False
            if rendered is None:
                LOGGER.warning("say could not render to a file; saying the rest aloud directly")
                return await self._run_macos_say_aloud(active, segments[index:])
            pcm, rate_hz = rendered
            resampler = SegmentResampler(
                input_rate_hz=rate_hz, output_rate_hz=self._config.canonical_sample_rate_hz,
            )
            await self._ring.write_all(
                active, resampler.feed(pcm) + resampler.finish(), sequence=sequence,
            )
            finished = self._player.finish_generation_segment(
                expected_playback_generation_id=active.lease.playback_generation_id,
                sequence=sequence,
            )
            if isinstance(finished, StalePlaybackGeneration):
                return False
        return True

    async def _say_process(
        self, active: ActiveResponse, *args: str,
    ) -> int | None:
        """Run ``say *args`` under the fallback's cancel ownership; its exit code."""
        spawn_task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                "say",
                "-v",
                lang.say_voice(),
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            ),
            name=f"media-say-spawn-{active.response.response_id}",
        )
        active.fallback_spawn_task = spawn_task
        try:
            process = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            raise
        except OSError:
            active.fallback_spawn_task = None
            return None
        active.fallback_spawn_task = None
        active.fallback_process = process
        return_code = await process.wait()
        if active.fallback_process is process:
            active.fallback_process = None
        return return_code

    async def _render_macos_say(
        self, active: ActiveResponse, text: str,
    ) -> tuple[bytes, int] | None:
        """``text`` as ``say``'s mono int16 samples and their rate, or None."""
        with tempfile.TemporaryDirectory(prefix="jarvis-say-") as folder:
            path = Path(folder) / "say.wav"
            return_code = await self._say_process(
                active,
                "-o",
                str(path),
                "--file-format=WAVE",
                f"--data-format=LEI16@{_SAY_RATE_HZ}",
                text,
            )
            if return_code != 0:
                return None
            try:
                with wave.open(str(path), "rb") as reader:
                    if reader.getsampwidth() != 2 or reader.getnchannels() != 1:  # noqa: PLR2004
                        return None
                    return reader.readframes(reader.getnframes()), reader.getframerate()
            except (OSError, EOFError, wave.Error):
                return None

    async def _run_macos_say_aloud(
        self,
        active: ActiveResponse,
        segments: list[tuple[int, str, str]],
    ) -> bool:
        """Say ``segments`` straight to the speakers; the cursor stays unknown."""
        speech = "".join(text for _sequence, text, _hash in segments)
        return_code = await self._say_process(active, speech)
        if return_code != 0 or self._current() is not active:
            return False
        for index, (sequence, text, segment_hash) in enumerate(segments):
            if index > 0:
                opened = self._player.begin_generation_segment(
                    expected_playback_generation_id=active.lease.playback_generation_id,
                    sequence=sequence,
                    text=text,
                    segment_hash=segment_hash,
                )
                if isinstance(opened, StalePlaybackGeneration):
                    return False
            finished = self._player.finish_generation_segment(
                expected_playback_generation_id=active.lease.playback_generation_id,
                sequence=sequence,
            )
            if isinstance(finished, StalePlaybackGeneration):
                return False
        return True

    async def cancel(
        self,
        active: ActiveResponse,
    ) -> None:
        """Cancel and reap ``active``'s ``say``, joining a janitor that is already at it."""
        existing_janitor = active.fallback_janitor_task
        if existing_janitor is not None:
            quiescent = await asyncio.shield(existing_janitor)
            if active.fallback_janitor_task is existing_janitor:
                active.fallback_janitor_task = None
            if not quiescent:
                self._isolate(active)
            return
        spawn_task = active.fallback_spawn_task
        owned_process = active.fallback_process
        if spawn_task is None and owned_process is None:
            return
        # Claim every handle and publish the janitor before the first await.
        # The response task and foreground interrupt can enter cleanup
        # concurrently, but the second caller will now join the same debt.
        active.fallback_spawn_task = None
        active.fallback_process = None
        janitor = asyncio.create_task(
            self._cleanup_fallback_handles(spawn_task, owned_process),
            name=f"media-say-janitor-{active.response.response_id}",
        )
        active.fallback_janitor_task = janitor
        self._fallback_janitors.add(janitor)
        janitor.add_done_callback(self._fallback_janitors.discard)
        quiescent = await asyncio.shield(janitor)
        if active.fallback_janitor_task is janitor:
            active.fallback_janitor_task = None
        if not quiescent:
            self._isolate(active)

    async def _cleanup_fallback_handles(
        self,
        spawn_task: asyncio.Task[asyncio.subprocess.Process] | None,
        owned_process: asyncio.subprocess.Process | None,
    ) -> bool:
        """Own claimed spawn/process handles through terminate/kill/reap."""
        spawned_process: asyncio.subprocess.Process | None = None
        if spawn_task is not None:
            spawn_task.cancel()
            _done, pending = await asyncio.wait(
                {spawn_task},
                timeout=self._clock.remaining_s(0.25),
            )
            if pending:
                try:
                    spawned_process = await spawn_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    LOGGER.exception("late macOS say spawn failed")
            elif not spawn_task.cancelled() and spawn_task.exception() is None:
                spawned_process = spawn_task.result()
        processes = [
            process
            for process in (spawned_process, owned_process)
            if process is not None
        ]
        for index, process in enumerate(processes):
            if index > 0 and process is processes[0]:
                continue
            if not await self._terminate_process(process):
                return False
        return True

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> bool:
        """Terminate, kill if needed, and never report quiescent before reap."""
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        wait_task = asyncio.create_task(process.wait())
        done, _pending = await asyncio.wait(
            {wait_task},
            timeout=self._clock.remaining_s(0.25),
        )
        if done:
            try:
                wait_task.result()
            except asyncio.CancelledError:
                LOGGER.warning("macOS say wait was cancelled after terminate; forcing kill")
            except Exception as exc:  # noqa: BLE001 - cleanup must still force kill
                LOGGER.warning("macOS say wait failed after terminate; forcing kill: %r", exc)
            else:
                return True
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        reap_task = wait_task if not wait_task.done() else asyncio.create_task(process.wait())
        try:
            # Reap completion is presentation ownership debt. A pathological
            # child may make close() return degraded, but cannot overlap a
            # successor or pin process exit because the actor is daemon-backed.
            await asyncio.shield(reap_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("macOS say could not be reaped after kill")
            return False
        return True
