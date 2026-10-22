r"""Live check of ``web_search`` depth deep (ADR 0289): ask the deep backend real questions.

    .venv/bin/python -m scripts.deep_search_check "question one" "question two" ...

Builds the deep seam exactly as the runtime does, from ``config/jarvis.yaml``'s
``tools.web.deep_preset``, and prints one JSON line per question: seconds, searches, whether it
finished, token use and cost (tokens at the preset's price plus $0.01 per search), the sources and
the brief. Each question is one paid Anthropic request; nothing is written to the event log and
nothing reaches his conversation. Keys come from the environment, then the runtime root's saved
keys; none is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

import jarvis.runtime.deep_search as deep
from jarvis.deployment import load_env_file

if TYPE_CHECKING:
    from jarvis.decision.llm import ChatResult
    from jarvis.execution.web_search_tool import DeepFindings

_SEARCH_FEE_USD = 0.01
_PRICE_PER_MTOK = {"input": 2.0, "output": 10.0}  # claude-sonnet-5-5, the default deep preset
_SCRATCH_MARKERS = ("fetch directly", "is a string", "```", "print(", "def ", "import ")


def main() -> int:
    """Ask each question once and print what came back."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("questions", nargs="+")
    parser.add_argument("--config", type=Path, default=Path("config/jarvis.yaml"))
    args = parser.parse_args()
    root = Path(os.environ.get("JARVIS_RUNTIME_ROOT") or Path.home() / ".jarvis")
    load_env_file(root.expanduser())  # fill-only; the values are never read here
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    search = deep.build_deep_search(config)
    if search is None:
        sys.stderr.write("no deep search: tools.web.deep_preset is not Anthropic on its host\n")
        return 1
    seen: list[ChatResult] = []
    read = deep.findings_from

    def keep(result: ChatResult) -> DeepFindings:
        seen.append(result)
        return read(result)

    deep.findings_from = keep  # the usage the brief does not carry
    for question in args.questions:
        started = time.monotonic()
        line: dict[str, Any] = {"question": question}
        try:
            found = search.research(question)
        except deep.DeepSearchError as exc:
            line["error"] = str(exc)
        else:
            used = seen[-1]
            tokens_in, tokens_out = used.tokens_in, used.tokens_out
            line |= {
                "complete": found.complete,
                "searches": found.searches,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": round(
                    tokens_in * _PRICE_PER_MTOK["input"] / 1e6
                    + tokens_out * _PRICE_PER_MTOK["output"] / 1e6
                    + found.searches * _SEARCH_FEE_USD,
                    4,
                ),
                "scratch_in_brief": [m for m in _SCRATCH_MARKERS if m in found.summary],
                "sources": [url for _title, url in found.sources],
                "brief": found.summary,
            }
        line["seconds"] = round(time.monotonic() - started, 1)
        sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
