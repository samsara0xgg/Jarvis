"""ADR 0294, ADR 0199: what the Mac terminal shipped to the brain, on the day line.

Acceptance checks against the real shipper, the real intake and the real fold, on fixture data:

- the Mac lane read from ``shipped.db`` (TimeSink rows copied by the real mirror) is item for item
  the lane read live, for every Mac case of the day line's own table, so the two paths cannot
  drift apart;
- the live read is preferred, the shipped rows fill in when the Mac is away or has nothing for the
  stretch, and ``mac`` is missing only when neither has anything;
- Claude Code sessions become ``code`` items: clipped to the day, on both days across midnight,
  titled by the newest of the best title, one per session whatever its revisions, never a
  sidechain, and absent (and silent) with no ``shipped.db``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.state import imports
from jarvis.state.day_line import DaySources, day_window, fold_day, parse_day
from jarvis.state.doing import read_doing
from jarvis.state.event_log import open_event_log
from jarvis.state.import_parsers import Parsed
from jarvis.state.ledger import TerminalScreen
from jarvis.state.shipments import Shipments
from jarvis.surface.mac_ship import ShipConfig, Shipper
from jarvis.surface.mac_ship_core import State
from jarvis.surface.mac_ship_timesink import TimesinkConfig
from tests.support.day_line import (
    MAC_CASES,
    VANCOUVER,
    Call,
    MacCase,
    Spun,
    expected_items,
    make_stores,
    minutes_ms,
)
from tests.support.ledger import terminal_link

if TYPE_CHECKING:
    from pathlib import Path

ZONE = ZoneInfo(VANCOUVER)
MINUTE = 60_000


class _Intake:
    """The brain's intake, called in place of HTTP."""

    def __init__(self, shipments: Shipments) -> None:
        self.shipments = shipments

    def send_entries(self, source: str, body: bytes) -> None:
        self.shipments.receive_entries(source, body)

    def send_file(self, source: str, relpath: str, data: bytes) -> str:  # pragma: no cover
        raise AssertionError((source, relpath, len(data)))


def _ship(tmp_path: Path, timesink: Path, now_ms: int) -> Path:
    """Run the real mirror over a TimeSink-shaped store; the imports directory it shipped to.

    A real TimeSink always holds some state events, so the store has one, whatever the case.
    """
    with closing(sqlite3.connect(timesink)) as conn, conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS "stateEvent" '
            '("id" INTEGER PRIMARY KEY, "at" DATETIME NOT NULL, "kind" TEXT NOT NULL)',
        )
        conn.execute(
            "INSERT INTO stateEvent (at, kind) VALUES ('2026-10-10 00:00:00.000', 'start')",
        )
    directory = tmp_path / "brain" / imports.IMPORTS_DIRNAME
    config = ShipConfig(
        tmp_path / "mac" / "state.db", timesink=TimesinkConfig(timesink, images=False),
    )
    config.state_path.parent.mkdir(parents=True, exist_ok=True)
    report = Shipper(
        config, _Intake(Shipments(directory, min_free_bytes=0)), State(config.state_path),
    ).poll(now_ms)
    assert not report.failed
    return directory


def _fold(sources: DaySources, tmp_path: Path, day: str, now: int) -> Any:  # noqa: ANN401
    start, end = day_window(parse_day(day), ZONE)
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        return fold_day(conn, start, end, now, sources)


@pytest.mark.parametrize("case", MAC_CASES, ids=[case.name for case in MAC_CASES])
def test_the_shipped_mac_lane_is_the_live_lane_item_for_item(
    tmp_path: Path, case: MacCase,
) -> None:
    """Each Mac case, shipped by the real mirror and read with no live TimeSink at all."""
    start_ms, _ = day_window(parse_day("2026-10-10"), ZONE)
    live = make_stores(
        tmp_path, ZONE, origin=start_ms, spans=case.spans, calls=case.calls, talks=case.talks,
    )
    assert live.timesink is not None
    shipped = DaySources(
        ZONE, None, live.memory_db, None,
        shipped=_ship(tmp_path, live.timesink, minutes_ms(start_ms, case.now)),
    )
    now = minutes_ms(start_ms, case.now)

    from_live = _fold(live, tmp_path, "2026-10-10", now)
    from_shipped = _fold(shipped, tmp_path, "2026-10-10", now)

    assert from_shipped.items == from_live.items == expected_items(case.items, start_ms)
    assert from_live.missing == []
    assert from_shipped.missing == ["screen"]  # no iPhone or iPad record was shipped (ADR 0304)


def test_a_revised_row_counts_once_with_its_newest_end(tmp_path: Path) -> None:
    """A span the mirror shipped twice (its end grew) is one span, as it ends now."""
    start_ms, _ = day_window(parse_day("2026-10-10"), ZONE)
    live = make_stores(tmp_path, ZONE, origin=start_ms, spans=(Spun(540, 560),))
    assert live.timesink is not None
    directory = _ship(tmp_path, live.timesink, minutes_ms(start_ms, 561))
    with closing(sqlite3.connect(live.timesink)) as conn, conn:
        conn.execute("UPDATE span SET end = ?", ("2026-10-10 18:10:00.000",))  # 11:10 Vancouver
    # an old row that changed is shipped at once (the mirror's rule: it has settled)
    _ship(tmp_path, live.timesink, minutes_ms(start_ms, 3000))
    with closing(imports.open_shipped(directory)) as conn:
        assert len(imports.entries(conn, source="timesink", kinds=("span",))) == 2
        # the bounded scan finds the same newest rows as the whole one
        for kinds in (("span",), ("span", "stateEvent"), ("callSpan",)):
            assert imports.newest(conn, "timesink", kinds=kinds, by_prefix=True) == imports.newest(
                conn, "timesink", kinds=kinds,
            )

    line = _fold(
        DaySources(ZONE, None, None, None, shipped=directory), tmp_path, "2026-10-10",
        minutes_ms(start_ms, 3000),
    )

    assert [(i["kind"], i["start_ms"], i["end_ms"]) for i in line.items] == [
        ("work", minutes_ms(start_ms, 540), minutes_ms(start_ms, 670)),
    ]


def test_a_window_reads_each_row_it_touches_at_its_newest_revision(tmp_path: Path) -> None:
    """Bounded at both ends, newest reads only rows with a revision inside, with the same answer.

    A row whose newest revision moved into the window is in it; one whose newest moved out is not,
    though an older revision is still inside; a longer row id is never read as another revision.
    """
    revisions = [  # (row, revision, minute of at_ms), oldest first
        ("span:1", "a", 600), ("span:1", "b", 600),
        ("span:2", "a", 480), ("span:2", "b", 630),
        ("span:3", "a", 640), ("span:3", "b", 780),
        ("span:6", "a", 645), ("span:6", "b", 660), ("span:6:x", "c", 800),
        ("span:4", "a", 420),
        ("callSpan:5", "a", 610),
    ]
    with closing(imports.open_shipped(tmp_path / "imports")) as conn:
        imports.add_entries(conn, "digest", [
            Parsed("timesink", f"{row}:{rev * 12}", "0", minute * MINUTE, "UTC",
                   row.split(":")[0], "", {"rev": rev})
            for row, rev, minute in revisions
        ], imports.Counts())
        start, end = 540 * MINUTE, 720 * MINUTE
        for kinds, by_prefix in ((("span",), True), (("span",), False), ((), False)):
            bounded = imports.newest(
                conn, "timesink", start_ms=start, end_ms=end, kinds=kinds, by_prefix=by_prefix,
            )
            whole = imports.newest(conn, "timesink", kinds=kinds, by_prefix=by_prefix)
            assert bounded == [e for e in whole if start <= e.at_ms < end]
            assert [(e.kind, e.at_ms // MINUTE, e.fields["rev"]) for e in bounded] == [
                ("span", 600, "b"),
                *([("callSpan", 610, "a")] if not kinds else []),
                ("span", 630, "b"), ("span", 660, "b"),
            ]


def test_shared_reads_read_a_covered_window_once_and_a_row_shipped_since_anew(
    tmp_path: Path,
) -> None:
    """Inside the scope a window inside an earlier read, widened, is cut from it, row for row."""

    def ship(row: str, rev: str, minute: int) -> None:
        with closing(imports.open_shipped(tmp_path / "imports")) as conn, conn:
            imports.add_entries(conn, "digest", [
                Parsed("timesink", f"{row}:{rev * 12}", "0", minute * MINUTE, "UTC", "span", "",
                       {"rev": rev}),
            ], imports.Counts())

    for n, minute in enumerate((300, 450, 600, 640, 900, 1300)):
        ship(f"span:{n}", "a", minute)
    reads: list[str] = []
    asks = [(540, 720), (480, 780), (60, 960)]  # the last starts more than before_ms earlier
    with closing(sqlite3.connect(tmp_path / "imports" / imports.SHIPPED_DB)) as conn:
        plain = [
            imports.newest(conn, "timesink", start_ms=a * MINUTE, end_ms=b * MINUTE,
                           kinds=("span",), by_prefix=True)
            for a, b in asks
        ]
        conn.set_trace_callback(lambda sql: reads.append(sql) if "rowid IN" in sql else None)
        with imports.shared_reads(before_ms=120 * MINUTE, after_ms=60 * MINUTE):
            shared = [
                imports.newest(conn, "timesink", start_ms=a * MINUTE, end_ms=b * MINUTE,
                               kinds=("span",), by_prefix=True)
                for a, b in asks
            ]
            assert shared == plain
            assert len(reads) == 2  # 540-720 read as 420-780, which holds 480-780; 60-960 is not
            ship("span:1", "b", 610)  # the row at 450 moves into the window
            ship("span:6", "a", 700)
            again = imports.newest(conn, "timesink", start_ms=540 * MINUTE, end_ms=720 * MINUTE,
                                   kinds=("span",), by_prefix=True)
            assert len(reads) == 3
        assert [(e.at_ms // MINUTE, e.fields["rev"]) for e in again] == [
            (600, "a"), (610, "b"), (640, "a"), (700, "a"),
        ]
        assert again == imports.newest(conn, "timesink", start_ms=540 * MINUTE,
                                       end_ms=720 * MINUTE, kinds=("span",), by_prefix=True)


def test_the_live_read_is_preferred_and_the_shipped_one_fills_in_when_it_has_nothing(
    tmp_path: Path,
) -> None:
    """Live rows win over shipped ones; a live store with nothing for the day gives way."""
    start_ms, _ = day_window(parse_day("2026-10-10"), ZONE)
    for name in "abc":
        (tmp_path / name).mkdir()
    old = make_stores(tmp_path / "a", ZONE, origin=start_ms, spans=(Spun(540, 600),))
    assert old.timesink is not None
    directory = _ship(tmp_path, old.timesink, minutes_ms(start_ms, 700))
    live = make_stores(tmp_path / "b", ZONE, origin=start_ms, spans=(Spun(100, 160),))
    now = minutes_ms(start_ms, 700)

    both = DaySources(ZONE, live.timesink, None, None, shipped=directory)
    assert [i["start_ms"] for i in _fold(both, tmp_path, "2026-10-10", now).items] == [
        minutes_ms(start_ms, 100),
    ]
    # the live store holds nothing for the next day: the shipped lane (also empty) answers
    assert _fold(both, tmp_path, "2026-10-11", now).items == []
    empty = make_stores(tmp_path / "c", ZONE, origin=start_ms)
    gap = DaySources(ZONE, empty.timesink, None, None, shipped=directory)
    assert [i["start_ms"] for i in _fold(gap, tmp_path, "2026-10-10", now).items] == [
        minutes_ms(start_ms, 540),
    ]


def test_an_away_terminal_falls_back_to_the_shipped_rows(tmp_path: Path) -> None:
    """The brain's terminal link is down: the lane is the shipped one and ``mac`` is not missing."""
    start_ms, _ = day_window(parse_day("2026-10-10"), ZONE)
    one = make_stores(
        tmp_path, ZONE, origin=start_ms, spans=(Spun(540, 600),), calls=(Call(560, 580),)
    )
    assert one.timesink is not None
    directory = _ship(tmp_path, one.timesink, minutes_ms(start_ms, 700))
    link = terminal_link(one.timesink, [], up=[False])
    now = minutes_ms(start_ms, 700)
    away = DaySources(ZONE, None, one.memory_db, TerminalScreen(link))

    assert _fold(away, tmp_path, "2026-10-10", now).missing == ["mac"]

    covered = DaySources(ZONE, None, one.memory_db, TerminalScreen(link), shipped=directory)
    line = _fold(covered, tmp_path, "2026-10-10", now)
    assert line.missing == ["screen"]  # a shipped store with no iPhone or iPad record (ADR 0304)
    assert [i["kind"] for i in line.items] == ["work", "call"]


def test_mac_is_missing_when_the_shipped_store_has_no_timesink_rows(tmp_path: Path) -> None:
    """No file, no table, or only Claude Code rows: nothing of TimeSink was ever shipped."""
    start_ms, _ = day_window(parse_day("2026-10-10"), ZONE)
    now = minutes_ms(start_ms, 700)
    nothing = tmp_path / "nothing"
    assert "mac" in _fold(DaySources(ZONE, shipped=nothing), tmp_path, "2026-10-10", now).missing
    only_code = tmp_path / "code"
    _ship_sessions(only_code, [_session("s1", "2026-10-10T17:00:00Z", "2026-10-10T17:30:00Z")])
    assert "mac" in _fold(DaySources(ZONE, shipped=only_code), tmp_path, "2026-10-10", now).missing


# --- Claude Code sessions -----------------------------------------------------------------


def _session(  # noqa: PLR0913 - one shipped session record: its id, times and every title
    sid: str,
    first: str,
    last: str,
    *,
    account: str = "a",
    cwd: str = "/work/jarvis/sub",
    top: str | None = "/work/jarvis",
    custom: tuple[str, ...] = (),
    ai: tuple[str, ...] = (),
    agent: tuple[str, ...] = (),
    desktop: str | None = None,
    away: tuple[str, ...] = (),
    sidechain: bool = False,
    sha: str = "a" * 12,
) -> dict[str, Any]:
    """One entry line as ``mac_ship_sessions`` ships it (``fields`` are the session's record)."""
    return {
        "key": f"session:{account}:{sid}:{sha}",
        "kind": "session",
        "zone": "UTC",
        "at_ms": 0,
        "text": "",
        "fields": {
            "account": account,
            "session_id": sid,
            "cwd": cwd,
            "cwds": [cwd],
            "git_toplevel": top,
            "git_origin": None,
            "git_branches": ["main"],
            "first_timestamp": first,
            "last_timestamp": last,
            "is_sidechain": sidechain,
            "custom_title": list(custom),
            "ai_title": list(ai),
            "agent_name": list(agent),
            "desktop_title": desktop,
            "desktop_title_source": None,
            "away_summaries": [
                {"timestamp": last, "uuid": f"u{n}", "content": text} for n, text in enumerate(away)
            ],
            "user_prompts": 3,
            "assistant_messages": 3,
            "tool_uses": 5,
        },
    }


def _utc_ms(text: str) -> int:
    return int(datetime.fromisoformat(text).astimezone(UTC).timestamp() * 1000)


def _ship_sessions(directory: Path, sessions: list[dict[str, Any]]) -> Path:
    """Put session lines into a brain's ``shipped.db`` through the real intake."""
    for line in sessions:
        first = line["fields"]["first_timestamp"]
        line["at_ms"] = _utc_ms(first)
    body = b"\n".join(json.dumps(line).encode() for line in sessions)
    Shipments(directory, min_free_bytes=0).receive_entries("claude-code", body)
    return directory


def _code_items(line: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    return [i for i in line.items if i["kind"] == "code"]


def test_a_session_is_one_code_item_titled_projected_and_clipped_to_the_day(
    tmp_path: Path,
) -> None:
    """Title custom > desktop > ai > agent (the newest of each); project from the repo or cwd."""
    directory = _ship_sessions(
        tmp_path / "imports",
        [
            _session(
                "s-custom",
                "2026-10-10T17:00:00Z",
                "2026-10-10T18:00:00Z",
                custom=("old", "mine"),
                desktop="desk",
                ai=("ai",),
                away=("first", "latest"),
            ),
            _session(
                "s-desk",
                "2026-10-10T19:00:00Z",
                "2026-10-10T19:30:00Z",
                desktop="desk",
                ai=("ai",),
            ),
            _session(
                "s-ai",
                "2026-10-10T20:00:00Z",
                "2026-10-10T20:30:00Z",
                ai=("ai1", "ai2"),
                agent=("agent",),
                account="b",
                top=None,
                cwd="/Users/me/Projects/other/",
            ),
            _session("s-agent", "2026-10-10T21:00:00Z", "2026-10-10T21:10:00Z", agent=("a1", "a2")),
            _session("s-none", "2026-10-10T22:00:00Z", "2026-10-10T22:10:00Z"),
            {**(tree := _session(
                "s-tree", "2026-10-10T22:10:00Z", "2026-10-10T22:20:00Z",
                top="/Users/me/Projects/jarvis-b/.claude/worktrees/bridge-cse_01FK",
            )), "fields": {
                **tree["fields"], "git_origin": "https://github.com/samsara0xgg/Jarvis.git",
                "user_prompts": 0, "thread_title": "语音识别出错", "ai_title": ["ai guess"],
            }},
            _session(
                "s-side",
                "2026-10-10T23:00:00Z",
                "2026-10-10T23:10:00Z",
                custom=("x",),
                sidechain=True,
            ),
        ],
    )
    start_ms, _ = day_window(parse_day("2026-10-10"), ZONE)
    now = start_ms + 24 * 60 * MINUTE

    line = _fold(DaySources(ZONE, shipped=directory), tmp_path, "2026-10-10", now)

    items = {i["id"]: i for i in _code_items(line)}
    assert sorted(items) == [
        "code-s-agent",
        "code-s-ai",
        "code-s-custom",
        "code-s-desk",
        "code-s-none",
        "code-s-tree",
    ]
    custom = items["code-s-custom"]
    assert custom == {
        "id": "code-s-custom",
        "lane": "you",
        "kind": "code",
        "state": "done",
        "start_ms": _utc_ms("2026-10-10T17:00:00Z"),
        "end_ms": _utc_ms("2026-10-10T18:00:00Z"),
        "title": "mine",
        "project": "jarvis",
        "prompts": 3,
        "remote_session": None,
        "account": "a",
        "session_id": "s-custom",
        "detail": "latest",
    }
    assert items["code-s-desk"]["title"] == "desk"
    assert (items["code-s-ai"]["title"], items["code-s-ai"]["project"]) == ("ai2", "other")
    # a Remote Control bridge's session is told by the claude.ai session it ran for
    assert (items["code-s-tree"]["prompts"], items["code-s-tree"]["remote_session"]) == (
        0, "cse_01FK",
    )
    # named by the thread's opening brief, before Claude Code's own guess (ADR 0403)
    assert items["code-s-tree"]["title"] == "语音识别出错"
    assert items["code-s-ai"]["account"] == "b"
    assert items["code-s-agent"]["title"] == "a2"
    assert "detail" not in items["code-s-none"]
    assert items["code-s-none"]["title"] == "jarvis"  # no name anywhere: its folder (ADR 0403)
    assert "mac" in line.missing  # no TimeSink was shipped: unchanged by the sessions
    assert "code" not in line.missing


def test_a_bridge_session_with_no_name_of_its_own_takes_the_thread_names_table(
    tmp_path: Path,
) -> None:
    """``thread-names.json`` names a bridge by its ``cse_`` id; its own brief line wins."""
    bridge = "/Users/me/Projects/jarvis-b/.claude/worktrees/bridge-cse_{}"
    directory = _ship_sessions(tmp_path / "imports", [
        _session("s-old", "2026-10-10T17:00:00Z", "2026-10-10T17:10:00Z",
                 top=bridge.format("01OLD"), ai=("ai guess",)),
        {**(named := _session(
            "s-new", "2026-10-10T18:00:00Z", "2026-10-10T18:10:00Z", top=bridge.format("01NEW"),
        )), "fields": {**named["fields"], "thread_title": "brief"}},
        _session("s-gone", "2026-10-10T19:00:00Z", "2026-10-10T19:10:00Z",
                 top=bridge.format("01GONE")),
    ])
    (directory / "thread-names.json").write_text(json.dumps(
        {"cse_01OLD": "部署", "cse_01NEW": "renamed", "cse_01GONE": ""},
    ), encoding="utf-8")
    start_ms, _ = day_window(parse_day("2026-10-10"), ZONE)
    sources = DaySources(ZONE, shipped=directory)

    titles = {
        i["id"]: i["title"]
        for i in _code_items(_fold(sources, tmp_path, "2026-10-10", start_ms + 24 * 60 * MINUTE))
    }
    assert titles == {"code-s-old": "部署", "code-s-new": "brief", "code-s-gone": ""}
    (directory / "thread-names.json").write_text("not json", encoding="utf-8")
    again = _code_items(_fold(sources, tmp_path, "2026-10-10", start_ms + 24 * 60 * MINUTE))
    assert {i["id"]: i["title"] for i in again}["code-s-old"] == "ai guess"


def test_a_programs_one_call_to_claude_is_no_code_item(tmp_path: Path) -> None:
    """One prompt through the SDK and no claude.ai session: not shown; any more of him is."""
    def one(sid: str, prompts: int, entrypoints: list[str], top: str = "/work/coop") -> Any:  # noqa: ANN401
        line = _session(sid, "2026-10-10T17:00:00Z", "2026-10-10T17:01:00Z", top=top)
        line["fields"].update(user_prompts=prompts, entrypoints=entrypoints)
        return line

    bridge = "/Users/me/Projects/jarvis-b/.claude/worktrees/bridge-cse_01RC"
    directory = _ship_sessions(tmp_path / "imports", [
        one("helper", 1, ["sdk-cli"]),
        one("silent", 0, ["sdk-cli"]),
        one("asked", 2, ["sdk-cli"]),
        one("typed", 1, ["cli"]),
        one("rc", 1, ["sdk-cli"], top=bridge),
    ])
    start_ms, _ = day_window(parse_day("2026-10-10"), ZONE)
    line = _fold(DaySources(ZONE, shipped=directory), tmp_path, "2026-10-10",
                 start_ms + 24 * 60 * MINUTE)

    assert {i["id"]: i["title"] for i in _code_items(line)} == {
        "code-asked": "coop", "code-typed": "coop", "code-rc": "",
    }


def test_a_session_across_midnight_shows_on_both_days_clipped_with_one_id(tmp_path: Path) -> None:
    """Clipped to each day's window; the id is the session's on both."""
    directory = _ship_sessions(
        tmp_path / "imports",
        [
            _session("s1", "2026-10-11T06:00:00Z", "2026-10-11T10:00:00Z", custom=("late",)),
        ],
    )  # 23:00 on the 10th to 03:00 on the 11th, Vancouver
    first, second = day_window(date(2026, 10, 10), ZONE), day_window(date(2026, 10, 11), ZONE)
    sources = DaySources(ZONE, shipped=directory)
    now = second[1]

    one = _code_items(_fold(sources, tmp_path, "2026-10-10", now))
    two = _code_items(_fold(sources, tmp_path, "2026-10-11", now))
    other = _code_items(_fold(sources, tmp_path, "2026-10-12", now))

    began, ended = _utc_ms("2026-10-11T06:00:00Z"), _utc_ms("2026-10-11T10:00:00Z")
    assert [(i["id"], i["start_ms"], i["end_ms"]) for i in one] == [("code-s1", began, first[1])]
    assert [(i["id"], i["start_ms"], i["end_ms"]) for i in two] == [("code-s1", second[0], ended)]
    assert other == []


def test_a_session_shipped_again_is_still_one_item_with_its_newest_revision(
    tmp_path: Path,
) -> None:
    """Revisions of a growing session are rows with other hashes; the newest stands."""
    directory = tmp_path / "imports"
    _ship_sessions(
        directory,
        [
            _session(
                "s1", "2026-10-10T17:00:00Z", "2026-10-10T17:20:00Z", sha="1" * 12, ai=("t1",)
            ),
        ],
    )
    _ship_sessions(
        directory,
        [
            _session(
                "s1",
                "2026-10-10T17:00:00Z",
                "2026-10-10T18:00:00Z",
                sha="2" * 12,
                ai=("t1", "t2"),
                away=("recap",),
            ),
        ],
    )
    start_ms, _ = day_window(parse_day("2026-10-10"), ZONE)

    (item,) = _code_items(
        _fold(DaySources(ZONE, shipped=directory), tmp_path, "2026-10-10", start_ms + 86_000_000),
    )

    assert (item["title"], item["detail"], item["end_ms"]) == (
        "t2",
        "recap",
        _utc_ms("2026-10-10T18:00:00Z"),
    )


def test_without_a_shipped_store_there_are_no_code_items_and_nothing_is_missing_for_them(
    tmp_path: Path,
) -> None:
    """The sessions are an extra: their absence is not a named gap."""
    start_ms, _ = day_window(parse_day("2026-10-10"), ZONE)
    line = _fold(DaySources(ZONE, shipped=tmp_path / "none"), tmp_path, "2026-10-10", start_ms)
    assert _code_items(line) == []
    assert "code" not in line.missing


def test_the_day_route_serves_the_shipped_sessions_and_lane(tmp_path: Path) -> None:
    """``GET /inherent/day``'s reader is given the imports directory and shows what it holds."""
    from jarvis.runtime.daemon.routes import read_day  # noqa: PLC0415 - the route's own reader

    start_ms, _ = day_window(parse_day("2026-10-10"), ZONE)
    one = make_stores(tmp_path, ZONE, origin=start_ms, spans=(Spun(540, 600),))
    assert one.timesink is not None
    directory = _ship(tmp_path, one.timesink, minutes_ms(start_ms, 700))
    _ship_sessions(directory, [_session("s1", "2026-10-10T17:00:00Z", "2026-10-10T18:00:00Z")])
    with closing(open_event_log(tmp_path / "events.db")):
        pass

    body = read_day(
        tmp_path / "events.db",
        ZONE,
        None,
        None,
        date(2026, 10, 10),
        minutes_ms(start_ms, 700),
        shipped=directory,
    )

    assert sorted(i["kind"] for i in body["items"]) == ["code", "work"]
    assert "mac" not in body["missing"]


def _ship_away_notes(directory: Path, notes: list[tuple[int, str, str, str]]) -> Path:
    """TimeSink ``awayNote`` rows ``(id, start, end, label)``, UTC as GRDB writes them, shipped."""
    lines = [
        {
            "key": f"awayNote:{ident}:{'b' * 12}",
            "kind": "awayNote",
            "zone": "UTC",
            "at_ms": _utc_ms(start.replace(" ", "T") + "Z"),
            "text": label,
            "fields": {"id": ident, "start": start, "end": end, "label": label, "symbol": "pencil"},
        }
        for ident, start, end, label in notes
    ]
    body = b"\n".join(json.dumps(line).encode() for line in lines)
    Shipments(directory, min_free_bytes=0).receive_entries("timesink", body)
    return directory


def test_his_timesink_notes_are_away_items_and_only_clues_to_the_doing_lane(tmp_path: Path) -> None:
    """A note is an item of his lane clipped to the day; one line for a stretch, not his answer."""
    directory = _ship_away_notes(
        tmp_path / "imports",
        [
            (1, "2026-10-10 17:00:00.000", "2026-10-10 17:40:00.000", "lay down"),
            (2, "2026-10-10 06:30:00.000", "2026-10-10 07:30:00.000", "walking"),  # from 23:30 PDT
            (3, "2026-10-10 19:00:00.000", "2026-10-10 19:20:00.000", " "),  # no words: no note
        ],
    )
    start_ms, end_ms = day_window(parse_day("2026-10-10"), ZONE)
    now = end_ms + MINUTE
    line = _fold(DaySources(ZONE, shipped=directory), tmp_path, "2026-10-10", now)
    away = {i["id"]: i for i in line.items if i["kind"] == "away_note"}
    assert away == {
        "away-1": {
            "id": "away-1",
            "lane": "you",
            "kind": "away_note",
            "state": "done",
            "start_ms": _utc_ms("2026-10-10T17:00:00Z"),
            "end_ms": _utc_ms("2026-10-10T17:40:00Z"),
            "title": "lay down",
        },
        "away-2": {
            "id": "away-2",
            "lane": "you",
            "kind": "away_note",
            "state": "done",
            "start_ms": start_ms,
            "end_ms": _utc_ms("2026-10-10T07:30:00Z"),
            "title": "walking",
        },
    }
    served = read_doing(None, "2026-10-10", line.items, start_ms, end_ms, now).served
    said = [s for s in served if s["start_ms"] <= _utc_ms("2026-10-10T17:10:00Z") < s["end_ms"]]
    assert [(s["title"], s["confirmed"]) for s in said] == [(None, False)]
