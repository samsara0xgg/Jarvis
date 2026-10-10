"""ADR 0201 — the ledger: his days, weeks and job hunt computed from his own data.

Real memory.db, Event Log and a TimeSink-shaped sqlite built in ``tmp_path``; the day-prose
call is the only fake (a client that answers from a script and records what it was asked).
Each check feeds known rows and asserts the text the program computes from them.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import httpx
import openai
import pytest

from jarvis.decision.day_prose import build_day_prose_messages, check_day_prose
from jarvis.decision.llm import ChatResult
from jarvis.execution.job_ledger_tool import build_record_application_tool
from jarvis.execution.tools import ToolError
from jarvis.runtime import terminal as terminal_runtime
from jarvis.runtime.ledger import LedgerContext, LedgerSettings, ProseSettings, run_day_prose
from jarvis.shared.device_link import DeviceCallError
from jarvis.state import core_memory
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.job_ledger import add_application, list_applications, record_seen, upsert_mail
from jarvis.state.ledger import (
    LedgerSources,
    TerminalScreen,
    job_hunt_text,
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
    hunt = job_hunt_text(src, MIDNIGHT)
    assert "Application confirmed (2): Acme Robotics (Pat Lee), Gamma Inc (Pat Lee)" in hunt
    assert "Interview mail (1): Delta Corp (Pat Lee)" in hunt
    assert (
        "Delta Corp (Intern): latest dated invitation Fri 2026-10-09 at 14:00-14:30 Pacific" in hunt
    )
    assert "[upcoming;" in hunt
    assert "Beta Labs" not in hunt
    # As of now it counts today's mail too; the standing text carries no job hunt at all.
    now_hunt = job_hunt_text(src, NOW)
    assert "Interview mail (2): Beta Labs (Pat Lee), Delta Corp (Pat Lee)" in now_hunt
    assert "[Job hunt" not in text
    # This week so far: Mon 10-05 to Wed 10-07 is 1h30m + 0h45m.
    assert "[This week so far · Mon 10-05 to Wed 10-07; today is in Today so far]" in text
    assert "active 2h15m; projects jarvis 1h30m" in text
    # The model-made line shows; a summary written after midnight does not, until asked.
    assert "The day: He shipped the ledger and went to bed." in text
    assert "resume polish" not in text
    later = standing_text(src, MIDNIGHT, notes_as_of=NOW)
    assert "Talked about: resume polish" in later
    assert "The day: He shipped the ledger and went to bed." in later


def test_a_day_that_ran_past_midnight_stops_at_its_real_end_from_five(tmp_path: Path) -> None:
    """At midnight Wednesday stops at midnight; from 05:00 on, at 00:40 when work went on."""
    src = _world(tmp_path)
    assert src.timesink is not None
    with closing(sqlite3.connect(src.timesink)) as conn, conn:
        conn.execute(
            "INSERT INTO span (start, end, appBundleID, appName, title, document) "
            "VALUES (?, ?, ?, 'Chrome', 'repl', '')",
            (_utc(_at(7, 23, 30)), _utc(_at(8, 0, 40)), CHROME),
        )
    at_midnight = standing_text(src, MIDNIGHT)
    assert "Computer: started 13:00, stopped 00:00 (after midnight), active 1h15m" in at_midnight
    at_five = standing_text(src, MIDNIGHT, day_end_as_of=_at(8, 5))
    assert "Computer: started 13:00, stopped 00:40 (after midnight), active 1h15m" in at_five


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
    # Wednesday's 13:00-13:45 was the last activity before today's 10:00 start.
    assert "Before that the computer was idle from Wed 13:45, 20h15m" in today
    # Mon 10-05 to now is 3h15m; last week to Thu 10-01 15:00 has no spans.
    assert (
        "This week including today: active 3h15m (the same stretch of last week, to "
        "Thu 10-01 15:00: 0h00m, +3.2h)"
    ) in today
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
    assert per_turn.startswith("[Job hunt as of now")
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


def test_ledger_warm_leaves_the_first_turn_a_cached_standing_text(tmp_path: Path) -> None:
    """The boot warm-up builds the standing text, so the first turn after a restart reuses it."""
    context = LedgerContext(_world(tmp_path), LedgerSettings())
    context.warm(lambda: NOW)
    deadline = time.monotonic() + 10
    while context._cached is None and time.monotonic() < deadline:  # noqa: SLF001
        time.sleep(0.01)
    assert context._cached is not None  # noqa: SLF001
    assert context(NOW, None)[0] is context._cached[1]  # noqa: SLF001


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



def _page(ts: Path, start: datetime, title: str, url: str | None, domain: str) -> None:
    with closing(sqlite3.connect(ts)) as conn, conn:
        conn.execute(
            "INSERT INTO span (start, end, appBundleID, appName, title, url, domain, document) "
            "VALUES (?, ?, ?, 'Chrome', ?, ?, ?, '')",
            (_utc(start), _utc(start + timedelta(minutes=2)), CHROME, title, url, domain),
        )


def test_applications_submitted_counts_each_submission_once_across_three_sources(
    tmp_path: Path,
) -> None:
    """Block D: mail receipts, success pages seen on screen and what he told her, deduplicated."""
    src = _world(tmp_path)  # two receipts: Gamma Inc 10-05 and Acme Robotics 10-07
    assert src.timesink is not None
    ts, memory = src.timesink, src.memory_db
    greenhouse = "job-boards.greenhouse.io"
    # A Greenhouse form, then its confirmation page, then the receipt 3 minutes later: one.
    form = f"https://{greenhouse}/zeta/jobs/55"
    _page(ts, _at(8, 9, 50), "Job Application for Backend Co-op at Zeta Corp", form, greenhouse)
    _page(ts, _at(8, 10), "Thank you for applying", form + "/confirmation", greenhouse)
    _mail(
        memory, "z1", _at(8, 10, 3), kind="receipt", company="Zeta Corp", role="Backend Co-op",
        subject="Thanks Zeta",
    )
    # RBC: a role page, the applythankyou page, and the same page again hours later; no mail: one.
    rbc = "jobs.rbc.com"
    role_page = "https://jobs.rbc.com/ca/en/job/R-1"
    _page(ts, _at(6, 10, 50), "Winter 2027 Co-op Student | RBC Careers", role_page, rbc)
    thanks = (
        "https://jobs.rbc.com/ca/en/applythankyou?jobId=R-1"
        "&jobTitle=Winter%202027%20Co-op%20Student#top"
    )
    _page(ts, _at(6, 11), "Application Submitted", thanks, rbc)
    _page(ts, _at(6, 18), "Application Submitted", thanks, rbc)
    # Workday: no jobTitle, so the role is the last real page title before it (not "Apply").
    clio = "clio.wd3.myworkdayjobs.com"
    flow = f"https://{clio}/en-US/Clio/job/Vancouver/Data-Co-op_R1"
    _page(ts, _at(7, 15), "Senior Data Co-op - Clio | Workday", flow, clio)
    _page(ts, _at(7, 15, 5), "Apply", flow + "/apply", clio)
    _page(ts, _at(7, 15, 10), "Application Received", flow + "/apply/success", clio)
    # A Gmail tab whose title reads like a success page is not a submission.
    inbox = "https://mail.google.com/mail/u/0/#inbox"
    _page(ts, _at(8, 12), "Thank you for applying to Acme - Gmail", inbox, "mail.google.com")
    # Moment Energy: its ATS confirming the same role 3 minutes later is the same submission, a
    # mail 70 minutes later is a second one.
    for mid, minute, sender in (("e1", 0, "Moment Energy"), ("e2", 3, "Workable"),
                                ("e3", 70, "Moment Energy")):
        _mail(
            memory, mid, _at(7, 10) + timedelta(minutes=minute), kind="receipt",
            company=sender, role="Co-op",
            subject="Thank you for applying to Moment Energy",
        )
    # He told her about Zeta (already seen) and Orbit; he added Hand Made on the Jobs page.
    (record,) = build_record_application_tool(
        lambda fields: add_application(memory, NOW, **fields),
    )
    for fields in (
        {"company": "zeta corp", "applied_at": "2026-10-08"},
        {"company": "Orbit Co", "role": "Data Co-op", "applied_at": "2026-10-08"},
    ):
        assert record.handler(fields, None)["recorded"] is True  # type: ignore[arg-type]
    add_application(memory, NOW, company="Hand Made Ltd", applied_at="2026-10-02")
    with pytest.raises(ToolError):
        record.handler({"company": "Bad", "applied_at": "yesterday"}, None)  # type: ignore[arg-type]
    assert {"Orbit Co", "zeta corp", "Hand Made Ltd"} <= {
        a["company"] for a in list_applications(memory, NOW)
    }
    with closing(sqlite3.connect(memory)) as conn:
        stored = conn.execute("SELECT company, source FROM job_application").fetchall()
    assert sorted(stored) == [
        ("Hand Made Ltd", "manual"), ("Orbit Co", "said"), ("zeta corp", "said"),
    ]

    hunt = job_hunt_text(src, NOW)
    line = next(x for x in hunt.splitlines() if x.startswith("  Applications submitted"))
    assert line.startswith(
        "  Applications submitted: at least 9 (5 by confirmation mail, 2 by a submission page "
        "seen on screen with no confirmation mail, 2 you told me or added).",
    )
    assert "By day, last 7 days: Thu 10-08 2, Wed 10-07 4, Tue 10-06 1, Mon 10-05 1, " in line
    assert "Sun 10-04 0, Sat 10-03 0, Fri 10-02 1." in line
    assert (
        "Seen on screen only: Clio (Senior Data Co-op - Clio, 10-07); "
        "Rbc (Winter 2027 Co-op Student, 10-06)."
    ) in line
    assert "Told me or added: Orbit Co (Data Co-op, 10-08); Hand Made Ltd (10-02)." in line
    assert "portal application with no mail and no such page is not visible" in hunt
    assert "Application confirmed (" in hunt  # the existing lines stay


def _terminal_link(store: Path | None, asked: list[str], *, up: list[bool]) -> Any:  # noqa: ANN401
    """A brain's device link whose far end is the real terminal reader on ``store``."""

    def link(op: str, arguments: Any, target: str | None) -> dict[str, Any]:  # noqa: ANN401
        del target
        asked.append(str(arguments["fn"]))
        if not up[0]:
            msg = "the Mac is not connected right now, so timesink_read cannot run"
            raise DeviceCallError(msg)
        reply = terminal_runtime._read_device(op, arguments, store, ())  # noqa: SLF001
        if not reply["ok"]:
            raise DeviceCallError(reply["message"], code=reply["code"])
        return dict(reply["output"])

    return link


def test_a_brains_ledger_reads_its_terminals_timesink_and_equals_one_machines(
    tmp_path: Path,
) -> None:
    """No local TimeSink path, a terminal behind the link: every TimeSink part is the same text."""
    one = _world(tmp_path)
    assert one.timesink is not None
    with closing(sqlite3.connect(one.timesink)) as conn, conn:
        conn.execute(
            'CREATE TABLE "callSpan" ("start" DATETIME, "end" DATETIME, "appBundleID" TEXT, '
            '"appName" TEXT)',
        )
        conn.execute(
            "INSERT INTO callSpan VALUES (?, ?, 'us.zoom.xos', 'zoom.us')",
            (_utc(_at(8, 10, 15)), _utc(_at(8, 10, 45))),
        )
        conn.execute(
            "INSERT INTO span (start, end, appBundleID, appName, title, document) "
            "VALUES (?, ?, ?, 'Ghostty', 'repl', 'jarvis-docs')",
            (_utc(_at(8, 12)), _utc(_at(8, 12, 40)), GHOSTTY),
        )
    _page(  # a submission page, no mail for it
        one.timesink, _at(8, 9), "Application Submitted", "https://jobs.rbc.com/ca/en/applythankyou",
        "jobs.rbc.com",
    )
    asked: list[str] = []
    screen = TerminalScreen(_terminal_link(one.timesink, asked, up=[True]))
    brain = LedgerSources(one.memory_db, one.event_log, None, ZONE, screen)
    standing = standing_text(brain, MIDNIGHT)
    assert standing == standing_text(one, MIDNIGHT)
    assert "Computer: started 09:00, stopped 10:30, active 1h30m" in standing
    assert "Projects (TimeSink): jarvis 1h30m" in standing
    today = today_text(brain, NOW)
    assert today == today_text(one, NOW)
    assert "active today 1h42m" in today  # 09:00-09:02, 10:00-11:00 and 12:00-12:40
    assert "top apps Ghostty 1h40m; Zoom call 0h30m" in today
    hunt = job_hunt_text(brain, NOW)
    assert hunt == job_hunt_text(one, NOW)
    assert "Seen on screen only: Rbc" in hunt
    # The turn path asks again within the minute for nothing: one ask per window.
    asked.clear()
    assert today_text(brain, NOW + timedelta(seconds=20)) == today
    assert job_hunt_text(brain, NOW + timedelta(seconds=20)) == hunt
    assert asked == []
    assert screen.misses == 0


def test_without_a_terminal_or_a_path_the_ledgers_timesink_parts_are_empty_not_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to read, or a terminal that is away: the one-machine empty text, no raise."""
    one = _world(tmp_path)
    bare = LedgerSources(one.memory_db, one.event_log, None, ZONE)
    assert "Computer: no recorded activity, active 0h00m" in standing_text(bare, MIDNIGHT)
    assert "Computer: no activity recorded yet today" in today_text(bare, NOW)
    assert "Applications submitted" in job_hunt_text(bare, NOW)
    # A terminal that is away: the same empty text, counted as a miss, nothing raised.
    asked: list[str] = []
    up = [False]
    away = TerminalScreen(_terminal_link(one.timesink, asked, up=up))
    src = LedgerSources(one.memory_db, one.event_log, None, ZONE, away)
    assert today_text(src, NOW) == today_text(bare, NOW)
    assert away.misses == 1
    assert today_text(src, NOW) == today_text(bare, NOW)  # kept for a minute: no second ask
    assert asked == ["ledger_screen"] * 1
    # The cached standing text built while it was away is built again once it answers.
    monkeypatch.setattr("jarvis.state.ledger._TERMINAL_TTL_S", 0.0)
    monkeypatch.setattr("jarvis.runtime.ledger._RETRY_S", 0.0)
    context = LedgerContext(src, LedgerSettings())
    assert "Computer: started 09:00" not in context(NOW, None)[0]
    up[0] = True
    assert "Computer: started 09:00" in context(NOW, None)[0]
    up[0] = False
    assert "Computer: started 09:00" in context(NOW, None)[0]  # answered once: kept for the day
