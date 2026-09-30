"""A turn's request starts with the previous turn's request.

docs/plans/replay-as-sent-proposal.md. OpenAI's prompt cache reuses only a
whole earlier request (2026-09-30 probe: a request differing from a cached one
in its last message alone hit 4,674 of 44,100 tokens), so each turn's user
message is replayed exactly as it was sent. Real ``drive_turn``, a real
memory.db, the real OpenAI SDK against a localhost /v1/responses peer.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis import runtime as runtime_module
from jarvis.state.memory_db import (
    MemorySettings,
    SessionSettings,
    append_record,
    record_sent,
    render_context,
)
from tests.integration.test_spoken_streaming import _Peer, _spoken, _spoken_runtime
from tests.integration.test_wire_routine_streaming import _drive

if TYPE_CHECKING:
    from pathlib import Path

_SENT_HEAD = "[State when this was said | from the program, not the user's words]"


@pytest.fixture(autouse=True)
def _fixture_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTINE_STREAM_FIXTURE_KEY", "synthetic")
    monkeypatch.setattr(runtime_module, "_labels_phases", lambda _base_url: True)


def _two_turns(tmp_path: Path, *, replay_sent: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    outputs = [[("final_answer", "我在。")], [("final_answer", "明天也在。")]]
    with _Peer(outputs) as peer:
        runtime = replace(
            _spoken_runtime(tmp_path, peer.url),
            memory=MemorySettings(db_path=tmp_path / "memory.db", audio_dir=tmp_path / "audio"),
            session=SessionSettings(replay_sent=replay_sent),
        )
        _drive(runtime, _spoken(runtime.conn, "turn-1", "你在吗"))
        _drive(runtime, _spoken(runtime.conn, "turn-2", "那明天呢"))
    (_, first), (_, second) = peer.requests
    return first, second


def test_the_next_turn_starts_with_the_last_turns_request(tmp_path: Path) -> None:
    """Instructions, tools and every input item of turn 1 open turn 2's request."""
    first, second = _two_turns(tmp_path, replay_sent=True)
    assert second["instructions"] == first["instructions"]
    assert second["tools"] == first["tools"]
    assert second["input"][: len(first["input"])] == first["input"]
    assert len(second["input"]) == len(first["input"]) + 2
    # What every voice turn repeats rides the system prompt, cached once.
    assert "Your reply is spoken aloud" in first["instructions"]
    sent = first["input"][-1]["content"]
    assert sent.startswith("[State when this was said | from the program")
    assert "Channel: voice" in sent
    assert "Your reply is spoken aloud" not in sent
    assert sent.endswith("你在吗")


def test_without_it_the_history_drops_the_state_block(tmp_path: Path) -> None:
    """ADR 0044's layout: turn 1's message comes back as its bare words."""
    first, second = _two_turns(tmp_path, replay_sent=False)
    assert second["input"][0] != first["input"][0]
    assert second["input"][0]["content"].endswith("你在吗")
    assert "Current state" in first["input"][-1]["content"]


def test_the_recent_window_replays_kept_rows_and_holds_its_head(tmp_path: Path) -> None:
    """With ``recent_records`` on too, the rows shown replay as sent between moves."""
    db = tmp_path / "memory.db"
    histories = []
    for n in range(1, 14):
        source = "allen" if n % 2 else "jarvis"
        append_record(db, record_id=f"r{n}", source=source, text=f"行{n}")
        if source == "allen":
            record_sent(db, f"r{n}", f"{_SENT_HEAD}\n行{n}")
        histories.append(render_context(db, exclude_id="", recent=3).history)
    moves = [
        n
        for n in range(2, 14)
        if histories[n - 1][: len(histories[n - 2])] != histories[n - 2]
    ]
    assert moves == [6, 9, 12]
    assert histories[4][0]["content"] == f"{_SENT_HEAD}\n行1"
    head, _answer, kept, _last = histories[11]
    assert head["content"].startswith("[Earlier conversation · 9 records before ")
    assert kept["content"] == f"{_SENT_HEAD}\n行11"
