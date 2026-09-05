"""ADR-0005 §3 + §4.2 + §8 fix #3 — ASR canonicalization, recognizers, filter.

Three pieces live in this module per ADR-0005 §4.2:

1. ``AsrNormalizer`` — three-layer cascade fixing systematic ASR errors
   (homophones, near-homophones) without retraining the model. Layers
   apply in order; first layer that changes the text returns:

     Layer 1: manual override entries with required-context guard.
     Layer 2: structured alias -> canonical replacement.
     Layer 3: Levenshtein fuzzy fallback, off by default.

   Performance budget per call: < 10 ms.

2. ``AsrRecognizer`` Protocol + three concrete providers (SenseVoice /
   MLX Whisper / openai-whisper local). Each provider returns a
   ``TranscriptionResult``; downstream composition (normalize + filter
   + lock release) lives in ``voice_pipeline.py``.

   Confidence semantics are provider-specific (ADR-0005 §3): SenseVoice
   returns a binary 0.1 / 0.9 heuristic, whisper returns a log-prob mean.
   Downstream MUST NOT cross-compare confidence between providers.

3. ``is_empty_or_too_short`` — unified empty-utterance filter (ADR-0005
   §8 fix #3). Single source of truth shared by wake + PTT paths.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from jarvis.shared.realtime_trace import record_realtime_trace

LOGGER = logging.getLogger(__name__)

_SAMPLE_RATE = 16000

# SenseVoice sometimes inserts "。" or "，" before a sentence-final particle,
# e.g. "学会查汇率。了" -> "学会查汇率了". This cleanup is provider-specific so
# it lives next to the recogniser, not in the generic normalizer.
_MISPLACED_PERIOD = re.compile(r"[。，]([了吧啊呢嘛呀哦哈的吗啦噢])")

# WP2 T2.1: tightened — bare "灯" was too broad (路灯/灯笼/灯泡 all trigger).
# "暗"/"亮" removed (暗恋/暗号/漂亮 false positives). "打开"/"关闭" added so real
# verbs survive after removing the single-char variants.
_ACTION_WORDS: tuple[str, ...] = (
    "开",
    "关",
    "打开",
    "关闭",
    "调",
    "模式",
    "切换",
    "启动",
    "场景",
)


class AsrNormalizer:
    """Three-layer normalizer for ASR transcripts."""

    def __init__(
        self,
        *,
        corrections: list[dict[str, object]],
        aliases: Mapping[str, Iterable[str]],
        fuzzy_enabled: bool,
        fuzzy_max_distance: int = 2,
    ) -> None:
        """Build a normalizer from already-validated kwargs."""
        self._corrections: list[dict[str, object]] = [
            entry for entry in corrections if isinstance(entry, dict)
        ]
        self._aliases: list[tuple[str, str]] = self._flatten_aliases(aliases)
        # Preserve canonical names even when they only appear as their own
        # alias — Layer 2 skips identity rewrites but Layer 3 still needs to
        # treat the canonical itself as a fuzzy target.
        self._canonicals: tuple[str, ...] = tuple(
            c for c in aliases if isinstance(c, str) and c
        )
        self._fuzzy_enabled = bool(fuzzy_enabled)
        self._fuzzy_max_distance = int(fuzzy_max_distance)

        # Targets for Layer 3: canonical names plus all aliases (each
        # alias maps to its canonical, canonicals map to themselves).
        self._fuzzy_targets: dict[str, str] = self._build_fuzzy_targets()

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> AsrNormalizer:
        """Build from a legacy ``config.yaml``-style mapping.

        Recognised sections:
          - ``asr_corrections``: list of {pattern, replace, require_context}
          - ``asr_aliases``: dict of canonical_name -> list of aliases
          - ``asr_normalizer_fuzzy``: {enabled, max_distance}
        """
        raw_corrections = config.get("asr_corrections") or []
        corrections: list[dict[str, object]] = []
        if isinstance(raw_corrections, list):
            corrections = [
                entry for entry in raw_corrections if isinstance(entry, dict)
            ]
        raw_aliases = config.get("asr_aliases") or {}
        aliases: Mapping[str, Iterable[str]] = (
            raw_aliases if isinstance(raw_aliases, Mapping) else {}
        )
        fuzzy_cfg_raw = config.get("asr_normalizer_fuzzy") or {}
        fuzzy_cfg: Mapping[str, object] = (
            fuzzy_cfg_raw if isinstance(fuzzy_cfg_raw, Mapping) else {}
        )
        max_distance_raw = fuzzy_cfg.get("max_distance", 2)
        max_distance = (
            int(max_distance_raw) if isinstance(max_distance_raw, (int, str)) else 2
        )
        return cls(
            corrections=corrections,
            aliases=aliases,
            fuzzy_enabled=bool(fuzzy_cfg.get("enabled", False)),
            fuzzy_max_distance=max_distance,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def normalize(self, text: str) -> str:
        """Apply the three layers in order; return on the first change."""
        if not text:
            return text

        layered = self._apply_corrections(text)
        if layered != text:
            return layered

        layered = self._apply_aliases(text)
        if layered != text:
            return layered

        if self._fuzzy_enabled:
            layered = self._apply_fuzzy(text)
            if layered != text:
                return layered

        return text

    # ------------------------------------------------------------------
    # Layer 1: manual corrections with require_context guard
    # ------------------------------------------------------------------

    def _apply_corrections(self, text: str) -> str:
        changed = text
        for entry in self._corrections:
            pattern = str(entry.get("pattern", ""))
            replace = str(entry.get("replace", ""))
            ctx_raw = entry.get("require_context") or []
            ctx: list[str] = (
                [c for c in ctx_raw if isinstance(c, str)]
                if isinstance(ctx_raw, list)
                else []
            )
            if not pattern or not replace or not ctx:
                # require_context is mandatory — silently skip malformed entries
                # (logging here would spam every turn).
                continue
            if pattern not in changed:
                continue
            if not any(c in changed for c in ctx):
                continue
            changed = changed.replace(pattern, replace)
        return changed

    # ------------------------------------------------------------------
    # Layer 2: structured aliases
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten_aliases(
        raw: Mapping[str, Iterable[str]],
    ) -> list[tuple[str, str]]:
        """Flatten {canonical: [alias, ...]} to a length-desc sorted list.

        Sorting longest-first prevents short aliases ("灯") from rewriting
        substrings of longer aliases ("床头灯") before the longer one
        gets its chance.
        """
        flat: list[tuple[str, str]] = []
        for canonical, aliases in raw.items():
            if not isinstance(canonical, str) or not aliases:
                continue
            for alias in aliases:
                if not isinstance(alias, str) or not alias:
                    continue
                if alias == canonical:
                    continue
                flat.append((alias, canonical))
        flat.sort(key=lambda pair: len(pair[0]), reverse=True)
        return flat

    def _apply_aliases(self, text: str) -> str:
        # Return on first hit (longest alias wins via the length-desc sort).
        # Iterating after a hit risks chained replacements where the canonical
        # we just inserted gets eaten by a shorter later alias rule.
        for alias, canonical in self._aliases:
            if alias in text:
                return text.replace(alias, canonical)
        return text

    # ------------------------------------------------------------------
    # Layer 3: Levenshtein fuzzy fallback
    # ------------------------------------------------------------------

    def _build_fuzzy_targets(self) -> dict[str, str]:
        """Map every canonical+alias string to its canonical form."""
        targets: dict[str, str] = {}
        for alias, canonical in self._aliases:
            targets[alias] = canonical
            targets[canonical] = canonical
        for canonical in self._canonicals:
            targets.setdefault(canonical, canonical)
        return targets

    @staticmethod
    def _action_word_positions(text: str) -> set[int]:
        """Return all character positions covered by an action word in *text*.

        This lets the fuzzy loop skip windows that overlap with action verb
        spans, preventing e.g. the suffix "大" in "打开大蛋灯" from being
        included in the fuzzy candidate window "开大".
        """
        covered: set[int] = set()
        for w in _ACTION_WORDS:
            start = 0
            while True:
                idx = text.find(w, start)
                if idx == -1:
                    break
                for k in range(idx, idx + len(w)):
                    covered.add(k)
                start = idx + 1
        return covered

    def _apply_fuzzy(self, text: str) -> str:
        # Only fire when an action word is present — without one, a 2-char
        # window matching "客厅" by accident would corrupt unrelated text.
        if not any(w in text for w in _ACTION_WORDS):
            return text
        if not self._fuzzy_targets:
            return text

        # T2.1 guard: precompute positions covered by action words so we can
        # skip windows that overlap with them (action verbs must not be
        # fuzzy-replaced with device names).
        action_positions = self._action_word_positions(text)

        n = len(text)
        for window_size in range(2, min(6, n + 1)):
            for i in range(n - window_size + 1):
                # Skip if window overlaps any action-word position.
                if action_positions.intersection(range(i, i + window_size)):
                    continue
                window = text[i : i + window_size]
                # Skip windows that already match exactly — Layer 2 should
                # have caught those already; redoing here just wastes work.
                if window in self._fuzzy_targets:
                    continue
                for cand, canonical in self._fuzzy_targets.items():
                    # T2.2: strict length match — window must equal alias length.
                    # This avoids 2-char windows matching 4-char aliases and
                    # corrupting text by expanding position-wise. Canonical CAN
                    # be longer than alias (that's the point: users say short,
                    # system fills in the full name).
                    if len(cand) != window_size:
                        continue
                    d = _levenshtein(window, cand)
                    if d <= self._fuzzy_max_distance:
                        return text[:i] + canonical + text[i + window_size :]
        return text


def _levenshtein(a: str, b: str) -> int:
    """Standard DP Levenshtein distance. Hand-rolled to avoid a new dep."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


# ---------------------------------------------------------------------------
# Recognizer (ASR provider) — ADR-0005 §4.2
# ---------------------------------------------------------------------------


# Float32-domain RMS floor for the SenseVoice silence / single-char collapse
# (audio normalised to [-1, 1]; below this we treat the chunk as background).
_SENSEVOICE_FLOAT_RMS_FLOOR = 0.01


@dataclass(frozen=True)
class TranscriptionResult:
    """Output of an ASR ``recognize()`` call (spec §3.6.1).

    Confidence semantics are provider-specific (see ADR-0005 §3): SenseVoice
    returns a binary 0.1 / 0.9 heuristic, mlx-whisper returns a log-prob
    mean. Downstream consumers must NOT cross-compare confidence values
    between providers.
    """

    text: str
    confidence: float
    language_detected: str | None
    emotion: str | None


class AsrRecognizer(Protocol):
    """Structural ASR provider — implementations live below in this module."""

    def recognize(self, audio_pcm: bytes) -> TranscriptionResult:
        """Return a ``TranscriptionResult`` for mono 16 kHz PCM16 audio."""
        ...


def _pcm16_to_float32(audio_pcm: bytes) -> np.ndarray:
    """Decode PCM16 mono little-endian bytes to a float32 [-1, 1] waveform.

    Empty bytes return an empty array; callers must guard against zero-length
    audio before invoking provider engines.
    """
    if not audio_pcm:
        return np.zeros(0, dtype=np.float32)
    samples = np.frombuffer(audio_pcm, dtype=np.int16)
    return samples.astype(np.float32) / 32768.0


def _estimate_whisper_confidence(transcription: Mapping[str, Any]) -> float:
    """Confidence from Whisper segment log-probs (legacy parity)."""
    segments = transcription.get("segments") or []
    if not segments:
        return 0.0 if not str(transcription.get("text", "")).strip() else 0.5

    scores: list[float] = []
    for segment in segments:
        avg_logprob = float(segment.get("avg_logprob", -1.0))
        no_speech_prob = float(segment.get("no_speech_prob", 0.0))
        probability = float(np.exp(min(avg_logprob, 0.0)))
        scores.append(max(0.0, min(1.0, probability * (1.0 - no_speech_prob))))
    return float(np.mean(scores, dtype=np.float64))


class SenseVoiceRecognizer:
    """sherpa-onnx SenseVoice INT8 backend — primary for short CN utterances.

    Confidence is a binary 0.1 / 0.9 heuristic per ADR-0005 §3: low-RMS or
    single-character results collapse to 0.1 + empty text so the downstream
    empty-utterance filter can drop them without provider-specific logic.
    """

    def __init__(
        self,
        *,
        model_dir: Path,
        num_threads: int = 4,
        language: str | None = None,
    ) -> None:
        """Wire SenseVoice paths without loading the model (lazy on first call)."""
        self._model_dir = Path(model_dir)
        self._num_threads = int(num_threads)
        # Empty string == auto-detect for sherpa-onnx; preserve that meaning.
        self._language = language or ""
        self._recognizer: Any | None = None
        # ADR-0006 D7: one serialized ASR lane. Final and partial decodes
        # share this lock so a rolling partial can delay the final by at most
        # one bounded snapshot decode and never runs beside it.
        self._decode_lock = threading.Lock()

    def recognize(self, audio_pcm: bytes) -> TranscriptionResult:
        """Transcribe PCM16 mono 16 kHz audio with SenseVoice."""
        audio = _pcm16_to_float32(audio_pcm)
        if audio.size == 0:
            return TranscriptionResult(
                text="",
                confidence=0.0,
                language_detected=None,
                emotion=None,
            )

        result = self._decode(audio)
        text = _MISPLACED_PERIOD.sub(r"\1", result.text.strip())

        raw_lang = getattr(result, "lang", "") or ""
        language = raw_lang.strip("<|>") if raw_lang else (self._language or None)

        emotion_raw = getattr(result, "emotion", "") or ""
        emotion = emotion_raw.strip("<|>") if emotion_raw else None

        rms = float(np.sqrt(np.mean(audio**2)))
        if rms < _SENSEVOICE_FLOAT_RMS_FLOOR or len(text) <= 1:
            confidence = 0.1
            text = ""
        else:
            confidence = 0.9

        LOGGER.info(
            "SenseVoice: lang=%s emotion=%s conf=%.1f text=%r",
            language,
            emotion,
            confidence,
            text,
        )
        return TranscriptionResult(
            text=text,
            confidence=confidence,
            language_detected=language or None,
            emotion=emotion,
        )

    def partial_text(self, audio_pcm: bytes) -> str:
        """Decode one bounded utterance snapshot for endpointing only (ADR-0006 D7).

        The text is ephemeral L5 input to the semantic endpoint decision. It
        carries no confidence, is never normalized for L3, and is never
        persisted; :meth:`recognize` on the committed audio stays the only
        authoritative transcript.
        """
        audio = _pcm16_to_float32(audio_pcm)
        if audio.size == 0:
            return ""
        return _MISPLACED_PERIOD.sub(r"\1", self._decode(audio).text.strip())

    def prewarm(self) -> None:
        """Load SenseVoice and run one silent stream before capture is armed."""
        self._decode(np.zeros(_SAMPLE_RATE // 10, dtype=np.float32))
        record_realtime_trace(
            "asr_prewarm_completed",
            provider="sensevoice",
            silence_samples=_SAMPLE_RATE // 10,
            measurement_boundary="software_model_and_stream_ready",
        )

    def _decode(self, audio: np.ndarray) -> Any:  # noqa: ANN401 — sherpa_onnx result is dynamic
        with self._decode_lock:
            recognizer = self._load()
            stream = recognizer.create_stream()
            stream.accept_waveform(_SAMPLE_RATE, audio)
            recognizer.decode_stream(stream)
            return stream.result

    def _load(self) -> Any:  # noqa: ANN401 — sherpa_onnx typing is dynamic
        if self._recognizer is not None:
            return self._recognizer
        try:
            import sherpa_onnx  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — environment-dependent
            msg = "sherpa-onnx is required for SenseVoice ASR."
            raise RuntimeError(msg) from exc

        LOGGER.info("Loading SenseVoice model from %s", self._model_dir)
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(self._model_dir / "model.int8.onnx"),
            tokens=str(self._model_dir / "tokens.txt"),
            num_threads=self._num_threads,
            language=self._language,
            use_itn=True,
        )
        return self._recognizer


class MlxWhisperRecognizer:
    """mlx-whisper backend — Apple Silicon native, recommended for EN / mixed.

    Confidence is a Whisper-style log-prob mean — not comparable to SenseVoice's
    binary heuristic.
    """

    def __init__(
        self,
        *,
        repo: str = "mlx-community/whisper-large-v3-turbo",
        fp16: bool = True,
        temperature: float = 0.0,
        language: str | None = None,
        # large-v3-turbo skews toward traditional CN tokens; a simplified-CN
        # prompt biases the decoder back toward simplified glyphs.
        initial_prompt: str | None = "以下是普通话的简体中文转录。",
    ) -> None:
        """Capture mlx-whisper config; module + model load on first recognize()."""
        self._repo = str(repo)
        self._fp16 = bool(fp16)
        self._temperature = float(temperature)
        self._language = language
        self._initial_prompt = initial_prompt or None
        self._module: Any | None = None

    def recognize(self, audio_pcm: bytes) -> TranscriptionResult:
        """Transcribe PCM16 mono 16 kHz audio with mlx-whisper."""
        audio = _pcm16_to_float32(audio_pcm)
        if audio.size == 0:
            return TranscriptionResult(
                text="",
                confidence=0.0,
                language_detected=None,
                emotion=None,
            )

        mlx_whisper = self._load()
        transcription: Mapping[str, Any] = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self._repo,
            fp16=self._fp16,
            temperature=self._temperature,
            language=self._language,
            initial_prompt=self._initial_prompt,
            verbose=False,
        )
        text = str(transcription.get("text", "")).strip()
        language = str(transcription.get("language") or self._language or "") or None
        confidence = _estimate_whisper_confidence(transcription)

        LOGGER.info(
            "MLX Whisper: language=%s confidence=%.3f text=%r",
            language,
            confidence,
            text,
        )
        return TranscriptionResult(
            text=text,
            confidence=confidence,
            language_detected=language,
            emotion=None,
        )

    def _load(self) -> Any:  # noqa: ANN401 — mlx_whisper typing is dynamic
        if self._module is not None:
            return self._module
        try:
            import mlx_whisper  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — environment-dependent
            msg = (
                "mlx-whisper is required for MlxWhisperRecognizer. "
                "Install with: uv pip install mlx-whisper"
            )
            raise RuntimeError(msg) from exc
        LOGGER.info("Loaded mlx_whisper (repo=%s)", self._repo)
        self._module = mlx_whisper
        return self._module


class LocalWhisperRecognizer:
    """openai-whisper local backend — slow CPU fallback when no GPU / MLX."""

    def __init__(
        self,
        *,
        model_size: str = "base",
        language: str | None = None,
    ) -> None:
        """Capture model-size + language; whisper model loads on first call."""
        self._model_size = str(model_size)
        self._language = language
        self._model: Any | None = None

    def recognize(self, audio_pcm: bytes) -> TranscriptionResult:
        """Transcribe PCM16 mono 16 kHz audio with openai-whisper."""
        audio = _pcm16_to_float32(audio_pcm)
        if audio.size == 0:
            return TranscriptionResult(
                text="",
                confidence=0.0,
                language_detected=None,
                emotion=None,
            )

        model = self._load()
        transcription: Mapping[str, Any] = model.transcribe(
            audio,
            language=self._language,
            fp16=False,
            verbose=False,
        )
        text = str(transcription.get("text", "")).strip()
        language = str(transcription.get("language") or self._language or "") or None
        confidence = _estimate_whisper_confidence(transcription)

        LOGGER.info(
            "Whisper transcription: language=%s confidence=%.3f text=%r",
            language,
            confidence,
            text,
        )
        return TranscriptionResult(
            text=text,
            confidence=confidence,
            language_detected=language,
            emotion=None,
        )

    def _load(self) -> Any:  # noqa: ANN401 — whisper typing is dynamic
        if self._model is not None:
            return self._model
        try:
            import whisper  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — environment-dependent
            msg = "openai-whisper is required for LocalWhisperRecognizer."
            raise RuntimeError(msg) from exc
        LOGGER.info("Loading Whisper model: %s", self._model_size)
        self._model = whisper.load_model(self._model_size)
        return self._model


# ---------------------------------------------------------------------------
# Partial-transcript helpers for the semantic endpoint — ADR-0006 D7
# ---------------------------------------------------------------------------


_TERMINAL_PUNCTUATION = frozenset("。！？!?.…")

# A stable prefix ending in one of these is an open clause: the speaker has
# announced a continuation and the endpoint must keep holding. Everything
# else that is non-empty counts as complete. Local and deterministic by
# design; never an LLM call and never prompt text.
_DANGLING_SUFFIXES: tuple[str, ...] = (
    "，",
    ",",
    "、",
    "；",
    ";",
    "：",
    ":",
    "和",
    "跟",
    "与",
    "或者",
    "还是",
    "但是",
    "不过",
    "然后",
    "因为",
    "所以",
    "如果",
    "的话",
    "虽然",
    "而且",
    "就是",
    "那个",
    "这个",
    "那么",
    "还有",
    "以及",
    "帮我",
    "把",
    "被",
    "给",
    "让",
    "呃",
    "嗯",
    " and",
    " or",
    " but",
    " because",
    " if",
    " the",
    " a",
    " an",
    " to",
    " with",
    " of",
    " so",
    " then",
    " for",
    " um",
    " uh",
)


def normalize_partial_text(text: str) -> str:
    """Return the code-point form used to compare consecutive partial revisions."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split())


def looks_complete(text: str) -> bool:
    """Return whether a normalized stable prefix reads as a finished clause."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[-1] in _TERMINAL_PUNCTUATION:
        return True
    return not stripped.endswith(_DANGLING_SUFFIXES)


# ---------------------------------------------------------------------------
# Empty / too-short filter — ADR-0005 §8 fix #3
# ---------------------------------------------------------------------------


_MIN_TEXT_LEN = 2
# PCM16 mono. Anything ≤ this RMS is treated as silence. The legacy SenseVoice
# heuristic was rms_float < 0.01 (≈ 328 on the int16 scale) but applied AFTER
# the recognizer ran; here we want to admit any frame whose amplitude is
# meaningfully above zero so the unit tests (and short-utterance ducked
# captures) survive without false-positive silence rejection.
_MIN_RMS_THRESHOLD = 10.0


def _rms(audio_pcm: bytes) -> float:
    """Root-mean-square amplitude of PCM16 mono little-endian audio."""
    if not audio_pcm:
        return 0.0
    samples = np.frombuffer(audio_pcm, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def _is_punctuation_only(text: str) -> bool:
    """True when ``text`` contains no non-punctuation, non-whitespace chars."""
    punct = set(
        "。，！？、；：“”‘’【】《》（）.,!?;:\"'()[]<>~`@#$%^&*-_+=|\\/ ",
    )
    return all(c in punct or c.isspace() for c in text)


def is_empty_or_too_short(text: str, *, audio_pcm: bytes) -> bool:
    """Unified empty-utterance filter for wake + PTT (ADR-0005 §8 fix #3)."""
    stripped = text.strip()
    if len(stripped) < _MIN_TEXT_LEN:
        return True
    if _is_punctuation_only(stripped):
        return True
    return _rms(audio_pcm) < _MIN_RMS_THRESHOLD


__all__ = [
    "AsrNormalizer",
    "AsrRecognizer",
    "LocalWhisperRecognizer",
    "MlxWhisperRecognizer",
    "SenseVoiceRecognizer",
    "TranscriptionResult",
    "is_empty_or_too_short",
    "looks_complete",
    "normalize_partial_text",
]
