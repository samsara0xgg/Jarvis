"""A confirmation accepted in the log, and the dispatch candidate built from it."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast

from jarvis.shared import ActionRequest, AuthorizationLease, CallerPrincipal, Event
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping


def seed_accepted_confirmation(
    conn: sqlite3.Connection,
    *,
    suffix: str,
) -> tuple[Event, Mapping[str, object]]:
    now_ms = 2_000_000
    content = "safe"
    content_bytes = content.encode("utf-8")
    target = f"file:/tmp/{suffix}.txt"
    snapshot: dict[str, object] = {
        "tool_name": "open_path",
        "caller": "jarvis_llm",
        "canonical_target": f"/tmp/{suffix}.txt",
        "target_entity_ref": target,
        "risk_level": "L3",
        "args_meta": {
            "target": target,
            "mode": "overwrite",
            "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
            "content_bytes": len(content_bytes),
            "content_artifact": f"/tmp/{suffix}.pending",
        },
    }
    requested = emit_event(
        conn,
        type="confirmation.requested",
        payload={
            "confirmation_id": f"CONF-{suffix}",
            "action_snapshot": snapshot,
            "template_line": "approved",
            "expires_at_ms": now_ms + 60_000,
        },
    )
    accepted = emit_event(
        conn,
        type="confirmation.accepted",
        payload={
            "confirmation_id": f"CONF-{suffix}",
            "utterance_raw": "可以",
            "grammar_rule_id": "yes",
        },
        source_event_id=requested.event_uid,
    )
    return accepted, snapshot


def confirmation_candidate(
    accepted_event_uid: str,
    snapshot: Mapping[str, object],
    *,
    random_suffix: str,
) -> tuple[ActionRequest, AuthorizationLease]:
    target = cast("str", snapshot["target_entity_ref"])
    request = ActionRequest(
        action_id=f"random-action-{random_suffix}",
        tool_name="open_path",
        target_entity_ref=target,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L3",
        arguments={"target": target, "mode": "overwrite", "content": "safe"},
        authorization_lease=None,
        run_id=None,
        turn_id="T-confirm",
    )
    lease: AuthorizationLease = {
        "lease_id": f"random-lease-{random_suffix}",
        "granted_by": "allen",
        "granted_to": CallerPrincipal.JARVIS_LLM,
        "allowed_tools": frozenset({"open_path"}),
        "allowed_targets": frozenset({target}),
        "expires_at_ms": 2_050_000,
        "max_uses": 1,
        "reason": "approved",
        "source_confirmation_event_id": accepted_event_uid,
    }
    return request, lease
