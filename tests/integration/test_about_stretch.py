"""ADR 0410: 问她 on a stretch of what he was doing finds it by its time, not its id.

Acceptance checks over the links world (a night, three stays, a walk, his Mac's work, a call):

- the window the phone drew comes with the day page's words on the stretch that shares most of
  it and every item it holds, and links to that stretch by its id now;
- a window cut again since the phone drew it (an older id, a start five minutes off) still finds
  the stretch the page has there now, and holds the moment the old window reached;
- a window nothing was recorded in says so and links nowhere;
- the record names where his screens had him and keeps whole items within its length, counting
  the rest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.runtime.about_item import STRETCH_RECORD_MAX_CHARS, AboutItems, Stretch
from jarvis.runtime.daemon.routes import read_day
from tests.support.links import DAY, NOW_MS, ORIGIN, ZONE, page_world

if TYPE_CHECKING:
    from pathlib import Path

MINUTE = 60_000
WORK = f"doing-{ORIGIN + 560 * MINUTE}"
"""His work at the library, 09:20-12:00: the stretch the page cuts when he arrives there."""
WORDS = (
    '{"title":"jarvis","category":"work","basis":"observed","confidence":"low","confirmed":false}'
)
MAC = (
    'work: 2026-10-14 09:10 to 12:00; {"state":"done","active_s":10200,'
    '"projects":[{"name":"jarvis","seconds":8400}],'
    '"apps":[{"name":"Ghostty","seconds":8400},{"name":"Chrome","seconds":1800}]}'
)
LIBRARY = (
    'stay: from 2026-10-14 09:20, still going; {"state":"now","lat":48.46,"lng":-123.31,'
    '"accuracy_m":65.0,"place":"UVic Library"}'
)


def _stretch(start_min: int, end_min: int, stretch_id: str = WORK) -> dict[str, Any]:
    """A ``doing`` about as the phone sends it, its times minutes from 10-14's midnight."""
    return {
        "kind": "doing", "id": stretch_id, "title": "jarvis",
        "start_ms": ORIGIN + start_min * MINUTE, "end_ms": ORIGIN + end_min * MINUTE,
    }


def _about_items(tmp_path: Path) -> AboutItems:
    log, sources = page_world(tmp_path)
    return AboutItems(log, sources, clock=lambda: NOW_MS / 1000)


def test_a_stretch_comes_with_the_pages_words_and_what_it_holds(tmp_path: Path) -> None:
    """The work stretch as the page shows it: its words, his Mac's work and the library stay."""
    log, sources = page_world(tmp_path)
    about_items = AboutItems(log, sources, clock=lambda: NOW_MS / 1000)
    page = read_day(log, ZONE, sources.memory_db, sources.timesink, DAY, NOW_MS)
    (span,) = [s for s in page["spans"] if s["id"] == WORK]
    assert (span["start_ms"], span["end_ms"]) == (ORIGIN + 560 * MINUTE, ORIGIN + 720 * MINUTE)

    assert about_items.record(_stretch(560, 720)) == (
        f"2026-10-14 09:20 to 12:00; his day page has 2026-10-14 09:20 to 12:00 there: {WORDS}; "
        f"2 items of his day line in it | {MAC} | {LIBRARY}"
    )
    assert about_items.row(_stretch(560, 720)) == {
        "kind": "row", "id": WORK, "date": "2026-10-14", "start_ms": span["start_ms"],
        "end_ms": span["end_ms"], "title": "jarvis",
    }


def test_a_stretch_cut_again_is_still_found_by_its_time(tmp_path: Path) -> None:
    """An old id and a start five minutes early: the page's stretch there now, by its id now."""
    about_items = _about_items(tmp_path)
    stale = _stretch(555, 720, f"doing-{ORIGIN + 555 * MINUTE}")

    assert about_items.record(stale) == (
        f"2026-10-14 09:15 to 12:00; his day page has 2026-10-14 09:20 to 12:00 there: {WORDS}; "
        f"3 items of his day line in it | {MAC} | "
        'talk: at 2026-10-14 09:15; {"state":"done","turns":1} | '
        f"{LIBRARY}"
    )
    row = about_items.row(stale)
    assert row is not None
    assert (row["id"], row["start_ms"]) == (WORK, ORIGIN + 560 * MINUTE)


def test_a_window_nothing_was_recorded_in_says_so_and_links_nowhere(tmp_path: Path) -> None:
    """Six days on, nothing: the window alone, and no row to link to."""
    about_items = _about_items(tmp_path)
    later = 6 * 24 * 60
    empty = _stretch(later, later + 30, "doing-1")

    assert about_items.record(empty) == "2026-10-20 00:00 to 00:30; 0 items of his day line in it"
    assert about_items.row(empty) is None


def _item(n: int) -> dict[str, Any]:
    return {
        "id": f"picture-{n}", "kind": "picture", "start_ms": ORIGIN + n * MINUTE,
        "end_ms": ORIGIN + n * MINUTE, "state": "done", "note": "x" * 280,
    }


def test_the_record_names_his_screens_and_counts_the_items_it_has_no_room_for() -> None:
    """Forty long items: whole ones while they fit, then how many more; at most the record's cap."""
    screens = {
        "front_ms": 90 * MINUTE,
        "threads": [
            {"name": "jarvis", "where": "mac", "ms": 60 * MINUTE},
            {"name": "WeChat", "where": "iPhone", "ms": 20.4 * MINUTE},
        ],
    }
    stretch = Stretch(
        ORIGIN + 600 * MINUTE, ORIGIN + 690 * MINUTE, None,
        [_item(600 + n) for n in range(40)], screens, NOW_MS,
    )

    record = stretch.record(ZONE)

    head = (
        "2026-10-14 10:00 to 11:30; "
        "his screens had him 90 min: jarvis 60 min on mac, WeChat 20 min on iPhone; "
        "40 items of his day line in it | picture: at 2026-10-14 10:00; "
    )
    assert record.startswith(head)
    kept = record.count(" | picture: ")
    assert 0 < kept < 40
    assert record.endswith(f" | and {40 - kept} more items")
    assert len(record) <= STRETCH_RECORD_MAX_CHARS
