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

ADR 0315, telling keys:

- before a run, the day's untold keys are asked of the threads model first, its gated answer is
  kept and the page names their threads; a key already told is not asked again;
- without a threads prompt no key is told; a telling that fails leaves the run standing;
- his word on a key wins over any model's, whenever it came;
- the gates: every key told once, by a label asked, a thread is a short name with a category, or
  null with none.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision.attention_threads import Asked, ThreadBrief, read_threads
from jarvis.runtime.daemon.routes import read_day
from jarvis.runtime.doing_day import DayDoing, Lane
from jarvis.runtime.doing_run import DoingSchedule, read_brief, read_thread_brief
from jarvis.state import attention, imports
from jarvis.state.day_line import DaySources
from jarvis.state.doing import Doing
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import append_attention_threads, attention_threads, open_memory_db
from jarvis.state.shipments import Shipments
from scripts import attention_replay
from tests.support.doing import CLOCKED, World, Writer, tile, world

__all__ = ["world"]  # the doing tests' day, used here as a fixture

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
            "key": f"span:{fields.get('id', n)}:{'b' * 12}", "kind": "span", "zone": "UTC",
            "at_ms": _ms(a),
            "text": "",
            "fields": {
                "id": n, "deviceID": None, "appName": "Claude", "appBundleID": "com.example",
                "domain": None, "document": None, "title": None, "keySeconds": 0, **fields,
                "start": _utc(a), "end": _utc(b),
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


def test_a_file_document_is_keyed_by_its_whole_path_and_only_its_name_is_cut(
    tmp_path: Path,
) -> None:
    """Paths that share their first 30 characters stay apart: the name keeps the path's end.

    The home is ``~``, a trailing slash is dropped and ``%20`` decoded.
    """
    home = "file:///Users/someone/"
    sources = _ship(tmp_path / "imports", [
        (_at(10), _at(10, 10), {"appName": "Cursor", "document": home + "Projects/jarvis/"}),
        (_at(10, 10), _at(10, 20), {"appName": "Cursor", "document": home + "Projects/timesink"}),
        (_at(10, 20), _at(10, 30), {
            "appName": "Cursor",
            "document": home + "Documents/coop/the%20very%20long%20folder/notes/final.md",
        }),
        (_at(10, 30), _at(10, 40), {"appName": "Cursor", "document": "Jarvis chat"}),
    ])
    keys = {k["key"]: k["name"] for t in _told(sources, _at(10), _at(11))["threads"]
            for k in t["keys"]}
    assert keys == {
        "mac|Cursor|~/Projects/jarvis": "Cursor ~/Projects/jarvis",
        "mac|Cursor|~/Projects/timesink": "Cursor ~/Projects/timesink",
        "mac|Cursor|~/Documents/coop/the very long folder/notes/final.md":
            "Cursor …ry long folder/notes/final.md",
        "mac|Cursor|Jarvis chat": "Cursor Jarvis chat",
    }
    assert all(len(name.removeprefix("Cursor ")) <= attention.TITLE_MAX_CHARS
               for name in keys.values())


def test_only_a_real_home_is_shown_as_tilde() -> None:
    """``/Users/Shared`` is a shared folder, not a home; ``/home/<name>`` is one."""
    shown = {
        document: attention.held({"appName": "Cursor", "document": document})[0][1]
        for document in (
            "file:///Users/Shared/x/", "file:///Users/Shared", "file:///Users/someone/x",
            "file:///home/allen/x", "file:///Users/Sharedx/y",
        )
    }
    assert list(shown.values()) == [
        "/Users/Shared/x", "/Users/Shared", "~/x", "~/x", "~/y",
    ]


def test_a_key_is_named_the_same_by_held_and_by_key_name() -> None:
    """Including a title that starts with "/" or "~", and a title longer than the cut."""
    for fields in (
        {"appName": "Safari", "title": "/etc/hosts - " + "x" * 40},
        {"appName": "Safari", "title": "~ notes " + "x" * 40},
        {"appName": "Safari", "title": "Release notes " + "x" * 40},
        {"appName": "Cursor", "document": "file:///Users/someone/Documents/coop/a/b/c/final.md"},
        {"appName": "Cursor", "document": "A | B conversation"},
    ):
        (app, what), name = attention.held(fields)
        assert attention.key_name(f"mac|{app}|{what}") == name
    assert attention.key_name("app|com.tencent.xin") == "com.tencent.xin"


def test_a_long_title_is_one_key_and_its_name_is_cut() -> None:
    """A title is folded for whitespace only: the key is whole, the name ends in an ellipsis."""
    title = "Release   notes " + "x" * 40
    key, name = attention.held({"appName": "Safari", "title": title})
    assert key == ("Safari", " ".join(title.split()))
    assert name.endswith("…")
    assert len(name.removeprefix("Safari ")) == attention.TITLE_MAX_CHARS


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
        page = read_day(log, VANCOUVER, memory, None, day, _ms(now), shipped=directory)
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
    assert DayDoing(lane, None, 0, [], {}).served(set())[0]["attention"] is None


# --- telling keys (ADR 0315) ---------------------------------------------------------------------

TELL = "say what each key served"
TOLD = {"Ghostty jarvis": ("Jarvis", "work"), "Claude": (None, None), "WeChat": ("微信", "social")}


def _teller(answer: dict[str, tuple[str | None, str | None]]) -> Any:  # noqa: ANN401
    """The threads model's answer to a brief: each key whose name is in ``answer`` told so."""

    def reply(prompt: str) -> str:
        if "\nKNOWN THREADS:" not in prompt:
            return tile(prompt)
        told = []
        for line in prompt.splitlines():
            if line.startswith("k") and " | " in line:
                label, rest = line.split(" ", 1)
                thread, category = answer[rest.split(" | ")[0]]
                told.append({"key": label, "thread": thread, "category": category})
        return json.dumps({"threads": told}, ensure_ascii=False)

    return reply


def _shipped_world(world: World, tmp_path: Path) -> World:
    """His Mac at 09:00-11:00 that day: Jarvis, an untitled Claude window, then WeChat."""
    directory = tmp_path / "imports"
    _ship(directory, [
        (_at(9), _at(10), {"appName": "Ghostty", "title": "jarvis", "keySeconds": 300}),
        (_at(10), _at(10, 20), {"appName": "Claude"}),
        (_at(10, 35), _at(11), {"appName": "WeChat"}),
    ])
    sources = DaySources(VANCOUVER, None, world.memory, shipped=directory)
    return World(world.memory, world.log, sources)


def _page_threads(world: World, now: datetime) -> dict[str, tuple[str | None, int]]:
    page = read_day(
        world.log, VANCOUVER, world.memory, None, DAY, _ms(now), shipped=world.sources.shipped,
    )
    found: dict[str, tuple[str | None, int]] = {}
    for span in page["spans"]:
        if span["lane"] == "doing" and span.get("attention"):
            for t in span["attention"]["threads"]:
                ms = found.get(t["name"], (None, 0))[1]
                found[t["name"]] = (t["category"], ms + t["ms"] // MIN)
    return found


def _schedule(world: World, client: Writer, *, prompt: str | None = TELL) -> DoingSchedule:
    settings = CLOCKED.__class__(**{**CLOCKED.__dict__, "threads_prompt": prompt})
    return DoingSchedule(
        settings=settings, sources=world.sources, memory_db=world.memory, event_log_path=world.log,
        llm_config={}, pricing_table={}, client=client,  # type: ignore[arg-type]
        threads_client=client,  # type: ignore[arg-type]
    )


def test_a_told_key_nobody_shows_is_named_as_held_names_it_and_never_whole(
    world: World, tmp_path: Path,
) -> None:
    """The threads brief lists a told key absent from the window by the cut name, not the key."""
    world = _shipped_world(world, tmp_path)
    path = "~/Documents/coop/the very long folder/notes/final.md"
    title = "Release notes " + "x" * 40
    told = {
        f"mac|Cursor|{path}": ("coop", "study"),
        f"mac|Safari|{title}": ("coop", "study"),
        "app|com.tencent.xin": ("chat", "social"),
    }
    append_attention_threads(world.memory, told, origin="model", model="m", day="d")
    brief = read_brief(DAY, world.sources, world.log, _at(15))
    assert brief is not None
    asked = read_thread_brief(brief, world.sources, world.memory)
    assert asked is not None
    (coop,) = (keys for name, _, keys in asked.known if name == "coop")
    assert sorted(coop) == sorted([
        attention.held({"appName": "Cursor", "document": "file:///Users/x/" + path[2:]})[1],
        attention.held({"appName": "Safari", "title": title})[1],
    ])
    assert all(len(k.split(" ", 1)[1]) <= attention.TITLE_MAX_CHARS for k in coop)
    assert [keys for name, _, keys in asked.known if name == "chat"] == [["com.tencent.xin"]]


def test_a_run_first_has_the_new_keys_told_and_the_page_names_their_threads(
    world: World, tmp_path: Path,
) -> None:
    """Told once before the run; the untitled window goes with Jarvis; asked again: nothing new."""
    world = _shipped_world(world, tmp_path)
    assert _page_threads(world, _at(15)) == {
        "Ghostty jarvis": (None, 60), "WeChat": (None, 25), "Claude": (None, 20),
    }
    client = Writer(_teller(TOLD))
    schedule = _schedule(world, client)
    assert schedule.tidy(_at(15)).startswith("landed")
    assert client.systems[0] == TELL
    assert client.systems[1] == CLOCKED.prompt
    assert "k1 Ghostty jarvis | Mac, 60m, typed 300s, 09:00-10:00" in client.prompts[0]
    with closing(open_memory_db(world.memory)) as conn:
        assert attention_threads(conn) == {
            "mac|Ghostty|jarvis": ("Jarvis", "work"), "mac|Claude|": (None, None),
            "mac|WeChat|": ("微信", "social"),
        }
        models = [row[0] for row in conn.execute("SELECT model FROM attention_threads")]
    assert models == ["fake-haiku"] * 3
    assert _page_threads(world, _at(15)) == {"Jarvis": ("work", 80), "微信": ("social", 25)}
    # a new key later: only it is asked about
    _ship(tmp_path / "imports", [
        (_at(11, 10), _at(11, 30), {"appName": "Chrome", "domain": "github.com", "id": 9}),
    ])
    client.reply = _teller({**TOLD, "Chrome github.com": ("Jarvis", "work")})
    assert schedule.tidy(_at(15, 5)).startswith("landed")
    assert client.systems[2:] == [TELL, CLOCKED.prompt]
    keys = [line for line in client.prompts[2].splitlines() if line.startswith("k")]
    assert keys == ["k1 Chrome github.com | Mac, 20m, 11:10-11:30"]
    assert "- Jarvis (work): Ghostty jarvis" in client.prompts[2]
    assert _page_threads(world, _at(15, 5))["Jarvis"] == ("work", 100)


def test_without_a_threads_prompt_no_key_is_told(world: World, tmp_path: Path) -> None:
    """The run alone; the store stays empty and each key is its own thread."""
    world = _shipped_world(world, tmp_path)
    client = Writer(_teller(TOLD))
    assert _schedule(world, client, prompt=None).tidy(_at(15)).startswith("landed")
    assert client.systems == [CLOCKED.prompt]
    with closing(open_memory_db(world.memory)) as conn:
        assert attention_threads(conn) == {}


def test_a_telling_that_fails_leaves_the_run_standing(world: World, tmp_path: Path) -> None:
    """A refused answer twice, then the run: nothing told, the run lands."""
    world = _shipped_world(world, tmp_path)
    client = Writer(lambda p: '{"threads": []}' if "\nKNOWN THREADS:" in p else tile(p))
    assert _schedule(world, client).tidy(_at(15)).startswith("landed")
    assert client.systems == [TELL, TELL, CLOCKED.prompt]
    assert "That answer" not in client.asked[1][-1]["content"]
    assert "not told: k1" in client.asked[1][-1]["content"]
    with closing(open_memory_db(world.memory)) as conn:
        assert attention_threads(conn) == {}


def test_his_word_on_a_key_wins_over_any_models(tmp_path: Path) -> None:
    """A model's word, his, then a model's again: his stands."""
    memory = tmp_path / "memory.db"
    open_memory_db(memory).close()
    key = "mac|Chrome|bilibili.com"
    append_attention_threads(memory, {key: ("刷视频", "rest")}, origin="model", model="m", day="d")
    append_attention_threads(memory, {key: ("数学课", "study")}, origin="him", model=None, day="d")
    append_attention_threads(memory, {key: ("看电影", "rest")}, origin="model", model="m", day="d")
    with closing(open_memory_db(memory)) as conn:
        assert attention_threads(conn) == {key: ("数学课", "study")}
    with pytest.raises(ValueError, match="origin"):
        append_attention_threads(memory, {key: ("x", "rest")}, origin="her", model=None, day="d")


_ASKED = ThreadBrief(
    day="2026-10-07", language="Chinese", zone=VANCOUVER, known=[], projects=[],
    asked=[Asked("mac|A|", "A", "mac", 1e6, 0, 0, 1), Asked("mac|B|", "B", "mac", 1e6, 0, 0, 1)],
)
GATE_CASES = {
    "both told": (
        [{"key": "k1", "thread": " Jarvis ", "category": "work"},
         {"key": "k2", "thread": None, "category": None}],
        {"mac|A|": ("Jarvis", "work"), "mac|B|": (None, None)},
    ),
    "one left out": ([{"key": "k1", "thread": None, "category": None}], "not told: k2"),
    "a label not asked": ([{"key": "k9", "thread": None, "category": None}], "names no key"),
    "told twice": (
        [{"key": "k1", "thread": None, "category": None},
         {"key": "k1", "thread": None, "category": None}], "k1 is told twice",
    ),
    "a name too long": (
        [{"key": "k1", "thread": "x" * 25, "category": "work"}], "longer than 24",
    ),
    "a category not in the list": (
        [{"key": "k1", "thread": "Jarvis", "category": "unknown"}], "category must be one of",
    ),
    "around it, with a category": (
        [{"key": "k1", "thread": None, "category": "work"}], "so it has no category",
    ),
    "an empty name": ([{"key": "k1", "thread": " ", "category": "work"}], "is not a name"),
}


@pytest.mark.parametrize("name", GATE_CASES)
def test_the_gates_on_what_the_model_told(name: str) -> None:
    """Every key told once, by an asked label, with a short name and a category, or null."""
    entries, expected = GATE_CASES[name]
    got = read_threads(json.dumps({"threads": entries}), "stop", _ASKED)
    if isinstance(expected, str):
        assert isinstance(got, str)
        assert expected in got
    else:
        assert got == expected


def test_the_replay_prints_each_stretch_with_where_his_attention_went(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """``scripts.attention_replay`` without ``--tell``: no model, the page's stretches as text."""
    world = _shipped_world(world, tmp_path)
    assert attention_replay.main([
        "--date", DAY.isoformat(), "--event-log", str(world.log), "--memory-db",
        str(world.memory), "--shipped", str(tmp_path / "imports"), "--now", "2026-10-07T15:00",
        "--zone", "America/Vancouver",
    ]) == 0
    out = capsys.readouterr().out
    assert out.startswith("== 2026-10-07\n")
    assert "  Ghostty jarvis [-] mac 60m typed 300s: Ghostty jarvis 60m" in out
    assert "09:00-11:00 Ghostty | front 105m\n" in out
    assert "  row 09:00-10:00 Ghostty jarvis 60m, others 0m, 0 switches\n" in out
    assert "  row 10:00-10:20 Claude 20m, others 0m, 0 switches\n" in out  # nobody told it
