"""ADR 0391: her answer links to the rows of his day it is about.

Acceptance checks over real event logs and stores, with no model but the scripted peer:

- the claims block's ``show`` lines are read in order, a row id keeps its case, and a ``show``
  line is no claim: it is not judged and not counted unread;
- ``my_day`` lists one day's rows with their ids and logs the ones it showed for the turn; a span
  of days, a store that cannot be read, or a tool built without the runtime's rows lists none;
- the rows are the day page's own: each id is a span of ``GET /inherent/day`` or an outing of
  ``GET /inherent/footprint``, the longest kept past the cap, by start;
- ``turn_links`` keeps only the rows she was shown this turn or the item his phone has open, at
  most three in her order, titled with her words cut to twenty characters;
- the whole path: a turn whose answer shows rows carries ``links`` on its
  ``surface.response_emitted`` row, which the phone's socket relays, and on its conversation row.
"""

# ruff: noqa: RUF001 - her words under test are Chinese, with fullwidth punctuation
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import claims as claims_check
from jarvis.execution.my_day_tool import ROWS_EVENT, build_my_day_tool
from jarvis.execution.tools import register_live_action, release_turn_actions
from jarvis.runtime import day_rows
from jarvis.runtime.daemon.output_watchers import add_places_and_links
from jarvis.runtime.daemon.routes import read_day
from jarvis.runtime.day_rows import DayRows
from jarvis.runtime.turn import start as start_module
from jarvis.runtime.turn.finish import turn_links
from jarvis.shared.receipts import Receipt
from jarvis.state import footprint
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface.cli import emit_surface_user_intent
from tests.support.clock import FakeClock
from tests.support.ledger import NOW, ledger_world
from tests.support.links import DAY, ORIGIN, ZONE, page_world
from tests.support.links import NOW_MS as PAGE_NOW_MS
from tests.support.routine import drive_streaming_turn, logged_rows
from tests.support.spoken import SpokenPeer, spoken_runtime
from tests.support.tool_context import tool_context

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from jarvis.execution.tools import Tool
    from jarvis.state.ledger import LedgerSources

MINUTE = 60_000
NOW_MS = int(NOW.timestamp() * 1000)


def _row(  # noqa: PLR0913 - a row's every field, all but its id defaulted
    row_id: str, start_min: int = 540, end_min: int = 600, *, kind: str = "row",
    title: str | None = "Corner Cafe", what: str = "stay", on: str = "2026-10-08",
) -> dict[str, Any]:
    """A row of 2026-10-08 (UTC-7) as the runtime folds it, its times minutes from midnight."""
    midnight = NOW_MS - 15 * 60 * MINUTE
    return {
        "kind": kind, "id": row_id, "date": on, "start_ms": midnight + start_min * MINUTE,
        "end_ms": midnight + end_min * MINUTE, "title": title, "what": what,
    }


def _link(row: dict[str, Any], title: str) -> dict[str, Any]:
    return {**{k: v for k, v in row.items() if k != "what"}, "title": title}


# --- the block's show lines ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "shown"),
    [
        (None, []),
        ("did r000001: set it", []),
        ("show stay-cafe: 那家咖啡馆", [("stay-cafe", "那家咖啡馆")]),
        # A row id keeps its case; the first line naming a row gives its words.
        (
            "show picture-AbC1: 那张照片\nshow stay-cafe: 咖啡\nshow stay-cafe: 又一次",
            [("picture-AbC1", "那张照片"), ("stay-cafe", "咖啡")],
        ),
        # A list mark and a full-width colon still read; two ids on a line share its words.
        ("- show doing-1, outing-2：上午", [("doing-1", "上午"), ("outing-2", "上午")]),
        ("show stay-cafe:", [("stay-cafe", "")]),
    ],
)
def test_show_lines_are_read_in_order_with_their_ids_as_written(
    block: str | None, shown: list[tuple[str, str]],
) -> None:
    """``shows`` gives each row a show line names, once, with the words after the colon."""
    assert claims_check.shows(block) == shown


def test_a_show_line_is_no_claim_so_it_is_neither_judged_nor_unread() -> None:
    """Only the ``did`` line is judged; a block of show lines alone has no verdict to fail."""
    done = Receipt("done", reads_only=False, line="Kept it", object="thing:aaaaaa")
    block = "show stay-cafe: 咖啡馆\ndid r000001: set it\nshow doing-1: 上午"
    verdicts = claims_check.check(block, shown={"r000001": done}, offerable=lambda _tool: False)
    assert [(v.entry.kind, v.ok) for v in verdicts] == [("did", True)]
    assert claims_check.check("show nothing-read: x", shown={}, offerable=lambda _t: True) == []
    assert claims_check.unread(block) == 0


# --- my_day lists a day's rows -------------------------------------------------------------------


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[tuple[LedgerSources, sqlite3.Connection]]:
    """The ledger's world and its event log, with action ``A1`` live in turn ``T1``."""
    src = ledger_world(tmp_path)
    conn = open_event_log(src.event_log)
    register_live_action(turn_id="T1", action_id="A1")
    try:
        yield src, conn
    finally:
        release_turn_actions("T1")
        conn.close()


def _my_day(src: LedgerSources, read: Callable[[date, int], list[dict[str, Any]]] | None) -> Tool:
    (tool,) = build_my_day_tool(src, read)
    return tool


def _shown(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [payload for _, _, payload, _, _ in logged_rows(conn, ROWS_EVENT)]


def test_a_day_lists_its_rows_with_ids_and_logs_them_for_the_turn(
    ledger: tuple[LedgerSources, sqlite3.Connection],
) -> None:
    """Today's rows as lines she reads, and the rows themselves on the turn, for its links."""
    src, conn = ledger
    asked: list[tuple[date, int]] = []
    found = [
        _row("stay-cafe"),
        _row("outing-1", 600, 620, kind="outing", title="walking to Home", what="outing"),
        _row("doing-2", 620, 15 * 60, title=None, what="doing"),
    ]

    def read(day: date, now_ms: int) -> list[dict[str, Any]]:
        asked.append((day, now_ms))
        return found

    out = _my_day(src, read).handler({}, tool_context(conn, "A1", clock=FakeClock(NOW)))
    assert asked == [(date(2026, 10, 8), NOW_MS)]
    assert out["timeline"] == [
        "9:00 am-10:00 am stay: Corner Cafe (id stay-cafe)",
        "10:00 am-10:20 am outing: walking to Home (id outing-1)",
        "10:20 am-now doing (id doing-2)",
    ]
    [logged] = _shown(conn)
    assert logged == {
        "turn_id": "T1", "action_id": "A1",
        "rows": [{k: v for k, v in row.items() if k != "what"} for row in found],
    }


def test_a_past_day_lists_its_rows_too_and_a_span_lists_none(
    ledger: tuple[LedgerSources, sqlite3.Connection],
) -> None:
    """One day asked by date has rows; a span of days is figures only and logs nothing."""
    src, conn = ledger
    tool = _my_day(src, lambda _day, _now: [_row("stay-cafe", on="2026-10-06")])
    context = tool_context(conn, "A1", clock=FakeClock(NOW))
    past = tool.handler({"date": "2026-10-06"}, context)
    assert past["timeline"] == ["9:00 am-10:00 am stay: Corner Cafe (id stay-cafe)"]
    span = tool.handler({"date": "2026-10-06", "to": "2026-10-07"}, context)
    assert "timeline" not in span
    about = tool.handler({"date": "2026-10-06", "about": "Jarvis"}, context)
    assert "timeline" not in about
    assert len(_shown(conn)) == 1


def test_rows_that_cannot_be_read_or_no_runtime_leave_the_day_without_them(
    ledger: tuple[LedgerSources, sqlite3.Connection],
) -> None:
    """The day's figures still answer; no row is logged that she was not shown."""
    src, conn = ledger

    def broken(_day: date, _now_ms: int) -> list[dict[str, Any]]:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    context = tool_context(conn, "A1", clock=FakeClock(NOW))
    for tool in (_my_day(src, broken), _my_day(src, None)):
        out = tool.handler({}, context)
        assert out["summary"].startswith("Today so far")
        assert "timeline" not in out
    assert _shown(conn) == []


def test_rows_cut_to_fit_the_answer_are_not_logged_as_shown(
    ledger: tuple[LedgerSources, sqlite3.Connection],
) -> None:
    """Past the answer's 4000 characters the last rows go, and only the rows left are logged."""
    src, conn = ledger
    many = [_row(f"stay-{n}", title="x" * 120) for n in range(60)]
    out = _my_day(src, lambda _day, _now: many).handler(
        {}, tool_context(conn, "A1", clock=FakeClock(NOW)),
    )
    assert 0 < len(out["timeline"]) < len(many)
    assert "cut" in out
    [logged] = _shown(conn)
    assert [row["id"] for row in logged["rows"]] == [row["id"] for row in many][
        : len(out["timeline"])
    ]


# --- the rows are the day page's own ------------------------------------------------------------


def test_each_row_is_a_span_of_the_day_page_or_an_outing_of_its_map(tmp_path: Path) -> None:
    """The same ids the phone shows, by start; a move gives way to its outing."""
    log, sources = page_world(tmp_path)
    found = DayRows(log, sources).read(DAY, PAGE_NOW_MS)
    page = read_day(log, ZONE, sources.memory_db, sources.timesink, DAY, PAGE_NOW_MS)
    with closing(open_event_log(log)) as conn:
        start_ms, end_ms = ORIGIN, ORIGIN + 24 * 60 * MINUTE
        outings = footprint.fold(conn, start_ms, end_ms, PAGE_NOW_MS, [])["outings"]
    spans = {span["id"] for span in page["spans"]}
    assert {row["id"] for row in found if row["kind"] == "row"} <= spans
    assert [row["id"] for row in found if row["kind"] == "outing"] == [o["id"] for o in outings]
    assert [row["start_ms"] for row in found] == sorted(row["start_ms"] for row in found)
    by_id = {row["id"]: row for row in found}
    assert by_id["stay-cafe"]["title"] == "Corner Cafe"
    assert by_id["stay-library"]["end_ms"] == PAGE_NOW_MS, "one still going ends now"
    assert by_id[outings[0]["id"]]["title"] == "walking to Corner Cafe"
    assert {row["what"] for row in found} == {"sleep", "stay", "doing", "outing", "call"}
    assert "walk-1" not in by_id
    assert all(row["date"] == "2026-10-14" for row in found)


def test_past_the_cap_the_longest_rows_are_kept_in_their_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With room for three: the night, the library and his morning's work, by start."""
    log, sources = page_world(tmp_path)
    every = DayRows(log, sources).read(DAY, PAGE_NOW_MS)
    monkeypatch.setattr(day_rows, "MAX_ROWS", 3)
    kept = DayRows(log, sources).read(DAY, PAGE_NOW_MS)
    longest = sorted(every, key=lambda row: row["start_ms"] - row["end_ms"])[:3]
    assert kept == sorted(longest, key=lambda row: (row["start_ms"], row["id"]))


# --- the links of a turn ------------------------------------------------------------------------


def _checked(
    conn: sqlite3.Connection, turn: str, show: Sequence[tuple[str, str]], *,
    sent_back: bool = False,
) -> None:
    payload: dict[str, Any] = {
        "turn_id": turn, "had_block": True, "entries": [], "sent_back": sent_back,
    }
    if show:
        payload["show"] = [{"id": row, "what": what} for row, what in show]
    emit_event(conn, type="answer.claims_checked", payload=payload, correlation={"turn_id": turn})


def _read(conn: sqlite3.Connection, turn: str, found: Sequence[dict[str, Any]]) -> None:
    emit_event(
        conn, type=ROWS_EVENT,
        payload={
            "turn_id": turn, "action_id": "A1",
            "rows": [{k: v for k, v in row.items() if k != "what"} for row in found],
        },
        correlation={"turn_id": turn},
    )


CAFE, WALK = _row("stay-cafe"), _row("outing-1", 600, 620, kind="outing", title="walking home")
WORK = _row("doing-2", 620, 700, title="jarvis", what="doing")
OPENED = {
    "kind": "row", "id": "picture-AbC1", "date": "2026-10-08", "start_ms": 1, "end_ms": 1,
    "title": "Sunset",
}


@pytest.mark.parametrize(
    ("show", "links", "about_reads"),
    [
        ([("stay-cafe", "那家咖啡馆")], [_link(CAFE, "那家咖啡馆")], 0),
        # Her order, not the log's; her words, else the row's own title.
        (
            [("doing-2", "写 Jarvis"), ("stay-cafe", "")],
            [_link(WORK, "写 Jarvis"), _link(CAFE, "Corner Cafe")],
            0,
        ),
        # A row she was not shown is dropped without a word; the open item is read once.
        ([("stay-elsewhere", "别处"), ("stay-nowhere", "也没有")], [], 1),
        (
            [("picture-AbC1", "那张日落"), ("stay-elsewhere", "别处")],
            [{**OPENED, "title": "那张日落"}],
            1,
        ),
        # At most three; a long name is cut to twenty characters.
        (
            [
                ("stay-cafe", "一" * 25), ("outing-1", "走回家"), ("doing-2", "写"),
                ("picture-AbC1", "日落"),
            ],
            [_link(CAFE, "一" * 19 + "…"), _link(WALK, "走回家"), _link(WORK, "写")],
            0,
        ),
    ],
)
def test_only_rows_she_was_shown_or_the_open_item_become_links(
    tmp_path: Path, show: list[tuple[str, str]], links: list[dict[str, Any]], about_reads: int,
) -> None:
    """``turn_links`` over the turn's logged rows and the item his phone has open."""
    reads: list[int] = []

    def about() -> dict[str, Any] | None:
        reads.append(1)
        return dict(OPENED)

    with closing(open_event_log(tmp_path / "events.db")) as conn:
        _read(conn, "T1", [CAFE, WALK])
        _read(conn, "T1", [WORK])
        _read(conn, "T2", [_row("stay-elsewhere")])
        _checked(conn, "T1", show)
        assert turn_links(conn, "T1", about) == links
    assert len(reads) == about_reads


def test_an_answer_sent_back_or_without_a_block_has_no_links(tmp_path: Path) -> None:
    """The last checked reply is the answer; one that was sent back and not replaced has none."""
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        _read(conn, "T1", [CAFE])
        assert turn_links(conn, "T1", lambda: None) == []
        _checked(conn, "T1", [("stay-cafe", "咖啡馆")], sent_back=True)
        assert turn_links(conn, "T1", lambda: None) == []
        _checked(conn, "T1", [])
        assert turn_links(conn, "T1", lambda: None) == []
        _checked(conn, "T1", [("stay-cafe", "咖啡馆")])
        assert turn_links(conn, "T1", lambda: None) == [_link(CAFE, "咖啡馆")]


# --- the whole path ------------------------------------------------------------------------------


@pytest.fixture
def _spoken_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTINE_STREAM_FIXTURE_KEY", "synthetic")
    monkeypatch.setattr(start_module, "labels_phases", lambda _base_url: True)


@pytest.mark.usefixtures("_spoken_route")
def test_the_answer_row_and_its_conversation_row_carry_the_links(tmp_path: Path) -> None:
    """Through ``drive_turn``: the row the phone's socket relays, and history, have the links."""
    answers = [
        # The fixture streams no newline, so both rows are on one show line; doing-9 was never
        # read this turn.
        [("final_answer", "<claims>show stay-cafe, doing-9: 街角咖啡馆</claims>"
                          "你上午在街角咖啡馆待了一个小时。")],
        [("final_answer", "没问题。")],
    ]
    with SpokenPeer(answers) as peer:
        runtime = spoken_runtime(tmp_path, peer.url)
        _read(runtime.conn, "T-links", [CAFE])
        for turn in ("T-links", "T-none"):
            intent = emit_surface_user_intent(
                runtime.conn, transcript="我上午在哪", turn_id=turn, channel="inherent_ptt",
                ingestion_node="iphone",
            )
            drive_streaming_turn(runtime, intent)
    answered = {
        payload["turn_id"]: (uid, payload)
        for _, _, payload, _, uid in logged_rows(runtime.conn, "surface.response_emitted")
    }
    uid, linked = answered["T-links"]
    assert linked["links"] == [_link(CAFE, "街角咖啡馆")]
    assert "links" not in answered["T-none"][1]
    assert "街角咖啡馆待了" in linked["text"]
    assert "claims" not in linked["text"]
    history: list[dict[str, object]] = [
        {"id": uid, "source": "jarvis"}, {"id": answered["T-none"][0], "source": "jarvis"},
    ]
    add_places_and_links(runtime.runtime_paths.event_log, history)
    assert history[0]["links"] == [_link(CAFE, "街角咖啡馆")]
    assert "links" not in history[1]
