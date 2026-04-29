"""Analyze v4 bench output. v3 -> v4 deltas:
  - 2-branch label_kind (no greeting). VALID_LABEL_KIND = {tool, defer}
  - REQUIRED_FIELDS reduced (drops reasoning_chain, ambiguity_signals, slot_alternatives)
  - VALID_INTENTS no longer includes "greeting" (greeting -> defer:out_of_scope)
  - VALID_DEFER reduced to 5 values
  - check_coherence drops greeting branch logic
  - Reuses postprocess_v3 functions (normalize_mixed_whitespace, repair_label) — same content slot fix-ups apply

Usage:
    uv run python experiments/surrogate_v1/scripts/analyze_v4.py <model>
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from experiments.surrogate_v1.scripts.postprocess_v3 import (
    repair_label, normalize_mixed_whitespace,
)
from experiments.surrogate_v1.scripts.schema_v4 import (
    TOOL_INTENTS, ACTION_VALUES, DEFER_VALUES, SLOT_VALUES,
)

VALID_INTENTS = set(TOOL_INTENTS) | {None}
VALID_ACTIONS = set(ACTION_VALUES)
VALID_DEFER = set(DEFER_VALUES) | {None}
VALID_SLOTS = set(SLOT_VALUES)
VALID_LABEL_KIND = {"tool", "defer"}

# v4 dropped: reasoning_chain, ambiguity_signals, slot_alternatives
REQUIRED_FIELDS = {
    "label_kind", "intent", "action", "tool_calls", "spans",
    "defer_reason", "alternative_tools", "response_text",
}

CITED_OBS_RE = re.compile(r"<cited_obs>\[([^\]]*)\]</cited_obs>")


def parse_label(raw: str) -> dict | None:
    """v4 output wrapped in {"label": {...}}. Returns inner label dict."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines)
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError:
        try:
            outer, _ = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(outer, dict) and "label" in outer:
        return outer["label"]
    return outer if isinstance(outer, dict) else None


def resolve_span_offsets(text: str, user_text: str) -> dict:
    """Compute (start_char, end_char) for an LLM-extracted text in user_text."""
    if not isinstance(text, str) or not text:
        return {"found": False, "start": None, "end": None,
                "method": "empty", "resolved_text": text}
    idx = user_text.find(text)
    if idx >= 0:
        return {"found": True, "start": idx, "end": idx + len(text),
                "method": "exact", "resolved_text": text}
    stripped = text.strip()
    if stripped and stripped != text:
        idx = user_text.find(stripped)
        if idx >= 0:
            return {"found": True, "start": idx, "end": idx + len(stripped),
                    "method": "strip", "resolved_text": stripped}
    norm_user = normalize_mixed_whitespace(user_text)
    norm_text = normalize_mixed_whitespace(text)
    if norm_text and norm_text in norm_user:
        return {"found": True, "start": None, "end": None,
                "method": "normalized", "resolved_text": text}
    return {"found": False, "start": None, "end": None,
            "method": "hallucination", "resolved_text": text}


def check_coherence(label: dict) -> str | None:
    """Defensive coherence checks for 2-branch v4 schema."""
    lk = label.get("label_kind")
    intent = label.get("intent")
    defer = label.get("defer_reason")
    tool_calls = label.get("tool_calls", [])
    spans = label.get("spans", [])

    if lk == "tool":
        if intent is None:
            return f"tool branch but intent=null"
        if defer is not None:
            return f"tool branch but defer_reason={defer!r}"
    elif lk == "defer":
        if intent is not None:
            return f"defer branch but intent={intent!r}"
        if defer is None:
            return f"defer branch but defer_reason=null"
        if tool_calls or spans:
            return f"defer branch with tools/spans non-empty"
    else:
        return f"unknown label_kind={lk!r}"
    if (intent is None) != (defer is not None):
        return f"intent/defer XOR violated: intent={intent!r}, defer={defer!r}"
    return None


def main(model: str) -> int:
    bench = ROOT / f"experiments/surrogate_v1/bench80_v4_{model}.jsonl"
    out_json = ROOT / f"experiments/surrogate_v1/analysis_v4_{model}.json"
    if not bench.exists():
        print(f"missing {bench}", file=sys.stderr)
        return 1

    samples = [json.loads(l) for l in bench.open()]
    n = len(samples)
    n_api_ok = sum(1 for s in samples if s.get("ok"))

    json_valid = schema_ok = coherent = 0
    eint_ok = eint_n = eact_ok = eact_n = edef_ok = edef_n = eslot_ok = eslot_n = 0
    spans_total = spans_text_found = 0
    spans_resolved_exact = spans_resolved_strip = spans_resolved_norm = 0
    response_present = response_nonempty = response_with_cite = 0
    label_kind_dist: Counter[str] = Counter()
    intent_dist: Counter[str] = Counter()
    defer_dist: Counter[str] = Counter()
    repaired_count = 0
    failures: list[dict] = []

    for s in samples:
        if not s.get("ok"):
            failures.append({"id": s["id"], "user_text": s["user_text"],
                             "issue": f"api_fail: {s.get('error', '')[:120]}"})
            continue
        p = parse_label(s.get("raw_text", ""))
        if p is None:
            failures.append({"id": s["id"], "user_text": s["user_text"],
                             "issue": "json_parse_fail",
                             "raw_excerpt": s.get("raw_text", "")[:200]})
            continue
        json_valid += 1
        missing = REQUIRED_FIELDS - set(p.keys())
        if missing:
            failures.append({"id": s["id"], "user_text": s["user_text"],
                             "issue": f"missing_fields:{sorted(missing)}"})
            continue
        schema_ok += 1

        before_repair = json.dumps(p, ensure_ascii=False, sort_keys=True)
        repair_label(p, s["user_text"])
        after_repair = json.dumps(p, ensure_ascii=False, sort_keys=True)
        if before_repair != after_repair:
            repaired_count += 1

        coh_issue = check_coherence(p)
        if coh_issue:
            failures.append({"id": s["id"], "user_text": s["user_text"],
                             "issue": f"coherence: {coh_issue}"})
        else:
            coherent += 1

        lk = p.get("label_kind")
        label_kind_dist[str(lk)] += 1

        intent = p.get("intent")
        intent_dist[str(intent)] += 1
        eint_n += 1
        if intent in VALID_INTENTS:
            eint_ok += 1
        else:
            failures.append({"id": s["id"], "user_text": s["user_text"],
                             "issue": f"intent_oov:{intent!r}"})

        action = p.get("action")
        eact_n += 1
        if action in VALID_ACTIONS:
            eact_ok += 1

        defer = p.get("defer_reason")
        defer_dist[str(defer)] += 1
        edef_n += 1
        if defer in VALID_DEFER:
            edef_ok += 1

        for span in p.get("spans", []):
            eslot_n += 1
            if span.get("slot") in VALID_SLOTS:
                eslot_ok += 1
            spans_total += 1
            res = resolve_span_offsets(span.get("text", ""), s["user_text"])
            if res["found"]:
                spans_text_found += 1
                if res["method"] == "exact":
                    spans_resolved_exact += 1
                elif res["method"] == "strip":
                    spans_resolved_strip += 1
                elif res["method"] == "normalized":
                    spans_resolved_norm += 1
                if res["start"] is not None:
                    span["start_char"] = res["start"]
                    span["end_char"] = res["end"]
                span["offset_method"] = res["method"]
            else:
                failures.append({"id": s["id"], "user_text": s["user_text"],
                                 "issue": f"text_hallucination slot={span.get('slot')!r} text={span.get('text')!r}"})

        rt = p.get("response_text", "")
        if isinstance(rt, str):
            response_present += 1
            if rt.strip():
                response_nonempty += 1
            if CITED_OBS_RE.search(rt):
                response_with_cite += 1

    total_in = sum(s.get("input_tokens", 0) for s in samples if s.get("ok"))
    total_cached = sum(s.get("cached_tokens", 0) for s in samples if s.get("ok"))
    total_out = sum(s.get("output_tokens", 0) for s in samples if s.get("ok"))
    total_cost = sum(s.get("cost_usd", 0) for s in samples if s.get("ok"))
    avg_lat = sum(s.get("elapsed_ms", 0) for s in samples if s.get("ok")) / max(n_api_ok, 1)
    cache_hit_rate = (total_cached / total_in) if total_in else 0.0
    avg_in = total_in / max(n_api_ok, 1)
    avg_out = total_out / max(n_api_ok, 1)

    pricing = json.loads((ROOT / "data/pricing.json").read_text())
    pp = pricing["llm"][model]
    monthly_calls = 100 * 30
    cost_uncached = (avg_in * pp["input_per_1m"] / 1e6 * monthly_calls
                     + avg_out * pp["output_per_1m"] / 1e6 * monthly_calls)
    cache_rate = pp.get("cache_read_per_1m") or pp["input_per_1m"]
    cost_actual = (
        (avg_in * (1 - cache_hit_rate) * pp["input_per_1m"] / 1e6
         + avg_in * cache_hit_rate * cache_rate / 1e6
         + avg_out * pp["output_per_1m"] / 1e6) * monthly_calls
    )

    report = {
        "model": model,
        "prompt_version": "v4",
        "n_samples": n,
        "n_api_ok": n_api_ok,
        "json_valid": json_valid,
        "schema_ok": schema_ok,
        "coherent": coherent,
        "post_repaired": repaired_count,
        "label_kind_distribution": dict(label_kind_dist),
        "intent_distribution": dict(intent_dist),
        "defer_distribution": dict(defer_dist),
        "enum_compliance": {
            "intent": {"ok": eint_ok, "n": eint_n, "rate": eint_ok / max(eint_n, 1)},
            "action": {"ok": eact_ok, "n": eact_n, "rate": eact_ok / max(eact_n, 1)},
            "defer_reason": {"ok": edef_ok, "n": edef_n, "rate": edef_ok / max(edef_n, 1)},
            "slot": {"ok": eslot_ok, "n": eslot_n, "rate": eslot_ok / max(eslot_n, 1)},
        },
        "text_substring_rate": {
            "found": spans_text_found,
            "n": spans_total,
            "rate": spans_text_found / max(spans_total, 1),
            "resolved_exact": spans_resolved_exact,
            "resolved_strip": spans_resolved_strip,
            "resolved_normalized": spans_resolved_norm,
            "hallucinated": spans_total - spans_text_found,
        },
        "response_text": {
            "present": response_present,
            "non_empty": response_nonempty,
            "with_cited_obs": response_with_cite,
        },
        "tokens": {
            "input_avg_per_call": round(avg_in, 1),
            "output_avg_per_call": round(avg_out, 1),
            "cache_hit_rate": round(cache_hit_rate, 4),
        },
        "cost": {
            "actual_bench_usd": round(total_cost, 4),
            "actual_per_call_usd": round(total_cost / max(n_api_ok, 1), 4),
            "monthly_uncached_usd": round(cost_uncached, 2),
            "monthly_actual_cache_usd": round(cost_actual, 2),
        },
        "latency_avg_ms": round(avg_lat, 0),
        "failures": failures,
    }

    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    ec = report["enum_compliance"]
    print(f"== {model} (v4) ==")
    print(f"n={n} api_ok={n_api_ok} json={json_valid} schema={schema_ok} coherent={coherent} repaired={repaired_count}")
    print(f"label_kind dist: {dict(label_kind_dist)}")
    print(f"intent  : {ec['intent']['ok']}/{ec['intent']['n']} = {ec['intent']['rate']:.3f}")
    print(f"action  : {ec['action']['ok']}/{ec['action']['n']} = {ec['action']['rate']:.3f}")
    print(f"defer   : {ec['defer_reason']['ok']}/{ec['defer_reason']['n']} = {ec['defer_reason']['rate']:.3f}")
    print(f"slot    : {ec['slot']['ok']}/{ec['slot']['n']} = {ec['slot']['rate']:.3f}")
    tsr = report["text_substring_rate"]
    print(f"spans : {tsr['found']}/{tsr['n']} = {tsr['rate']:.3f} "
          f"(exact={tsr['resolved_exact']} strip={tsr['resolved_strip']} norm={tsr['resolved_normalized']} hallucinated={tsr['hallucinated']})")
    print(f"tokens: in_avg={avg_in:.0f} cached={total_cached} cache_hit={cache_hit_rate*100:.1f}%")
    print(f"cost  : ${total_cost:.4f} | monthly: uncached=${cost_uncached:.2f} | actual_cache=${cost_actual:.2f}")
    print(f"latency avg: {avg_lat:.0f}ms")
    print(f"failures: {len(failures)}")
    if failures:
        print("\nFirst 5 failures:")
        for f in failures[:5]:
            print(f"  id={f['id']}: {f['issue']}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: analyze_v4.py <model>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
