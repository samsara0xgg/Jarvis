"""A slow answer comes after the one she is saying, and says what it answers.

ADR 0107 (``realtime.response.slow_results``).
2026-09-29 live: the email took 30 s; Allen asked something else meanwhile.
The new turn looked the email up again, its line cut off the email answer as
it began, and the other question went unanswered. The lane cases drive the
real media owner with the L3 policy it is given in production; the turn cases
drive two real ``drive_turn`` calls at once against a localhost
/v1/responses peer, only the model's output scripted.
"""

# ruff: noqa: RUF001 — the answers carry fullwidth commas.
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.response_run import decide_foreground
from jarvis.runtime import make_foreground_decision_callable
from jarvis.shared.realtime import Wave4ResponseFlags
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.turn_overlap import turns_in_flight
from jarvis.surface import voice_media
from tests.integration.test_foreground_arbitration import _emit_emitted, _emit_open, _whole
from tests.integration.test_spoken_streaming import (
    _fixture_key,  # noqa: F401 — autouse fixture: the key and the phase labels
    _Peer,
    _spoken,
    _spoken_runtime,
)
from tests.integration.test_wave2_streaming_media import (
    _Behavior,
    _CallbackPump,
    _config,
    _FakeProvider,
    _player,
    _submit_response,
    _terminal_rows,
)
from tests.integration.test_wire_routine_streaming import _drive, _wait_for

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.shared import Event

_WAIT = partial(decide_foreground, wait_for_lane=True)


def _pipeline(
    db_path: Path, provider: _FakeProvider, decision: Callable[..., str],
) -> tuple[voice_media.StreamingTTSPipeline, Any]:
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        foreground_decision_callable=decision,
        start_player=False,
    )
    return pipeline, player


def _outcomes(db_path: Path) -> list[tuple[str, str]]:
    """Each playback terminal in the order it was written."""
    return [
        (str(payload["response_id"]), kind.removeprefix("surface.playback_"))
        for kind, payload in _terminal_rows(open_event_log(db_path))
    ]


def test_the_switch_is_what_the_runtime_hands_the_media_owner() -> None:
    """Off, the old policy; on (shipped), a cross-group answer waits, older or newer."""
    off, on = make_foreground_decision_callable(), make_foreground_decision_callable(
        wait_for_lane=True,
    )
    assert (off("GA", 5, "GB", 9), off("GA", 9, "GB", 5)) == ("supersede", "decline")
    assert (on("GA", 5, "GB", 9), on("GA", 9, "GB", 5)) == (
        "enqueue_after_drain", "enqueue_after_drain",
    )
    assert on("GA", 5, "GA", 9) == off("GA", 5, "GA", 9) == "enqueue_after_drain"
    config = {"slow_results": {"enabled": True}}
    assert Wave4ResponseFlags.from_mapping(config).slow_results
    assert not Wave4ResponseFlags.from_mapping({}).slow_results


@pytest.mark.parametrize("wait", [True, False])
def test_an_answer_arriving_while_she_speaks_waits_for_her_to_finish(
    tmp_path: Path, wait: bool,  # noqa: FBT001 - pytest parameter
) -> None:
    """The live cut: a newer turn's line arrives while the email answer plays.

    Off, the newer line cuts the email answer off. On, the email answer is
    said to its end and the line follows.
    """
    db_path = tmp_path / "lane.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(
        {("RMAIL", 0): _Behavior("success", final_delay_s=0.01)}, candidate_count=1,
    )
    playing = threading.Event()
    provider.segment_gates[("RMAIL", 0)] = playing
    pipeline, player = _pipeline(db_path, provider, _WAIT if wait else decide_foreground)
    try:
        with _CallbackPump(player):
            asyncio.run(_submit_response(pipeline, _whole(
                conn, response_id="RMAIL", group_id="GMAIL", turn_id="TMAIL",
                text="你有三封新邮件。",
            )))
            _wait_for(lambda: ("RMAIL", 0) in provider.opened)
            asyncio.run(_submit_response(pipeline, _whole(
                conn, response_id="RLINE", group_id="GNEXT", turn_id="TNEXT",
                text="我查一下。",
            )))
            playing.set()
            assert pipeline.wait_until_idle(timeout_s=3.0)
    finally:
        assert pipeline.close()
        conn.close()
    if wait:
        assert _outcomes(db_path) == [("RMAIL", "completed"), ("RLINE", "completed")]
    else:
        assert _outcomes(db_path) == [("RMAIL", "interrupted"), ("RLINE", "completed")]


@pytest.mark.parametrize("wait", [True, False])
def test_an_older_turns_late_answer_is_said_after_not_dropped(
    tmp_path: Path, wait: bool,  # noqa: FBT001 - pytest parameter
) -> None:
    """The slow answer opened first and finished last: on, it follows; off, it is lost."""
    db_path = tmp_path / "late.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(
        {("RNOW", 0): _Behavior("success", final_delay_s=0.01)}, candidate_count=1,
    )
    playing = threading.Event()
    provider.segment_gates[("RNOW", 0)] = playing
    pipeline, player = _pipeline(db_path, provider, _WAIT if wait else decide_foreground)
    try:
        with _CallbackPump(player):
            _emit_open(conn, response_id="RMAIL", group_id="GMAIL", turn_id="TMAIL",
                       text="对了，邮件查到了。")
            asyncio.run(_submit_response(pipeline, _whole(
                conn, response_id="RNOW", group_id="GNOW", turn_id="TNOW",
                text="明天晴。",
            )))
            _wait_for(lambda: ("RNOW", 0) in provider.opened)
            asyncio.run(_submit_response(pipeline, _emit_emitted(
                conn, response_id="RMAIL", group_id="GMAIL", turn_id="TMAIL",
                text="对了，邮件查到了。",
            )))
            playing.set()
            assert pipeline.wait_until_idle(timeout_s=3.0)
    finally:
        assert pipeline.close()
        conn.close()
    if wait:
        assert _outcomes(db_path) == [("RNOW", "completed"), ("RMAIL", "completed")]
    else:
        assert _outcomes(db_path) == [("RNOW", "completed")]


def test_her_own_next_part_goes_ahead_of_another_turns_waiting_answer(tmp_path: Path) -> None:
    """Lead-in playing, the slow answer waiting, then the lead-in's answer: it goes first."""
    db_path = tmp_path / "order.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(
        {("RLINE", 0): _Behavior("success", final_delay_s=0.01)}, candidate_count=1,
    )
    playing = threading.Event()
    provider.segment_gates[("RLINE", 0)] = playing
    pipeline, player = _pipeline(db_path, provider, _WAIT)
    try:
        with _CallbackPump(player):
            asyncio.run(_submit_response(pipeline, _whole(
                conn, response_id="RLINE", group_id="GNOW", turn_id="TNOW", text="我看看。",
            )))
            _wait_for(lambda: ("RLINE", 0) in provider.opened)
            asyncio.run(_submit_response(pipeline, _whole(
                conn, response_id="RMAIL", group_id="GMAIL", turn_id="TMAIL",
                text="对了，邮件查到了。",
            )))
            asyncio.run(_submit_response(pipeline, _whole(
                conn, response_id="RNOW", group_id="GNOW", turn_id="TNOW", text="明天晴。",
            )))
            playing.set()
            assert pipeline.wait_until_idle(timeout_s=3.0)
    finally:
        assert pipeline.close()
        conn.close()
    assert _outcomes(db_path) == [
        ("RLINE", "completed"), ("RNOW", "completed"), ("RMAIL", "completed"),
    ]


class _FirstHeldPeer(_Peer):
    """The first request waits for ``go`` before the peer reads it."""

    def __init__(self, outputs: list[list[tuple[str, str]]]) -> None:
        super().__init__(outputs)
        self.arrived = threading.Event()
        self.go = threading.Event()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self.arrived.is_set():
            self.arrived.set()
            await asyncio.get_running_loop().run_in_executor(None, self.go.wait)
        await super()._serve(reader, writer)


def _user_text(body: dict[str, Any]) -> str:
    return "\n".join(
        str(item["content"]) for item in body["input"] if item.get("role") == "user"
    )


@pytest.mark.parametrize("slow", [True, False])
def test_two_turns_at_once_answer_their_own_words(
    tmp_path: Path, slow: bool,  # noqa: FBT001 - pytest parameter
) -> None:
    """Allen asks for his memos, then about the weather before the lookup is back.

    The weather turn is told the memo question is still being answered, not
    that it was cut off; the memo turn's request after its tool is told what
    he said meanwhile.
    """
    outputs = [
        [("final_answer", "明天晴，二十度。")],
        [("commentary", "我看一下你的备忘录。"), ("call", "list_memos")],
        [("final_answer", "对了，备忘录查到了：你还没有记过。")],
    ]
    with _FirstHeldPeer(outputs) as peer:
        runtime = _spoken_runtime(tmp_path, peer.url)
        runtime = replace(
            runtime, response_flags=replace(runtime.response_flags, slow_results=slow),
        )
        memos = _spoken(runtime.conn, "turn-memo", "我记过什么")
        with ThreadPoolExecutor(max_workers=1) as pool:
            memo_turn = pool.submit(_drive, runtime, memos)
            assert peer.arrived.wait(10)
            weather: Event = _spoken(runtime.conn, "turn-weather", "明天天气怎么样")
            _drive(runtime, weather)
            peer.go.set()
            memo_turn.result(timeout=20)
    weather_request, memo_first, memo_after_tool = (body for _path, body in peer.requests)
    in_flight = (
        'Still being answered in another turn: "我记过什么" (asked '
    )
    note = 'While this was being looked up, the user went on to say: "明天天气怎么样".'
    assert "明天天气怎么样" in _user_text(weather_request)
    assert "Still being answered" not in _user_text(memo_first)
    assert "went on to say" not in _user_text(memo_first)
    interrupted = "Previous turn: interrupted before it was answered"
    if slow:
        assert in_flight in _user_text(weather_request)
        # Still running is not cut off: the look-back passes over it.
        assert interrupted not in _user_text(weather_request)
        last = memo_after_tool["input"][-1]
        assert last["role"] == "user"
        assert note in last["content"]
        assert "pointing back to what they asked here" in last["content"]
    else:
        assert "Still being answered" not in _user_text(weather_request)
        assert interrupted in _user_text(weather_request)
        assert "went on to say" not in _user_text(memo_after_tool)


def test_a_turn_that_ended_or_was_cancelled_is_not_in_flight(tmp_path: Path) -> None:
    """Only a turn with no end, no failure and no cancelled run is still being answered."""
    conn = open_event_log(tmp_path / "events.db")
    starts: dict[str, Event] = {}
    for turn_id, words in [("T1", "查邮件"), ("T2", "查日程"), ("T3", "查天气"), ("T4", "嗯")]:
        said = emit_event(conn, type="surface.user_intent",
                          payload={"transcript": words, "turn_id": turn_id})
        starts[turn_id] = said
        emit_event(conn, type="turn.started", payload={"turn_id": turn_id},
                   source_event_id=said.event_uid)
    emit_event(conn, type="turn.ended", payload={"turn_id": "T2"})
    emit_event(conn, type="response.cancelled", payload={
        "turn_id": "T3", "response_id": "R3", "response_group_id": "G3", "reason": "superseded",
    })
    now = starts["T4"]
    earlier = turns_in_flight(conn, trigger_event_uid=now.event_uid, since_ms=0)
    assert [(turn.turn_id, turn.words) for turn in earlier] == [("T1", "查邮件")]
    # Before the window, and from the current trigger's own point of view.
    assert turns_in_flight(conn, trigger_event_uid=now.event_uid, since_ms=10**15) == ()
    assert turns_in_flight(conn, trigger_event_uid=starts["T1"].event_uid, since_ms=0) == ()
