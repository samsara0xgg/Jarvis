"""ADR 9901: the doing-lane writer reads what his Mac's screen said, window by window.

Checks, input to expected output:

- a stretch's windows hold every line of every capture on screen in it, each once per window with
  the moment it was first seen (the stretch's start for one taken before it); a line kept in one
  window is kept again in another; a capture outside the stretch is left out;
- the shipped captures are keyed by the span they were taken in, else by their own app and title,
  and a read of one key holds only its windows; nothing shipped reads nothing;
- an answer names the stretch and the key, heads each window and times its new lines; a long read
  is cut into pages at line ends that lose no line, each page after the first reopening its
  window; a bad time, key or page, and a store that fails, are told as errors.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision.screen_reads import PAGE_CHARS, answer, lines, pages
from jarvis.state import screen_text
from jarvis.state.day_line import DaySources
from jarvis.state.screen_text import Capture, Window
from jarvis.state.shipments import Shipments

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

VANCOUVER = ZoneInfo("America/Vancouver")
DAY = date(2026, 10, 7)


def _ms(hour: int, minute: int = 0, second: int = 0) -> int:
    return int(datetime.combine(DAY, time(hour, minute, second), VANCOUVER).timestamp()) * 1000


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S.000")


def _capture(start: int, end: int, app: str, title: str, text: str) -> Capture:
    return Capture(start, end, app, title, f"mac|{app}|{title}", text)


# --- windows -------------------------------------------------------------------------------------


def test_a_stretchs_windows_hold_every_line_once_with_when_it_was_first_seen() -> None:
    """Lines repeat across captures of one window only once; another window keeps its own."""
    found = [
        _capture(_ms(9, 50), _ms(10, 5), "WeChat", "妈妈", "妈妈: 吃饭了吗\n我:  还没 "),
        _capture(_ms(10, 6), _ms(10, 9), "WeChat", "妈妈", "妈妈: 吃饭了吗\n我: 还没\n妈妈: 快去"),
        _capture(_ms(10, 7), _ms(10, 8), "Chrome", "放映厅", "妈妈: 吃饭了吗\n\n放映厅 · 抖音"),
        _capture(_ms(10, 10), _ms(10, 12), "WeChat", "妈妈", "我: 还没!"),
        _capture(_ms(11), _ms(11, 5), "WeChat", "妈妈", "after the stretch"),
        _capture(_ms(9), _ms(9, 10), "WeChat", "妈妈", "before the stretch"),
    ]
    got = screen_text.windows(found, _ms(10), _ms(11))
    assert [(w.app, w.title, w.first_ms, w.last_ms) for w in got] == [
        ("WeChat", "妈妈", _ms(10), _ms(10, 12)),
        ("Chrome", "放映厅", _ms(10, 7), _ms(10, 8)),
    ]
    assert got[0].lines == [
        (_ms(10), "妈妈: 吃饭了吗"), (_ms(10), "我: 还没"), (_ms(10, 6), "妈妈: 快去"),
        (_ms(10, 10), "我: 还没!"),  # differs by a character: kept
    ]
    assert got[1].lines == [(_ms(10, 7), "妈妈: 吃饭了吗"), (_ms(10, 7), "放映厅 · 抖音")]
    for capture in found[:4]:  # nothing on screen in the stretch is lost
        (window,) = (w for w in got if (w.app, w.title) == (capture.app, capture.title))
        assert set(screen_text.lines_of(capture.text)) <= {text for _, text in window.lines}


def _ship_captures(
    directory: Path, spans: Sequence[dict[str, Any]], shots: Sequence[Capture],
) -> None:
    """Spans and captures as the Mac terminal ships them; a capture's ``key`` is its span's id."""
    rows = [
        {
            "key": f"span:{span['id']}:{'b' * 12}", "kind": "span", "zone": "UTC",
            "at_ms": span["at_ms"], "text": "",
            "fields": {
                "appName": "Claude", "domain": None, "document": None, "title": None,
                "keySeconds": 0, **{k: v for k, v in span.items() if k != "at_ms"},
            },
        }
        for span in spans
    ] + [
        {
            "key": f"capture:{n}:{'c' * 12}", "kind": "capture", "zone": "UTC", "at_ms": shot.at_ms,
            "text": shot.text,
            "fields": {
                "id": n, "at": _utc(shot.at_ms), "lastSeenAt": _utc(shot.last_ms),
                "appName": shot.app, "title": shot.title, "spanID": shot.key or None,
                "text": shot.text,
            },
        }
        for n, shot in enumerate(shots, 1)
    ]
    Shipments(directory, min_free_bytes=0).receive_entries(
        "timesink", "\n".join(map(json.dumps, rows)).encode(),
    )


def test_shipped_captures_are_keyed_by_their_span_and_a_read_of_a_key_holds_only_its_windows(
    tmp_path: Path,
) -> None:
    """A capture in a GitHub span is keyed by the site; one with no span by its own app."""
    spans = [{
        "id": 7, "at_ms": _ms(10), "appName": "Chrome", "domain": "github.com", "title": "PR 12",
        "start": _utc(_ms(10)), "end": _utc(_ms(10, 30)),
    }]
    _ship_captures(tmp_path / "imports", spans, [
        Capture(_ms(10, 1), _ms(10, 4), "Chrome", "PR 12", "7", "Fix the writer\nLGTM"),
        Capture(_ms(10, 5), _ms(10, 6), "WeChat", "", "", "早"),
    ])
    sources = DaySources(VANCOUVER, shipped=tmp_path / "imports")
    every = screen_text.seen(sources, _ms(10), _ms(11))
    assert [(w.app, w.key, [t for _, t in w.lines]) for w in every] == [
        ("Chrome", "mac|Chrome|github.com PR 12", ["Fix the writer", "LGTM"]),
        ("WeChat", "mac|WeChat|", ["早"]),
    ]
    assert [w.app for w in screen_text.seen(sources, _ms(10), _ms(11), "mac|WeChat|")] == [
        "WeChat",
    ]
    assert screen_text.seen(DaySources(VANCOUVER, shipped=tmp_path / "none"), 0, _ms(11)) == []


# --- answers -------------------------------------------------------------------------------------

TIMES = {"t0": _ms(10), "t1": _ms(10, 30), "t2": _ms(11)}
KEYS = {"k1": "mac|WeChat|", "v1": "mac|Claude|"}
NAMES = {"mac|WeChat|": "WeChat", "mac|Claude|": "Claude"}
WECHAT = Window("WeChat", "妈妈", "mac|WeChat|", _ms(10), _ms(10, 9), [
    (_ms(10), "妈妈: 吃饭了吗"), (_ms(10, 6, 30), "妈妈: 快去"),
])


def _reader(found: Sequence[Window], asked: list[tuple[int, int, str | None]]) -> Any:  # noqa: ANN401
    def read(start: int, end: int, key: str | None) -> Sequence[Window]:
        asked.append((start, end, key))
        return found

    return read


def test_an_answer_names_the_stretch_and_key_heads_each_window_and_times_its_lines() -> None:
    """One page, the key named by its label and name; times from the labels given."""
    asked: list[tuple[int, int, str | None]] = []
    text, error = answer(
        {"from": "t0", "to": "t1", "key": "k1"}, TIMES, KEYS, NAMES, _reader([WECHAT], asked),
        VANCOUVER,
    )
    assert (error, asked) == (False, [(_ms(10), _ms(10, 30), "mac|WeChat|")])
    assert text == (
        "SCREEN 10:00-10:30, k1 WeChat, page 1 of 1:\n"
        "== 10:00-10:09 | WeChat | 妈妈\n@10:00:00\n妈妈: 吃饭了吗\n@10:06:30\n妈妈: 快去"
    )
    nothing = answer({"from": "t1", "to": "t2"}, TIMES, KEYS, NAMES, _reader([], []), VANCOUVER)
    assert nothing == (
        "SCREEN 10:30-11:00, every window: nothing was read off the screen then", False,
    )


def test_a_long_read_is_cut_into_pages_at_line_ends_and_loses_no_line() -> None:
    """Each page under the cap reopens its window and the time of its next lines."""
    long = Window("Ghostty", "jarvis", "mac|Ghostty|jarvis", _ms(10), _ms(10, 20), [
        (_ms(10, n // 100), f"line {n} " + "x" * 90) for n in range(400)
    ])
    read = lines([WECHAT, long], VANCOUVER)
    cut = pages(read)
    assert len(cut) > 2
    assert all(len(page) <= PAGE_CHARS for page in cut)
    kept = [line for page in cut for line in page.split("\n")]
    assert [t for kind, t in read if kind == "text"] == [
        line for line in kept if line.startswith(("line ", "妈妈"))
    ]
    reopened = "== 10:00-10:20 | Ghostty | jarvis (continued)\n@10:"
    assert all(page.startswith(reopened) for page in cut[1:])
    second, error = answer(
        {"from": "t0", "to": "t2", "page": 2}, TIMES, KEYS, NAMES, _reader([WECHAT, long], []),
        VANCOUVER,
    )
    assert not error
    assert second == f"SCREEN 10:00-11:00, every window, page 2 of {len(cut)}:\n{cut[1]}"


def _broken(_start: int, _end: int, _key: str | None) -> Sequence[Window]:
    msg = "disk I/O error"
    raise OSError(msg)


_TIMES_BAD = "from and to must be two listed times, the first earlier"
_NO_PAGE = "SCREEN 10:00-10:30, every window: page must be from 1 to 1"


@pytest.mark.parametrize(("arguments", "said"), [
    ({"from": "t9", "to": "t1"}, _TIMES_BAD),
    ({"from": "t1", "to": "t0"}, _TIMES_BAD),
    ({"from": "t0"}, _TIMES_BAD),
    ({"from": "t0", "to": "t1", "key": "k7"}, "'k7' is not a key of NEW KEYS or VARYING KEYS"),
    ({"from": "t0", "to": "t1", "page": 2}, _NO_PAGE),
    ({"from": "t0", "to": "t1", "page": "1"}, _NO_PAGE),
    ({"from": "t0", "to": "t1", "page": True}, _NO_PAGE),
])
def test_a_bad_read_is_told_why(arguments: dict[str, Any], said: str) -> None:
    """A time or key not given, a stretch run backwards, a page that is not there."""
    assert answer(arguments, TIMES, KEYS, NAMES, _reader([WECHAT], []), VANCOUVER) == (said, True)


def test_a_store_that_fails_is_told_and_not_raised() -> None:
    """The writer is told the screen could not be read and goes on without it."""
    assert answer({"from": "t0", "to": "t1"}, TIMES, KEYS, NAMES, _broken, VANCOUVER) == (
        "the screen could not be read (OSError)", True,
    )
