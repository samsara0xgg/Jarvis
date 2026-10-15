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

import pytest
from fastapi.testclient import TestClient

from jarvis.decision.response_run import ResponseCancelledError
from jarvis.runtime import (
    inherent_loop,
    make_continuation_callable,
    make_relation_supersede_callable,
    make_supersede_unspoken_callable,
)
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log
from jarvis.state.memory_db import MemorySettings, render_context
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.state.projections import PendingConfirmations
from jarvis.surface import voice_media, voice_pipeline
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.phone_link import PHONE_PATH, PhoneHub
from jarvis.surface.terminal_events import BrainEvents
from tests.integration.test_conversation_mode import _Session
from tests.integration.test_foreground_arbitration import _whole
from tests.integration.test_phone_here import REMOTE
from tests.integration.test_phone_judge import _open, _say
from tests.integration.test_spoken_streaming import (
    _fixture_key,  # noqa: F401 - autouse fixture of the spoken-route peer
    _Peer,
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
from tests.integration.test_wave3_single_audio_ingress import _RecordingPipeline, _wait_until
from tests.integration.test_wave4a_response_run import (
    _drive_turn_on_own_connection,
    _emit_intent,
    _event_count,
    _final_result,
    _make_runtime,
    _payloads,
    _script_decide,
)
from tests.integration.test_wire_routine_streaming import _drive

if TYPE_CHECKING:
    from pathlib import Path


# --- capture side ------------------------------------------------------------


class _AcceptingPipeline(_RecordingPipeline):
    """ASR that accepts every utterance, or finds nothing in it."""

    def __init__(
        self, record: list[str], *, empty: bool = False, said: str = "明天上午十点提醒我",
    ) -> None:
        super().__init__()
        self._record = record
        self._empty = empty
        self._said = said

    def run_turn(self, **kwargs: Any) -> Any:  # noqa: ANN401
        if self._empty:
            msg = "silence"
            raise voice_pipeline.VoicePipelineEmptyError(msg)
        kwargs["before_emit"](self._said)
        self._record.append("utterance.received")
        return super().run_turn(**kwargs)


def _capture_rig(
    monkeypatch: pytest.MonkeyPatch, *, empty: bool = False, said: str = "明天上午十点提醒我",
) -> tuple[_Session, list[str]]:
    record: list[str] = []
    rig = _Session(
        monkeypatch,
        pipeline=_AcceptingPipeline(record, empty=empty, said=said),
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
        _wait_until(lambda: "release" in record)
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
        _wait_until(lambda: "release" in record)
        assert record == ["hold", "release"]
    finally:
        rig.close()


def test_a_dismissal_drops_the_answer_still_on_its_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 2026-10-02: an answer to the sentence before 「退下吧」 played after her goodbye."""
    rig, record = _capture_rig(monkeypatch, said="退下吧")
    try:
        rig.speak()
        _wait_until(lambda: "release" in record)
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
    _wait_until(lambda: len(runtime.response_runs.open_runs()) == count)


def test_no_answer_completes_while_allen_talks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run waits at its completion while held and completes once released."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    _script_decide(monkeypatch, _final_result())
    assert runtime.response_runs is not None
    runtime.response_runs.hold_completion(held=True)
    intent = _emit_intent(runtime.conn, "T-first")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _drive_turn_on_own_connection,
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
        _wait_open(runtime, 1)
        time.sleep(0.3)
        assert _event_count(runtime.conn, "response.completed") == 0
        assert _event_count(runtime.conn, "surface.response_emitted") == 0
        runtime.response_runs.hold_completion(held=False)
        future.result(timeout=10)
    assert _event_count(runtime.conn, "response.completed") == 1
    assert _event_count(runtime.conn, "surface.response_emitted") == 1
    runtime.conn.close()


def test_the_next_sentence_cancels_only_an_unspoken_answer_to_a_recent_sentence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Of four held answers only the unspoken voice and card ones are dropped.

    ``T-voice`` answers a voice sentence and never reached the speaker: dropped.
    ``T-card`` answers a filled-in ask card (ADR 0072) the same way: dropped.
    ``T-heard`` answers a voice sentence, but L5 already played some of it: kept.
    ``T-typed`` answers typed text: kept. The kept two complete on release.
    """
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    _script_decide(monkeypatch, _final_result())
    assert runtime.response_runs is not None
    runtime.response_runs.hold_completion(held=True)
    asked: list[frozenset[str]] = []

    def _drop(turn_ids: frozenset[str]) -> frozenset[str]:
        asked.append(turn_ids)
        return turn_ids - {"T-heard"}

    supersede = make_supersede_unspoken_callable(runtime, _drop)
    intents = {turn: _emit_intent(runtime.conn, turn) for turn in ("T-voice", "T-heard", "T-typed")}
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
                _drive_turn_on_own_connection,
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
    cancelled = _payloads(runtime.conn, "response.cancelled")
    assert sorted((row["turn_id"], row["reason"]) for row in cancelled) == [
        ("T-card", "superseded"), ("T-voice", "superseded"),
    ]
    emitted = {row["turn_id"] for row in _payloads(runtime.conn, "surface.response_emitted")}
    assert emitted == {"T-heard", "T-typed"}
    runtime.conn.close()


def test_a_dropped_turn_takes_its_waiting_card_with_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0074: the card the first half put up is rejected as ``superseded``, so none waits."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    _script_decide(monkeypatch, _final_result())
    assert runtime.response_runs is not None
    runtime.response_runs.hold_completion(held=True)
    intent = _emit_intent(runtime.conn, "T-first")
    _heard(runtime.conn, "T-first")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _drive_turn_on_own_connection,
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

    rejected = _payloads(runtime.conn, "confirmation.rejected")
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
    monkeypatch: pytest.MonkeyPatch,
    slow: bool,  # noqa: FBT001 - pytest parameter
    dispatched: bool,  # noqa: FBT001 - pytest parameter
    superseded: bool,  # noqa: FBT001 - pytest parameter
) -> None:
    """With slow_results on, a turn that dispatched a tool is kept.

    Live test 2026-10-01: a sentence 7 s after a search began must not cancel
    it. Without a dispatched action, or with the flag off, ADR 0053/0074 still
    drops the unspoken answer.
    """
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    runtime = replace(runtime, response_flags=replace(runtime.response_flags, slow_results=slow))
    _script_decide(monkeypatch, _final_result())
    assert runtime.response_runs is not None
    runtime.response_runs.hold_completion(held=True)
    intent = _emit_intent(runtime.conn, "T-first")
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
            _drive_turn_on_own_connection,
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
    assert len(_payloads(runtime.conn, "response.cancelled")) == int(superseded)
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
    monkeypatch: pytest.MonkeyPatch,
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
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    _script_decide(monkeypatch, _final_result())
    assert runtime.response_runs is not None
    runtime.response_runs.hold_completion(held=True)
    intent = _emit_intent(runtime.conn, "T-first")
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
            _drive_turn_on_own_connection,
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
    assert len(_payloads(runtime.conn, "response.cancelled")) == int(cancelled)
    if cancelled:
        (row,) = _payloads(runtime.conn, "response.cancelled")
        assert (row["turn_id"], row["reason"]) == ("T-first", "superseded")
        assert row["interrupted_by_turn_id"] == "T-next"
    runtime.conn.close()


def test_a_relation_to_a_turn_with_no_open_answer_does_nothing(tmp_path: Path) -> None:
    """The earlier answer already left (or was dropped by the 10 s sweep): no open run."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    relate = make_relation_supersede_callable(runtime, lambda ids: ids, lambda: None)
    assert relate("T-gone", "T-next", "supplement") == "no_open_run"
    runtime.conn.close()


# --- media lane --------------------------------------------------------------


def _media(
    tmp_path: Path, *response_ids: str,
) -> tuple[voice_media.StreamingTTSPipeline, Any, Any]:
    db_path = tmp_path / "hold.db"
    provider = _FakeProvider(
        {(rid, 0): _Behavior("success", final_delay_s=0.01) for rid in response_ids},
        candidate_count=1,
    )
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    return pipeline, provider, player


def _played(db_path: Path) -> list[str]:
    return [
        str(payload["response_id"])
        for kind, payload in _terminal_rows(open_event_log(db_path))
        if kind == "surface.playback_completed"
    ]


def _answer(conn: Any, response_id: str, turn_id: str) -> Any:  # noqa: ANN401
    return _whole(
        conn, response_id=response_id, group_id=f"G{turn_id}", turn_id=turn_id, text="好的",
    )


def test_an_answer_waits_while_allen_talks_and_a_dropped_one_never_plays(tmp_path: Path) -> None:
    """Both answers park while held; the dropped one never plays, the other plays on release."""
    pipeline, provider, player = _media(tmp_path, "R-drop", "R-keep")
    conn = open_event_log(tmp_path / "hold.db")
    try:
        with _CallbackPump(player):
            pipeline.hold_output(held=True)
            asyncio.run(_submit_response(pipeline, _answer(conn, "R-drop", "T-drop")))
            asyncio.run(_submit_response(pipeline, _answer(conn, "R-keep", "T-keep")))
            time.sleep(0.3)
            assert provider.opened == []
            assert not pipeline.is_output_active()
            assert pipeline.drop_unspoken(frozenset({"T-drop"})) == frozenset({"T-drop"})
            pipeline.hold_output(held=False)
            _wait_until(lambda: _played(tmp_path / "hold.db") == ["R-keep"])
            time.sleep(0.2)
    finally:
        assert pipeline.close()
        conn.close()
    assert [rid for rid, _ in provider.opened] == ["R-keep"]
    assert _played(tmp_path / "hold.db") == ["R-keep"]


def test_a_turn_that_started_playing_is_never_dropped(tmp_path: Path) -> None:
    """Once any of a turn's audio has played, the drop leaves it to barge-in."""
    pipeline, provider, player = _media(tmp_path, "R-said")
    conn = open_event_log(tmp_path / "hold.db")
    try:
        with _CallbackPump(player):
            asyncio.run(_submit_response(pipeline, _answer(conn, "R-said", "T-said")))
            _wait_until(lambda: bool(provider.opened))
            assert pipeline.drop_unspoken(frozenset({"T-said"})) == frozenset()
            _wait_until(lambda: _played(tmp_path / "hold.db") == ["R-said"])
    finally:
        assert pipeline.close()
        conn.close()



def test_a_sentence_accepted_before_the_earlier_run_opens_still_supersedes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live 2026-10-02 23:22: a line 0.3 s after the one before it was swept first.

    The earlier line's run opens ~0.4 s after its words, so the sweep for the
    next line found nothing to cancel and both answers played, one 18 s behind.
    The run must be cancelled as ``superseded`` the moment it opens.
    """
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    _script_decide(monkeypatch, _final_result())
    assert runtime.response_runs is not None
    intent = _emit_intent(runtime.conn, "T-first")
    _heard(runtime.conn, "T-first")
    make_supersede_unspoken_callable(runtime, lambda turn_ids: turn_ids)("T-next")
    assert not runtime.response_runs.open_runs()
    with pytest.raises(ResponseCancelledError):
        _drive_turn_on_own_connection(
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
    cancelled = _payloads(runtime.conn, "response.cancelled")
    assert [(row["turn_id"], row["reason"]) for row in cancelled] == [("T-first", "superseded")]
    assert cancelled[0]["interrupted_by_turn_id"] == "T-next"
    assert _event_count(runtime.conn, "surface.response_emitted") == 0
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


_FIRST = "我想问一下去学校的公交"
_SECOND = "现在出发能赶上哪一班"


def _merge_rig(tmp_path: Path, peer: Any) -> Any:  # noqa: ANN401 - JarvisRuntime
    runtime = _spoken_runtime(tmp_path, peer.url)
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
    with _Peer([[("final_answer", answer)]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        assert runtime.response_runs is not None
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(_drive, runtime, _spoken_turn(runtime.conn, "T-1", _FIRST))
            _wait_until(lambda: len(peer.requests) == 1, timeout_s=15.0)
            time.sleep(1.0)
            peer.first_event_delay_s = 0.0
            make_supersede_unspoken_callable(runtime, lambda turn_ids: turn_ids)("T-2")
            second = pool.submit(_drive, runtime, _spoken_turn(runtime.conn, "T-2", words))
            second.result(timeout=20)
            with pytest.raises(ResponseCancelledError):
                first.result(timeout=10)

    (cancelled,) = _payloads(runtime.conn, "response.cancelled")
    assert (cancelled["turn_id"], cancelled["reason"]) == ("T-1", "superseded")
    assert cancelled["interrupted_by_turn_id"] == "T-2"
    assert not _payloads(runtime.conn, "response.failed")
    assert not _payloads(runtime.conn, "turn.failed")
    emitted = _payloads(runtime.conn, "surface.response_emitted")
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
    with _Peer(scripts, first_event_delay_s=1.5) as peer:
        runtime = _merge_rig(tmp_path, peer)
        runtime = replace(
            runtime, response_flags=replace(runtime.response_flags, slow_results=True),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(_drive, runtime, _spoken_turn(runtime.conn, "T-1", _FIRST))
            _wait_until(lambda: len(peer.requests) == 1, timeout_s=15.0)
            emit_event(
                runtime.conn,
                type="action.dispatched",
                payload={"action_id": "A-1"},
                correlation={"turn_id": "T-1", "action_id": "A-1"},
            )
            time.sleep(1.0)
            peer.first_event_delay_s = 0.0
            make_supersede_unspoken_callable(runtime, lambda turn_ids: turn_ids)("T-2")
            second = pool.submit(_drive, runtime, _spoken_turn(runtime.conn, "T-2", words))
            first.result(timeout=20)
            if answers == 1:
                with pytest.raises(ResponseCancelledError):
                    second.result(timeout=10)
            else:
                second.result(timeout=20)

    cancelled = _payloads(runtime.conn, "response.cancelled")
    emitted = _payloads(runtime.conn, "surface.response_emitted")
    assert len(emitted) == answers
    # The repeat never asked the model: only the turns that answer were admitted a request.
    admitted = {row["response_id"] for row in _payloads(runtime.conn, "response.request_admitted")}
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
    with _Peer(
        [[("final_answer", "第一句。第二句。")], [("final_answer", "好的。")]], hold=True,
    ) as peer:
        runtime = _merge_rig(tmp_path, peer)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(_drive, runtime, _spoken_turn(runtime.conn, "T-1", _FIRST))
            _wait_until(lambda: bool(_payloads(runtime.conn, "surface.response_chunk")))
            # L5 reports the first sentence played: nothing of the turn is left to drop.
            make_supersede_unspoken_callable(runtime, lambda _turn_ids: frozenset())("T-2")
            second = pool.submit(_drive, runtime, _spoken_turn(runtime.conn, "T-2", _SECOND))
            peer.release.set()
            first.result(timeout=20)
            second.result(timeout=20)

    assert not _payloads(runtime.conn, "response.cancelled")
    emitted = _payloads(runtime.conn, "surface.response_emitted")
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
                    connection_lost=lambda: None, is_running=False,
                    observe_clock=lambda *_a: None,
                ),
                begin=nothing, close=nothing, stop_playback=stop_playback,
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
            app, lambda header: local_key_matches(local_key(root), header),
            extra_hosts=[REMOTE],
            device_token_matches=lambda one: device_token_matches(root, one),
        )
        self.client = TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000))

    def connect(self) -> Any:  # noqa: ANN401 - a Starlette test websocket context
        return self.client.websocket_connect(
            f"ws://{REMOTE}:8006{PHONE_PATH}", headers={"Authorization": f"Bearer {self.token}"},
        )

    def heard(self, turn_id: str) -> Any:  # noqa: ANN401 - Event
        """The ``utterance.received`` the take-over wrote for ``turn_id``, to drive its turn."""
        (event,) = [
            one for one in iter_events_of_types(self.events, ("utterance.received",))
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
    with _Peer([[("final_answer", answer)]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        flags = {"over_her": True} if over_her else {}
        with rig.connect() as ws, ThreadPoolExecutor(max_workers=2) as pool:
            _open(ws)
            first_said = _say(ws, "ua", _FIRST, **flags)
            first = pool.submit(_drive, runtime, rig.heard(first_said["turn_id"]))
            _wait_until(lambda: len(peer.requests) == 1, timeout_s=15.0)
            time.sleep(0.5)  # the peer reads its delay a moment after it logs the request
            peer.first_event_delay_s = 0.0
            second_said = _say(ws, "ub", _SECOND, continues="ua", **flags)
            assert second_said["verdict"] == "turn"
            second = pool.submit(_drive, runtime, rig.heard(second_said["turn_id"]))
            second.result(timeout=20)
            with pytest.raises(ResponseCancelledError):
                first.result(timeout=10)

    assert rig.taken_over == [(first_said["turn_id"], second_said["turn_id"], "superseded")]
    assert rig.voice_calls[-2:] == [  # the first say's own barge-in, if it was over her, is before
        ("drop_unspoken", frozenset({first_said["turn_id"]})), ("stop_playback", "barge_in"),
    ]
    (cancelled,) = _payloads(runtime.conn, "response.cancelled")
    assert (cancelled["turn_id"], cancelled["reason"]) == (first_said["turn_id"], "superseded")
    assert cancelled["interrupted_by_turn_id"] == second_said["turn_id"]
    # The event holds the thought and the segment; the rows hold the segments.
    heard = rig.heard(second_said["turn_id"]).payload
    assert (heard["transcript"], heard["segment"]) == (_THOUGHT, _SECOND)
    assert heard["continues"] == first_said["turn_id"]
    emitted = _payloads(runtime.conn, "surface.response_emitted")
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
    with _Peer(
        [[("final_answer", "我先查一下。再看看。")], [("final_answer", answer)]], hold=True,
    ) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime, audible=True)
        with rig.connect() as ws, ThreadPoolExecutor(max_workers=2) as pool:
            _open(ws)
            first_said = _say(ws, "ua", _FIRST, over_her=True)
            first = pool.submit(_drive, runtime, rig.heard(first_said["turn_id"]))
            _wait_until(lambda: bool(_payloads(runtime.conn, "surface.response_chunk")))
            second_said = _say(ws, "ub", _SECOND, over_her=True, continues="ua")
            peer.release.set()
            second = pool.submit(_drive, runtime, rig.heard(second_said["turn_id"]))
            second.result(timeout=20)
            with pytest.raises(ResponseCancelledError):
                first.result(timeout=10)

    assert ("stop_playback", "barge_in") in rig.voice_calls
    (cancelled,) = _payloads(runtime.conn, "response.cancelled")
    assert (cancelled["turn_id"], cancelled["reason"]) == (first_said["turn_id"], "superseded")
    assert cancelled["interrupted_by_turn_id"] == second_said["turn_id"]
    emitted = _payloads(runtime.conn, "surface.response_emitted")
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
    with _Peer([[("final_answer", answer)]], first_event_delay_s=30.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        with rig.connect() as ws, ThreadPoolExecutor(max_workers=3) as pool:
            _open(ws)
            said = [_say(ws, "ua", _FIRST, over_her=True)]
            futures = [pool.submit(_drive, runtime, rig.heard(said[0]["turn_id"]))]
            _wait_until(lambda: len(peer.requests) == 1, timeout_s=15.0)
            for utterance, words, before in (("ub", _SECOND, "ua"), ("uc", third, "ub")):
                time.sleep(0.5)  # the peer reads its delay a moment after it logs the request
                peer.first_event_delay_s = 0.0 if utterance == "uc" else 30.0
                said.append(_say(ws, utterance, words, over_her=True, continues=before))
                futures.append(pool.submit(_drive, runtime, rig.heard(said[-1]["turn_id"])))
                _wait_until(lambda: len(peer.requests) == len(futures), timeout_s=15.0)
            futures[2].result(timeout=60)
            for earlier in futures[:2]:
                with pytest.raises(ResponseCancelledError):
                    earlier.result(timeout=10)

    heard = rig.heard(said[2]["turn_id"]).payload
    assert heard["transcript"] == _FIRST + _SECOND + third
    assert heard["segment"] == third
    assert len(heard["folded"]) == 2
    cancelled = {row["turn_id"]: row["interrupted_by_turn_id"]
                 for row in _payloads(runtime.conn, "response.cancelled")}
    assert cancelled == {said[0]["turn_id"]: said[1]["turn_id"],
                         said[1]["turn_id"]: said[2]["turn_id"]}
    asked = json.dumps(peer.requests[-1][1]["input"], ensure_ascii=False)
    assert [asked.count(words) for words in (_FIRST, _SECOND, third)] == [1, 1, 1]
    assert _records(runtime) == [
        ("allen", _FIRST), ("allen", _SECOND), ("allen", third), ("jarvis", answer),
    ]
    (message,) = _unspoken_history(runtime)
    assert [message.count(words) for words in (_FIRST, _SECOND, third)] == [1, 1, 1]
    runtime.conn.close()


def test_a_sentence_that_names_no_earlier_one_is_a_turn_of_its_own(tmp_path: Path) -> None:
    """No ``continues``: nothing is taken over, the event holds the words as said, two answers."""
    scripts = [[("final_answer", "一。")], [("final_answer", "二。")]]
    with _Peer(scripts, first_event_delay_s=1.0) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        with rig.connect() as ws, ThreadPoolExecutor(max_workers=2) as pool:
            _open(ws)
            first_said = _say(ws, "ua", _FIRST, over_her=True)
            first = pool.submit(_drive, runtime, rig.heard(first_said["turn_id"]))
            _wait_until(lambda: len(peer.requests) == 1, timeout_s=15.0)
            time.sleep(0.5)  # the peer reads its delay a moment after it logs the request
            peer.first_event_delay_s = 0.0
            second_said = _say(ws, "ub", _SECOND, over_her=True)
            second = pool.submit(_drive, runtime, rig.heard(second_said["turn_id"]))
            first.result(timeout=20)
            second.result(timeout=20)

    assert rig.taken_over == []
    heard = rig.heard(second_said["turn_id"]).payload
    assert heard["transcript"] == _SECOND
    assert not {"segment", "continues", "folded"} & set(heard)
    assert not _payloads(runtime.conn, "response.cancelled")
    assert len(_payloads(runtime.conn, "surface.response_emitted")) == 2
    runtime.conn.close()


def test_words_that_go_on_from_an_answer_already_given_stand_alone(tmp_path: Path) -> None:
    """The earlier turn finished: its answer is a record that stays, so the words are a new turn."""
    with _Peer([[("final_answer", "一。")], [("final_answer", "二。")]]) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        with rig.connect() as ws:
            _open(ws)
            first_said = _say(ws, "ua", _FIRST)
            _drive(runtime, rig.heard(first_said["turn_id"]))
            second_said = _say(ws, "ub", _SECOND, over_her=True, continues="ua")
            _drive(runtime, rig.heard(second_said["turn_id"]))

    assert rig.taken_over == [(first_said["turn_id"], second_said["turn_id"], "over")]
    heard = rig.heard(second_said["turn_id"]).payload
    assert heard["transcript"] == _SECOND
    assert "continues" not in heard
    assert not _payloads(runtime.conn, "response.cancelled")
    assert [source for source, _text in _records(runtime)] == ["allen", "jarvis", "allen", "jarvis"]
    runtime.conn.close()


def test_a_continues_that_is_not_an_utterance_id_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """A malformed field is ``bad_say``, as the other flags are; an unknown id is no earlier one."""
    with _Peer([]) as peer:
        runtime = _merge_rig(tmp_path, peer)
        rig = _PhoneRig(runtime)
        with rig.connect() as ws:
            _open(ws)
            assert _say(ws, "ub", _SECOND, continues=7)["code"] == "bad_say"
            assert _say(ws, "ub", _SECOND, continues="has space")["code"] == "bad_say"
            unknown = _say(ws, "ub", _SECOND, continues="ua")
            assert unknown["verdict"] == "turn"
    assert rig.taken_over == []
    heard = rig.heard(unknown["turn_id"]).payload
    assert heard["transcript"] == _SECOND
    assert "continues" not in heard
    runtime.conn.close()
