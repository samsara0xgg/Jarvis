"""Allen's own Codex sessions, folded from Codex hook payloads (ADR 0019 step 4).

``scripts/codex_hook_log.py`` POSTs every hook payload to
``/inherent/codex-hook``; :func:`fold_codex_hook` turns that stream into one
row per session (``running`` / ``needs_input`` / ``finished``) that the
Resonance dashboard polls from ``/inherent/codex-sessions``.
"""

from __future__ import annotations

import json
from typing import Any

# The surface retains pinned snapshots. Never evict active work for newer work.
RETENTION_MS = 24 * 60 * 60 * 1000
_DETAIL_CHARS = 160

CodexSession = dict[str, Any]


def prune_codex_sessions(board: dict[str, CodexSession], *, now_ms: int) -> None:
    """Expire inactive rows; active turns are never evicted by list capacity."""
    for key, old in list(board.items()):
        inactive = old["state"] not in {"running", "needs_input"}
        if inactive and now_ms - int(old["since_ms"]) >= RETENTION_MS:
            board.pop(key)


def _end_session(board: dict[str, CodexSession], session_id: str, now_ms: int) -> None:
    row = board.get(session_id)
    if row is None:
        return
    row["since_ms"] = now_ms
    if row["state"] != "finished":
        row.update(state="idle", detail="会话已结束")


def _short(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= _DETAIL_CHARS else text[: _DETAIL_CHARS - 1] + "…"


def _prompt(value: object) -> str:
    """Prefer the user request to desktop-injected ambient context."""
    text = value if isinstance(value, str) else ""
    marker = "## My request:"
    if marker in text:
        text = text.rsplit(marker, 1)[-1]
    return _short(text.strip())


def fold_codex_hook(
    board: dict[str, CodexSession], payload: dict[str, Any], *, now_ms: int
) -> None:
    """Apply one hook payload to ``board`` (session_id -> row), in place.

    SessionStart opens an ``idle`` row, UserPromptSubmit flips it to
    ``running``, PermissionRequest to ``needs_input`` (PostToolUse takes it
    back to ``running`` once the approved tool ran), Stop to ``finished``
    with the last assistant message. SessionEnd preserves the recent row.
    """
    session_id = payload.get("session_id")
    name = payload.get("hook_event_name")
    if not isinstance(session_id, str) or not isinstance(name, str):
        return
    if name not in {
        "SessionStart", "UserPromptSubmit", "PermissionRequest",
        "PostToolUse", "Stop", "SessionEnd",
    }:
        return
    prune_codex_sessions(board, now_ms=now_ms)
    if name == "SessionEnd":
        _end_session(board, session_id, now_ms)
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
            "turn_started_ms": now_ms,
        },
    )
    row["since_ms"] = now_ms
    for key in ("cwd", "model"):
        if isinstance(payload.get(key), str):
            row[key] = payload[key]
    if name == "UserPromptSubmit":
        row.update(
            state="running", prompt=_prompt(payload.get("prompt", "")), detail="", last_message="",
            turn_started_ms=now_ms,
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
