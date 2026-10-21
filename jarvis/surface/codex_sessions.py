"""Allen's own Codex sessions, folded from Codex hook payloads (ADR 0019 step 4).

``scripts/codex_hook_log.py`` POSTs every hook payload to
``/inherent/codex-hook``; :func:`fold_codex_hook` turns that stream into one
row per session (``running`` / ``needs_input`` / ``finished``) that the
Resonance dashboard polls from ``/inherent/codex-sessions``. The thread's own
rollout (the payload's ``transcript_path``) settles what hooks cannot say:
a turn that ended without a Stop, and an approval Codex's reviewer answers.

The path comes from the request body, so it is trusted no further than
:func:`rollout_path` lets it: a regular file under Codex's sessions
directory, read in bounded pieces by a reader that cannot block on it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.shared.lang import t

LOGGER = logging.getLogger(__name__)

# The surface retains pinned snapshots. Never evict active work for newer work.
RETENTION_MS = 24 * 60 * 60 * 1000
_DETAIL_CHARS = 160
# A turn's end is among the rollout's last lines; a turn's settings line may sit further back.
_TAIL_BYTES = 256 * 1024
# The most of one rollout ever read: the reviewer line is rewritten at every turn start, so
# one beyond this is a turn that wrote megabytes of output, not one worth reading whole.
_MAX_ROLLOUT_BYTES = 8 * 1024 * 1024
_TURN_EVENTS = frozenset({"task_started", "task_complete", "turn_aborted"})
_QUESTION_TOOL = "request_user_input"
_MARKS = (*_TURN_EVENTS, _QUESTION_TOOL, '"UserMessage"')
_REVIEWER = re.compile(r'"approvals_reviewer":"(\w+)"')
_HOOKS = frozenset({
    "SessionStart", "UserPromptSubmit", "PermissionRequest", "PostToolUse", "Stop", "SessionEnd",
})
_ACTIVE = frozenset({"running", "needs_input"})

CodexSession = dict[str, Any]


def codex_sessions_root() -> Path:
    """Where Codex keeps its rollouts: ``sessions`` under ``$CODEX_HOME`` or ``~/.codex``."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "sessions"


def rollout_path(raw: object) -> str | None:
    """``raw`` as the resolved path of a regular file under Codex's sessions, else ``None``.

    Only looks (``stat``), never opens: a FIFO or a device named here must not block the
    caller, and nothing outside Codex's own directory is anyone's rollout.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        path = Path(raw).resolve()
        if path.is_relative_to(codex_sessions_root().resolve()) and stat.S_ISREG(
            path.stat().st_mode,
        ):
            return str(path)
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def _rollout(path: str, *, tail: int) -> str:
    """The rollout's last ``tail`` bytes; empty when it is not a readable regular file.

    Opened without following a link and without waiting for a writer, and the descriptor
    (not the name) is checked, so a path swapped for a FIFO or a device after
    :func:`rollout_path` still cannot block this or run on without end.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return ""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return ""
        left = min(tail, info.st_size)
        os.lseek(fd, info.st_size - left, os.SEEK_SET)
        pieces: list[bytes] = []
        while left > 0 and (piece := os.read(fd, left)):
            pieces.append(piece)
            left -= len(piece)
    except OSError:
        return ""
    finally:
        os.close(fd)
    return b"".join(pieces).decode("utf-8", "replace")


def _auto_reviewed(path: str) -> bool:
    """Whether Codex's reviewer, not Allen, answers this thread's approval requests.

    Codex writes the thread settings at every turn start; the newest line wins.
    """
    for tail in (_TAIL_BYTES, _MAX_ROLLOUT_BYTES):
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


def _keep_rollout(row: CodexSession, raw: object) -> None:
    """Take the payload's ``transcript_path`` onto the row only when it is a rollout file."""
    if (rollout := rollout_path(raw)) is not None:
        row["transcript_path"] = rollout
    elif raw:
        # Not written yet, or not Codex's: the row is settled by its hooks alone.
        LOGGER.info("codex hook: transcript_path is no rollout file under CODEX_HOME; ignored")


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
    for key in ("cwd", "model"):
        if isinstance(payload.get(key), str):
            row[key] = payload[key]
    _keep_rollout(row, payload.get("transcript_path"))
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
