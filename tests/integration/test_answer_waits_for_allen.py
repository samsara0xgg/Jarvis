"""ADR 0053: no answer starts while Allen talks; his next sentence drops the unspoken one.

Three seams, each on the real code it names. The capture side holds answers
from speech onset until the utterance is accepted or comes to nothing, and
asks for the drop before ``utterance.received`` exists. The runtime keeps
every run from completing during the hold, so the drop cancels an open run
(``superseded``) and nothing of it is rendered. The media lane parks an answer
that would start during the hold and discards a dropped one.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import anthropic  # noqa: F401 - the first request of a process imports the SDKs; not in a timing
import openai  # noqa: F401
import pytest
from fastapi.testclient import TestClient

from jarvis.decision.response_run import ResponseCancelledError
from jarvis.runtime import (
    device_voice,
    inherent_loop,
    make_compose_callable,
    make_continuation_callable,
    make_relation_supersede_callable,
    make_supersede_unspoken_callable,
)
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log
from jarvis.state.memory_db import MemorySettings, render_context
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.state.projections import PendingConfirmations
from jarvis.surface import voice_media
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.phone_link import PHONE_PATH, PhoneHub
from jarvis.surface.terminal_events import BrainEvents
from jarvis.surface.terminal_link import TerminalHub
from jarvis.surface.voice_controls import VoiceControls
from tests.support.answer_hold import AcceptingPipeline, answer_of, played_texts
from tests.support.conversation_mode import ModeSession
from tests.support.media import (
    Behavior,
    CallbackPump,
    FakeProvider,
    make_player,
    media_config,
    submit_response,
)
from tests.support.phone import REMOTE, open_ws, say_words
from tests.support.response_run import (
    drive_turn_on_own_connection,
    emit_intent,
    event_count,
    make_runtime,
    payloads_of,
)
from tests.support.routine import drive_streaming_turn
from tests.support.spoken import (
    SpokenPeer,
    fixture_key,  # noqa: F401 - autouse fixture of the spoken-route peer
    spoken_runtime,
)
from tests.support.voice_input import wait_until_true

if TYPE_CHECKING:
    from pathlib import Path


# --- capture side ------------------------------------------------------------


def _capture_rig(
    monkeypatch: pytest.MonkeyPatch, *, empty: bool = False, said: str = "明天上午十点提醒我",
) -> tuple[ModeSession, list[str]]:
    record: list[str] = []
    rig = ModeSession(
        monkeypatch,
        pipeline=AcceptingPipeline(record, empty=empty, said=said),
        hold_output=lambda held: record.append("hold" if held else "release"),
        supersede_unspoken=lambda turn_id: record.append(f"supersede {turn_id}"),
    )
    return rig, record


def test_a_sentence_holds_answers_and_drops_the_unspoken_one_before_it_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Onset holds; acceptance asks for the drop, then writes, then releases."""
    rig, record = _capture_rig(monkeypatch)
    try:
        rig.speak()
        wait_until_true(lambda: "release" in record)
        turn_id = rig.pipeline.calls[0]["turn_id"]
        assert record == ["hold", f"supersede {turn_id}", "utterance.received", "release"]
    finally:
        rig.close()


def test_a_sound_that_comes_to_nothing_releases_and_drops_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was said: the hold lifts and no answer is dropped."""
    rig, record = _capture_rig(monkeypatch, empty=True)
    try:
        rig.speak()
        wait_until_true(lambda: "release" in record)
        assert record == ["hold", "release"]
    finally:
        rig.close()


def test_a_dismissal_drops_the_answer_still_on_its_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 2026-10-02: an answer to the sentence before 「退下吧」 played after her goodbye."""
    rig, record = _capture_rig(monkeypatch, said="退下吧")
    try:
        rig.speak()
        wait_until_true(lambda: "release" in record)
        # The words were no turn, so nothing is written; the earlier answer is dropped.
        assert record[0] == "hold"
        assert record[1].startswith("supersede T")
        assert record[2:] == ["release"]
        assert rig.changes == [(False, "dismissed")]
    finally:
        rig.close()


# --- runtime -----------------------------------------------------------------


def _heard(conn: Any, turn_id: str) -> None:  # noqa: ANN401 - sqlite3.Connection
    """Mark ``turn_id`` as a voice sentence, the way the capture side commits one."""
    emit_event(
        conn,
        type="utterance.received",
        payload={"transcript": "怎么说", "turn_id": turn_id, "channel": "inherent_wake"},
        correlation={"turn_id": turn_id},
    )


def _wait_open(runtime: Any, count: int) -> None:  # noqa: ANN401 - JarvisRuntime
    assert runtime.response_runs is not None
    wait_until_true(lambda: len(runtime.response_runs.open_runs()) == count)


def test_no_answer_completes_while_allen_talks(
    tmp_path: Path,
) -> None:
    """The run waits at its completion while held and completes once released."""
    runtime = make_runtime(tmp_path, lifecycle=True, cancel=True)
    assert runtime.response_runs is not None
    runtime.response_runs.hold_completion(held=True)
    intent = emit_intent(runtime.conn, "T-first")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            drive_turn_on_own_connection,
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
        _wait_open(runtime, 1)
        time.sleep(0.3)
        assert event_count(runtime.conn, "response.completed") == 0
        assert event_count(runtime.conn, "surface.response_emitted") == 0
        runtime.response_runs.hold_completion(held=False)
        future.result(timeout=10)
    assert event_count(runtime.conn, "response.completed") == 1
    assert event_count(runtime.conn, "surface.response_emitted") == 1
    runtime.conn.close()


def test_the_next_sentence_cancels_only_an_unspoken_answer_to_a_recent_sentence(
    tmp_path: Path,
) -> None:
    """Of four held answers only the unspoken voice and card ones are dropped.

    ``T-voice`` answers a voice sentence and never reached the speaker: dropped.
    ``T-card`` answers a filled-in ask card (ADR 0072) the same way: dropped.
    ``T-heard`` answers a voice sentence, but L5 already played some of it: kept.
    ``T-typed`` answers typed text: kept. The kept two complete on release.
    """
    runtime = make_runtime(tmp_path, lifecycle=True, cancel=True)
    assert runtime.response_runs is not None
    runtime.response_runs.hold_completion(held=True)
    asked: list[frozenset[str]] = []

    def _drop(turn_ids: frozenset[str]) -> frozenset[str]:
        asked.append(turn_ids)
        return turn_ids - {"T-heard"}

    supersede = make_supersede_unspoken_callable(runtime, _drop)
    intents = {turn: emit_intent(runtime.conn, turn) for turn in ("T-voice", "T-heard", "T-typed")}
    intents["T-card"] = emit_event(
        runtime.conn,
        type="surface.user_intent",
        payload={"transcript": "邮箱: a@b.c", "turn_id": "T-card", "channel": "clarify"},
        correlation={"turn_id": "T-card"},
    )
    _heard(runtime.conn, "T-voice")
    _heard(runtime.conn, "T-heard")
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            turn: pool.submit(
                drive_turn_on_own_connection,
                runtime,
                user_intent_event=intent,
                available_surfaces=frozenset(),
                streaming_enabled=True,
            )
            for turn, intent in intents.items()
        }
        _wait_open(runtime, 4)
        supersede("T-next")
        for dropped in ("T-voice", "T-card"):
            with pytest.raises(ResponseCancelledError):
                futures[dropped].result(timeout=10)
        runtime.response_runs.hold_completion(held=False)
        futures["T-heard"].result(timeout=10)
        futures["T-typed"].result(timeout=10)

    assert asked == [frozenset({"T-voice", "T-card", "T-heard"})]
    cancelled = payloads_of(runtime.conn, "response.cancelled")
    assert sorted((row["turn_id"], row["reason"]) for row in cancelled) == [
        ("T-card", "superseded"), ("T-voice", "superseded"),
    ]
    emitted = {row["turn_id"] for row in payloads_of(runtime.conn, "surface.response_emitted")}
    assert emitted == {"T-heard", "T-typed"}
    runtime.conn.close()


def test_a_dropped_turn_takes_its_waiting_card_with_it(
    tmp_path: Path,
) -> None:
    """ADR 0074: the card the first half put up is rejected as ``superseded``, so none waits."""
    runtime = make_runtime(tmp_path, lifecycle=True, cancel=True)
    assert runtime.response_runs is not None
    runtime.response_runs.hold_completion(held=True)
    intent = emit_intent(runtime.conn, "T-first")
    _heard(runtime.conn, "T-first")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            drive_turn_on_own_connection,
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
        _wait_open(runtime, 1)
        emit_event(
            runtime.conn,
            type="confirmation.requested",
            payload={
                "confirmation_id": "C-first",
                "action_snapshot": {"tool_name": "mcp__gmail__gmail_send", "args_meta": {}},
                "template_line": "要发吗",
                "expires_at_ms": int(time.time() * 1000) + 60_000,
            },
            correlation={"turn_id": "T-first", "action_id": "A-first"},
        )
        make_supersede_unspoken_callable(runtime, lambda turn_ids: turn_ids)("T-next")
        with pytest.raises(ResponseCancelledError):
            future.result(timeout=10)
        runtime.response_runs.hold_completion(held=False)

    rejected = payloads_of(runtime.conn, "confirmation.rejected")
    assert [(row["confirmation_id"], row["grammar_rule_id"]) for row in rejected] == [
        ("C-first", "superseded"),
    ]
    slot = PendingConfirmations.from_events(
        iter_events_of_types(runtime.conn, ("confirmation.requested", "confirmation.rejected")),
    ).slot
    assert slot is not None
    assert not slot.is_live(int(time.time() * 1000))
    runtime.conn.close()


@pytest.mark.parametrize(
    ("slow", "dispatched", "superseded"),
    [(True, True, False), (True, False, True), (False, True, True)],
)
def test_a_turn_already_working_on_a_tool_is_superseded_only_without_slow_results(
    tmp_path: Path,
    slow: bool,  # noqa: FBT001 - pytest parameter
    dispatched: bool,  # noqa: FBT001 - pytest parameter
    superseded: bool,  # noqa: FBT001 - pytest parameter
) -> None:
    """With slow_results on, a turn that dispatched a tool is kept.

    Live test 2026-10-01: a sentence 7 s after a search began must not cancel
    it. Without a dispatched action, or with the flag off, ADR 0053/0074 still
    drops the unspoken answer.
    """
    runtime = make_runtime(tmp_path, lifecycle=True, cancel=True)
    runtime = replace(runtime, response_flags=replace(runtime.response_flags, slow_results=slow))
    assert runtime.response_runs is not None
    runtime.response_runs.hold_completion(held=True)
    intent = emit_intent(runtime.conn, "T-first")
    _heard(runtime.conn, "T-first")
    if dispatched:
        emit_event(
            runtime.conn,
            type="action.dispatched",
            payload={"action_id": "A-first"},
            correlation={"turn_id": "T-first", "action_id": "A-first"},
        )
    dropped: list[frozenset[str]] = []

    def _drop(turn_ids: frozenset[str]) -> frozenset[str]:
        dropped.append(turn_ids)
        return turn_ids

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            drive_turn_on_own_connection,
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
        _wait_open(runtime, 1)
        make_supersede_unspoken_callable(runtime, _drop)("T-next")
        if superseded:
            with pytest.raises(ResponseCancelledError):
                future.result(timeout=10)
        runtime.response_runs.hold_completion(held=False)
        if not superseded:
            future.result(timeout=10)

    assert dropped == [frozenset({"T-first"} if superseded else ())]
    assert len(payloads_of(runtime.conn, "response.cancelled")) == int(superseded)
    runtime.conn.close()


@pytest.mark.parametrize(
    ("relation", "unspoken", "dispatched", "outcome", "stopped"),
    [
        ("supplement", True, False, "superseded", False),
        ("correction", True, False, "superseded", False),
        ("supplement", False, False, "audible", False),
        ("correction", False, False, "stopped", True),
        ("supplement", True, True, "working", False),
        ("correction", True, True, "working", False),
    ],
)
def test_a_supplement_or_correction_acts_on_the_one_turn_it_names(  # noqa: PLR0913 - the table's columns
    tmp_path: Path,
    relation: str,
    unspoken: bool,  # noqa: FBT001 - pytest parameter
    dispatched: bool,  # noqa: FBT001 - pytest parameter
    outcome: str,
    stopped: bool,  # noqa: FBT001 - pytest parameter
) -> None:
    """ADR 0139: the earlier answer is dropped, or stopped when audible and corrected.

    The turn's sentence is not within ADR 0074's 10 s window (no ``utterance.received`` at
    all): the relation names the turn, so the window does not bound it. An answer that began
    playing is left to barge-in, except a correction stops it; a turn working on a tool is
    left alone.
    """
    runtime = make_runtime(tmp_path, lifecycle=True, cancel=True)
    assert runtime.response_runs is not None
    runtime.response_runs.hold_completion(held=True)
    intent = emit_intent(runtime.conn, "T-first")
    if dispatched:
        emit_event(
            runtime.conn,
            type="action.dispatched",
            payload={"action_id": "A-first"},
            correlation={"turn_id": "T-first", "action_id": "A-first"},
        )
    asked: list[frozenset[str]] = []
    stops: list[str] = []

    def _drop(turn_ids: frozenset[str]) -> frozenset[str]:
        asked.append(turn_ids)
        return turn_ids if unspoken else frozenset()

    relate = make_relation_supersede_callable(runtime, _drop, lambda: stops.append("stop"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            drive_turn_on_own_connection,
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
        _wait_open(runtime, 1)
        assert relate("T-first", "T-next", relation) == outcome
        cancelled = outcome in {"superseded", "stopped"}
        if cancelled:
            with pytest.raises(ResponseCancelledError):
                future.result(timeout=10)
        runtime.response_runs.hold_completion(held=False)
        if not cancelled:
            future.result(timeout=10)

    assert (stops == ["stop"]) is stopped
    assert len(payloads_of(runtime.conn, "response.cancelled")) == int(cancelled)
    if cancelled:
        (row,) = payloads_of(runtime.conn, "response.cancelled")
        assert (row["turn_id"], row["reason"]) == ("T-first", "superseded")
        assert row["interrupted_by_turn_id"] == "T-next"
    runtime.conn.close()


def test_a_relation_to_a_turn_with_no_open_answer_does_nothing(tmp_path: Path) -> None:
    """The earlier answer already left (or was dropped by the 10 s sweep): no open run."""
    runtime = make_runtime(tmp_path, lifecycle=True, cancel=True)
    relate = make_relation_supersede_callable(runtime, lambda ids: ids, lambda: None)
    assert relate("T-gone", "T-next", "supplement") == "no_open_run"
    runtime.conn.close()


# --- media lane --------------------------------------------------------------


def _media(
    tmp_path: Path, *response_ids: str,
) -> tuple[voice_media.StreamingTTSPipeline, Any, Any]:
    db_path = tmp_path / "hold.db"
    provider = FakeProvider(
        {(rid, 0): Behavior("success", final_delay_s=0.01) for rid in response_ids},
        candidate_count=1,
    )
    player = make_player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=media_config(),
        start_player=False,
    )
    return pipeline, provider, player


def test_an_answer_waits_while_allen_talks_and_a_dropped_one_never_plays(tmp_path: Path) -> None:
    """Both answers park while held; the dropped one never plays, the other plays on release."""
    pipeline, provider, player = _media(tmp_path, "R-drop", "R-keep")
    conn = open_event_log(tmp_path / "hold.db")
    try:
        with CallbackPump(player):
            pipeline.hold_output(held=True)
            asyncio.run(submit_response(pipeline, answer_of(conn, "R-drop", "T-drop")))
            asyncio.run(submit_response(pipeline, answer_of(conn, "R-keep", "T-keep")))
            time.sleep(0.3)
            assert provider.opened == []
            assert not pipeline.is_output_active()
            assert pipeline.drop_unspoken(frozenset({"T-drop"})) == frozenset({"T-drop"})
            pipeline.hold_output(held=False)
            wait_until_true(lambda: played_texts(tmp_path / "hold.db") == ["R-keep"])
            time.sleep(0.2)
    finally:
        assert pipeline.close()
        conn.close()
    assert [rid for rid, _ in provider.opened] == ["R-keep"]
    assert played_texts(tmp_path / "hold.db") == ["R-keep"]


def test_a_turn_that_started_playing_is_never_dropped(tmp_path: Path) -> None:
    """Once any of a turn's audio has played, the drop leaves it to barge-in."""
    pipeline, provider, player = _media(tmp_path, "R-said")
    conn = open_event_log(tmp_path / "hold.db")
    try:
        with CallbackPump(player):
            asyncio.run(submit_response(pipeline, answer_of(conn, "R-said", "T-said")))
            wait_until_true(lambda: bool(provider.opened))
            assert pipeline.drop_unspoken(frozenset({"T-said"})) == frozenset()
            wait_until_true(lambda: played_texts(tmp_path / "hold.db") == ["R-said"])
    finally:
        assert pipeline.close()
        conn.close()


def test_a_sentence_accepted_before_the_earlier_run_opens_still_supersedes_it(
    tmp_path: Path,
) -> None:
    """Live 2026-10-02 23:22: a line 0.3 s after the one before it was swept first.

    The earlier line's run opens ~0.4 s after its words, so the sweep for the
    next line found nothing to cancel and both answers played, one 18 s behind.
    The run must be cancelled as ``superseded`` the moment it opens.
    """
    runtime = make_runtime(tmp_path, lifecycle=True, cancel=True)
    assert runtime.response_runs is not None
    intent = emit_intent(runtime.conn, "T-first")
    _heard(runtime.conn, "T-first")
    make_supersede_unspoken_callable(runtime, lambda turn_ids: turn_ids)("T-next")
    assert not runtime.response_runs.open_runs()
    with pytest.raises(ResponseCancelledError):
        drive_turn_on_own_connection(
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
    cancelled = payloads_of(runtime.conn, "response.cancelled")
    assert [(row["turn_id"], row["reason"]) for row in cancelled] == [("T-first", "superseded")]
    assert cancelled[0]["interrupted_by_turn_id"] == "T-next"
    assert event_count(runtime.conn, "surface.response_emitted") == 0
    runtime.conn.close()


# --- one request for two back-to-back sentences (ADR 0268) -------------------


def _spoken_turn(conn: Any, turn_id: str, words: str) -> Any:  # noqa: ANN401 - Event
    """The two rows a heard sentence leaves: ``utterance.received`` and its intent."""
    emit_event(
        conn,
        type="utterance.received",
        payload={"transcript": words, "turn_id": turn_id, "channel": "inherent_wake"},
        correlation={"turn_id": turn_id},
    )
    return emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": words, "turn_id": turn_id, "channel": "inherent_wake"},
        correlation={"turn_id": turn_id},
    )


# The ADR 0268 tests below drive the sweep alone, as the host's own session does (it has no
# terminal and records its utterances itself); a terminal's sweep leaves the pending sentence to
# composition (ADR 0278), which its tests further down drive end to end.
_FIRST = "我想问一下去学校的公交"
_SECOND = "现在出发能赶上哪一班"


def _merge_rig(
    tmp_path: Path, peer: Any, *, hold_s: float | None = 0.0  # noqa: ANN401 - SpokenPeer
) -> Any:  # noqa: ANN401 - JarvisRuntime
    """The runtime on ``peer``; no answer hold unless a test is about the hold (None: shipped)."""
    runtime = spoken_runtime(tmp_path, peer.url, answer_hold_s=hold_s)
    return replace(runtime, memory=MemorySettings.from_config({}, runtime_root=tmp_path))


def _records(runtime: Any) -> list[tuple[str, str]]:  # noqa: ANN401 - JarvisRuntime
    import sqlite3  # noqa: PLC0415 - one read of memory.db

    with sqlite3.connect(runtime.memory.db_path) as conn:
        rows = conn.execute("SELECT source, text FROM records ORDER BY rowid").fetchall()
    return [(str(source), str(text)) for source, text in rows]


@pytest.mark.parametrize("words", [_SECOND, _FIRST], ids=["another", "the_same_twice"])
def test_a_sentence_one_second_after_the_one_not_yet_answered_is_answered_with_it(
    tmp_path: Path,
    words: str,
) -> None:
    """The first sentence's request is in flight with no word out; the second one arrives.

    The first request is cancelled quietly and names the sentence that took its place; one
    request is answered, carrying both sentences in order; one answer is rendered; nothing
    of his is dropped from the record (live 2026-10-14 21:18: sentences cancelled each
    other and he said the question again). The same words heard twice (22:14: a session
    double) are the same case: one answer, not two.
    """
    answer = "八点二十有一班。"
    with SpokenPeer([[("final_answer", answer)]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        assert runtime.response_runs is not None
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                drive_streaming_turn, runtime, _spoken_turn(runtime.conn, "T-1", _FIRST)
            )
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            time.sleep(1.0)
            peer.first_event_delay_s = 0.0
            make_supersede_unspoken_callable(runtime, lambda turn_ids: turn_ids)("T-2")
            second = pool.submit(
                drive_streaming_turn, runtime, _spoken_turn(runtime.conn, "T-2", words)
            )
            second.result(timeout=20)
            with pytest.raises(ResponseCancelledError):
                first.result(timeout=10)

    (cancelled,) = payloads_of(runtime.conn, "response.cancelled")
    assert (cancelled["turn_id"], cancelled["reason"]) == ("T-1", "superseded")
    assert cancelled["interrupted_by_turn_id"] == "T-2"
    assert not payloads_of(runtime.conn, "response.failed")
    assert not payloads_of(runtime.conn, "turn.failed")
    emitted = payloads_of(runtime.conn, "surface.response_emitted")
    assert [row["turn_id"] for row in emitted] == ["T-2"]
    # One request was answered, and it carries both sentences, the earlier one first.
    asked = json.dumps(peer.requests[-1][1]["input"], ensure_ascii=False)
    if words == _FIRST:
        assert asked.count(_FIRST) == 2
    else:
        assert 0 <= asked.index(_FIRST) < asked.index(words)
    assert peer.completed == 1
    assert _records(runtime) == [("allen", _FIRST), ("allen", words), ("jarvis", answer)]
    runtime.conn.close()


@pytest.mark.parametrize(
    ("words", "answers"),
    [
        (_FIRST, 1),
        ("去学校的公交 。", 1),
        (_SECOND, 2),
    ],
    ids=["the_same_twice", "a_part_of_it", "other_words_keep_the_tool_exemption"],
)
def test_the_same_words_again_while_a_tool_turn_answers_start_no_second_answer(
    tmp_path: Path,
    words: str,
    answers: int,
) -> None:
    """Live 2026-10-14 22:14:19 and 22:14:24: one sentence arrived twice, two answers.

    The first turn called a tool, so ADR 0107 keeps it from being superseded. The repeat is
    a different matter: his row stays, the repeat starts no answer of its own and the
    log names the turn that answers for it. Other words keep the exemption: two answers.
    """
    # A turn that dispatched a tool asks the model again; the script has rounds to spare.
    scripts = [[("final_answer", "八点二十有一班。")]] + [[("final_answer", "好的。")]] * 3
    with SpokenPeer(scripts, first_event_delay_s=1.5) as peer:
        runtime = _merge_rig(tmp_path, peer)
        runtime = replace(
            runtime,
            response_flags=replace(runtime.response_flags, slow_results=True),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                drive_streaming_turn, runtime, _spoken_turn(runtime.conn, "T-1", _FIRST)
            )
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            emit_event(
                runtime.conn,
                type="action.dispatched",
                payload={"action_id": "A-1"},
                correlation={"turn_id": "T-1", "action_id": "A-1"},
            )
            time.sleep(1.0)
            peer.first_event_delay_s = 0.0
            make_supersede_unspoken_callable(runtime, lambda turn_ids: turn_ids)("T-2")
            second = pool.submit(
                drive_streaming_turn, runtime, _spoken_turn(runtime.conn, "T-2", words)
            )
            first.result(timeout=20)
            if answers == 1:
                with pytest.raises(ResponseCancelledError):
                    second.result(timeout=10)
            else:
                second.result(timeout=20)

    cancelled = payloads_of(runtime.conn, "response.cancelled")
    emitted = payloads_of(runtime.conn, "surface.response_emitted")
    assert len(emitted) == answers
    # The repeat never asked the model: only the turns that answer were admitted a request.
    admitted = {
        row["response_id"] for row in payloads_of(runtime.conn, "response.request_admitted")
    }
    assert len(admitted) == answers
    if answers == 1:
        assert [(row["turn_id"], row["reason"]) for row in cancelled] == [("T-2", "superseded")]
        assert cancelled[0]["interrupted_by_turn_id"] == "T-1"
        assert [row["turn_id"] for row in emitted] == ["T-1"]
        records = _records(runtime)
        assert [source for source, _text in records] == ["allen", "allen", "jarvis"]
        assert [text for _source, text in records[:2]] == [_FIRST, words]
        assert records[2][1].startswith("八点二十有一班。")
    else:
        assert not cancelled
    runtime.conn.close()


def test_a_sentence_after_the_first_has_started_speaking_leaves_it_to_barge_in(
    tmp_path: Path,
) -> None:
    """Unchanged: once any of the answer has reached the speaker neither turn is cancelled."""
    with SpokenPeer(
        [[("final_answer", "第一句。第二句。")], [("final_answer", "好的。")]],
        hold=True,
    ) as peer:
        runtime = _merge_rig(tmp_path, peer)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                drive_streaming_turn, runtime, _spoken_turn(runtime.conn, "T-1", _FIRST)
            )
            wait_until_true(lambda: bool(payloads_of(runtime.conn, "surface.response_chunk")))
            # L5 reports the first sentence played: nothing of the turn is left to drop.
            make_supersede_unspoken_callable(runtime, lambda _turn_ids: frozenset())("T-2")
            second = pool.submit(
                drive_streaming_turn, runtime, _spoken_turn(runtime.conn, "T-2", _SECOND)
            )
            peer.release.set()
            first.result(timeout=20)
            second.result(timeout=20)

    assert not payloads_of(runtime.conn, "response.cancelled")
    emitted = payloads_of(runtime.conn, "surface.response_emitted")
    assert sorted(row["turn_id"] for row in emitted) == ["T-1", "T-2"]
    runtime.conn.close()


# --- a phone's sentence that goes on from its last (ADR 0271) ----------------

_THOUGHT = _FIRST + _SECOND


@dataclass
class _PhoneRig:
    """The phone route over the runtime's own log, with the real take-over seam.

    The phone's voice is a fake that records what it was asked; ``audible`` says whether the
    earlier turn's audio had reached the speaker (the answer to ``drop_unspoken``).
    """

    runtime: Any
    audible: bool = False
    voice_calls: list[tuple[str, Any]] = field(default_factory=list)
    taken_over: list[tuple[str, str, str]] = field(default_factory=list)
    client: TestClient = field(init=False)
    events: sqlite3.Connection = field(init=False)

    def __post_init__(self) -> None:
        log = self.runtime.runtime_paths.event_log
        root = log.parent
        self.events = sqlite3.connect(log, check_same_thread=False)
        token = pair_device(root, "iphone")
        self.token = token
        calls = self.voice_calls

        async def open_voice(_device: str, _send: Any) -> Any:  # noqa: ANN401
            async def stop_playback(reason: str) -> str:
                calls.append(("stop_playback", reason))
                return "stopped"

            async def drop_unspoken(turn_ids: frozenset[str]) -> frozenset[str]:
                calls.append(("drop_unspoken", turn_ids))
                return frozenset() if self.audible else turn_ids

            async def nothing() -> None:
                return None

            return SimpleNamespace(
                player=SimpleNamespace(
                    connection_lost=lambda: None,
                    is_running=False,
                    observe_clock=lambda *_a: None,
                ),
                begin=nothing,
                close=nothing,
                stop_playback=stop_playback,
                drop_unspoken=drop_unspoken,
            )

        real = make_continuation_callable(self.runtime)

        def supersede_turn(earlier: str, turn_id: str) -> str:
            outcome = real(earlier, turn_id)
            self.taken_over.append((earlier, turn_id, outcome))
            return outcome

        hub = PhoneHub(
            events=BrainEvents(self.events),
            rows=inherent_loop._PhoneRows(self.events),  # noqa: SLF001
            open_voice=open_voice,
            supersede_turn=supersede_turn,
            pick_earlier=make_compose_callable(self.runtime),
        )
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                phone=hub,
                device_name=lambda one: device_name_for_token(root, one),
            ),
        )
        require_local_key(
            app,
            lambda header: local_key_matches(local_key(root), header),
            extra_hosts=[REMOTE],
            device_token_matches=lambda one: device_token_matches(root, one),
        )
        self.client = TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000))

    def connect(self) -> Any:  # noqa: ANN401 - a Starlette test websocket context
        return self.client.websocket_connect(
            f"ws://{REMOTE}:8006{PHONE_PATH}",
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def heard(self, turn_id: str) -> Any:  # noqa: ANN401 - Event
        """The ``utterance.received`` the take-over wrote for ``turn_id``, to drive its turn."""
        (event,) = [
            one
            for one in iter_events_of_types(self.events, ("utterance.received",))
            if one.payload["turn_id"] == turn_id
        ]
        return event


def _unspoken_history(runtime: Any) -> list[str]:  # noqa: ANN401 - JarvisRuntime
    """What the next turn would read as his side of the history, one string per message."""
    context = render_context(runtime.memory.db_path, exclude_id="")
    return [str(turn["content"]) for turn in context.history if turn["role"] == "user"]


@pytest.mark.parametrize("over_her", [True, False], ids=["over_her", "plain"])
def test_a_sentence_that_goes_on_while_the_first_is_thought_about_is_one_answer(
    tmp_path: Path,
    over_her: bool,  # noqa: FBT001 - pytest parameter
) -> None:
    """Live 2026-10-14 21:36: one long thought on a bus was answered eight times.

    The phone sends only the new segment and names the one before. The earlier turn is
    cancelled as superseded by the new one, its unspoken audio is dropped and what was audible
    stopped; one request is answered and carries the whole thought once; the history holds each
    segment as its own row and reads them as one message.
    """
    answer = "八点二十有一班。"
    with SpokenPeer([[("final_answer", answer)]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        flags = {"over_her": True} if over_her else {}
        with rig.connect() as ws, ThreadPoolExecutor(max_workers=2) as pool:
            open_ws(ws)
            first_said = say_words(ws, "ua", _FIRST, **flags)
            first = pool.submit(drive_streaming_turn, runtime, rig.heard(first_said["turn_id"]))
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            time.sleep(0.5)  # the peer reads its delay a moment after it logs the request
            peer.first_event_delay_s = 0.0
            second_said = say_words(ws, "ub", _SECOND, continues="ua", **flags)
            assert second_said["verdict"] == "turn"
            second = pool.submit(drive_streaming_turn, runtime, rig.heard(second_said["turn_id"]))
            second.result(timeout=20)
            with pytest.raises(ResponseCancelledError):
                first.result(timeout=10)

    assert rig.taken_over == [(first_said["turn_id"], second_said["turn_id"], "superseded")]
    assert rig.voice_calls[-2:] == [  # the first say's own barge-in, if it was over her, is before
        ("drop_unspoken", frozenset({first_said["turn_id"]})),
        ("stop_playback", "barge_in"),
    ]
    (cancelled,) = payloads_of(runtime.conn, "response.cancelled")
    assert (cancelled["turn_id"], cancelled["reason"]) == (first_said["turn_id"], "superseded")
    assert cancelled["interrupted_by_turn_id"] == second_said["turn_id"]
    # The event holds the thought and the segment; the rows hold the segments.
    heard = rig.heard(second_said["turn_id"]).payload
    assert (heard["transcript"], heard["segment"]) == (_THOUGHT, _SECOND)
    assert heard["continues"] == first_said["turn_id"]
    emitted = payloads_of(runtime.conn, "surface.response_emitted")
    assert [row["turn_id"] for row in emitted] == [second_said["turn_id"]]
    asked = json.dumps(peer.requests[-1][1]["input"], ensure_ascii=False)
    assert asked.count(_FIRST) == 1
    assert asked.count(_SECOND) == 1
    assert asked.index(_FIRST) < asked.index(_SECOND)
    assert _records(runtime) == [("allen", _FIRST), ("allen", _SECOND), ("jarvis", answer)]
    history = _unspoken_history(runtime)
    assert len(history) == 1
    assert history[0].count(_FIRST) == 1
    assert history[0].count(_SECOND) == 1
    runtime.conn.close()


def test_a_sentence_that_goes_on_after_the_first_has_started_to_speak_stops_it(
    tmp_path: Path,
) -> None:
    """A phone that names its earlier sentence has said what these words are.

    ADR 0074 leaves an audible answer to barge-in; here the answer under way is stopped and
    cancelled all the same.
    """
    answer = "八点二十有一班。"
    with SpokenPeer(
        [[("final_answer", "我先查一下。再看看。")], [("final_answer", answer)]],
        hold=True,
    ) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime, audible=True)
        with rig.connect() as ws, ThreadPoolExecutor(max_workers=2) as pool:
            open_ws(ws)
            first_said = say_words(ws, "ua", _FIRST, over_her=True)
            first = pool.submit(drive_streaming_turn, runtime, rig.heard(first_said["turn_id"]))
            wait_until_true(lambda: bool(payloads_of(runtime.conn, "surface.response_chunk")))
            second_said = say_words(ws, "ub", _SECOND, over_her=True, continues="ua")
            peer.release.set()
            second = pool.submit(drive_streaming_turn, runtime, rig.heard(second_said["turn_id"]))
            second.result(timeout=20)
            with pytest.raises(ResponseCancelledError):
                first.result(timeout=10)

    assert ("stop_playback", "barge_in") in rig.voice_calls
    (cancelled,) = payloads_of(runtime.conn, "response.cancelled")
    assert (cancelled["turn_id"], cancelled["reason"]) == (first_said["turn_id"], "superseded")
    assert cancelled["interrupted_by_turn_id"] == second_said["turn_id"]
    emitted = payloads_of(runtime.conn, "surface.response_emitted")
    assert [row["turn_id"] for row in emitted] == [second_said["turn_id"]]
    asked = json.dumps(peer.requests[-1][1]["input"], ensure_ascii=False)
    assert asked.count(_FIRST) == 1
    assert asked.count(_SECOND) == 1
    assert _records(runtime)[-3:] == [("allen", _FIRST), ("allen", _SECOND), ("jarvis", answer)]
    runtime.conn.close()


def test_three_steps_of_one_thought_are_one_answer_to_all_three(tmp_path: Path) -> None:
    """Each segment names the one before it; the third carries the thought, not three copies."""
    third = "往三十九路的地方走"
    answer = "好的。"
    with SpokenPeer([[("final_answer", answer)]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        with rig.connect() as ws, ThreadPoolExecutor(max_workers=3) as pool:
            open_ws(ws)
            said = [say_words(ws, "ua", _FIRST, over_her=True)]
            futures = [pool.submit(drive_streaming_turn, runtime, rig.heard(said[0]["turn_id"]))]
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            for utterance, words, before in (("ub", _SECOND, "ua"), ("uc", third, "ub")):
                time.sleep(0.5)  # the peer reads its delay a moment after it logs the request
                peer.first_event_delay_s = 0.0 if utterance == "uc" else 30.0
                said.append(say_words(ws, utterance, words, over_her=True, continues=before))
                futures.append(
                    pool.submit(drive_streaming_turn, runtime, rig.heard(said[-1]["turn_id"]))
                )
                wait_until_true(lambda: len(peer.requests) == len(futures), timeout_s=15.0)
            futures[2].result(timeout=60)
            for earlier in futures[:2]:
                with pytest.raises(ResponseCancelledError):
                    earlier.result(timeout=10)

    heard = rig.heard(said[2]["turn_id"]).payload
    assert heard["transcript"] == _FIRST + _SECOND + third
    assert heard["segment"] == third
    assert len(heard["folded"]) == 2
    cancelled = {
        row["turn_id"]: row["interrupted_by_turn_id"]
        for row in payloads_of(runtime.conn, "response.cancelled")
    }
    assert cancelled == {
        said[0]["turn_id"]: said[1]["turn_id"],
        said[1]["turn_id"]: said[2]["turn_id"],
    }
    asked = json.dumps(peer.requests[-1][1]["input"], ensure_ascii=False)
    assert [asked.count(words) for words in (_FIRST, _SECOND, third)] == [1, 1, 1]
    assert _records(runtime) == [
        ("allen", _FIRST),
        ("allen", _SECOND),
        ("allen", third),
        ("jarvis", answer),
    ]
    (message,) = _unspoken_history(runtime)
    assert [message.count(words) for words in (_FIRST, _SECOND, third)] == [1, 1, 1]
    runtime.conn.close()


def test_a_continues_that_names_another_turn_than_the_pending_one_is_dropped(
    tmp_path: Path,
) -> None:
    """The brain picks the turn by state; a name that disagrees is dropped (ADR 0278)."""
    scripts = [[("final_answer", "一。")], [("final_answer", "二。")]]
    with SpokenPeer(scripts, first_event_delay_s=1.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        with rig.connect() as ws, ThreadPoolExecutor(max_workers=2) as pool:
            open_ws(ws)
            first_said = say_words(ws, "ua", _FIRST, over_her=True)
            first = pool.submit(drive_streaming_turn, runtime, rig.heard(first_said["turn_id"]))
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            time.sleep(0.5)  # the peer reads its delay a moment after it logs the request
            peer.first_event_delay_s = 0.0
            second_said = say_words(ws, "ub", _SECOND, over_her=True, continues="uz")
            second = pool.submit(drive_streaming_turn, runtime, rig.heard(second_said["turn_id"]))
            first.result(timeout=20)
            second.result(timeout=20)

    assert rig.taken_over == []
    heard = rig.heard(second_said["turn_id"]).payload
    assert heard["transcript"] == _SECOND
    assert not {"segment", "continues", "folded"} & set(heard)
    assert not payloads_of(runtime.conn, "response.cancelled")
    assert len(payloads_of(runtime.conn, "surface.response_emitted")) == 2
    runtime.conn.close()


def test_a_spoken_say_with_no_continues_goes_on_from_the_pending_turn_all_the_same(
    tmp_path: Path,
) -> None:
    """ADR 0278: no time window and no name from the phone; the earlier answer is not over."""
    answer = "八点二十有一班。"
    with SpokenPeer([[("final_answer", answer)]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        with rig.connect() as ws, ThreadPoolExecutor(max_workers=2) as pool:
            open_ws(ws)
            first_said = say_words(ws, "ua", _FIRST)
            first = pool.submit(drive_streaming_turn, runtime, rig.heard(first_said["turn_id"]))
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            time.sleep(0.5)  # the peer reads its delay a moment after it logs the request
            peer.first_event_delay_s = 0.0
            second_said = say_words(ws, "ub", _SECOND)
            assert second_said["verdict"] == "turn"
            second = pool.submit(drive_streaming_turn, runtime, rig.heard(second_said["turn_id"]))
            second.result(timeout=20)
            with pytest.raises(ResponseCancelledError):
                first.result(timeout=10)

    assert rig.taken_over == [(first_said["turn_id"], second_said["turn_id"], "superseded")]
    heard = rig.heard(second_said["turn_id"]).payload
    assert (heard["transcript"], heard["segment"]) == (_THOUGHT, _SECOND)
    assert _records(runtime) == [("allen", _FIRST), ("allen", _SECOND), ("jarvis", answer)]
    runtime.conn.close()


@pytest.mark.parametrize(
    "words",
    ["停", _FIRST],
    ids=["a_stop_request", "the_same_words_again"],
)
def test_a_phone_stop_request_or_repeat_is_not_composed(tmp_path: Path, words: str) -> None:
    """A stop is never part of a request, and a repeat is the ADR 0268 rule's, not a join."""
    with SpokenPeer([[("final_answer", "八点二十有一班。")]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        with rig.connect() as ws, ThreadPoolExecutor(max_workers=1) as pool:
            open_ws(ws)
            first_said = say_words(ws, "ua", _FIRST)
            pool.submit(drive_streaming_turn, runtime, rig.heard(first_said["turn_id"]))
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            second_said = say_words(ws, "ub", words)
            peer.release_delayed()  # nothing else asks it; let the pending turn end
    assert rig.taken_over == []
    heard = rig.heard(second_said["turn_id"]).payload
    assert heard["transcript"] == words
    assert not {"segment", "continues", "folded"} & set(heard)
    runtime.conn.close()


def test_words_that_go_on_from_an_answer_already_given_stand_alone(tmp_path: Path) -> None:
    """The earlier turn finished: its answer is a record that stays, so the words are a new turn."""
    with SpokenPeer([[("final_answer", "一。")], [("final_answer", "二。")]]) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        with rig.connect() as ws:
            open_ws(ws)
            first_said = say_words(ws, "ua", _FIRST)
            drive_streaming_turn(runtime, rig.heard(first_said["turn_id"]))
            second_said = say_words(ws, "ub", _SECOND, over_her=True, continues="ua")
            drive_streaming_turn(runtime, rig.heard(second_said["turn_id"]))

    assert rig.taken_over == []  # the brain saw the earlier answer was over and took nothing
    heard = rig.heard(second_said["turn_id"]).payload
    assert heard["transcript"] == _SECOND
    assert "continues" not in heard
    assert not payloads_of(runtime.conn, "response.cancelled")
    assert [source for source, _text in _records(runtime)] == ["allen", "jarvis", "allen", "jarvis"]
    runtime.conn.close()


def test_a_continues_that_is_not_an_utterance_id_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """A malformed field is ``bad_say``, as the other flags are; an unknown id is no earlier one."""
    with SpokenPeer([]) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        with rig.connect() as ws:
            open_ws(ws)
            assert say_words(ws, "ub", _SECOND, continues=7)["code"] == "bad_say"
            assert say_words(ws, "ub", _SECOND, continues="has space")["code"] == "bad_say"
            unknown = say_words(ws, "ub", _SECOND, continues="ua")
            assert unknown["verdict"] == "turn"
    assert rig.taken_over == []
    heard = rig.heard(unknown["turn_id"]).payload
    assert heard["transcript"] == _SECOND
    assert "continues" not in heard
    runtime.conn.close()


# --- any spoken sentence goes on from the pending one, by state (ADR 0278) ---

_MAC = "macbook"


@dataclass
class _MacRig:
    """A voice terminal's two asks on the real hooks the runtime binds for it.

    ``hear`` does what the capture session does for an accepted sentence: the ``supersede`` ask
    (the sweep), then the ``utterance`` ask. The terminal link is not involved: dropping what is
    unspoken and stopping what is audible are recorded.
    """

    runtime: Any
    taken_over: list[tuple[str, str, str]] = field(default_factory=list)
    dropped: list[frozenset[str]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    listening: Any = field(init=False)

    def __post_init__(self) -> None:
        self.listening = inherent_loop._build_brain_listening(  # noqa: SLF001
            self.runtime,
            TerminalHub(events=BrainEvents(self.runtime.conn)),
            controls=VoiceControls(),
            set_conversation=lambda _on, _why: None,
            set_quiet=lambda _level: None,
        )
        assert self.listening is not None
        self.listening.drop_unspoken = self._drop
        self.listening._stop_on = self._stop  # noqa: SLF001
        real = self.listening.hooks.take_over

        def take_over(earlier: str, turn_id: str) -> str:
            outcome = str(real(earlier, turn_id))
            self.taken_over.append((earlier, turn_id, outcome))
            return outcome

        self.listening.hooks.take_over = take_over

    def _drop(self, turn_ids: frozenset[str]) -> frozenset[str]:
        self.dropped.append(turn_ids)
        return turn_ids

    def _stop(self, _link: Any, reason: str) -> str:  # noqa: ANN401
        self.stopped.append(reason)
        return "applied"

    def hear(self, turn_id: str, words: str) -> Any:  # noqa: ANN401 - Event
        """The ``utterance.received`` the brain wrote for a sentence of the terminal's."""
        self.listening.hooks.supersede(_MAC, turn_id)
        args = {
            "transcript": words,
            "turn_id": turn_id,
            "utterance_id": f"U-{turn_id}",
            "channel": "inherent_wake",
        }
        link = SimpleNamespace(name=_MAC, speech=None)
        reply = asyncio.run(self.listening._utterance(link, args))  # noqa: SLF001
        (event,) = [
            one
            for one in iter_events_of_types(self.runtime.conn, ("utterance.received",))
            if one.event_uid == reply["event_uid"]
        ]
        return event


def _asked(peer: Any) -> str:  # noqa: ANN401 - _Peer
    return str(json.dumps(peer.requests[-1][1]["input"], ensure_ascii=False))


def test_a_second_mac_utterance_while_the_first_is_thought_about_is_one_composed_turn(
    tmp_path: Path,
) -> None:
    """Live 2026-10-15 15:22: two sentences ~1 s apart were two turns that contradicted.

    The second utterance arrives while the first has no word out: the first is cancelled as
    superseded naming the second, one request is answered and carries the whole thought once,
    and memory.db holds each segment as it was spoken.
    """
    answer = "八点二十有一班。"
    with SpokenPeer([[("final_answer", answer)]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _MacRig(runtime)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_heard = rig.hear("T-1", _FIRST)
            first = pool.submit(drive_streaming_turn, runtime, first_heard)
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            time.sleep(0.5)  # the peer reads its delay a moment after it logs the request
            peer.first_event_delay_s = 0.0
            second_heard = rig.hear("T-2", _SECOND)
            second = pool.submit(drive_streaming_turn, runtime, second_heard)
            second.result(timeout=20)
            with pytest.raises(ResponseCancelledError):
                first.result(timeout=10)

    assert rig.taken_over == [("T-1", "T-2", "superseded")]
    assert rig.dropped == [frozenset({"T-1"})]
    assert rig.stopped == ["barge_in"]
    payload = second_heard.payload
    assert (payload["transcript"], payload["segment"], payload["continues"]) == (
        _THOUGHT,
        _SECOND,
        "T-1",
    )
    assert payload["folded"] == [first_heard.event_uid]
    (cancelled,) = payloads_of(runtime.conn, "response.cancelled")
    assert (cancelled["turn_id"], cancelled["reason"]) == ("T-1", "superseded")
    assert cancelled["interrupted_by_turn_id"] == "T-2"
    assert [row["turn_id"] for row in payloads_of(runtime.conn, "surface.response_emitted")] == [
        "T-2",
    ]
    asked = _asked(peer)
    assert (asked.count(_FIRST), asked.count(_SECOND)) == (1, 1)
    assert asked.index(_FIRST) < asked.index(_SECOND)
    assert _records(runtime) == [("allen", _FIRST), ("allen", _SECOND), ("jarvis", answer)]
    (message,) = _unspoken_history(runtime)
    assert (message.count(_FIRST), message.count(_SECOND)) == (1, 1)
    runtime.conn.close()


def test_a_second_mac_utterance_takes_over_a_turn_that_called_a_tool_and_carries_its_results(
    tmp_path: Path,
) -> None:
    """With slow_results on the sweep spared a turn working on a tool; now it is taken over.

    The first turn searched (the call and its result are on the log). The composed request
    cannot carry tool blocks, so a note lists the call and its observed output.
    """
    answer = "八点二十有一班。"
    found = '{"departures": ["08:20 39路", "08:35 39路"]}'
    with SpokenPeer([[("final_answer", answer)]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        runtime = replace(
            runtime,
            response_flags=replace(runtime.response_flags, slow_results=True),
        )
        rig = _MacRig(runtime)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_heard = rig.hear("T-1", _FIRST)
            first = pool.submit(drive_streaming_turn, runtime, first_heard)
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            for kind, payload in (
                (
                    "action.proposed",
                    {
                        "action_id": "A-1",
                        "tool_name": "transit_times",
                        "caller_principal": "llm",
                        "risk_level": "low",
                        "arguments": {"to": "学校"},
                    },
                ),
                ("action.dispatched", {"action_id": "A-1"}),
                (
                    "action.result_observed",
                    {
                        "action_id": "A-1",
                        "semantics": "observation",
                        "tool_output": found,
                    },
                ),
            ):
                emit_event(
                    runtime.conn,
                    type=kind,
                    payload=payload,
                    correlation={"turn_id": "T-1", "action_id": "A-1"},
                )
            time.sleep(0.5)  # the peer reads its delay a moment after it logs the request
            peer.first_event_delay_s = 0.0
            second_heard = rig.hear("T-2", _SECOND)
            second = pool.submit(drive_streaming_turn, runtime, second_heard)
            second.result(timeout=20)
            with pytest.raises(ResponseCancelledError):
                first.result(timeout=10)

    assert rig.taken_over == [("T-1", "T-2", "superseded")]
    (cancelled,) = payloads_of(runtime.conn, "response.cancelled")
    assert (cancelled["turn_id"], cancelled["interrupted_by_turn_id"]) == ("T-1", "T-2")
    asked = _asked(peer)
    assert "Already done for this thought" in asked
    assert "transit_times" in asked
    assert asked.count("08:20 39路") == 1
    assert [row["turn_id"] for row in payloads_of(runtime.conn, "surface.response_emitted")] == [
        "T-2",
    ]
    runtime.conn.close()


def test_a_second_mac_utterance_after_the_first_was_answered_is_its_own_turn(
    tmp_path: Path,
) -> None:
    """The first answer completed: a record that stays, so nothing is taken over."""
    with SpokenPeer([[("final_answer", "一。")], [("final_answer", "二。")]]) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _MacRig(runtime)
        drive_streaming_turn(runtime, rig.hear("T-1", _FIRST))
        second_heard = rig.hear("T-2", _SECOND)
        drive_streaming_turn(runtime, second_heard)

    assert rig.taken_over == []
    assert second_heard.payload["transcript"] == _SECOND
    assert not {"segment", "continues", "folded"} & set(second_heard.payload)
    assert not payloads_of(runtime.conn, "response.cancelled")
    assert len(payloads_of(runtime.conn, "surface.response_emitted")) == 2
    runtime.conn.close()


def test_a_mac_stop_request_is_never_composed(tmp_path: Path) -> None:
    """Words that only ask her to stop stand alone; the pending answer is not taken over."""
    with SpokenPeer([[("final_answer", "一。")]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _MacRig(runtime)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(drive_streaming_turn, runtime, rig.hear("T-1", _FIRST))
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            stop_heard = rig.hear("T-2", "停")
            peer.release_delayed()  # nothing else asks it; let the pending turn end
    assert rig.taken_over == []
    assert stop_heard.payload["transcript"] == "停"
    assert not {"segment", "continues", "folded"} & set(stop_heard.payload)
    runtime.conn.close()


def test_the_same_mac_words_again_are_left_to_the_repeat_rule_not_composed(
    tmp_path: Path,
) -> None:
    """ADR 0268 stays: his row is kept, the repeat starts no answer and names the first turn."""
    with SpokenPeer([[("final_answer", "八点二十有一班。")]], first_event_delay_s=1.5) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _MacRig(runtime)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(drive_streaming_turn, runtime, rig.hear("T-1", _FIRST))
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            time.sleep(0.5)
            peer.first_event_delay_s = 0.0
            repeat_heard = rig.hear("T-2", _FIRST)
            second = pool.submit(drive_streaming_turn, runtime, repeat_heard)
            first.result(timeout=20)
            with pytest.raises(ResponseCancelledError):
                second.result(timeout=10)

    assert rig.taken_over == []
    assert repeat_heard.payload["transcript"] == _FIRST
    assert not {"segment", "continues", "folded"} & set(repeat_heard.payload)
    (cancelled,) = payloads_of(runtime.conn, "response.cancelled")
    assert (cancelled["turn_id"], cancelled["reason"]) == ("T-2", "superseded")
    assert cancelled["interrupted_by_turn_id"] == "T-1"
    admitted = {
        row["response_id"] for row in payloads_of(runtime.conn, "response.request_admitted")
    }
    assert len(admitted) == 1
    assert [row["turn_id"] for row in payloads_of(runtime.conn, "surface.response_emitted")] == [
        "T-1",
    ]
    runtime.conn.close()


def test_an_earlier_mac_utterance_older_than_the_bound_stands_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is a safety net for a turn that never ended: past it nothing is composed."""
    monkeypatch.setattr(device_voice, "COMPOSE_BOUND_S", 0.3)
    with SpokenPeer([[("final_answer", "一。")], [("final_answer", "二。")]], hold=False) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _MacRig(runtime)
        # A turn the log never saw end: only its sentence is there, and it is old enough.
        rig.hear("T-1", _FIRST)
        time.sleep(0.6)
        second_heard = rig.hear("T-2", _SECOND)
        drive_streaming_turn(runtime, second_heard)

    assert rig.taken_over == []
    assert second_heard.payload["transcript"] == _SECOND
    assert not {"segment", "continues", "folded"} & set(second_heard.payload)
    runtime.conn.close()


def test_a_third_mac_utterance_composes_into_the_second_and_the_thought_is_in_once(
    tmp_path: Path,
) -> None:
    """A chain names only the last turn; the third carries the thought, not three copies."""
    third_words = "往三十九路的地方走"
    with SpokenPeer([[("final_answer", "好的。")]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _MacRig(runtime)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(drive_streaming_turn, runtime, rig.hear("T-1", _FIRST))]
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            time.sleep(0.5)
            futures.append(pool.submit(drive_streaming_turn, runtime, rig.hear("T-2", _SECOND)))
            wait_until_true(lambda: len(peer.requests) == 2, timeout_s=15.0)
            time.sleep(0.5)
            peer.first_event_delay_s = 0.0
            third_heard = rig.hear("T-3", third_words)
            futures.append(pool.submit(drive_streaming_turn, runtime, third_heard))
            futures[2].result(timeout=60)
            for earlier in futures[:2]:
                with pytest.raises(ResponseCancelledError):
                    earlier.result(timeout=10)

    assert [(e, t) for e, t, _o in rig.taken_over] == [("T-1", "T-2"), ("T-2", "T-3")]
    assert third_heard.payload["transcript"] == _FIRST + _SECOND + third_words
    assert len(third_heard.payload["folded"]) == 2
    asked = _asked(peer)
    assert [asked.count(words) for words in (_FIRST, _SECOND, third_words)] == [1, 1, 1]
    runtime.conn.close()


def test_a_sentence_after_a_repeat_goes_on_from_the_turn_the_repeat_repeated(
    tmp_path: Path,
) -> None:
    """The repeat is cancelled (ADR 0268); it must not hide the turn that still answers."""
    third_words = "往三十九路的地方走"
    with SpokenPeer([[("final_answer", "好的。")]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _MacRig(runtime)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(drive_streaming_turn, runtime, rig.hear("T-1", _FIRST))
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            time.sleep(0.5)
            repeat = pool.submit(drive_streaming_turn, runtime, rig.hear("T-2", _FIRST))
            with pytest.raises(ResponseCancelledError):
                repeat.result(timeout=10)
            peer.first_event_delay_s = 0.0
            third_heard = rig.hear("T-3", third_words)
            third = pool.submit(drive_streaming_turn, runtime, third_heard)
            third.result(timeout=60)
            with pytest.raises(ResponseCancelledError):
                first.result(timeout=10)

    assert rig.taken_over == [("T-1", "T-3", "superseded")]
    assert third_heard.payload["transcript"] == _FIRST + third_words
    assert third_heard.payload["continues"] == "T-1"
    runtime.conn.close()


def test_a_typed_say_never_goes_on_from_a_pending_spoken_turn(tmp_path: Path) -> None:
    """Typed words are never composed; a typed Mac submit does not reach the composing seam."""
    with SpokenPeer([[("final_answer", "一。")]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        with rig.connect() as ws, ThreadPoolExecutor(max_workers=1) as pool:
            open_ws(ws)
            first_said = say_words(ws, "ua", _FIRST)
            pool.submit(drive_streaming_turn, runtime, rig.heard(first_said["turn_id"]))
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            typed = say_words(ws, "ub", _SECOND, spoken=False, continues="ua")
            peer.release_delayed()  # nothing else asks it; let the pending turn end
    assert typed["verdict"] == "turn"
    assert rig.taken_over == []
    assert not payloads_of(runtime.conn, "response.cancelled")
    runtime.conn.close()


# --- a spoken answer waits for the end of his thought (ADR 0272) -------------


def _events_of(conn: Any, event_type: str) -> list[Any]:  # noqa: ANN401 - sqlite3.Connection
    return list(iter_events_of_types(conn, (event_type,)))


def _chunk_turns(runtime: Any) -> list[str]:  # noqa: ANN401 - JarvisRuntime
    """The turn of every answer sentence that has left her, in order."""
    chunks = _events_of(runtime.conn, "surface.response_chunk")
    return [str(one.correlation["turn_id"]) for one in chunks]


def _sleep_until_after(event: Any, seconds: float) -> None:  # noqa: ANN401 - Event
    """Sleep until ``seconds`` past the event, as the clock he spoke on counts them."""
    time.sleep(max(0.0, event.ts_epoch_ms / 1000 + seconds - time.time()))


_HELD_S = 1.5
_SLACK_S = 0.1  # the log stamps a row a moment after the code that wrote it ran


def test_a_long_spoken_turn_sends_its_request_at_once_and_holds_the_answer(
    tmp_path: Path,
) -> None:
    """The model hears him at once; the first sentence leaves 1.5 s after his speech ended."""
    with SpokenPeer([[("final_answer", "八点二十有一班。")]]) as peer:
        runtime = _merge_rig(tmp_path, peer, hold_s=None)
        heard = _spoken_turn(runtime.conn, "T-1", _FIRST)
        with ThreadPoolExecutor(max_workers=1) as pool:
            turn = pool.submit(drive_streaming_turn, runtime, heard)
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            asked_after = time.time() - heard.ts_epoch_ms / 1000
            assert not _chunk_turns(runtime)
            turn.result(timeout=20)
    (chunk,) = _events_of(runtime.conn, "surface.response_chunk")
    assert asked_after < _HELD_S - 0.5
    assert chunk.ts_epoch_ms / 1000 - heard.ts_epoch_ms / 1000 >= _HELD_S - _SLACK_S
    runtime.conn.close()


@pytest.mark.parametrize(
    ("channel", "words"),
    [("inherent_wake", "几点了"), ("cli_stdin", _FIRST + _SECOND)],
    ids=["a_short_line_that_reads_finished", "typed_text"],
)
def test_a_short_finished_line_and_typed_text_are_answered_with_no_hold(
    tmp_path: Path,
    channel: str,
    words: str,
) -> None:
    """Only a spoken turn that may be cut in two waits; these are not."""
    with SpokenPeer([[("final_answer", "八点二十有一班。")]]) as peer:
        runtime = _merge_rig(tmp_path, peer, hold_s=None)
        heard = emit_event(
            runtime.conn,
            type="surface.user_intent",
            payload={"transcript": words, "turn_id": "T-1", "channel": channel},
            correlation={"turn_id": "T-1"},
        )
        drive_streaming_turn(runtime, heard)
    (chunk,) = _events_of(runtime.conn, "surface.response_chunk")
    assert chunk.ts_epoch_ms / 1000 - heard.ts_epoch_ms / 1000 < _HELD_S - 0.5
    runtime.conn.close()


def test_a_phone_sentence_that_goes_on_inside_the_hold_leaves_nothing_of_the_first_answer(
    tmp_path: Path,
) -> None:
    """B names A and arrives 1.0 s after A: the model answered A at once, none of it was said."""
    answer = "八点二十有一班。"
    with SpokenPeer([[("final_answer", "先说前半句。")], [("final_answer", answer)]]) as peer:
        runtime = _merge_rig(tmp_path, peer, hold_s=None)
        rig = _PhoneRig(runtime)
        with rig.connect() as ws, ThreadPoolExecutor(max_workers=2) as pool:
            open_ws(ws)
            first_said = say_words(ws, "ua", _FIRST, over_her=True)
            first_heard = rig.heard(first_said["turn_id"])
            first = pool.submit(drive_streaming_turn, runtime, first_heard)
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            _sleep_until_after(first_heard, 1.0)
            assert not _chunk_turns(runtime)
            second_said = say_words(ws, "ub", _SECOND, continues="ua", over_her=True)
            assert second_said["verdict"] == "turn"
            second = pool.submit(drive_streaming_turn, runtime, rig.heard(second_said["turn_id"]))
            second.result(timeout=20)
            with pytest.raises(ResponseCancelledError):
                first.result(timeout=10)

    assert rig.taken_over == [(first_said["turn_id"], second_said["turn_id"], "superseded")]
    assert set(_chunk_turns(runtime)) == {second_said["turn_id"]}
    assert [row["turn_id"] for row in payloads_of(runtime.conn, "surface.response_emitted")] == [
        second_said["turn_id"],
    ]
    assert len(peer.requests) == 2  # A's went out at once, B's carries the whole thought
    asked = json.dumps(peer.requests[-1][1]["input"], ensure_ascii=False)
    assert (asked.count(_FIRST), asked.count(_SECOND)) == (1, 1)
    runtime.conn.close()


def test_a_second_mac_utterance_inside_the_hold_is_answered_with_the_first_and_nothing_of_it(
    tmp_path: Path,
) -> None:
    """The sweep finds the first answer with no word out, because none was allowed out yet."""
    answer = "八点二十有一班。"
    with SpokenPeer([[("final_answer", "先说前半句。")], [("final_answer", answer)]]) as peer:
        runtime = _merge_rig(tmp_path, peer, hold_s=None)
        first_heard = _spoken_turn(runtime.conn, "T-1", _FIRST)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(drive_streaming_turn, runtime, first_heard)
            wait_until_true(lambda: len(peer.requests) == 1, timeout_s=15.0)
            _sleep_until_after(first_heard, 1.0)
            assert not _chunk_turns(runtime)
            make_supersede_unspoken_callable(runtime, lambda turn_ids: turn_ids)("T-2")
            second = pool.submit(
                drive_streaming_turn, runtime, _spoken_turn(runtime.conn, "T-2", _SECOND)
            )
            second.result(timeout=20)
            with pytest.raises(ResponseCancelledError):
                first.result(timeout=10)

    (cancelled,) = payloads_of(runtime.conn, "response.cancelled")
    assert (cancelled["turn_id"], cancelled["reason"]) == ("T-1", "superseded")
    assert set(_chunk_turns(runtime)) == {"T-2"}
    asked = json.dumps(peer.requests[-1][1]["input"], ensure_ascii=False)
    assert (asked.count(_FIRST), asked.count(_SECOND)) == (1, 1)
    assert _records(runtime) == [("allen", _FIRST), ("allen", _SECOND), ("jarvis", answer)]
    runtime.conn.close()


def test_a_write_waits_for_the_hold_and_a_read_does_not(tmp_path: Path) -> None:
    """The reading is done when the hold ends; the card taken down is not done before it."""
    script = [[("call", "kept"), ("call", "dismiss_card")], [("final_answer", "好的。")]]
    with SpokenPeer(script) as peer:
        runtime = _merge_rig(tmp_path, peer, hold_s=None)
        heard = _spoken_turn(runtime.conn, "T-1", "看一下备忘录然后把卡片收起来")
        drive_streaming_turn(runtime, heard)
    named = {
        one.payload["action_id"]: one.payload["tool_name"]
        for one in _events_of(runtime.conn, "action.proposed")
    }
    dispatched = {
        named[one.payload["action_id"]]: one.ts_epoch_ms
        for one in _events_of(runtime.conn, "action.dispatched")
    }
    assert set(dispatched) >= {"kept", "dismiss_card"}
    after = {name: ms / 1000 - heard.ts_epoch_ms / 1000 for name, ms in dispatched.items()}
    assert after["kept"] < _HELD_S - 0.5
    assert after["dismiss_card"] >= _HELD_S - _SLACK_S
    runtime.conn.close()


def test_the_hold_counts_from_the_end_of_his_speech_when_the_device_says_how_long_ago(
    tmp_path: Path,
) -> None:
    """A Mac that heard him stop 1.0 s before its utterance reached the brain: 0.5 s is left."""
    with SpokenPeer([[("final_answer", "八点二十有一班。")]]) as peer:
        runtime = _merge_rig(tmp_path, peer, hold_s=None)
        uid = BrainEvents(runtime.conn).record_utterance(
            "macbook",
            {
                "transcript": _FIRST,
                "turn_id": "T-1",
                "utterance_id": "u1",
                "channel": "inherent_wake",
                "spoke_ago_ms": 1000,
            },
        )
        (heard,) = [
            one for one in _events_of(runtime.conn, "utterance.received") if one.event_uid == uid
        ]
        assert heard.payload["spoke_ago_ms"] == 1000
        drive_streaming_turn(runtime, heard)
    (chunk,) = _events_of(runtime.conn, "surface.response_chunk")
    waited = chunk.ts_epoch_ms / 1000 - heard.ts_epoch_ms / 1000
    assert _HELD_S - 1.0 - _SLACK_S <= waited < _HELD_S - 0.3
    runtime.conn.close()
