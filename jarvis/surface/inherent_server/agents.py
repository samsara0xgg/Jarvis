"""The Agents page: Codex and Claude Code sessions, their marks and hooks.

- ADR 0019: Allen's own Codex sessions, fed by ``scripts/codex_hook_log.py``
- ADR 0046, 0049, 0070: his Claude Code sessions, the prompts Jarvis holds for them, a reply
- ADR 0069: the marks on sessions (unread, parked, archived); ADR 0125, 0128: a finished turn's end
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any, Final

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from jarvis.state.agent_marks import AgentMarks
from jarvis.surface.claude_hooks import ClaudeHooks
from jarvis.surface.claude_sessions import ClaudeSessions
from jarvis.surface.codex_sessions import (
    fold_codex_hook,
    prune_codex_sessions,
    settle_codex_sessions,
)
from jarvis.surface.inherent_server.waiting import register_waiting_route

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


# ADR 0070 / 0069: the longest line the island types into a session, the longest mark key.
_REPLY_CHARS: Final = 4000
_SESSION_ID_CHARS: Final = 128


class TurnEndRequest(BaseModel):
    """Body of ``POST /inherent/agents/turn-end`` (ADR 0125): a finished turn's last message."""

    session_id: str = Field(min_length=1, max_length=_SESSION_ID_CHARS)
    text: str = Field(max_length=100_000)


class TurnEndAnswered(BaseModel):
    """Body of ``POST /inherent/agents/turn-end/answered`` (ADR 0128): the session he wrote in."""

    session_id: str = Field(min_length=1, max_length=_SESSION_ID_CHARS)


def register_codex_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0019: the Codex hook, and the board it fills."""
    # ADR 0019 step 4: Allen's own Codex sessions, fed by scripts/codex_hook_log.py.
    codex_board = deps.codex_board
    codex_lock = threading.Lock()  # one writer at a time: both routes read rollouts off the loop

    @app.post("/inherent/codex-hook", status_code=200)
    async def codex_hook(payload: dict[str, Any]) -> dict[str, bool]:
        """Fold one Codex hook payload into the session board; never a decision."""

        def fold(now_ms: int) -> None:
            with codex_lock:
                fold_codex_hook(codex_board, payload, now_ms=now_ms)

        await asyncio.to_thread(fold, int(time.time() * 1000))
        return {"ok": True}

    @app.get("/inherent/codex-sessions")
    async def codex_sessions() -> dict[str, Any]:
        """Newest-first rows for the Resonance Codex card."""

        def read(now_ms: int) -> list[dict[str, Any]]:
            with codex_lock:
                prune_codex_sessions(codex_board, now_ms=now_ms)
                settle_codex_sessions(codex_board)
                rows = sorted(codex_board.values(), key=lambda r: int(r["since_ms"]), reverse=True)
                return [dict(row) for row in rows]  # copies: the next hook may edit a row

        return {"sessions": await asyncio.to_thread(read, int(time.time() * 1000))}


def register_claude_routes(app: FastAPI, deps: InherentDeps) -> None:  # noqa: C901, PLR0915 — one closed route table.
    """ADR 0046, 0049, 0069, 0070, 0125: the Claude Code board, its hooks, marks and replies."""
    # ADR 0046: Allen's own Claude Code sessions, read from Claude Code's own state;
    # ADR 0049: with the prompts Jarvis holds for them and their compacting / stopped marks.
    # ADR 0170: on a brain (``terminals``) the session files are the terminal's, so the board, a
    # conversation and a reply are asked of it, and the brain's own files are never read.
    claude_board = ClaudeSessions(
        deps.turn_end_peek, deps.turn_end_answered,
        None if deps.terminals is None else deps.terminals.call,
    )
    claude_hooks = ClaudeHooks(
        lambda: deps.controls.quiet if deps.controls is not None else "off",
        deps.claude_request,
        deps.claude_prompt_log,
    )

    async def read_claude_board() -> dict[str, Any]:
        """The board with its held prompts; reading it is what tells a hook someone listens."""
        if not deps.claude_sessions_read:
            return claude_hooks.merge({"sessions": []})
        return claude_hooks.merge(await asyncio.to_thread(claude_board.read))

    @app.get("/inherent/claude-sessions")
    async def claude_sessions() -> dict[str, Any]:
        """Newest-first Claude Code session rows for the Resonance Agents page."""
        return await read_claude_board()

    register_waiting_route(app, deps, read_claude_board)

    @app.get("/inherent/claude-sessions/{session_id}/conversation")
    async def claude_conversation(session_id: str) -> dict[str, Any]:
        """ADR 0070: what Allen said and each turn's final answer, for the island's page."""
        if not deps.claude_sessions_read:
            raise HTTPException(status_code=404, detail="reading Claude Code's files is off")
        try:
            return await asyncio.to_thread(claude_board.conversation, session_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="no such session") from None
        except OSError as exc:  # the terminal is not there, or reads no Claude Code files
            raise HTTPException(status_code=502, detail=str(exc)[:200]) from None

    @app.post("/inherent/claude-sessions/{session_id}/reply", status_code=200)
    async def claude_reply(session_id: str, body: dict[str, Any]) -> dict[str, bool]:
        """ADR 0070: type Allen's line into an idle background session; 409 if it cannot."""
        text = body.get("text")
        if not deps.claude_sessions_read or not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="a reply needs text")
        try:
            await asyncio.to_thread(claude_board.reply, session_id, text[:_REPLY_CHARS])
        except LookupError:
            raise HTTPException(status_code=409, detail="it cannot take a reply now") from None
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
        return {"ok": True}

    @app.post("/inherent/agents/turn-end", status_code=200)
    async def turn_end(req: TurnEndRequest) -> dict[str, bool | None]:
        """ADR 0125: does the end of a finished agent turn ask Allen something; null = unknown."""
        if deps.turn_end_asks is None:
            return {"asks": None}
        return {"asks": await deps.turn_end_asks(req.session_id, req.text)}

    @app.post("/inherent/agents/turn-end/answered", status_code=200)
    async def turn_end_answered(req: TurnEndAnswered) -> dict[str, bool]:
        """ADR 0128: Allen just sent a message in a Startrail session; fire and forget."""
        if deps.turn_end_answered is not None:
            deps.turn_end_answered(req.session_id, [time.time()])
        return {"ok": True}

    if deps.agent_marks_path is not None:
        marks = AgentMarks(deps.agent_marks_path)

        @app.get("/inherent/agent-marks")
        async def agent_marks() -> dict[str, Any]:
            """ADR 0069: unread, parked and archived, per session, for every surface."""
            return marks.read()

        @app.post("/inherent/agent-marks/{session_id}", status_code=200)
        async def agent_mark(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
            """ADR 0069: ``seen`` / ``unread`` / ``park`` / ``archive`` on one session."""
            if not 0 < len(session_id) <= _SESSION_ID_CHARS:
                raise HTTPException(status_code=400, detail="bad session id")
            return await asyncio.to_thread(marks.update, session_id, body)

    @app.post("/inherent/claude-hook", status_code=200)
    async def claude_hook(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        """ADR 0049: the body is the hook's stdout; ``{}`` is no decision."""
        if payload.get("hook_event_name") == "PermissionRequest":
            return await claude_hooks.permission(payload, request.is_disconnected)
        claude_hooks.event(payload)
        return {}

    @app.post("/inherent/claude-requests/{request_id}", status_code=200)
    async def claude_request_answer(request_id: str, body: dict[str, Any]) -> dict[str, bool]:
        """ADR 0049: Allen's answer from the notice card; 404 once the prompt is gone."""
        if not claude_hooks.answer(request_id, body):
            raise HTTPException(status_code=404, detail="that prompt is no longer waiting")
        return {"ok": True}
