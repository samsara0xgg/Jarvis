"""ADR-0008 D3: the spoken prefix is durable, immutable, and finalized byte-for-byte."""

from __future__ import annotations

import contextlib
import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from jarvis.decision.gates import ResponsePlan
from jarvis.decision.llm_stream import LLMResponseCompleted, LLMTextDelta
from jarvis.decision.response_run import ResponseTerminalizer, start_response_run
from jarvis.decision.stream_finalize import StreamFinalizationFailure, finalize_stream
from jarvis.decision.stream_gate import routine_stream_policy
from jarvis.decision.stream_risk import SegmentRiskClassifier
from jarvis.decision.stream_sentences import SemanticAssembler
from jarvis.shared.realtime import TerminalCommitted
from jarvis.state.event_log import get_event, iter_events, open_event_log
from jarvis.state.stream_emission import StreamEmissionError, committed_text_prefix
from tests.integration.test_stream_emission_gate import _context, _Run

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.decision.llm_stream import LLMStreamEvent

_SEGMENTS = ("冰从周围吸收热量。", "这些热量来自空气。")
_TAIL = "所以冰会变成水。"
_REJECTED_TAIL = "邮件已经发送。"


def _delta(text: str) -> LLMTextDelta:
    return LLMTextDelta(
        llm_request_id="req-finalize", provider_response_id=None, text=text, content_block_index=0
    )


# Provider coalescing splits sentences arbitrarily; the assembler, not the
# provider, decides what becomes a candidate.
_STREAM: tuple[LLMStreamEvent, ...] = (
    _delta("冰从周围"),
    _delta("吸收热量。这些热量"),
    _delta("来自空气。所以冰"),
    _delta("会变成水"),
    LLMResponseCompleted(
        llm_request_id="req-finalize", provider_response_id=None, finish_reason="stop"
    ),
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _stream_prefix(run: _Run, stream: tuple[LLMStreamEvent, ...]) -> tuple[str, str]:
    """Drive typed deltas through assembler, classifier, gate and surface."""
    assembler = SemanticAssembler()
    classifier = SegmentRiskClassifier()
    prefix = ""
    sequence = 0
    for event in stream:
        if not isinstance(event, LLMTextDelta):
            continue
        for candidate in assembler.feed(event.text):
            outcome = run.gate(candidate.text, sequence=sequence, classifier=classifier)
            assert outcome.permit is not None
            run.emit(outcome.permit, text=candidate.text)
            prefix += candidate.text
            sequence += 1
    return prefix, assembler.pending_text


def _commit_prefix(run: _Run, segments: tuple[str, ...]) -> str:
    """Gate and expose ``segments`` in order; return the text now spoken."""
    prefix = ""
    for sequence, text in enumerate(segments):
        outcome = run.gate(text, sequence=sequence)
        assert outcome.permit is not None
        run.emit(outcome.permit, text=text)
        prefix += text
    return prefix


def test_restart_reconstructs_prefix_from_event_log_equal_to_memory(tmp_path: Path) -> None:
    """After a crash only exposed chunks are prefix; a permit alone never is."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        run = _Run(conn, _context())
        prefix = _commit_prefix(run, _SEGMENTS)
        dangling = run.gate(_TAIL, sequence=len(_SEGMENTS))
        assert dangling.permit is not None
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as restarted:
        durable = committed_text_prefix(restarted, run.run.response_id)
        assert durable.text == prefix
        assert durable.next_segment_sequence == len(_SEGMENTS)
        assert durable.policy_hash == run.policy.policy_hash
        assert durable.prefix_hash == _sha256(prefix)
        cancelled = ResponseTerminalizer(lambda: restarted, close_after=False).cancel(
            run.run.facts,
            reason="daemon_restart",
            cancel_scope="generation",
            committed_prefix_hash=durable.prefix_hash,
        )
        assert isinstance(cancelled, TerminalCommitted)
        assert cancelled.event.type == "response.cancelled"
        assert cancelled.event.payload["committed_prefix_hash"] == _sha256(prefix)


def test_chain_finalizes_byte_for_byte_and_completes_with_returned_hash(tmp_path: Path) -> None:
    """Typed stream -> permits -> chunks -> finalize -> terminal, one text throughout."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        run = _Run(conn, _context())
        prefix, suffix = _stream_prefix(run, _STREAM)
        assert prefix == "".join(_SEGMENTS)
        assert suffix == "所以冰会变成水"
        before = tuple(iter_events(conn))
        plan = finalize_stream(
            conn,
            committed_prefix=prefix,
            uncommitted_suffix=suffix,
            policy=run.policy,
            context=run.context,
        )
        assert isinstance(plan, ResponsePlan)
        assert plan.text.encode() == prefix.encode() + suffix.encode()
        assert plan.response_hash == _sha256(plan.text)
        assert (plan.output_risk_class, plan.required_gate_mode) == ("routine", "sentence")
        assert tuple(iter_events(conn)) == before
        outcome = run.terminalizer.complete(run.run.facts, response_hash=plan.response_hash)
        assert isinstance(outcome, TerminalCommitted)
        assert outcome.event.type == "response.completed"
        assert outcome.event.payload["response_hash"] == plan.response_hash
        assert outcome.event.source_event_id == run.run.facts.started_event_uid


def test_rejected_suffix_leaves_prefix_and_a_second_suffix_finalizes(tmp_path: Path) -> None:
    """Only the tail is regenerated; the spoken prefix and the log do not move."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        run = _Run(conn, _context())
        prefix = _commit_prefix(run, _SEGMENTS)
        before = tuple(iter_events(conn))
        failure = finalize_stream(
            conn,
            committed_prefix=prefix,
            uncommitted_suffix=_REJECTED_TAIL,
            policy=run.policy,
            context=run.context,
        )
        assert isinstance(failure, StreamFinalizationFailure)
        assert failure.reason == "suffix_rejected"
        assert failure.committed_prefix_hash == _sha256(prefix)
        assert failure.gate_reasons[-2:] == (
            "action_evidence_or_progress",
            "routine_ceiling_not_met",
        )
        assert failure.suffix_risk == "consequential_claim"
        assert tuple(iter_events(conn)) == before
        assert committed_text_prefix(conn, run.run.response_id).text == prefix
        plan = finalize_stream(
            conn,
            committed_prefix=prefix,
            uncommitted_suffix=_TAIL,
            policy=run.policy,
            context=run.context,
        )
        assert isinstance(plan, ResponsePlan)
        assert plan.text == prefix + _TAIL


def test_policy_hash_differing_from_permits_yields_policy_mismatch(tmp_path: Path) -> None:
    """The permits' pinned policy is the only policy a finalization may claim."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        run = _Run(conn, _context())
        prefix = _commit_prefix(run, _SEGMENTS)
        drifted = replace(run.policy, preset_snapshot_hash="c" * 64)
        assert drifted.policy_hash != run.policy.policy_hash
        failure = finalize_stream(
            conn,
            committed_prefix=prefix,
            uncommitted_suffix=_TAIL,
            policy=drifted,
            context=run.context,
        )
        assert failure == StreamFinalizationFailure(
            run.run.response_id,
            "policy_mismatch",
            _sha256(prefix),
            ("policy_hash_differs_from_committed_permits",),
        )
        with pytest.raises(StreamEmissionError, match="pinned"):
            finalize_stream(
                conn,
                committed_prefix=prefix,
                uncommitted_suffix=_TAIL,
                policy=run.policy,
                context=replace(run.context, evidence_snapshot_hash="c" * 64),
            )


def test_invalid_prefix_fails_the_run_and_starts_a_correction_run(tmp_path: Path) -> None:
    """Memory that disagrees with the log is failed, never re-spoken or retried whole."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        run = _Run(conn, _context())
        prefix = _commit_prefix(run, _SEGMENTS)
        failure = finalize_stream(
            conn,
            committed_prefix=prefix + "还在继续。",
            uncommitted_suffix=_TAIL,
            policy=run.policy,
            context=run.context,
        )
        assert isinstance(failure, StreamFinalizationFailure)
        assert failure.reason == "committed_prefix_invalid"
        assert failure.committed_prefix_hash == _sha256(prefix)
        failed = run.terminalizer.fail(
            run.run.facts,
            reason=failure.reason,
            retryable=False,
            committed_prefix_hash=failure.committed_prefix_hash,
        )
        assert isinstance(failed, TerminalCommitted)
        assert failed.event.type == "response.failed"
        assert failed.event.payload["committed_prefix_hash"] == _sha256(prefix)
        correction_context = replace(run.context, response_id="response-correction")
        correction = start_response_run(
            conn,
            turn_id=run.context.turn_id,
            trigger_event_uid=_started_source(conn, run.run.facts.started_event_uid),
            request_client=run.run.request_client,
            policy=routine_stream_policy(correction_context, preset_snapshot_hash="b" * 64),
            response_id=correction_context.response_id,
            corrects_response_id=run.run.response_id,
        )
        started = next(
            event
            for event in iter_events(conn)
            if event.type == "response.started"
            and event.payload["response_id"] == correction.response_id
        )
        assert started.payload["corrects_response_id"] == run.run.response_id
        assert started.payload["response_group_id"] == run.run.response_group_id
        assert committed_text_prefix(conn, correction.response_id).text == ""
        assert committed_text_prefix(conn, run.run.response_id).text == prefix
        print(failed.event.type, failed.event.payload)  # noqa: T201 - acceptance evidence
        print(started.type, started.payload)  # noqa: T201 - acceptance evidence


def _started_source(conn: sqlite3.Connection, started_event_uid: str) -> str:
    """Return the user trigger that caused a response start."""
    started = get_event(conn, started_event_uid)
    assert started is not None
    assert started.source_event_id is not None
    return started.source_event_id
