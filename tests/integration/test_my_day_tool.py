"""ADR 0244 — ``my_day``: what he did and is doing, as one read-only tool.

Real memory.db, Event Log and a TimeSink-shaped sqlite in ``tmp_path`` (the ledger's own test
world), the brain's terminal link where one is needed; nothing is asked of a model. Each check is
a call and the exact words or fields it gives back, with local times (UTC-7 here), Thursday
2026-10-08 15:00 being now.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from jarvis.execution.my_day_tool import build_my_day_tool
from jarvis.execution.tools import Tool, ToolError
from jarvis.runtime import terminal as terminal_runtime
from jarvis.shared import CallerPrincipal
from jarvis.state.device_reads import TIMESINK_READ
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.ledger import TURN_WAIT_S, LedgerSources, TerminalScreen
from jarvis.surface.terminal_link import TerminalHub
from scripts import tool_exam
from tests.support.clock import FakeClock
from tests.support.day_line import load_rows
from tests.support.ledger import CHROME, NOW, ZONE, ledger_at, ledger_world, terminal_link, utc_text
from tests.support.ledger_phone import ORIGIN, phone_rows
from tests.support.tool_context import tool_context

REPO = Path(__file__).resolve().parents[2]
WECHAT = "com.tencent.xinWeChat"
ZH_REPORT = "# 日报\n\n## 核心摘要\n他整理了简历。\n\n## 工作事项\n- 略\n"
REPORT = "# Report\n\n## Summary\n{}\n\n## Work items\n- not the summary line\n"


def _call(tool: Tool, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the model's arguments.
    return dict(tool.handler(args, tool_context(clock=FakeClock(NOW))))


def _failure(tool: Tool, **args: Any) -> ToolError:  # noqa: ANN401 - the model's arguments.
    with pytest.raises(ToolError) as caught:
        _call(tool, **args)
    return caught.value


def _size(result: dict[str, Any]) -> int:
    return len(json.dumps(result, ensure_ascii=False))


def _tool(src: LedgerSources) -> Tool:
    (tool,) = build_my_day_tool(src)
    return tool


def _state(conn: sqlite3.Connection, analysed: str, text: str, basis: str = "observed") -> None:
    item = {"analyzed_at": analysed, "now": {"text": text, "basis": basis, "as_of": analysed}}
    emit_event(conn, type="work_state.revised", payload={"item": item, "trigger": "dashboard"})


def _briefing(conn: sqlite3.Connection, day: str, content: str, version: int = 1) -> None:
    item = {"local_date": day, "version": version, "content": content}
    emit_event(conn, type="briefing.revised", payload={
        "request_id": "r", "request_hash": "h", "operation": "save", "action_id": "a", "item": item,
    })


def _build(root: Path, *, analysed: bool = True) -> LedgerSources:
    """The ledger's world plus a call, WeChat, three commits, the phone, a work state, reports."""
    src = ledger_world(root)
    assert src.timesink is not None
    with closing(sqlite3.connect(src.timesink)) as conn, conn:
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
            "VALUES (?, ?, ?, 'WeChat', 'chat', '')",
            (utc_text(ledger_at(8, 12)), utc_text(ledger_at(8, 12, 40)), WECHAT),
        )
    with closing(open_event_log(src.event_log)) as conn:
        load_rows(conn, phone_rows(), ORIGIN)
        for sha, when in (
            ("c0", ledger_at(6, 11)),
            ("c1", ledger_at(6, 12)),
            ("c2", ledger_at(8, 13)),
        ):
            ms = int(when.timestamp() * 1000)
            emit_event(
                conn,
                type="project.commit_seen",
                ts_epoch_ms=ms,
                payload={
                    "repo_path": "/Users/a/Projects/jarvis",
                    "commit_sha": sha,
                    "subject": "s",
                    "committed_at_ms": ms,
                    "actor": "observer",
                },
            )
        if analysed:
            _state(conn, ledger_at(8, 14, 30).isoformat(), "Writing the my_day tool tests")
        _briefing(conn, "2026-10-07", REPORT.format("An old first version."))
        _briefing(conn, "2026-10-07", REPORT.format("He polished the resume and applied."), 2)
        _briefing(conn, "2026-10-05", ZH_REPORT)
        _briefing(conn, "2026-10-04", REPORT.format("word " * 1000))
    return src


@pytest.fixture
def src(tmp_path: Path) -> LedgerSources:
    """The ledger's world with the extras every path reads."""
    return _build(tmp_path)


@pytest.fixture
def tool(src: LedgerSources) -> Tool:
    """The tool over that world."""
    return _tool(src)


def test_the_tool_is_a_small_read_only_menu_entry_that_replaces_four(tool: Tool) -> None:
    """One L0 read on the menu with a short brief, replacing four, able to start no analysis."""
    assert tool.name == "my_day"
    assert tool.risk_level == "L0"
    assert tool.read_only
    assert not tool.deferred
    assert tool.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert len(tool.description) <= 650
    assert tool.description.isascii()
    assert tool.input_schema["additionalProperties"] is False
    assert set(tool.input_schema["properties"]) == {"date", "to", "about", "page"}
    assert set(tool.supersedes) == {
        "get_briefing",
        "query_activity",
        "read_activity",
        "refresh_work_state",
    }
    # It reads the saved analysis; nothing in what it is built from can start a paid one.
    assert list(inspect.signature(build_my_day_tool).parameters) == ["sources"]


# (arguments, the spoken summary exactly). Times are Vancouver; "today" is Thu Oct 8.
SUMMARIES: list[tuple[dict[str, str | int], str]] = [
    (  # today so far, with the latest analysis and its age
        {},
        "Today so far (Thu 3:00 pm): 1h 40m at the computer; mostly jarvis 1h; 1 commit; "
        "Zoom call 30m; at Home 15h. Right now (as of 30 minutes ago, at 2:30 pm): "
        "Writing the my_day tool tests.",
    ),
    (  # the saved report, the newest version of it
        {"date": "2026-10-07"},
        "yesterday (Wed), from his saved daily report: He polished the resume and applied.",
    ),
    (  # a report saved in Chinese
        {"date": "2026-10-05"},
        "Mon Oct 5, from his saved daily report: 他整理了简历。",
    ),
    (  # no report: the live aggregate
        {"date": "2026-10-06"},
        "Tue Oct 6, no saved report yet: 1h 30m at the computer; mostly jarvis 1h 30m; 2 commits.",
    ),
    (  # the same day by its word
        {"date": "yesterday"},
        "yesterday (Wed), from his saved daily report: He polished the resume and applied.",
    ),
    (  # a span: per day and in total
        {"date": "2026-10-05", "to": "2026-10-08"},
        "Mon Oct 5 to today (Thu), 4 days: 3h 55m at the computer; mostly jarvis 2h 30m; "
        "3 commits; Zoom call 30m; at Home 36h 10m, Library 3h 30m; 3 of 4 days with activity, "
        "averaging 1h 18m on those.",
    ),
    (  # one project
        {"date": "2026-10-08", "about": "jarvis"},
        '"jarvis", today (Thu): 1h at the computer; mostly jarvis 1h; 1 commit.',
    ),
    (  # one app over a span
        {"date": "2026-10-05", "to": "2026-10-08", "about": "WeChat"},
        '"WeChat", Mon Oct 5 to today (Thu): 40m at the computer; mostly WeChat 40m.',
    ),
    (  # nothing matches: what the time went to, to retry with
        {"date": "2026-10-08", "about": "Figma"},
        'Nothing on "Figma" today (Thu). The time went to: jarvis 1h, Ghostty 1h, WeChat 40m; '
        "try one of those.",
    ),
]


@pytest.mark.parametrize(("args", "summary"), SUMMARIES)
def test_each_path_leads_with_the_words_he_would_say(
    tool: Tool,
    args: dict[str, Any],
    summary: str,
) -> None:
    """Every path's summary is the exact sentence, first, within four thousand characters."""
    got = _call(tool, **args)
    assert got["summary"] == summary
    assert next(iter(got)) == "summary"
    assert _size(got) <= 4000


def test_today_carries_the_fields_and_what_he_is_doing_now_with_its_age(tool: Tool) -> None:
    """Today: the lists, when he began, and the latest analysis with how old it is."""
    got = _call(tool)
    assert got["active"] == "1h 40m"
    assert got["since"] == "10:00 am"
    assert got["projects"] == ["jarvis 1h"]
    assert got["apps"] == ["Ghostty 1h", "WeChat 40m"]
    assert got["calls"] == ["Zoom call 30m"]
    assert got["commits"] == ["jarvis 1"]
    assert got["places"] == ["Home 15h"]
    assert got["now"] == {
        "text": "Writing the my_day tool tests",
        "basis": "observed",
        "as_of": "30 minutes ago, at 2:30 pm",
    }
    assert "missing" not in got


def test_today_with_no_analysis_yet_says_what_he_is_doing_is_unknown(tmp_path: Path) -> None:
    """Before any analysis exists, today says what he is doing is unknown, not a guess."""
    got = _call(_tool(_build(tmp_path, analysed=False)))
    assert got["summary"].endswith(
        " No work-state analysis has been made yet, so what he is doing now is unknown.",
    )
    assert "now" not in got


def test_an_old_analysis_is_shown_with_its_age_and_a_warning(tmp_path: Path) -> None:
    """An analysis older than three hours is shown with its age and a warning."""
    src = _build(tmp_path)
    with closing(open_event_log(src.event_log)) as conn:
        _state(conn, ledger_at(7, 13, 45).isoformat(), "Reading a paper", "inferred")
    got = _call(_tool(src))
    assert got["now"] == {
        "text": "Reading a paper",
        "basis": "inferred",
        "as_of": "25 hours ago, at yesterday (Wed) 1:45 pm",
        "stale": "this analysis is old and may not describe the present",
    }
    assert (
        "Right now (as of 25 hours ago, at yesterday (Wed) 1:45 pm): Reading a paper. "
        "Note: this analysis is old and may not describe the present."
    ) in got["summary"]


def test_an_analysis_with_no_present_claim_does_not_pass_the_past_off_as_now(
    tmp_path: Path,
) -> None:
    """Without a `now` claim the day's activities are named as covered, not as the present."""
    src = _build(tmp_path, analysed=False)
    item = {
        "analyzed_at": ledger_at(8, 14, 30).isoformat(),
        "now": None,
        "activities": [{"text": "Polishing the resume", "basis": "stated"}, {"text": "Mail"}],
    }
    with closing(open_event_log(src.event_log)) as conn:
        emit_event(conn, type="work_state.revised", payload={"item": item, "trigger": "auto"})
    got = _call(_tool(src))
    assert got["now"] == {
        "as_of": "30 minutes ago, at 2:30 pm",
        "covered": ["Polishing the resume", "Mail"],
    }
    assert (
        " The latest work analysis (30 minutes ago, at 2:30 pm) names nothing he is doing right "
        "now. It covered: Polishing the resume; Mail."
    ) in got["summary"]


def test_a_past_day_without_a_report_gives_when_it_began_and_ended(src: LedgerSources) -> None:
    """A day past midnight is stopped at its real end, in local time."""
    assert src.timesink is not None
    with closing(sqlite3.connect(src.timesink)) as conn, conn:
        conn.execute(
            "INSERT INTO span (start, end, appBundleID, appName, title, document) "
            "VALUES (?, ?, ?, 'Chrome', 'repl', '')",
            (utc_text(ledger_at(6, 23, 30)), utc_text(ledger_at(7, 0, 40)), CHROME),
        )
    got = _call(_tool(src), date="2026-10-06")
    assert got["started"] == "9:00 am"
    assert got["stopped"] == "12:40 am (after midnight)"
    assert got["commits"] == ["jarvis 2"]


def test_a_span_lists_each_busy_day_and_folds_quiet_runs_and_weeks(src: LedgerSources) -> None:
    """A span: one row per busy day, quiet runs folded to one row, week blocks past seven days."""
    got = _call(_tool(src), date="2026-10-05", to="2026-10-08")
    assert got["days"] == [
        "Mon Oct 5: no computer activity",
        "Tue Oct 6: 1h 30m, jarvis 1h 30m, 2 commits",
        "yesterday (Wed): 45m",
        "today (Thu): 1h 40m, jarvis 1h, 1 commit",
    ]
    assert "weeks" not in got  # a week or less is read day by day
    long = _call(_tool(src), date="2026-09-26", to="2026-10-08")
    assert long["days"] == [
        "Sat Sep 26 to Mon Oct 5: no computer activity (10 days)",
        "Tue Oct 6: 1h 30m, jarvis 1h 30m, 2 commits",
        "yesterday (Wed): 45m",
        "today (Thu): 1h 40m, jarvis 1h, 1 commit",
    ]
    assert long["weeks"] == [
        "Sep 26 - Oct 2 (7 days): no computer activity",
        "Oct 3-8 (6 days so far): 3h 55m, 39m a day",
    ]


def test_about_names_the_time_inside_one_project_and_its_days(tool: Tool) -> None:
    """about: one project's time by day and by window; no places, which belong to no project."""
    got = _call(tool, date="2026-10-05", to="2026-10-08", about="jarvis")
    assert got["projects"] == ["jarvis 2h 30m"]
    assert got["commits"] == ["jarvis 3"]
    assert got["days"] == [
        "Tue Oct 6: 1h 30m, jarvis 1h 30m, 2 commits",
        "today (Thu): 1h, jarvis 1h, 1 commit",
    ]
    assert got["inside"] == ["Ghostty: repl 2h 30m"]
    assert got["more"] is False
    assert "places" not in got  # a project has no place


def test_about_pages_by_number_and_says_where_the_list_ends(tmp_path: Path) -> None:
    """20 window titles, longest first: pages of 8, 8 and 4, then a sentence for page 4."""
    src = _build(tmp_path)
    assert src.timesink is not None
    start = ledger_at(8, 1)
    with closing(sqlite3.connect(src.timesink)) as conn, conn:
        for i in range(1, 21):  # chat-01 lasts 2 minutes, chat-20 lasts 40
            end = start + timedelta(minutes=2 * i)
            conn.execute(
                "INSERT INTO span (start, end, appBundleID, appName, title, document) "
                "VALUES (?, ?, 'org.signal', 'Signal', ?, '')",
                (utc_text(start), utc_text(end), f"chat-{i:02d}"),
            )
            start = end
    tool = _tool(src)
    pages = [_call(tool, date="2026-10-08", about="signal", page=n) for n in (1, 2, 3)]
    names = [[row.split(" ")[1] for row in page["inside"]] for page in pages]
    assert names[0] == [f"chat-{i:02d}" for i in range(20, 12, -1)]
    assert names[1] == [f"chat-{i:02d}" for i in range(12, 4, -1)]
    assert names[2] == [f"chat-{i:02d}" for i in range(4, 0, -1)]
    assert [page["more"] for page in pages] == [True, True, False]
    assert _call(tool, date="2026-10-08", about="signal") == pages[0]
    past = _call(tool, date="2026-10-08", about="signal", page=4)
    assert past == {
        "summary": 'There is no page 4 of "signal"; its list ends at page 3.',
        "inside": [],
        "more": False,
    }


def test_a_saved_report_is_cut_to_fit_and_points_at_the_rest(tool: Tool) -> None:
    """A long saved report is cut to its first 2400 characters."""
    got = _call(tool, date="2026-10-04")
    assert got["source"] == "saved report"
    assert got["summary"].startswith("Sun Oct 4, from his saved daily report: word word")
    assert got["summary"].endswith("…")
    assert 2400 < len(got["summary"]) < 2500
    assert _size(got) <= 4000


def test_without_timesink_it_says_which_parts_are_missing_and_gives_the_rest(
    src: LedgerSources,
) -> None:
    """No TimeSink here: the apps part is named missing, commits and places still come."""
    bare = LedgerSources(src.memory_db, src.event_log, None, ZONE)
    got = _call(_tool(bare))
    gone = "apps, projects and calls (TimeSink is not set up on this machine)"
    assert got["missing"] == [gone]
    assert got["commits"] == ["jarvis 1"]
    assert got["places"] == ["Home 15h"]
    assert "active" not in got
    assert got["now"]["text"] == "Writing the my_day tool tests"
    assert f"Could not read: {gone}." in got["summary"]
    assert got["summary"].startswith("Today so far (Thu 3:00 pm): no computer activity recorded; ")


def test_a_terminal_that_is_away_is_named_and_the_brains_own_data_still_answers(
    src: LedgerSources,
) -> None:
    """A brain whose terminal is away says so and answers from what the brain holds."""
    away = TerminalScreen(terminal_link(src.timesink, [], up=[False]))
    brain = LedgerSources(src.memory_db, src.event_log, None, ZONE, away)
    got = _call(_tool(brain), date="2026-10-05", to="2026-10-07")
    assert got["missing"] == ["apps, projects and calls (his computer's terminal is not connected)"]
    assert got["summary"].endswith(
        "Could not read: apps, projects and calls (his computer's terminal is not connected). "
        "Commits and places are what the brain already holds.",
    )
    assert got["commits"] == ["jarvis 2"]
    assert got["places"][:2] == ["Home 21h 10m", "Library 3h 30m"]
    assert got["days"] == [
        "Mon Oct 5: no computer activity",
        "Tue Oct 6: 2 commits",
        "yesterday (Wed): no computer activity",
    ]
    assert away.misses == 1


def test_a_terminal_that_does_not_answer_holds_my_day_two_seconds_and_its_late_answer_is_kept(
    src: LedgerSources,
) -> None:
    """A Mac asleep behind a half-open socket: this turn goes on, the next has its answer."""
    asked: list[dict[str, Any]] = []

    async def scenario() -> tuple[float, dict[str, Any], dict[str, Any]]:
        hub = TerminalHub()  # the daemon's own call timeout

        async def send(text: str) -> None:
            asked.append(json.loads(text))  # the frame goes out; nothing comes back yet

        link = hub.attach("macbook", frozenset({TIMESINK_READ}), send)
        brain = LedgerSources(
            src.memory_db,
            src.event_log,
            None,
            ZONE,
            TerminalScreen(hub.call, wait_s=TURN_WAIT_S),
        )
        tool = _tool(brain)
        began = time.monotonic()
        without = await asyncio.to_thread(_call, tool, date="2026-10-06")
        took = time.monotonic() - began
        for frame in asked:  # the Mac wakes and answers what it was asked
            reply = terminal_runtime._read_device(  # noqa: SLF001
                frame["tool"],
                frame["arguments"],
                src.timesink,
                (),
            )
            hub.deliver(link, {"type": "result", "id": frame["id"], **reply})
        await asyncio.sleep(0.2)
        return took, without, await asyncio.to_thread(_call, tool, date="2026-10-06")

    took, without, later = asyncio.run(scenario())
    assert took < TURN_WAIT_S + 1
    assert without["missing"] == [
        "apps, projects and calls (his computer's terminal is not connected)",
    ]
    assert without["commits"] == ["jarvis 2"]
    assert len(asked) == 1
    assert later == _call(_tool(src), date="2026-10-06")


def test_a_connected_terminal_gives_what_one_machine_gives(src: LedgerSources) -> None:
    """A connected terminal reads the same as the machine that holds TimeSink."""
    up = TerminalScreen(terminal_link(src.timesink, [], up=[True]))
    brain = LedgerSources(src.memory_db, src.event_log, None, ZONE, up)
    for args in ({}, {"date": "2026-10-06"}, {"date": "2026-10-05", "to": "2026-10-08"}):
        assert _call(_tool(brain), **args) == _call(_tool(src), **args)
    assert up.misses == 0


def test_a_long_span_with_long_names_is_trimmed_to_four_thousand_and_says_so(
    tmp_path: Path,
) -> None:
    """31 days with long project names: rows are dropped from the end and the cut is said."""
    src = ledger_world(tmp_path)
    assert src.timesink is not None
    names = [f"{chr(65 + i)}-" + "project-name-" * 9 for i in range(5)]  # 119 characters each
    with closing(sqlite3.connect(src.timesink)) as conn, conn:
        for table in ("span", "project", "jevProjectVerdict"):
            conn.execute(f'DELETE FROM "{table}"')  # noqa: S608 - fixed table names
        for i, name in enumerate(names, 1):
            conn.execute("INSERT INTO project VALUES (?, ?)", (i, name))
            conn.execute(
                "INSERT INTO jevProjectVerdict VALUES (?, '', 'w', '', ?)",
                (f"app.{i}", i),
            )
        for n in range(31):  # Sep 8 .. Oct 8
            day = ledger_at(8, 9) - timedelta(days=n)
            conn.execute(
                "INSERT INTO span (start, end, appBundleID, appName, title, document) "
                "VALUES (?, ?, ?, ?, 'w', '')",
                (
                    utc_text(day),
                    utc_text(day + timedelta(hours=1)),
                    f"app.{n % 5 + 1}",
                    f"App {n % 5}",
                ),
            )
    got = _call(_tool(src), date="2026-09-08", to="2026-10-08")
    assert _size(got) <= 4000
    assert got["cut"].endswith("rows left out to fit; ask for a shorter span or use about")
    assert len(got["days"]) < 31
    assert got["summary"].startswith("Tue Sep 8 to today (Thu), 31 days: 31h at the computer")


ERRORS: list[tuple[dict[str, Any], str]] = [
    ({"bogus": 1}, "my_day: no argument 'bogus'; the arguments are date, to, about and page"),
    (
        {"to": "2026-10-08"},
        "my_day: to is the last day of a span that date starts; pass date=... as well",
    ),
    ({"page": 2}, "my_day: page continues the list that about=... gives; pass the same about"),
    ({"date": "2026-10-09"}, "my_day: 2026-10-09 has not happened yet; today is 2026-10-08"),
    (
        {"date": "soon"},
        "my_day: a date looks like 2026-10-01 (or the words today, yesterday), not 'soon'",
    ),
    (
        {"date": "2026-10-08", "to": "2026-10-01"},
        "my_day: to=2026-10-01 is before date=2026-10-08; to is the span's last day",
    ),
    (
        {"date": "2026-09-01", "to": "2026-10-08"},
        "my_day: a span is at most 31 days; ask for a longer stretch in parts",
    ),
    (
        {"date": "2026-10-08", "about": "x", "page": 0},
        "my_day: page must be a whole number, e.g. page=2 for items 9-16",
    ),
    (
        {"date": "2026-10-08", "about": "x", "page": "two"},
        "my_day: page must be a whole number, e.g. page=2 for items 9-16",
    ),
]


@pytest.mark.parametrize(("args", "message"), ERRORS)
def test_a_wrong_call_says_what_happened_and_what_to_pass(
    tool: Tool,
    args: dict[str, Any],
    message: str,
) -> None:
    """Each wrong call is refused with the sentence that tells the model what to pass."""
    error = _failure(tool, **args)
    assert error.code == "invalid_arguments"
    assert str(error) == message


def test_an_event_log_that_cannot_be_opened_is_one_clear_error(tmp_path: Path) -> None:
    """A store that cannot be read is one `unavailable` error that says to tell him."""
    src = _build(tmp_path)
    gone = LedgerSources(src.memory_db, tmp_path / "missing.db", src.timesink, ZONE)
    error = _failure(_tool(gone))
    assert error.code == "unavailable"
    assert str(error).startswith("His day cannot be read just now (")
    assert str(error).endswith("); tell him so.")


def test_the_menu_offers_my_day_and_not_the_four_tools_it_replaces(tmp_path: Path) -> None:
    """The shipped config: `my_day` is on the menu; the old reads and the analyst are off it."""
    recorded = tmp_path / "mcp-tools.json"
    recorded.write_text("{}", encoding="utf-8")
    config = yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    names = {one["name"] for one in tool_exam.offered_tools(config, recorded)}
    old = {"get_briefing", "query_activity", "read_activity", "refresh_work_state"}
    assert "my_day" in names
    assert not names & old
    registry = tool_exam._exam_registry(config, recorded)  # noqa: SLF001 - the registry behind the menu
    assert old <= {t.name for t in registry.get_definitions() if t.deferred}
    assert {k: v for k, v in registry.superseded().items() if v[0] == "my_day"} == {
        "get_briefing": ("my_day", "my_day(date=...)"),
        "query_activity": ("my_day", "my_day(date=..., about=...)"),
        "read_activity": ("my_day", "my_day(date=..., about=...)"),
        "refresh_work_state": ("my_day", "my_day()"),
    }
