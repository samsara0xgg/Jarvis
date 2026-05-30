# mypy: ignore-errors
"""JSON serializers for tools_v2 handler returns.

Vendored from /Users/alllllenshi/Projects/jarvis-legacy/tools_v2/helpers.py on 2026-05-18.
License/origin: Allen-owned (jarvis-legacy).
DO NOT EDIT IN-PLACE — upstream sync only.

Mirrors Hermes Agent's helpers (tools/registry.py:537-563). Every handler
returns a JSON string in one of two shapes:

* Error:   ``{"error": "<reason>", ...optional extras}``
* Success: arbitrary dict ``{...}`` — no schema_version, no envelope, no
  status field. Hermes-style ad-hoc keys per tool.

Why two functions instead of one: ``tool_error`` is a clear visual marker
in tool handlers, making error paths easy to spot in code review.
"""

from __future__ import annotations

import json
from typing import Any


def tool_error(message: Any, **extra: Any) -> str:  # noqa: ANN401
    """Serialize a tool failure as JSON.

    Args:
        message: Human-readable error string (coerced via ``str``).
        **extra: Optional additional keys to merge into the payload, e.g.
            ``code=404`` or ``partial_stdout="..."``.

    Returns:
        JSON string of the form ``{"error": "<msg>", ...extra}`` with
        ``ensure_ascii=False`` so CJK characters survive intact.
    """
    payload: dict[str, Any] = {"error": str(message)}
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def tool_result(data: dict[str, Any] | None = None, **kwargs: Any) -> str:  # noqa: ANN401
    """Serialize a tool success result as JSON.

    Accepts a ``data`` dict positional argument OR keyword arguments — not
    both meaningfully (kwargs are ignored if ``data`` is provided).

    Args:
        data: Pre-built dict payload to serialize verbatim.
        **kwargs: Fallback shape — when ``data`` is None, the kwargs become
            the payload.

    Returns:
        JSON string with ``ensure_ascii=False``.

    Examples:
        >>> tool_result(success=True, count=42)
        '{"success": true, "count": 42}'
        >>> tool_result({"key": "value"})
        '{"key": "value"}'
    """
    payload = data if data is not None else kwargs
    return json.dumps(payload, ensure_ascii=False)
