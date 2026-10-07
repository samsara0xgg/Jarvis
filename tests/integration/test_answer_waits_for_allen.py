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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.response_run import ResponseCancelledError
from jarvis.runtime import make_relation_supersede_callable, make_supersede_unspoken_callable
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log
from jarvis.state.projections import PendingConfirmations
from jarvis.surface import voice_media, voice_pipeline
from tests.integration.test_conversation_mode import _Session
from tests.integration.test_foreground_arbitration import _whole
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
    assert _event_count(runtime.conn, "surface.response_emitted") == 0
    runtime.conn.close()
