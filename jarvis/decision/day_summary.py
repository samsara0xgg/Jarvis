"""L3 day summary — the summariser's input and the gate on its answer.

One local day of records goes in, one summary comes out or the reason to
store nothing. The gates are ``compaction.check_summary``'s, with this
summary's headings and ids restricted to that day's records. The runtime
owns when it runs and the client; L2 owns the day's records and the write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.compaction import check_summary

if TYPE_CHECKING:
    from collections.abc import Sequence

# The headings ``day_summary.prompt`` asks for; every day summary carries all of them.
REQUIRED_HEADINGS: Final[tuple[str, ...]] = (
    "### Topics",
    "### Decisions and facts the user stated",
    "### Unfinished",
)


def build_day_summary_messages(
    *,
    day: str,
    records: Sequence[tuple[str, str, str, str]],
) -> list[dict[str, Any]]:
    """The one user message the summariser sees: the day's records, each with its id."""
    lines = [f"[Records of {day}, oldest first; each line: record_id | time | speaker | words]"]
    lines.extend(f"{rid} | {ts} | {source} | {text}" for rid, ts, source, text in records)
    return [{"role": "user", "content": "\n".join(lines)}]


def check_day_summary(
    text: str | None,
    finish_reason: str | None,
    *,
    max_chars: int,
    record_ids: Sequence[str],
) -> str | None:
    """Return why the summary must not be stored, or None when it may."""
    return check_summary(
        text,
        finish_reason,
        max_chars=max_chars,
        known_ids=record_ids,
        headings=REQUIRED_HEADINGS,
    )


__all__ = ["REQUIRED_HEADINGS", "build_day_summary_messages", "check_day_summary"]
