"""Acceptance for the L2 v2 input submission inbox (ADR-0014 D21).

Every case runs against a real Event Log on disk: the properties under test
are transactional, so an in-memory double would prove nothing about what
survives a rollback.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from jarvis.state.event_log import emit_event, get_event, open_event_log
from jarvis.state.input_claim import claim_input_once
from jarvis.state.input_submission_inbox import (
    AsrProcessingLease,
    InputReceipt,
    PayloadConflictError,
    SubmissionInProgressError,
    SubmissionKey,
    claim_asr_request,
    payload_hash,
    release_asr_request,
    resolve_asr_request,
    submit_text_once,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.state.input_submission_inbox import FailureStage

_KEY = SubmissionKey(
    authenticated_principal="inherent_v2",
    client_instance_id="I-abc",
    request_id="req-1",
)


def _log(tmp_path: Path) -> sqlite3.Connection:
    return open_event_log(tmp_path / "mac_events.db")


def _intent_rows(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (str(uid), str(payload))
        for uid, payload in conn.execute(
            "SELECT event_uid, payload_json FROM events "
            "WHERE type = 'surface.user_intent' ORDER BY id ASC",
        )
    ]


def _receipt_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM input_submission_receipts").fetchone()[0],
    )


def test_text_submit_appends_one_canonical_intent_with_the_request_id(tmp_path: Path) -> None:
    """The happy path: one receipt, one canonical row carrying D21's stamps."""
    conn = _log(tmp_path)
    receipt = submit_text_once(conn, key=_KEY, transcript="今天天气怎么样")

    rows = _intent_rows(conn)
    assert len(rows) == 1
    event_uid, payload = rows[0]
    assert receipt.input_event_uid == event_uid
    assert receipt.request_id == "req-1"
    assert receipt.turn_id.startswith("T")
    assert receipt.replayed is False
    decoded = json.loads(payload)
    assert decoded["source_client_request_id"] == "req-1"
    assert decoded["source_surface"] == "inherent_v2"
    assert decoded["turn_id"] == receipt.turn_id
    assert decoded["transcript"] == "今天天气怎么样"
    assert _receipt_count(conn) == 1
    conn.close()


def test_identical_retry_replays_the_receipt_and_appends_nothing(tmp_path: Path) -> None:
    """A lost HTTP response is what this exists for: retry, same ids, no new turn."""
    conn = _log(tmp_path)
    first = submit_text_once(conn, key=_KEY, transcript="hello")
    second = submit_text_once(conn, key=_KEY, transcript="hello")

    assert second.input_event_uid == first.input_event_uid
    assert second.turn_id == first.turn_id
    assert second.replayed is True
    assert len(_intent_rows(conn)) == 1
    assert _receipt_count(conn) == 1
    conn.close()


def test_same_request_id_with_a_different_payload_is_rejected(tmp_path: Path) -> None:
    """D21: a different payload under one request id is a conflict, not a second turn."""
    conn = _log(tmp_path)
    submit_text_once(conn, key=_KEY, transcript="hello")

    with pytest.raises(PayloadConflictError):
        submit_text_once(conn, key=_KEY, transcript="goodbye")

    assert len(_intent_rows(conn)) == 1
    assert _receipt_count(conn) == 1
    conn.close()


def test_a_different_client_instance_is_a_different_request(tmp_path: Path) -> None:
    """The key separates clients: the same request id from another instance is new."""
    conn = _log(tmp_path)
    first = submit_text_once(conn, key=_KEY, transcript="hello")
    other = SubmissionKey(
        authenticated_principal="inherent_v2",
        client_instance_id="I-other",
        request_id="req-1",
    )
    second = submit_text_once(conn, key=other, transcript="hello")

    assert second.input_event_uid != first.input_event_uid
    assert second.turn_id != first.turn_id
    assert len(_intent_rows(conn)) == 2
    conn.close()


@pytest.mark.parametrize("stage", ["after_input_append", "after_receipt_insert"])
def test_a_fault_before_commit_leaves_neither_the_event_nor_the_receipt(
    tmp_path: Path,
    stage: FailureStage,
) -> None:
    """The crash window D21 forbids: no receipt without its input event, either way."""
    conn = _log(tmp_path)

    def fail_at(reached: FailureStage) -> None:
        if reached == stage:
            msg = f"injected fault at {reached}"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="injected fault"):
        submit_text_once(conn, key=_KEY, transcript="hello", failure_injector=fail_at)

    assert _intent_rows(conn) == []
    assert _receipt_count(conn) == 0

    # The request id is still free: the retry that follows the crash succeeds.
    receipt = submit_text_once(conn, key=_KEY, transcript="hello")
    assert len(_intent_rows(conn)) == 1
    assert receipt.replayed is False
    conn.close()


def test_claim_input_once_reuses_the_receipt_turn_id(tmp_path: Path) -> None:
    """D21/D8: the pump's ``turn.started`` carries the id the inbox minted."""
    conn = _log(tmp_path)
    receipt = submit_text_once(conn, key=_KEY, transcript="hello")
    trigger = conn.execute(
        "SELECT event_uid FROM events WHERE type = 'surface.user_intent'",
    ).fetchone()
    assert str(trigger[0]) == receipt.input_event_uid

    event = get_event(conn, receipt.input_event_uid)
    assert event is not None
    outcome = claim_input_once(conn, trigger_event=event)

    assert outcome.turn_id == receipt.turn_id
    started = conn.execute(
        "SELECT payload_json FROM events WHERE type = 'turn.started'",
    ).fetchall()
    assert len(started) == 1
    assert json.loads(str(started[0][0]))["turn_id"] == receipt.turn_id
    conn.close()


# --- ASR lease -------------------------------------------------------------


def _emit_utterance(conn: sqlite3.Connection, turn_id: str, text: str = "你好") -> str:
    event = emit_event(
        conn,
        type="utterance.received",
        payload={"transcript": text, "turn_id": turn_id, "emotion": "HAPPY"},
        correlation={"turn_id": turn_id},
    )
    return event.event_uid


def test_asr_lease_claims_then_resolves_to_the_committed_utterance(tmp_path: Path) -> None:
    """The two-phase D21 shape: claim, run ASR outside the lock, then resolve."""
    conn = _log(tmp_path)
    claim = claim_asr_request(conn, key=_KEY, audio_sha256="a" * 64)
    assert isinstance(claim, AsrProcessingLease)

    uid = _emit_utterance(conn, claim.turn_id)
    receipt = resolve_asr_request(
        conn,
        key=_KEY,
        audio_sha256="a" * 64,
        turn_id=claim.turn_id,
        input_event_uid=uid,
        utterance_id="U1",
        text="你好",
        emotion="HAPPY",
    )

    assert receipt.turn_id == claim.turn_id
    assert receipt.input_event_uid == uid
    assert receipt.text == "你好"

    replay = claim_asr_request(conn, key=_KEY, audio_sha256="a" * 64)
    assert isinstance(replay, InputReceipt)
    assert replay.replayed is True
    assert replay.input_event_uid == uid
    assert replay.utterance_id == "U1"
    assert replay.emotion == "HAPPY"
    conn.close()


def test_asr_retry_with_a_different_audio_hash_is_rejected(tmp_path: Path) -> None:
    """A second upload under one request id must not become a second utterance."""
    conn = _log(tmp_path)
    claim_asr_request(conn, key=_KEY, audio_sha256="a" * 64)

    with pytest.raises(PayloadConflictError):
        claim_asr_request(conn, key=_KEY, audio_sha256="b" * 64)
    conn.close()


def test_asr_retry_under_a_live_lease_is_refused(tmp_path: Path) -> None:
    """Two concurrent identical uploads: the second waits rather than doubling ASR."""
    conn = _log(tmp_path)
    claim_asr_request(conn, key=_KEY, audio_sha256="a" * 64, now_ms=1_000)

    with pytest.raises(SubmissionInProgressError):
        claim_asr_request(conn, key=_KEY, audio_sha256="a" * 64, now_ms=2_000)
    conn.close()


def test_an_expired_lease_resumes_the_same_turn_id(tmp_path: Path) -> None:
    """A crashed run that committed nothing: the retry resumes, it does not re-mint."""
    conn = _log(tmp_path)
    first = claim_asr_request(conn, key=_KEY, audio_sha256="a" * 64, now_ms=1_000)
    assert isinstance(first, AsrProcessingLease)

    resumed = claim_asr_request(
        conn, key=_KEY, audio_sha256="a" * 64, now_ms=1_000 + 200_000,
    )
    assert isinstance(resumed, AsrProcessingLease)
    assert resumed.turn_id == first.turn_id
    conn.close()


def test_a_crashed_run_that_committed_its_utterance_resolves_by_lookup(tmp_path: Path) -> None:
    """D21 recovery: exactly one ``utterance.received`` per accepted request."""
    conn = _log(tmp_path)
    claim = claim_asr_request(conn, key=_KEY, audio_sha256="a" * 64, now_ms=1_000)
    assert isinstance(claim, AsrProcessingLease)
    uid = _emit_utterance(conn, claim.turn_id)

    # The receipt update was lost; the retry finds the durable row instead.
    recovered = claim_asr_request(
        conn, key=_KEY, audio_sha256="a" * 64, now_ms=1_000 + 200_000,
    )
    assert isinstance(recovered, InputReceipt)
    assert recovered.input_event_uid == uid
    assert recovered.turn_id == claim.turn_id
    rows = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'utterance.received'",
    ).fetchone()
    assert int(rows[0]) == 1
    conn.close()


def test_releasing_a_failed_lease_lets_the_retry_resume_at_once(tmp_path: Path) -> None:
    """An empty or busy ASR run must not lock the request id out for the TTL."""
    conn = _log(tmp_path)
    first = claim_asr_request(conn, key=_KEY, audio_sha256="a" * 64, now_ms=1_000)
    assert isinstance(first, AsrProcessingLease)
    release_asr_request(conn, key=_KEY)

    retried = claim_asr_request(conn, key=_KEY, audio_sha256="a" * 64, now_ms=1_500)
    assert isinstance(retried, AsrProcessingLease)
    assert retried.turn_id == first.turn_id
    conn.close()


def test_payload_hash_is_field_order_independent() -> None:
    """The digest binds the values, not the JSON the client happened to send."""
    assert payload_hash("text", {"transcript": "a", "channel": "b"}) == payload_hash(
        "text", {"channel": "b", "transcript": "a"},
    )
    assert payload_hash("text", {"transcript": "a"}) != payload_hash("text", {"transcript": "b"})
