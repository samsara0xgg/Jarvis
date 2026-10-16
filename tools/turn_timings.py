"""Where each answered turn's time went, read from a brain over HTTP (ADR 0276).

    .venv/bin/python tools/turn_timings.py [--url http://jarvis:8006] [--since 2h] [--limit 200]
        [--token-file ~/.jarvis/brain-token] [--json]

Reads ``GET /inherent/turn-timings`` with the brain's token (read in this process from
``--token-file``, never printed or taken from the command line), prints one row per turn and, per
model, the p50 and p90 of each segment in ms. Every time is on the brain's own monotonic clock,
so the segments need no clock the Mac shares with the Pi.

Segments, for the request that produced the turn's first text:

- ``queue``: the intent sat in the log before the brain began the turn.
- ``context``: the turn began -> its first model request was made (history, ledger, state block).
- ``earlier``: tool rounds before the request that answered.
- ``wire``: that request sent -> its first stream event (network and the model reading the prompt).
- ``think``: its first thinking -> its first text (providers that stream thinking first).
- ``gen``: that request sent -> its first text.
- ``to_chunk``: first text -> the first chunk that left her (guards, holds, the gate).
- ``first_chunk``: the turn began -> the first chunk left.
- ``held``: the longest wait named in ``holds`` (``held_by``).
"""

# ruff: noqa: T201

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_UNITS = {"m": 60, "h": 3600, "d": 86400}
SEGMENTS = (
    "queue", "context", "earlier", "wire", "think", "gen", "to_chunk", "first_chunk", "held",
)


def _read_token(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        sys.exit(f"{path} is readable by others; chmod 600 it")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        sys.exit(f"{path} is empty")
    return token


def _since_ms(text: str) -> int:
    """``90m``, ``2h``, ``1d`` ago, or an epoch in ms."""
    match = re.fullmatch(r"(\d+)([mhd])", text)
    if match is None:
        return int(text)
    return int((time.time() - int(match[1]) * _UNITS[match[2]]) * 1000)


def _fetch(url: str, token: str, since: int, limit: int) -> list[dict[str, Any]]:
    request = urllib.request.Request(  # noqa: S310 - the brain's own http URL
        f"{url.rstrip('/')}/inherent/turn-timings?since={since}&limit={limit}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as reply:  # noqa: S310
            return list(json.load(reply)["turns"])
    except urllib.error.HTTPError as exc:
        sys.exit(f"{url}: HTTP {exc.code}")
    except OSError as exc:
        sys.exit(f"{url}: {exc}")


def _diff(later: float | None, earlier: float | None) -> float | None:
    return None if later is None or earlier is None else round(later - earlier, 1)


def _answering(turn: dict[str, Any]) -> dict[str, Any]:
    """The request that produced the turn's first text, or an empty one."""
    for request in turn.get("requests", []):
        if request["kind"] == "decision" and request["first_text_ms"]:
            return dict(request)
    return {}


def segments(turn: dict[str, Any]) -> dict[str, float | None]:
    """The turn's segments in ms; ``None`` where the turn had no such moment."""
    answering = _answering(turn)
    held = turn.get("holds") or {}
    return {
        "queue": turn.get("queue_ms"),
        "context": turn.get("context_ms"),
        "earlier": _diff(answering.get("sent_ms"), turn.get("context_ms")),
        "wire": _diff(answering.get("first_event_ms"), answering.get("sent_ms")),
        "think": _diff(answering.get("first_text_ms"), answering.get("first_thinking_ms")),
        "gen": _diff(answering.get("first_text_ms"), answering.get("sent_ms")),
        "to_chunk": _diff(turn.get("first_chunk_ms"), turn.get("first_text_ms")),
        "first_chunk": turn.get("first_chunk_ms"),
        "held": max(held.values()) if held else None,
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    at = fraction * (len(ordered) - 1)
    low = int(at)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (at - low)


def _cell(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def _row(turn: dict[str, Any]) -> str:
    seg = segments(turn)
    answering = _answering(turn)
    at = datetime.fromtimestamp(turn["ts_epoch_ms"] / 1000, UTC).astimezone()
    when = at.strftime("%m-%d %H:%M:%S")
    tokens = "/".join(
        _cell(answering.get(key))
        for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
    )
    return (
        f"{when}  {str(turn.get('model') or '-')[:18]:<18} "
        f"{str(turn.get('channel') or '-')[:12]:<12} "
        + " ".join(f"{_cell(seg[name]):>6}" for name in SEGMENTS)
        + f"  {'T' if turn.get('tool_round_before_text') else '-'}"
        + f"  {turn.get('held_by') or '-':<15} {tokens}"
    )


def report(turns: list[dict[str, Any]]) -> str:
    """Per-turn rows, then p50 and p90 of each segment for each model."""
    header = (
        f"{'time':<14}  {'model':<18} {'channel':<12} "
        + " ".join(f"{name:>6}" for name in SEGMENTS)
        + "  T  held_by         in/cache_r/cache_w/out"
    )
    lines = [header, *(_row(turn) for turn in turns)]
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for turn in turns:
        by_model[str(turn.get("model") or "-")].append(turn)
    for model, group in sorted(by_model.items()):
        lines.append(f"\n{model}: {len(group)} turns (ms)")
        lines.append(f"{'':<5}" + " ".join(f"{name:>11}" for name in SEGMENTS))
        per_turn = [segments(turn) for turn in group]
        for label, fraction in (("p50", 0.5), ("p90", 0.9)):
            cells = []
            for name in SEGMENTS:
                values = [v for s in per_turn if (v := s[name]) is not None]
                cells.append(_cell(_percentile(values, fraction) if values else None))
            lines.append(f"{label:<5}" + " ".join(f"{cell:>11}" for cell in cells))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Fetch, then print the rows and the percentiles (or the raw facts)."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--url", default="http://jarvis:8006")
    parser.add_argument("--token-file", type=Path, default=Path.home() / ".jarvis" / "brain-token")
    parser.add_argument("--since", default="1d", help="90m, 2h, 1d, or an epoch in ms")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--json", action="store_true", help="print the raw facts")
    args = parser.parse_args(argv)
    turns = _fetch(args.url, _read_token(args.token_file), _since_ms(args.since), args.limit)
    if args.json:
        print(json.dumps(turns, ensure_ascii=False, indent=2))
    elif turns:
        print(report(turns))
    else:
        print("no turn timings in that window")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
