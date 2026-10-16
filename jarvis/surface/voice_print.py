"""ADR 0286: the speaker embedding of an utterance, computed beside its final ASR.

A terminal that hears the owner sends each utterance with a ``voiceprint`` (the embedding, the
speech seconds, the microphone that heard it and how long its stream has been open); the brain
scores it (:mod:`jarvis.state.voiceprint`). The embedding is sherpa-onnx's 3D-Speaker CAM++
(``3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx``, 192 numbers), about 30 ms for
3 s of speech on the Mac's CPU; :meth:`VoiceprintExtractor.begin` runs it on a thread of its own
while the final ASR works, so it adds nothing to the path.

Any failure is no voiceprint: the utterance goes without the field and is judged as it was.

Layer rules: stdlib, numpy and ``sherpa_onnx`` only.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Final

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

LOGGER = logging.getLogger("jarvis.surface.voice_print")

_DECIMALS: Final = 5
_BYTES_PER_SAMPLE: Final = 2


class VoiceprintExtractor:
    """The speaker-embedding model at ``model_path``, loaded on the first utterance."""

    def __init__(self, model_path: Path) -> None:
        """Remember where the model is; nothing is read yet."""
        self._path = model_path
        self._lock = threading.Lock()
        self._extractor: Any = None
        self._failed = False

    def begin(
        self, pcm16: bytes, sample_rate_hz: int, *, seconds: float, mic: str, mic_age_s: float,
    ) -> Callable[[], dict[str, Any] | None]:
        """Start the embedding of ``pcm16`` on its own thread; the result waits for it.

        The result is the ``voiceprint`` object of the wire (``embedding`` rounded to five
        decimals, ``seconds`` of speech, ``mic`` and ``mic_age_s``), or ``None`` when it could
        not be made.
        """
        done: Future[dict[str, Any] | None] = Future()

        def work() -> None:
            try:
                vector = self._embed(pcm16, sample_rate_hz)
            except Exception:  # noqa: BLE001 - a voiceprint never costs the owner his words
                LOGGER.warning("voiceprint: the embedding failed; none is sent", exc_info=True)
                vector = None
            done.set_result(None if vector is None else {
                "embedding": [round(float(one), _DECIMALS) for one in vector],
                "seconds": round(seconds, 2), "mic": mic, "mic_age_s": round(mic_age_s, 1),
            })

        threading.Thread(target=work, name="voiceprint", daemon=True).start()
        return done.result

    def _embed(self, pcm16: bytes, sample_rate_hz: int) -> np.ndarray | None:
        samples = np.frombuffer(
            pcm16[: len(pcm16) // _BYTES_PER_SAMPLE * _BYTES_PER_SAMPLE], dtype="<i2",
        ).astype(np.float32) / 32768.0
        with self._lock:
            extractor = self._load()
            if extractor is None or samples.size == 0:
                return None
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate_hz, samples)  # sherpa resamples to 16 kHz
            stream.input_finished()
            if not extractor.is_ready(stream):
                return None
            vector = np.asarray(extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        return None if norm == 0.0 or not np.isfinite(norm) else vector / norm

    def _load(self) -> Any:  # noqa: ANN401 - sherpa_onnx typing is dynamic
        if self._extractor is not None or self._failed:
            return self._extractor
        try:
            import sherpa_onnx  # noqa: PLC0415

            LOGGER.info("Loading the speaker model from %s", self._path)
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=str(self._path), num_threads=1, provider="cpu",
                ),
            )
        except Exception:  # noqa: BLE001 - a broken model is none; said once, not at every turn
            LOGGER.warning("voiceprint: the speaker model did not load; none is sent",
                           exc_info=True)
            self._failed = True
        return self._extractor


def load_extractor(model_path: Path) -> VoiceprintExtractor | None:
    """The extractor for ``model_path``, or ``None`` (said once) when the model is not there."""
    if model_path.is_file():
        return VoiceprintExtractor(model_path)
    LOGGER.info(
        "voiceprint: no speaker model at %s; utterances go without a voiceprint", model_path,
    )
    return None
