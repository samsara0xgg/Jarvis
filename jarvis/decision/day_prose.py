"""L3 day prose: the input and the gate for the one model-made line of the ledger (ADR 0201).

A day's computed numbers, its conversation summary and its work report go in; two to four plain
sentences come out, or the reason to store nothing. The runtime owns when it runs and the client;
L2 owns the numbers and the write.
"""

from __future__ import annotations

import re
from typing import Any, Final

_REPORT_CHARS: Final[int] = 2600
_MARKDOWN: Final[re.Pattern[str]] = re.compile(r"^\s*(#{1,6}\s|[-*+]\s|\d+\.\s)", re.MULTILINE)


def build_day_prose_messages(
    day: str, numbers: str, summary: str | None, report: str | None,
) -> list[dict[str, Any]]:
    """The one user message the writer sees: numbers, summary and report excerpt of ``day``."""
    excerpt = (report or "").strip()[:_REPORT_CHARS]
    talked = (summary or "").strip() or "(none yet)"
    content = "\n\n".join(
        (
            f"DAY: {day}",
            f"NUMBERS (computed by the program):\n{numbers.strip()}",
            f"DAY SUMMARY (conversation with the assistant):\n{talked}",
            f"DAILY WORK REPORT (excerpt):\n{excerpt or '(none yet)'}",
        ),
    )
    return [{"role": "user", "content": content}]


def check_day_prose(text: str | None, finish_reason: str | None, max_chars: int) -> str | None:
    """Return why the line must not be stored, or None when it may."""
    body = (text or "").strip()
    gates: tuple[tuple[bool, str], ...] = (
        (not body, "empty"),
        (finish_reason in ("length", "max_tokens"), "cut off by the output limit"),
        (len(body) > max_chars, f"{len(body)} chars over the {max_chars} cap"),
        (bool(_MARKDOWN.search(body)), "contains markdown headings or bullets"),
    )
    return next((reason for failed, reason in gates if failed), None)


__all__ = ["build_day_prose_messages", "check_day_prose"]
