"""L5 wake-word listener — openwakeword daemon thread (ADR-0005 §5.1).

Two classes:

- :class:`WakeEngine` — thin wrapper around ``openwakeword.Model`` so the
  rest of the module can talk to a single ``predict(frame_bytes) -> dict``
  contract (and so unit tests can substitute a ``MagicMock``).

- :class:`WakeListener` — daemon thread that polls the engine, gates on
  :data:`voice_pipeline.VOICE_INPUT_LOCK` and an optional
  ``is_speaking_callable``, then drives one voice turn through
  :class:`voice_pipeline.VoicePipeline` per detection.

Per ADR §5.1, on detection the listener:

1. Tries a **non-blocking** acquire of ``VOICE_INPUT_LOCK``. On failure
   (PTT mid-turn) it logs INFO and drops the detection — ADR §F5 wake side.
2. **Holds** the lock for the entire capture + ASR span (ADR-0005 §8
   fix #2 — "one input stream at a time"). The lock is released in
   the wake side's ``finally``; the pipeline is invoked with
   ``lock_already_held=True`` so it skips its own acquire (which would
   deadlock on a non-reentrant ``threading.Lock``).
3. Broadcasts ``voice("listening", turn_id=...)`` via the injected
   broadcaster's worker-thread bridge.
4. Calls ``capture_callable()`` to record one utterance — under the
   lock so PTT cannot reach the mic mid-capture.
5. Broadcasts ``voice("transcribing", turn_id=...)``.
6. Calls ``pipeline.run_turn(audio, turn_id, channel="inherent_wake",
   language="zh-CN", lock_already_held=True)`` which handles normalize /
   empty filter / emit and broadcasts ``voice("accepted", ...)`` or
   ``voice("empty", ...)`` internally.

Per ADR §2 (no barge-in this ADR), the loop suspends while
``is_speaking_callable()`` returns ``True``.

Per ADR §F8, an unexpected exception in the loop body sleeps 2 s and
resumes (legacy parity self-heal).

Layer rules: imports only stdlib and ``jarvis.surface.voice_pipeline``
(sibling module). Does NOT name ``jarvis.decision``, ``jarvis.execution``,
``jarvis.deployment``, ``jarvis.runtime``, ``jarvis.cli``.
``openwakeword``, ``sounddevice``, and ``numpy`` are lazy-imported inside
methods so this module imports cleanly in test envs that omit those wheels.
"""
from __future__ import annotations

import logging
import secrets
import threading
from typing import TYPE_CHECKING, Protocol

from jarvis.surface import voice_pipeline

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.surface import voice_ducking


LOGGER = logging.getLogger("jarvis.surface.voice_wake")

# 80 ms at 16 kHz, the frame size openwakeword expects (legacy
# core/wake_word.py:113 — `frame_length` property).
_FRAME_SAMPLES = 1280
_FRAME_BYTES = _FRAME_SAMPLES * 2  # int16

# When TTS is speaking (no barge-in) or while waiting for the next frame
# in the zero-fallback path, sleep this long to avoid a hot loop. 20 ms is
# small enough that stop-event latency stays well under the 1 s test cap.
_IDLE_SLEEP_S = 0.02

# Self-heal cooldown after an unexpected per-iteration exception (ADR §F8).
_HEAL_SLEEP_S = 2.0


class _EnginePort(Protocol):
    """Subset of :class:`WakeEngine` that :class:`WakeListener` calls."""

    def predict(self, frame_bytes: bytes) -> dict[str, float]: ...
    def reset(self) -> None: ...


class _PipelinePort(Protocol):
    """Subset of :class:`voice_pipeline.VoicePipeline` the listener drives.

    ``lock_already_held`` is the wake-side toggle for the ADR-0005 §8
    fix #2 invariant: the listener owns ``VOICE_INPUT_LOCK`` for the
    full capture + ASR span (see :meth:`WakeListener._run_one_iter`),
    so the pipeline must skip its inner acquire / release.
    """

    def run_turn(
        self,
        *,
        audio_bytes: bytes,
        turn_id: str,
        channel: str,
        language: str,
        lock_already_held: bool = ...,
    ) -> object: ...


class _BroadcasterPort(Protocol):
    """Subset of :class:`InherentBroadcaster` the listener forwards through."""

    def broadcast_voice_sync(
        self, phase: str, *, turn_id: str, **payload: object,
    ) -> None: ...


class WakeEngine:
    """Thin wrapper over :class:`openwakeword.Model`.

    ``openwakeword`` is **lazy-imported** in :meth:`start` so module
    imports (including unit-test collection) do not require the wheel.
    The Model file itself is downloaded by openwakeword's bootstrap on
    first call if missing (legacy ``core/wake_word.py:WakeWordDetector.start``
    parity).

    Args:
        model_name: openwakeword model id. Default ``"hey_jarvis_v0.1"``
            matches ADR §4.2 / §5.1.
        inference_framework: ``"onnx"`` (default) or ``"tflite"``.
    """

    def __init__(
        self,
        *,
        model_name: str = "hey_jarvis_v0.1",
        inference_framework: str = "onnx",
    ) -> None:
        """Store config; model itself is constructed in :meth:`start`."""
        self._model_name = model_name
        self._inference_framework = inference_framework
        self._model: object | None = None  # openwakeword.Model lazy
        self._np_module: object | None = None  # numpy lazy

    @property
    def model_name(self) -> str:
        """The model id this engine reports against in ``predict`` output."""
        return self._model_name

    def start(self) -> None:
        """Initialize the underlying openwakeword model.

        Downloads the model assets on first run if not already cached
        (matches legacy ``core/wake_word.py`` behavior). Raises
        ``RuntimeError`` if the model still cannot be located after a
        download attempt (ADR §F1 — caller logs ERROR and skips wake
        thread).
        """
        from pathlib import Path  # noqa: PLC0415

        import numpy as np  # noqa: PLC0415
        import openwakeword  # noqa: PLC0415
        from openwakeword.model import (  # noqa: PLC0415
            Model,
        )
        from openwakeword.utils import (  # noqa: PLC0415
            download_models,
        )

        def _find_model_path() -> str | None:
            for path in openwakeword.get_pretrained_model_paths(
                self._inference_framework,
            ):
                if self._model_name in path and Path(path).exists():
                    return str(path)
            return None

        model_path = _find_model_path()
        if model_path is None:
            LOGGER.info(
                "wake: downloading openwakeword model assets for %s",
                self._model_name,
            )
            download_models([self._model_name])
            model_path = _find_model_path()
        if model_path is None:
            msg = (
                f"openwakeword model {self._model_name!r} not found for "
                f"framework={self._inference_framework!r}"
            )
            raise RuntimeError(msg)

        self._model = Model(
            wakeword_models=[model_path],
            inference_framework=self._inference_framework,
        )
        self._np_module = np
        LOGGER.info(
            "wake: openwakeword engine started (model=%s, framework=%s)",
            self._model_name,
            self._inference_framework,
        )

    def predict(self, frame_bytes: bytes) -> dict[str, float]:
        """Run one inference frame through the model.

        Args:
            frame_bytes: 1280 ``int16`` little-endian PCM samples
                (2560 bytes; 80 ms at 16 kHz).

        Returns:
            ``{model_name: probability, ...}`` mapping for every model
            this engine carries. Empty dict if the engine has not been
            started (paranoid fallback so the caller's threshold check
            cleanly returns ``0.0``).
        """
        model = self._model
        np_module = self._np_module
        if model is None or np_module is None:
            return {}
        # ``predict`` accepts a numpy int16 array shaped ``(N,)``.
        audio = np_module.frombuffer(frame_bytes, dtype=np_module.int16)  # type: ignore[attr-defined]
        result = model.predict(audio)  # type: ignore[attr-defined]
        return {str(k): float(v) for k, v in dict(result).items()}

    def reset(self) -> None:
        """Clear the model's accumulated audio features (post-detection)."""
        model = self._model
        if model is not None:
            model.reset()  # type: ignore[attr-defined]

    def close(self) -> None:
        """Release model handles (idempotent)."""
        self._model = None
        self._np_module = None


class WakeListener:
    """Daemon thread that turns wake detections into voice pipeline turns.

    The listener is *constructed* by ``runtime/inherent_loop.serve_inherent``
    (ADR §4.2 layer note); the constructor takes all dependencies by
    keyword so unit tests can substitute MagicMock duck-types freely
    (the type signatures use small :class:`typing.Protocol` ports rather
    than nominal classes for that reason).

    The thread is named ``"jarvis-wake"`` and runs as ``daemon=True`` so
    process shutdown does not block on it; explicit shutdown via
    :meth:`request_stop` is the supported path.

    Args:
        engine: anything implementing :class:`_EnginePort` (the
            production wiring uses :class:`WakeEngine`).
        pipeline: anything implementing :class:`_PipelinePort` (the
            production wiring uses :class:`voice_pipeline.VoicePipeline`).
        broadcaster: optional :class:`_BroadcasterPort`. ``None`` disables
            UI envelopes (e.g. CLI-only smoke runs).
        capture_callable: a no-argument callable returning ``bytes`` of
            int16 PCM. In production this is
            ``functools.partial(voice_audio.capture_utterance, ...)``;
            tests pass a ``MagicMock``.
        threshold: detection probability threshold (default 0.5 per ADR §5.1).
        is_speaking_callable: optional ``() -> bool`` predicate. While it
            returns ``True``, the loop sleeps without polling — ADR §2
            no-barge-in rule.
        model_name: the key into the engine's ``predict`` output dict.
            Default matches :class:`WakeEngine` default.
        frame_factory: optional ``() -> bytes`` returning one frame for
            the engine. Default produces 2560 zero bytes (test-safe; the
            engine's mock decides detections regardless of content).
            Production wires this to an ``sd.RawInputStream`` reader via
            :meth:`runtime/inherent_loop.serve_inherent`.
        ducker: optional :class:`voice_ducking.SystemAudioDucker`. When
            wired, the listener mutes system output (refcounted via
            :meth:`SystemAudioDucker.duck`) before invoking
            ``capture_callable`` and restores it after — ADR §5.1
            prevents the assistant's own TTS bleed-back into the mic.
            Ducker failures are logged at DEBUG and never break wake.
    """

    def __init__(  # noqa: PLR0913 — keyword-only deps form the composition boundary.
        self,
        *,
        engine: _EnginePort,
        pipeline: _PipelinePort,
        broadcaster: _BroadcasterPort | None,
        capture_callable: Callable[[], bytes],
        threshold: float = 0.5,
        is_speaking_callable: Callable[[], bool] | None = None,
        model_name: str = "hey_jarvis_v0.1",
        frame_factory: Callable[[], bytes] | None = None,
        ducker: voice_ducking.SystemAudioDucker | None = None,
    ) -> None:
        """Wire one listener. See class docstring for semantics."""
        self._engine = engine
        self._pipeline = pipeline
        self._broadcaster = broadcaster
        self._capture_callable = capture_callable
        self._threshold = threshold
        self._is_speaking_callable = is_speaking_callable
        self._model_name = model_name
        self._frame_factory = frame_factory or _zero_frame
        self._ducker = ducker
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- thread lifecycle -------------------------------------------------

    def start(self) -> None:
        """Spawn the daemon thread; no-op if already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="jarvis-wake",
            daemon=True,
        )
        self._thread.start()

    def request_stop(self) -> None:
        """Signal the loop to exit at its next iteration boundary."""
        self._stop_event.set()

    def join(self, *, timeout_s: float | None = None) -> None:
        """Wait for the thread to finish; idempotent if never started."""
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_s)

    def is_alive(self) -> bool:
        """Return whether the worker thread is still running."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    # -- internals --------------------------------------------------------

    def _broadcast(self, phase: str, *, turn_id: str, **payload: object) -> None:
        """Forward to the broadcaster's worker-thread bridge if present."""
        broadcaster = self._broadcaster
        if broadcaster is None:
            return
        try:
            broadcaster.broadcast_voice_sync(phase, turn_id=turn_id, **payload)
        except Exception:  # noqa: BLE001 — broadcaster errors must not kill loop
            LOGGER.debug(
                "wake: broadcast_voice_sync failed (phase=%s)",
                phase,
                exc_info=True,
            )

    def _run(self) -> None:
        """Thread main loop. Honors ``_stop_event`` at every iteration."""
        LOGGER.info("wake: listener thread started (threshold=%.2f)", self._threshold)
        while not self._stop_event.is_set():
            try:
                self._run_one_iter()
            except Exception:
                LOGGER.exception(
                    "wake: unexpected error; sleeping %.1fs",
                    _HEAL_SLEEP_S,
                )
                # Sleep in small chunks so request_stop() still bites quickly.
                _interruptible_sleep(self._stop_event, _HEAL_SLEEP_S)
        LOGGER.info("wake: listener thread stopped")

    def _run_one_iter(self) -> None:
        """One iteration of the poll loop.

        Split out so the outer :meth:`_run` can wrap every iteration in
        the self-heal try/except without smearing control flow.
        """
        # 1. No-barge-in: pause while TTS is speaking.
        if (
            self._is_speaking_callable is not None
            and self._is_speaking_callable()
        ):
            _interruptible_sleep(self._stop_event, _IDLE_SLEEP_S)
            return

        # 2. Read one frame (zero-fallback in test env; real stream in prod).
        frame = self._frame_factory()

        # 3. Inference.
        result = self._engine.predict(frame)
        prob = float(result.get(self._model_name, 0.0))
        if prob < self._threshold:
            return

        LOGGER.info("wake: detection prob=%.3f", prob)

        # 4. Non-blocking lock acquire. Drop if busy (ADR §F5 wake side).
        #    Hold the lock for the FULL capture + ASR span — ADR-0005 §8
        #    fix #2: "one input stream at a time". The legacy
        #    core/inherent_wake_listener.py holds across record+transcribe
        #    too; releasing here and reacquiring inside run_turn would
        #    open a window where PTT could grab the mic mid-capture.
        if not voice_pipeline.VOICE_INPUT_LOCK.acquire(blocking=False):
            LOGGER.info("wake: VOICE_INPUT_LOCK busy; dropping detection")
            return

        try:
            # 5. Mint turn id + broadcast listening.
            turn_id = "T" + secrets.token_hex(4)
            self._broadcast("listening", turn_id=turn_id)

            # 6. Capture under ducking (ADR §5.1 — prevents speaker bleed).
            audio_bytes = self._capture_with_ducking(turn_id=turn_id)
            if audio_bytes is None:
                return

            # 7. Transcribe + emit. The pipeline broadcasts accepted/empty
            #    itself. Pass lock_already_held=True so the (non-reentrant)
            #    VOICE_INPUT_LOCK is not re-acquired on the same thread.
            self._broadcast("transcribing", turn_id=turn_id)
            try:
                self._pipeline.run_turn(
                    audio_bytes=audio_bytes,
                    turn_id=turn_id,
                    channel="inherent_wake",
                    language="zh-CN",
                    lock_already_held=True,
                )
            except voice_pipeline.VoicePipelineEmptyError:
                # Pipeline already broadcast voice("empty", ...) internally.
                LOGGER.info("wake: empty utterance; turn_id=%s", turn_id)
            except Exception:
                LOGGER.exception("wake: pipeline error; turn_id=%s", turn_id)
                self._broadcast("error", turn_id=turn_id, reason="asr_error")
            finally:
                # Reset the engine's accumulated features so the next utterance
                # starts clean (legacy parity — process_frame returns True only
                # after model.reset()).
                try:
                    self._engine.reset()
                except Exception:  # noqa: BLE001 — reset is best-effort
                    LOGGER.debug("wake: engine.reset() failed", exc_info=True)
        finally:
            voice_pipeline.VOICE_INPUT_LOCK.release()

    def _capture_with_ducking(self, *, turn_id: str) -> bytes | None:
        """Capture one utterance with system output ducked (ADR §5.1).

        Returns the captured PCM bytes, or ``None`` if capture failed
        (an ``error`` envelope was already broadcast). Ducker failures
        are logged at DEBUG and never block capture.
        """
        ducked = False
        if self._ducker is not None:
            try:
                ducked = bool(self._ducker.duck())
            except Exception:  # noqa: BLE001 — ducker errors must not kill wake
                LOGGER.debug("wake: ducker.duck() failed", exc_info=True)
        try:
            try:
                return self._capture_callable()
            except Exception:
                LOGGER.exception("wake: capture failed; turn_id=%s", turn_id)
                self._broadcast("error", turn_id=turn_id, reason="capture_error")
                return None
        finally:
            if ducked and self._ducker is not None:
                try:
                    self._ducker.restore()
                except Exception:  # noqa: BLE001 — ducker errors must not kill wake
                    LOGGER.debug("wake: ducker.restore() failed", exc_info=True)


def _zero_frame() -> bytes:
    """Default frame factory: 1280 silent int16 samples (2560 bytes)."""
    return b"\x00" * _FRAME_BYTES


def _interruptible_sleep(stop_event: threading.Event, total_s: float) -> None:
    """Sleep up to ``total_s`` but wake immediately on stop signal."""
    # ``Event.wait`` returns True once set; False means timed out.
    stop_event.wait(timeout=total_s)


__all__ = ["WakeEngine", "WakeListener"]
