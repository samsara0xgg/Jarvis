"""ADR 0200 — the ledger: his days, weeks and job hunt computed from his own data.

Real memory.db, Event Log and a TimeSink-shaped sqlite built in ``tmp_path``; the day-prose
call is the only fake (a client that answers from a script and records what it was asked).
Each check feeds known rows and asserts the text the program computes from them.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import httpx
import openai

from jarvis.decision.day_prose import build_day_prose_messages, check_day_prose
from jarvis.decision.llm import ChatResult
from jarvis.runtime.ledger import LedgerContext, LedgerSettings, ProseSettings, run_day_prose
from jarvis.state import core_memory
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.job_ledger import record_seen, upsert_mail
from jarvis.state.ledger import (
    LedgerSources,
    notes_stamp,
    since_text,
    standing_text,
    today_text,
)
from jarvis.state.memory_db import (
    open_memory_db,
    prose_days,
    render_context,
)

if TYPE_CHECKING:
    from pathlib import Path

ZONE = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 8, 15, 0, tzinfo=ZONE)  # a Thursday
MIDNIGHT = datetime(2026, 10, 8, tzinfo=ZONE)

_SPAN = (
    'CREATE TABLE "span" ("id" INTEGER PRIMARY KEY AUTOINCREMENT, "start" DATETIME NOT NULL, '
    '"end" DATETIME NOT NULL, "appBundleID" TEXT NOT NULL, "appName" TEXT NOT NULL, "title" TEXT, '
    '"url" TEXT, "domain" TEXT, "document" TEXT, "deviceID" TEXT, "originID" INTEGER, '
    '"remoteSeq" TEXT, "keySeconds" INTEGER NOT NULL DEFAULT 0)'
)
_PROJECT = 'CREATE TABLE "project" ("id" INTEGER PRIMARY KEY, "name" TEXT NOT NULL)'
_VERDICT = (
    'CREATE TABLE "jevProjectVerdict" ("appBundleID" TEXT, "domain" TEXT, "title" TEXT, '
    '"document" TEXT, "projectID" INTEGER)'
)
GHOSTTY = "com.mitchellh.ghostty"
CHROME = "com.google.Chrome"


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE)


def _utc(moment: datetime) -> str:
    """GRDB's text form: UTC, a space, milliseconds, no suffix."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.000")


def _timesink(path: Path) -> None:
    spans = [  # (start, end, bundle, app) over three days
        (_at(6, 9), _at(6, 10, 30), GHOSTTY, "Ghostty"),
        (_at(6, 9, 30), _at(6, 10), CHROME, "Chrome"),  # inside the first: counted once
        (_at(7, 13), _at(7, 13, 45), CHROME, "Chrome"),
        (_at(8, 10), _at(8, 11), GHOSTTY, "Ghostty"),
    ]
    with closing(sqlite3.connect(path)) as conn, conn:
        for ddl in (_SPAN, _PROJECT, _VERDICT):
            conn.execute(ddl)
        conn.execute("INSERT INTO project VALUES (1, 'jarvis')")
        conn.execute(
            "INSERT INTO jevProjectVerdict VALUES (?, '', 'repl', '', 1)", (GHOSTTY,),
        )
        conn.executemany(
            "INSERT INTO span (start, end, appBundleID, appName, title, document) "
            "VALUES (?, ?, ?, ?, 'repl', '')",
            [(_utc(a), _utc(b), bundle, app) for a, b, bundle, app in spans],
        )


def _mail(path: Path, mid: str, when: datetime, **fields: str | None) -> None:
    mail = {
        "message_id": mid, "received_at": when.isoformat(), "sender_name": "Pat Lee",
        "sender_domain": "x.example", "subject": "s", "confidence": 0.9, "extracted_by": "test",
        **fields,
    }
    upsert_mail(path, mail, datetime.now(UTC))


def _world(tmp_path: Path) -> LedgerSources:
    memory, log, ts = tmp_path / "memory.db", tmp_path / "events.db", tmp_path / "ts.db"
    _timesink(ts)
    with closing(open_memory_db(memory)) as conn, conn:
        conn.execute(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, 'allen', 'hi')",
            ("r1", _at(8, 14).isoformat()),
        )
        conn.execute(
            "INSERT INTO day_summaries (id, day, ts, summary, model, record_count, input_chars, "
            "output_chars) VALUES ('s1', '2026-10-07', ?, ?, 'm', 1, 1, 1)",
            (
                _at(8, 5).isoformat(),  # written after midnight
                "## 2026-10-07\n### Topics\n- resume polish [record_id=r1]\n"
                "### Decisions and facts the user stated\n- none\n### Unfinished\n- none",
            ),
        )
        conn.execute(
            "INSERT INTO day_prose (id, day, ts, text, model, input_chars, output_chars) "
            "VALUES ('p1', '2026-10-06', ?, 'He shipped the ledger and went to bed.', 'm', 1, 1)",
            (_at(7, 5).isoformat(),),
        )
    _mail(memory, "m1", _at(5, 9), kind="receipt", company="Gamma Inc", role="Co-op")
    _mail(memory, "m2", _at(7, 14), kind="receipt", company="Acme Robotics", role="Co-op Dev")
    _mail(
        memory, "m3", _at(6, 12), kind="interview", company="Delta Corp", role="Intern",
        event_at=_at(9, 14).isoformat(), event_text="Friday 2:00 PM - 2:30 PM PT",
    )
    _mail(memory, "m4", _at(8, 11, 30), kind="interview", company="Beta Labs", role="Co-op")
    record_seen(
        memory, "seen1", "not_job", datetime.now(UTC), audit={"received_at": _at(8, 9).isoformat()},
    )
    conn = open_event_log(log)
    emit_event(
        conn, type="reminder.scheduled", ts_epoch_ms=int(_at(8, 8).timestamp() * 1000),
        payload={
            "reminder_id": "reminder-1", "due_at_epoch_ms": int(_at(9, 9).timestamp() * 1000),
            "due_at_local": "2026-10-09T09:00", "text": "call the dentist", "action_id": "a1",
        },
    )
    conn.close()
    return LedgerSources(memory, log, ts, ZONE)


def test_standing_text_computes_days_job_hunt_and_notes(tmp_path: Path) -> None:
    """Blocks B-D: hand-computed day lines, week totals, job hunt and the notes cut-off."""
    src = _world(tmp_path)
    text = standing_text(src, MIDNIGHT)
    # Tue 10-06: Ghostty 09:00-10:30 with Chrome inside it counts once.
    assert "2026-10-06 Tue" in text
    assert "Computer: started 09:00, stopped 10:30, active 1h30m" in text
    assert "Projects (TimeSink): jarvis 1h30m" in text
    # Wed 10-07: 45 minutes, and its application confirmation.
    assert "Computer: started 13:00, stopped 13:45, active 0h45m" in text
    assert "Job events: application confirmed (Acme Robotics, Co-op Dev, from Pat Lee)" in text
    # Job hunt as of midnight: today's mail (Beta Labs) is not in it, the dated invitation is.
    assert "Application confirmed (2): Acme Robotics (Pat Lee), Gamma Inc (Pat Lee)" in text
    assert "Interview mail (1): Delta Corp (Pat Lee)" in text
    assert (
        "Delta Corp (Intern): latest dated invitation Fri 2026-10-09 at 14:00-14:30 Pacific" in text
    )
    assert "[upcoming;" in text
    assert "Beta Labs" not in text
    # This week so far: Mon 10-05 to Wed 10-07 is 1h30m + 0h45m.
    assert "[This week so far · Mon 10-05 to Wed 10-07; today is in Today so far]" in text
    assert "active 2h15m; projects jarvis 1h30m" in text
    # The model-made line shows; a summary written after midnight does not, until asked.
    assert "The day: He shipped the ledger and went to bed." in text
    assert "resume polish" not in text
    later = standing_text(src, MIDNIGHT, notes_as_of=NOW)
    assert "Talked about: resume polish" in later
    assert "The day: He shipped the ledger and went to bed." in later


def test_notes_stamp_follows_the_newest_note(tmp_path: Path) -> None:
    """The cache key is the newest note time of each table, empty without them."""
    src = _world(tmp_path)
    assert notes_stamp(src) == (_at(8, 5).isoformat(), _at(7, 5).isoformat())
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()  # a store with neither table
    assert notes_stamp(LedgerSources(bare, src.event_log, None, ZONE)) == ("", "")


def test_today_and_since_text(tmp_path: Path) -> None:
    """Blocks E-F: today's job mail and reminders, and the nothing-new case."""
    src = _world(tmp_path)
    today = today_text(src, NOW)
    assert "[Today so far · Thu 2026-10-08, now 15:00]" in today
    assert "started 10:00, still active, active today 1h00m" in today
    assert "Job mail today: 1, 11:30 interview mail (Beta Labs, from Pat Lee)" in today
    assert "Mail screened today: 1" in today
    assert "Pending reminders (1): Fri 10-09 09:00 call the dentist" in today
    # He last talked at 14:00, after the 11:30 mail: nothing is new.
    assert "Nothing new: no mail, reminders or commits." in since_text(src, NOW, _at(8, 14))
    assert since_text(src, NOW, None) == ""
    earlier = since_text(src, NOW, _at(8, 10))
    assert "Mail 10-08 11:30: interview mail (Beta Labs, from Pat Lee)" in earlier
    assert "Nothing new" not in earlier


def test_render_context_places_the_blocks_and_dates_the_core_memory(tmp_path: Path) -> None:
    """Standing text follows the core memory, per-turn text the time line; items are dated."""
    path = tmp_path / "memory.db"
    said = datetime(2026, 10, 3, 12).astimezone()  # local noon, so its local date is the 3rd
    doc = core_memory.empty_doc()
    doc["偏好"] = [
        {"id": "a1", "text": "likes tea", "sources": ["r1"], "since": "2026-10-04"},
        {"id": "a2", "text": "lives in Vancouver", "sources": [], "since": "2026-09-20"},
    ]
    with closing(open_memory_db(path)) as conn:
        conn.execute(
            "INSERT INTO records (id, ts, source, text) VALUES ('r1', ?, 'allen', 'tea')",
            (said.isoformat(),),
        )
        with core_memory.write_transaction(conn):
            core_memory.append_version(
                conn, base=None, doc=doc, origin="test", upto_day=None, changes=[],
                now=said.isoformat(),
            )
        conn.commit()
    asked: list[tuple[datetime, datetime | None]] = []

    def ledger(now: datetime, last: datetime | None) -> tuple[str, str]:
        asked.append((now, last))
        return "STANDING-BLOCKS", "PER-TURN-BLOCKS"

    context = render_context(path, exclude_id="", now=NOW, ledger=ledger)
    lines = context.profile.splitlines()
    assert lines[0] == "[About the user]"
    assert "- (2026-10-03, said) likes tea" in lines
    assert "- (2026-09-20, set) lives in Vancouver" in lines
    assert context.profile.endswith("\n\nSTANDING-BLOCKS")
    assert context.now.startswith("Time: 2026-10-08T15:00")
    assert context.now.endswith("\n\nPER-TURN-BLOCKS")
    assert asked == [(NOW, said)]
    assert render_context(path, exclude_id="", now=NOW).now.startswith("Time:")


def test_ledger_context_caches_per_day_and_never_breaks_a_turn(tmp_path: Path) -> None:
    """One text per day and notes, rebuilt on a new note; a failing source gives empty blocks."""
    src = _world(tmp_path)
    context = LedgerContext(src, LedgerSettings())
    standing, per_turn = context(NOW, _at(8, 14))
    assert "[Recent days" in standing
    assert "[Today so far" in per_turn
    assert "Nothing new" in per_turn
    assert context(NOW + timedelta(hours=1), None)[0] is standing  # same day, same notes: cached
    assert "[Since you last talked" not in context(NOW, None)[1]
    # A new note changes the stamp, so the text is rebuilt.
    with closing(open_memory_db(src.memory_db)) as conn, conn:
        conn.execute(
            "INSERT INTO day_prose (id, day, ts, text, model, input_chars, output_chars) "
            "VALUES ('p2', '2026-10-07', ?, 'A quiet Wednesday.', 'm', 1, 1)",
            (_at(8, 6).isoformat(),),
        )
    assert "The day: A quiet Wednesday." in context(NOW, None)[0]
    # A broken source costs the turn its blocks and nothing else.
    broken = LedgerContext(
        LedgerSources(tmp_path / "missing" / "memory.db", src.event_log, None, ZONE),
        LedgerSettings(),
    )
    assert broken(NOW, _at(8, 14)) == ("", "")


def test_settings_from_config() -> None:
    """The ``ledger:`` block: off when absent or not enabled, defaults otherwise."""
    assert LedgerSettings.from_config(None) is None
    assert LedgerSettings.from_config({}) is None
    assert LedgerSettings.from_config({"enabled": False, "prose": {"prompt": "p"}}) is None
    assert LedgerSettings.from_config({"enabled": True}) == LedgerSettings(7, 7, None)
    full = LedgerSettings.from_config(
        {"enabled": True, "full_days": 3, "prose": {"prompt": " write ", "days": 2}},
    )
    assert full == LedgerSettings(3, 7, ProseSettings("gpt6-luna-flex", "write", 2, 800))
    no_prompt = {"enabled": True, "prose": {"preset": "x"}}
    assert LedgerSettings.from_config(no_prompt) == LedgerSettings()


class _Writer:
    """Stands in for the prose preset's client: answers from a script, records what it is asked."""

    provider = "openai"
    model = "fake-prose"

    def __init__(self, script: list[object]) -> None:
        self.script = script
        self.asked: list[str] = []

    def chat(self, *, messages: list[dict[str, Any]], **_kwargs: object) -> ChatResult:
        self.asked.append(str(messages[0]["content"]))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return ChatResult(
            text=str(step), tool_calls=(), finish_reason="stop", input_tokens=1, output_tokens=1,
            raw={}, model_used=self.model,
        )


def _run(
    src: LedgerSources, writer: _Writer, days: int, sleeps: list[float],
) -> dict[str, str]:
    return run_day_prose(
        src, ProseSettings("p", "write the day", days, 200), writer,  # type: ignore[arg-type]
        event_log_path=src.event_log, pricing_table={}, today=date(2026, 10, 8),
        sleep=sleeps.append,
    )


def _rows(src: LedgerSources) -> dict[str, list[str]]:
    with closing(sqlite3.connect(src.memory_db)) as conn:
        out: dict[str, list[str]] = {}
        for day, text in conn.execute("SELECT day, text FROM day_prose ORDER BY rowid"):
            out.setdefault(day, []).append(text)
        return out


def test_run_day_prose_retries_skips_and_stores_one_row_per_day(tmp_path: Path) -> None:
    """A timeout is retried; days with a line or no activity are skipped."""
    src = _world(tmp_path)  # 10-06 already has a line; 10-04 has no activity at all
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "http://invalid.test"))
    writer = _Writer([timeout, "Gamma wrote back. Not much else.", "A short Wednesday."])
    sleeps: list[float] = []
    outcomes = _run(src, writer, 4, sleeps)
    assert outcomes == {
        "2026-10-04": "no activity",
        "2026-10-05": "landed (32 chars)",
        "2026-10-07": "landed (18 chars)",
    }
    assert _rows(src) == {
        "2026-10-05": ["Gamma wrote back. Not much else."],
        "2026-10-06": ["He shipped the ledger and went to bed."],
        "2026-10-07": ["A short Wednesday."],
    }
    assert sleeps == [2.0]  # the timeout waited once, then the same day went through
    assert len(writer.asked) == 3
    wednesday = writer.asked[2]
    assert "NUMBERS (computed by the program):" in wednesday
    assert "active 0h45m" in wednesday
    assert "DAY SUMMARY (conversation with the assistant):\n## 2026-10-07" in wednesday
    assert "DAILY WORK REPORT (excerpt):\n(none yet)" in wednesday
    assert prose_days(src.memory_db) == {"2026-10-05", "2026-10-06", "2026-10-07"}
    assert _run(src, _Writer([]), 4, []) == {"2026-10-04": "no activity"}  # nothing left to write


def test_run_day_prose_stores_nothing_for_a_rejected_or_failed_day(tmp_path: Path) -> None:
    """Twice rejected or a hard error stores nothing and the next day still runs."""
    src = _world(tmp_path)
    writer = _Writer(["## A heading", "- a bullet", RuntimeError("boom"), "Wednesday was quiet."])
    outcomes = _run(src, writer, 3, [])  # 10-05 rejected twice, 10-07 fails, but 10-06 has a line
    assert outcomes["2026-10-05"] == "rejected (contains markdown headings or bullets)"
    assert outcomes["2026-10-07"] == "failed"
    assert _rows(src) == {"2026-10-06": ["He shipped the ledger and went to bed."]}
    again = _run(src, _Writer(["Gamma wrote back.", "Wednesday was quiet."]), 3, [])
    assert [again[k].split(" ")[0] for k in sorted(again)] == ["landed", "landed"]


def test_day_prose_input_and_gate() -> None:
    """The three input sections, the report trim and each reason the gate rejects."""
    long_report = "r" * 5000
    content = build_day_prose_messages("2026-10-07", "NUM", None, long_report)[0]["content"]
    assert content.index("NUMBERS (computed by the program):\nNUM") < content.index(
        "DAY SUMMARY (conversation with the assistant):\n(none yet)",
    )
    assert content.endswith("DAILY WORK REPORT (excerpt):\n" + "r" * 2600)
    no_report = build_day_prose_messages("d", "n", "s", None)[0]["content"]
    assert no_report.endswith("DAILY WORK REPORT (excerpt):\n(none yet)")
    assert check_day_prose("Two plain sentences. Here.", "stop", 100) is None
    assert check_day_prose("  ", "stop", 100) == "empty"
    assert check_day_prose("cut", "length", 100) == "cut off by the output limit"
    assert check_day_prose("x" * 101, "stop", 100) == "101 chars over the 100 cap"
    assert check_day_prose("Fine.\n- a bullet", "stop", 100) is not None
    assert check_day_prose("# Heading", "stop", 100) is not None

