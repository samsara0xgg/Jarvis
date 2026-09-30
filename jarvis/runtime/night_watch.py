"""ADR 0093: the agent sessions a night run watches, from every list the daemon can read.

Three lists, each answering on its own: the agent host's (the Agents window),
Claude Code's own (``claude agents --json``, only when the owner turned
reading on, ADR 0046) and the Codex board its hooks fill (ADR 0019). Each
row is folded into one shape: ``{id, agent, title, st, busy, since_ms,
what}``, where ``st`` is the host's (work, pack, wait, done, err) and
``busy`` means the session is working now: a turn running, compacting, or a
background task still going. A list that does not answer is None; a list
that is off, or Codex's board while it is empty, is left out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.surface.agent_host import host_sessions
from jarvis.surface.codex_sessions import settle_codex_sessions

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.surface.claude_sessions import ClaudeSessions
    from jarvis.surface.codex_sessions import CodexSession

Row = dict[str, Any]
_TITLE_CHARS: Final = 80
_STATES: Final = frozenset({"work", "pack", "wait", "done", "err"})
_CODEX_ST: Final = {"running": "work", "needs_input": "wait", "idle": "done", "finished": "done"}
_CLAUDE_ST: Final = {"working": "work", "needs_input": "wait", "done": "done"}


def _short(value: object) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= _TITLE_CHARS else f"{text[: _TITLE_CHARS - 1]}…"


def _ms(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) and value > 0 else None


def host_rows(sessions: list[dict[str, Any]]) -> list[Row]:
    """The host's ``Sess`` rows as the night sees them; archived and parked idle ones are not."""
    rows = []
    for sess in sessions:
        st = sess.get("st")
        if sess.get("archived") or st not in _STATES or not isinstance(sess.get("id"), str):
            continue
        tasks = sess.get("tasks")
        running = isinstance(tasks, list) and any(
            isinstance(task, dict) and task.get("st") == "run" for task in tasks
        )
        busy = st in {"work", "pack"} or bool(sess.get("bg")) or running
        if sess.get("parked") and not busy:
            continue
        rows.append({
            "id": sess["id"],
            "agent": str(sess.get("agent") or "claude"),
            "title": _short(sess.get("title")),
            "st": st,
            "busy": busy,
            "since_ms": _ms(sess.get("since") if st in {"work", "pack"} else sess.get("updated")),
            "what": _short(sess.get("bg") if st in {"done", "err"} else sess.get("now")),
        })
    return rows


def claude_rows(board: dict[str, Any]) -> list[Row] | None:
    """``claude agents --json`` rows; None when the read failed."""
    if board.get("error"):
        return None
    rows = []
    for row in board.get("sessions", []):
        st = "err" if row.get("error") else _CLAUDE_ST.get(str(row.get("phase")), "done")
        if st == "work" and row.get("compacting"):
            st = "pack"
        rows.append({
            "id": str(row.get("session_id")),
            "agent": "claude",
            "title": _short(row.get("title")),
            "st": st,
            "busy": st in {"work", "pack"},
            "since_ms": None if st == "work" else _ms(row.get("updated_ms")),
            "what": _short(row.get("activity")),
        })
    return rows


def codex_rows(board: dict[str, CodexSession]) -> list[Row]:
    """The Codex hook board's rows, settled on a copy: the loop owns the board itself."""
    copy = {key: dict(row) for key, row in list(board.items())}
    settle_codex_sessions(copy)
    return [
        {
            "id": str(row.get("session_id")),
            "agent": "codex",
            "title": _short(row.get("prompt") or row.get("cwd")),
            "st": _CODEX_ST.get(str(row.get("state")), "done"),
            "busy": row.get("state") == "running",
            "since_ms": _ms(row.get("since_ms")),
            "what": _short(row.get("detail")),
        }
        for row in copy.values()
    ]


class NightWatch:
    """One look at every list; the night run calls it off its lock, every few seconds."""

    def __init__(
        self,
        *,
        port: int,
        key: Callable[[], str],
        claude: ClaudeSessions | None,
        codex: dict[str, CodexSession],
    ) -> None:
        """Bind the host's port and key, Claude Code's board when reading is on, Codex's board."""
        self._port = port
        self._key = key
        self._claude = claude
        self._codex = codex

    def __call__(self) -> dict[str, list[Row] | None]:
        """``{startrail, claude?, codex?}``: each list's rows, or None when it did not answer."""
        sessions = host_sessions(self._port, self._key())
        found: dict[str, list[Row] | None] = {
            "startrail": None if sessions is None else host_rows(sessions),
        }
        if self._claude is not None:
            found["claude"] = claude_rows(self._claude.read())
        if self._codex:  # an empty board says nothing: Codex may simply not be hooked up
            found["codex"] = codex_rows(self._codex)
        return found


__all__ = ["NightWatch", "Row", "claude_rows", "codex_rows", "host_rows"]
