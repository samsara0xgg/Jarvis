"""Unit tests for cross-layer typed contracts in `jarvis.shared`."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from jarvis.shared import (
    ActionRequest,
    AuthorizationLease,
    CallerPrincipal,
    Claim,
    Event,
    Evidence,
    PromptContext,
)


def test_event_is_frozen():
    """Event is frozen — attribute set raises FrozenInstanceError."""
    evt = Event(
        event_uid="evt-1",
        type="task.created",
        schema_version=1,
        ts_epoch_ms=0,
        payload={"goal": "x"},
        source_event_id=None,
        correlation={"turn_id": "T1"},
    )
    with pytest.raises(FrozenInstanceError):
        evt.type = "task.updated"  # type: ignore[misc]


def test_claim_is_frozen():
    """Claim is frozen — attribute set raises FrozenInstanceError."""
    claim = Claim(
        claim_id="cl-1",
        type="Report",
        statement="codex reported complete",
        subject_ref="task_X",
        produced_by_event_id="evt-1",
        ts_epoch_ms=0,
    )
    with pytest.raises(FrozenInstanceError):
        claim.statement = "x"  # type: ignore[misc]


def test_evidence_is_frozen():
    """Evidence is frozen — attribute set raises FrozenInstanceError."""
    ev = Evidence(
        evidence_id="ev-1",
        claim_id="cl-1",
        level="reported",
        source_event_id="evt-1",
        payload={"artifact_path": "/tmp/x"},
        ts_epoch_ms=0,
    )
    with pytest.raises(FrozenInstanceError):
        ev.level = "verified"  # type: ignore[misc]


def test_action_request_is_frozen():
    """ActionRequest is frozen — attribute set raises FrozenInstanceError."""
    req = ActionRequest(
        action_id="A1",
        tool_name="spawn_worker",
        target_entity_ref="task_X",
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L2",
        arguments={"goal": "run codex"},
        authorization_lease=None,
        run_id="R1",
        turn_id="T1",
    )
    with pytest.raises(FrozenInstanceError):
        req.tool_name = "verify_diff"  # type: ignore[misc]


def test_caller_principal_members_exist():
    """Spec §3.5.2 lists 6 canonical caller principals."""
    assert CallerPrincipal.OBSERVER.value == "observer"
    assert CallerPrincipal.REGEX_ROUTER.value == "regex_router"
    assert CallerPrincipal.JARVIS_LLM.value == "jarvis_llm"
    assert CallerPrincipal.WORKER_AGENT.value == "worker_agent"
    assert CallerPrincipal.BACKGROUND_SUBSCRIBER.value == "background_subscriber"
    assert CallerPrincipal.SYSTEM_MAINTENANCE.value == "system_maintenance"


def test_prompt_context_holds_system_string():
    """ADR Q3 option (a): PromptContext carries only `system: str` on Day-1."""
    ctx = PromptContext(system="hi")
    assert ctx.system == "hi"
    with pytest.raises(FrozenInstanceError):
        ctx.system = "bye"  # type: ignore[misc]


def test_authorization_lease_typeddict_shape():
    """AuthorizationLease is a TypedDict — a literal dict satisfies the type.

    `__total__` is the standard introspection attribute exposed by every
    TypedDict; this also catches accidental conversion to a regular class.
    """
    assert hasattr(AuthorizationLease, "__total__")
    sample: AuthorizationLease = {
        "lease_id": "lease-1",
        "granted_at_ms": 0,
        "expires_at_ms": 1000,
        "scope": {"allowed_tools": ["patch"]},
    }
    # Runtime sanity: the literal is a plain dict; TypedDict is structural.
    assert sample["lease_id"] == "lease-1"
    assert sample["scope"]["allowed_tools"] == ["patch"]


def test_action_request_lease_field_accepts_lease_or_none():
    """Accept a lease dict on the optional `authorization_lease` field.

    Day-1 always passes None; Stage 2 readiness needs the field to typecheck
    against `AuthorizationLease` literals too.
    """
    lease: AuthorizationLease = {
        "lease_id": "lease-1",
        "granted_at_ms": 0,
        "expires_at_ms": 1000,
        "scope": {},
    }
    req = ActionRequest(
        action_id="A2",
        tool_name="patch",
        target_entity_ref=None,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L3",
        arguments={},
        authorization_lease=lease,
        run_id=None,
        turn_id=None,
    )
    assert req.authorization_lease is not None
    assert req.authorization_lease["lease_id"] == "lease-1"
