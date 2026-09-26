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
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.response_run import ResponseCancelledError
from jarvis.runtime import make_supersede_unspoken_callable
from jarvis.state.event_log import emit_event, open_event_log
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

    def __init__(self, record: list[str], *, empty: bool = False) -> None:
        super().__init__()
        self._record = record
        self._empty = empty

    def run_turn(self, **kwargs: Any) -> Any:  # noqa: ANN401
        if self._empty:
            msg = "silence"
            raise voice_pipeline.VoicePipelineEmptyError(msg)
        kwargs["before_emit"]()
        self._record.append("utterance.received")
        return super().run_turn(**kwargs)


def _capture_rig(
    monkeypatch: pytest.MonkeyPatch, *, empty: bool = False,
) -> tuple[_Session, list[str]]:
    record: list[str] = []
    rig = _Session(
        monkeypatch,
        pipeline=_AcceptingPipeline(record, empty=empty),
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
    """Of three held answers only the unspoken voice one is dropped.

    ``T-voice`` answers a voice sentence and never reached the speaker: dropped.
    ``T-heard`` answers one too, but L5 already played some of it: kept.
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
    _heard(runtime.conn, "T-voice")
    _heard(runtime.conn, "T-heard")
    with ThreadPoolExecutor(max_workers=3) as pool:
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
        _wait_open(runtime, 3)
        supersede("T-next")
        with pytest.raises(ResponseCancelledError):
            futures["T-voice"].result(timeout=10)
        runtime.response_runs.hold_completion(held=False)
        futures["T-heard"].result(timeout=10)
        futures["T-typed"].result(timeout=10)

    assert asked == [frozenset({"T-voice", "T-heard"})]
    cancelled = _payloads(runtime.conn, "response.cancelled")
    assert [(row["turn_id"], row["reason"]) for row in cancelled] == [("T-voice", "superseded")]
    emitted = {row["turn_id"] for row in _payloads(runtime.conn, "surface.response_emitted")}
    assert emitted == {"T-heard", "T-typed"}
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

