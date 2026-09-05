"""Transactional storage of stream verdicts and consumption of their receipts.

L3 determines risk. These primitives only enforce durable identity, ordering,
monotonic closure, and cancellation against the same SQLite write lock. A gate
receipt is not trusted until its committed event has been read and matched.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, NoReturn

from jarvis.state.event_log import append_event_in_transaction, get_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from jarvis.shared import Event
    from jarvis.shared.stream_emission import EmissionPermit, StreamGateAssessment
    from jarvis.state.committed_event_bus import CommittedEventBus
    from jarvis.state.lifecycle_terminal import FailureInjector, FailureStage


class StreamEmissionError(RuntimeError):
    """Invalid, conflicting, closed, or uncommitted emission request."""


@dataclass(frozen=True)
class SegmentCommit:
    """Durable chunk plus whether this call appended it for the first time."""

    event: Event
    appended: bool


@dataclass(frozen=True)
class CommittedPrefix:
    """The spoken prefix of one response, folded from its durable chunk chain."""

    response_id: str
    text: str
    next_segment_sequence: int
    policy_hash: str

    @property
    def prefix_hash(self) -> str:
        """Return the sha256 of the committed text, the terminal-payload form."""
        return hashlib.sha256(self.text.encode()).hexdigest()


def _inject(injector: FailureInjector | None, stage: FailureStage) -> None:
    if injector is not None:
        injector(stage)


def _fail(message: str) -> NoReturn:
    raise StreamEmissionError(message)


def _begin(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        _fail("stream emission primitive requires transaction ownership")
    conn.execute("BEGIN IMMEDIATE")


def _events(conn: sqlite3.Connection, response_id: str, event_type: str) -> tuple[Event, ...]:
    rows = conn.execute(
        "SELECT event_uid FROM events WHERE type = ? "
        "AND json_extract(payload_json, '$.response_id') = ? ORDER BY id",
        (event_type, response_id),
    ).fetchall()
    return tuple(_event(conn, str(row[0])) for row in rows)


def _event(conn: sqlite3.Connection, event_uid: str) -> Event:
    event = get_event(conn, event_uid)
    if event is None:
        _fail("stream source event does not exist")
    return event


def _started(conn: sqlite3.Connection, response_id: str) -> Event:
    starts = _events(conn, response_id, "response.started")
    if len(starts) != 1:
        _fail("stream emission requires exactly one response start")
    return starts[0]


def _require_open(conn: sqlite3.Connection, response_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM events WHERE type IN "
        "('response.completed', 'response.cancelled', 'response.failed') "
        "AND json_extract(payload_json, '$.response_id') = ? LIMIT 1",
        (response_id,),
    ).fetchone()
    if row is not None:
        _fail("response is terminal")


def _match(payload: Mapping[str, object], expected: Mapping[str, object]) -> None:
    if any(payload.get(key) != value for key, value in expected.items()):
        _fail("stream identity or policy binding mismatch")


def _gate_payload(assessment: StreamGateAssessment) -> dict[str, object]:
    payload = asdict(assessment)
    payload["reasons"] = list(assessment.reasons)
    payload["gate"] = "stream_emit"
    payload["required_gate_mode"] = "sentence"
    return payload


def _validate_assessment(assessment: StreamGateAssessment) -> None:
    if (
        type(assessment.sequence) is not int
        or assessment.sequence < 0
        or assessment.phase not in {"commentary", "final"}
        or assessment.channel not in {"speech", "document", "both"}
        or assessment.outcome not in {"permit", "buffer_full_text", "refuse"}
        or assessment.candidate_risk
        not in {"routine", "consequential_claim", "high_risk_claim", "unknown"}
        or not math.isfinite(assessment.classification_elapsed_ms)
        or assessment.classification_elapsed_ms < 0
    ):
        _fail("invalid stream assessment")
    for value in (
        assessment.segment_hash,
        assessment.policy_hash,
        assessment.evidence_snapshot_hash,
        assessment.risk_context_hash,
    ):
        if len(value) != hashlib.sha256().digest_size * 2 or any(
            char not in "0123456789abcdef" for char in value
        ):
            _fail("stream assessment requires canonical sha256 bindings")


def append_stream_gate(
    conn: sqlite3.Connection,
    assessment: StreamGateAssessment,
    *,
    committed_event_bus: CommittedEventBus | None = None,
    failure_injector: FailureInjector | None = None,
) -> Event:
    """Commit one immutable verdict per response/sequence before receipt issuance.

    Exact retry returns the original event without republishing. Timing is an
    observation of the original attempt, not part of retry identity. A denied
    candidate seals this run against subsequent ordinary stream permissions.
    """
    _validate_assessment(assessment)
    payload = _gate_payload(assessment)
    _begin(conn)
    try:
        _inject(failure_injector, "after_begin")
        started = _started(conn, assessment.response_id)
        _match(
            started.payload,
            {
                key: payload[key]
                for key in (
                    "response_id",
                    "phase",
                    "channel",
                    "policy_hash",
                    "evidence_snapshot_hash",
                    "risk_context_hash",
                    "classifier_rule_version",
                )
            },
        )
        gates = tuple(
            event
            for event in _events(conn, assessment.response_id, "gate.evaluated")
            if event.payload.get("gate") == "stream_emit"
        )
        for existing in gates:
            if existing.payload.get("sequence") == assessment.sequence:
                _match(
                    existing.payload,
                    {
                        key: value
                        for key, value in payload.items()
                        if key != "classification_elapsed_ms"
                    },
                )
                conn.commit()
                return existing
        _require_open(conn, assessment.response_id)
        _inject(failure_injector, "after_terminal_check")
        if assessment.sequence != len(gates):
            _fail("stream gate sequence must be contiguous")
        if assessment.outcome == "permit":
            _match(
                started.payload,
                {
                    "emission_mode": "routine_stream",
                    "output_risk_class": "routine",
                    "required_gate_mode": "sentence",
                    "active_subject_ref": "none",
                },
            )
            if assessment.candidate_risk != "routine" or any(
                event.payload.get("outcome") != "permit" for event in gates
            ):
                _fail("ordinary stream permission cannot loosen prior risk")
        event = append_event_in_transaction(
            conn,
            type="gate.evaluated",
            payload=payload,
            source_event_id=started.event_uid,
            correlation=started.correlation,
        )
        _inject(failure_injector, "after_event_append")
        _inject(failure_injector, "before_commit")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if committed_event_bus is not None:
        committed_event_bus.publish(event)
    return event


def _validate_permit(conn: sqlite3.Connection, permit: EmissionPermit, text: str) -> Event:
    if type(permit.segment_sequence) is not int or permit.segment_sequence < 0:
        _fail("invalid permit sequence")
    gate = _event(conn, permit.source_gate_event_uid)
    if gate.type != "gate.evaluated":
        _fail("permit source must be a committed stream gate")
    if not text.strip() or hashlib.sha256(text.encode()).hexdigest() != permit.segment_hash:
        _fail("permit text hash mismatch")
    _match(
        gate.payload,
        {
            "gate": "stream_emit",
            "outcome": "permit",
            "candidate_risk": "routine",
            "response_id": permit.response_id,
            "sequence": permit.segment_sequence,
            "phase": permit.phase,
            "channel": permit.channel,
            "segment_hash": permit.segment_hash,
            "policy_hash": permit.policy_hash,
            "required_gate_mode": permit.gate_mode,
        },
    )
    if permit.gate_mode != "sentence" or permit.issued_at_ms != gate.ts_epoch_ms:
        _fail("permit issuance binding mismatch")
    started = _started(conn, permit.response_id)
    if gate.source_event_id != started.event_uid:
        _fail("permit gate is not caused by this response start")
    _match(
        started.payload,
        {
            "policy_hash": permit.policy_hash,
            "phase": permit.phase,
            "channel": permit.channel,
            "emission_mode": "routine_stream",
            "required_gate_mode": "sentence",
        },
    )
    return started


def append_permitted_segment(  # noqa: C901, PLR0913 - one atomic validation/append transaction
    conn: sqlite3.Connection,
    permit: EmissionPermit,
    text: str,
    *,
    query: str,
    attention_channel: str,
    committed_event_bus: CommittedEventBus | None = None,
    failure_injector: FailureInjector | None = None,
) -> SegmentCommit:
    """Consume a committed permit once; append open+chunk atomically on first use.

    Recheck terminal state under the writer lock: a permit committed just before
    cancellation cannot introduce output afterward. Committed retries are reads;
    watchers recover any notification lost after an earlier successful commit.
    """
    _begin(conn)
    published: list[Event] = []
    try:
        _inject(failure_injector, "after_begin")
        started = _validate_permit(conn, permit, text)
        trigger = _event(conn, started.source_event_id or "")
        if trigger.type not in {"utterance.received", "surface.user_intent"}:
            _fail("ordinary stream requires a direct user input source")
        _match(trigger.payload, {"transcript": query, "turn_id": started.payload["turn_id"]})
        _match(
            _event(conn, permit.source_gate_event_uid).payload,
            {
                "attention_channel": attention_channel,
            },
        )
        payload = {
            "response_id": permit.response_id,
            "response_group_id": started.payload["response_group_id"],
            "turn_id": started.payload["turn_id"],
            "sequence": permit.segment_sequence,
            "phase": permit.phase,
            "channel": permit.channel,
            "segment_hash": permit.segment_hash,
            "text": text,
        }
        chunks = _events(conn, permit.response_id, "surface.response_chunk")
        opens = _events(conn, permit.response_id, "surface.response_open")
        open_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"sequence", "segment_hash", "text"}
        }
        open_payload.update(
            query=query,
            kind="stream",
            required_gate_mode="sentence",
            attention_channel=attention_channel,
        )
        if opens:
            if len(opens) != 1 or opens[0].source_event_id != started.event_uid:
                _fail("conflicting surface response open")
            _match(opens[0].payload, open_payload)
        elif chunks:
            _fail("surface chunks require a committed response open")
        for existing in chunks:
            if existing.payload.get("sequence") == permit.segment_sequence:
                _match(existing.payload, payload)
                if existing.source_event_id != permit.source_gate_event_uid:
                    _fail("conflicting surface source gate")
                conn.commit()
                return SegmentCommit(existing, appended=False)
        _require_open(conn, permit.response_id)
        _inject(failure_injector, "after_terminal_check")
        if permit.segment_sequence != len(chunks):
            _fail("surface segment sequence must be contiguous")
        if not opens:
            published.append(
                append_event_in_transaction(
                    conn,
                    type="surface.response_open",
                    payload=open_payload,
                    source_event_id=started.event_uid,
                    correlation=started.correlation,
                )
            )
        event = append_event_in_transaction(
            conn,
            type="surface.response_chunk",
            payload=payload,
            source_event_id=permit.source_gate_event_uid,
            correlation=started.correlation,
        )
        published.append(event)
        _inject(failure_injector, "after_event_append")
        _inject(failure_injector, "before_commit")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if committed_event_bus is not None:
        for notification in published:
            committed_event_bus.publish(notification)
    return SegmentCommit(event, appended=True)


def committed_text_prefix(conn: sqlite3.Connection, response_id: str) -> CommittedPrefix:
    """Reconstruct what was durably exposed, never what memory believes was spoken.

    Only a ``surface.response_chunk`` caused by a committed permit counts. A
    permit whose chunk never committed was never exposed, so it is not prefix;
    a chunk whose bindings disagree with its permit is corruption, not prefix.
    """
    started = _started(conn, response_id)
    policy_hash = str(started.payload["policy_hash"])
    text = ""
    chunks = _events(conn, response_id, "surface.response_chunk")
    for sequence, chunk in enumerate(chunks):
        segment = chunk.payload.get("text")
        if not isinstance(segment, str):
            _fail("committed chunk carries no text")
        segment_hash = hashlib.sha256(segment.encode()).hexdigest()
        _match(chunk.payload, {"sequence": sequence, "segment_hash": segment_hash})
        _match(
            _event(conn, chunk.source_event_id or "").payload,
            {
                "gate": "stream_emit",
                "outcome": "permit",
                "response_id": response_id,
                "sequence": sequence,
                "segment_hash": segment_hash,
                "policy_hash": policy_hash,
            },
        )
        text += segment
    return CommittedPrefix(response_id, text, len(chunks), policy_hash)
