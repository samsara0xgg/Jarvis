"""ADR 0330: where his attention went, from what his Mac, phone and tablet had in front of him.

At any moment his attention is in at most one place:

- at his Mac, while TimeSink counts him there (it stops counting while the Mac is idle, locked or
  asleep): the app, site or document in front of him;
- else on his iPhone or iPad: the app in front, shared evenly while both have one;
- else on no screen.

An app in front on his phone or tablet while he is at the Mac is alongside, never his attention.
Each thing his attention lands on is a key: on the Mac a site by app, domain and page title,
anything else by app and document, else window title (:func:`held`); on his phone or tablet an app
by its bundle, the same key on both devices.

:func:`read` reads the pieces of a window from what the Mac terminal shipped (ADR 0294, 0304).
:func:`within` tells one stretch of them by thread: what each key served, as he or a model told it
(:class:`Thread`; a key nobody told is its own thread), the minutes each thread held and the typing
in it, the rows in which one thread led, and what was alongside. A row is cut only where it pays: a
row's cost is the attention minutes its lead leaves out, plus :data:`ROW_COST_MS` for the row
itself, and the rows of least total cost are found exactly.

Nothing is stored and nothing is guessed: a key is not a project until it is told, and a minute no
screen shows is not counted as anything.

Layer rules: ``jarvis.state`` and ``jarvis.shared`` only (L2).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.state import imports, timesink
from jarvis.state.day_line import app_names, shipped_conn

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping, Sequence

    from jarvis.state.day_line import DaySources

LOGGER = logging.getLogger(__name__)

_MINUTE_MS: Final = 60_000
REACH_MS: Final = 12 * 60 * _MINUTE_MS
"""How long before a window a span is looked for that may still reach into it."""
UNIT_MS: Final = 5 * _MINUTE_MS
"""The clock grain a row is cut at."""
ROW_COST_MS: Final = 10 * _MINUTE_MS
"""What one more row costs, in attention minutes put right: a row is cut out only when its lead
holds more than this that the row around it would leave out."""
TITLE_MAX_CHARS: Final = 30
"""The most of a window's title or document a Mac key holds; a longer one is cut with an
ellipsis."""
AROUND_MS: Final = 15 * _MINUTE_MS
"""How far around a key that serves what is around it the thread it served is looked for."""
MAC: Final = "mac"
"""Where a piece was when it was at his Mac; else the device's name, ``iPhone`` or ``iPad``."""


class Piece(NamedTuple):
    """A stretch in which one key held a ``share`` of the time, or was alongside it."""

    start_ms: int
    end_ms: int
    key: str
    name: str
    where: str
    share: float
    """1, or less while two of his devices had an app in front at once."""
    typed_ms: float
    alongside: bool

    @property
    def ms(self) -> float:
        """The time it held."""
        return (self.end_ms - self.start_ms) * self.share


class Thread(NamedTuple):
    """What a key served: a thread's name and category.

    No name: the key serves what is around it (an untitled window, a login page).
    """

    name: str | None
    category: str | None


type _Stretch = tuple[int, int, str, str, str, float]
"""A span or app stretch as read: start, end, key, name, where, milliseconds typed in it."""


# --- reading -------------------------------------------------------------------------------


def _cut(text: object) -> str:
    line = " ".join(str(text or "").split())
    cut = len(line) > TITLE_MAX_CHARS
    return line[: TITLE_MAX_CHARS - 1].rstrip() + "…" if cut else line


def held(fields: Mapping[str, Any]) -> tuple[tuple[str, str], str]:
    """What a TimeSink span had in front, as (the key it is summed by, how it is named).

    A site is summed by its domain and page title, one search or page apart from the next;
    anything else by its document, else its window title.
    """
    app = str(fields.get("appName") or "unknown app")
    domain = str(fields.get("domain") or "")
    if domain:
        title = _cut(fields.get("title"))
        return (app, f"{domain} {title}".rstrip()), f"{app} {domain}" + (
            f" ({title})" if title else ""
        )
    what = _cut(fields.get("document") or fields.get("title"))
    return (app, what), f"{app} {what}" if what and what != app else app


def mac_spans(
    shipped: sqlite3.Connection, start_ms: int, end_ms: int, reach_ms: int,
) -> list[tuple[int, int, tuple[str, str], str, int]]:
    """The Mac's spans touching ``[start_ms, end_ms)``: start, end, key, name, seconds typed.

    A span is looked for from ``reach_ms`` before the window, so one that began before it and
    reaches into it is found.
    """
    found: list[tuple[int, int, tuple[str, str], str, int]] = []
    for row in imports.newest(
        shipped, "timesink", start_ms=start_ms - reach_ms, end_ms=end_ms,
        kinds=("span",), by_prefix=True,
    ):
        fields = row.fields
        if fields.get("deviceID") is not None or not fields.get("start") or not fields.get("end"):
            continue
        lo, hi = (
            round(datetime.fromisoformat(timesink.moment(str(fields[edge]))).timestamp() * 1000)
            for edge in ("start", "end")
        )
        if hi > max(lo, start_ms):
            key, name = held(fields)
            found.append((lo, hi, key, name, int(fields.get("keySeconds") or 0)))
    return found


def _device_stretches(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> list[_Stretch]:
    names = app_names(conn)
    found: list[_Stretch] = []
    for row in imports.newest(
        conn, imports.DEVICE_SOURCE, start_ms=start_ms - REACH_MS, end_ms=end_ms,
        kinds=(imports.DEVICE_APP,), by_prefix=True,
    ):
        f = row.fields
        lo, hi, bundle = int(f["start_ms"]), int(f["end_ms"]), str(f["bundle_id"])
        if hi > max(lo, start_ms):
            name = str(names.get(bundle, {}).get("app") or bundle)
            found.append((lo, hi, f"app|{bundle}", name, str(f["device"]), 0.0))
    return found


def read(sources: DaySources, start_ms: int, end_ms: int) -> list[Piece]:
    """The pieces of ``[start_ms, end_ms)``, by start; none when nothing was shipped.

    A source that cannot be read leaves its pieces out, logged.
    """
    if end_ms <= start_ms:
        return []
    conn = shipped_conn(sources)
    if conn is None:
        return []
    with closing(conn):
        try:
            mac: list[_Stretch] = [
                (lo, hi, f"mac|{app}|{what}", name, MAC, typed * 1000.0)
                for lo, hi, (app, what), name, typed in mac_spans(conn, start_ms, end_ms, REACH_MS)
            ]
        except Exception:
            LOGGER.exception("attention: the Mac's spans could not be read")
            mac = []
        try:
            devices = _device_stretches(conn, start_ms, end_ms)
        except Exception:
            LOGGER.exception("attention: his phone and tablet apps could not be read")
            devices = []
    return pieces(mac, devices, start_ms, end_ms)


def pieces(
    mac: Iterable[_Stretch], devices: Iterable[_Stretch], start_ms: int, end_ms: int,
) -> list[Piece]:
    """Where his attention was through ``[start_ms, end_ms)``, by the rule of this module.

    Between any two edges of the stretches, the Mac's spans there share the time evenly (they
    seldom overlap), each with the part of its typing that falls there; with none, the apps in
    front on his devices share it evenly; with the Mac, those apps are alongside.
    """
    held_: list[tuple[_Stretch, bool]] = [
        *((s, True) for s in mac), *((s, False) for s in devices),
    ]
    events: list[tuple[int, int, int]] = []
    for at, (s, _) in enumerate(held_):
        lo, hi = max(s[0], start_ms), min(s[1], end_ms)
        if hi > lo:
            events.extend(((lo, 1, at), (hi, -1, at)))
    events.sort()
    found: list[Piece] = []
    active: set[int] = set()
    e = 0
    while e < len(events):
        a = events[e][0]
        while e < len(events) and events[e][0] == a:
            _, step, at = events[e]
            (active.add if step > 0 else active.discard)(at)
            e += 1
        if e == len(events) or not active:
            continue
        b = events[e][0]
        on = sorted(at for at in active if held_[at][1])
        off = sorted(at for at in active if not held_[at][1])
        for group, alongside in ((on, False), (off, bool(on))):
            for at in group:
                lo, hi, key, name, where, typed = held_[at][0]
                share = 1.0 if alongside else 1.0 / len(group)
                typed_here = typed * (b - a) / (hi - lo) if hi > lo else 0.0
                found.append(Piece(a, b, key, name, where, share, typed_here, alongside))
    return _joined(found)


def _joined(found: Sequence[Piece]) -> list[Piece]:
    """Neighbouring pieces of one key, share and kind as one."""
    out: list[Piece] = []
    last: dict[tuple[str, str, float, bool], int] = {}
    for p in sorted(found, key=lambda p: (p.start_ms, p.alongside, p.key, p.where)):
        ident = (p.key, p.where, p.share, p.alongside)
        at = last.get(ident)
        if at is not None and out[at].end_ms == p.start_ms:
            prev = out[at]
            out[at] = prev._replace(end_ms=p.end_ms, typed_ms=prev.typed_ms + p.typed_ms)
            continue
        last[ident] = len(out)
        out.append(p)
    return sorted(out, key=lambda p: (p.start_ms, p.alongside, p.key, p.where))


# --- telling a stretch ---------------------------------------------------------------------


def _clip(p: Piece, start_ms: int, end_ms: int) -> Piece | None:
    lo, hi = max(p.start_ms, start_ms), min(p.end_ms, end_ms)
    if hi <= lo:
        return None
    part = (hi - lo) / (p.end_ms - p.start_ms)
    return p._replace(start_ms=lo, end_ms=hi, typed_ms=p.typed_ms * part)


def _units(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """``[start_ms, end_ms)`` cut on the clock's five minutes."""
    cuts = [start_ms, *range(start_ms - start_ms % UNIT_MS + UNIT_MS, end_ms, UNIT_MS), end_ms]
    return list(pairwise(cuts))


def _rows(
    front: Sequence[tuple[Piece, str]], start_ms: int, end_ms: int, rank: Mapping[str, int],
) -> list[tuple[int, int, str]]:
    """The rows of least cost over ``front``: (start, end, lead thread), on the clock's units."""
    units = _units(start_ms, end_ms)
    ids = sorted(rank, key=lambda t: rank[t])
    index = {t: i for i, t in enumerate(ids)}
    held_in = [[0.0] * len(ids) for _ in units]
    u = 0
    for p, thread in front:  # by start
        while units[u][1] <= p.start_ms:
            u += 1
        v = u
        while v < len(units) and units[v][0] < p.end_ms:
            lo, hi = max(units[v][0], p.start_ms), min(units[v][1], p.end_ms)
            held_in[v][index[thread]] += (hi - lo) * p.share
            v += 1
    prefix = [[0.0] * len(ids)]
    for row in held_in:
        prefix.append([a + b for a, b in zip(prefix[-1], row, strict=True)])
    n = len(units)
    best = [0.0] + [float("inf")] * n
    back = [(0, "")] * (n + 1)
    for j in range(1, n + 1):
        for i in range(j):
            sums = [b - a for a, b in zip(prefix[i], prefix[j], strict=True)]
            top = max(range(len(ids)), key=lambda k: (sums[k], -k))
            cost = best[i] + sum(sums) - sums[top] + ROW_COST_MS
            if cost < best[j]:
                best[j], back[j] = cost, (i, ids[top])
    rows: list[tuple[int, int, str]] = []
    j = n
    while j > 0:
        i, lead = back[j]
        rows.append((i, j, lead))
        j = i
    return [(units[i][0], units[j - 1][1], lead) for i, j, lead in reversed(rows)]


def _switches(front: Sequence[tuple[Piece, str]], lead: str) -> int:
    """How often his attention left ``lead`` for another thread, back to it later or not."""
    order = [t for p, t in sorted(front, key=lambda one: (one[0].start_ms, one[1]))]
    return sum(1 for a, b in pairwise(order) if a == lead and b != lead)


def _threaded(
    group: Sequence[Piece], threads: Mapping[str, Thread],
) -> list[tuple[Piece, str]]:
    """Each piece with the id of the thread it served.

    A key told to serve a thread goes to that thread; a key told to serve what is around it goes
    to the thread that held most of the rest within :data:`AROUND_MS` of it, and stays itself
    when there is none; a key nobody told stays itself.
    """
    def follows(p: Piece) -> bool:
        return p.key in threads and threads[p.key].name is None

    own = [(p, p.key if p.key not in threads or follows(p) else thread_id(threads[p.key]))
           for p in group]
    found = []
    for p, thread in own:
        near: dict[str, float] = defaultdict(float)
        if follows(p):
            for q, other in own:
                lo = max(q.start_ms, p.start_ms - AROUND_MS)
                hi = min(q.end_ms, p.end_ms + AROUND_MS)
                if hi > lo and not follows(q):
                    near[other] += (hi - lo) * q.share
        found.append((p, max(near, key=lambda t: (near[t], t)) if near else thread))
    return found


def thread_id(thread: Thread) -> str:
    """The id a told thread is summed and served by; a key nobody told is its own id."""
    return f"thread|{thread.name}"


def _tally(
    group: Sequence[tuple[Piece, str]], threads: Mapping[str, Thread],
) -> list[dict[str, Any]]:
    """The threads of ``group``, most first, each with its keys, most first."""
    told = {thread_id(t): t for t in threads.values() if t.name}
    ms: dict[str, float] = defaultdict(float)
    typed: dict[str, float] = defaultdict(float)
    where: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    keys: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    names: dict[str, str] = {}
    for p, thread in group:
        ms[thread] += p.ms
        typed[thread] += p.typed_ms
        where[thread][p.where] += p.ms
        keys[thread][p.key] += p.ms
        names.setdefault(p.key, p.name)
    return [
        {
            "id": thread,
            "name": told[thread].name if thread in told else names[thread],
            "category": told[thread].category if thread in told else None,
            "where": max(where[thread], key=lambda w: (where[thread][w], w)),
            "ms": round(ms[thread]), "typed_ms": round(typed[thread]),
            "keys": [
                {"key": key, "name": names[key], "ms": round(keys[thread][key])}
                for key in sorted(keys[thread], key=lambda k: (-keys[thread][k], k))
                if keys[thread][key] >= _MINUTE_MS / 2
            ],
        }
        for thread in sorted(ms, key=lambda t: (-ms[t], t))
        if ms[thread] >= _MINUTE_MS / 2
    ]


def within(
    found: Sequence[Piece], start_ms: int, end_ms: int, threads: Mapping[str, Thread],
) -> dict[str, Any]:
    """Where his attention went in ``[start_ms, end_ms)``, in the served shape (ADR 0330).

    ``threads`` says what keys served (a key not in it is its own thread). ``front_ms`` is the
    time a screen had him; ``threads`` what held it, most first, each ``{"id", "name",
    "category", "where", "ms", "typed_ms", "keys"}`` (``where`` is ``mac``, ``iPhone`` or
    ``iPad``, the one it held most on; ``category`` is null for a key nobody told; ``keys`` each
    ``{"key", "name", "ms"}``); ``rows`` the rows of least cost, by start, each ``{"start_ms",
    "end_ms", "thread", "ms", "others_ms", "switches"}``: from the first to the last moment a
    screen had him inside it, the minutes its lead held, the minutes the others did, and how often
    his attention left the lead; ``alongside`` what his phone or tablet had in front while he was
    at the Mac, as ``threads`` without ``typed_ms``. A thread or key under half a minute is left
    out of the lists, never out of the sums.
    """
    inside = [q for p in found if (q := _clip(p, start_ms, end_ms)) is not None]
    front = _threaded([p for p in inside if not p.alongside], threads)
    totals: dict[str, float] = defaultdict(float)
    for p, thread in front:
        totals[thread] += p.ms
    rank = {t: i for i, t in enumerate(sorted(totals, key=lambda t: (-totals[t], t)))}
    rows = []
    for lo, hi, lead in _rows(front, start_ms, end_ms, rank) if front else []:
        part = [(q, thread) for p, thread in front if (q := _clip(p, lo, hi)) is not None]
        if not part:
            continue
        lead_ms = sum(p.ms for p, thread in part if thread == lead)
        rows.append({
            "start_ms": min(p.start_ms for p, _ in part),
            "end_ms": max(p.end_ms for p, _ in part),
            "thread": lead, "ms": round(lead_ms),
            "others_ms": round(sum(p.ms for p, _ in part) - lead_ms),
            "switches": _switches(part, lead),
        })
    alongside = [
        {k: v for k, v in one.items() if k != "typed_ms"}
        for one in _tally(_threaded([p for p in inside if p.alongside], threads), threads)
    ]
    return {
        "front_ms": round(sum(p.ms for p, _ in front)),
        "threads": _tally(front, threads),
        "rows": rows,
        "alongside": alongside,
    }


__all__ = [
    "AROUND_MS",
    "MAC",
    "REACH_MS",
    "ROW_COST_MS",
    "TITLE_MAX_CHARS",
    "UNIT_MS",
    "Piece",
    "Thread",
    "held",
    "mac_spans",
    "pieces",
    "read",
    "thread_id",
    "within",
]
