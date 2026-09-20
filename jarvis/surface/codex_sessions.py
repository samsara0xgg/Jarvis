"""Allen's own Codex sessions, folded from Codex hook payloads (ADR 0019 step 4).

``scripts/codex_hook_log.py`` POSTs every hook payload to
``/inherent/codex-hook``; :func:`fold_codex_hook` turns that stream into one
row per session (``running`` / ``needs_input`` / ``finished``) that the
Resonance dashboard polls from ``/inherent/codex-sessions``.
"""

from __future__ import annotations

import json
from typing import Any

# ponytail: in-memory board, lost on daemon restart and capped at the newest
# sessions; fold it from the event log if history ever matters.
MAX_SESSIONS = 8
_DETAIL_CHARS = 160

CodexSession = dict[str, Any]


def _short(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= _DETAIL_CHARS else text[: _DETAIL_CHARS - 1] + "…"


def fold_codex_hook(
    board: dict[str, CodexSession], payload: dict[str, Any], *, now_ms: int
) -> None:
    """Apply one hook payload to ``board`` (session_id -> row), in place.

    SessionStart opens an ``idle`` row, UserPromptSubmit flips it to
    ``running``, PermissionRequest to ``needs_input`` (PostToolUse takes it
    back to ``running`` once the approved tool ran), Stop to ``finished``
    with the last assistant message, SessionEnd drops the row.
    """
    session_id = payload.get("session_id")
    name = payload.get("hook_event_name")
    if not isinstance(session_id, str) or not isinstance(name, str):
        return
    if name == "SessionEnd":
        board.pop(session_id, None)
        return
    row = board.setdefault(
        session_id,
        {
            "session_id": session_id,
            "state": "idle",
            "cwd": "",
            "model": "",
            "prompt": "",
            "detail": "",
            "last_message": "",
            "since_ms": now_ms,
        },
    )
    row["since_ms"] = now_ms
    for key in ("cwd", "model"):
        if isinstance(payload.get(key), str):
            row[key] = payload[key]
    if name == "UserPromptSubmit":
        row.update(
            state="running", prompt=_short(payload.get("prompt", "")), detail="", last_message=""
        )
    elif name == "PermissionRequest":
        row.update(
            state="needs_input",
            detail=_short(f"{payload.get('tool_name', '?')} {payload.get('tool_input', '')}"),
        )
    elif name == "PostToolUse":
        row.update(state="running", detail=_short(str(payload.get("tool_name", ""))))
    elif name == "Stop":
        row.update(
            state="finished",
            detail="",
            last_message=_short(payload.get("last_assistant_message") or ""),
        )
    while len(board) > MAX_SESSIONS:
        oldest = min(board.values(), key=lambda r: int(r["since_ms"]))
        board.pop(str(oldest["session_id"]))
