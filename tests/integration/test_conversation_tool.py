"""ADR 0243: what he and Jarvis said is read with one tool.

A real memory.db (records and day summaries, written through the store's own opener) is read by
the real tool under a fixed clock; no model, no network. Each check asserts what the tool returns
to the model: the summary first, the fields, their size, the wording of an error.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
import yaml

from jarvis.execution.conversation_tool import build_conversation_tool
from jarvis.execution.tools import Tool, ToolContext, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state.memory_db import open_memory_db
from scripts import tool_exam

REPO = Path(__file__).resolve().parents[2]
ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 11, 21, 0, tzinfo=UTC)  # Sunday 2:00 pm in Vancouver
SUMMARY = """## 2026-10-01
### Topics
- Looking for co-op jobs, Shopify first [record_id=r01]
- The Shopify interview reminder [record_id=r03]
### Decisions and facts the user stated
- Interview is on Friday [record_id=r03]
### Unfinished
none
"""

# (id, stored timestamp, source, text). Stored offsets differ on purpose: a record is placed on
# the day it was said in Vancouver whatever offset it was written with.
RECORDS: list[tuple[str, str, str, str]] = [
    ("s01", "2026-09-03T08:00:00-07:00", "allen", "First words of September, about the garden"),
    ("s02", "2026-09-03T08:01:00-07:00", "jarvis", "The garden sounds nice"),
    ("r01", "2026-10-01T09:00:00-07:00", "allen", "找 co-op 的工作, 先看 Shopify"),
    ("r02", "2026-10-01T09:01:00-07:00", "jarvis", "好的, 我来查 co-op 的岗位"),
    ("r03", "2026-10-01T15:12:00-07:00", "allen", "remind me about the Shopify interview"),
    ("r04", "2026-10-01T15:13:00-07:00", "jarvis", "Okay, I will remind you about the interview"),
    ("r05", "2026-10-02T02:30:00+00:00", "allen", "late note, stored in UTC: pack the laptop"),
    ("r06", "2026-10-03T10:00:00-07:00", "mail", "From: lee@uvic.ca\n\nThe co-op report is due"),
    ("p01", "2026-10-03T10:05:00-07:00", "picture_note", "A whiteboard with the co-op schedule"),
    ("y01", "2026-10-10T20:15:00-07:00", "allen", "how did the weather look yesterday"),
    ("y02", "2026-10-10T20:16:00-07:00", "jarvis", "Mostly rain in the morning"),
    ("t01", "2026-10-11T09:30:00-07:00", "allen", "what is on my calendar today"),
    ("t02", "2026-10-11T09:31:00-07:00", "jarvis_live", "Two meetings this afternoon"),
]
LONG = "word " * 900  # 4500 characters of one answer


def _tool(path: Path) -> Tool:
    (tool,) = build_conversation_tool(path, ZONE, lambda: NOW)
    return tool


def _call(tool: Tool, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the model's arguments.
    return dict(tool.handler(args, cast("ToolContext", None)))


def _failure(tool: Tool, **args: Any) -> ToolError:  # noqa: ANN401 - the model's arguments.
    with pytest.raises(ToolError) as caught:
        _call(tool, **args)
    return caught.value


def _store(root: Path, extra: list[tuple[str, str, str, str]] | None = None) -> Path:
    path = root / "memory.db"
    with closing(open_memory_db(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
            [*RECORDS, *(extra or [])],
        )
        conn.execute(
            "INSERT INTO day_summaries (id, day, ts, summary, model, record_count, input_chars, "
            "output_chars) VALUES ('d1', '2026-10-01', '2026-10-02T05:00:00-07:00', ?, 'm', 4, 1, "
            "1)",
            (SUMMARY,),
        )
    return path


@pytest.fixture
def tool(tmp_path: Path) -> Tool:
    """The tool over the scripted history."""
    return _tool(_store(tmp_path))


def _size(result: dict[str, Any]) -> int:
    return len(json.dumps(result, ensure_ascii=False))


def test_the_tool_is_a_small_read_only_native_menu_entry(tool: Tool) -> None:
    """One L0 read for the model, on the menu from the first request, with a short brief."""
    assert tool.name == "conversation"
    assert tool.risk_level == "L0"
    assert tool.read_only
    assert not tool.deferred
    assert tool.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert len(tool.description) <= 600
    assert tool.description.isascii()
    assert set(tool.input_schema["properties"]) == {
        "query", "date", "to", "open", "order", "speaker", "page",
    }
    assert build_conversation_tool(None, ZONE) == ()


def test_a_query_returns_moments_with_who_when_and_an_excerpt(tool: Tool) -> None:
    """The summary leads; each moment is an id, a local time, a side and an excerpt."""
    got = _call(tool, query="shopify")
    assert got["summary"] == '2 lines hold "shopify"; 1-2 shown, newest first.'
    assert got["more"] is False
    assert got["moments"] == [
        {"id": "r03", "when": "Thu Oct 1, 3:12 pm", "who": "Allen",
         "excerpt": "remind me about the Shopify interview"},
        {"id": "r01", "when": "Thu Oct 1, 9:00 am", "who": "Allen",
         "excerpt": "找 co-op 的工作, 先看 Shopify"},
    ]


def test_several_words_match_any_and_the_rarer_one_ranks_first(tool: Tool) -> None:
    """`co-op` is in four lines and `whiteboard` in one: the line with both ranks above the rest."""
    got = _call(tool, query="co-op whiteboard")
    assert got["moments"][0]["id"] == "p01"
    assert got["moments"][0]["who"] == "picture note"
    assert "best match first, newest among equals" in got["summary"]
    assert {m["id"] for m in got["moments"]} == {"p01", "r06", "r02", "r01"}
    assert [m["who"] for m in got["moments"] if m["id"] == "r06"] == ["mail"]


def test_a_word_found_nowhere_is_named_so_the_next_try_changes_it(tool: Tool) -> None:
    """The model learns which of its words matched nothing, and what to do on no match at all."""
    assert "No line holds: zebra." in _call(tool, query="interview zebra")["summary"]
    none = _call(tool, query="zebra")
    assert none["moments"] == []
    assert none["summary"].startswith('No line holds "zebra".')
    assert "Try other words" in none["summary"]


def test_an_excerpt_is_cut_around_the_match(tmp_path: Path) -> None:
    """A long line shows up to 200 characters, starting before the first matching word."""
    text = "intro " * 60 + "the needle sits here " + "tail " * 60
    tool = _tool(_store(tmp_path, [("big", "2026-10-04T12:00:00-07:00", "jarvis", text)]))
    (moment,) = _call(tool, query="needle")["moments"]
    assert len(moment["excerpt"]) <= 202  # 200 and the two ellipses
    assert "needle" in moment["excerpt"]
    assert moment["excerpt"].startswith("…")
    assert moment["excerpt"].endswith("…")


def test_the_first_thing_he_said_in_a_month_is_date_to_oldest_and_speaker(tool: Tool) -> None:
    """'The first thing I told you last month' needs no words: the window's oldest lines of his."""
    got = _call(tool, date="2026-09-01", to="2026-09-30", order="oldest", speaker="Allen")
    assert got["summary"] == (
        "1 line said between Tue Sep 1 and Wed Sep 30; 1-1 shown, oldest first."
    )
    assert [m["id"] for m in got["moments"]] == ["s01"]
    everything = _call(tool, order="oldest", speaker="Allen")
    assert [m["id"] for m in everything["moments"]][:2] == ["s01", "r01"]


def test_a_query_inside_a_window_leaves_out_the_rest(tool: Tool) -> None:
    """A date limits a search to a day; the UTC-stored line counts on the day it was said."""
    got = _call(tool, query="laptop", date="2026-10-01")
    assert [m["id"] for m in got["moments"]] == ["r05"]
    assert got["moments"][0]["when"] == "Thu Oct 1, 7:30 pm"
    assert got["summary"].endswith("shown, newest first.")
    assert " on Thu Oct 1" in got["summary"]


def test_pages_of_eight_with_a_sentence_past_the_end(tmp_path: Path) -> None:
    """Twenty lines are pages of 8, 8 and 4; each page call differs from the last."""
    many = [
        (f"w{n:02d}", f"2026-09-{n + 4:02d}T10:00:00-07:00", "allen", f"drizzle report {n}")
        for n in range(1, 21)
    ]
    tool = _tool(_store(tmp_path, many))
    pages = [_call(tool, query="drizzle", page=n) for n in (1, 2, 3)]
    assert [len(p["moments"]) for p in pages] == [8, 8, 4]
    assert [p["more"] for p in pages] == [True, True, False]
    assert pages[0]["moments"][0]["id"] == "w20"
    assert pages[1]["moments"][0]["id"] == "w12"
    assert pages[2]["moments"][-1]["id"] == "w01"
    assert pages[1]["summary"].startswith('20 lines hold "drizzle"; 9-16 shown')
    ids = [m["id"] for p in pages for m in p["moments"]]
    assert len(ids) == len(set(ids)) == 20
    past = _call(tool, query="drizzle", page=4)
    assert past["moments"] == []
    assert past["summary"] == "There is no page 4 of these 20 lines; they end at page 3."


def test_a_day_is_its_saved_topics_in_time_order(tool: Tool) -> None:
    """A past day's summary gives its topics, without the record ids behind them."""
    got = _call(tool, date="2026-10-01")
    assert got["summary"] == "1 day of conversation on Thu Oct 1, 5 lines in all."
    assert got["days"] == [{
        "day": "Thu Oct 1", "lines": 5,
        "topics": ["Looking for co-op jobs, Shopify first", "The Shopify interview reminder"],
    }]


def test_a_day_without_a_summary_shows_his_own_lines_and_says_so(tool: Tool) -> None:
    """Today and yesterday have no written summary yet: topics are his lines, marked as such."""
    got = _call(tool, date="today")
    assert got["days"] == [{
        "day": "today (Sun)", "lines": 2, "topics": ["9:30 am what is on my calendar today"],
        "note": "no written summary of this day; these are his own lines",
    }]
    assert _call(tool, date="yesterday")["days"][0]["day"] == "yesterday (Sat)"


def test_a_span_lists_the_days_with_talk_and_counts_the_quiet_ones(tool: Tool) -> None:
    """Each day with lines is an entry; days with none are counted, not listed."""
    got = _call(tool, date="2026-10-01", to="2026-10-03")
    assert [d["day"] for d in got["days"]] == ["Thu Oct 1", "Sat Oct 3"]
    assert got["summary"] == (
        "2 days of conversation between Thu Oct 1 and Sat Oct 3, 7 lines in all; "
        "1 quiet day left out."
    )
    got = _call(tool, date="2026-10-02", to="2026-10-09")
    assert [d["day"] for d in got["days"]] == ["Sat Oct 3"]
    assert "7 quiet days left out" in got["summary"]
    assert _call(tool, date="2026-10-05")["summary"] == "Nothing was said on Mon Oct 5."


def test_a_span_of_many_busy_days_is_trimmed_to_four_thousand_characters(tmp_path: Path) -> None:
    """Fourteen days of four long topics each narrow to fewer, shorter topics and still fit."""
    topics = "\n".join(f"- {'topic ' * 40}number {n} [record_id=x]" for n in range(6))
    days = [f"2026-09-{n:02d}" for n in range(10, 24)]
    rows = [(f"b{n}", f"{d}T10:00:00-07:00", "allen", "hello") for n, d in enumerate(days)]
    path = _store(tmp_path, rows)
    with closing(open_memory_db(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO day_summaries (id, day, ts, summary, model, record_count, input_chars, "
            "output_chars) VALUES (?, ?, ?, ?, 'm', 1, 1, 1)",
            [(f"s{d}", d, f"{d}T23:00:00-07:00", f"### Topics\n{topics}\n") for d in days],
        )
    got = _call(_tool(path), date=days[0], to=days[-1])
    assert len(got["days"]) == 14
    assert _size(got) <= 4000
    assert all(len(d["topics"]) >= 1 for d in got["days"])


def test_open_returns_the_exchange_around_a_line(tool: Tool) -> None:
    """Two lines before and after, in order, the asked one marked."""
    got = _call(tool, open="r03")
    assert [t["id"] for t in got["turns"]] == ["r01", "r02", "r03", "r04", "r05"]
    assert [t["id"] for t in got["turns"] if t.get("here")] == ["r03"]
    assert got["summary"] == "Allen, Thu Oct 1, 3:12 pm: this line with 2 before and 2 after."
    assert got["turns"][3] == {
        "id": "r04", "when": "Thu Oct 1, 3:13 pm", "who": "Jarvis",
        "text": "Okay, I will remind you about the interview",
    }
    edge = _call(tool, open="s01")
    assert [t["id"] for t in edge["turns"]] == ["s01", "s02", "r01"]
    assert edge["summary"].endswith("0 before and 2 after.")


def test_open_cuts_a_huge_line_and_keeps_the_answer_under_four_thousand(tmp_path: Path) -> None:
    """The asked line is shown up to what fits with its neighbours, and says how much is not."""
    rows = [
        ("h0", "2026-10-05T10:00:00-07:00", "allen", "q " * 400),
        ("h1", "2026-10-05T10:01:00-07:00", "jarvis", LONG * 10),
        ("h2", "2026-10-05T10:02:00-07:00", "allen", "w " * 400),
    ]
    got = _call(_tool(_store(tmp_path, rows)), open="h1")
    assert _size(got) <= 4000
    here = next(t for t in got["turns"] if t.get("here"))
    assert "more characters)" in here["text"]
    assert len(here["text"]) > 1000
    assert all("more characters)" in t["text"] or len(t["text"]) <= 300 for t in got["turns"])


def test_times_are_his_local_time_with_weekday_and_today_or_yesterday(tool: Tool) -> None:
    """Today and yesterday are named; an older day is a date with its weekday."""
    when = {m["id"]: m["when"] for m in _call(tool, query="weather calendar garden")["moments"]}
    assert when == {
        "y01": "yesterday (Sat) 8:15 pm",
        "t01": "today (Sun) 9:30 am",
        "s01": "Thu Sep 3, 8:00 am",
        "s02": "Thu Sep 3, 8:01 am",
    }


@pytest.mark.parametrize(
    ("arguments", "words"),
    [
        ({}, ["pass query=", "open=<id>"]),
        ({"page": 2}, ["pass query="]),
        ({"date": "2026-10-01", "page": 2}, ["page continues a list of lines"]),
        ({"query": "x", "page": 0}, ["page must be a whole number"]),
        ({"query": "x", "page": "two"}, ["page must be a whole number"]),
        ({"to": "2026-10-03"}, ["to is the last day of a span", "date=..."]),
        ({"open": "r01", "query": "x"}, ["open=<id> alone"]),
        ({"date": "last week"}, ["a date looks like 2026-10-01", "today, yesterday"]),
        ({"date": "2026-10-20"}, ["has not happened yet; today is 2026-10-11"]),
        ({"date": "2026-10-05", "to": "2026-10-01"}, ["is before date=", "last day"]),
        ({"date": "2026-09-01", "to": "2026-10-11"}, ["at most 14 days", "order=oldest"]),
        ({"query": "x", "order": "first"}, ["order is newest or oldest"]),
        ({"query": "x", "speaker": "me"}, ["speaker is Allen or Jarvis"]),
        ({"keyword": "x"}, ["no argument 'keyword'", "query, date, to, open"]),
        ({"open": "nope"}, ["No line with id nope", "conversation(query=...)"]),
    ],
)
def test_a_wrong_call_says_what_happened_and_what_to_pass(
    tool: Tool, arguments: dict[str, Any], words: list[str],
) -> None:
    """Every refusal names the fix, so the model's next call differs from the failed one."""
    error = _failure(tool, **arguments)
    assert all(word in str(error) for word in words), str(error)
    assert error.code in {"invalid_arguments", "not_found"}


def test_a_missing_store_is_one_clear_error(tmp_path: Path) -> None:
    """History that cannot be read says so and tells the model to tell him."""
    error = _failure(_tool(tmp_path / "absent.db"), query="x")
    assert error.code == "unavailable"
    assert "tell him" in str(error)


def test_a_result_never_outgrows_four_thousand_characters(tmp_path: Path) -> None:
    """Eight long lines on a page are excerpts: the page stays small whatever was said."""
    rows = [
        (f"z{n}", f"2026-09-{n + 4:02d}T10:00:00-07:00", "jarvis", ("giant " + "x" * 5000))
        for n in range(1, 13)
    ]
    got = _call(_tool(_store(tmp_path, rows)), query="giant")
    assert len(got["moments"]) == 8
    assert _size(got) <= 4000


def test_the_menu_offers_conversation_and_not_the_three_tools_it_replaces(tmp_path: Path) -> None:
    """The shipped config: `conversation` is on the menu, the three old reads registered but off."""
    recorded = tmp_path / "mcp-tools.json"
    recorded.write_text("{}", encoding="utf-8")
    config = yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    names = {one["name"] for one in tool_exam.offered_tools(config, recorded)}
    assert "conversation" in names
    assert not names & {"search_records", "recall", "read_records"}
    registry = tool_exam._exam_registry(config, recorded)  # noqa: SLF001 - the registry behind the menu
    off = {t.name for t in registry.get_definitions() if t.deferred}
    assert {"search_records", "recall", "read_records"} <= off


def test_tool_search_for_an_old_name_says_to_call_conversation(tmp_path: Path) -> None:
    """The old reads are deferred but not indexed by tool_search: naming one still gets the note."""
    recorded = tmp_path / "mcp-tools.json"
    recorded.write_text(json.dumps({"mcp__notion__notion_search": {
        "name": "mcp__notion__notion_search", "description": "Search Notion pages",
        "input_schema": {"type": "object"},
    }}), encoding="utf-8")
    config = yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    registry = tool_exam._exam_registry(config, recorded)  # noqa: SLF001 - the registry behind the menu
    assert {k: v for k, v in registry.superseded().items() if v[0] == "conversation"} == {
        "search_records": ("conversation", "conversation(query=...)"),
        "recall": ("conversation", "conversation(date=...)"),
        "read_records": ("conversation", "conversation(open=<id>)"),
    }
    search = next(t for t in registry.get_definitions() if t.name == "tool_search")
    assert isinstance(search, Tool)

    def ask(query: str) -> dict[str, Any]:
        return _call(search, query=query)

    got = ask("search_records")
    assert "search_records" not in got["loaded_tools"]
    assert got["note"] == (
        "search_records is replaced by conversation, which is already in your tools: "
        "call conversation(query=...)."
    )
    assert "read_records is replaced by conversation" in ask("how to use read records")["note"]
    other = ask("notion pages")  # a query that names no old tool is answered as before
    assert other == {"loaded_tools": ["mcp__notion__notion_search"]}
