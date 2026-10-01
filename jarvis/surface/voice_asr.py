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
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
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
            "SenseVoice: lang=%s emotion=%s conf=%.1f chars=%d",  # never the words (ADR 0067)
            language,
            emotion,
            confidence,
            len(text),
        )
        return TranscriptionResult(
            text=text,
            confidence=confidence,
            language_detected=language or None,
            emotion=emotion,
        )

    def partial_text(self, audio_pcm: bytes) -> str:
        """Decode without the utterance gate: an endpointing snapshot or a dictation stretch.

        The text is ephemeral L5 input: the semantic endpoint decision
        (ADR-0006 D7), or a stretch of dictation that goes to the caret (ADR
        0076). It carries no confidence, is never normalized for L3, and is
        never persisted;
        :meth:`recognize` on the committed audio stays a voice turn's only
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


# 言文 (typeless-local asr.py at 9b11024), verbatim: what Whisper writes for
# silence or room noise, subtitle credits from its training data. Only a
# transcript that is nothing but one of these is dropped.
_WHISPER_SILENCE_RE = re.compile(
    r"^(优优独播剧场.*|字幕.{0,20}(提供|制作|by.*)|.*请不吝点赞.*|明镜与点点栏目|(谢谢|感谢)(大家)?(收看|观看)"
    r"|thanks? (you )?(so much )?for watching|subtitles by.*)$",
    re.IGNORECASE,
)
# A 2-16 character unit three times in a row, or one character eight times.
_WHISPER_LOOP_RE = re.compile(r"(.{2,16})\1{2,}")
_WHISPER_RUN_RE = re.compile(r"(\S)\1{7,}")
_WHISPER_FALLBACK_TEMPERATURES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
_WHISPER_TERMS_MAX_CHARS = 600
# mlx_whisper keeps one model per process, so every recognizer shares one lock.
_WHISPER_LOCK = threading.Lock()


def _looks_looped(text: str) -> bool:
    unpunctuated = re.sub(r"[\s\W_]+", "", text)
    return bool(_WHISPER_LOOP_RE.search(unpunctuated) or _WHISPER_RUN_RE.search(text))


def _terms_prompt(terms: Sequence[str]) -> str:
    """言文's ``Common terms: a, b.`` line, cut at a term within 600 characters."""
    prefix, suffix = "Common terms: ", "."
    budget = _WHISPER_TERMS_MAX_CHARS - len(prefix) - len(suffix)
    kept: list[str] = []
    used = 0
    for term in terms:
        addition = (", " if kept else "") + term
        if kept and used + len(addition) > budget:
            break
        kept.append(term)
        used += len(addition)
    return prefix + ", ".join(kept) + suffix if kept else ""


class MlxWhisperRecognizer:
    """mlx-whisper backend — Apple Silicon native, recommended for EN / mixed.

    Confidence is a Whisper-style log-prob mean — not comparable to SenseVoice's
    binary heuristic. Decoded as 言文 decodes it: no window conditions the
    next, a looped transcript is heard again without the word list and with
    temperature fallback, and a transcript that is only a subtitle credit is
    silence. ``terms`` returns the user's own words for the prompt, read on
    every call.
    """

    def __init__(  # noqa: PLR0913 - the decode knobs, each a keyword
        self,
        *,
        repo: str = "mlx-community/whisper-large-v3-turbo",
        fp16: bool = True,
        temperature: float = 0.0,
        language: str | None = None,
        # large-v3-turbo skews toward traditional CN tokens; a simplified-CN
        # prompt biases the decoder back toward simplified glyphs.
        initial_prompt: str | None = "以下是普通话的简体中文转录。",
        terms: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        """Capture mlx-whisper config; module + model load on first recognize()."""
        self._repo = str(repo)
        self._fp16 = bool(fp16)
        self._temperature = float(temperature)
        self._language = language
        self._initial_prompt = initial_prompt or None
        self._terms = terms
        self._module: Any | None = None
        self.last_used = 0.0

    def prewarm(self) -> None:
        """Load the model (~1.6 GB, a few seconds) by hearing half a second of silence."""
        self.recognize(bytes(_SAMPLE_RATE))

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

        listed = _terms_prompt(self._terms()) if self._terms is not None else ""
        prompt = " ".join(part for part in (self._initial_prompt, listed) if part) or None
        with _WHISPER_LOCK:
            transcription = self._decode(audio, prompt=prompt, temperature=self._temperature)
            text = self._heard(transcription, prompt, self._initial_prompt, listed)
            if _looks_looped(text):
                # 言文 e94d055: heard again without the list, hotter where it repeats.
                LOGGER.info("MLX Whisper looped; hearing it again with temperature fallback")
                transcription = self._decode(
                    audio, prompt=self._initial_prompt, temperature=_WHISPER_FALLBACK_TEMPERATURES,
                )
                text = self._heard(transcription, self._initial_prompt)
            self.last_used = time.monotonic()
        if _WHISPER_SILENCE_RE.match(re.sub(r"[\s\W_]+$|^[\s\W_]+", "", text)):
            text = ""
        language = str(transcription.get("language") or self._language or "") or None
        confidence = _estimate_whisper_confidence(transcription)

        LOGGER.info(
            "MLX Whisper: language=%s confidence=%.3f chars=%d",  # never the words (ADR 0067)
            language,
            confidence,
            len(text),
        )
        return TranscriptionResult(
            text=text,
            confidence=confidence,
            language_detected=language,
            emotion=None,
        )

    def _decode(
        self, audio: np.ndarray, *, prompt: str | None, temperature: float | tuple[float, ...],
    ) -> Mapping[str, Any]:
        result: Mapping[str, Any] = self._load().transcribe(
            audio,
            path_or_hf_repo=self._repo,
            fp16=self._fp16,
            temperature=temperature,
            language=self._language,
            initial_prompt=prompt,
            condition_on_previous_text=False,
            verbose=None,
        )
        return result

    @staticmethod
    def _heard(transcription: Mapping[str, Any], *prompts: str | None) -> str:
        """The text, less a leading copy of a prompt Whisper writes on near-silence."""
        text = str(transcription.get("text", "")).strip()
        for prompt in prompts:
            if prompt and text.startswith(prompt):
                return text[len(prompt):].strip()
        return re.sub(r"^\s*Common terms:[^\n]*\n", "", text, count=1, flags=re.IGNORECASE)

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


# 言文's gate before the model (assets/config.yaml, 5231742): at least 0.15 s,
# and some 0.2 s of it at 0.003 RMS or more, so only a dead or muted mic is
# cut and quiet speech is heard.
_WHISPER_MIN_BYTES = int(0.15 * _SAMPLE_RATE) * 2
_WHISPER_LEVEL_FLOOR = 0.003
# 言文 c257562: after 20 s idle a pass took 1.0-1.2 s against 0.37 s warm.
_WHISPER_WARM_IDLE_S = 20.0


class WhisperFinalRecognizer:
    """A voice turn's words from local Whisper as 言文 hears them; SenseVoice keeps the partials.

    ``realtime.final_asr: whisper`` (docs/plans/whisper-final-asr-proposal.md).
    The rules after the recognizer were tuned on SenseVoice, whose text
    normalization closes every sentence; Whisper often leaves a short answer
    bare, and a bare 「好」 is one character, which the empty filter drops. So a
    transcript with no closing punctuation gets a stop.
    """

    def __init__(self, *, whisper: MlxWhisperRecognizer, partials: SenseVoiceRecognizer) -> None:
        """Hear finals with ``whisper`` and endpoint snapshots with ``partials``."""
        self._whisper = whisper
        self._partials = partials
        self._warming = threading.Lock()

    def recognize(self, audio_pcm: bytes) -> TranscriptionResult:
        """The authoritative transcript; nothing for a clip too short or a dead mic."""
        if len(audio_pcm) < _WHISPER_MIN_BYTES or too_quiet_for_speech(
            audio_pcm, floor=_WHISPER_LEVEL_FLOOR,
        ):
            return TranscriptionResult(
                text="", confidence=0.0, language_detected=None, emotion=None,
            )
        result = self._whisper.recognize(audio_pcm)
        text = result.text.strip()
        if text and not unicodedata.category(text[-1]).startswith("P"):
            text += "." if text[-1].isascii() else "。"
        return replace(result, text=text)

    def partial_text(self, audio_pcm: bytes) -> str:
        """SenseVoice's snapshot decode for the semantic endpoint (ADR-0006 D7)."""
        return self._partials.partial_text(audio_pcm)

    def prewarm(self) -> None:
        """Load both models before the mic opens."""
        self._partials.prewarm()
        self._whisper.prewarm()

    def warm(self) -> None:
        """Allen started talking: after 20 s idle, run one silent pass in the background."""
        if time.monotonic() - self._whisper.last_used < _WHISPER_WARM_IDLE_S:
            return
        if not self._warming.acquire(blocking=False):
            return

        def _run() -> None:
            try:
                self._whisper.prewarm()
            except Exception:  # noqa: BLE001 - a failed warm-up only leaves the next pass cold
                LOGGER.warning("MLX Whisper warm-up failed", exc_info=True)
            finally:
                self._warming.release()

        threading.Thread(target=_run, name="jarvis-whisper-warm", daemon=True).start()


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
            "Whisper transcription: language=%s confidence=%.3f chars=%d",  # never the words
            language,
            confidence,
            len(text),
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
    "问一下",
    "请问",
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
    # SenseVoice appends a period to every snapshot ("...的话。"), so the
    # dangling-clause check runs on the text with terminal punctuation removed.
    stripped = text.rstrip().rstrip("".join(_TERMINAL_PUNCTUATION)).rstrip()
    if not stripped:
        return False
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


def too_quiet_for_speech(audio_pcm: bytes, *, floor: float = _SENSEVOICE_FLOAT_RMS_FLOOR) -> bool:
    """True when no 0.2 s of PCM16 mono audio reaches ``floor`` (SenseVoice's speech floor).

    The loudest window decides, not the mean: a dictation stretch that holds a
    long pause averages his words under the floor (ADR 0076).
    """
    samples = np.frombuffer(audio_pcm, dtype=np.int16).astype(np.float64) / 32768.0
    window = _SAMPLE_RATE // 5
    if samples.size <= window:
        level = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
        return level < floor
    energy = np.concatenate(([0.0], np.cumsum(samples**2)))
    hop = window // 4
    loudest = float(np.max(energy[window::hop] - energy[:-window:hop])) / window
    return float(np.sqrt(loudest)) < floor


def _is_punctuation_only(text: str) -> bool:
    """True when ``text`` contains no non-punctuation, non-whitespace chars."""
    punct = set(
        "。，！？、；：“”‘’【】《》（）.,!?;:\"'()[]<>~`@#$%^&*-_+=|\\/ ",
    )
    return all(c in punct or c.isspace() for c in text)


# A wake-channel transcript that is nothing but the wake phrase: Allen said
# "Hey Jarvis", paused, and the acoustic endpoint closed before his question.
# Seen live as "Hey, Ja he.", "Hey, Javis hey.", "Hey, Ja, hey, Jara.".
_WAKE_ONLY_RE = re.compile(r"(?:hey|hay|hi|he|ja[rv]?\w{0,4}|嘿|嗨|贾维斯|[\W_])*", re.IGNORECASE)


def is_wake_only(text: str) -> bool:
    """True when ``text`` holds only wake-phrase fragments and punctuation."""
    return _WAKE_ONLY_RE.fullmatch(text.strip()) is not None


# The wake phrase said in one breath with the request: heard as
# 「嘿ja班javis斯,我要睡觉了，能让他继续跑吗？」 on 2026-09-29, where Tier 0's
# anchored patterns then missed the night run. A lead that opens like the wake
# phrase and ends at the first pause mark is cut; one that runs on into words
# (「Javascript怎么学」) is not.
_WAKE_LEAD_RE = re.compile(
    r"\s*(?:(?:hey|hay|hi|嘿|嗨)[\s,，、]*(?:ja|贾)[^,，、。.!！?？]{0,8}"
    r"|hey|hay|嘿|嗨|(?:ja|贾)[^,，、。.!！?？\s]{0,8})[,，、。.!！?？]+\s*",
    re.IGNORECASE,
)


def strip_wake_lead(text: str) -> str:
    """``text`` without the wake phrase it opens with, if it does."""
    for _ in range(3):  # 「Hey，Jarvis，…」 comes back in pieces
        lead = _WAKE_LEAD_RE.match(text)
        if lead is None:
            break
        text = text[lead.end():]
    return text


# What Allen says over Jarvis that must not take the turn from her (soft
# barge-in). Matched whole, case and punctuation aside. A listening sound
# keeps her talking; a lone 对/是/好 or "yes" may be answering a waiting card
# (ADR 0062), so only their doubled forms count, and a question mark makes
# any of them a request to repeat (「啊？」). A 「嗯」 came back as "And." in
# the 2026-09-28 live test, and a drawn-out one as three pieces of Japanese
# 「うん」「う」 in the 2026-09-29 one; final ASR guesses Korean on short sounds
# too.
_BACKCHANNEL_RE = re.compile(
    r"(?:[嗯哼哦噢喔唔呃额啊哈呵]|对对+|是是+|好好+|行行+)+"
    r"|[あうえおんぁぅぇぉっはふへほアウエオンァゥェォッハフヘホー]+|[응음으흠어아]+"
    r"|(?:mm+|m+h+m+|uhhuh|hm+|uh+|um+|oh+|ah+|ha|and)+",
)
# A request to stop talking: she stops, and it is not a question to answer.
# Over her voice a lone 「停」 comes back as any ting/ding syllable, sometimes
# with a stray tail: 「停立」 and 「顶」 in the 2026-09-28 live test. 「OK可以了」
# and 「可以啦」 (enough) in the 2026-09-29 ones; a lone 「可以」 still answers
# a card. A lone English word is one too (is_stop_request).
_STOP_REQUEST_RE = re.compile(
    r"(?:ok|okay|嗯|哎|唉|好|行|那|你|好[了啦]|行[了啦])?"
    r"(?:停+(?:一下|下)?|先停(?:一下)?|暂停(?:一下)?|等(?:一下|等|下)?|别说[了啦]|不要说[了啦]"
    r"|不用说[了啦]|别念[了啦]|闭嘴|安静(?:一下|一点|点)?|够[了啦]|可以[了啦]|好[了啦]好[了啦]"
    r"|行[了啦]行[了啦])(?:吧|啊|呀|哈|啦)?"
    r"|[停亭婷庭廷挺艇听厅顶鼎定丁叮钉][立啲一]?"
    r"|(?:ok|okay|please|jarvis|hey)*"
    r"(?:stop(?:it|talking|that)?|wait|pause|enough|bequiet|quiet|shutup|hush)"
    r"(?:please|jarvis|now)*",
)
# What final ASR makes of a hum or a cough over her is often one syllable:
# 「五」 in the 2026-09-28 live test, answered as a question. A lone word
# that may answer a waiting card (ADR 0062; every one-word answer in
# config/confirm_grammar.yaml) stays a turn.
_SHORT_ANSWER_RE = re.compile(
    r"[对是好行要不发否别]|yes|yeah|yep|no|nope|ok|okay|sure|right|confirm|send|cancel|don'?t",
)


# Allen sending Jarvis out of conversation mode (ADR 0102): she leaves it,
# and it is no question to answer. Matched whole, wake phrase, case and
# punctuation aside.
_DISMISS_RE = re.compile(
    r"(?:hey|hi|嘿|嗨)?(?:jarvis|贾维斯)?(?:ok|okay|好|行|嗯)?(?:你|那)?"
    r"(?:(?:可以)?退下|没事[了啦]?|就这样|先这样|拜拜|再见|结束(?:对话|会话)?|去休息"
    r"|bye(?:bye)?|goodbye|thatsall|dismissed)(?:了|吧|啦|啊|哈)*",
)


# Allen asking Jarvis to keep listening for him (ADR 0102): conversation mode
# waits conversation_wait_s, and it is no question to answer. A lone 「等一下」
# stays a stop request; 「等我一下」 does not stop her for good, only waits.
_WAIT_RE = re.compile(
    r"(?:hey|hi|嘿|嗨)?(?:jarvis|贾维斯)?(?:ok|okay|好|行|嗯)?(?:你)?"
    r"(?:等(?:我|等我)(?:一下|一会儿?|下|会儿)?|稍等(?:我)?(?:一下)?|等着"
    r"|holdon|waitforme|givemea(?:sec(?:ond)?|minute|moment))(?:啊|呀|哈|吧|please)*",
)


def _squashed(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def is_dismissal(text: str) -> bool:
    """True when ``text`` only sends Jarvis out of conversation mode (退下, 没事了, bye)."""
    return _DISMISS_RE.fullmatch(_squashed(text)) is not None


def is_wait_request(text: str) -> bool:
    """True when ``text`` only asks Jarvis to wait for Allen (等我一下, hold on)."""
    return _WAIT_RE.fullmatch(_squashed(text)) is not None


def is_backchannel(text: str) -> bool:
    """True when ``text`` is only a listening sound (嗯, 对对, mm-hmm), not a question."""
    stripped = text.strip()
    if stripped.endswith(("?", "？")):
        return False
    return _BACKCHANNEL_RE.fullmatch(_squashed(stripped)) is not None


def is_stop_request(text: str) -> bool:
    """True when ``text`` only asks Jarvis to stop talking (停, 别说了, stop).

    A lone English word counts unless it is a listening sound, a card's
    answer or a question: in the 2026-09-29 live tests final ASR heard every
    「pause」 over her voice as some other lone English word of two to six
    characters ("Pulse" as Allen tells it; a synthesized one reads "Cause.").
    """
    squashed = _squashed(text)
    if _STOP_REQUEST_RE.fullmatch(squashed) is not None:
        return True
    return (
        is_unclear_sound(text)
        and re.fullmatch(r"[a-z]+", squashed) is not None
        and _BACKCHANNEL_RE.fullmatch(squashed) is None
    )


def is_unclear_sound(text: str) -> bool:
    """True for one syllable or word that answers nothing (五), not a question."""
    stripped = text.strip()
    if stripped.endswith(("?", "？")):
        return False
    words = re.findall(r"[a-z']+|\w", stripped.lower())
    return len(words) == 1 and _SHORT_ANSWER_RE.fullmatch(words[0]) is None


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
    "WhisperFinalRecognizer",
    "is_backchannel",
    "is_dismissal",
    "is_empty_or_too_short",
    "is_stop_request",
    "is_unclear_sound",
    "is_wait_request",
    "is_wake_only",
    "looks_complete",
    "normalize_partial_text",
    "too_quiet_for_speech",
]
