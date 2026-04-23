"""Compute total USD cost for trace rows using ``data/pricing.json``.

Re-reads the trace table from scratch and tallies three cost components
per turn:

  * LLM primary:   ``llm_model`` × ``llm_tokens_in/out/cache_read``
  * Router:        ``llm_metadata.router_model`` × ``router_tokens_in/out``
  * TTS:           chars in ``assistant_text`` × active TTS engine rate

Existing ``trace.cost_usd`` values are ignored (they were computed with a
stale config.yaml table). This script does not write back — it only
reports. Use ``--update-db`` if/when you want to rewrite ``cost_usd``.

Usage::

    python scripts/compute_trace_costs.py                 # last 30 days
    python scripts/compute_trace_costs.py --days 7
    python scripts/compute_trace_costs.py --top 20        # show 20 costliest turns
    python scripts/compute_trace_costs.py --tts-model gpt-4o-mini-tts
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "memory" / "jarvis_memory.db"
DEFAULT_PRICING = REPO_ROOT / "data" / "pricing.json"
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"

LOGGER = logging.getLogger("compute_trace_costs")


# ---------------------------------------------------------------------------
# Cost primitives — single-turn, single-component.
# ---------------------------------------------------------------------------

def _llm_cost(
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    cache_read: int | None,
    cache_write: int | None,
    llm_table: dict,
) -> float | None:
    """Compute LLM cost for one (primary or router) call. Pure arithmetic.

    Returns ``None`` when the model is missing from the table or the
    essential token counts are absent — matches ``compute_cost_usd``.
    """
    if not model or tokens_in is None or tokens_out is None:
        return None
    entry = llm_table.get(model)
    if not entry:
        return None
    cr = cache_read or 0
    cw = cache_write or 0
    non_cached = max(0, tokens_in - cr - cw)
    cw_rate = entry.get("cache_write", entry["input"] * 1.25)
    cost = (
        non_cached * entry["input"]
        + tokens_out * entry["output"]
        + cr * entry.get("cache_read", 0)
        + cw * cw_rate
    ) / 1_000_000
    return round(cost, 6)


def _tts_cost(text: str | None, tts_entry: dict | None) -> float | None:
    """Cost for one TTS synthesis, based on the active engine's billing unit.

    MiniMax and OpenAI ``tts-1*`` models are character-billed, so we use
    ``len(text)``. ``gpt-4o-mini-tts`` is token-billed for input plus audio
    tokens on output; we approximate with text token count (``~len(text)``
    is a conservative upper bound for Chinese, low bound for English) and
    omit the audio-duration component since trace doesn't record duration.
    """
    if not text or not tts_entry:
        return None
    chars = len(text)
    unit = tts_entry.get("unit")
    if unit == "character":
        rate = tts_entry.get("input_per_1m_chars")
        if rate is None:
            return None
        return round(chars * rate / 1_000_000, 6)
    if unit == "token":
        in_rate = tts_entry.get("input_per_1m_tokens") or 0.0
        # Audio-token output cost omitted: trace has no audio duration.
        return round(chars * in_rate / 1_000_000, 6)
    return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _parse_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def tally(
    rows: list[sqlite3.Row],
    llm_table: dict,
    tts_entry: dict | None,
) -> dict[str, Any]:
    """Walk trace rows, compute per-turn costs, build aggregates.

    Returns a dict with totals, per-model breakdown, per-session breakdown,
    and the per-row list (for top-N ranking by caller).
    """
    per_row: list[dict[str, Any]] = []
    totals = {"llm": 0.0, "router": 0.0, "tts": 0.0, "turns": 0}
    per_model: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "cost": 0.0})
    per_session: dict[str, dict[str, float]] = defaultdict(lambda: {"turns": 0, "cost": 0.0})
    unknown_models: set[str] = set()

    for r in rows:
        meta = _parse_metadata(r["llm_metadata"])

        primary_cost = _llm_cost(
            model=r["llm_model"],
            tokens_in=r["llm_tokens_in"],
            tokens_out=r["llm_tokens_out"],
            cache_read=r["cache_read_input_tokens"],
            cache_write=meta.get("cache_creation_input_tokens"),
            llm_table=llm_table,
        )
        # Flag only models that are genuinely absent from the pricing table —
        # a None cost due to missing tokens is not a pricing gap.
        if r["llm_model"] and r["llm_model"] not in llm_table:
            unknown_models.add(r["llm_model"])

        router_cost = _llm_cost(
            model=meta.get("router_model"),
            tokens_in=meta.get("router_tokens_in"),
            tokens_out=meta.get("router_tokens_out"),
            cache_read=None,
            cache_write=None,
            llm_table=llm_table,
        )
        rm = meta.get("router_model")
        if rm and rm not in llm_table:
            unknown_models.add(rm)

        tts_val = _tts_cost(r["assistant_text"], tts_entry)
        total = (primary_cost or 0.0) + (router_cost or 0.0) + (tts_val or 0.0)

        per_row.append({
            "id": r["id"],
            "session_id": r["session_id"],
            "turn_id": r["turn_id"],
            "created_at": r["created_at"],
            "llm_model": r["llm_model"],
            "llm_cost": primary_cost,
            "router_cost": router_cost,
            "tts_cost": tts_val,
            "total": round(total, 6),
        })

        totals["llm"] += primary_cost or 0.0
        totals["router"] += router_cost or 0.0
        totals["tts"] += tts_val or 0.0
        totals["turns"] += 1

        if r["llm_model"] and primary_cost is not None:
            per_model[r["llm_model"]]["calls"] += 1
            per_model[r["llm_model"]]["cost"] += primary_cost
        per_session[r["session_id"]]["turns"] += 1
        per_session[r["session_id"]]["cost"] += total

    return {
        "totals": totals,
        "per_model": dict(per_model),
        "per_session": dict(per_session),
        "per_row": per_row,
        "unknown_models": sorted(unknown_models),
    }


# ---------------------------------------------------------------------------
# TTS engine selection
# ---------------------------------------------------------------------------

def pick_tts_model(config: dict, override: str | None = None) -> str:
    """Return the TTS model id matching the active engine in config.yaml."""
    if override:
        return override
    tts_cfg = config.get("tts", {})
    engine = tts_cfg.get("engine", "minimax")
    if engine == "minimax":
        return tts_cfg.get("minimax_model", "speech-2.8-turbo")
    if engine == "openai":
        return tts_cfg.get("openai_tts_model", "gpt-4o-mini-tts")
    if engine == "azure":
        return "azure/speech/azure-tts"
    # edge-tts, pyttsx3 → free; return a sentinel the pricing lookup will miss.
    return engine


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_pricing(path: Path) -> tuple[dict, dict]:
    """Return (llm_table, tts_index). llm_table is legacy format for reuse."""
    if not path.exists():
        LOGGER.error("pricing file missing: %s — run scripts/refresh_pricing.py", path)
        sys.exit(2)
    data = json.loads(path.read_text(encoding="utf-8"))
    llm_table: dict = {}
    for model, e in (data.get("llm") or {}).items():
        if e.get("input_per_1m") is None or e.get("output_per_1m") is None:
            continue
        row = {"input": e["input_per_1m"], "output": e["output_per_1m"]}
        if e.get("cache_read_per_1m") is not None:
            row["cache_read"] = e["cache_read_per_1m"]
        if e.get("cache_write_per_1m") is not None:
            row["cache_write"] = e["cache_write_per_1m"]
        llm_table[model] = row
    tts_index = data.get("tts") or {}
    return llm_table, tts_index


def _fmt(n: float) -> str:
    if n >= 1.0:
        return f"${n:,.4f}"
    if n >= 0.001:
        return f"${n:.5f}"
    return f"${n:.7f}"


def _print_report(result: dict, tts_model: str, tts_found: bool, days: int, top_n: int) -> None:
    t = result["totals"]
    grand = t["llm"] + t["router"] + t["tts"]

    print(f"\n=== Trace cost report — last {days} days, {t['turns']} turns ===")
    print(f"  LLM primary:  {_fmt(t['llm']):>14s}")
    print(f"  Router:       {_fmt(t['router']):>14s}")
    tts_note = "" if tts_found else f"  (no pricing for {tts_model!r} — skipped)"
    print(f"  TTS:          {_fmt(t['tts']):>14s}  [{tts_model}]{tts_note}")
    print(f"  ----")
    print(f"  Grand total:  {_fmt(grand):>14s}")
    if t["turns"]:
        print(f"  Per turn avg: {_fmt(grand / t['turns']):>14s}")

    pm = result["per_model"]
    if pm:
        print("\nPer primary LLM:")
        print(f"  {'model':38s} {'calls':>6s} {'cost':>12s} {'avg/call':>12s}")
        for m, v in sorted(pm.items(), key=lambda kv: -kv[1]["cost"]):
            avg = v["cost"] / v["calls"] if v["calls"] else 0
            print(f"  {m:38s} {v['calls']:>6d} {_fmt(v['cost']):>12s} {_fmt(avg):>12s}")

    ps = result["per_session"]
    if ps:
        top = sorted(ps.items(), key=lambda kv: -kv[1]["cost"])[:10]
        print("\nTop 10 sessions by cost:")
        print(f"  {'session_id':40s} {'turns':>6s} {'cost':>12s}")
        for sid, v in top:
            print(f"  {sid:40s} {v['turns']:>6d} {_fmt(v['cost']):>12s}")

    rows = sorted(result["per_row"], key=lambda r: -r["total"])[:top_n]
    if rows:
        print(f"\nTop {top_n} most expensive turns:")
        print(f"  {'id':>6s} {'created_at':20s} {'model':30s} {'llm':>10s} {'rtr':>9s} {'tts':>9s} {'total':>10s}")
        for r in rows:
            m = (r["llm_model"] or "-")[:30]
            print(
                f"  {r['id']:>6d} {r['created_at'][:19]:20s} {m:30s} "
                f"{_fmt(r['llm_cost'] or 0):>10s} {_fmt(r['router_cost'] or 0):>9s} "
                f"{_fmt(r['tts_cost'] or 0):>9s} {_fmt(r['total']):>10s}"
            )

    if result["unknown_models"]:
        print(
            "\nWarning: no pricing found for {}: {}".format(
                len(result["unknown_models"]),
                ", ".join(result["unknown_models"]),
            )
        )
        print("  (these turns contribute $0 to the tally — add mappings to scripts/refresh_pricing.py)")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"trace DB (default {DEFAULT_DB})")
    parser.add_argument("--pricing", type=Path, default=DEFAULT_PRICING, help=f"pricing.json (default {DEFAULT_PRICING})")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=f"config.yaml (default {DEFAULT_CONFIG})")
    parser.add_argument("--days", type=int, default=30, help="look-back window in days (default 30)")
    parser.add_argument("--tts-model", help="override active TTS model for pricing lookup")
    parser.add_argument("--top", type=int, default=10, help="how many costly turns to list (default 10)")
    parser.add_argument("--include-test", action="store_true", help="include test-model rows (excluded by default)")
    args = parser.parse_args()

    llm_table, tts_index = _load_pricing(args.pricing)
    config = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}

    tts_model = pick_tts_model(config, args.tts_model)
    tts_entry = tts_index.get(tts_model)
    if tts_entry is None:
        LOGGER.warning("no TTS pricing entry for %r — TTS costs will be $0", tts_model)

    if not args.db.exists():
        LOGGER.error("trace DB missing: %s", args.db)
        return 2

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    sql = (
        "SELECT id, session_id, turn_id, created_at, llm_model, "
        "llm_tokens_in, llm_tokens_out, cache_read_input_tokens, llm_metadata, "
        "assistant_text "
        "FROM trace "
        "WHERE created_at > datetime('now', ?) "
    )
    params: list[Any] = [f"-{args.days} days"]
    if not args.include_test:
        sql += "AND (llm_model IS NULL OR llm_model != 'test-model') "
    sql += "ORDER BY id ASC"

    rows = conn.execute(sql, params).fetchall()
    LOGGER.info("loaded %d trace rows over %d days", len(rows), args.days)

    result = tally(rows, llm_table, tts_entry)
    _print_report(result, tts_model, tts_entry is not None, args.days, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
