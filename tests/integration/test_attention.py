"""ADR 0314: where his attention went, read from what his Mac and his devices had in front.

Checks, input to expected output:

- at any moment his attention is in one place: the Mac while TimeSink counts him there, else the
  app in front on his phone or tablet (shared evenly while both have one); a device's app while
  he is at the Mac is alongside; typing goes with the time it was typed in;
- the keys: a site by app and domain, else document, else title; a device app by its bundle, the
  same on both devices; a key under half a minute is not listed but still counted;
- threads: a key nobody told is its own thread; keys told to serve one thread are summed as it;
  a key told to serve what is around it goes to the thread that held most within fifteen minutes
  of it, and stays itself with none;
- rows: a lead change is cut only when the new lead holds more than ten minutes the row around it
  would leave out; a short excursion stays inside as the others' minutes and a switch;
- ``GET /inherent/day``: every doing stretch tells its own attention, the day before's tail too;
  a planned one tells nothing; with nothing shipped, a stretch tells no attention.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.runtime.doing_day import DayDoing, Lane
from jarvis.runtime.inherent_loop import _read_day
from jarvis.state import attention, imports
from jarvis.state.day_line import DaySources
from jarvis.state.doing import Doing
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import open_memory_db
from jarvis.state.shipments import Shipments

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

VANCOUVER = ZoneInfo("America/Vancouver")
DAY = date(2026, 10, 7)
MIN = 60_000


def _at(hour: int, minute: int = 0, *, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hour, minute), VANCOUVER)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp()) * 1000


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.000")


type _Mac = tuple[datetime, datetime, dict[str, Any]]
type _App = tuple[str, str, datetime, datetime]


def _ship(directory: Path, mac: Sequence[_Mac], apps: Sequence[_App] = ()) -> DaySources:
    """TimeSink spans and device app stretches as the Mac terminal ships them."""
    spans = [
        {
            "key": f"span:{n}:{'b' * 12}", "kind": "span", "zone": "UTC", "at_ms": _ms(a),
            "text": "",
            "fields": {
                "id": n, "deviceID": None, "appName": "Claude", "domain": None, "document": None,
                "title": None, "keySeconds": 0, **fields, "start": _utc(a), "end": _utc(b),
            },
        }
        for n, (a, b, fields) in enumerate(mac, 1)
    ]
    names = {"com.tencent.xin": "WeChat", "com.ss.iphone.ugc.Aweme": "抖音"}
    rows = [
        {
            "key": f"{imports.DEVICE_APP}:{device}:{bundle}:{_ms(a)}:{'c' * 12}",
            "kind": imports.DEVICE_APP, "zone": "UTC", "at_ms": _ms(a), "text": "",
            "fields": {
                "device_id": device, "device": device, "bundle_id": bundle, "start_ms": _ms(a),
                "end_ms": _ms(b), "unpaired": False,
            },
        }
        for device, bundle, a, b in apps
    ] + [
        {
            "key": f"{imports.DEVICE_NAME}:{bundle}:{'d' * 12}", "kind": imports.DEVICE_NAME,
            "zone": "UTC", "at_ms": 0, "text": name,
            "fields": {"bundle_id": bundle, "app": name, "category": None},
        }
        for bundle, name in names.items()
    ]
    shipments = Shipments(directory, min_free_bytes=0)
    if spans:
        shipments.receive_entries("timesink", "\n".join(map(json.dumps, spans)).encode())
    if rows:
        shipments.receive_entries(imports.DEVICE_SOURCE, "\n".join(map(json.dumps, rows)).encode())
    return DaySources(VANCOUVER, shipped=directory)


def _told(
    sources: DaySources, start: datetime, end: datetime,
    threads: dict[str, attention.Thread] | None = None,
) -> dict[str, Any]:
    found = attention.read(sources, _ms(start), _ms(end))
    return attention.within(found, _ms(start), _ms(end), threads or {})


def _minutes(told: dict[str, Any], part: str) -> dict[str, float]:
    return {one["name"]: one["ms"] / MIN for one in told[part]}


# --- one place at a time -----------------------------------------------------------------------


def test_the_mac_holds_his_attention_and_his_phone_fills_what_it_leaves(tmp_path: Path) -> None:
    """Mac 10:00-10:30; the phone 10:20-10:50 is alongside to 10:30, then his attention."""
    sources = _ship(
        tmp_path / "imports",
        [(_at(10), _at(10, 30), {"document": "Jarvis - Claude Code", "keySeconds": 120})],
        [("iPhone", "com.tencent.xin", _at(10, 20), _at(10, 50))],
    )
    told = _told(sources, _at(10), _at(11))
    assert told["front_ms"] == 50 * MIN
    jarvis, wechat = "mac|Claude|Jarvis - Claude Code", "app|com.tencent.xin"
    assert told["threads"] == [
        {"id": jarvis, "name": "Claude Jarvis - Claude Code", "category": None, "where": "mac",
         "ms": 30 * MIN, "typed_ms": 120_000,
         "keys": [{"key": jarvis, "name": "Claude Jarvis - Claude Code", "ms": 30 * MIN}]},
        {"id": wechat, "name": "WeChat", "category": None, "where": "iPhone", "ms": 20 * MIN,
         "typed_ms": 0, "keys": [{"key": wechat, "name": "WeChat", "ms": 20 * MIN}]},
    ]
    assert told["alongside"] == [
        {"id": wechat, "name": "WeChat", "category": None, "where": "iPhone", "ms": 10 * MIN,
         "keys": [{"key": wechat, "name": "WeChat", "ms": 10 * MIN}]},
    ]


def test_two_devices_in_front_at_once_share_his_attention(tmp_path: Path) -> None:
    """Both his iPhone and iPad in front 21:00-21:20: ten minutes each; one key on both."""
    sources = _ship(tmp_path / "imports", [], [
        ("iPhone", "com.ss.iphone.ugc.Aweme", _at(21), _at(21, 20)),
        ("iPad", "com.tencent.xin", _at(21), _at(21, 20)),
        ("iPad", "com.ss.iphone.ugc.Aweme", _at(21, 20), _at(21, 30)),
    ])
    told = _told(sources, _at(21), _at(22))
    assert _minutes(told, "threads") == {"抖音": 20.0, "WeChat": 10.0}
    assert told["front_ms"] == 30 * MIN
    assert told["alongside"] == []


def test_typing_goes_with_the_time_it_was_typed_in_and_keys_follow_site_document_title(
    tmp_path: Path,
) -> None:
    """A span over the edge of the stretch keeps the part of its typing inside it."""
    sources = _ship(tmp_path / "imports", [
        (_at(9, 50), _at(10, 10), {"appName": "Ghostty", "title": "jarvis", "keySeconds": 600}),
        (_at(10, 10), _at(10, 20), {
            "appName": "Chrome", "domain": "github.com", "title": "PR 12", "keySeconds": 30,
        }),
        (_at(10, 20), _at(10, 20) + timedelta(seconds=20), {"appName": "Finder"}),
    ])
    told = _told(sources, _at(10), _at(11))
    assert [(k["id"], k["name"], k["ms"], k["typed_ms"]) for k in told["threads"]] == [
        ("mac|Chrome|github.com", "Chrome github.com (PR 12)", 10 * MIN, 30_000),
        ("mac|Ghostty|jarvis", "Ghostty jarvis", 10 * MIN, 300_000),
    ]  # as long as each other: by key
    assert told["front_ms"] == 20 * MIN + 20_000  # the Finder's 20 s are counted, not listed


# --- rows ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("excursion_min", "rows"),
    [
        (8, [("10:00", "11:00", "Ghostty jarvis", 52, 8, 1)]),
        (30, [
            ("10:00", "10:15", "Ghostty jarvis", 15, 0, 0),
            ("10:15", "10:45", "Chrome youtube.com", 30, 0, 0),
            ("10:45", "11:00", "Ghostty jarvis", 15, 0, 0),
        ]),
    ],
)
def test_a_row_is_cut_only_where_the_new_lead_puts_more_than_ten_minutes_right(
    tmp_path: Path, excursion_min: int, rows: list[tuple[str, str, str, int, int, int]],
) -> None:
    """An eight-minute excursion stays in the row as others and a switch; thirty minutes cut."""
    start = 30 - excursion_min // 2
    away = (_at(10, start), _at(10, start + excursion_min))
    sources = _ship(tmp_path / "imports", [
        (_at(10), away[0], {"appName": "Ghostty", "title": "jarvis"}),
        (*away, {"appName": "Chrome", "domain": "youtube.com"}),
        (away[1], _at(11), {"appName": "Ghostty", "title": "jarvis"}),
    ])
    told = _told(sources, _at(10), _at(11))
    names = {k["id"]: k["name"] for k in told["threads"]}
    got = [
        (
            f"{datetime.fromtimestamp(r['start_ms'] / 1000, VANCOUVER):%H:%M}",
            f"{datetime.fromtimestamp(r['end_ms'] / 1000, VANCOUVER):%H:%M}",
            names[r["thread"]], r["ms"] // MIN, r["others_ms"] // MIN, r["switches"],
        )
        for r in told["rows"]
    ]
    assert got == rows


def test_a_row_runs_from_the_first_to_the_last_moment_a_screen_had_him(tmp_path: Path) -> None:
    """No screen before 10:07 or after 10:40: the row is 10:07-10:40, not the whole stretch."""
    sources = _ship(tmp_path / "imports", [
        (_at(10, 7), _at(10, 40), {"appName": "Ghostty", "title": "jarvis"}),
    ])
    told = _told(sources, _at(9), _at(12))
    assert [(r["start_ms"], r["end_ms"]) for r in told["rows"]] == [
        (_ms(_at(10, 7)), _ms(_at(10, 40))),
    ]


def test_with_nothing_shipped_a_stretch_tells_no_attention(tmp_path: Path) -> None:
    """No imports directory: nothing in front, no keys, no rows."""
    told = _told(DaySources(VANCOUVER, shipped=tmp_path / "none"), _at(10), _at(11))
    assert told == {"front_ms": 0, "threads": [], "rows": [], "alongside": []}


# --- threads -------------------------------------------------------------------------------------


def test_keys_told_to_serve_one_thread_are_summed_as_it_and_a_window_goes_with_its_surroundings(
    tmp_path: Path,
) -> None:
    """Ghostty and GitHub serve Jarvis; an untitled window between them goes with Jarvis."""
    sources = _ship(tmp_path / "imports", [
        (_at(10), _at(10, 20), {"appName": "Ghostty", "title": "jarvis", "keySeconds": 60}),
        (_at(10, 20), _at(10, 25), {"appName": "Claude"}),
        (_at(10, 25), _at(10, 40), {"appName": "Chrome", "domain": "github.com"}),
        (_at(10, 40), _at(10, 50), {"appName": "WeChat"}),
        (_at(11, 30), _at(11, 35), {"appName": "Claude"}),
    ])
    threads = {
        "mac|Ghostty|jarvis": attention.Thread("Jarvis", "work"),
        "mac|Chrome|github.com": attention.Thread("Jarvis", "work"),
        "mac|Claude|": attention.Thread(None, None),
    }
    told = _told(sources, _at(10), _at(12), threads)
    assert [(t["id"], t["name"], t["category"], t["ms"] // MIN) for t in told["threads"]] == [
        ("thread|Jarvis", "Jarvis", "work", 40),
        ("mac|WeChat|", "WeChat", None, 10),
        ("mac|Claude|", "Claude", None, 5),  # 11:30, nothing around it: itself
    ]
    assert [k["key"] for k in told["threads"][0]["keys"]] == [
        "mac|Ghostty|jarvis", "mac|Chrome|github.com", "mac|Claude|",
    ]
    assert told["threads"][0]["typed_ms"] == 60_000
    assert [(r["thread"], r["ms"] // MIN, r["others_ms"] // MIN) for r in told["rows"]] == [
        ("thread|Jarvis", 40, 15),  # WeChat and the lone window: under ten minutes each
    ]


# --- the day page --------------------------------------------------------------------------------


def _visit(log: Path, uid: str, arrived: datetime, departed: datetime) -> None:
    payload = {
        "lat": 1.0, "lng": 2.0, "accuracy_m": 30.0, "arrived_at_ms": _ms(arrived),
        "departed_at_ms": _ms(departed), "place": "Home",
    }
    with closing(open_event_log(log)) as conn:
        emit_event(
            conn, type="phone.visit_observed", payload=payload, ts_epoch_ms=_ms(arrived),
            event_uid=uid, ingestion_node="phone",
        )


def test_every_doing_stretch_on_the_page_tells_its_own_attention(tmp_path: Path) -> None:
    """Each doing span carries ``within`` over its own time, the day before's tail as well."""
    memory, log = tmp_path / "memory.db", tmp_path / "events.db"
    open_memory_db(memory).close()
    open_event_log(log).close()
    _visit(log, "v1", _at(19), _at(1, 30, day=DAY + timedelta(days=1)))
    directory = tmp_path / "imports"
    _ship(directory, [
        (_at(22), _at(23, 30), {"appName": "Ghostty", "title": "jarvis", "keySeconds": 300}),
        (_at(23, 30), _at(0, 40, day=DAY + timedelta(days=1)), {"appName": "WeChat"}),
    ], [("iPhone", "com.ss.iphone.ugc.Aweme", _at(0, 50, day=DAY + timedelta(days=1)),
         _at(1, 20, day=DAY + timedelta(days=1)))])
    sources = DaySources(VANCOUVER, None, memory, shipped=directory)
    after = DAY + timedelta(days=1)
    for day, now in ((DAY, _at(23)), (after, _at(1, 0, day=after))):
        page = _read_day(log, VANCOUVER, memory, None, day, _ms(now), shipped=directory)
        doing = [s for s in page["spans"] if s["lane"] == "doing"]
        assert doing
        for span in doing:
            start, end = span["start_ms"], span["end_ms"]
            want = attention.within(attention.read(sources, start, end), start, end, {})
            assert span["attention"] == want
    tail = [s for s in doing if s["start_ms"] < _ms(_at(0, day=DAY + timedelta(days=1)))]
    assert tail, "the next page shows the day before's stretch past midnight"
    assert {k["name"] for s in doing for k in s["attention"]["threads"]} == {
        "Ghostty jarvis", "WeChat", "抖音",
    }


def test_a_planned_stretch_tells_no_attention() -> None:
    """A stretch that has not happened has no attention to tell."""
    planned = {
        "id": "p1", "lane": "doing", "kind": "planned", "start_ms": 10, "end_ms": 20,
        "item_ids": [],
    }
    lane = Lane(DAY, [], Doing([], [planned], {"waking_start_ms": 0, "waking_end_ms": 5}))
    assert DayDoing(lane, None, 0, []).served(set())[0]["attention"] is None
