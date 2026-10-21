"""L5 wake-word engine — microWakeWord behind one ``predict`` contract (ADR-0042).

:class:`MicroWakeWordEngine` wraps pymicro-wakeword's ``hey_jarvis`` model behind a single
``predict(frame_bytes) -> dict`` contract, so tests can substitute a ``MagicMock``. The
single audio ingress (:mod:`jarvis.surface.voice_session`) is its only user.

Layer rules: stdlib only. ``pymicro_wakeword`` is lazy-imported inside :meth:`start` so this
module imports cleanly in test envs that omit the wheel.
"""
from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger("jarvis.surface.voice_wake")


class MicroWakeWordEngine:
    """microWakeWord behind the same ``predict`` / ``reset`` / ``close`` contract.

    pymicro-wakeword streams 10 ms feature windows and runs one INT8 inference
    every three of them; an 80 ms frame therefore yields two or three
    probabilities and :meth:`predict` reports the highest, so a caller's
    per-frame threshold check sees the same peak the bench script scored.

    Args:
        model_name: a pymicro-wakeword builtin model id. Default
            ``"hey_jarvis"`` (v2, ``probability_cutoff`` 0.97 upstream on a
            5-inference average; the daemon applies ``realtime.wake_threshold``
            to each raw inference instead).
    """

    def __init__(self, *, model_name: str = "hey_jarvis") -> None:
        """Store config; model and feature frontend are built in :meth:`start`."""
        self._model_name = model_name
        self._model: Any = None
        self._features: Any = None

    @property
    def model_name(self) -> str:
        """The model id this engine reports against in ``predict`` output."""
        return self._model_name

    def start(self) -> None:
        """Load the builtin tflite model and its feature frontend."""
        import contextlib  # noqa: PLC0415
        import io  # noqa: PLC0415

        from pymicro_wakeword import (  # noqa: PLC0415
            MicroWakeWord,
            MicroWakeWordFeatures,
        )
        from pymicro_wakeword.const import (  # noqa: PLC0415
            Model,
        )

        # The package prints its model config to stdout while loading.
        with contextlib.redirect_stdout(io.StringIO()):
            self._model = MicroWakeWord.from_builtin(Model(self._model_name))
        self._features = MicroWakeWordFeatures()
        LOGGER.info("wake: microwakeword engine started (model=%s)", self._model_name)

    def predict(self, frame_bytes: bytes) -> dict[str, float]:
        """Score one 80 ms PCM16 frame; the peak probability inside it."""
        model, features = self._model, self._features
        if model is None or features is None:
            return {}
        peak = 0.0
        for window in features.process_streaming(frame_bytes):
            prob = model.process_streaming_prob(window)
            if prob is not None and prob > peak:
                peak = float(prob)
        return {self._model_name: peak}

    def reset(self) -> None:
        """Drop buffered features and the model's stride window (post-detection)."""
        if self._model is not None:
            self._model.reset()
        if self._features is not None:
            self._features.reset()

    def close(self) -> None:
        """Release model handles (idempotent)."""
        if self._model is not None:
            self._model.close()
        self._model = None
        self._features = None


__all__ = ["MicroWakeWordEngine"]
