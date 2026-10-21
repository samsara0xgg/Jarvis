"""What the devices said, as the Event Log answers it (ADR 0349).

The runtime's voice paths ask which sentences another device or an earlier turn of one device
recorded. Each test writes ``utterance.received`` rows under the device that heard them in a real
on-disk Event Log and asserts the answer; times are fixed epoch milliseconds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.state.device_utterances import (
    Utterance,
    first_utterance_row,
    latest_utterances,
    transcripts_heard_elsewhere_since,
    utterances_before,
)
from tests.support.turn_rows import TurnLog

if TYPE_CHECKING:
    from pathlib import Path

PHONE = "iphone-2"
WATCH = "watch-1"


def test_the_words_other_devices_recorded_since(tmp_path: Path) -> None:
    """Every other device's utterance at or after the time counts, on whatever channel."""
    log = TurnLog(tmp_path)
    log.heard("T-old", "too old", at=900, node=PHONE)
    log.heard("T-phone", "what time is it", at=1_000, node=PHONE, channel="phone_voice")
    log.heard("T-watch", "set a timer", at=2_000, node=WATCH, channel="inherent_wake")
    log.heard("T-mac", "my own words", at=2_000)
    log.typed("T-typed", "gpt_live", at=2_000)

    assert sorted(transcripts_heard_elsewhere_since(log.conn, "mac", since_ms=1_000)) == [
        "set a timer", "what time is it",
    ]
    assert transcripts_heard_elsewhere_since(log.conn, PHONE, since_ms=1_000) == [
        "set a timer", "my own words",
    ]
    assert transcripts_heard_elsewhere_since(log.conn, PHONE, since_ms=2_001) == []


def test_the_log_row_of_the_first_sentence_a_turn_heard(tmp_path: Path) -> None:
    """The earliest utterance row of the turn; a turn that heard nothing has none."""
    log = TurnLog(tmp_path)
    assert first_utterance_row(log.conn, "T1") is None
    log.typed("T1", "gpt_live")
    assert first_utterance_row(log.conn, "T1") is None
    log.heard("T0")
    first = log.heard("T1", "first")
    log.heard("T1", "second")
    assert first_utterance_row(log.conn, "T1") == log.conn.execute(
        "SELECT id FROM events WHERE event_uid = ?", (first.event_uid,),
    ).fetchone()[0]


def test_the_sentences_a_device_recorded_before_a_row(tmp_path: Path) -> None:
    """Only that device's sentences before the row, newest first; other rows are not read."""
    log = TurnLog(tmp_path)
    log.heard("T1", "one", at=1_000, node=PHONE)
    log.heard("T2", "elsewhere", at=1_100, node=WATCH)
    log.heard("T3", "two", at=1_200, node=PHONE)
    log.typed("T-typed", "gpt_live", at=1_250)
    log.put("turn.started", {"turn_id": "T3"}, turn_id="T3", node=PHONE)
    log.heard("T4", "three", at=1_300, node=PHONE)
    log.heard("T5", "after", at=1_400, node=PHONE)
    row = first_utterance_row(log.conn, "T4")
    assert row is not None

    assert utterances_before(log.conn, PHONE, row_id=row) == [
        Utterance("T3", "two", 1_200),
        Utterance("T1", "one", 1_000),
    ]
    assert utterances_before(log.conn, "mac", row_id=row) == []


def test_the_last_twenty_sentences_of_a_device(tmp_path: Path) -> None:
    """Newest first and no more than twenty, whichever device wrote the rows around them."""
    log = TurnLog(tmp_path)
    for number in range(22):
        log.heard(f"T{number}", f"words {number}", at=1_000 + number, node=PHONE)
        log.heard(f"M{number}", "the mac's", at=1_000 + number)

    latest = latest_utterances(log.conn, PHONE)
    assert [one.turn_id for one in latest] == [f"T{number}" for number in range(21, 1, -1)]
    assert latest[0] == Utterance("T21", "words 21", 1_021)
    assert latest_utterances(log.conn, WATCH) == []
