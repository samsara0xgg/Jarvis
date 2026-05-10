"""Helpers for structured tool result strings.

Jarvis tools historically returned plain text.  The LLM and regex fast path
need a small amount of structure to distinguish verified success from failure
without forcing every caller to pass Python objects around.
"""

from __future__ import annotations

import json
from typing import Any

SUCCESS = "success"
PARTIAL_SUCCESS = "partial_success"
FAILURE = "failure"
NOOP = "noop"
NEEDS_CLARIFICATION = "needs_clarification"

_SUCCESS_STATUSES = {SUCCESS, NOOP}
_FAILURE_STATUSES = {FAILURE, NEEDS_CLARIFICATION}

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
) -> str:
    """Return a JSON string with a stable status/message envelope."""
    payload: dict[str, Any] = {
        "status": status,
        "message": message,
    }
    if data is not None:
        payload["data"] = data
    if error_code:
        payload["error_code"] = error_code
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


def tool_message(raw: Any) -> str:
    """Extract the user-facing message from a tool result."""
    return parse_tool_result(raw)["message"]


def tool_succeeded(raw: Any) -> bool:
    """Return True only for complete, verified success-like statuses."""
    return parse_tool_result(raw)["status"] in _SUCCESS_STATUSES


def tool_failed(raw: Any) -> bool:
    """Return True for clear failure or clarification-required statuses."""
    status = parse_tool_result(raw)["status"]
    return status in _FAILURE_STATUSES


def _infer_status(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in _FAILURE_MARKERS):
        return FAILURE
    return SUCCESS
