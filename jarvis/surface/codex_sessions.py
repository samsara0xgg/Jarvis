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
_QUESTION_TOOL = "request_user_input"
_MARKS = (*_TURN_EVENTS, _QUESTION_TOOL, '"UserMessage"')
_REVIEWER = re.compile(r'"approvals_reviewer":"(\w+)"')
_HOOKS = frozenset({
    "SessionStart", "UserPromptSubmit", "PermissionRequest", "PostToolUse", "Stop", "SessionEnd",
})
_ACTIVE = frozenset({"running", "needs_input"})

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


def _question(arguments: str) -> str:
    """The first question Codex put to Allen, with a count of any more."""
    questions = json.loads(arguments)["questions"]
    more = f" (+{len(questions) - 1})" if len(questions) > 1 else ""
    return f"{questions[0]['title']}{more}"


TurnEvent = tuple[str, int, dict[str, Any]]


def _read_rollout(path: str, question: str | None) -> tuple[TurnEvent | None, str | None]:
    """The newest turn start / end ``(type, at_ms, payload)`` and the question still open.

    Codex asks (``request_user_input_async``) and works on, so the call can scroll out
    of the tail before Allen answers: ``question`` carries the one open at the last read,
    and any message he sends in the thread (a reply or not) closes it.
    """
    turn = None
    # "\n" only: splitlines() also breaks at U+2028 and friends inside JSON strings.
    for line in _rollout(path, tail=_TAIL_BYTES).split("\n"):
        if not any(mark in line for mark in _MARKS):
            continue
        try:
            entry = json.loads(line)
            payload = entry["payload"]
            kind = (entry["type"], payload["type"])
            if kind[0] == "event_msg" and kind[1] in _TURN_EVENTS:
                at_ms = int(datetime.fromisoformat(entry["timestamp"]).timestamp() * 1000)
                turn = (kind[1], at_ms, payload)
            elif kind == ("response_item", "function_call"):
                if str(payload["name"]).startswith(_QUESTION_TOOL):
                    question = _question(payload["arguments"])
            elif kind == ("event_msg", "item_completed"):
                if payload["item"]["type"] == "UserMessage":
                    question = None
        except (ValueError, KeyError, TypeError, IndexError):
            continue
    return turn, question


def settle_codex_sessions(board: dict[str, CodexSession]) -> None:
    """Settle active rows from the rollout: a turn ended Stop or not, a question open.

    Codex fires no Stop for an interrupted turn, and none at all while it skips
    the Stop handler (it does after hooks.json is rewritten, until re-trusted);
    it fires nothing when it asks Allen a question.
    """
    for row in board.values():
        path = row.get("transcript_path")
        if row["state"] not in _ACTIVE or not isinstance(path, str):
            continue
        was = row.get("question")
        turn, row["question"] = _read_rollout(path, was)
        if was and not row["question"] and row["state"] == "needs_input":
            row.update(state="running", detail="")
        # An end older than the row's last hook belongs to the turn before it.
        if turn is not None and turn[1] >= int(row["since_ms"]):
            kind, at_ms, payload = turn
            if kind == "task_complete":
                row.update(
                    state="finished", detail="", since_ms=at_ms,
                    last_message=_short(payload.get("last_agent_message") or ""),
                )
            elif kind == "turn_aborted":
                row.update(state="idle", detail=t("codex.turn_stopped"), since_ms=at_ms)
        # An unanswered question outlasts the turn that asked it.
        if row["question"]:
            asks = t("codex.asks", question=row["question"])
            row.update(state="needs_input", detail=_short(asks))


def _hold_question(row: CodexSession, name: str, payload: dict[str, Any]) -> bool:
    """While a question waits on Allen, only his own message moves the row."""
    if not row.get("question") or name == "UserPromptSubmit":
        row["question"] = None
        return False
    if name == "Stop":
        row["last_message"] = _short(payload.get("last_assistant_message") or "")
    return True


def prune_codex_sessions(board: dict[str, CodexSession], *, now_ms: int) -> None:
    """Expire inactive rows; active turns are never evicted by list capacity."""
    for key, old in list(board.items()):
        inactive = old["state"] not in _ACTIVE
        if inactive and now_ms - int(old["since_ms"]) >= RETENTION_MS:
            board.pop(key)


def _end_session(board: dict[str, CodexSession], session_id: str, now_ms: int) -> None:
    row = board.get(session_id)
    if row is None:
        return
    row.update(since_ms=now_ms, question=None)
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
    approved tool ran), Stop to ``finished`` with the last assistant message;
    while a question waits on Allen only UserPromptSubmit moves the row.
    SessionEnd preserves the recent row.
    """
    session_id = payload.get("session_id")
    name = payload.get("hook_event_name")
    if not isinstance(session_id, str) or not isinstance(name, str) or name not in _HOOKS:
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
    for key in ("cwd", "model", "transcript_path"):
        if isinstance(payload.get(key), str):
            row[key] = payload[key]
    if _hold_question(row, name, payload):
        return
    row["since_ms"] = now_ms
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
