"""L3 compaction — the summariser's input and the gates on its answer.

Stateless: the prompt comes from config, the previous summary and the
records from the caller, and the outcome is either a summary to store or
the reason to store nothing. The runtime owns when a compaction runs and
the client it runs on; L2 owns the range selection and the write.

The gates are mechanical on purpose (Hermes grounds its summaries the same
way): a wrong summary is fixed here or in the template's structure, never
by accreting instructions onto the prompt.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

# The headings ``session.compact_prompt`` asks for; every summary carries all of them.
REQUIRED_HEADINGS: Final[tuple[str, ...]] = (
    "### 相比上次的变化",
    "### 当前在谈什么",
    "### 已确认的决定与用户修正",
    "### 助手提过、尚未确认的建议",
    "### 未决问题",
    "### 已答问题",
    "### 背景事实",
    "### 未完事项 / 下一步",
    "### 未完整读取/未纳入的内容",
)

# A record id is an event uid (32 hex digits) or a Live row id
# (``live:<session>:<source>:<ms>``).
_RECORD_ID: Final[re.Pattern[str]] = re.compile(r"\b[0-9a-f]{32}\b|\blive:[0-9A-Za-z_\-:]+")


def build_compaction_messages(
    *,
    previous_summary: str | None,
    records: Sequence[tuple[str, str, str, str]],
) -> list[dict[str, Any]]:
    """The one user message the summariser sees.

    The previous summary, then the records to fold, each with the id the
    summary must cite.
    """
    lines = [
        "[上一份摘要]",
        previous_summary or "(无, 这是第一份摘要)",
        "",
        "[待纳入的记录, 时间正序; 每行: record_id | 时间 | 说话人 | 原文]",
    ]
    lines.extend(f"{rid} | {ts} | {source} | {text}" for rid, ts, source, text in records)
    return [{"role": "user", "content": "\n".join(lines)}]


def cited_record_ids(summary: str) -> set[str]:
    """Every record id the summary mentions."""
    return set(_RECORD_ID.findall(summary))


def check_summary(
    text: str | None,
    finish_reason: str | None,
    *,
    max_chars: int,
    known_ids: Collection[str],
) -> str | None:
    """Return why the summary must not be stored, or None when it may.

    ``known_ids`` are the ids it may cite (the folded records plus whatever
    the previous summary already cited). The cap is the only size gate: a
    "smaller than what it replaces" rule is implied by the cap for any
    range worth compacting and blocks a small range forever, because the
    template and its record-id citations have a fixed overhead (live
    2026-09-14: 3.1k chars for 417 chars of records).
    """
    body = (text or "").strip()
    if not body:
        return "empty"
    missing = [heading for heading in REQUIRED_HEADINGS if heading not in body]
    unknown = sorted(cited_record_ids(body) - set(known_ids))
    gates: tuple[tuple[bool, str], ...] = (
        (finish_reason in ("length", "max_tokens"), "cut off by the output limit"),
        (len(body) > max_chars, f"{len(body)} chars over the {max_chars} cap"),
        (bool(missing), f"missing headings: {', '.join(missing)}"),
        (bool(unknown), f"cites record ids that exist nowhere: {', '.join(unknown[:5])}"),
    )
    return next((reason for failed, reason in gates if failed), None)


__all__ = [
    "REQUIRED_HEADINGS",
    "build_compaction_messages",
    "check_summary",
    "cited_record_ids",
]
