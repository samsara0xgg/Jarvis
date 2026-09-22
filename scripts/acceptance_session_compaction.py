"""Hermetic acceptance for the conversation store and compaction gates (ADR-0019).

Single running conversation. Runs against a throwaway memory.db, no daemon,
no network, and asserts on the durable observables:

- ``render_context``: every record verbatim before a summary; after one,
  the summary plus only the records after its anchor; the current input
  excluded; the time line separate, with the gap suffix past 30 minutes;
- ``brief_note``: whole-item trimming, oldest records first, then summary
  sections from the bottom, the profile last;
- ``append_summary``: a valid row lands; a stale base is discarded; an
  anchor that does not advance is discarded; the records after the anchor
  are identical before and after VACUUM; a deleted anchor raises;
- ``check_summary``: empty, truncated, missing heading, over the cap and
  unknown id each rejected, a valid summary accepted;
- ``blocked_reason``: Live open, within the idle window, under the ratio
  and nothing older than the window each block; all conditions met passes;
- ``search_records``: ``record_ids`` fetches exactly those rows and a
  summary is never a result.

Run: ``PYTHONPATH=. python scripts/acceptance_session_compaction.py``
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis.decision.compaction import REQUIRED_HEADINGS, check_summary
from jarvis.runtime.session_compaction import blocked_reason
from jarvis.state.memory_db import (
    SessionSettings,
    append_summary,
    brief_note,
    compaction_range,
    iso_seconds,
    open_memory_db,
    render_context,
    search_records,
    verbatim_stats,
)

if TYPE_CHECKING:
    import sqlite3

CHECKS = 0
NOW = datetime(2026, 9, 14, 12, 0, 0).astimezone()
RECORD_TEXT = "这一句是第{i:02d}条记录，" + "内容" * 60  # noqa: RUF001 — Chinese punctuation.


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _ok(cond: bool, what: str) -> None:  # noqa: FBT001 - assertion helper
    global CHECKS  # noqa: PLW0603 - script-level counter
    CHECKS += 1
    if not cond:
        _out(f"FAIL {what}")
        sys.exit(1)
    _out(f"ok   {what}")


def _rid(i: int) -> str:
    return f"{i:032x}"


def _insert(conn: sqlite3.Connection, i: int, days_ago: float) -> None:
    conn.execute(
        "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
        (
            _rid(i),
            iso_seconds(NOW - timedelta(days=days_ago)),
            "allen" if i % 2 else "jarvis",
            RECORD_TEXT.format(i=i),
        ),
    )


def _summary(*cited: str) -> str:
    body = dict.fromkeys(REQUIRED_HEADINGS, "无")
    body["### 已确认的决定与用户修正"] = "\n".join(
        f"「明天去多伦多」[allen, record_id={record_id}]" for record_id in cited
    )
    lines = ["## 对话摘要（2026-08-25 至 2026-09-07）"]  # noqa: RUF001 — Chinese punctuation.
    for heading in REQUIRED_HEADINGS:
        lines.extend([heading, body[heading]])
    return "\n".join(lines)


def _flat(history: tuple[dict[str, str], ...]) -> str:
    return "\n".join(turn["content"] for turn in history)


def _check_render_before_summary(db: Path) -> None:
    ctx = render_context(db, exclude_id=_rid(10), now=NOW)
    _ok(ctx.profile == "", "an empty profile renders no block")
    _ok(bool(ctx.history) and ctx.history[0]["content"].startswith("[2026-"),
        "history leads with the records")
    _ok(
        all(RECORD_TEXT.format(i=i) in _flat(ctx.history) for i in range(1, 10)),
        "every earlier record is verbatim before a summary",
    )
    _ok(RECORD_TEXT.format(i=10) not in _flat(ctx.history), "the current input is excluded")
    _ok("[对话摘要" not in _flat(ctx.history), "no summary block before a compaction")
    _ok(ctx.now.startswith("时间：2026-09-14T12:00-"), "the time line is separate")  # noqa: RUF001 — Chinese punctuation is intentional.
    _ok("距上次交流 1 天" in ctx.now, "the gap suffix appears past 30 minutes")
    stats = verbatim_stats(db)
    _ok(stats.chars == sum(len(RECORD_TEXT.format(i=i)) for i in range(1, 11)), "verbatim chars")
    _ok(stats.oldest_ts == iso_seconds(NOW - timedelta(days=20)), "oldest verbatim ts")
    span = compaction_range(db, window_days=7, now=NOW)
    assert span is not None  # noqa: S101 - acceptance script
    _ok(
        span.base_id is None and [r[0] for r in span.records] == [_rid(i) for i in range(1, 6)],
        "compaction range = records older than 7 days, oldest first",
    )


def _check_gates() -> None:
    valid = _summary(_rid(1), _rid(3))
    known = {_rid(i) for i in range(1, 6)}
    _ok(check_summary("", "stop", max_chars=3000, known_ids=known) == "empty",
        "empty summary rejected")
    _ok(
        "cut off" in str(check_summary(valid, "length", max_chars=3000, known_ids=known)),
        "truncated summary rejected",
    )
    missing = valid.replace("### 未决问题", "### 问题")
    _ok(
        "missing headings" in str(check_summary(missing, "stop", max_chars=3000, known_ids=known)),
        "summary missing a heading rejected",
    )
    _ok(
        "over the" in str(check_summary(valid, "stop", max_chars=10, known_ids=known)),
        "summary over the cap rejected",
    )
    unknown = _summary(_rid(1), "0" * 32)
    _ok(
        "exist nowhere" in str(check_summary(unknown, "stop", max_chars=3000, known_ids=known)),
        "summary citing an unknown record id rejected",
    )
    _ok(check_summary(valid, "stop", max_chars=3000, known_ids=known) is None,
        "valid summary accepted")


def _check_summary_rows(db: Path) -> None:
    first = _summary(_rid(1), _rid(3))
    landed = append_summary(db, base_id=None, upto_record_id=_rid(5), summary=first,
                            model="test", input_chars=1000, output_chars=len(first))
    _ok(landed, "first summary lands")
    ctx = render_context(db, exclude_id=_rid(10), now=NOW)
    anchor_ts = iso_seconds(NOW - timedelta(days=12))
    _ok(f"[对话摘要 · 覆盖到 {anchor_ts}" in _flat(ctx.history) and first in _flat(ctx.history),
        "render shows the summary with its anchor time")
    _ok(
        all(RECORD_TEXT.format(i=i) in _flat(ctx.history) for i in range(6, 10))
        and not any(RECORD_TEXT.format(i=i) in _flat(ctx.history) for i in range(1, 6)),
        "render shows only the records after the anchor verbatim",
    )
    stale = append_summary(db, base_id=None, upto_record_id=_rid(7), summary=first,
                           model="test", input_chars=1, output_chars=1)
    _ok(not stale, "a summary against a stale base is discarded")
    backwards = append_summary(db, base_id=f"summary:{_rid(5)}", upto_record_id=_rid(3),
                               summary=first, model="test", input_chars=1, output_chars=1)
    _ok(not backwards, "a summary whose anchor does not advance is discarded")
    second = _summary(_rid(1), _rid(6))
    _ok(
        append_summary(db, base_id=f"summary:{_rid(5)}", upto_record_id=_rid(7), summary=second,
                       model="test", input_chars=1000, output_chars=len(second)),
        "a summary extending the current one lands",
    )
    before = _flat(render_context(db, exclude_id=_rid(10), now=NOW).history)
    with closing(open_memory_db(db)) as conn:
        conn.execute("VACUUM")
    after = _flat(render_context(db, exclude_id=_rid(10), now=NOW).history)
    _ok(before == after and RECORD_TEXT.format(i=8) in after and second in after,
        "records after the anchor identical before and after VACUUM")
    with closing(open_memory_db(db)) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    _ok(rows == 2, "exactly two summary rows")  # noqa: PLR2004 - two writes landed


def _check_brief(db: Path) -> None:
    with closing(open_memory_db(db)) as conn, conn:
        conn.execute("INSERT INTO profile (id, ts, text) VALUES ('p1', ?, '住多伦多')",
                     (iso_seconds(NOW),))
    full = brief_note(db, max_chars=100_000, now=NOW)
    _ok(full.startswith("[关于 Allen]\n- 住多伦多\n时间："), "brief leads with profile and time")  # noqa: RUF001 — Chinese punctuation is intentional.
    _ok("原话细节请向后台查询" in full and "search_records" not in full,
        "brief tells Live to ask the backend, not to call a tool")
    _ok(
        all(RECORD_TEXT.format(i=i) in full for i in range(8, 11)),
        "unbudgeted brief has every record",
    )
    two_records = len(full) - len(RECORD_TEXT.format(i=8)) - 40
    trimmed = brief_note(db, max_chars=two_records, now=NOW)
    _ok(len(trimmed) <= two_records, "brief within budget")
    _ok(RECORD_TEXT.format(i=8) not in trimmed and RECORD_TEXT.format(i=10) in trimmed,
        "brief drops the oldest record first, whole")
    _ok(REQUIRED_HEADINGS[-1] in trimmed, "summary intact while records can still go")
    record_chars = sum(len(line) + 1 for line in trimmed.splitlines() if line.startswith("[2026"))
    last_section = f"{REQUIRED_HEADINGS[-1]}\n无"
    tight = brief_note(db, max_chars=len(trimmed) - record_chars - len(last_section) - 1, now=NOW)
    _ok(
        REQUIRED_HEADINGS[0] in tight and REQUIRED_HEADINGS[-1] not in tight
        and "- 住多伦多" in tight,
        "then summary sections go from the bottom, profile last",
    )


def _check_search(db: Path) -> None:
    rows = search_records(db, record_ids=[_rid(2), _rid(9)])
    _ok(
        [r[0] for r in rows] == [_rid(9), _rid(2)],
        "record_ids fetch exactly those rows, newest first",
    )
    _ok(rows[0][3] == RECORD_TEXT.format(i=9), "rows carry the full text")
    _ok(search_records(db, keyword="明天去多伦多") == [], "a summary is never a search result")


def _check_blocked(db: Path) -> None:
    # After the two summaries the verbatim records are 3, 1 and 0 days old,
    # so a 2-day window has one record older than it and a 30-day window none.
    settings = SessionSettings(idle_before_compact_s=3600, compact_at_context_ratio=0.4,
                               verbatim_window_days=2, compact_prompt="x")
    stats = verbatim_stats(db)
    newest = datetime.fromisoformat(stats.newest_ts or "")

    def _reason(
        *, now: datetime, live: bool = False, context_length: int | None = 100,
    ) -> str | None:
        return blocked_reason(stats, now=now, settings=settings, live_open=live,
                              context_length=context_length)

    _ok("Live" in str(_reason(now=newest + timedelta(hours=2), live=True)), "Live open blocks")
    _ok("idle" in str(_reason(now=newest + timedelta(minutes=10))), "within the idle window blocks")
    _ok("tokens <=" in str(_reason(now=newest + timedelta(hours=2), context_length=10**9)),
        "under the ratio blocks")
    _ok("context_length" in str(_reason(now=newest + timedelta(hours=2), context_length=None)),
        "a preset without context_length blocks")
    _ok(
        _reason(now=newest + timedelta(hours=2)) is None,
        "idle, over the ratio, with old records: due",
    )
    young = SessionSettings(idle_before_compact_s=3600, compact_at_context_ratio=0.4,
                            verbatim_window_days=30, compact_prompt="x")
    _ok(
        "older" in str(blocked_reason(stats, now=newest + timedelta(hours=2), settings=young,
                                      live_open=False, context_length=100)),
        "nothing older than the window blocks",
    )


def _check_missing_anchor(db: Path) -> None:
    with closing(open_memory_db(db)) as conn, conn:
        conn.execute("DELETE FROM records WHERE id = ?", (_rid(7),))
    try:
        render_context(db, exclude_id="", now=NOW)
    except LookupError as exc:
        _ok(_rid(7) in str(exc), "a deleted anchor raises an explicit error")
    else:
        _ok(False, "a deleted anchor raises an explicit error")  # noqa: FBT003 - assertion helper


def main() -> None:
    """Run every check against a throwaway memory.db."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "memory.db"
        with closing(open_memory_db(db)) as conn, conn:
            for i, days_ago in enumerate([20, 18, 16, 14, 12, 6, 4, 3, 1, 0], start=1):
                _insert(conn, i, days_ago)
        _check_render_before_summary(db)
        _check_gates()
        _check_summary_rows(db)
        _check_brief(db)
        _check_search(db)
        _check_blocked(db)
        _check_missing_anchor(db)
    _out(f"ACCEPTANCE OK {CHECKS}/{CHECKS}")


if __name__ == "__main__":
    main()
