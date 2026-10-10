"""Claude Code hooks Jarvis answers (ADR 0049).

``scripts/claude_hook.py`` pipes four Claude Code hook events here.
``PermissionRequest`` is held until Allen answers on the companion's notice
card, and the answer goes back as the hook's decision; ``PreCompact`` /
``PostCompact`` and ``StopFailure`` mark a session compacting or stopped,
which ``claude agents --json`` cannot tell. :meth:`ClaudeHooks.merge` lays
all three onto the session board rows of ADR 0046.

A held prompt is never the only way to answer: Claude Code shows its own
dialog alongside, and the first answer wins. Jarvis gives up a prompt (no
decision, Claude Code carries on as without the hook) when no companion has
read the board lately (or it stops reading while the prompt waits), when
the hook process goes away, and when the session moved on without it. From the
quiet level ``no-pop`` up (ADR 0153) the notch shows no cards, so nothing is
held and a prompt already held is let go within a second. A project thread's
session (``~/Projects``) is held like any other (ADR 0218).

Every prompt that comes in, held or not, is also counted in ``claude-prompts.jsonl``: its time,
tool, folder and the rule Claude Code offers to remember, never the command or the tool's input,
so the owner can pick which kinds to allow for good (his choice of 2026-10-10).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

LOGGER = logging.getLogger(__name__)

# A companion that read the board this recently is showing notices.
LISTENER_S = 10.0
# Claude Code's own hook timeout for this handler is a day; give up just before it.
HOLD_S = 86_000.0
# Transcript writes this long after a prompt came in mean the session went on without it.
MOVED_ON_MS = 3_000
# The prompt count stops growing past this; a week of prompts is a few hundred lines.
PROMPT_LOG_BYTES = 1_000_000
_RULE_CHARS = 120
# A long unbroken run in a rule may be a token pasted into a command: never written.
_SECRETISH = re.compile(r"[A-Za-z0-9_\-+/=]{20,}")
_ERRORS = {
    "rate_limit": "Rate limited",
    "overloaded": "The API is overloaded",
    "authentication_failed": "Signed out",
    "billing_error": "Billing problem",
    "max_output_tokens": "Hit the output limit",
    "server_error": "API server error",
    "invalid_request": "Invalid request",
    "model_not_found": "Model not found",
}


@dataclass
class _Held:
    id: str
    session_id: str
    tool: str
    input: dict[str, Any]
    cwd: str
    suggestions: list[dict[str, Any]]
    at_ms: int
    answer: asyncio.Future[dict[str, Any] | None] = field(repr=False)


def _offered(suggestions: list[dict[str, Any]]) -> str:
    """The rule Claude Code's first suggestion would remember, as settings write it, or empty."""
    first = suggestions[0] if suggestions else {}
    if first.get("type") == "setMode":
        return f"mode:{first.get('mode', '')}"
    rules = [r for r in first.get("rules") or [] if isinstance(r, dict)]
    if not rules:
        return ""
    content = rules[0].get("ruleContent")
    name = str(rules[0].get("toolName", ""))
    return f"{name}({content})" if content else name


def _always(suggestions: list[dict[str, Any]]) -> str:
    """Words for Claude Code's first "don't ask again" suggestion, or empty."""
    if not suggestions:
        return ""
    first = suggestions[0]
    if first.get("type") == "setMode" and first.get("mode") == "acceptEdits":
        return "Allow edits for the rest of this session"
    rules = [r for r in first.get("rules") or [] if isinstance(r, dict)]
    if first.get("type") == "addRules" and rules:
        rule = rules[0]
        content = rule.get("ruleContent")
        name = (
            f"{rule.get('toolName', '')}({content})" if content else str(rule.get("toolName", ""))
        )
        return f"Don't ask again for {name}"
    return "Don't ask again"


class ClaudeHooks:
    """Held permission prompts and the compacting / stopped marks, per session."""

    def __init__(
        self,
        quiet: Callable[[], str] | None = None,
        on_request: Callable[[str, str, str, Callable[[], bool]], None] | None = None,
        prompt_log: Path | None = None,
    ) -> None:
        """Nothing held, nothing marked, no companion reading yet; ``quiet`` reads the level.

        ``on_request(tool, cwd, request_id, waiting)`` is called when a prompt is held for him
        (ADR 0210); ``waiting()`` says, from any thread, whether it still waits (ADR 0218).
        ``prompt_log`` is where each prompt is counted; ``None`` counts nothing.
        """
        self._quiet = quiet or (lambda: "off")
        self._on_request = on_request
        self._prompt_log = prompt_log
        self._held: dict[str, _Held] = {}
        self._compacting: set[str] = set()
        self._stopped: dict[str, tuple[str, int]] = {}
        self._read_at = -LISTENER_S

    def event(self, payload: dict[str, Any]) -> None:
        """Fold one non-blocking hook event into the session marks."""
        name, session = payload.get("hook_event_name"), str(payload.get("session_id") or "")
        if name == "PreCompact":
            self._compacting.add(session)
        elif name == "PostCompact":
            self._compacting.discard(session)
        elif name == "StopFailure":
            self._compacting.discard(session)
            code = str(payload.get("error") or "unknown")
            detail = str(
                payload.get("error_details") or payload.get("last_assistant_message") or ""
            )
            text = _ERRORS.get(code, "Stopped on an error")
            self._stopped[session] = (
                f"{text}: {detail[:140]}" if detail else text,
                int(time.time() * 1000),
            )

    async def permission(
        self, payload: dict[str, Any], gone: Callable[[], Awaitable[bool]]
    ) -> dict[str, Any]:
        """Hold one prompt until Allen answers it; ``{}`` means no decision."""
        self._count(payload)
        if time.monotonic() - self._read_at > LISTENER_S or self._no_cards():
            return {}
        tool_input = payload.get("tool_input")
        suggestions = payload.get("permission_suggestions")
        held = _Held(
            id=secrets.token_hex(8),
            session_id=str(payload.get("session_id") or ""),
            tool=str(payload.get("tool_name") or ""),
            input=tool_input if isinstance(tool_input, dict) else {},
            cwd=str(payload.get("cwd") or ""),
            suggestions=[s for s in suggestions if isinstance(s, dict)]
            if isinstance(suggestions, list)
            else [],
            at_ms=int(time.time() * 1000),
            answer=asyncio.get_running_loop().create_future(),
        )
        self._held[held.id] = held
        if self._on_request is not None:
            try:
                self._on_request(
                    held.tool, held.cwd, held.id,
                    lambda: held.id in self._held and not held.answer.done(),
                )
            except Exception:  # noqa: BLE001 — telling his phone must not release the prompt.
                LOGGER.warning("claude hooks: the held-prompt callback failed")
        deadline = time.monotonic() + HOLD_S
        try:
            while not held.answer.done() and time.monotonic() < deadline:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(held.answer), timeout=1.0)
                # The hook process went away, or the companion stopped reading: no answer is coming.
                listening = time.monotonic() - self._read_at <= LISTENER_S
                if not held.answer.done() and (not listening or self._no_cards() or await gone()):
                    return {}
            decision = held.answer.result() if held.answer.done() else None
        finally:
            self._held.pop(held.id, None)
        if decision is None:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}

    def _count(self, payload: dict[str, Any]) -> None:
        """Append one line for this prompt: when, which tool, which folder, the rule on offer."""
        if self._prompt_log is None:
            return
        suggestions = payload.get("permission_suggestions")
        rule = _offered([s for s in suggestions if isinstance(s, dict)]) if isinstance(
            suggestions, list,
        ) else ""
        line = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "tool": str(payload.get("tool_name") or "")[:_RULE_CHARS],
            "folder": Path(str(payload.get("cwd") or "")).name,
            "rule": _SECRETISH.sub("…", rule)[:_RULE_CHARS],
        }
        try:
            if self._prompt_log.exists() and self._prompt_log.stat().st_size > PROMPT_LOG_BYTES:
                return
            with self._prompt_log.open("a", encoding="utf-8") as out:
                out.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            LOGGER.warning("claude hooks: the prompt count could not be written")

    def _no_cards(self) -> bool:
        """ADR 0153: at ``no-pop`` and ``dnd`` the notch shows no card to answer on."""
        return self._quiet() in {"no-pop", "dnd"}

    def answer(self, request_id: str, body: dict[str, Any]) -> bool:
        """Turn Allen's answer into the hook's decision; False when the prompt is gone."""
        held = self._held.get(request_id)
        if held is None or held.answer.done():
            return False
        choice = body.get("decision")
        if choice == "deny":
            message = str(body.get("message") or "") or "The user said no from Jarvis."
            held.answer.set_result({"behavior": "deny", "message": message})
            return True
        if choice not in {"allow", "always"}:
            return False
        decision: dict[str, Any] = {"behavior": "allow"}
        # These two tools only take an allow that carries their input back (with the answers).
        if held.tool == "AskUserQuestion":
            answers = body.get("answers")
            decision["updatedInput"] = {
                **held.input,
                "answers": answers if isinstance(answers, dict) else {},
            }
        elif held.tool == "ExitPlanMode":
            decision["updatedInput"] = held.input
        if choice == "always" and held.suggestions:
            decision["updatedPermissions"] = held.suggestions[:1]
        held.answer.set_result(decision)
        return True

    def merge(self, board: dict[str, Any]) -> dict[str, Any]:
        """The board with each session's held prompt, compacting and stopped marks laid on."""
        self._read_at = time.monotonic()
        rows = {r["session_id"]: r for r in board.get("sessions", [])}
        now_ms = int(time.time() * 1000)
        for held in list(self._held.values()):
            row = rows.get(held.session_id)
            # Not on the board once it had time to list it (a `claude -p` run has no card), or the
            # session wrote on without the answer.
            let_go = (
                now_ms > held.at_ms + MOVED_ON_MS
                if row is None
                else row["phase"] == "working" and row["updated_ms"] > held.at_ms + MOVED_ON_MS
            )
            if let_go and not held.answer.done():
                held.answer.set_result(None)
        requests = {
            h.session_id: {
                "id": h.id,
                "tool": h.tool,
                "input": h.input,
                "cwd": h.cwd,
                "always": _always(h.suggestions),
            }
            for h in sorted(self._held.values(), key=lambda h: h.at_ms, reverse=True)
            if not h.answer.done() and not self._no_cards()
        }
        for session, (_, at_ms) in list(self._stopped.items()):
            row = rows.get(session)
            # Working again after the error, or gone: the stop is over.
            if row is None or (row["phase"] != "done" and row["updated_ms"] > at_ms + MOVED_ON_MS):
                self._stopped.pop(session)
        return {
            **board,
            "sessions": [
                {
                    **r,
                    "request": requests.get(r["session_id"]),
                    "compacting": r["session_id"] in self._compacting and r["phase"] == "working",
                    "error": self._stopped[r["session_id"]][0]
                    if r["session_id"] in self._stopped
                    else "",
                }
                for r in board.get("sessions", [])
            ],
        }
