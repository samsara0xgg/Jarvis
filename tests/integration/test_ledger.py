"""ADR 0201 — the ledger: his days, weeks and job hunt computed from his own data.

Real memory.db, Event Log and a TimeSink-shaped sqlite built in ``tmp_path``; the day-prose
call is the only fake (a client that answers from a script and records what it was asked).
Each check feeds known rows and asserts the text the program computes from them.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import openai
import pytest

from jarvis.decision.day_prose import build_day_prose_messages, check_day_prose
from jarvis.execution.job_ledger_tool import build_record_application_tool
from jarvis.execution.tools import ToolError
from jarvis.runtime.ledger import LedgerContext, LedgerSettings, ProseSettings
from jarvis.state import core_memory, reminders, things
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.job_ledger import add_application, list_applications
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
from tests.support.ledger import (
    CHROME,
    GHOSTTY,
    MIDNIGHT,
    NOW,
    ZONE,
    ProseWriter,
    ledger_at,
    ledger_rows,
    ledger_world,
    run_ledger,
    terminal_link,
    utc_text,
    write_mail,
)
from tests.support.prompt_assembly import drive_one_turn

if TYPE_CHECKING:
    from pathlib import Path


def test_standing_text_computes_days_job_hunt_and_notes(tmp_path: Path) -> None:
    """Blocks B-D: hand-computed day lines, week totals, job hunt and the notes cut-off."""
    src = ledger_world(tmp_path)
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
    src = ledger_world(tmp_path)
    assert src.timesink is not None
    with closing(sqlite3.connect(src.timesink)) as conn, conn:
        conn.execute(
            "INSERT INTO span (start, end, appBundleID, appName, title, document) "
            "VALUES (?, ?, ?, 'Chrome', 'repl', '')",
            (utc_text(ledger_at(7, 23, 30)), utc_text(ledger_at(8, 0, 40)), CHROME),
        )
    at_midnight = standing_text(src, MIDNIGHT)
    assert "Computer: started 13:00, stopped 00:00 (after midnight), active 1h15m" in at_midnight
    at_five = standing_text(src, MIDNIGHT, day_end_as_of=ledger_at(8, 5))
    assert "Computer: started 13:00, stopped 00:40 (after midnight), active 1h15m" in at_five


def test_notes_stamp_follows_the_newest_note(tmp_path: Path) -> None:
    """The cache key is the newest note time of each table, empty without them."""
    src = ledger_world(tmp_path)
    assert notes_stamp(src) == (ledger_at(8, 5).isoformat(), ledger_at(7, 5).isoformat())
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()  # a store with neither table
    assert notes_stamp(LedgerSources(bare, src.event_log, None, ZONE)) == ("", "")


def test_today_and_since_text(tmp_path: Path) -> None:
    """Blocks E-F: today's job mail and reminders, and the nothing-new case."""
    src = ledger_world(tmp_path)
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
    assert (
        "Pending reminders (1; quoted words are what she says when it rings, worded for that "
        'moment; a ring for a thing names the thing): Fri 10-09 09:00 "call the dentist"'
    ) in today
    # He last talked at 14:00, after the 11:30 mail: nothing is new.
    assert "Nothing new: no mail, reminders or commits." in since_text(src, NOW, ledger_at(8, 14))
    assert since_text(src, NOW, None) == ""
    earlier = since_text(src, NOW, ledger_at(8, 10))
    assert "Mail 10-08 11:30: interview mail (Beta Labs, from Pat Lee)" in earlier
    assert "Nothing new" not in earlier


def test_today_lists_his_open_things_the_overdue_ones_and_their_rings(tmp_path: Path) -> None:
    """ADR 0299: the day's open things come from the ledger with their ids, and their rings too."""
    src = ledger_world(tmp_path)
    write: dict[str, Any] = {
        "by": "her",
        "evidence": "said so",
        "zone": ZONE,
        "now": ledger_at(8, 7),
    }
    with closing(open_event_log(src.event_log)) as conn:
        for title, kind, fields in (
            ("Dentist", "go", {"start_at": ledger_at(8, 16, 30).isoformat(), "rings": ("-30m",)}),
            ("Stand-up", "go", {"start_at": ledger_at(8, 9).isoformat(), "rings": ("-30m",)}),
            ("Holiday", "go", {"on_day": "2026-10-08"}),
            ("Therapy", "go", {"start_at": ledger_at(8, 18).isoformat(), "private": True}),
            ("Pay rent", "do", {"due": "2026-10-08"}),
            ("Send form", "do", {"due": ledger_at(8, 17).isoformat(), "confirmed": False}),
            (
                "Pottery",
                "go",
                {"start_at": ledger_at(1, 19).isoformat(), "weekly_until": "2026-10-29"},
            ),
            ("Call mum", "do", {"start_at": ledger_at(6, 10).isoformat()}),
            ("Old bill", "do", {"due": "2026-10-01"}),
            ("Seminar", "go", {"start_at": ledger_at(5, 10).isoformat()}),
            ("Fix the bike", "do", {}),
            ("Done already", "do", {"due": "2026-10-08"}),
            ("Dropped", "go", {"start_at": ledger_at(8, 20).isoformat()}),
            ("Tomorrow", "go", {"start_at": ledger_at(9, 10).isoformat()}),
        ):
            things.add(conn, things.Thing(things.new_id(), title, kind, **fields), **write)
        held = {t.title: t for t in things.fold(conn).values()}
        things.close(conn, held["Done already"].thing_id, "done", **write)
        things.close(conn, held["Dropped"].thing_id, "cancelled", **write)
        # The stand-up's ring (08:30) rang this morning, and so did a reminder he set himself.
        (ring,) = [
            r.reminder_id
            for r in reminders.fold(conn).values()
            if r.reminder_id.startswith(things.RING_PREFIX) and r.text.endswith("Stand-up")
        ]
        own = ledger_at(8, 10)
        emit_event(
            conn,
            type="reminder.scheduled",
            ts_epoch_ms=int(ledger_at(8, 7).timestamp() * 1000),
            payload={
                "reminder_id": "reminder-own",
                "due_at_epoch_ms": int(own.timestamp() * 1000),
                "due_at_local": own.isoformat(),
                "text": "stretch",
                "action_id": "a2",
            },
        )
        for rid, at in ((ring, ledger_at(8, 8, 31)), ("reminder-own", ledger_at(8, 10, 1))):
            emit_event(
                conn,
                type="reminder.fired",
                ts_epoch_ms=int(at.timestamp() * 1000),
                payload={
                    "reminder_id": rid,
                    "fired_at": int(at.timestamp() * 1000),
                    "late_ms": 0,
                    "delivered": "speak",
                },
            )
    today = today_text(src, NOW).splitlines()
    ids = {title: things.short_id(one) for title, one in held.items()}
    assert (
        f'  Open things today (7): all day "Holiday" [{ids["Holiday"]}]; 09:00 "Stand-up" '
        f'[{ids["Stand-up"]}]; 16:30 "Dentist" [{ids["Dentist"]}]; due 17:00 "Send form" '
        f'[{ids["Send form"]}] (not confirmed); 18:00 "something private" [{ids["Therapy"]}]; '
        f'19:00 "Pottery" [{ids["Pottery"]}]; due today "Pay rent" [{ids["Pay rent"]}]'
    ) in today
    assert (
        f'  Overdue things (2): "Old bill" [{ids["Old bill"]}] (was Thu 10-01); "Call mum" '
        f'[{ids["Call mum"]}] (was Tue 10-06)'
    ) in today
    # A thing's rings are listed with the reminders, named by the thing and its id: the
    # dentist's 16:00 ring is still to ring, the stand-up's 08:30 ring rang this morning.
    (pending,) = [line for line in today if line.startswith("  Pending reminders")]
    assert pending.startswith("  Pending reminders (2;")
    assert pending.endswith(
        f'Thu 10-08 16:00 for "Dentist" [{ids["Dentist"]}] at Thu 10-08 16:30; '
        'Fri 10-09 09:00 "call the dentist"',
    )
    assert (
        f'  Fired earlier today: 08:30 for "Stand-up" [{ids["Stand-up"]}]; 10:00 stretch'
    ) in today


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
                conn,
                base=None,
                doc=doc,
                origin="test",
                upto_day=None,
                changes=[],
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
    src = ledger_world(tmp_path)
    context = LedgerContext(src, LedgerSettings())
    standing, per_turn = context(NOW, ledger_at(8, 14))
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
            (ledger_at(8, 6).isoformat(),),
        )
    assert "The day: A quiet Wednesday." in context(NOW, None)[0]
    # A broken source costs the turn its blocks and nothing else.
    broken = LedgerContext(
        LedgerSources(tmp_path / "missing" / "memory.db", src.event_log, None, ZONE),
        LedgerSettings(),
    )
    assert broken(NOW, ledger_at(8, 14)) == ("", "")


def test_ledger_warm_leaves_the_first_turn_a_cached_standing_text(tmp_path: Path) -> None:
    """The boot warm-up builds the standing text, so the first turn after a restart reuses it."""
    context = LedgerContext(ledger_world(tmp_path), LedgerSettings())
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


def _answer(prose: str) -> str:
    """What the prose preset answers (ADR 0235): the prose, a one-sentence line, no highlights."""
    return json.dumps({"prose": prose, "line": "A day, told in a line.", "highlights": []})


def test_run_day_prose_retries_skips_and_stores_one_row_per_day(tmp_path: Path) -> None:
    """A timeout is retried; days with a line or no activity are skipped."""
    src = ledger_world(tmp_path)  # 10-06 already has a line; 10-04 has no activity at all
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "http://invalid.test"))
    writer = ProseWriter(
        [
            timeout,
            _answer("Gamma wrote back. Not much else."),
            _answer("A short Wednesday."),
        ]
    )
    sleeps: list[float] = []
    outcomes = run_ledger(src, writer, 4, sleeps)
    assert outcomes == {
        "2026-10-04": "no activity",
        "2026-10-05": "landed (32 chars)",
        "2026-10-07": "landed (18 chars)",
    }
    assert ledger_rows(src) == {
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
    assert run_ledger(src, ProseWriter([]), 4, []) == {
        "2026-10-04": "no activity"
    }  # nothing left to write


def test_run_day_prose_stores_nothing_for_a_rejected_or_failed_day(tmp_path: Path) -> None:
    """Twice rejected or a hard error stores nothing and the next day still runs."""
    src = ledger_world(tmp_path)
    writer = ProseWriter(
        [
            _answer("## A heading"),
            _answer("- a bullet"),
            RuntimeError("boom"),
            _answer("Wednesday was quiet."),
        ]
    )
    outcomes = run_ledger(
        src, writer, 3, []
    )  # 10-05 rejected twice, 10-07 fails, but 10-06 has a line
    assert outcomes["2026-10-05"] == "rejected (contains markdown headings or bullets)"
    assert outcomes["2026-10-07"] == "failed"
    assert ledger_rows(src) == {"2026-10-06": ["He shipped the ledger and went to bed."]}
    redo = ProseWriter([_answer("Gamma wrote back."), _answer("Wednesday was quiet.")])
    again = run_ledger(src, redo, 3, [])
    assert [again[k].split(" ")[0] for k in sorted(again)] == ["landed", "landed"]


def test_day_prose_input_and_gate() -> None:
    """The three input sections, the report trim and each reason the gate rejects."""
    long_report = "r" * 5000
    content = build_day_prose_messages(
        "2026-10-07",
        "NUM",
        None,
        long_report,
        "ITEMS",
        "English",
    )[0]["content"]
    assert content.index("NUMBERS (computed by the program):\nNUM") < content.index(
        "DAY SUMMARY (conversation with the assistant):\n(none yet)",
    )
    assert content.endswith(
        "DAILY WORK REPORT (excerpt):\n"
        + "r" * 2600
        + "\n\nDAY LINE ITEMS (id | kind | local time | facts):\nITEMS\n\nLINE LANGUAGE: English",
    )
    no_report = build_day_prose_messages("d", "n", "s", None, "", "English")[0]["content"]
    assert "DAILY WORK REPORT (excerpt):\n(none yet)\n\nDAY LINE ITEMS" in no_report
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
            (utc_text(start), utc_text(start + timedelta(minutes=2)), CHROME, title, url, domain),
        )


def test_applications_submitted_counts_each_submission_once_across_three_sources(
    tmp_path: Path,
) -> None:
    """Block D: mail receipts, success pages seen on screen and what he told her, deduplicated."""
    src = ledger_world(tmp_path)  # two receipts: Gamma Inc 10-05 and Acme Robotics 10-07
    assert src.timesink is not None
    ts, memory = src.timesink, src.memory_db
    greenhouse = "job-boards.greenhouse.io"
    # A Greenhouse form, then its confirmation page, then the receipt 3 minutes later: one.
    form = f"https://{greenhouse}/zeta/jobs/55"
    _page(
        ts, ledger_at(8, 9, 50), "Job Application for Backend Co-op at Zeta Corp", form, greenhouse
    )
    _page(ts, ledger_at(8, 10), "Thank you for applying", form + "/confirmation", greenhouse)
    write_mail(
        memory,
        "z1",
        ledger_at(8, 10, 3),
        kind="receipt",
        company="Zeta Corp",
        role="Backend Co-op",
        subject="Thanks Zeta",
    )
    # RBC: a role page, the applythankyou page, and the same page again hours later; no mail: one.
    rbc = "jobs.rbc.com"
    role_page = "https://jobs.rbc.com/ca/en/job/R-1"
    _page(ts, ledger_at(6, 10, 50), "Winter 2027 Co-op Student | RBC Careers", role_page, rbc)
    thanks = (
        "https://jobs.rbc.com/ca/en/applythankyou?jobId=R-1"
        "&jobTitle=Winter%202027%20Co-op%20Student#top"
    )
    _page(ts, ledger_at(6, 11), "Application Submitted", thanks, rbc)
    _page(ts, ledger_at(6, 18), "Application Submitted", thanks, rbc)
    # Workday: no jobTitle, so the role is the last real page title before it (not "Apply").
    clio = "clio.wd3.myworkdayjobs.com"
    flow = f"https://{clio}/en-US/Clio/job/Vancouver/Data-Co-op_R1"
    _page(ts, ledger_at(7, 15), "Senior Data Co-op - Clio | Workday", flow, clio)
    _page(ts, ledger_at(7, 15, 5), "Apply", flow + "/apply", clio)
    _page(ts, ledger_at(7, 15, 10), "Application Received", flow + "/apply/success", clio)
    # A Gmail tab whose title reads like a success page is not a submission.
    inbox = "https://mail.google.com/mail/u/0/#inbox"
    _page(ts, ledger_at(8, 12), "Thank you for applying to Acme - Gmail", inbox, "mail.google.com")
    # Moment Energy: its ATS confirming the same role 3 minutes later is the same submission, a
    # mail 70 minutes later is a second one.
    for mid, minute, sender in (
        ("e1", 0, "Moment Energy"),
        ("e2", 3, "Workable"),
        ("e3", 70, "Moment Energy"),
    ):
        write_mail(
            memory,
            mid,
            ledger_at(7, 10) + timedelta(minutes=minute),
            kind="receipt",
            company=sender,
            role="Co-op",
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
        ("Hand Made Ltd", "manual"),
        ("Orbit Co", "said"),
        ("zeta corp", "said"),
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


def test_a_brains_ledger_reads_its_terminals_timesink_and_equals_one_machines(
    tmp_path: Path,
) -> None:
    """No local TimeSink path, a terminal behind the link: every TimeSink part is the same text."""
    one = ledger_world(tmp_path)
    assert one.timesink is not None
    with closing(sqlite3.connect(one.timesink)) as conn, conn:
        conn.execute(
            'CREATE TABLE "callSpan" ("start" DATETIME, "end" DATETIME, "appBundleID" TEXT, '
            '"appName" TEXT)',
        )
        conn.execute(
            "INSERT INTO callSpan VALUES (?, ?, 'us.zoom.xos', 'zoom.us')",
            (utc_text(ledger_at(8, 10, 15)), utc_text(ledger_at(8, 10, 45))),
        )
        conn.execute(
            "INSERT INTO span (start, end, appBundleID, appName, title, document) "
            "VALUES (?, ?, ?, 'Ghostty', 'repl', 'jarvis-docs')",
            (utc_text(ledger_at(8, 12)), utc_text(ledger_at(8, 12, 40)), GHOSTTY),
        )
    _page(  # a submission page, no mail for it
        one.timesink,
        ledger_at(8, 9),
        "Application Submitted",
        "https://jobs.rbc.com/ca/en/applythankyou",
        "jobs.rbc.com",
    )
    asked: list[str] = []
    screen = TerminalScreen(terminal_link(one.timesink, asked, up=[True]))
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


def test_a_turn_never_waits_on_the_terminal_for_a_read_it_already_knows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cold ask waits; a new minute gets the kept answer at once, a background ask replaces it."""
    delay, asked = [0.3], list[str]()

    def link(op: str, arguments: Any, target: str | None) -> dict[str, Any]:  # noqa: ANN401
        del op, target
        asked.append(arguments["args"]["as_of"])
        time.sleep(delay[0])
        return {"result": f"answer@{arguments['args']['as_of']}"}

    screen = TerminalScreen(link)
    caplog.set_level("INFO", logger="jarvis.state.ledger")
    began = time.monotonic()
    assert screen.read("ledger_submissions", as_of="m1") == "answer@m1"
    assert time.monotonic() - began >= 0.3  # nothing known: waits
    assert any("terminal read ledger_submissions took" in r.message for r in caplog.records)
    delay[0] = 3.0  # a slow terminal from here on
    began = time.monotonic()
    assert screen.read("ledger_submissions", as_of="m2") == "answer@m1"  # new minute: kept one
    assert screen.read("ledger_submissions", as_of="m3") == "answer@m1"  # one refresh in flight
    assert time.monotonic() - began < 0.5
    deadline = time.monotonic() + 10
    while screen.read("ledger_submissions", as_of="m2") != "answer@m2":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert asked == ["m1", "m2"]  # m3 asked nothing; the refresh replaced the answer
    assert screen.misses == 0


def _gated(link: Any, answers: threading.Event) -> Any:  # noqa: ANN401
    """``link``, but the Mac answers only once ``answers`` is set; until then it hangs 10 s."""

    def gated(op: str, arguments: Any, target: str | None) -> dict[str, Any]:  # noqa: ANN401
        answers.wait(10)
        return dict(link(op, arguments, target))

    return gated


def _turn_state(tmp_path: Path, src: LedgerSources, context: LedgerContext) -> tuple[str, float]:
    """The state block a turn sends, and the seconds from his words to the model request."""
    (tmp_path / "turn").mkdir(exist_ok=True)
    began = time.monotonic()
    memory = render_context(src.memory_db, exclude_id="", now=NOW, ledger=context)
    _, messages = drive_one_turn(tmp_path / "turn", (), time_note=memory.now)
    return str(messages[-1]["content"]), time.monotonic() - began


def test_a_turn_reaches_the_model_while_the_terminal_hangs_and_leaves_the_computer_out(
    tmp_path: Path,
) -> None:
    """ADR 0264: a Mac that does not answer for 10 s holds no turn; the line is left out."""
    one = ledger_world(tmp_path)
    answers = threading.Event()  # never set here: every read of the Mac hangs
    link = _gated(terminal_link(one.timesink, [], up=[True]), answers)
    src = LedgerSources(one.memory_db, one.event_log, None, ZONE, TerminalScreen(link))
    context = LedgerContext(src, LedgerSettings())
    context.warm(lambda: NOW)
    time.sleep(0.3)  # the warm-up thread is inside the hung read
    block, took = _turn_state(tmp_path, src, context)
    answers.set()
    assert took < 0.2
    assert "[Today so far" in block
    assert "Computer:" not in block
    assert "no activity recorded" not in block


def test_a_turn_shows_the_computer_from_the_prefetch_with_when_it_was_read(
    tmp_path: Path,
) -> None:
    """ADR 0264: the prefetched answer is the line's source, aged; a later hang changes nothing."""
    one = ledger_world(tmp_path)
    answers = threading.Event()
    answers.set()
    link = _gated(terminal_link(one.timesink, [], up=[True]), answers)
    src = LedgerSources(one.memory_db, one.event_log, None, ZONE, TerminalScreen(link))
    context = LedgerContext(src, LedgerSettings())
    context.warm(lambda: NOW)
    deadline = time.monotonic() + 10
    while "Computer:" not in context(NOW, None)[1]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    answers.clear()  # the Mac stops answering
    block, took = _turn_state(tmp_path, src, context)
    answers.set()
    assert took < 0.2
    assert re.search(r"Computer: .*active today 1h00m.*\(TimeSink as of 1[45]:\d\d\)", block)


def test_without_a_terminal_or_a_path_the_ledgers_timesink_parts_are_empty_not_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to read, or a terminal that is away: the one-machine empty text, no raise."""
    one = ledger_world(tmp_path)
    bare = LedgerSources(one.memory_db, one.event_log, None, ZONE)
    assert "Computer: no recorded activity, active 0h00m" in standing_text(bare, MIDNIGHT)
    assert "Computer: no activity recorded yet today" in today_text(bare, NOW)
    assert "Applications submitted" in job_hunt_text(bare, NOW)
    # A terminal that is away: the same empty text, counted as a miss, nothing raised.
    asked: list[str] = []
    up = [False]
    away = TerminalScreen(terminal_link(one.timesink, asked, up=up))
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
