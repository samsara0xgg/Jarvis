"""Allen's own Codex sessions, folded from Codex hook payloads (ADR 0019 step 4).

``scripts/codex_hook_log.py`` POSTs every hook payload to
``/inherent/codex-hook``; :func:`fold_codex_hook` turns that stream into one
row per session (``running`` / ``needs_input`` / ``finished``) that the
Resonance dashboard polls from ``/inherent/codex-sessions``. The thread's own
rollout (the payload's ``transcript_path``) settles what hooks cannot say:
a turn that ended without a Stop, and an approval Codex's reviewer answers.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.shared.lang import t

# The surface retains pinned snapshots. Never evict active work for newer work.
RETENTION_MS = 24 * 60 * 60 * 1000
_DETAIL_CHARS = 160
# A turn's end is among the rollout's last lines; a turn's settings line may sit further back.
_TAIL_BYTES = 256 * 1024
_TURN_EVENTS = frozenset({"task_started", "task_complete", "turn_aborted"})
_REVIEWER = re.compile(r'"approvals_reviewer":"(\w+)"')

CodexSession = dict[str, Any]


def _rollout(path: str, *, tail: int | None) -> str:
    """The rollout's last ``tail`` bytes, or all of it; empty when it cannot be read."""
    try:
        with Path(path).open("rb") as fh:
            if tail is not None:
                fh.seek(max(0, fh.seek(0, os.SEEK_END) - tail))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _auto_reviewed(path: str) -> bool:
    """Whether Codex's reviewer, not Allen, answers this thread's approval requests.

    Codex writes the thread settings at every turn start; the newest line wins.
    """
    for tail in (_TAIL_BYTES, None):
        found: list[str] = _REVIEWER.findall(_rollout(path, tail=tail))
        if found:
            return found[-1] != "user"
    return False


def _asks_allen(row: CodexSession, turn_id: object) -> bool:
    """Whether a PermissionRequest waits on Allen; auto-review answers in seconds, turn goes on.

    Read once per turn, since a long turn asks many times.
    """
    if "auto_review" not in row or row.get("reviewed_turn") != turn_id:
        row["reviewed_turn"] = turn_id
        path = row.get("transcript_path")
        row["auto_review"] = isinstance(path, str) and _auto_reviewed(path)
    return not row["auto_review"]


def _last_turn_event(path: str) -> tuple[str, int, dict[str, Any]] | None:
    """The newest turn start / completion / abort in the rollout: (type, at_ms, payload)."""
    for line in reversed(_rollout(path, tail=_TAIL_BYTES).splitlines()):
        if not any(name in line for name in _TURN_EVENTS):
            continue
        try:
            entry = json.loads(line)
            payload = entry["payload"]
            if entry["type"] != "event_msg" or payload["type"] not in _TURN_EVENTS:
                continue
            at_ms = int(datetime.fromisoformat(entry["timestamp"]).timestamp() * 1000)
        except (ValueError, KeyError, TypeError):
            continue
        return payload["type"], at_ms, payload
    return None


def settle_codex_sessions(board: dict[str, CodexSession]) -> None:
    """End active rows whose turn the rollout shows over, Stop or not.

    Codex fires no Stop for an interrupted turn, and none at all while it skips
    the Stop handler (it does after hooks.json is rewritten, until re-trusted).
    """
    for row in board.values():
        if row["state"] not in {"running", "needs_input"} or not row.get("transcript_path"):
            continue
        event = _last_turn_event(row["transcript_path"])
        # An end older than the row's last hook belongs to the turn before it.
        if event is None or event[1] < int(row["since_ms"]):
            continue
        kind, at_ms, payload = event
        if kind == "task_complete":
            row.update(
                state="finished", detail="", since_ms=at_ms,
                last_message=_short(payload.get("last_agent_message") or ""),
            )
        elif kind == "turn_aborted":
            row.update(state="idle", detail=t("codex.turn_stopped"), since_ms=at_ms)


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
        row.update(state="idle", detail=t("codex.session_ended"))


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
    ``running``, PermissionRequest to ``needs_input`` unless the thread runs
    under auto-review (PostToolUse takes it back to ``running`` once the
    approved tool ran), Stop to ``finished``
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
    for key in ("cwd", "model", "transcript_path"):
        if isinstance(payload.get(key), str):
            row[key] = payload[key]
    if name == "UserPromptSubmit":
        row.update(
            state="running", prompt=_prompt(payload.get("prompt", "")), detail="", last_message="",
            turn_started_ms=now_ms,
        )
    elif name == "PermissionRequest":
        row.update(
            state="needs_input" if _asks_allen(row, payload.get("turn_id")) else "running",
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
