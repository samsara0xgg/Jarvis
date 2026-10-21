"""Rows, stores and cases for the day line: phone rows in minutes, TimeSink-shaped stores."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from jarvis.state.day_line import DaySources
from jarvis.state.event_log import emit_event
from jarvis.state.memory_db import open_memory_db
from tests.support.ledger import PROJECT, SPAN, VERDICT

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from zoneinfo import ZoneInfo

VANCOUVER = "America/Vancouver"


MINUTE = 60_000


VISIT, MOTION, HEALTH = "phone.visit_observed", "phone.motion_observed", "phone.health_observed"


MINUTE_KEYS = frozenset(
    {
        "arrived_at_ms",
        "departed_at_ms",
        "started_at_ms",
        "ended_at_ms",
        "start_ms",
        "end_ms",
        "due_at_epoch_ms",
        "fired_at",
    }
)


class Row(NamedTuple):
    """One log row: type, uid, a payload whose times are minutes, and the minute it was logged."""

    type: str
    uid: str
    payload: dict[str, Any]
    ts: float


def _latest(payload: dict[str, Any]) -> float:
    return max((v for k, v in payload.items() if k in MINUTE_KEYS), default=0)


def _phone(kind: str, uid: str, payload: dict[str, Any], ts: float | None) -> Row:
    return Row(kind, uid, payload, _latest(payload) if ts is None else ts)


def visit(
    uid: str,
    arrived: float,
    departed: float | None = None,
    place: str | None = None,
    *,
    ts: float | None = None,
) -> Row:
    """A ``phone.visit_observed`` row: arrival only, or with a departure."""
    payload: dict[str, Any] = {
        "lat": 48.46,
        "lng": -123.31,
        "accuracy_m": 65.0,
        "arrived_at_ms": arrived,
    }
    if departed is not None:
        payload["departed_at_ms"] = departed
    if place is not None:
        payload["place"] = place
    return _phone(VISIT, uid, payload, ts)


def motion(
    uid: str,
    activity: str,
    start: float,
    end: float | None = None,
    confidence: str = "high",
) -> Row:
    """A ``phone.motion_observed`` row."""
    payload: dict[str, Any] = {
        "activity": activity,
        "confidence": confidence,
        "started_at_ms": start,
    }
    if end is not None:
        payload["ended_at_ms"] = end
    return _phone(MOTION, uid, payload, None)


def health(  # noqa: PLR0913 — a row's every part, each defaulted
    uid: str,
    start: float,
    end: float,
    value: float,
    metric: str = "sleep",
    stage: str = "core",
) -> Row:
    """A ``phone.health_observed`` row; a span of sleep in ``stage`` unless said otherwise."""
    payload: dict[str, Any] = {"metric": metric, "start_ms": start, "end_ms": end, "value": value}
    if metric == "sleep":
        payload["detail"] = stage
    return _phone(HEALTH, uid, payload, None)


def remind(rid: str, due: float, text: str = "stand up") -> list[Row]:
    """A ``reminder.scheduled`` row."""
    payload = {
        "reminder_id": rid,
        "due_at_epoch_ms": due,
        "due_at_local": "-",
        "text": text,
        "action_id": "A1",
    }
    return [Row("reminder.scheduled", f"{rid}-scheduled", payload, 0)]


def fired(rid: str, at: float) -> Row:
    """A ``reminder.fired`` row."""
    payload = {"reminder_id": rid, "fired_at": at, "late_ms": 0, "delivered": "speak"}
    return Row("reminder.fired", f"{rid}-fired", payload, at)


class Item(NamedTuple):
    """An expected item; times are minutes from the opening midnight."""

    uid: str
    kind: str
    start: float
    end: float | None
    state: str
    extra: dict[str, Any]


def due(rid: str, at: float, state: str, text: str = "stand up", fate: str = "") -> Item:
    """An expected ``reminder`` in the lane of her; ``fate`` is "", "fired" or "taken"."""
    extra = {"text": text, "fired": bool(fate), "acknowledged": fate == "taken"}
    return Item(rid, "reminder", at, None, state, extra)


def minutes_ms(origin: int, minutes: float) -> int:
    return origin + round(minutes * MINUTE)


def load_rows(conn: sqlite3.Connection, rows: list[Row], origin: int) -> None:
    """Append the rows to the log; every ``*_ms`` payload field and the row time are minutes."""
    for row in rows:
        payload = {
            k: minutes_ms(origin, v) if k in MINUTE_KEYS else v for k, v in row.payload.items()
        }
        emit_event(
            conn,
            type=row.type,
            payload=payload,
            ts_epoch_ms=minutes_ms(origin, row.ts),
            event_uid=row.uid,
            ingestion_node="phone",
        )


def _stages(stages: list[tuple[str, float, float]], origin: int) -> list[dict[str, Any]]:
    return [
        {"stage": stage, "start_ms": minutes_ms(origin, begin), "end_ms": minutes_ms(origin, stop)}
        for stage, begin, stop in stages
    ]


def expected_items(items: list[Item], origin: int) -> list[dict[str, Any]]:
    """The items as the route words them; an ``@`` in a uid stands for the item's start in ms."""
    return [
        {
            "id": item.uid.replace("@", str(minutes_ms(origin, item.start))),
            "lane": "her" if item.kind in ("reminder", "talk") else "you",
            "kind": item.kind,
            "start_ms": minutes_ms(origin, item.start),
            "end_ms": None if item.end is None else minutes_ms(origin, item.end),
            "state": item.state,
            **item.extra,
            **({"stages": _stages(item.extra["stages"], origin)} if "stages" in item.extra else {}),
        }
        for item in items
    ]


_BUNDLES = {  # the app as the ledger names it: (TimeSink's bundle id, TimeSink's own name for it)
    "Ghostty": ("com.mitchellh.ghostty", "Ghostty"),
    "Chrome": ("com.google.Chrome", "Google Chrome"),
    "Zoom": ("us.zoom.xos", "zoom.us"),
    "WeChat": ("com.tencent.xinWeChat", "微信"),
}


_CALL = (
    'CREATE TABLE "callSpan" ("id" INTEGER PRIMARY KEY AUTOINCREMENT, "start" DATETIME NOT NULL, '
    '"end" DATETIME NOT NULL, "appBundleID" TEXT NOT NULL, "appName" TEXT NOT NULL)'
)


class Spun(NamedTuple):
    """A TimeSink span in minutes from the opening midnight; ``project`` "none" has no verdict."""

    start: float
    end: float
    app: str = "Ghostty"
    project: str = "none"


class Call(NamedTuple):
    """A TimeSink call span in minutes from the opening midnight."""

    start: float
    end: float
    app: str = "Zoom"


class Said(NamedTuple):
    """A row of memory.db's records: the minute it was written and who said it."""

    at: float
    source: str = "allen"


def _app(display: str) -> tuple[str, str]:
    return _BUNDLES.get(display, (f"com.example.{display}", display))


def _grdb(ms: int) -> str:
    """GRDB's text form of a time: UTC, a space, milliseconds, no suffix."""
    at = datetime.fromtimestamp(ms / 1000, UTC)
    return at.strftime("%Y-%m-%d %H:%M:%S.") + f"{at.microsecond // 1000:03d}"


def add_spans(path: Path, origin: int, spans: Sequence[Spun]) -> None:
    """Append spans (and the project verdicts that name them) to a TimeSink-shaped store."""
    with closing(sqlite3.connect(path)) as conn, conn:
        known = dict(conn.execute("SELECT name, id FROM project"))
        for name in sorted({span.project for span in spans} - {"none"} - set(known)):
            conn.execute("INSERT INTO project (name) VALUES (?)", (name,))
            known[name] = conn.execute("SELECT id FROM project WHERE name=?", (name,)).fetchone()[0]
        for span in spans:
            bundle, name = _app(span.app)
            title = f"in-{span.project}"
            conn.execute(
                "INSERT INTO span (start, end, appBundleID, appName, title, document) "
                "VALUES (?, ?, ?, ?, ?, '')",
                (
                    _grdb(minutes_ms(origin, span.start)),
                    _grdb(minutes_ms(origin, span.end)),
                    bundle,
                    name,
                    title,
                ),
            )
            if span.project != "none":
                conn.execute(
                    "INSERT INTO jevProjectVerdict VALUES (?, '', ?, '', ?)",
                    (bundle, title, known[span.project]),
                )


def add_calls(path: Path, origin: int, calls: Sequence[Call]) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        for call in calls:
            bundle, name = _app(call.app)
            conn.execute(
                "INSERT INTO callSpan (start, end, appBundleID, appName) VALUES (?, ?, ?, ?)",
                (
                    _grdb(minutes_ms(origin, call.start)),
                    _grdb(minutes_ms(origin, call.end)),
                    bundle,
                    name,
                ),
            )


def add_talks(path: Path, origin: int, zone: ZoneInfo, talks: Sequence[Said]) -> None:
    """Append records, stamped in the owner's local time as the daemon writes them."""
    with closing(open_memory_db(path)) as conn, conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM records").fetchone()
        for n, said in enumerate(talks, start=count):
            at = datetime.fromtimestamp(minutes_ms(origin, said.at) / 1000, zone)
            conn.execute(
                "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, 'x')",
                (f"r{n}", at.isoformat(), said.source),
            )


def make_stores(  # noqa: PLR0913 — the three stores of a day and the origin they are timed from.
    tmp_path: Path,
    zone: ZoneInfo,
    *,
    origin: int = 0,
    spans: Sequence[Spun] = (),
    calls: Sequence[Call] = (),
    talks: Sequence[Said] = (),
) -> DaySources:
    """A TimeSink-shaped store and a memory.db holding the rows, as a daemon would be wired."""
    ts, memory = tmp_path / "timesink.db", tmp_path / "memory.db"
    with closing(sqlite3.connect(ts)) as conn, conn:
        for ddl in (SPAN, PROJECT, VERDICT, _CALL):
            conn.execute(ddl)
    add_spans(ts, origin, spans)
    add_calls(ts, origin, calls)
    open_memory_db(memory).close()
    add_talks(memory, origin, zone, talks)
    return DaySources(zone, ts, memory)


def work(  # noqa: PLR0913 — a block's span, its active time, its two tops and its state.
    start: float,
    end: float,
    active_s: int,
    projects: Sequence[tuple[str, int]] = (),
    apps: Sequence[tuple[str, int]] = (),
    state: str = "done",
) -> Item:
    """An expected ``work`` block; the id is ``work-`` and its start in ms."""
    extra = {
        "active_s": active_s,
        "projects": [{"name": name, "seconds": seconds} for name, seconds in projects],
        "apps": [{"name": name, "seconds": seconds} for name, seconds in apps],
    }
    return Item("work-@", "work", start, end, state, extra)


def call(start: float, end: float, app: str = "Zoom", nth: int = 1, state: str = "done") -> Item:
    """An expected ``call``; the id is ``call-`` and its start, then ``-n`` for the nth together."""
    return Item("call-@" if nth == 1 else f"call-@-{nth}", "call", start, end, state, {"app": app})


def talk(start: float, end: float, turns: int, state: str = "done") -> Item:
    """An expected ``talk`` in the lane of her; the id is ``talk-`` and its start in ms."""
    return Item("talk-@", "talk", start, end, state, {"turns": turns})


class MacCase(NamedTuple):
    """TimeSink spans and calls and his records of one day, and the items that day folds to."""

    name: str
    items: list[Item]
    spans: tuple[Spun, ...] = ()
    calls: tuple[Call, ...] = ()
    talks: tuple[Said, ...] = ()
    now: float = 1500


def seconds_as_minutes(seconds: float) -> float:
    """Seconds as the minutes the rows are timed in."""
    return seconds / 60


MAC_CASES: list[MacCase] = [
    # --- work blocks -------------------------------------------------------------------------
    MacCase(
        "one span is one block, named by its app and by no project when it has no verdict",
        [work(540, 600, 3600, apps=[("Ghostty", 3600)])],
        spans=(Spun(540, 600),),
    ),
    MacCase(
        "spans fifteen minutes apart are one block, fifteen exactly is within, and the gap is not "
        "active time",
        [work(540, 600, 2700, apps=[("Ghostty", 2700)])],
        spans=(Spun(540, 560), Spun(575, 600)),
    ),
    MacCase(
        "a gap of a second over fifteen minutes makes two blocks",
        [
            work(540, 560, 1200, apps=[("Ghostty", 1200)]),
            work(575 + seconds_as_minutes(1), 600, 1499, apps=[("Ghostty", 1499)]),
        ],
        spans=(Spun(540, 560), Spun(575 + seconds_as_minutes(1), 600)),
    ),
    MacCase(
        "a block with under five minutes of active time is dropped, five exactly is kept, and "
        "the gaps inside a block do not count toward it",
        [
            work(120, 125, 300, apps=[("Ghostty", 300)]),
            work(300, 312, 300, apps=[("Ghostty", 300)]),
        ],
        spans=(
            Spun(60, 60 + seconds_as_minutes(299)),  # 4m59s alone
            Spun(120, 125),  # 5m exactly
            Spun(200, 202),
            Spun(210, 212),  # 12 minutes across, 4 of them active
            Spun(300, 303),
            Spun(310, 312),  # 12 minutes across, 5 of them active
        ),
    ),
    MacCase(
        "two apps at once count once, and each project and app is its own union of time",
        [
            work(
                540,
                640,
                6000,
                projects=[("docs", 4200), ("jarvis", 3600)],
                apps=[("Chrome", 4200), ("Ghostty", 3600)],
            ),
        ],
        spans=(
            Spun(540, 600, "Ghostty", "jarvis"),
            Spun(570, 630, "Chrome", "docs"),
            Spun(620, 640, "Chrome", "docs"),
        ),
    ),
    MacCase(
        "a block names its three biggest projects and apps; the project none is never named",
        [
            work(
                540,
                800,
                15600,
                projects=[("alpha", 3600), ("beta", 3000), ("gamma", 2400)],
                apps=[("Notes", 4800), ("Ghostty", 3600), ("Chrome", 3000)],
            ),
        ],
        spans=(
            Spun(540, 600, "Ghostty", "alpha"),
            Spun(600, 650, "Chrome", "beta"),
            Spun(650, 690, "Xcode", "gamma"),
            Spun(690, 720, "Slack", "delta"),
            Spun(720, 800, "Notes"),  # the most time of any, and no project
        ),
    ),
    MacCase(
        "a project or app under three minutes is not named and three exactly is",
        [
            work(
                540,
                605 + seconds_as_minutes(59),
                3959,
                projects=[("main", 3600), ("brief", 180)],
                apps=[("Ghostty", 3600), ("Chrome", 180)],
            ),
        ],
        spans=(
            Spun(540, 600, "Ghostty", "main"),
            Spun(600, 603, "Chrome", "brief"),
            Spun(603, 605 + seconds_as_minutes(59), "Xcode", "tiny"),  # 2m59s
        ),
    ),
    MacCase(
        "bundle ids TimeSink spells two ways are one app, and equal times are told apart by name",
        [work(540, 580, 2400, apps=[("Chrome", 1200), ("WeChat", 1200)])],
        spans=(Spun(540, 560, "WeChat"), Spun(560, 580, "Chrome")),
    ),
    MacCase(
        "seconds are rounded to the nearest, not cut",
        [
            work(
                540,
                600 + seconds_as_minutes(0.6),
                3601,
                projects=[("jarvis", 3601)],
                apps=[("Ghostty", 3601)],
            ),
        ],
        spans=(Spun(540, 600 + seconds_as_minutes(0.6), "Ghostty", "jarvis"),),
    ),
    MacCase(
        "a block that crosses a midnight is returned whole, from the day it began in",
        [
            work(-50, 20, 4200, apps=[("Ghostty", 4200)]),
            work(1400, 1480, 4800, apps=[("Ghostty", 4800)]),
        ],
        spans=(Spun(-300, -240), Spun(-50, 20), Spun(1400, 1480), Spun(1600, 1700)),
    ),
    MacCase(
        "a block that only touches the day is not in it",
        [],
        spans=(Spun(-120, -60), Spun(-30, 0), Spun(1440, 1500)),
    ),
    MacCase(
        "a block with the clock inside it is now",
        [work(540, 600, 3600, apps=[("Ghostty", 3600)], state="now")],
        spans=(Spun(540, 600),),
        now=570,
    ),
    # --- calls -------------------------------------------------------------------------------
    MacCase(
        "a call of two minutes is kept and one a second shorter is not",
        [call(600, 602)],
        calls=(Call(600, 602), Call(700, 702 - seconds_as_minutes(1))),
    ),
    MacCase(
        "a call is named by the app the ledger names, a call across midnight is whole, and a "
        "row seen twice is one call",
        [
            call(-10, 40),
            call(1000, 1030, "WeChat"),
            call(1430, 1500),
        ],
        calls=(
            Call(-10, 40),
            Call(-10, 40),
            Call(1000, 1030, "WeChat"),
            Call(1430, 1500),
            Call(-200, -100),
        ),
    ),
    MacCase(
        "calls that start together are told apart, in order of when they end",
        [call(800, 815, "WeChat"), call(800, 830, "Zoom", nth=2)],
        calls=(Call(800, 830), Call(800, 815, "WeChat")),
    ),
    MacCase(
        "a call and the work around it are two items in the lane of him",
        [work(540, 600, 3600, apps=[("Ghostty", 3600)]), call(570, 600)],
        spans=(Spun(540, 600),),
        calls=(Call(570, 600),),
    ),
    # --- talks -------------------------------------------------------------------------------
    MacCase(
        "his words ten minutes apart are one talk and a second more is another",
        [talk(480, 490, 2), talk(500 + seconds_as_minutes(1), 500 + seconds_as_minutes(1), 1)],
        talks=(Said(480), Said(490), Said(500 + seconds_as_minutes(1))),
    ),
    MacCase(
        "only what he said is a talk",
        [talk(480, 480, 1)],
        talks=(Said(480), Said(481, "jarvis"), Said(482, "mail")),
    ),
    MacCase(
        "one word is a talk of one turn that starts and ends together",
        [talk(700, 700, 1)],
        talks=(Said(700),),
    ),
    MacCase(
        "a talk that crosses a midnight is returned whole",
        [talk(-5, 3, 2), talk(1435, 1445, 2)],
        talks=(Said(-5), Said(3), Said(1435), Said(1445), Said(-600), Said(1600)),
    ),
    MacCase(
        "a talk that ends as the day opens, or begins as it closes, is not in it",
        [],
        talks=(Said(-5), Said(0), Said(1440)),
    ),
    # --- all of it ---------------------------------------------------------------------------
    MacCase(
        "work, a call and a talk together, in the lanes of him and of her, in time order",
        [
            talk(530, 538, 3),
            work(540, 600, 3600, projects=[("jarvis", 3600)], apps=[("Ghostty", 3600)]),
            call(570, 590),
        ],
        spans=(Spun(540, 600, "Ghostty", "jarvis"),),
        calls=(Call(570, 590),),
        talks=(Said(530), Said(535), Said(538)),
    ),
]


def raw_row(conn: sqlite3.Connection, uid: str, kind: str, payload: str, ts: int) -> None:
    """A row the registry would have refused, as a damaged log would hold it."""
    conn.execute(
        "INSERT INTO events (event_uid, type, schema_version, ts_epoch_ms, payload_json) "
        "VALUES (?, ?, 1, ?, ?)",
        (uid, kind, ts, payload),
    )
    conn.commit()
