"""``session.recent_records``: the prompt shows the latest records, the rest stay searchable.

About 500 records and 40k tokens went with every turn on 2026-09-30. With the
window on, the history is the summary, one note on the earlier records, and
the latest N to 2N-1 records; the cut moves N at a time, so the provider's
prefix cache keeps the history's start. Real memory.db, the real
``search_records`` tool body, and, for the cache, the real ``drive_turn`` and
prefix warm against a localhost /v1/responses peer.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from jarvis.state.daily_records import search_records
from jarvis.state.memory_db import SessionSettings, open_memory_db, render_context
from tests.integration.test_prefix_warm import _assert_prefix_of, _runtime, _wait_for_requests
from tests.integration.test_spoken_streaming import (
    _fixture_key,  # noqa: F401 — autouse fixture: the key and the phase labels
    _Peer,
    _spoken,
)
from tests.integration.test_wire_routine_streaming import _drive

if TYPE_CHECKING:
    from pathlib import Path

START = datetime.fromisoformat("2026-09-01T09:00:00-07:00")


def _store(tmp_path: Path, count: int) -> Path:
    """``count`` records, Allen and Jarvis in turn, one minute apart."""
    path = tmp_path / "memory.db"
    with open_memory_db(path) as conn, conn:
        conn.executemany(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
            [
                (
                    f"r{index:03d}",
                    (START + timedelta(minutes=index)).isoformat(),
                    "allen" if index % 2 == 0 else "jarvis",
                    f"第{index}句",
                )
                for index in range(count)
            ],
        )
    return path


def _add(path: Path, index: int) -> None:
    with open_memory_db(path) as conn, conn:
        conn.execute(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
            (f"r{index:03d}", (START + timedelta(minutes=index)).isoformat(),
             "allen" if index % 2 == 0 else "jarvis", f"第{index}句"),
        )


def _said(history: tuple[dict[str, str], ...]) -> list[int]:
    return [int(number) for number in re.findall(r"第(\d+)句", str(history))]


def test_only_the_latest_records_show_and_a_note_says_where_the_rest_are(
    tmp_path: Path,
) -> None:
    """500 records at N=40: records 440-499 show, the note points at the other 440."""
    path = _store(tmp_path, 500)
    everything = render_context(path, exclude_id="").history
    window = render_context(path, exclude_id="", recent=40).history
    assert _said(everything) == list(range(500))
    assert _said(window) == list(range(440, 500))
    first_ts = (START + timedelta(minutes=440)).isoformat()
    assert window[0] == {
        "role": "user",
        "content": (
            f"[Earlier conversation · 440 records before {first_ts} are not shown · "
            f"find them with search_records (to={first_ts}), then read_records]\n"
            "[9月1日 周二]\n第440句"
        ),
    }
    assert len(str(window)) < len(str(everything)) / 7
    # What the note tells the model to do finds a record the window hides.
    found = search_records(path, {"keyword": "第12句", "to": first_ts})
    assert [row["id"] for row in found["records"]] == ["r012"]
    # Off, and under 2N records, nothing is cut.
    assert render_context(path, exclude_id="", recent=0).history == everything
    assert _said(render_context(path, exclude_id="", recent=251).history) == list(range(500))


def test_the_start_holds_for_n_records_then_moves_n(tmp_path: Path) -> None:
    """Each new record extends the last history, except once every N records."""
    path = _store(tmp_path, 60)
    before = render_context(path, exclude_id="", recent=20).history
    moves = []
    for index in range(60, 140):
        _add(path, index)
        after = render_context(path, exclude_id="", recent=20).history
        extends = str(after).startswith(str(before)[:-2])  # the last message may grow
        if not extends:
            moves.append(index + 1)
        assert 20 <= len(_said(after)) <= 39
        before = after
    assert moves == [80, 100, 120, 140]


def test_the_setting_reads_from_the_session_block() -> None:
    """A positive integer turns it on; anything else leaves every record."""
    assert SessionSettings.from_config({"recent_records": 40}).recent_records == 40
    assert SessionSettings.from_config({}).recent_records == 0
    assert SessionSettings.from_config({"recent_records": -3}).recent_records == 0


def test_the_prefix_warm_and_the_next_turn_see_the_same_window(tmp_path: Path) -> None:
    """Six records and a turn make eight: at N=2 both requests start past six of them."""
    outputs = [[("final_answer", "晴天。")], [("final_answer", "好")], [("final_answer", "也是。")]]
    with _Peer(outputs) as peer:
        runtime, _memory = _runtime(tmp_path, peer.url, warm=True)
        runtime = replace(runtime, session=replace(runtime.session, recent_records=2))
        _drive(runtime, _spoken(runtime.conn, "turn-1", "今天天气怎么样"))
        _wait_for_requests(peer, 2)
        _drive(runtime, _spoken(runtime.conn, "turn-2", "明天呢"))
    _first, warm, second = (body for _path, body in peer.requests)
    _assert_prefix_of(warm, second)
    # The note, then the two records of turn 1: Allen's question and the answer.
    note, answer = warm["input"]
    assert note["content"].startswith("[Earlier conversation · 6 records before ")
    assert note["content"].endswith("\n今天天气怎么样")
    assert answer == {"role": "assistant", "content": "晴天。"}
