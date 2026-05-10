"""Helpers for structured tool result strings.

Jarvis tools historically returned plain text.  The LLM and regex fast path
need a small amount of structure to distinguish verified success from failure
without forcing every caller to pass Python objects around.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

SCHEMA_VERSION = "jarvis.tool_result.v1"

SUCCESS = "success"
PARTIAL_SUCCESS = "partial_success"
FAILURE = "failure"
NOOP = "noop"
NEEDS_CLARIFICATION = "needs_clarification"

OBSERVED = "observed"
CHANGED = "changed"
CREATED = "created"
UPDATED = "updated"
DELETED = "deleted"
DELIVERED = "delivered"
QUEUED = "queued"
INTERRUPT_REQUESTED = "interrupt_requested"
INTERRUPTED = "interrupted"
NO_CHANGE = "no_change"
LEGACY_UNVERIFIED = "legacy_unverified"
OUTCOME_FAILED = "failed"

_SUCCESS_STATUSES = {SUCCESS, NOOP}
_FAILURE_STATUSES = {FAILURE, NEEDS_CLARIFICATION}
_HIGH_RISK_LEVELS = {"high", "critical"}

_FAILURE_MARKERS = (
    "failed",
    "failure",
    "error:",
    "tool execution error",
    "unknown tool",
    "not found",
    "permission denied",
    "blocked:",
    "失败",
    "错误",
    "拒绝",
    "无法",
    "不能",
    "未找到",
    "没有找到",
    "读不到",
    "超时",
)


def make_tool_result(
    status: str,
    message: str,
    *,
    data: Any | None = None,
    error_code: str | None = None,
    skill_name: str | None = None,
    call_id: str | None = None,
    caller: str | None = None,
    outcome: dict[str, Any] | None = None,
    outcome_type: str | None = None,
    verified: bool | None = None,
    verification_source: str | None = None,
    claim_policy: dict[str, Any] | None = None,
) -> str:
    """Return a JSON string with a stable Jarvis tool-result envelope."""
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "message": message,
    }
    if skill_name:
        payload["skill_name"] = skill_name
    if call_id:
        payload["call_id"] = call_id
    if caller:
        payload["caller"] = caller
    if data is not None:
        payload["data"] = data
    if error_code:
        payload["error_code"] = error_code
    if outcome is not None:
        payload["outcome"] = outcome
    elif outcome_type:
        payload["outcome"] = {
            "type": outcome_type,
            "verified": bool(verified),
            "verification_source": verification_source or "none",
        }
    if claim_policy is not None:
        payload["claim_policy"] = claim_policy
    return json.dumps(payload, ensure_ascii=False)


def parse_tool_result(raw: Any) -> dict[str, Any]:
    """Parse a Jarvis tool result, accepting legacy plain-text results."""
    if isinstance(raw, dict):
        status = str(raw.get("status") or "").strip() or _infer_status(
            str(raw.get("message") or raw)
        )
        message = str(raw.get("message") or raw)
        return {**raw, "status": status, "message": message}

    text = "" if raw is None else str(raw)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {"status": _infer_status(text), "message": text}

    if isinstance(parsed, dict) and "status" in parsed:
        message = str(parsed.get("message") or text)
        return {**parsed, "status": str(parsed.get("status")), "message": message}
    return {"status": _infer_status(text), "message": text, "data": parsed}


def normalize_tool_result(
    raw: Any,
    *,
    skill_name: str,
    caller: str = "unknown",
    call_id: str | None = None,
    read_only: bool = True,
    destructive: bool = False,
    risk_level: str = "low",
    action_type: str | None = None,
) -> str:
    """Normalize a tool result into ``jarvis.tool_result.v1``.

    The normalizer is intentionally compatible with old tools. Structured
    ``status/message`` envelopes are upgraded with evidence fields. Legacy
    side-effect plain text is treated conservatively unless the executor type
    itself provides a reliable acknowledgement, such as ``file_write`` or
    ``zellij_send`` after a successful subprocess call.
    """
    payload = normalize_tool_result_payload(
        raw,
        skill_name=skill_name,
        caller=caller,
        call_id=call_id,
        read_only=read_only,
        destructive=destructive,
        risk_level=risk_level,
        action_type=action_type,
    )
    return json.dumps(payload, ensure_ascii=False)


def normalize_tool_result_payload(
    raw: Any,
    *,
    skill_name: str,
    caller: str = "unknown",
    call_id: str | None = None,
    read_only: bool = True,
    destructive: bool = False,
    risk_level: str = "low",
    action_type: str | None = None,
) -> dict[str, Any]:
    """Return a normalized tool-result payload as a dict."""
    call_id = call_id or _new_call_id()
    risk = str(risk_level or "low").strip().lower()
    is_structured = _is_structured_result(raw)
    parsed = parse_tool_result(raw)
    status = str(parsed.get("status") or _infer_status(str(raw)))

    if is_structured:
        message = str(parsed.get("message") or "")
        payload = {
            **parsed,
            "schema_version": parsed.get("schema_version") or SCHEMA_VERSION,
            "skill_name": parsed.get("skill_name") or skill_name,
            "call_id": parsed.get("call_id") or call_id,
            "caller": parsed.get("caller") or caller,
            "status": status,
            "message": message,
        }
        if "outcome" not in payload:
            payload["outcome"] = _default_outcome(
                skill_name,
                status,
                read_only=read_only,
                destructive=destructive,
                action_type=action_type,
            )
        if "claim_policy" not in payload:
            payload["claim_policy"] = _default_claim_policy(payload["outcome"], status)
        return payload

    text = "" if raw is None else str(raw)
    has_side_effects = destructive or not read_only
    inferred_failure = status in _FAILURE_STATUSES

    if not has_side_effects:
        outcome = {
            "type": OBSERVED if not inferred_failure else OUTCOME_FAILED,
            "verified": False,
            "verification_source": "legacy_plain_text",
        }
        return _normalized_payload(
            skill_name=skill_name,
            caller=caller,
            call_id=call_id,
            status=(FAILURE if inferred_failure else SUCCESS),
            message=("工具执行失败。" if inferred_failure else text),
            outcome=outcome,
            data={"legacy_raw_result": text},
            error_code=("legacy_tool_failure" if inferred_failure else None),
        )

    legacy_ack = _known_legacy_ack(skill_name, action_type)
    if legacy_ack and not inferred_failure:
        outcome_type, verification_source = legacy_ack
        outcome = {
            "type": outcome_type,
            "verified": True,
            "verification_source": verification_source,
        }
        return _normalized_payload(
            skill_name=skill_name,
            caller=caller,
            call_id=call_id,
            status=SUCCESS,
            message=text,
            outcome=outcome,
            data={"legacy_raw_result": text},
        )

    if inferred_failure:
        return _normalized_payload(
            skill_name=skill_name,
            caller=caller,
            call_id=call_id,
            status=FAILURE,
            message="工具执行失败。",
            outcome={
                "type": OUTCOME_FAILED,
                "verified": False,
                "verification_source": "legacy_plain_text",
            },
            data={"legacy_raw_result": text},
            error_code="legacy_tool_failure",
        )

    if risk in _HIGH_RISK_LEVELS:
        return _normalized_payload(
            skill_name=skill_name,
            caller=caller,
            call_id=call_id,
            status=FAILURE,
            message="这个工具返回了旧格式结果，无法验证高风险操作是否安全完成。",
            outcome={
                "type": OUTCOME_FAILED,
                "verified": False,
                "verification_source": "legacy_plain_text",
            },
            data={"legacy_raw_result": text},
            error_code="untyped_side_effect_result",
        )

    return _normalized_payload(
        skill_name=skill_name,
        caller=caller,
        call_id=call_id,
        status=SUCCESS,
        message="工具返回了旧格式结果，但没有可验证完成证据。",
        outcome={
            "type": LEGACY_UNVERIFIED,
            "verified": False,
            "verification_source": "legacy_plain_text",
        },
        data={"legacy_raw_result": text},
    )


def tool_message(raw: Any) -> str:
    """Extract the user-facing message from a tool result."""
    return parse_tool_result(raw)["message"]


def tool_succeeded(raw: Any) -> bool:
    """Return True only for complete, verified success-like statuses."""
    parsed = parse_tool_result(raw)
    if parsed["status"] not in _SUCCESS_STATUSES:
        return False
    outcome = parsed.get("outcome")
    if isinstance(outcome, dict):
        if outcome.get("type") in {LEGACY_UNVERIFIED, OUTCOME_FAILED}:
            return False
        if outcome.get("verified") is False and outcome.get("type") != OBSERVED:
            return False
    return True


def tool_failed(raw: Any) -> bool:
    """Return True for clear failure or clarification-required statuses."""
    status = parse_tool_result(raw)["status"]
    return status in _FAILURE_STATUSES


def _infer_status(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in _FAILURE_MARKERS):
        return FAILURE
    return SUCCESS


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"


def _is_structured_result(raw: Any) -> bool:
    if isinstance(raw, dict):
        return "status" in raw
    if not isinstance(raw, str):
        return False
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and "status" in parsed


def _normalized_payload(
    *,
    skill_name: str,
    caller: str,
    call_id: str,
    status: str,
    message: str,
    outcome: dict[str, Any],
    data: Any | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "skill_name": skill_name,
        "call_id": call_id,
        "caller": caller,
        "status": status,
        "message": message,
        "outcome": outcome,
        "claim_policy": _default_claim_policy(outcome, status),
    }
    if data is not None:
        payload["data"] = data
    if error_code:
        payload["error_code"] = error_code
    return payload


def _default_outcome(
    skill_name: str,
    status: str,
    *,
    read_only: bool,
    destructive: bool,
    action_type: str | None,
) -> dict[str, Any]:
    if status == NOOP:
        return {
            "type": NO_CHANGE,
            "verified": True,
            "verification_source": "read_result",
        }
    if status in _FAILURE_STATUSES:
        return {
            "type": OUTCOME_FAILED,
            "verified": False,
            "verification_source": "none",
        }
    if status == PARTIAL_SUCCESS:
        return {
            "type": CHANGED if (destructive or not read_only) else OBSERVED,
            "verified": False,
            "verification_source": "partial_result",
        }

    outcome_type = _infer_success_outcome_type(skill_name, read_only, action_type)
    return {
        "type": outcome_type,
        "verified": True,
        "verification_source": _infer_verification_source(
            skill_name, outcome_type, action_type
        ),
    }


def _infer_success_outcome_type(
    skill_name: str,
    read_only: bool,
    action_type: str | None,
) -> str:
    name = skill_name.lower()
    if read_only or name.startswith(("get_", "list_")):
        return OBSERVED
    if name == "cc_interrupt":
        return INTERRUPT_REQUESTED
    if name in {"cc_tell", "cc_slash"} or action_type == "zellij_send":
        return DELIVERED
    if name.startswith(("create_", "add_")) or name in {
        "set_timer",
        "obsidian_add_to_inbox",
    }:
        return CREATED
    if name.startswith("complete_"):
        return UPDATED
    if name.startswith("delete_"):
        return DELETED
    return CHANGED


def _infer_verification_source(
    skill_name: str,
    outcome_type: str,
    action_type: str | None,
) -> str:
    name = skill_name.lower()
    if action_type == "file_write":
        return "file_write_ack"
    if name == "cc_interrupt":
        return "signal_ack"
    if outcome_type == DELIVERED:
        return "delivery_ack"
    if name == "smart_home_control":
        return "controller_ack"
    if outcome_type == OBSERVED:
        return "read_result"
    if outcome_type in {CREATED, UPDATED, DELETED}:
        return "store_ack"
    return "tool_ack"


def _known_legacy_ack(
    skill_name: str,
    action_type: str | None,
) -> tuple[str, str] | None:
    if action_type == "file_write":
        return CREATED, "file_write_ack"
    if action_type == "http_get":
        return OBSERVED, "read_result"
    if action_type == "zellij_send":
        if skill_name == "cc_interrupt":
            return INTERRUPT_REQUESTED, "signal_ack"
        return DELIVERED, "delivery_ack"
    return None


def _default_claim_policy(outcome: dict[str, Any], status: str) -> dict[str, Any]:
    outcome_type = str(outcome.get("type") or "")
    verified = outcome.get("verified") is True

    if status in _FAILURE_STATUSES or outcome_type == OUTCOME_FAILED:
        return {
            "allowed_claims": ["tool_failed_contract_validation"],
            "forbidden_claims": [
                "action_completed",
                "state_changed",
                "created",
                "updated",
                "deleted",
                "typed",
                "sent",
                "task_completed",
            ],
        }
    if outcome_type == LEGACY_UNVERIFIED or not verified:
        return {
            "allowed_claims": ["tool_returned_unverified_result"],
            "forbidden_claims": [
                "action_completed",
                "state_changed",
                "created",
                "updated",
                "deleted",
                "typed",
                "sent",
                "task_completed",
            ],
        }
    if outcome_type == DELIVERED:
        return {
            "allowed_claims": ["message_delivered"],
            "forbidden_claims": ["task_completed", "code_modified", "bug_fixed"],
        }
    if outcome_type == INTERRUPT_REQUESTED:
        return {
            "allowed_claims": ["interrupt_requested"],
            "forbidden_claims": ["agent_stopped", "task_cancelled"],
        }
    return {
        "allowed_claims": [outcome_type],
        "forbidden_claims": [],
    }
