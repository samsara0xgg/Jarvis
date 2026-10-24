r"""Live check of ``web_search`` depth deep (ADR 0289, 0359): ask the deep backend real questions.

    .venv/bin/python -m scripts.deep_search_check "question one" "question two" ...

Builds the deep seam exactly as the runtime does, from ``config/jarvis.yaml``'s
``tools.web.deep_preset`` and ``home.weather`` (his place, when the config has one), and prints one
JSON line per question: seconds, searches, whether it finished and why not, token use and cost
(tokens at the preset's price, from its own cost rows in a throwaway event log, plus $0.01 per
search), the sources, the pages found and the brief. Each question is one or more paid Anthropic
requests inside one minute; nothing is written to his event log and nothing reaches his
conversation. Keys come from the environment, then the runtime root's saved keys; none is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

import jarvis.runtime.deep_search as deep
from jarvis.deployment import load_env_file
from jarvis.runtime.search_location import HomePlace
from jarvis.shared.clock import SystemClock, zone_key
from jarvis.shared.here import Coarse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.execution.web_search_tool import DeepFindings

_SEARCH_FEE_USD = 0.01
_PRICING = Path(__file__).resolve().parent.parent / "data" / "pricing.json"
_SCRATCH_MARKERS = ("fetch directly", "is a string", "```", "print(", "def ", "import ")


def _home(
    config: Mapping[str, Any], zone: str | None,
) -> tuple[HomePlace | None, Coarse | None]:
    """His home as the config has it: for the location of the search, and as where he is."""
    block = (config.get("home") or {}).get("weather")
    if not isinstance(block, dict) or "latitude" not in block:
        return None, None
    home = HomePlace.from_weather(block, zone)
    return home, Coarse(home.lat, home.lng, home=True)


def _spent(log: Path) -> tuple[int, int, float]:
    """Tokens in, tokens out and cost in USD of the cost rows the check has written."""
    tokens_in = tokens_out = 0
    cost = 0.0
    with sqlite3.connect(log) as db:
        rows = db.execute("SELECT payload_json FROM events WHERE type = 'cost.recorded'")
        for (payload,) in rows:
            row = json.loads(payload)
            tokens_in += int(row.get("tokens_in") or 0)
            tokens_out += int(row.get("tokens_out") or 0)
            cost += float(row.get("cost_usd") or 0)
    return tokens_in, tokens_out, cost


def _line(found: DeepFindings, spent: tuple[int, int, float]) -> dict[str, Any]:
    """What one question printed: how it ended, what it cost, what it found."""
    return {
        "stopped": found.stopped,
        "searches": found.searches,
        "tokens_in": spent[0],
        "tokens_out": spent[1],
        "cost_usd": round(spent[2] + found.searches * _SEARCH_FEE_USD, 4),
        "scratch_in_brief": [m for m in _SCRATCH_MARKERS if m in found.brief],
        "sources": [one.row() for one in found.sources],
        "pages": len(found.pages),
        "brief": found.brief,
    }


def main() -> int:
    """Ask each question once and print what came back."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("questions", nargs="+")
    parser.add_argument("--config", type=Path, default=Path("config/jarvis.yaml"))
    args = parser.parse_args()
    root = Path(os.environ.get("JARVIS_RUNTIME_ROOT") or Path.home() / ".jarvis")
    load_env_file(root.expanduser())  # fill-only; the values are never read here
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    clock = SystemClock()
    home, where = _home(config, zone_key(clock.zone))
    with tempfile.TemporaryDirectory(prefix="deep-check-") as scratch:
        log = Path(scratch) / "events.db"
        search = deep.build_deep_search(
            config, event_log_path=log, pricing_path=_PRICING, account_cost=True, home=home,
        )
        if search is None:
            sys.stderr.write("no deep search: tools.web.deep_preset is not Anthropic on its host\n")
            return 1
        for question in args.questions:
            started = time.monotonic()
            before = _spent(log) if log.exists() else (0, 0, 0.0)
            line: dict[str, Any] = {"question": question}
            try:
                found = search.research(question, where=where, clock=clock)
            except deep.DeepSearchError as exc:
                line["error"] = str(exc)
            else:
                after = _spent(log)
                line |= _line(
                    found, (after[0] - before[0], after[1] - before[1], after[2] - before[2]),
                )
            line["seconds"] = round(time.monotonic() - started, 1)
            sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
