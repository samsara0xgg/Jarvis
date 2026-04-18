#!/usr/bin/env python3
"""Voice pipeline benchmark — WP1-7 quantitative measurements.

Single entry point for measuring every voice-pipeline WP (1-7) plus the
2026-04-17 interrupt-ASR migration. Designed so one run gives enough
debug info to pin a regression without re-running narrower benches.

Modes
-----
default         offline: pure-python benches, no mic/speaker, no net
--live          adds mic/speaker benches (interrupt, soft-stop, …)
--real-api      allows real LLM/TTS API calls (for E2E only)
--only NAME     run only a single bench (e.g. ``--only wp6_vad``)
--refresh-baseline  overwrite the regression baseline

Output
------
``scripts/bench_results/voice_pipeline_<ts>/``

    results.jsonl       one measurement per line (machine-parseable)
    summary.md          human-readable table + regressions
    config.snapshot.yaml  config.yaml at run time (reproducibility)
    frames/             per-bench raw dumps (WP6 VAD traces etc.)

Exit codes
----------
0  all measurements pass, no regression vs baseline
1  ≥1 regression or correctness failure
2  bench itself errored (unhandled exception)

Usage
-----
    python scripts/bench_voice_pipeline.py                   # offline all
    python scripts/bench_voice_pipeline.py --live            # + live
    python scripts/bench_voice_pipeline.py --only wp6_vad    # one bench
    python scripts/bench_voice_pipeline.py --refresh-baseline  # rebase

The file is long by design — keeping each bench inline (rather than
splitting to sub-modules) lets you read the whole surface in one place.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import logging
import os
import signal
import statistics
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOGGER = logging.getLogger("bench_voice_pipeline")

RESULTS_DIR = ROOT / "scripts" / "bench_results"
BASELINE_FILE = RESULTS_DIR / "voice_pipeline_baseline.json"
MIC_TEES_DIR = RESULTS_DIR / "bench_mic_tees"


# =====================================================================
# Common infrastructure
# =====================================================================


@dataclasses.dataclass
class Measurement:
    """One bench measurement line.

    ``status`` uses three values:
      - ``pass``  boolean assertion held
      - ``fail``  boolean assertion violated (counted for exit code 1)
      - ``info``  informational; never fails the run
    """

    bench: str
    name: str
    status: str
    value: Any = None
    unit: str = ""
    details: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class Results:
    """Collect measurements across all benches; append-only JSONL on disk."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.measurements: list[Measurement] = []
        self._jsonl_path = run_dir / "results.jsonl"
        self._jsonl_path.touch()

    def add(self, m: Measurement) -> None:
        self.measurements.append(m)
        with self._jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(m.to_dict(), ensure_ascii=False) + "\n")

    def count_fails(self) -> int:
        return sum(1 for m in self.measurements if m.status == "fail")

    def by_bench(self) -> dict[str, list[Measurement]]:
        out: dict[str, list[Measurement]] = {}
        for m in self.measurements:
            out.setdefault(m.bench, []).append(m)
        return out


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Interpolated percentile of a sorted list; p in [0, 100]."""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _load_config() -> dict:
    with (ROOT / "config.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _dump_config_snapshot(config: dict, run_dir: Path) -> None:
    """Copy current config.yaml into the run dir for reproducibility."""
    with (run_dir / "config.snapshot.yaml").open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)


def _make_run_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RESULTS_DIR / f"voice_pipeline_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "frames").mkdir(exist_ok=True)
    return run_dir


# =====================================================================
# WP2 — ASR normalizer (offline, pure Python)
# =====================================================================
# Tests three-layer cascade correctness + perf budget.
# Layer 1 (correction + require_context) / Layer 2 (alias) / Layer 3
# (Levenshtein, default off). config.yaml ships with 2 Layer-1 entries
# and 2 Layer-2 entries as examples; the bench uses those plus additions.


def bench_wp2_normalizer(config: dict, results: Results) -> None:
    """WP2: three-layer ASR normalizer correctness + <10ms perf budget."""
    from core.asr_normalizer import ASRNormalizer

    n = ASRNormalizer(config)
    cases: list[tuple[str, str, str]] = [
        # (case_name, input, expected)
        ("L1 correction + ctx fires",   "打开客厅大蛋",     "打开客厅大灯"),
        ("L1 no ctx — no fire",          "客厅大蛋今天",     "客厅大蛋今天"),
        ("L1 放送 + 模式 fires",          "切换到放送模式",   "切换到放松模式"),
        ("L2 alias canonical",           "打开客厅主灯",     "打开客厅大灯"),
        ("L2 alias 休闲",                "切换到休闲模式",   "切换到放松模式"),
        ("pass-through no match",        "今天天气怎么样",   "今天天气怎么样"),
        ("empty string",                 "",                 ""),
    ]
    for case_name, input_text, expected in cases:
        got = n.normalize(input_text)
        passed = got == expected
        results.add(Measurement(
            bench="wp2_normalizer", name=f"correctness::{case_name}",
            status="pass" if passed else "fail", value=got,
            details={"input": input_text, "expected": expected, "got": got},
        ))

    # Perf: <10ms budget per call, median + p95 + p99 over 1200 calls
    sample_inputs = [c[1] for c in cases if c[1]] * 200  # skip empty
    times_ms: list[float] = []
    for txt in sample_inputs:
        t0 = time.perf_counter()
        n.normalize(txt)
        times_ms.append((time.perf_counter() - t0) * 1000)
    times_ms.sort()
    median = statistics.median(times_ms)
    p95 = _percentile(times_ms, 95)
    p99 = _percentile(times_ms, 99)
    results.add(Measurement(
        bench="wp2_normalizer", name="perf::median_ms",
        status="pass" if median < 10 else "fail",
        value=round(median, 4), unit="ms",
        details={"n": len(times_ms), "p95_ms": round(p95, 4),
                 "p99_ms": round(p99, 4), "max_ms": round(max(times_ms), 4),
                 "budget_ms": 10.0},
    ))

    # Layer 3 fuzzy toggle: verify opt-in. With fuzzy enabled + max_distance=1,
    # a near-miss like "打开客厅大灯灯" (extra char) should still be left alone
    # or corrected — the bench just logs what happens.
    fuzzy_cfg = copy.deepcopy(config)
    fuzzy_cfg["asr_normalizer_fuzzy"] = {"enabled": True, "max_distance": 1}
    n_fuzzy = ASRNormalizer(fuzzy_cfg)
    test_in = "打开客厅大蛋"  # L1 still fires
    fuzzy_out = n_fuzzy.normalize(test_in)
    results.add(Measurement(
        bench="wp2_normalizer", name="layer3_enabled_fires_L1_first",
        status="pass" if fuzzy_out == "打开客厅大灯" else "fail",
        value=fuzzy_out,
        details={"input": test_in, "output": fuzzy_out,
                 "note": "L3 is last-resort; L1 with ctx still wins"},
    ))


# =====================================================================
# WP3 — TTS preprocessor + MiniMax vol (offline)
# =====================================================================
# Per-filter correctness; NFKC; currency preservation; vol int+clamp.
# Note: some of these cases document *actual* behavior that is subtle
# (e.g. angle bracket filter strips the tag chars only, not XML nesting
# — so "<think>内部</think>你好" → "内部你好", not "你好").


def bench_wp3_preprocessor(config: dict, results: Results) -> None:
    """WP3: preprocessor correctness, NFKC, MiniMax vol int/clamp."""
    from core import tts_preprocessor
    from core.tts import TTSEngine

    cases: list[tuple[str, str, str]] = [
        ("emoji removed",              "今天天气真好😊",            "今天天气真好"),
        ("markdown bold stripped",     "这很**重要**",              "这很"),
        # Underscore-italic NOT stripped (filter_asterisks only handles *).
        # Documented gap vs OLV plan — add to info, not fail.
        ("markdown italic passthrough","_emphasis_",                "_emphasis_"),
        ("brackets [key]",             "[开心]你好",                 "你好"),
        ("brackets 【书】",             "【书名】你好",              "你好"),
        ("parens ASCII",               "hello (aside) world",       "hello world"),
        ("parens full-width",          "开灯（功率100W）",            "开灯"),
        ("angle tag chars only",       "<br/>你好",                  "你好"),
        ("angle nested content kept",  "<think>内部</think>你好",    "内部你好"),
        ("currency yuan preserved",    "价格¥100",                   "价格¥100"),
        ("currency usd preserved",     "Cost $5",                    "Cost $5"),
        ("NFKC fullwidth digits",      "１００块",                   "100块"),
        ("math symbol = dropped",      "x=3",                        "x3"),
        ("whitespace collapse",        "hello  \n  world",           "hello world"),
        ("empty",                      "",                           ""),
    ]
    for case_name, input_text, expected in cases:
        got = tts_preprocessor.clean(input_text)
        passed = got == expected
        results.add(Measurement(
            bench="wp3_preprocessor", name=f"default::{case_name}",
            status="pass" if passed else "fail", value=got,
            details={"input": input_text, "expected": expected, "got": got},
        ))

    # Toggle: disable asterisks → markdown survives
    got = tts_preprocessor.clean("这很**重要**", {"ignore_asterisks": False})
    passed = "**重要**" in got
    results.add(Measurement(
        bench="wp3_preprocessor", name="toggle::asterisks_off",
        status="pass" if passed else "fail", value=got,
        details={"input": "这很**重要**", "got": got},
    ))

    # Perf (500 iters, should be microseconds)
    sample = "[开心]今天天气**真好**😊 价格¥100 <br/>功率100W"
    times_ms: list[float] = []
    for _ in range(500):
        t0 = time.perf_counter()
        tts_preprocessor.clean(sample)
        times_ms.append((time.perf_counter() - t0) * 1000)
    times_ms.sort()
    results.add(Measurement(
        bench="wp3_preprocessor", name="perf::median_ms",
        status="pass" if statistics.median(times_ms) < 5 else "fail",
        value=round(statistics.median(times_ms), 4), unit="ms",
        details={"n": len(times_ms),
                 "p95_ms": round(_percentile(times_ms, 95), 4),
                 "max_ms": round(max(times_ms), 4), "budget_ms": 5.0},
    ))

    # MiniMax vol: int round + clamp to [1, 10]
    for raw, expected in [
        (1, 1), (1.0, 1), (5.6, 6), (0, 1), (100, 10), (-5, 1),
        ("3", 3), ("garbage", 1), (None, 1),
    ]:
        cfg2 = copy.deepcopy(config)
        cfg2["tts"]["minimax_volume"] = raw
        try:
            engine = TTSEngine(cfg2)
            got_vol = engine.minimax_volume
            passed = got_vol == expected and isinstance(got_vol, int)
        except Exception as exc:
            got_vol = f"EXCEPTION: {exc}"
            passed = False
        results.add(Measurement(
            bench="wp3_preprocessor", name=f"minimax_vol::raw={raw!r}",
            status="pass" if passed else "fail", value=got_vol,
            details={"input": raw, "expected": expected, "got": got_vol,
                     "is_int": isinstance(got_vol, int)},
        ))


# =====================================================================
# WP4 — LLM sentence divider (offline)
# =====================================================================
# Feed a token stream char-by-char through ``LLMClient._try_flush`` and
# record: (a) char count before first on_sentence fires with/without
# faster_first_response, (b) abbreviation guard holds for all 14 terms,
# (c) decimal guard holds.
#
# Construction note: ``LLMClient(config)`` sets self._api_key but makes
# no network calls — safe offline.


def _feed_stream(client: Any, text: str) -> tuple[list[str], list[int]]:
    """Feed ``text`` char-by-char through ``client._try_flush``.

    Returns (sentences_fired, char_indices_at_fire). The char index is
    the 1-based position in ``text`` at which the on_sentence fire
    happened — lower = lower first-token latency.
    """
    client._is_first_sentence = True  # noqa: SLF001 reset for each stream
    fires: list[str] = []
    fire_positions: list[int] = []
    buffer = ""
    # Track chars fed into the stream independently — can't rely on
    # ``len(buffer)`` in the closure since the inner _flush_sentences
    # mutates a local ``buffer`` variable, not the outer one.
    chars_fed = [0]

    def on_sentence(s: str) -> None:
        fires.append(s)
        fire_positions.append(chars_fed[0])

    for i, ch in enumerate(text, start=1):
        chars_fed[0] = i
        buffer += ch
        buffer = client._flush_sentences(buffer, on_sentence, force=False)  # noqa: SLF001
    # Final flush for residual
    chars_fed[0] = len(text)
    client._flush_sentences(buffer, on_sentence, force=True)  # noqa: SLF001
    return fires, fire_positions


def bench_wp4_sentence_divider(config: dict, results: Results) -> None:
    """WP4: faster_first_response delta + 14 abbreviations + decimal guard."""
    from core.llm import LLMClient

    # faster_first_response ON vs OFF — delta in char count to first fire
    cfg_on = copy.deepcopy(config)
    cfg_on["llm"]["sentence_divider"] = {
        "faster_first_response": True, "abbreviation_protect": True,
    }
    cfg_off = copy.deepcopy(config)
    cfg_off["llm"]["sentence_divider"] = {
        "faster_first_response": False, "abbreviation_protect": True,
    }
    client_on = LLMClient(cfg_on)
    client_off = LLMClient(cfg_off)

    text = "好的，我来帮你打开客厅灯。稍等一下。"
    fires_on, pos_on = _feed_stream(client_on, text)
    fires_off, pos_off = _feed_stream(client_off, text)

    first_pos_on = pos_on[0] if pos_on else -1
    first_pos_off = pos_off[0] if pos_off else -1
    # Expect ON to fire earlier (at the '，') than OFF (at '。')
    results.add(Measurement(
        bench="wp4_sentence_divider",
        name="faster_first_response::delta_chars_to_first_fire",
        status="pass" if first_pos_on < first_pos_off else "fail",
        value=first_pos_off - first_pos_on, unit="chars",
        details={"text": text, "fires_on": fires_on, "fires_off": fires_off,
                 "first_pos_on": first_pos_on, "first_pos_off": first_pos_off},
    ))

    # First fire ON should be "好的，" (breaks on comma)
    results.add(Measurement(
        bench="wp4_sentence_divider", name="faster_first_response::on_breaks_on_comma",
        status="pass" if fires_on and "好的" in fires_on[0] and "，" in fires_on[0]
                else "fail",
        value=fires_on[0] if fires_on else None,
        details={"fires": fires_on},
    ))

    # Second+ sentence should NOT break on commas (faster_first only fires once)
    text2 = "我来，慢慢想，然后告诉你。"
    fires_on2, _ = _feed_stream(client_on, text2)
    # Expected: first fire breaks on '，' (faster_first), subsequent don't.
    # So fires_on2 should be approximately ["我来，", "慢慢想，然后告诉你。"]
    # (no break on the second comma). That's 2 sentences.
    results.add(Measurement(
        bench="wp4_sentence_divider", name="faster_first_response::only_first_sentence",
        status="pass" if len(fires_on2) == 2 else "fail",
        value=fires_on2,
        details={"text": text2, "n_fires": len(fires_on2),
                 "expected_n": 2, "note": "faster_first should only bite first sentence"},
    ))

    # Abbreviation guard: all 14 must survive char-by-char streaming
    abbreviations = [
        "Mrs.", "Prof.", "e.g.", "i.e.",
        "Mr.", "Ms.", "Dr.", "Jr.", "Sr.", "St.", "Rd.",
        "Inc.", "Ltd.", "vs.",
    ]
    for abbr in abbreviations:
        # Put the abbreviation mid-sentence; only the FINAL "." should split
        text_abbr = f"Hello {abbr} Smith is here."
        client = LLMClient(cfg_off)  # OFF so first-response doesn't bite commas
        fires, _ = _feed_stream(client, text_abbr)
        # Expect exactly 1 fire = full sentence
        passed = len(fires) == 1 and fires[0].rstrip() == text_abbr.rstrip()
        details: dict[str, Any] = {
            "text": text_abbr, "n_fires": len(fires), "fires": fires,
        }
        # Known bug: multi-dot abbreviations (e.g., i.e.) split in char-by-char
        # streaming. Once the trailing "g" (or "e") arrives after "e." (or "i."),
        # the "." is no longer at end-of-buffer, so the streaming-edge guard in
        # _possible_abbreviation_prefix doesn't fire; the full "e.g." isn't yet
        # in buffer so _protected_dot_positions is empty. Between those two
        # states, the internal dot is treated as a split point.
        # Single-dot abbreviations (Dr., Mr., ...) are unaffected.
        if "." in abbr[:-1]:  # has internal dot
            details["known_bug"] = (
                "multi-dot abbreviation not protected in streaming; see "
                "_possible_abbreviation_prefix — guard only catches dots "
                "currently at buffer end"
            )
        results.add(Measurement(
            bench="wp4_sentence_divider", name=f"abbrev::{abbr}",
            status="pass" if passed else "fail", value=fires,
            details=details,
        ))

    # Decimal guard: "pi is 3.14." should split ONCE, at the trailing '.'
    client = LLMClient(cfg_off)
    fires, _ = _feed_stream(client, "pi is 3.14.")
    results.add(Measurement(
        bench="wp4_sentence_divider", name="decimal::3.14_no_split",
        status="pass" if len(fires) == 1 else "fail", value=fires,
        details={"n_fires": len(fires), "fires": fires},
    ))

    # Word-boundary guard: "Welcome." at buffer-end should NOT be held as
    # "e." abbreviation prefix (audit P1 / T1.3 fix).
    client = LLMClient(cfg_off)
    fires, _ = _feed_stream(client, "Welcome.")
    results.add(Measurement(
        bench="wp4_sentence_divider", name="decimal::word_boundary_Welcome",
        status="pass" if len(fires) == 1 and "Welcome" in fires[0] else "fail",
        value=fires,
        details={"n_fires": len(fires), "fires": fires,
                 "note": "T1.3 fix: 'e.' matching 'Welcome.' tail should not hold"},
    ))

    # _is_first_sentence reset: re-running should fire on comma again
    fires_second_run, _ = _feed_stream(client_on, text)
    results.add(Measurement(
        bench="wp4_sentence_divider", name="first_sentence::resets_per_stream",
        status="pass" if fires_second_run and "，" in fires_second_run[0] else "fail",
        value=fires_second_run[0] if fires_second_run else None,
        details={"fires": fires_second_run},
    ))


# =====================================================================
# WP5 — Interrupt memory injection truncation (offline)
# =====================================================================
# Tests the pure-list logic of ``_truncate_assistant_for_interrupt`` via
# unbound method call with a stub self (avoids heavy Jarvis init).


def _call_truncate(messages: list[dict], played: list[str]) -> list[dict]:
    """Invoke Jarvis._truncate_assistant_for_interrupt with a stub self.

    The method only touches ``self.logger`` for an info log; a
    SimpleNamespace-style stub is enough.
    """
    from types import SimpleNamespace
    from jarvis import JarvisApp

    stub = SimpleNamespace(logger=logging.getLogger("wp5-bench"))
    # Python 3: Class.method returns the function; pass stub as self.
    return JarvisApp._truncate_assistant_for_interrupt(  # noqa: SLF001
        stub, messages, played,
    )


def bench_wp5_memory_injection(config: dict, results: Results) -> None:
    """WP5: _truncate_assistant_for_interrupt correctness on OpenAI + Anthropic shapes."""
    del config  # unused — logic is pure

    # Case 1: OpenAI shape (content: str), single played sentence
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "今天天气？"},
        {"role": "assistant", "content": "北京多云。气温15度。建议穿长袖。"},
    ]
    played = ["北京多云。"]
    out = _call_truncate(msgs, played)
    # Expect: assistant content = "北京多云.…", followed by "[Interrupted by user]"
    ok = (
        len(out) == 4
        and out[2]["role"] == "assistant"
        and out[2]["content"] == "北京多云。..."
        and out[3] == {"role": "user", "content": "[Interrupted by user]"}
    )
    results.add(Measurement(
        bench="wp5_memory_injection", name="openai::single_played",
        status="pass" if ok else "fail", value=out,
        details={"played": played, "msgs_in": msgs, "msgs_out": out},
    ))

    # Case 2: Anthropic shape (content: list[block])
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "今天天气？"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "北京多云。气温15度。建议穿长袖。"},
        ]},
    ]
    played = ["北京多云。", "气温15度。"]
    out = _call_truncate(msgs, played)
    # assistant content should be a list with the joined played content + ellipsis
    asst = out[1]["content"]
    ok = (
        len(out) == 3
        and isinstance(asst, list)
        and len(asst) == 1
        and asst[0].get("type") == "text"
        and asst[0].get("text") == "北京多云。气温15度。..."
        and out[-1] == {"role": "user", "content": "[Interrupted by user]"}
    )
    results.add(Measurement(
        bench="wp5_memory_injection", name="anthropic::list_block",
        status="pass" if ok else "fail", value=out,
        details={"played": played, "msgs_out": out},
    ))

    # Case 3: empty played (edge case — interrupt before first sentence done)
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "这是第一句。这是第二句。"},
    ]
    out = _call_truncate(msgs, [])
    ok_marker = len(out) >= 1 and out[-1].get("content") == "[Interrupted by user]"
    results.add(Measurement(
        bench="wp5_memory_injection", name="edge::empty_played",
        status="pass" if ok_marker else "fail", value=out,
        details={"msgs_out": out, "note": "played=[] should still append marker"},
    ))

    # Case 4: no assistant message (edge case — interrupted during user turn)
    msgs = [{"role": "user", "content": "hi"}]
    out = _call_truncate(msgs, ["anything"])
    results.add(Measurement(
        bench="wp5_memory_injection", name="edge::no_assistant_message",
        status="info",
        value=out,
        details={"msgs_out": out, "note": "no assistant to truncate; behavior documented"},
    ))

    # Case 5: TTSPipeline.played_texts accumulation discipline (pure, no engine)
    # Skipped if we can't import without side effects — kept minimal check that
    # the contract (abort → played_texts is stable snapshot) is documented.
    from core.tts import TTSPipeline
    # Construct without engine to check attribute shape; start() is not called
    # to avoid threads. We just check the attribute exists.
    try:
        # TTSPipeline needs engine; we check the class has the attr contract
        has_attr = hasattr(TTSPipeline, "played_texts") and hasattr(TTSPipeline, "abort")
        results.add(Measurement(
            bench="wp5_memory_injection", name="tts_pipeline::api_surface",
            status="pass" if has_attr else "fail", value=has_attr,
            details={"note": "TTSPipeline must expose played_texts + abort()"},
        ))
    except Exception as exc:
        results.add(Measurement(
            bench="wp5_memory_injection", name="tts_pipeline::api_surface",
            status="fail", value=str(exc),
        ))


# =====================================================================
# WP6 — VAD replay on recorded WAVs (offline)
# =====================================================================
# Feed every bench_mic_tees/*.wav through SileroVADDirect at 512-sample
# chunks. Log frame-level prob/dB/state transitions for debug. Per-WAV
# summary lands in results.jsonl; full frame dumps go to frames/*.jsonl.
#
# This is the single best debug artifact: if VAD misbehaves on a
# specific recording, you have every frame's decision in a file.


def _load_wav_mono_16k(path: Path) -> np.ndarray:
    """Load a WAV as mono float32 @ 16 kHz. Assumes fixtures match."""
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if sr != 16000:
        raise ValueError(f"{path}: expected 16 kHz, got {sr}")
    if sw == 2:
        pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        pcm = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"{path}: unsupported sample width {sw}")
    if nch > 1:
        pcm = pcm.reshape(-1, nch).mean(axis=1)
    return pcm.astype(np.float32)


def _vad_trace(vad: Any, audio: np.ndarray) -> list[dict]:
    """Feed ``audio`` 512-at-a-time; after each chunk record vad frame state.

    Reaches into ``SileroVADDirect`` internals to pull smooth_prob /
    smooth_db / state / hits / misses. Intentionally coupled: this is
    what makes the trace useful.
    """
    from core.vad_silero import SileroVADDirect
    assert isinstance(vad, SileroVADDirect)

    CHUNK = 512
    trace: list[dict] = []
    frame_idx = 0
    prev_state = vad._state  # noqa: SLF001
    # vad_silero smooths across last N frames; pull its internals after each
    for start in range(0, len(audio) - CHUNK + 1, CHUNK):
        chunk = audio[start:start + CHUNK]
        vad.accept_waveform(chunk)
        smooth_prob = (sum(vad._prob_window) / len(vad._prob_window)  # noqa: SLF001
                       if vad._prob_window else 0.0)  # noqa: SLF001
        smooth_db = (sum(vad._db_window) / len(vad._db_window)  # noqa: SLF001
                     if vad._db_window else -100.0)  # noqa: SLF001
        state = vad._state  # noqa: SLF001
        edge = None
        if state != prev_state:
            edge = f"{prev_state}->{state}"
        trace.append({
            "frame": frame_idx, "t_ms": round(frame_idx * 32, 1),
            "smooth_prob": round(smooth_prob, 4),
            "smooth_db": round(smooth_db, 2),
            "state": state,
            "hits": vad._hits, "misses": vad._misses,  # noqa: SLF001
            "edge": edge,
        })
        prev_state = state
        frame_idx += 1
    return trace


def bench_wp6_vad_replay(config: dict, results: Results) -> None:
    """WP6: replay all bench_mic_tees/*.wav through SileroVADDirect."""
    from core.vad_silero import SileroVADDirect

    if not MIC_TEES_DIR.exists():
        results.add(Measurement(
            bench="wp6_vad_replay", name="fixtures::present", status="fail",
            value=None, details={"dir": str(MIC_TEES_DIR)},
        ))
        return

    wavs = sorted(MIC_TEES_DIR.glob("*.wav"))
    results.add(Measurement(
        bench="wp6_vad_replay", name="fixtures::count", status="info",
        value=len(wavs), details={"dir": str(MIC_TEES_DIR)},
    ))
    if not wavs:
        return

    # Build VAD with production-default TTS-mode params (this is what
    # interrupt_monitor uses). Mac dBFS -22 default.
    icfg = config.get("interrupt", {})
    import platform
    if platform.system() == "Darwin":
        db_default = float(icfg.get("vad_db_threshold_during_tts_mac", -22.0))
    else:
        db_default = float(icfg.get("vad_db_threshold_during_tts_rpi", -32.0))

    frames_dir = results.run_dir / "frames"

    for wav in wavs:
        try:
            audio = _load_wav_mono_16k(wav)
        except Exception as exc:
            results.add(Measurement(
                bench="wp6_vad_replay", name=f"load::{wav.name}",
                status="fail", value=str(exc),
            ))
            continue

        vad = SileroVADDirect(
            model_path=str(ROOT / icfg.get("vad_model_path", "data/silero_vad.onnx")),
            prob_threshold=float(icfg.get("vad_prob_threshold_during_tts", 0.5)),
            db_threshold=db_default,
            smoothing_window=int(icfg.get("vad_smoothing_window", 5)),
            required_hits=int(icfg.get("vad_required_hits", 3)),
            required_misses=int(icfg.get("vad_required_misses", 24)),
        )
        trace = _vad_trace(vad, audio)
        # Dump full frame trace to frames/ for debug
        dump_path = frames_dir / f"vad_{wav.stem}.jsonl"
        with dump_path.open("w", encoding="utf-8") as f:
            for frame in trace:
                f.write(json.dumps(frame, ensure_ascii=False) + "\n")

        edges = [t for t in trace if t["edge"]]
        start_edges = [t for t in edges if t["edge"].endswith("ACTIVE")]
        end_edges = [t for t in edges if t["edge"].endswith("IDLE")]
        duration_s = len(audio) / 16000
        results.add(Measurement(
            bench="wp6_vad_replay", name=f"wav::{wav.name}",
            status="info",
            value={"starts": len(start_edges), "ends": len(end_edges)},
            details={
                "duration_s": round(duration_s, 2),
                "n_frames": len(trace),
                "starts": [{"t_ms": e["t_ms"], "smooth_prob": e["smooth_prob"],
                            "smooth_db": e["smooth_db"]} for e in start_edges],
                "ends": [{"t_ms": e["t_ms"], "smooth_prob": e["smooth_prob"],
                          "smooth_db": e["smooth_db"]} for e in end_edges],
                "frame_dump": str(dump_path.relative_to(results.run_dir)),
                "params": {"prob_thresh": vad._prob_threshold,  # noqa: SLF001
                           "db_thresh": vad._db_threshold,  # noqa: SLF001
                           "smoothing": vad._smoothing_window,  # noqa: SLF001
                           "hits": vad._required_hits,  # noqa: SLF001
                           "misses": vad._required_misses},  # noqa: SLF001
            },
        ))


# =====================================================================
# Live benches — require mic + speaker
# =====================================================================
#
# Structure shared across live benches: build TTSEngine+Pipeline +
# InterruptMonitor, play a clip, observe timing + VAD + interrupt edges.
# Each bench is a separate function so --only <bench> can select.


def _build_live_components(config: dict) -> tuple[Any, Any, Any, Any]:
    """Shared builder: SpeechRecognizer + ASRNormalizer + TTSEngine + TTSPipeline.

    Returns pre-built instances to share across live benches so we only
    pay the SenseVoice load cost once.
    """
    from core.speech_recognizer import SpeechRecognizer
    from core.asr_normalizer import ASRNormalizer
    from core.tts import TTSEngine, TTSPipeline
    sr = SpeechRecognizer(config)
    nm = ASRNormalizer(config)
    engine = TTSEngine(config)
    pipeline = TTSPipeline(engine)
    return sr, nm, engine, pipeline


def _build_monitor(config: dict, sr: Any, nm: Any,
                   on_interrupt: Callable[[], None] | None = None,
                   on_soft_pause: Callable[[], None] | None = None,
                   on_soft_resume: Callable[[], None] | None = None) -> Any:
    from core.interrupt_monitor import InterruptMonitor
    return InterruptMonitor(
        config, on_interrupt=on_interrupt, on_soft_pause=on_soft_pause,
        on_soft_resume=on_soft_resume,
        speech_recognizer=sr, asr_normalizer=nm,
    )


LONG_TEXT = (
    "下面是一段比较长的测试文本，我会一直说下去，直到你打断我为止。"
    "今天的天气其实挺不错的，阳光明媚，温度适宜，是个出门散步的好日子。"
    "我们可以聊聊最近发生的有趣的事情，或者讨论一下未来的计划。"
    "你想听什么呢，工作的事情，还是生活上的琐事，都可以随便说。"
    "Jarvis 会一直陪着你，无论是开心还是难过，我都在这里。"
)

SHORT_TEXT = "我在这里一直说话，你可以打断我。"


def _prompt(msg: str) -> None:
    """Print a prompt that stands out in interleaved TTS logs."""
    print(f"\n\x1b[1;36m>>> {msg}\x1b[0m", flush=True)


def _wait_for(event: Any, timeout_s: float, poll: float = 0.05) -> bool:
    """Poll an event-like (or predicate callable) until set or timeout."""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if callable(event):
            if event():
                return True
        elif event.is_set():
            return True
        time.sleep(poll)
    return False


# =====================================================================
# WP1 / migration — interrupt latency + keyword sweep (live)
# =====================================================================


def bench_live_interrupt_latency(config: dict, results: Results, *,
                                  runs: int = 3,
                                  keyword: str = "停") -> None:
    """Measure speech→detect for a given keyword across N runs."""
    from core.tts import SentenceType
    from core.vad_silero import SileroVADDirect

    if config.get("interrupt", {}).get("vad_provider") != "silero_direct":
        results.add(Measurement(
            bench="live_interrupt", name="precondition",
            status="fail", value=None,
            details={"note": "interrupt.vad_provider must be silero_direct"},
        ))
        return

    # Build engine ONCE (so stream_player + SenseVoice load only once), but
    # rebuild TTSPipeline per run — its _text_queue/_audio_queue can hold
    # stale _SENTINEL tokens from prior-run abort+stop, which would cause
    # fresh workers in the next run to exit immediately and playback to
    # never start.
    from core.speech_recognizer import SpeechRecognizer
    from core.asr_normalizer import ASRNormalizer
    from core.tts import TTSEngine, TTSPipeline
    sr = SpeechRecognizer(config)
    nm = ASRNormalizer(config)
    engine = TTSEngine(config)

    samples: list[dict] = []
    try:
        for run_i in range(1, runs + 1):
            detected: list[float] = []
            vad_start: list[float | None] = []
            pipeline = TTSPipeline(engine)  # fresh queues each run

            def on_interrupt() -> None:
                detected.append(time.perf_counter())
                vad = monitor._vad  # noqa: SLF001
                vad_start.append(vad.last_start_perf if isinstance(vad, SileroVADDirect)
                                 else None)
                pipeline.abort()

            monitor = _build_monitor(config, sr, nm, on_interrupt=on_interrupt)
            monitor.start()
            pipeline.start()

            # Transcript tee: print every SenseVoice output in real time so
            # failed keyword matches can be diagnosed (usually ASR mis-heard
            # a multi-char keyword at conversational speed).
            transcripts: list[str] = []
            _orig_check = monitor._check_partial  # noqa: SLF001

            def _logged_check(text: str) -> None:
                transcripts.append(text)
                print(f"  \x1b[2m[transcript] {text!r}\x1b[0m", flush=True)
                _orig_check(text)

            monitor._check_partial = _logged_check  # type: ignore[method-assign]  # noqa: SLF001

            _prompt(f"[run {run_i}/{runs}] 3 秒后播放 — 随时说 '{keyword}' 打断")
            time.sleep(3)
            if isinstance(monitor._vad, SileroVADDirect):  # noqa: SLF001
                monitor._vad.reset()  # noqa: SLF001
            monitor.start_mic_listener()
            pipeline.submit(LONG_TEXT, SentenceType.FIRST)
            pipeline.finish()

            # Wait up to 30s for interrupt or playback end
            deadline = time.perf_counter() + 30
            while time.perf_counter() < deadline:
                if detected:
                    break
                if pipeline._done.is_set():  # noqa: SLF001
                    break
                time.sleep(0.05)
            monitor.stop_mic_listener()
            monitor.stop()
            pipeline.stop()

            # Guard against empty lists — short-circuit or-expression makes the
            # condition safe, but the details dict built inside the branch would
            # otherwise IndexError on vad_start[0] when the run yielded no fire.
            if not detected or not vad_start or vad_start[0] is None:
                results.add(Measurement(
                    bench="live_interrupt", name=f"{keyword}::run{run_i}",
                    status="fail", value=None,
                    details={"reason": "no detect or no VAD start",
                             "detected": bool(detected),
                             "vad_start_captured": (
                                 bool(vad_start) and vad_start[0] is not None
                             ),
                             # Key diagnostic: what did SenseVoice ACTUALLY hear?
                             # If transcripts has items but keyword wasn't matched,
                             # the ASR mis-heard — not a VAD / latency issue.
                             "transcripts": transcripts},
                ))
                continue

            speech_to_detect_ms = (detected[0] - vad_start[0]) * 1000
            sample = {
                "run_index": run_i,
                "keyword": keyword,
                "speech_to_detect_ms": round(speech_to_detect_ms, 1),
                "transcripts": transcripts,  # typically 1 item, but log all
            }
            samples.append(sample)
            results.add(Measurement(
                bench="live_interrupt", name=f"{keyword}::run{run_i}",
                status="info", value=round(speech_to_detect_ms, 1), unit="ms",
                details=sample,
            ))
    finally:
        # Close stream_player explicitly — Python-GC'd shutdown races with
        # PortAudio teardown and can segfault on process exit. This is
        # paranoid but cheap.
        try:
            engine.close_stream_player()
        except Exception as exc:
            LOGGER.warning("stream_player close error (ignored): %s", exc)

    if samples:
        ms = [s["speech_to_detect_ms"] for s in samples]
        results.add(Measurement(
            bench="live_interrupt", name=f"{keyword}::summary",
            status="info",
            value={"median_ms": round(statistics.median(ms), 1),
                   "min_ms": round(min(ms), 1), "max_ms": round(max(ms), 1),
                   "hit_rate": f"{len(samples)}/{runs}"},
            details={"samples": samples},
        ))


def bench_live_keyword_sweep(config: dict, results: Results) -> None:
    """Run interrupt latency for 4 common keywords × 2 runs each."""
    keywords = ["停", "等一下", "打住", "暂停"]
    for kw in keywords:
        bench_live_interrupt_latency(config, results, runs=2, keyword=kw)


# =====================================================================
# Live: false-positive during TTS
# =====================================================================


def bench_live_false_positive(config: dict, results: Results, *,
                               duration_s: float = 30.0) -> None:
    """Play TTS for ``duration_s`` seconds; user stays silent; count fires."""
    from core.tts import SentenceType
    from core.vad_silero import SileroVADDirect

    sr, nm, engine, pipeline = _build_live_components(config)

    fires: list[float] = []

    def on_interrupt() -> None:
        fires.append(time.perf_counter())
        pipeline.abort()

    monitor = _build_monitor(config, sr, nm, on_interrupt=on_interrupt)
    monitor.start()
    pipeline.start()

    _prompt(
        f"[false-positive test] {duration_s:.0f}s TTS, 请保持沉默 — "
        "任何 interrupt 触发都是假报"
    )
    time.sleep(2)
    if isinstance(monitor._vad, SileroVADDirect):  # noqa: SLF001
        monitor._vad.reset()  # noqa: SLF001
    monitor.start_mic_listener()

    text = LONG_TEXT + " " + LONG_TEXT  # pad to ~duration
    pipeline.submit(text, SentenceType.FIRST)
    pipeline.finish()

    t_start = time.perf_counter()
    while time.perf_counter() - t_start < duration_s:
        if pipeline._done.is_set():  # noqa: SLF001
            break
        time.sleep(0.1)

    monitor.stop_mic_listener()
    monitor.stop()
    pipeline.stop()

    elapsed = time.perf_counter() - t_start
    false_pos_rate = len(fires) / max(elapsed, 0.001)
    results.add(Measurement(
        bench="live_false_positive", name="rate_per_second",
        status="pass" if len(fires) == 0 else "fail",
        value=round(false_pos_rate, 4), unit="/s",
        details={"fires": len(fires), "duration_s": round(elapsed, 1),
                 "note": "any non-zero fire during silent TTS is a false positive"},
    ))


# =====================================================================
# Live: soft-stop SIGSTOP + afplay verification (CRITICAL)
# =====================================================================
# Multi-session-deferred blocker: does SIGSTOP on afplay actually freeze
# audio output on Mac? Measures:
#   (a) SIGSTOP → perceived-silence delay (buffer drain time)
#   (b) SIGCONT → perceived-resume delay
#   (c) 3s timer → auto-SIGCONT works
#   (d) Hard interrupt mid-soft-pause → CANCELLED state → no SIGCONT rebound
#
# How "perceived" is measured: no built-in way to sample what Mac is
# playing. Instead we prompt the user to report in real time — the
# bench logs a timestamp when the user presses Enter.


def _prompt_enter(msg: str, timeout_s: float = 10.0) -> float | None:
    """Print msg, wait for user Enter; return perf_counter at press or None."""
    import select
    print(f"\n\x1b[1;33m>>> {msg}  (Enter = now)\x1b[0m", flush=True)
    rlist, _, _ = select.select([sys.stdin], [], [], timeout_s)
    if rlist:
        sys.stdin.readline()
        return time.perf_counter()
    return None


def _wait_for_playback_active(engine: Any, timeout_s: float = 15.0) -> bool:
    """Poll until audio is actually playing — via either playback path.

    Handles two cases:

    1. Stream-player path (default): the persistent AudioStreamPlayer
       is open and the ring buffer has PCM queued. We check the ring's
       available_read — once it's >0, the callback is actively feeding
       samples to the speaker.

    2. Subprocess fallback: legacy afplay/ffplay path. Check
       ``engine._play_proc`` for a live subprocess.

    MiniMax TTS synth takes ~300-800ms; for either path we wait up to
    ``timeout_s`` for the first real audio to flow.
    """
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        # Path 1: stream player — ring has samples = callback is feeding speaker
        sp = getattr(engine, "_stream_player", None)
        if sp is not None and sp.is_running:
            if sp._ring.available_read() > 0:  # noqa: SLF001
                return True
        # Path 2: subprocess fallback
        with engine._play_lock:  # noqa: SLF001
            proc = engine._play_proc  # noqa: SLF001
        if proc is not None and proc.poll() is None:
            return True
        time.sleep(0.1)
    return False


def bench_live_soft_stop(config: dict, results: Results) -> None:
    """Direct SIGSTOP/SIGCONT verification on afplay — the critical test.

    Tests the kernel-level behavior in isolation (no InterruptMonitor,
    no mic): can SIGSTOP actually freeze afplay audio? Does SIGCONT
    resume cleanly? How many ms of audio buffer does the kernel drain?

    The integration path (InterruptMonitor soft-pause callback → VAD
    triggers → SIGSTOP fires) is in :func:`bench_live_soft_stop_integration`
    — opt-in, because it tickles a known pre-existing bus error in
    ``InterruptMonitor.stop_mic_listener`` cleanup.
    """
    from core.tts import TTSEngine, TTSPipeline, SentenceType

    import platform
    if platform.system() != "Darwin":
        results.add(Measurement(
            bench="live_soft_stop", name="platform",
            status="info", value=platform.system(),
            details={"note": "this bench targets macOS afplay specifically"},
        ))
        return

    engine = TTSEngine(config)
    pipeline = TTSPipeline(engine)
    pipeline.start()

    # Detect whether stream_player is active (duck path) or subprocess (SIGSTOP).
    # Informs the prompt wording — with ducking we expect volume drop, not silence.
    sp_cfg = config.get("tts", {}).get("stream_player", {})
    duck_path = bool(sp_cfg.get("enabled", True))
    duck_vol = sp_cfg.get("duck_volume", 0.3)

    print()
    print("=" * 64)
    print("  soft_stop — suspend_playback() 行为实测")
    print("=" * 64)
    print()
    if duck_path:
        print("  路径:stream_player.duck() (gain ducking, sample-accurate)")
        print(f"  预期:音量平滑降到 ~{int(duck_vol*100)}%,然后平滑回到 100%")
    else:
        print("  路径:SIGSTOP → afplay subprocess (legacy)")
        print("  预期:音频突然静音(可能带 ~300ms CoreAudio loop 尾巴)")
    print()
    print("  流程:")
    print("    1. 开始播放一段中文 TTS")
    print("    2. 稳定播放 2 秒后,程序调 suspend_playback()")
    if duck_path:
        print(f"       → 应听到音量『平滑降到 ~{int(duck_vol*100)}%』")
        print("    3. 听到音量变小按 Enter (10s 超时)")
    else:
        print("       → 应听到声音『突然停住』")
        print("    3. 听到静音按 Enter (10s 超时)")
    print("    4. 再过 2 秒,程序调 resume_playback()")
    if duck_path:
        print("       → 应听到音量『恢复到 100%』")
    else:
        print("       → 应听到声音『接着刚才的地方继续』")
    print("    5. 听到恢复的那一刻按 Enter (10s 超时)")
    print()
    print("  注意:整个过程保持安静,只用耳朵判断,用 Enter 打时间戳。")
    print()
    try:
        input("  准备好了按 Enter 开始 ... ")
    except EOFError:
        return
    print()

    # Kick off playback
    pipeline.submit(LONG_TEXT, SentenceType.FIRST)
    pipeline.finish()

    # CRITICAL: wait for afplay to actually be alive before SIGSTOP.
    if not _wait_for_playback_active(engine, timeout_s=15.0):
        results.add(Measurement(
            bench="live_soft_stop", name="setup::audio_started",
            status="fail", value=False,
            details={"reason": "afplay never came up within 15s — MiniMax key? network?"},
        ))
        pipeline.abort()
        pipeline.stop()
        return
    results.add(Measurement(
        bench="live_soft_stop", name="setup::audio_started",
        status="pass", value=True,
    ))

    # Let user hear a stable 2s of audio so SIGSTOP is clearly perceptible
    print("  \x1b[2m(音频播放中 — 2 秒后 SIGSTOP ...)\x1b[0m", flush=True)
    time.sleep(2)
    print("\n  \x1b[1;31m*** SIGSTOP now ***\x1b[0m", flush=True)
    t_stop_sent = time.perf_counter()
    stopped = engine.suspend_playback()
    results.add(Measurement(
        bench="live_soft_stop", name="sigstop::success",
        status="pass" if stopped else "fail", value=stopped,
        details={"note": "False = afplay wasn't running or already paused"},
    ))

    prompt_label = ("听到音量变小按 Enter" if duck_path
                    else "听到静音按 Enter (没听到就让它超时)")
    t_user_heard = _prompt_enter(prompt_label, timeout_s=10.0)
    drain_ms = ((t_user_heard - t_stop_sent) * 1000
                if t_user_heard else None)
    results.add(Measurement(
        bench="live_soft_stop",
        name=("duck::perceived_drop_ms" if duck_path
              else "sigstop::perceived_silence_ms"),
        status="pass" if drain_ms is not None else "fail",
        value=round(drain_ms, 0) if drain_ms is not None else None,
        unit="ms",
        details={"timed_out": t_user_heard is None,
                 "path": "duck" if duck_path else "sigstop",
                 "note": ("duck: time from suspend to user hearing volume drop "
                          "(30ms ramp + perceptual reaction); "
                          "sigstop: time to perceived silence (CoreAudio drain)")},
    ))

    # 2s of pause so resume is also clearly audible
    print("  \x1b[2m(已暂停 — 2 秒后 SIGCONT ...)\x1b[0m", flush=True)
    time.sleep(2)
    print("\n  \x1b[1;32m*** SIGCONT now ***\x1b[0m", flush=True)
    t_cont_sent = time.perf_counter()
    resumed = engine.resume_playback()
    results.add(Measurement(
        bench="live_soft_stop", name="sigcont::success",
        status="pass" if resumed else "fail", value=resumed,
    ))

    prompt_label_r = ("听到音量恢复按 Enter" if duck_path
                      else "听到恢复播放按 Enter (没听到就让它超时)")
    t_user_resume = _prompt_enter(prompt_label_r, timeout_s=10.0)
    resume_ms = ((t_user_resume - t_cont_sent) * 1000
                 if t_user_resume else None)
    results.add(Measurement(
        bench="live_soft_stop",
        name=("unduck::perceived_resume_ms" if duck_path
              else "sigcont::perceived_resume_ms"),
        status="pass" if resume_ms is not None else "fail",
        value=round(resume_ms, 0) if resume_ms is not None else None,
        unit="ms",
        details={"timed_out": t_user_resume is None,
                 "path": "unduck" if duck_path else "sigcont"},
    ))

    pipeline.abort()
    pipeline.stop()

    print()
    print("  \x1b[1msoft_stop Part A 完成。\x1b[0m")
    print("  若要继续验证 InterruptMonitor 集成路径(VAD → 自动软停),")
    print("  跑: python scripts/bench_voice_pipeline.py --only live_soft_stop_integration")
    print("  (注意: integration 路径可能在 cleanup 阶段 bus error — 今晚已知未做尾巴)")
    print()


def bench_live_soft_stop_integration(config: dict, results: Results) -> None:
    """Integration: VAD → InterruptMonitor soft-pause callback → SIGSTOP.

    Tests that the full state machine (NORMAL → DUCKED → NORMAL) wires
    through correctly: user makes noise, VAD fires, on_soft_pause
    callback runs suspend_playback, timer after 3s auto-resumes.

    Known bug: after this bench, cleanup may hit ``zsh: bus error`` —
    ``interrupt_monitor.stop_mic_listener`` can race the
    sounddevice.InputStream C read. Same bug as today's migration note
    "Run 3 bus error" — pre-existing, not caused by this bench.

    Results are written append-only to results.jsonl, so even if
    cleanup crashes the measurements are preserved.
    """
    import platform
    if platform.system() != "Darwin":
        results.add(Measurement(
            bench="live_soft_stop_integration", name="platform",
            status="info", value=platform.system(),
        ))
        return

    cfg = copy.deepcopy(config)
    cfg.setdefault("interrupt", {})["soft_stop_enabled"] = True
    cfg["interrupt"]["soft_stop_timeout_ms"] = 3000

    from core.tts import SentenceType

    sr, nm, engine, pipeline = _build_live_components(cfg)
    soft_pause_times: list[float] = []
    soft_resume_times: list[float] = []

    def on_soft_pause() -> None:
        soft_pause_times.append(time.perf_counter())
        engine.suspend_playback()

    def on_soft_resume() -> None:
        soft_resume_times.append(time.perf_counter())
        engine.resume_playback()

    monitor = _build_monitor(
        cfg, sr, nm,
        on_soft_pause=on_soft_pause, on_soft_resume=on_soft_resume,
    )
    monitor.start()
    pipeline.start()

    print()
    print("=" * 64)
    print("  soft_stop_integration — VAD → soft-pause callback")
    print("=" * 64)
    print()
    print("  流程:")
    print("    1. 开始播放 TTS")
    print("    2. 音频稳定后,请发出任意声音(咳嗽、啊-声均可,**不要**说关键词)")
    print("    3. 你应该听到播放『静音』(软停)")
    print("    4. 3s 后应自动『恢复』(timer)")
    print()
    try:
        input("  准备好了按 Enter 开始 ... ")
    except EOFError:
        return
    print()

    pipeline.submit(LONG_TEXT + " " + LONG_TEXT, SentenceType.FIRST)
    pipeline.finish()

    if not _wait_for_playback_active(engine, timeout_s=15.0):
        results.add(Measurement(
            bench="live_soft_stop_integration", name="setup::audio_started",
            status="fail", value=False,
        ))
        try:
            monitor.stop()
            pipeline.stop()
        except Exception:
            pass
        return
    results.add(Measurement(
        bench="live_soft_stop_integration", name="setup::audio_started",
        status="pass", value=True,
    ))

    print("  \x1b[1;33m*** 现在请发出声音 ***\x1b[0m", flush=True)
    monitor.start_mic_listener()
    t_mic_start = time.perf_counter()

    # Wait up to 20s for a full pause+resume cycle
    deadline = t_mic_start + 20
    while time.perf_counter() < deadline:
        if soft_pause_times and soft_resume_times:
            break
        time.sleep(0.1)

    pause_ok = len(soft_pause_times) >= 1
    resume_ok = len(soft_resume_times) >= 1
    results.add(Measurement(
        bench="live_soft_stop_integration", name="state_machine::pause_fired",
        status="pass" if pause_ok else "fail", value=len(soft_pause_times),
        details={"fires_at_s": [round(t - t_mic_start, 2) for t in soft_pause_times]},
    ))
    results.add(Measurement(
        bench="live_soft_stop_integration", name="state_machine::resume_fired",
        status="pass" if resume_ok else "fail", value=len(soft_resume_times),
        details={"fires_at_s": [round(t - t_mic_start, 2) for t in soft_resume_times]},
    ))
    if pause_ok and resume_ok:
        gap_ms = (soft_resume_times[0] - soft_pause_times[0]) * 1000
        within = abs(gap_ms - cfg["interrupt"]["soft_stop_timeout_ms"]) < 1500
        results.add(Measurement(
            bench="live_soft_stop_integration", name="state_machine::pause_to_resume_ms",
            status="pass" if within else "fail",
            value=round(gap_ms, 0), unit="ms",
            details={"target_ms": cfg["interrupt"]["soft_stop_timeout_ms"],
                     "note": "should be ~3000 ms (timer) or earlier if VAD ended first"},
        ))

    # Best-effort cleanup — may bus-error on stop_mic_listener (pre-existing).
    print("  \x1b[2m(cleanup — 可能 bus error, 数据已写入 jsonl 不会丢)\x1b[0m",
          flush=True)
    try:
        monitor.stop_mic_listener()
        monitor.stop()
        pipeline.abort()
        pipeline.stop()
    except Exception as exc:
        LOGGER.warning("cleanup error (expected — pre-existing bug): %s", exc)


# =====================================================================
# Live: SIGSTOP probe — compare afplay vs ffplay on a pure tone
# =====================================================================
# Plays a clean 5s sine wave via each available macOS player, asks the
# user to categorize what they hear after SIGSTOP: clean silence /
# glitch loop / no change. Pure tone makes glitches unambiguous —
# looping a sine wave sounds like "beep beep beep", far more obvious
# than speech where the loop hides in natural phonemes.
#
# Purpose: figure out which player has clean SIGSTOP behavior so WP7
# soft_stop can actually be turned on without sounding like a bug.


def _generate_test_tone_wav(path: Path, duration_s: float = 5.0,
                             freq_hz: float = 440.0) -> None:
    """Write a mono 16 kHz WAV with a sine wave at ~50% amplitude."""
    sr = 16000
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
    # Fade in/out 50ms to avoid click
    env = np.ones_like(t)
    fade = int(sr * 0.05)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    audio = 0.5 * np.sin(2 * np.pi * freq_hz * t) * env
    pcm = (audio * 32767).clip(-32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def _prompt_choice(msg: str, choices: dict[str, str],
                    timeout_s: float = 30.0) -> str | None:
    """Prompt user to pick one of ``choices``. Returns the choice key or None."""
    import select
    options = " / ".join(f"({k}) {v}" for k, v in choices.items())
    print(f"\n\x1b[1;33m>>> {msg}\x1b[0m\n    {options}  > ", end="",
          flush=True)
    rlist, _, _ = select.select([sys.stdin], [], [], timeout_s)
    if not rlist:
        return None
    line = sys.stdin.readline().strip().lower()
    for k in choices:
        if line == k or line == k[0]:
            return k
    return None


def _probe_one_player(name: str, cmd: list[str], tone_path: Path) -> dict:
    """Play ``tone_path`` via ``cmd``, SIGSTOP after 2s, record user verdict."""
    import shutil
    if not shutil.which(cmd[0]):
        return {"player": name, "skipped": True, "reason": "not installed"}

    print()
    print(f"  \x1b[1m--- {name} ({shutil.which(cmd[0])}) ---\x1b[0m")
    print(f"  播放 5 秒 440Hz 正弦波 → 2 秒后 SIGSTOP")
    try:
        input("  准备好按 Enter 开始 ... ")
    except EOFError:
        return {"player": name, "skipped": True, "reason": "stdin eof"}

    proc = subprocess.Popen(
        cmd + [str(tone_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Wait for process to actually be running + audio stable
    time.sleep(2.0)
    if proc.poll() is not None:
        print(f"  \x1b[31m{name} 进程提前结束了 (exit code {proc.returncode})\x1b[0m")
        return {"player": name, "skipped": True, "reason": f"exit={proc.returncode}"}

    print(f"  \x1b[1;31m*** SIGSTOP now ***\x1b[0m", flush=True)
    t_stop = time.perf_counter()
    try:
        os.kill(proc.pid, signal.SIGSTOP)
    except (ProcessLookupError, PermissionError) as exc:
        return {"player": name, "skipped": True, "reason": f"kill: {exc}"}

    verdict = _prompt_choice(
        "听到了什么?",
        {"c": "clean — 干净静音",
         "l": "loop — 有卡顿/重复尾巴",
         "n": "nothing — 声音没变化",
         "s": "skip"},
        timeout_s=15.0,
    )
    t_verdict = time.perf_counter()

    # Resume + cleanup
    try:
        os.kill(proc.pid, signal.SIGCONT)
        time.sleep(0.3)
        proc.terminate()
        proc.wait(timeout=2.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

    return {
        "player": name,
        "verdict": verdict or "timeout",
        "time_to_verdict_ms": round((t_verdict - t_stop) * 1000, 0),
        "sigstop_sent": True,
    }


def bench_live_stream_player_probe(config: dict, results: Results) -> None:
    """Probe the self-written AudioStreamPlayer: duck/unduck on pure tone.

    Uses a background feeder thread to loop a 1s pure sine forever —
    this guarantees audio is playing continuously no matter how long
    the user takes to answer prompts (previous version ran out of
    audio before the user could hear the effect).

    500Hz chosen over 440 because 500 Hz × 1s = 500 integer cycles at
    48kHz (96 samples/cycle), so looping the buffer seamlessly with no
    phase discontinuity click.
    """
    del config
    import threading

    from core.audio_stream_player import AudioStreamPlayer

    sr = 48000
    freq = 500  # Hz — integer divisor of sr for phase-continuous loop
    # 1s of pure sine, no fade (fade would click at loop boundary).
    n = sr
    phase = 2 * np.pi * freq * np.arange(n, dtype=np.float32) / sr
    tone_1s = (0.5 * np.sin(phase)).astype(np.float32)

    print()
    print("=" * 64)
    print("  AudioStreamPlayer probe — duck/unduck on 500Hz continuous tone")
    print("=" * 64)
    print()
    print("  流程:")
    print("    1. 持续播放 500Hz 纯正弦(后台线程循环喂,不会断)")
    print("    2. 你听到稳定音 → 按 Enter 开始")
    print("    3. 程序 duck 到 30%,你听 2 秒后报告 clean/loop/nothing")
    print("    4. 程序 unduck 回 100%,你再听 2 秒后报告")
    print()
    print("  关键听点:")
    print("    - duck: 音量'平滑降下去',不是'突然静音然后重复短片段'")
    print("    - unduck: 音量'干净回来',不是'啪'地爆音")
    print()
    try:
        input("  准备好按 Enter 开始 ... ")
    except EOFError:
        return
    print()

    # Ring 3s to absorb 1-2s of pre-fed audio plus feeder overshoot.
    player = AudioStreamPlayer(sample_rate=sr, channels=1, ring_seconds=3.0)
    try:
        player.start()
    except Exception as exc:
        results.add(Measurement(
            bench="live_stream_player_probe", name="start",
            status="fail", value=str(exc),
        ))
        return

    stop_feed = threading.Event()

    def _feeder() -> None:
        """Loop the 1s tone forever until stop_feed is set."""
        while not stop_feed.is_set():
            # write() blocks when ring full → natural pacing, no overshoot.
            # Timeout so we notice stop_feed even if the player dies.
            player.write(tone_1s, wait_if_full=True, timeout_s=2.0)

    feeder_thread = threading.Thread(
        target=_feeder, daemon=True, name="bench-tone-feeder",
    )
    feeder_thread.start()

    try:
        # Let user hear a stable baseline for 2s before the first action.
        time.sleep(2.0)

        print("  \x1b[1;33m*** duck(0.3) now — 听 2 秒 ***\x1b[0m", flush=True)
        player.duck(volume=0.3, ramp_ms=30)
        # CRITICAL: give user time to hear the ducked state BEFORE the prompt.
        # Previous version prompted instantly → user hadn't heard anything yet.
        time.sleep(2.0)

        verdict_duck = _prompt_choice(
            "duck 之后听到的是?",
            {"c": "clean — 音量平滑降到 ~30%,持续稳定",
             "l": "loop — 有卡顿或重复尾巴",
             "n": "nothing — 音量没变化",
             "s": "skip"},
            timeout_s=30.0,
        )
        results.add(Measurement(
            bench="live_stream_player_probe", name="duck::verdict",
            status="pass" if verdict_duck == "c" else
                   "fail" if verdict_duck in ("l", "n") else "info",
            value=verdict_duck,
            details={"ramp_ms": 30, "duck_volume": 0.3,
                     "hold_ms_before_prompt": 2000},
        ))

        print("  \x1b[1;32m*** unduck now — 听 2 秒 ***\x1b[0m", flush=True)
        player.unduck(ramp_ms=10)
        time.sleep(2.0)

        verdict_unduck = _prompt_choice(
            "unduck 之后听到的是?",
            {"c": "clean — 音量平滑恢复到 100%,持续稳定",
             "l": "loop/click — 恢复时有爆音或卡顿",
             "n": "nothing — 音量没变化",
             "s": "skip"},
            timeout_s=30.0,
        )
        results.add(Measurement(
            bench="live_stream_player_probe", name="unduck::verdict",
            status="pass" if verdict_unduck == "c" else
                   "fail" if verdict_unduck in ("l", "n") else "info",
            value=verdict_unduck,
            details={"ramp_ms": 10, "hold_ms_before_prompt": 2000,
                     "callback_calls": player.callback_calls,
                     "underflow_count": player.underflow_count},
        ))

        # Health check — zero underflow over a few thousand callbacks means
        # the player kept up cleanly. Non-zero suggests GIL contention or
        # device issue worth investigating.
        results.add(Measurement(
            bench="live_stream_player_probe", name="health::underflow_rate",
            status="pass" if player.underflow_count == 0 else "info",
            value=player.underflow_count, unit="occurrences",
            details={"callback_calls": player.callback_calls,
                     "note": "0 = clean feeding; non-zero = investigate GIL/device"},
        ))
    finally:
        stop_feed.set()
        feeder_thread.join(timeout=3.0)
        player.flush()
        player.stop()


def bench_live_sigstop_probe(config: dict, results: Results) -> None:
    """Compare SIGSTOP behavior across available players on a pure tone."""
    del config  # unused

    import platform
    if platform.system() != "Darwin":
        results.add(Measurement(
            bench="live_sigstop_probe", name="platform",
            status="info", value=platform.system(),
            details={"note": "probe targets macOS players; Linux has its own defaults"},
        ))
        return

    tone_path = results.run_dir / "frames" / "probe_tone.wav"
    _generate_test_tone_wav(tone_path)

    print()
    print("=" * 64)
    print("  SIGSTOP probe — compare players on a pure 440Hz tone")
    print("=" * 64)
    print()
    print("  对照测试:同一段 5s 正弦波分别用 afplay / ffplay 播放,")
    print("  2s 后各发 SIGSTOP,你告诉我哪个干净、哪个卡顿。")
    print()
    print("  正弦波 loop 听起来像'哔-哔-哔',跟说话混着不好分辨,")
    print("  但正弦波一循环就一听即知。")
    print()

    players = [
        ("afplay", ["afplay"]),
        ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]),
    ]
    # mpv if installed — cleaner pause semantics than afplay typically
    import shutil
    if shutil.which("mpv"):
        players.append(("mpv", ["mpv", "--no-video", "--quiet"]))

    for name, cmd in players:
        result = _probe_one_player(name, cmd, tone_path)
        status = (
            "pass" if result.get("verdict") == "c" else
            "fail" if result.get("verdict") in ("l", "n") else
            "info"
        )
        results.add(Measurement(
            bench="live_sigstop_probe", name=f"{name}::verdict",
            status=status, value=result.get("verdict"),
            details=result,
        ))

    print()
    print("  probe 完成。看 summary.md 对比各 player verdict。")
    print("  若某 player 是 'c' (干净):改 core/tts.py:615 把 Mac 默认换成它")


# =====================================================================
# Live: E2E first-audio latency (needs --real-api)
# =====================================================================


def bench_live_first_audio_e2e(config: dict, results: Results) -> None:
    """Measure text→first audio byte latency end-to-end.

    Currently measures TTS-only (canned text, no LLM) to keep the bench
    zero-cost by default. LLM-inclusive variant is TODO — would need a
    clean way to trigger a short deterministic response.
    """
    from core.tts import TTSEngine

    engine = TTSEngine(config)
    text = "好的。"  # minimal to maximize floor sensitivity

    # Measure time to synth_to_file returning (first bytes written)
    t0 = time.perf_counter()
    path = engine.synth_to_file(text)
    synth_ms = (time.perf_counter() - t0) * 1000

    results.add(Measurement(
        bench="live_e2e", name="tts_only::text_to_synth_ms",
        status="info", value=round(synth_ms, 0), unit="ms",
        details={"text": text, "path": path, "engine": engine.engine_name,
                 "note": "TTS synth only (no LLM); real-api enabled"},
    ))


# =====================================================================
# Baseline + summary
# =====================================================================


def _compare_to_baseline(results: Results) -> list[str]:
    """Return a list of regression lines (empty if no regressions)."""
    if not BASELINE_FILE.exists():
        return []
    with BASELINE_FILE.open("r", encoding="utf-8") as f:
        baseline = json.load(f)
    base_by_key: dict[str, Any] = {
        f"{m['bench']}::{m['name']}": m for m in baseline.get("measurements", [])
    }
    regressions: list[str] = []
    for m in results.measurements:
        key = f"{m.bench}::{m.name}"
        base = base_by_key.get(key)
        if not base:
            continue
        if m.status == "fail" and base["status"] == "pass":
            regressions.append(
                f"{key}: pass → fail (was {base.get('value')!r}, now {m.value!r})",
            )
        elif isinstance(m.value, (int, float)) and isinstance(base.get("value"), (int, float)):
            base_v = float(base["value"])
            got_v = float(m.value)
            # Flag >30% regression on perf numbers
            if base_v > 0 and got_v > base_v * 1.3:
                regressions.append(
                    f"{key}: {base_v:.2f} → {got_v:.2f} {m.unit} "
                    f"(+{(got_v/base_v - 1)*100:.0f}%)",
                )
    return regressions


def _write_baseline(results: Results) -> None:
    BASELINE_FILE.write_text(
        json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "measurements": [m.to_dict() for m in results.measurements],
        }, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _write_summary_md(results: Results, regressions: list[str]) -> None:
    by_bench = results.by_bench()
    lines = [
        f"# Voice pipeline bench — {results.run_dir.name}",
        "",
        f"- total measurements: {len(results.measurements)}",
        f"- fails: {results.count_fails()}",
        f"- regressions vs baseline: {len(regressions)}",
        "",
    ]
    if regressions:
        lines.append("## Regressions")
        lines.extend(f"- `{r}`" for r in regressions)
        lines.append("")
    for bench_name, ms in by_bench.items():
        lines.append(f"## {bench_name}")
        lines.append("")
        lines.append("| name | status | value | unit |")
        lines.append("|---|---|---|---|")
        for m in ms:
            v = m.value
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
                if len(v) > 80:
                    v = v[:77] + "..."
            lines.append(f"| {m.name} | {m.status} | {v} | {m.unit} |")
        lines.append("")
    (results.run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


# =====================================================================
# Registry + main
# =====================================================================


OFFLINE_BENCHES: dict[str, Callable[[dict, Results], None]] = {
    "wp2_normalizer":    bench_wp2_normalizer,
    "wp3_preprocessor":  bench_wp3_preprocessor,
    "wp4_sentence":      bench_wp4_sentence_divider,
    "wp5_memory":        bench_wp5_memory_injection,
    "wp6_vad":           bench_wp6_vad_replay,
}

LIVE_BENCHES: dict[str, Callable[[dict, Results], None]] = {
    "live_interrupt":     lambda c, r: bench_live_interrupt_latency(c, r, runs=3, keyword="停"),
    "live_keyword_sweep": bench_live_keyword_sweep,
    "live_false_pos":     bench_live_false_positive,
    "live_soft_stop":     bench_live_soft_stop,
}

# Opt-in only: runnable via ``--only <name>``, NOT via ``--live``.
# Kept out of the default live bundle because they have known side
# effects (cleanup crashes, long runtimes, or require extra prep).
OPT_IN_BENCHES: dict[str, Callable[[dict, Results], None]] = {
    "live_soft_stop_integration":   bench_live_soft_stop_integration,
    "live_sigstop_probe":           lambda c, r: bench_live_sigstop_probe(c, r),
    "live_stream_player_probe":     bench_live_stream_player_probe,
}

REAL_API_BENCHES: dict[str, Callable[[dict, Results], None]] = {
    "live_e2e":           bench_live_first_audio_e2e,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                        help="also run live benches (needs mic + speaker)")
    parser.add_argument("--real-api", action="store_true",
                        help="allow benches that hit real LLM/TTS APIs")
    parser.add_argument("--only", default="",
                        help="run only named bench (e.g. wp2_normalizer)")
    parser.add_argument("--refresh-baseline", action="store_true",
                        help="overwrite baseline.json with this run's results")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    config = _load_config()
    run_dir = _make_run_dir()
    _dump_config_snapshot(config, run_dir)
    results = Results(run_dir)

    # Which benches to run?
    selected: list[tuple[str, Callable]] = []
    if args.only:
        for reg in (OFFLINE_BENCHES, LIVE_BENCHES, OPT_IN_BENCHES, REAL_API_BENCHES):
            if args.only in reg:
                selected.append((args.only, reg[args.only]))
                break
        if not selected:
            all_benches = (list(OFFLINE_BENCHES) + list(LIVE_BENCHES)
                           + list(OPT_IN_BENCHES) + list(REAL_API_BENCHES))
            print(f"Unknown bench: {args.only}", file=sys.stderr)
            print(f"Available: {sorted(all_benches)}", file=sys.stderr)
            return 2
    else:
        selected.extend(OFFLINE_BENCHES.items())
        if args.live:
            selected.extend(LIVE_BENCHES.items())
        if args.real_api:
            selected.extend(REAL_API_BENCHES.items())
        # OPT_IN_BENCHES are never added automatically — only via --only

    print(f"[bench] run_dir={run_dir}")
    print(f"[bench] benches: {[name for name, _ in selected]}")
    print()

    for name, fn in selected:
        print(f"=== {name} ===", flush=True)
        try:
            fn(config, results)
        except Exception as exc:
            LOGGER.exception("bench %s crashed", name)
            results.add(Measurement(
                bench=name, name="__crash__", status="fail", value=str(exc),
                details={"exception_type": type(exc).__name__},
            ))

    regressions = _compare_to_baseline(results)
    _write_summary_md(results, regressions)

    if args.refresh_baseline:
        _write_baseline(results)
        print(f"[bench] baseline refreshed → {BASELINE_FILE}")

    print()
    print(f"=== summary ===")
    print(f"  measurements: {len(results.measurements)}")
    print(f"  fails:        {results.count_fails()}")
    print(f"  regressions:  {len(regressions)}")
    print(f"  output:       {run_dir}")

    fails = results.count_fails()
    if fails > 0 or regressions:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[bench] interrupted", file=sys.stderr)
        sys.exit(130)
    except Exception:  # noqa: BLE001
        logging.exception("bench harness crashed")
        sys.exit(2)
