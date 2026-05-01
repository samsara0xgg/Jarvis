"""Read Claude Code conversation logs from ``~/.claude/projects/``.

Each running ``claude`` session writes a JSONL log under
``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`` where the cwd is
encoded by replacing ``/`` with ``-``. Each line is one event with at
least a ``type`` field; assistant turns carry a structured ``message``
with content blocks (``text`` / ``tool_use`` / ``tool_result``).

This module is a read-only helper for ``cc_show``-style skills:

    find_active_jsonl(cwd)     →  Path | None
    read_last_assistant(path)  →  AssistantTurn

Subagent traces (under ``<session-uuid>/subagents/agent-*.jsonl``) are
ignored — those are spawned by the cc Task tool and are not the user's
conversation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

LOGGER = logging.getLogger(__name__)

CC_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Reverse-scan limit per file. cc assistant turns are typically among the
# last few hundred lines; we cap at 5000 to defend against degenerate
# logs without OOM.
_MAX_LINES_REVERSE_SCAN = 5000


@dataclass
class ToolCall:
    """A single ``tool_use`` block extracted from an assistant turn."""

    name: str
    input_summary: str  # one-line summary of the tool's input dict


@dataclass
class AssistantTurn:
    """The most recent assistant message, distilled for narration.

    ``text`` is the concatenation of all ``text`` blocks in order
    (joined by ``\\n\\n``). ``tool_calls`` lists ``tool_use`` blocks in
    the order they appear. ``empty`` is True when no assistant turn was
    found in the scanned range (e.g., session just opened).
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    empty: bool = True


@dataclass
class CCCandidate:
    """A single recently-active cc conversation summary.

    Used by ``cc_show`` to feed multiple candidates to the chat LLM,
    which then matches them against the pane's viewport content to
    figure out which jsonl belongs to which pane. This avoids the need
    for any deterministic pane→jsonl protocol.

    Fields:
        jsonl_path           : absolute path of the jsonl file
        session_id           : cc session UUID (jsonl basename without ``.jsonl``)
        mtime                : float epoch seconds of last write
        last_user_prompt     : truncated first ~150 chars of the most recent
                               human user prompt (NOT a tool_result message)
        last_assistant_text  : truncated first ~300 chars of the most recent
                               assistant text content
        recent_tool_calls    : up to 5 most-recent tool_use blocks from the
                               last assistant turn
        empty                : True if no assistant message was found
    """

    jsonl_path: Path
    session_id: str
    mtime: float
    last_user_prompt: str = ""
    last_assistant_text: str = ""
    recent_tool_calls: list[ToolCall] = field(default_factory=list)
    empty: bool = True


# Candidate text caps. Allen's daily voice flow tolerates 1500 token tool
# results comfortably; 3 candidates × ~400 chars + viewport keeps us
# well below that.
_CANDIDATE_USER_PROMPT_CHARS = 150
_CANDIDATE_ASSISTANT_CHARS = 300
_CANDIDATE_MAX_TOOL_CALLS = 5


def encode_cwd_for_cc(cwd: str) -> str:
    """Convert a filesystem path into cc's project-dir encoding.

    cc replaces every ``/`` with ``-`` in the absolute cwd. So
    ``/Users/foo/bar`` → ``-Users-foo-bar``. Used to locate the
    project's jsonl directory under ``~/.claude/projects/``.
    """
    return cwd.replace("/", "-")


def find_active_jsonl(cwd: str) -> Path | None:
    """Return the most recently modified top-level jsonl for ``cwd``.

    Subagent jsonls (in nested ``<uuid>/subagents/`` dirs) are excluded
    — they belong to cc Task subagents, not the user's session. Returns
    ``None`` if the project directory does not exist or has no jsonls.

    Convenience wrapper around :func:`find_recent_jsonls` for callers
    that only want the single freshest jsonl. cc_show uses
    ``find_recent_jsonls`` directly to pull multiple candidates.
    """
    found = find_recent_jsonls(cwd, n=1)
    return found[0] if found else None


def find_recent_jsonls(cwd: str, n: int = 3) -> list[Path]:
    """Return up to ``n`` most-recently-modified top-level jsonls for ``cwd``.

    Output is sorted newest-first. Subagent jsonls under nested
    ``<uuid>/subagents/`` are excluded — those are cc Task spawns, not
    user conversations. ``[]`` if the project dir doesn't exist or has
    no jsonls.

    cc_show feeds these as multi-candidate context to the chat LLM,
    which performs content-correlation matching against the pane's
    viewport to identify which candidate belongs to the asked pane.
    No deterministic pane→jsonl mapping is required.
    """
    project_dir = CC_PROJECTS_ROOT / encode_cwd_for_cc(cwd)
    if not project_dir.is_dir():
        LOGGER.debug("cc project dir not found: %s", project_dir)
        return []

    candidates = [p for p in project_dir.glob("*.jsonl") if p.is_file()]
    if not candidates:
        return []
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[:n]


def read_last_assistant(jsonl_path: Path) -> AssistantTurn:
    """Reverse-scan a cc jsonl for the most recent assistant turn.

    Returns an empty ``AssistantTurn`` (``empty=True``) on any of:
        - file missing / unreadable
        - no assistant message in the last ``_MAX_LINES_REVERSE_SCAN``
        - all assistant blocks malformed

    Only the LAST assistant turn is returned (not aggregated across
    turns). This matches "what cc said most recently".
    """
    try:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        LOGGER.exception("Failed to read cc jsonl: %s", jsonl_path)
        return AssistantTurn()

    scan_window = lines[-_MAX_LINES_REVERSE_SCAN:]
    for raw in reversed(scan_window):
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue

        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue

        return _extract_assistant_turn(content)

    return AssistantTurn()


def read_recent_exchange(jsonl_path: Path) -> CCCandidate:
    """Reverse-scan a cc jsonl for last assistant turn + last real user prompt.

    "Real user prompt" = a ``type="user"`` event whose content blocks are
    plain ``text`` blocks. cc emits ``type="user"`` events for tool
    results too — those have ``content[*].type == "tool_result"`` and are
    skipped here. The combination of both signals (user text + assistant
    text) gives the chat LLM enough fingerprint to match against the
    pane viewport.

    Returns a populated :class:`CCCandidate`. ``empty=True`` only if the
    file has no readable assistant message at all (e.g., session just
    opened, or file unreadable).
    """
    candidate = CCCandidate(
        jsonl_path=jsonl_path,
        session_id=jsonl_path.stem,
        mtime=_safe_mtime(jsonl_path),
    )

    try:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        LOGGER.exception("Failed to read cc jsonl: %s", jsonl_path)
        return candidate

    scan_window = lines[-_MAX_LINES_REVERSE_SCAN:]

    assistant_seen = False
    for raw in reversed(scan_window):
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue

        etype = event.get("type")
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue

        if etype == "assistant" and not assistant_seen:
            turn = _extract_assistant_turn(content)
            candidate.last_assistant_text = _truncate(
                turn.text, _CANDIDATE_ASSISTANT_CHARS
            )
            candidate.recent_tool_calls = turn.tool_calls[:_CANDIDATE_MAX_TOOL_CALLS]
            candidate.empty = turn.empty
            assistant_seen = True
            continue

        if etype == "user" and _is_real_user_prompt(content):
            candidate.last_user_prompt = _truncate(
                _user_text_blocks(content), _CANDIDATE_USER_PROMPT_CHARS
            )
            # User prompt is the second piece — once we have it, stop.
            if assistant_seen:
                break

    return candidate


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _is_real_user_prompt(content: list) -> bool:
    """True iff the user content is human input (text blocks), not a tool_result.

    cc writes both real user prompts and tool feedback as ``type="user"``
    events; the discriminator is the inner block type. ``tool_result``
    blocks are cc's own post-tool feedback to itself.
    """
    if not content:
        return False
    return all(
        isinstance(b, dict) and b.get("type") == "text"
        for b in content
    )


def _user_text_blocks(content: list) -> str:
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text", "")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts).strip()


def _extract_assistant_turn(content: list) -> AssistantTurn:
    """Pull text + tool_use blocks out of an assistant content array."""
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text", "")
            if isinstance(t, str) and t.strip():
                texts.append(t)
        elif btype == "tool_use":
            name = block.get("name", "?")
            tool_input = block.get("input", {})
            tool_calls.append(
                ToolCall(name=name, input_summary=_summarize_tool_input(name, tool_input))
            )

    return AssistantTurn(
        text="\n\n".join(texts),
        tool_calls=tool_calls,
        empty=not texts and not tool_calls,
    )


def _summarize_tool_input(name: str, tool_input: dict) -> str:
    """One-line synopsis of a tool call's input — for narration.

    Picks the most-identifying field per tool name and truncates to
    120 chars. Unknown tools fall back to listing the first key/value.
    """
    if not isinstance(tool_input, dict):
        return ""

    name_l = (name or "").lower()
    if name_l == "bash":
        return _truncate(tool_input.get("command", ""), 120)
    if name_l in ("read", "write", "edit"):
        return _truncate(str(tool_input.get("file_path", "")), 120)
    if name_l in ("grep", "glob"):
        return _truncate(str(tool_input.get("pattern", "")), 120)
    if name_l == "task":
        return _truncate(str(tool_input.get("description", "")), 120)
    if name_l == "webfetch":
        return _truncate(str(tool_input.get("url", "")), 120)

    # Fallback: first key=value pair
    if tool_input:
        k = next(iter(tool_input))
        return _truncate(f"{k}={tool_input[k]}", 120)
    return ""


def _truncate(s: str, n: int) -> str:
    s = str(s).replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 3] + "..."
