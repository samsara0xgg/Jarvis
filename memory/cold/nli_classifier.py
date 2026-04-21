"""NLI-based outcome signal classifier using Erlangshen-Roberta-110M-NLI.

Model: IDEA-CCNL/Erlangshen-Roberta-110M-NLI (Apache 2.0)
ONNX INT8 quantized, ~98 MB. Label order: 0=CONTRADICTION, 1=NEUTRAL, 2=ENTAILMENT.

Backup (not loaded, noted for reference only):
  MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7 (317 MB INT8,
  80.3% zh XNLI + 85.7% en MNLI — multilingual, larger, for future consideration).
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import onnxruntime as ort
    from transformers import PreTrainedTokenizerBase

LOGGER = logging.getLogger(__name__)

# Label indices from config.json: {0: CONTRADICTION, 1: NEUTRAL, 2: ENTAILMENT}
_IDX_CONTRADICTION = 0
_IDX_NEUTRAL = 1
_IDX_ENTAILMENT = 2


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x))
    return e / e.sum()


class NLIClassifier:
    """Erlangshen-Roberta-110M-NLI ONNX wrapper with lazy loading.

    Thread-safe: concurrent callers block on the first load, then share
    the session. All subsequent calls are lock-free reads.
    """

    def __init__(self, model_dir: str | Path = "data/nli-erlangshen") -> None:
        """Store path. Model loads on first classify() call."""
        self._model_dir = Path(model_dir)
        self._session: "ort.InferenceSession | None" = None
        self._tokenizer: "PreTrainedTokenizerBase | None" = None
        self._init_lock = threading.Lock()
        self._input_name: str = "input_ids"

    def _lazy_load(self) -> None:
        if self._session is not None:
            return
        with self._init_lock:
            if self._session is not None:
                return
            model_path = self._model_dir / "model.onnx"
            if not model_path.exists():
                raise FileNotFoundError(
                    f"NLI model not found at {model_path}. "
                    "Run: python scripts/export_nli_onnx.py"
                )
            LOGGER.info("NLI classifier lazy load from %s", model_path)
            import onnxruntime as ort
            from transformers import AutoTokenizer

            sess_opts = ort.SessionOptions()
            sess_opts.intra_op_num_threads = 2
            sess_opts.graph_optimization_level = (
                ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            )
            self._session = ort.InferenceSession(
                str(model_path), sess_options=sess_opts
            )
            self._input_name = self._session.get_inputs()[0].name
            self._tokenizer = AutoTokenizer.from_pretrained(str(self._model_dir))
            LOGGER.info("NLI classifier ready")

    def classify(self, premise: str, hypothesis: str) -> dict[str, float]:
        """Return {entailment, neutral, contradiction} probabilities summing to 1.0.

        Lazy-loads model on first call. Thread-safe.

        Args:
            premise: The text to evaluate (e.g. user utterance).
            hypothesis: The NLI hypothesis to test against.

        Returns:
            Dict with keys 'entailment', 'neutral', 'contradiction'.
        """
        self._lazy_load()
        assert self._session is not None
        assert self._tokenizer is not None

        enc = self._tokenizer(
            premise,
            hypothesis,
            return_tensors="np",
            max_length=512,
            truncation=True,
            padding=True,
        )
        inputs = {k: v.astype(np.int64) for k, v in enc.items()
                  if k in {inp.name for inp in self._session.get_inputs()}}
        logits = self._session.run(None, inputs)[0][0]
        probs = _softmax(logits.astype(np.float32))
        return {
            "contradiction": float(probs[_IDX_CONTRADICTION]),
            "neutral": float(probs[_IDX_NEUTRAL]),
            "entailment": float(probs[_IDX_ENTAILMENT]),
        }

    # Thresholds: empirically calibrated against Erlangshen-Roberta-110M-NLI
    # on Chinese conversational feedback utterances. Lowered from config default
    # of 0.7 → 0.65 to capture mid-confidence positives ("你说得很对" ≈ 0.67)
    # without false-positive risk at 0.65 boundary (tested: "对了我还想问" = 0.65
    # strictly not above threshold → correctly returns None).
    _ENTAILMENT_THRESHOLD: float = 0.65

    # Hypothesis templates: shorter, factual-style hypotheses calibrated for
    # this model's NLI training distribution. E.g. "说话人表示认可" achieves
    # entailment 0.80 for "好的" vs 0.43 with longer phrasings.
    _HYP_POSITIVE: str = "说话人表示认可"
    _HYP_NEGATIVE: str = "说话人表示不认可"

    def detect_outcome(self, user_text: str) -> int | None:
        """Detect outcome signal from user text via NLI.

        Runs classify() twice: once for positive signal, once for negative.
        Negative checked first to prefer conservative labeling.

        Returns:
            -1 if entailment with negative hypothesis > threshold,
            +1 if entailment with positive hypothesis > threshold,
            None otherwise (ambiguous).
        """
        try:
            neg_scores = self.classify(user_text, self._HYP_NEGATIVE)
            if neg_scores["entailment"] > self._ENTAILMENT_THRESHOLD:
                return -1

            pos_scores = self.classify(user_text, self._HYP_POSITIVE)
            if pos_scores["entailment"] > self._ENTAILMENT_THRESHOLD:
                return 1
        except Exception:
            LOGGER.exception("NLI classify failed in detect_outcome")

        return None
