"""Full-duplex interrupt monitoring during TTS playback.

Uses VAD to gate mic audio into per-utterance segments, then runs the
main ``SpeechRecognizer`` (SenseVoice offline) on each VAD-closed
segment, normalizes via ``ASRNormalizer`` (the same three-layer
pipeline main ASR uses), and fires the interrupt/resume callback if
the recognized text contains a keyword.

This replaces an earlier design that ran a separate streaming
transducer model (sherpa-onnx streaming-zipformer-small-bilingual).
That design was inconsistent with Jarvis' "one ASR stack" decision
(B4 in ``olv语音管线对比与优化方案.md``) and proved unable to
commit isolated short Chinese keywords like "停" in practice.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import numpy as np

LOGGER = logging.getLogger(__name__)

INTERRUPT_KEYWORDS = frozenset({
    "等一下", "停", "打住", "暂停", "等等", "你听我说",
    "不对", "你理解错了", "不是这样", "说错了",
})

RESUME_KEYWORDS = frozenset({
    "继续说", "接着说", "你继续", "继续",
})

# Pattern: keyword optionally followed by punctuation/space, then content
_STRIP_RE = re.compile(
    r"^(" + "|".join(re.escape(kw) for kw in sorted(INTERRUPT_KEYWORDS, key=len, reverse=True))
    + r")[，,。.！!？?\s]*",
)


def strip_interrupt_prefix(text: str) -> str:
    """Remove leading interrupt keyword + trailing punctuation from text.

    >>> strip_interrupt_prefix("停，改成多伦多的天气")
    '改成多伦多的天气'
    >>> strip_interrupt_prefix("明天天气怎么样")
    '明天天气怎么样'
    """
    return _STRIP_RE.sub("", text)


class InterruptMonitor:
    """Monitor audio during TTS playback for interrupt keywords.

    VAD gates mic audio into utterance segments. Each closed segment is
    transcribed offline via the shared ``SpeechRecognizer`` and
    normalized through ``ASRNormalizer``; the resulting text is then
    checked against keyword sets and callbacks fire on hit.

    Args:
        config: Application config dict (reads ``interrupt`` section).
        on_interrupt: Called when an interrupt keyword is detected.
        on_resume: Called when a resume keyword is detected.
        on_soft_pause: Called on VAD IDLE→ACTIVE (WP7 soft stop).
        on_soft_resume: Called on VAD ACTIVE→IDLE or no-keyword timeout.
        speech_recognizer: Optional shared ``SpeechRecognizer`` instance.
            If omitted, one is constructed on start() from config.
        asr_normalizer: Optional shared ``ASRNormalizer`` instance.
            If omitted, one is constructed on start() from config.
    """

    def __init__(
        self,
        config: dict,
        on_interrupt: Callable[[], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        on_soft_pause: Callable[[], None] | None = None,
        on_soft_resume: Callable[[], None] | None = None,
        speech_recognizer: Any | None = None,
        asr_normalizer: Any | None = None,
    ) -> None:
        self._full_config = config
        icfg = config.get("interrupt", {})
        self.enabled = bool(icfg.get("enabled", False))
        self._on_interrupt = on_interrupt
        self._on_resume = on_resume
        self._on_soft_pause = on_soft_pause
        self._on_soft_resume = on_soft_resume
        self._fired = False
        self._lock = threading.Lock()

        # WP7 soft-stop: pause TTS playback as soon as VAD detects user
        # speech, and resume on either VAD-end OR a no-keyword timeout.
        # Off by default — flip on after Allen verifies SIGSTOP+afplay
        # behavior on the target machine.
        self._soft_stop_enabled = bool(icfg.get("soft_stop_enabled", False))
        self._soft_stop_timeout_s = float(icfg.get("soft_stop_timeout_ms", 3000)) / 1000.0
        self._soft_state = "NORMAL"  # NORMAL | DUCKED | CANCELLED
        self._was_speech_detected = False
        self._soft_resume_timer: threading.Timer | None = None

        # Custom keyword sets from config (fallback to defaults)
        kw_list = icfg.get("keywords")
        self._interrupt_kw = frozenset(kw_list) if kw_list else INTERRUPT_KEYWORDS
        resume_list = icfg.get("resume_keywords")
        self._resume_kw = frozenset(resume_list) if resume_list else RESUME_KEYWORDS

        # Shared ASR stack (lazy-built if not injected)
        self._speech_recognizer: Any = speech_recognizer
        self._asr_normalizer: Any = asr_normalizer

        # VAD-segment audio accumulator (speech-active samples only).
        # Flushed to SenseVoice when VAD closes the segment.
        self._vad_speech_audio = np.zeros(0, dtype=np.float32)
        # Skip too-short VAD segments (clicks, chair creaks) — 150ms default.
        self._min_segment_samples = int(
            float(icfg.get("min_segment_ms", 150)) * 16000 / 1000,
        )

        # Pre-roll ring buffer: keep the last N ms of audio so that when
        # VAD opens we can prepend context to the segment. Without this
        # the first ~96ms of speech (below VAD required_hits threshold)
        # is clipped, which strips the initial consonant of short words
        # like "停" and makes SenseVoice mis-recognize them as "嗯".
        # Empirically +500ms pre / +200ms post fixes isolated "停" at
        # the cost of ~45KB of rolling memory at 16 kHz float32.
        self._preroll_samples = int(
            float(icfg.get("preroll_ms", 500)) * 16000 / 1000,
        )
        self._postroll_samples = int(
            float(icfg.get("postroll_ms", 200)) * 16000 / 1000,
        )
        self._preroll_buffer = np.zeros(0, dtype=np.float32)
        # After VAD closes, keep feeding this many samples into the
        # pending segment before actually dispatching transcribe.
        self._postroll_remaining = 0
        self._pending_segment = np.zeros(0, dtype=np.float32)

        # Audio accumulator for post-interrupt re-transcription
        # (the full unsegmented stream, used by the main pipeline when
        # the user interrupts to pick up what they said next).
        self._audio_chunks: list[np.ndarray] = []
        self._recording = False

        # Silero VAD gate (loaded lazily in start() for symmetry)
        self._vad: Any = None
        self._vad_config = icfg

        # Offline transcription is blocking (~100-200ms on Mac M-series).
        # Dispatch to a single-worker executor so the mic reader thread
        # doesn't stall and drop audio while SenseVoice runs.
        self._transcribe_executor: ThreadPoolExecutor | None = None

        # Mic listener state (initialized here for type-safety)
        self._mic_stop: threading.Event | None = None
        self._mic_stream: Any = None
        self._mic_thread: threading.Thread | None = None

    def start(self) -> None:
        """Begin a monitoring session. Call before TTS playback starts.

        Lazy-builds the shared ASR stack if not injected via the
        constructor. Writes to mutable state go under ``self._lock`` so
        the mic-thread sees consistent values.
        """
        if not self.enabled:
            return
        self._ensure_asr_stack()
        self._load_vad()
        with self._lock:
            self._fired = False
            self._audio_chunks = []
            self._recording = True
            self._vad_speech_audio = np.zeros(0, dtype=np.float32)
            self._preroll_buffer = np.zeros(0, dtype=np.float32)
            self._pending_segment = np.zeros(0, dtype=np.float32)
            self._postroll_remaining = 0
            self._vad_was_active = False
            self._soft_state = "NORMAL"
            self._was_speech_detected = False
            self._cancel_soft_timer_locked()
        if self._vad is not None:
            self._vad.reset()
        # Start a per-session worker so a stray future from a previous
        # session can't race the next one's state.
        if self._transcribe_executor is None:
            self._transcribe_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="interrupt-asr",
            )

    def stop(self) -> np.ndarray | None:
        """End monitoring session. Returns accumulated audio or None.

        Single critical section covers ``_recording`` flip, timer cancel,
        and soft-state read/clear. This prevents a timer callback from
        racing state inspection — whichever thread wins the lock decides
        whether on_soft_resume fires (resume_playback is idempotent so
        a duplicate from the other thread is harmless).
        """
        with self._lock:
            self._recording = False
            self._cancel_soft_timer_locked()
            should_resume = (
                self._soft_stop_enabled
                and self._soft_state == "DUCKED"
                and self._on_soft_resume is not None
            )
            self._soft_state = "NORMAL"
            audio_chunks_snapshot = self._audio_chunks
            self._audio_chunks = []
            pending_segment = self._pending_segment
            self._pending_segment = np.zeros(0, dtype=np.float32)
            self._vad_speech_audio = np.zeros(0, dtype=np.float32)
            self._preroll_buffer = np.zeros(0, dtype=np.float32)
            self._postroll_remaining = 0
        if should_resume and self._on_soft_resume is not None:
            try:
                self._on_soft_resume()
            except Exception as exc:
                LOGGER.warning("on_soft_resume on stop() failed: %s", exc)
        # If we're stopping mid-utterance and the pending segment is
        # long enough to matter, give it one last chance to trigger.
        if pending_segment.size >= self._min_segment_samples:
            self._transcribe_segment(pending_segment)
        # Drain worker so any in-flight transcription completes (or is
        # discarded) before the caller moves on.
        if self._transcribe_executor is not None:
            self._transcribe_executor.shutdown(wait=True)
            self._transcribe_executor = None
        if audio_chunks_snapshot:
            return np.concatenate(audio_chunks_snapshot)
        return None

    def reset(self) -> None:
        """Reset fired state so new detections can trigger."""
        with self._lock:
            self._fired = False

    def feed_audio(self, audio: np.ndarray, sample_rate: int = 16000) -> None:
        """Feed an audio chunk for analysis.

        Accumulates the raw stream for post-interrupt replay regardless
        of VAD state; during VAD ACTIVE also accumulates into a
        per-segment buffer. At the VAD ACTIVE→IDLE edge, that segment
        is dispatched to SenseVoice for offline transcription.
        """
        if not self.enabled:
            return
        with self._lock:
            if not self._recording:
                return
            # Always accumulate full stream for post-interrupt replay
            # (unchanged behavior from the streaming-era monitor).
            if not self._fired:
                self._audio_chunks.append(audio.copy())

        if self._vad is None:
            return

        try:
            self._vad.accept_waveform(audio)
            is_speech = self._vad.is_speech_detected()
        except Exception as exc:
            LOGGER.debug("VAD gate error: %s", exc)
            return

        if self._soft_stop_enabled:
            self._update_soft_state(is_speech)

        # Segment accumulation with pre/post-roll:
        #   IDLE state: maintain rolling pre-roll buffer only.
        #   IDLE→ACTIVE edge: seed segment with pre-roll buffer + this chunk.
        #   ACTIVE state: append chunk to segment.
        #   ACTIVE→IDLE edge: arm post-roll counter; keep appending until it
        #     drains, then flush to transcribe.
        #   IDLE after post-roll drain: flush pending + reset.
        segment_to_flush: np.ndarray | None = None
        with self._lock:
            prev_state = self._vad_was_active
            if is_speech:
                if not prev_state:
                    # Edge IDLE→ACTIVE: prepend the pre-roll context so
                    # SenseVoice sees the initial consonant/onset.
                    self._pending_segment = np.concatenate(
                        [self._preroll_buffer, audio],
                    )
                    self._postroll_remaining = 0
                else:
                    self._pending_segment = np.concatenate(
                        [self._pending_segment, audio],
                    )
            else:
                if prev_state:
                    # Edge ACTIVE→IDLE: start collecting post-roll tail.
                    self._pending_segment = np.concatenate(
                        [self._pending_segment, audio],
                    )
                    self._postroll_remaining = self._postroll_samples - audio.size
                    if self._postroll_remaining <= 0:
                        if self._pending_segment.size >= self._min_segment_samples:
                            segment_to_flush = self._pending_segment
                        self._pending_segment = np.zeros(0, dtype=np.float32)
                        self._postroll_remaining = 0
                elif self._postroll_remaining > 0:
                    # Still draining post-roll after VAD closed.
                    self._pending_segment = np.concatenate(
                        [self._pending_segment, audio],
                    )
                    self._postroll_remaining -= audio.size
                    if self._postroll_remaining <= 0:
                        if self._pending_segment.size >= self._min_segment_samples:
                            segment_to_flush = self._pending_segment
                        self._pending_segment = np.zeros(0, dtype=np.float32)
                        self._postroll_remaining = 0
                else:
                    # Pure IDLE: keep the pre-roll ring fresh.
                    self._preroll_buffer = np.concatenate(
                        [self._preroll_buffer, audio],
                    )
                    if self._preroll_buffer.size > self._preroll_samples:
                        self._preroll_buffer = self._preroll_buffer[
                            -self._preroll_samples:
                        ]
            self._vad_was_active = is_speech

        if segment_to_flush is not None:
            self._dispatch_transcribe(segment_to_flush)

    # ------------------------------------------------------------------
    # ASR stack (shared with main pipeline)
    # ------------------------------------------------------------------

    def _ensure_asr_stack(self) -> None:
        """Lazy-build SpeechRecognizer + ASRNormalizer if not injected."""
        if self._speech_recognizer is None:
            try:
                from core.speech_recognizer import SpeechRecognizer
                self._speech_recognizer = SpeechRecognizer(self._full_config)
                LOGGER.info(
                    "Interrupt path: built own SpeechRecognizer "
                    "(consider injecting from main pipeline to avoid "
                    "loading the model twice)",
                )
            except Exception as exc:
                LOGGER.warning("Cannot build SpeechRecognizer: %s", exc)
        if self._asr_normalizer is None:
            try:
                from core.asr_normalizer import ASRNormalizer
                self._asr_normalizer = ASRNormalizer(self._full_config)
            except Exception as exc:
                LOGGER.warning("Cannot build ASRNormalizer: %s", exc)

    def _dispatch_transcribe(self, segment: np.ndarray) -> None:
        """Hand off a closed VAD segment to the worker thread."""
        if self._transcribe_executor is None:
            self._transcribe_segment(segment)
            return
        try:
            self._transcribe_executor.submit(self._transcribe_segment, segment)
        except RuntimeError:
            # Executor shut down between the check and the submit —
            # just drop the segment, we're exiting anyway.
            pass

    def _transcribe_segment(self, segment: np.ndarray) -> None:
        """Transcribe one VAD-closed segment and check for keywords."""
        if self._speech_recognizer is None:
            return
        try:
            result = self._speech_recognizer.transcribe(segment)
        except Exception as exc:
            LOGGER.debug("Interrupt transcribe error: %s", exc)
            return
        text = getattr(result, "text", "") or ""
        if self._asr_normalizer is not None:
            try:
                text = self._asr_normalizer.normalize(text)
            except Exception as exc:
                LOGGER.debug("Interrupt normalize error: %s", exc)
        if text:
            self._check_partial(text)

    # ------------------------------------------------------------------
    # Keyword match
    # ------------------------------------------------------------------

    def _check_partial(self, text: str) -> None:
        """Check a recognized text segment against keyword sets."""
        if not self.enabled:
            return
        callback = None
        with self._lock:
            if self._fired:
                return
            for kw in self._interrupt_kw:
                if kw in text:
                    self._fired = True
                    callback = self._on_interrupt
                    # Hard interrupt: cancel any pending soft-resume so
                    # we don't accidentally SIGCONT after stop() kills it.
                    self._soft_state = "CANCELLED"
                    self._cancel_soft_timer_locked()
                    break
            if callback is None:
                for kw in self._resume_kw:
                    if kw in text:
                        self._fired = True
                        callback = self._on_resume
                        break
        if callback:
            callback()

    # ------------------------------------------------------------------
    # WP7 soft-stop helpers
    # ------------------------------------------------------------------

    def _update_soft_state(self, is_speech: bool) -> None:
        """Drive NORMAL → DUCKED → NORMAL transitions on VAD edges."""
        callback: Callable[[], None] | None = None
        with self._lock:
            prev = self._was_speech_detected
            self._was_speech_detected = is_speech

            if (
                not prev and is_speech
                and self._soft_state == "NORMAL"
                and self._on_soft_pause is not None
            ):
                self._soft_state = "DUCKED"
                self._start_soft_timer_locked()
                callback = self._on_soft_pause
            elif (
                prev and not is_speech
                and self._soft_state == "DUCKED"
            ):
                # VAD ended without a keyword landing — resume playback
                # immediately rather than waiting for the timer.
                self._soft_state = "NORMAL"
                self._cancel_soft_timer_locked()
                callback = self._on_soft_resume
        if callback is not None:
            try:
                callback()
            except Exception as exc:
                LOGGER.warning("soft-stop callback failed: %s", exc)

    def _start_soft_timer_locked(self) -> None:
        """Schedule no-keyword timeout. Caller must hold ``self._lock``."""
        if self._soft_resume_timer is not None:
            self._soft_resume_timer.cancel()
        timer = threading.Timer(self._soft_stop_timeout_s, self._on_soft_timeout)
        timer.daemon = True
        self._soft_resume_timer = timer
        timer.start()

    def _on_soft_timeout(self) -> None:
        """Fired by the timer: if still DUCKED, resume playback."""
        callback: Callable[[], None] | None = None
        with self._lock:
            if self._soft_state == "DUCKED":
                self._soft_state = "NORMAL"
                callback = self._on_soft_resume
            self._soft_resume_timer = None
        if callback is not None:
            try:
                callback()
            except Exception as exc:
                LOGGER.warning("soft-stop timeout callback failed: %s", exc)

    def _cancel_soft_timer_locked(self) -> None:
        if self._soft_resume_timer is not None:
            self._soft_resume_timer.cancel()
            self._soft_resume_timer = None

    # ------------------------------------------------------------------
    # Mic listener
    # ------------------------------------------------------------------

    def start_mic_listener(self, sample_rate: int = 16000, block_size: int = 1600) -> None:
        """Open a microphone stream and feed audio to the monitor."""
        if not self.enabled:
            return
        if self._mic_stream is not None:
            LOGGER.warning("start_mic_listener called while already running; ignoring")
            return
        import sounddevice as sd
        self._mic_stop = threading.Event()
        self._mic_stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=block_size,
        )
        self._mic_stream.start()

        # Capture stream in closure so reader never dereferences self._mic_stream
        _stream = self._mic_stream
        _stop = self._mic_stop

        def _reader() -> None:
            while not _stop.is_set():
                try:
                    data, _ = _stream.read(block_size)
                    self.feed_audio(data[:, 0], sample_rate)
                except Exception as exc:
                    if not _stop.is_set():
                        LOGGER.warning("Mic listener reader exited: %s", exc)
                    break

        self._mic_thread = threading.Thread(target=_reader, daemon=True, name="interrupt-mic")
        self._mic_thread.start()
        LOGGER.debug("Interrupt mic listener started")

    def stop_mic_listener(self) -> None:
        """Stop the background microphone stream.

        Shutdown order matters:
          1. Set the stop event — tells the reader loop to exit at its
             next top-of-while check.
          2. Join the reader thread — wait for it to finish its current
             ``InputStream.read()`` call (≤ one block, ~100ms at 16 kHz
             blocksize=1600) and exit the loop.
          3. Only THEN stop/close the stream — safe now that no one is
             reading from its C-level buffer.

        Reversed order (close-before-join, prior behavior) could free the
        stream while the reader was mid-``read()`` in PortAudio, leading
        to a use-after-free bus error / segfault at process exit.
        Migration 2026-04-17 "未做尾巴 #2"; fixed here.
        """
        if self._mic_stop is not None:
            self._mic_stop.set()
        if self._mic_thread is not None:
            self._mic_thread.join(timeout=2)
            if self._mic_thread.is_alive():
                LOGGER.warning("interrupt-mic thread did not exit within 2s")
            self._mic_thread = None
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None
        self._mic_stop = None
        LOGGER.debug("Interrupt mic listener stopped")

    # ------------------------------------------------------------------
    # VAD loading (unchanged)
    # ------------------------------------------------------------------

    def _load_vad(self) -> None:
        """Lazy-load the VAD gate (provider chosen via config)."""
        if self._vad is not None:
            return
        model_path = self._vad_config.get("vad_model_path", "")
        if not model_path:
            LOGGER.info("No VAD model configured; interrupt gate disabled")
            return
        from core.vad_silero import build_vad
        self._vad = build_vad(self._vad_config, mode="tts")
        provider = self._vad_config.get("vad_provider", "silero_direct")
        LOGGER.info(
            "Interrupt VAD loaded (provider=%s, model=%s)", provider, model_path,
        )

    # Track VAD state for edge detection (accessed under self._lock).
    # Declared here so instance attribute exists even before start().
    _vad_was_active: bool = False
