"""Real L3/L2/L5 stream admission, SQLite rollback, replay, and cancel races."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.packet import assemble_packet
from jarvis.decision.response_run import ResponseTerminalizer, start_response_run
from jarvis.decision.stream_gate import routine_stream_policy, stream_emission_gate
from jarvis.decision.stream_risk import (
    ResponseRiskContext,
    SegmentRiskClassifier,
    snapshot_content_hash,
)
from jarvis.decision.stream_sentences import SemanticAssembler, SemanticCandidate
from jarvis.shared.realtime import TerminalCommitted
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.state.stream_emission import StreamEmissionError
from jarvis.surface.stream_emission import emit_permitted_segment

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.decision.response_run import ResponseRun
    from jarvis.decision.stream_gate import StreamGateOutcome
    from jarvis.shared import Event
    from jarvis.shared.stream_emission import EmissionPermit

_QUERY = "解释冰为什么融化。"
_TEXT = "冰从周围吸收热量。"


def _context(**changes: Any) -> ResponseRiskContext:  # noqa: ANN401 - typed dataclass replacement table
    base = ResponseRiskContext(
        response_id="response-stream-test",
        turn_id="turn-stream-test",
        user_request=_QUERY,
        route="casual_or_explanatory",
        active_subject_ref="none",
        linked_action_ids=(),
        pending_action_risk="none",
        confirmation_state="none",
        evidence_snapshot_hash="a" * 64,
        attention_channel="voice_notify",
        tools_offered=False,
        context_complete=True,
        unresolved_references=False,
        history_status="complete",
    )
    return replace(base, **changes)


class _Run:
    def __init__(
        self,
        conn: sqlite3.Connection,
        context: ResponseRiskContext,
        llm_config: dict[str, Any] | None = None,
    ) -> None:
        self.conn = conn
        trigger = emit_event(
            conn,
            type="utterance.received",
            payload={
                "transcript": context.user_request,
                "turn_id": context.turn_id,
            },
        )
        packet = assemble_packet(trigger, conn)
        self.context = replace(context, evidence_snapshot_hash=snapshot_content_hash(packet))
        self.policy = routine_stream_policy(self.context, preset_snapshot_hash="b" * 64)
        factory = LLMSessionFactory(
            llm_config
            if llm_config is not None
            else {
                "provider": "openai",
                "model": "synthetic",
                "base_url": "https://example.invalid",
            }
        )
        self.run: ResponseRun = start_response_run(
            conn,
            turn_id=context.turn_id,
            trigger_event_uid=trigger.event_uid,
            request_client=factory.create(factory.snapshot(), response_id=context.response_id),
            policy=self.policy,
            response_id=context.response_id,
        )
        self.terminalizer = ResponseTerminalizer(lambda: conn, close_after=False)

    def gate(
        self,
        text: str = _TEXT,
        sequence: int = 0,
        **kwargs: Any,  # noqa: ANN401 - forward real transaction failure fixtures
    ) -> StreamGateOutcome:
        return stream_emission_gate(
            self.conn,
            policy=self.policy,
            context=self.context,
            segment=SemanticCandidate(text, "sentence"),
            sequence=sequence,
            phase="final",
            channel="both",
            **kwargs,
        )

    def emit(
        self,
        permit: EmissionPermit,
        text: str = _TEXT,
        **kwargs: Any,  # noqa: ANN401 - forward real transaction failure fixtures
    ) -> Event:
        return emit_permitted_segment(
            self.conn,
            permit,
            text,
            query=self.context.user_request,
            attention_channel=self.context.attention_channel,
            **kwargs,
        ).event


@pytest.fixture
def stream_run(tmp_path: Path) -> Iterator[_Run]:
    """Open one isolated real response lifecycle with complete synthetic context."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        yield _Run(conn, _context())


def test_permitted_prefix_has_durable_causal_chain_and_idempotent_consumption(
    stream_run: _Run,
) -> None:
    """No final response is required to commit a safe stable model candidate."""
    bus = CommittedEventBus()
    seen: list[str] = []

    def after_commit(event: Event) -> None:
        assert not stream_run.conn.in_transaction
        assert any(item.event_uid == event.event_uid for item in iter_events(stream_run.conn))
        seen.append(event.event_uid)

    bus.subscribe(after_commit)
    assembler = SemanticAssembler()
    candidates = assembler.feed(_TEXT + "这些热量")
    assert len(candidates) == 1
    outcome = stream_run.gate(candidates[0].text, committed_event_bus=bus)
    assert outcome.permit is not None
    assert seen == [outcome.event.event_uid]
    chunk = stream_run.emit(outcome.permit, committed_event_bus=bus)
    events = tuple(iter_events(stream_run.conn))
    assert chunk.source_event_id == outcome.event.event_uid
    assert outcome.event.source_event_id == stream_run.run.facts.started_event_uid
    assert len(seen) == 3  # gate, response_open, chunk
    assert not any(
        item.type in {"response.completed", "surface.response_emitted"} for item in events
    )
    replay = stream_run.gate(committed_event_bus=bus)
    assert replay.permit == outcome.permit
    assert stream_run.emit(replay.permit, committed_event_bus=bus) == chunk
    assert len(seen) == 3
    stream_run.terminalizer.cancel(
        stream_run.run.facts,
        reason="test",
        cancel_scope="generation",
    )
    assert stream_run.emit(replay.permit, committed_event_bus=bus) == chunk
    assert len(seen) == 3


@pytest.mark.parametrize(
    "stage", ["after_begin", "after_terminal_check", "after_event_append", "before_commit"]
)
@pytest.mark.parametrize("where", ["gate", "surface"])
def test_commit_failure_never_publishes_and_retry_retains_sequence(
    stream_run: _Run,
    stage: str,
    where: str,
) -> None:
    """SQLite rollback leaves no receipt or first-open residue to speak."""
    bus = CommittedEventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)

    def fail(actual: str) -> None:
        if actual == stage:
            message = "injected emission rollback"
            raise sqlite3.OperationalError(message)

    permit = stream_run.gate().permit if where == "surface" else None
    before = tuple(iter_events(stream_run.conn))
    if where == "gate":
        with pytest.raises(sqlite3.OperationalError, match="emission rollback"):
            stream_run.gate(committed_event_bus=bus, failure_injector=fail)
    else:
        assert permit is not None
        with pytest.raises(sqlite3.OperationalError, match="emission rollback"):
            stream_run.emit(permit, committed_event_bus=bus, failure_injector=fail)
    assert tuple(iter_events(stream_run.conn)) == before
    assert not seen
    outcome = stream_run.gate()
    assert outcome.permit is not None
    assert stream_run.emit(outcome.permit).payload["sequence"] == 0


@pytest.mark.parametrize(
    "changed",
    [
        {"response_id": "other"},
        {"segment_sequence": 1},
        {"segment_sequence": True},
        {"phase": "commentary"},
        {"channel": "speech"},
        {"segment_hash": "c" * 64},
        {"policy_hash": "c" * 64},
        {"gate_mode": "full_text"},
        {"source_gate_event_uid": "absent"},
        {"issued_at_ms": 0},
    ],
)
def test_forged_receipt_cannot_emit(stream_run: _Run, changed: dict[str, Any]) -> None:
    """Every receipt identity field is verified against the persisted gate."""
    outcome = stream_run.gate()
    assert outcome.permit is not None
    forged = replace(outcome.permit, **changed)
    with pytest.raises(StreamEmissionError):
        stream_run.emit(forged)
    assert not any(
        event.type.startswith("surface.response") for event in iter_events(stream_run.conn)
    )


def test_text_attention_gate_source_and_conflicting_replay_cannot_change(stream_run: _Run) -> None:
    """Permit metadata cannot authorize different bytes, channel, or candidate."""
    outcome = stream_run.gate()
    assert outcome.permit is not None
    with pytest.raises(StreamEmissionError, match="text hash"):
        stream_run.emit(outcome.permit, text=_TEXT + " ")
    with pytest.raises(StreamEmissionError, match="binding mismatch"):
        emit_permitted_segment(
            stream_run.conn,
            outcome.permit,
            _TEXT,
            query=_QUERY,
            attention_channel="ask_confirm",
        )
    forged = replace(outcome.permit, source_gate_event_uid=stream_run.run.facts.started_event_uid)
    with pytest.raises(StreamEmissionError, match="committed stream gate"):
        stream_run.emit(forged)
    with pytest.raises(StreamEmissionError, match="binding mismatch"):
        stream_run.gate("冰会逐渐变成液态水。")


def test_buffered_candidate_seals_later_permits_and_full_text_policy_never_streams(
    stream_run: _Run,
    tmp_path: Path,
) -> None:
    """Creating another gate cannot loosen a previous conservative decision."""
    denied = stream_run.gate("没有发送邮件。")
    assert denied.permit is None
    assert denied.event.payload["candidate_risk"] == "consequential_claim"
    with pytest.raises(StreamEmissionError, match="cannot loosen"):
        stream_run.gate(_TEXT, sequence=1)
    with contextlib.closing(open_event_log(tmp_path / "unknown.db")) as conn:
        full = _Run(conn, _context(active_subject_ref="unknown"))
        assert full.policy.emission_mode == "full_text"
        assert full.gate().permit is None


@pytest.mark.parametrize("when", ["before_gate", "between_gate_and_surface"])
def test_cancel_prevents_future_surface_admission(stream_run: _Run, when: str) -> None:
    """A previously committed permission cannot outlive the response's cancel."""
    permit = stream_run.gate().permit if when == "between_gate_and_surface" else None
    stream_run.terminalizer.cancel(stream_run.run.facts, reason="stop", cancel_scope="generation")
    if permit is None:
        with pytest.raises(StreamEmissionError, match="terminal"):
            stream_run.gate()
    else:
        with pytest.raises(StreamEmissionError, match="terminal"):
            stream_run.emit(permit)
    assert not any(
        event.type.startswith("surface.response") for event in iter_events(stream_run.conn)
    )


def test_cancel_waiting_on_sqlite_writer_lock_linearizes_after_committed_chunk(
    stream_run: _Run,
    tmp_path: Path,
) -> None:
    """Real two-connection contention yields one of the two legal durable orders."""
    outcome = stream_run.gate()
    assert outcome.permit is not None
    entered = threading.Event()
    finished = threading.Event()

    def cancel() -> None:
        with contextlib.closing(sqlite3.connect(tmp_path / "events.db", timeout=2)) as conn:
            terminalizer = ResponseTerminalizer(lambda: conn, close_after=False)
            entered.set()
            result = terminalizer.cancel(
                stream_run.run.facts, reason="race", cancel_scope="generation"
            )
            assert isinstance(result, TerminalCommitted)
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = None

        def held(stage: str) -> None:
            nonlocal future
            if stage == "after_terminal_check":
                future = pool.submit(cancel)
                assert entered.wait(1)
                assert not finished.is_set()

        chunk = stream_run.emit(outcome.permit, failure_injector=held)
        assert future is not None
        future.result(timeout=2)
    events = tuple(iter_events(stream_run.conn))
    cancellation = next(event for event in events if event.type == "response.cancelled")
    assert events.index(chunk) < events.index(cancellation)
    with pytest.raises(StreamEmissionError, match="terminal"):
        stream_run.gate(sequence=1)


def test_classifier_timeout_and_unknown_version_are_durable_buffer_outcomes(
    stream_run: _Run,
) -> None:
    """The latency cutoff changes permission, not merely the telemetry label."""
    times = iter((0.0, 0.021))
    classifier = SegmentRiskClassifier(clock=lambda: next(times))
    outcome = stream_run.gate(classifier=classifier)
    assert outcome.permit is None
    assert "classifier_deadline_exceeded" in outcome.event.payload["reasons"]
    outcome = stream_run.gate(
        sequence=1, classifier=SegmentRiskClassifier(rule_version="unreviewed")
    )
    assert outcome.permit is None
    assert "unknown_rule_version" in outcome.event.payload["reasons"]


def test_snapshot_hash_uses_contents_and_rejects_opaque_objects(stream_run: _Run) -> None:
    """Equal log positions with different evidence cannot share a risk identity."""
    assert snapshot_content_hash(
        {"x": (1, 2), "y": frozenset({"a", "b"})}
    ) == snapshot_content_hash(
        {"y": frozenset({"b", "a"}), "x": [1, 2]},
    )
    assert snapshot_content_hash({"id": 10, "evidence": "refuted"}) != snapshot_content_hash(
        {"id": 10, "evidence": "verified"},
    )
    with pytest.raises(TypeError, match="unsupported snapshot"):
        snapshot_content_hash(object())
    changed = replace(stream_run.context, evidence_snapshot_hash="c" * 64)
    with pytest.raises(StreamEmissionError, match="pinned policy"):
        stream_emission_gate(
            stream_run.conn,
            policy=stream_run.policy,
            context=changed,
            segment=SemanticCandidate(_TEXT, "sentence"),
            sequence=0,
            phase="final",
            channel="both",
        )


@pytest.mark.parametrize(
    "text",
    [
        "Take two pills.",
        "吞两片阿司匹林。",
        "批准了。",
        "已删光了。",
        "I erased your photos.",
        "I opened the door.",
        "You may proceed.",
        "Run sudo rm -rf /.",
        "No need to ask for consent.",
        "I did not erase anything.",
        "冰" * 100 + "。",
        "A dog chased a ball.",
    ],
)
def test_off_topic_and_unbounded_candidates_never_receive_permits(
    stream_run: _Run, text: str
) -> None:
    """Independent counterexamples are rejected at the real durable L3 seam."""
    assert stream_run.gate(text).permit is None


@pytest.mark.parametrize("already_emitted", [False, True])
def test_open_query_cannot_impersonate_different_input(
    stream_run: _Run, *, already_emitted: bool
) -> None:
    """Initial display and replay both bind query to the original user event."""
    outcome = stream_run.gate()
    assert outcome.permit is not None
    if already_emitted:
        stream_run.emit(outcome.permit)
    with pytest.raises(StreamEmissionError, match="binding mismatch"):
        emit_permitted_segment(
            stream_run.conn,
            outcome.permit,
            _TEXT,
            query="I approved the payment.",
            attention_channel=stream_run.context.attention_channel,
        )
