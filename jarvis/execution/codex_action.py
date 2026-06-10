"""One-shot Codex driver — spawns codex app-server, runs one turn, returns result.

ADR-0002 Step 7 — see § Codex contract (L4) and § submit_report tool injection
(spec §3.5.8). This module wraps the vendored :mod:`jarvis.execution.codex_client`
and is the bridge between jarvis L4 and Codex's app-server JSON-RPC protocol.

Mechanism is **spawn-time ``-c`` flag injection**, mirroring Hermes'
``agent/transports/codex_app_server.py:75-130`` pattern. Eight flags are
passed at spawn:

* 4 sandbox flags — ``model``, ``model_reasoning_effort``, ``sandbox_mode``,
  ``sandbox_workspace_write.writable_roots``.
* 4 MCP-server flags — ``mcp_servers.jarvis-tools.command``,
  ``mcp_servers.jarvis-tools.args``, ``mcp_servers.jarvis-tools.startup_timeout_sec``,
  ``mcp_servers.jarvis-tools.tool_timeout_sec`` — point Codex at the Step-6
  ``codex_mcp_tools.py`` stdio server so the ``submit_report`` tool is
  discoverable via the standard MCP ``tools/list`` handshake.

The notification poll loop captures submit_report from two protocol shapes
to span Codex 0.125-0.130+:

* **Codex 0.125-0.129** — ``item/tool_call`` notification with
  ``params.toolName == "submit_report"`` and ``params.arguments``.
* **Codex 0.130+** — ``item/completed`` notification with
  ``params.item.type == "mcpToolCall"`` and ``params.item.tool ==
  "submit_report"``; args are at ``params.item.arguments``.

Codex 0.130 also gates every external-MCP tool call behind a
server-initiated ``mcpServer/elicitation/request`` JSON-RPC. The poll
loop drains :py:meth:`CodexAppServerClient.take_server_request` on every
iteration and auto-accepts elicitations / approvals so the tool actually
executes (B-0013: leaving these unanswered hangs the turn forever).

The actual ``spawn_worker_handler`` wiring (event-log emission, dirty-tree
stash, artifact write) is Step 10's concern; this module is the pure
driver with no L2 access.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.execution.codex_client import CodexAppServerClient

if TYPE_CHECKING:
    from collections.abc import Callable

# Minimum codex CLI version we accept. Bumping is a one-line edit. The
# Step-7 module exposes :func:`ensure_codex_version_supported` as a helper;
# the gate is invoked by ``spawn_worker_handler`` at handler registration
# (Step 10), not on every turn.
_MIN_VERSION: tuple[int, int, int] = (0, 125, 0)
_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")

# TOML basic-string escape table: backslash and double-quote get a leading
# backslash. Newlines/control chars are not expected in cwd or sys.executable
# paths on Allen's Mac, so we escape only the two characters that *would*
# break TOML quoting if left raw. If we ever need to inject paths with
# control characters, extend this table per the TOML 1.0 basic-string spec.
_TOML_ESCAPE_RE = re.compile(r'(["\\])')

# Default heartbeat interval — every 30s of wall-clock with no
# ``turn/completed``, emit one heartbeat callback so observers know the
# turn is alive. Matches spec §3.5.8 ladder.
_HEARTBEAT_INTERVAL_S: float = 30.0

# Polling cadence for ``take_notification``. Short enough that timeout /
# heartbeat checks have <250 ms latency, long enough that the reader
# thread isn't busy-spinning.
_POLL_INTERVAL_S: float = 0.25

# Prefix for the per-spawn empty ``CODEX_HOME`` directory created in
# :func:`run_codex_action`. The directory exists for the lifetime of a
# single Codex turn and is removed in the result-finalize path. Its sole
# purpose is to isolate the spawned ``codex app-server`` from Allen's
# personal ``~/.codex/AGENTS.md`` / ``config.toml`` so that worker
# behavior is determined entirely by the spawn-time ``-c`` flags +
# MCP-injected tools (P-0009 in ``docs/live-run-bugs.md``).
_CODEX_HOME_PREFIX: str = "jarvis-codex-home-"

# ADR-0002 §643-646 mandates that codex_action.py repeat the
# submit_report rule as a system prompt (Prompt-pressure source 2).
# In Codex 0.130 the only working channel for jarvis-side instructions
# is ``$CODEX_HOME/AGENTS.md`` -- it is the lone path the app-server
# reports in ``instructionSources`` on ``thread/start``. The schema-
# declared ``ThreadStartParams.developerInstructions`` and
# ``baseInstructions`` fields are accepted but silently dropped
# (live-verified 2026-05-18: setting them leaves ``instructionSources``
# empty and ``turn_context.developer_instructions`` null).
# Per-spawn ``CODEX_HOME`` isolation (P-0009) means we own this file
# outright: we write the text below into the isolated tempdir at spawn
# time, so the AGENTS.md below is the entire developer-role instruction
# stream the worker sees. Content is the direct translation of ADR §643
# and the ``SUBMIT_REPORT_TOOL.description`` in
# ``codex_mcp_tools.py:73-76``; it adds no architectural elements
# beyond what ADR-0002 § submit_report tool injection already pins.
_JARVIS_AGENTS_MD: str = (
    "You are running a task on behalf of Jarvis. "
    "You MUST call the `submit_report` tool exactly once before ending the turn. "
    "The report is how Jarvis reads your result; without it the run is marked "
    "report_missing and treated as a failure.\n\n"
    "Required submit_report fields:\n"
    "- status: one of ok, partial, failed, blocked\n"
    "- summary: short description of what you did\n\n"
    "After completing the task (or determining you cannot complete it), "
    "call submit_report with the appropriate status."
)


def _canonical_auth_path() -> Path:
    """Return the canonical ``~/.codex/auth.json`` path (single source of truth).

    Used both for B-0004 seed-in (copy into the isolated ``CODEX_HOME``)
    and for the rotated-token writeback in :func:`_sync_rotated_auth`.
    """
    return Path("~/.codex/auth.json").expanduser()


def _parse_last_refresh(value: object) -> datetime | None:
    """Parse an auth.json ``last_refresh`` ISO-8601 timestamp, or ``None``.

    Codex writes Z-suffixed UTC timestamps, which ``fromisoformat``
    parses tz-aware. A naive timestamp (hand-edited / older-codex /
    hand-restored canonical) is normalized to UTC — comparing naive
    against aware raises TypeError, which would silently skip the
    writeback and re-lose the rotated single-use token.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _sync_rotated_auth(isolated_home: Path, canonical_auth: Path) -> None:
    """Write a rotated ``auth.json`` back from the isolated ``CODEX_HOME``.

    OpenAI refresh tokens are single-use rotating. B-0004 copy-seeds the
    canonical ``~/.codex/auth.json`` into the per-spawn throwaway home;
    when Codex refreshes the token *inside* that home, the new token
    lands in the throwaway copy and the canonical file keeps the
    now-consumed predecessor. Without this writeback, the first run
    after access-token expiry kills the canonical auth ("refresh token
    was already used") and every later turn dies as a silent empty turn
    (live-traced 2026-06-10; recovery needed interactive ``codex login``).

    Rules (ADR-0002 § Codex auth amendment):

    * Write back only when the isolated copy parses as JSON, still has a
      non-empty ``tokens`` object, AND its ``last_refresh`` is strictly
      newer than the canonical one (or canonical is missing/unreadable).
      ``last_refresh`` is compared as JSON content — mtime is useless
      because the B-0004 seed uses ``copy2`` which preserves it.
    * A canonical file that is newer (concurrent external refresh) is
      never clobbered.
    * Atomic replace via ``tempfile.mkstemp`` (0600 by design) +
      ``os.replace`` in the canonical's directory.
    * Never raises — a writeback failure must not mask the turn result;
      it degrades to a stderr warning.
    """
    try:
        isolated_auth = isolated_home / "auth.json"
        try:
            raw_text = isolated_auth.read_text()
            data = json.loads(raw_text)
        except (OSError, ValueError):
            return
        if not isinstance(data, Mapping):
            return
        tokens = data.get("tokens")
        if not isinstance(tokens, Mapping) or not tokens:
            # Logged-out / token-less shape — never clobber canonical with it.
            return
        isolated_refresh = _parse_last_refresh(data.get("last_refresh"))
        if isolated_refresh is None:
            return
        canonical_refresh: datetime | None = None
        try:
            canonical_data = json.loads(canonical_auth.read_text())
            if isinstance(canonical_data, Mapping):
                canonical_refresh = _parse_last_refresh(canonical_data.get("last_refresh"))
        except (OSError, ValueError):
            canonical_refresh = None
        if canonical_refresh is not None and isolated_refresh <= canonical_refresh:
            return
        # mkstemp creates the file 0600; os.replace preserves that mode,
        # matching codex's own permission on auth.json.
        fd, tmp_name = tempfile.mkstemp(dir=str(canonical_auth.parent), prefix=".jarvis-auth-")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(raw_text)
            Path(tmp_name).replace(canonical_auth)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink()
            raise
    except Exception as exc:  # noqa: BLE001 - writeback must never mask the turn result
        sys.stderr.write(f"[jarvis] rotated auth.json writeback failed: {exc}\n")


class CodexVersionTooLowError(RuntimeError):
    """Raised when ``codex --version`` parses below :data:`_MIN_VERSION`."""


# Back-compat alias: callers (and the build-step spec) reference
# ``CodexVersionTooLow``. The N818 rule wants an ``Error`` suffix; we keep
# both names so tests and external callers can use either.
CodexVersionTooLow = CodexVersionTooLowError


@dataclass(frozen=True)
class CodexActionResult:
    """Structured outcome of one ``run_codex_action`` invocation.

    Fields:
        final_text: Concatenated text items emitted during the turn (best-effort).
        diff_text: Captured ``git -C cwd diff`` after ``turn/completed``.
        diff_path: Always ``None`` in Step 7; Step 8 owns artifact persistence.
        turn_id: ``turnId`` from the ``turn/completed`` payload, if present.
        error: One of ``codex_initialize_failed: ...``, ``codex_turn_timeout``,
            ``codex_thread_start_failed: ...``, ``codex_turn_start_failed: ...``,
            ``codex_empty_turn`` (turn completed without streaming a single
            ``item/*`` notification — the dead-auth signature, live-traced
            2026-06-10), or ``None`` on success.
        interrupted: ``True`` iff the driver issued ``turn/interrupt`` (timeout).
        tokens_in / tokens_out: From ``turn/completed.usage`` (defensive ``.get``).
        elapsed_ms: Wall-clock from spawn to ``turn/completed`` (or timeout).
        submit_report: First captured ``submit_report`` tool-call arguments, or
            ``None`` if the worker never called the tool (Step 10 turns this
            into a ``worker.report_missing`` Limitation Claim per spec §3.5.8).
        submit_report_calls: All captured calls. Spec says exactly one; we
            preserve the full tuple in case Codex ever emits multiple.
    """

    final_text: str
    diff_text: str
    diff_path: Path | None
    turn_id: str | None
    error: str | None
    interrupted: bool
    tokens_in: int
    tokens_out: int
    elapsed_ms: int
    submit_report: Mapping[str, Any] | None = None
    submit_report_calls: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


def _toml_str(s: str) -> str:
    r"""Render ``s`` as a TOML basic-string literal (``"...escaped..."``).

    Escapes the two characters that break TOML basic-string quoting:
    a double-quote and a backslash. Used for scalar string values such as
    the MCP server's ``command`` path.
    """
    escaped = _TOML_ESCAPE_RE.sub(r"\\\1", s)
    return f'"{escaped}"'


def _toml_list_quote(path: Path | str) -> str:
    """Render a single-path list as a TOML inline-array literal (``["..."]``).

    Used for ``sandbox_workspace_write.writable_roots`` which is a TOML
    list. Reuses :func:`_toml_str` so the escape table stays canonical.
    """
    return f"[{_toml_str(str(path))}]"


def _build_extra_args(cwd: Path, model: str, reasoning_effort: str) -> list[str]:
    """Return the 8-flag ``-c`` argv slice passed to ``codex app-server``.

    Each flag is one ``-c`` plus one ``key=value`` pair. The string literals
    ``mcp_servers.jarvis-tools.command``, ``mcp_servers.jarvis-tools.args``,
    ``mcp_servers.jarvis-tools.startup_timeout_sec``, and
    ``mcp_servers.jarvis-tools.tool_timeout_sec`` MUST appear verbatim in
    this file's source — the Step-7 canaries AST-scan for them.
    """
    return [
        "-c",
        f"model={model}",
        "-c",
        f"model_reasoning_effort={reasoning_effort}",
        "-c",
        "approval_policy=never",
        "-c",
        "features.enable_mcp_apps=true",
        "-c",
        "features.builtin_mcp=true",
        "-c",
        "sandbox_mode=workspace-write",
        "-c",
        f"sandbox_workspace_write.writable_roots={_toml_list_quote(cwd)}",
        "-c",
        f"mcp_servers.jarvis-tools.command={_toml_str(sys.executable)}",
        "-c",
        'mcp_servers.jarvis-tools.args=["-m","jarvis.execution.codex_mcp_tools"]',
        "-c",
        "mcp_servers.jarvis-tools.startup_timeout_sec=30.0",
        "-c",
        "mcp_servers.jarvis-tools.tool_timeout_sec=600.0",
    ]


def _extract_tool_name(params: Mapping[str, Any]) -> str | None:
    """Return the tool name from an ``item/tool_call`` params dict.

    The protocol surfaces this as ``toolName`` per Hermes; tolerate
    ``tool_name`` as well in case a future protocol revision switches.
    """
    name = params.get("toolName")
    if isinstance(name, str):
        return name
    snake = params.get("tool_name")
    if isinstance(snake, str):
        return snake
    return None


def _extract_tool_arguments(params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the arguments dict from an ``item/tool_call`` params dict."""
    args = params.get("arguments")
    if isinstance(args, Mapping):
        return args
    snake = params.get("args")
    if isinstance(snake, Mapping):
        return snake
    return {}


def _extract_text_item(params: Mapping[str, Any]) -> str | None:
    """Return text content from an ``item/text`` (or ``item/message``) params dict.

    Defensive across protocol field-name variants — best-effort
    concatenation only; we never depend on the natural-language text for
    correctness (the structured ``submit_report`` payload is canonical).
    """
    text = params.get("text")
    if isinstance(text, str):
        return text
    content = params.get("content")
    if isinstance(content, str):
        return content
    return None


def _extract_usage(params: Mapping[str, Any]) -> tuple[int, int]:
    """Return ``(tokens_in, tokens_out)`` from a ``turn/completed`` params dict.

    Legacy shape only — Codex 0.130 (live-traced 2026-06-10) no longer
    carries a ``usage`` field on ``turn/completed``; cumulative usage
    arrives on ``thread/tokenUsage/updated`` instead (see
    :func:`_extract_token_usage_update`). Kept for back-compat with the
    0.125-0.129 flat shape.
    """
    usage = params.get("usage")
    if not isinstance(usage, Mapping):
        return (0, 0)
    tokens_in = usage.get("input_tokens") or usage.get("tokens_in") or 0
    tokens_out = usage.get("output_tokens") or usage.get("tokens_out") or 0
    return (int(tokens_in), int(tokens_out))


def _extract_token_usage_update(params: Mapping[str, Any]) -> tuple[int, int] | None:
    """Return cumulative ``(tokens_in, tokens_out)`` from ``thread/tokenUsage/updated``.

    Codex 0.130 shape (live-traced 2026-06-10)::

        params.tokenUsage.total = {
            "totalTokens": ..., "inputTokens": ..., "cachedInputTokens": ...,
            "outputTokens": ..., "reasoningOutputTokens": ...,
        }

    ``total`` is cumulative across the turn, so the last update before
    ``turn/completed`` is the turn's final count. Returns ``None`` when
    the payload does not carry the expected shape.
    """
    usage = params.get("tokenUsage")
    if not isinstance(usage, Mapping):
        return None
    total = usage.get("total")
    if not isinstance(total, Mapping):
        return None
    tokens_in = total.get("inputTokens") or 0
    tokens_out = total.get("outputTokens") or 0
    # Non-numeric counts must not raise: an exception escaping the poll
    # loop skips the _result finalizer (subprocess never closed, isolated
    # CODEX_HOME leaked, rotated-auth sync never run). Drop the update.
    if not isinstance(tokens_in, (int, float)) or not isinstance(tokens_out, (int, float)):
        return None
    return (int(tokens_in), int(tokens_out))


def _extract_turn_id(params: Mapping[str, Any]) -> str | None:
    """Return the turn id from a ``turn/completed`` params dict.

    Codex 0.130 nests it at ``params.turn.id`` (live-traced 2026-06-10);
    the flat ``turnId`` / ``turn_id`` keys are kept as legacy fallbacks.
    """
    turn = params.get("turn")
    if isinstance(turn, Mapping):
        nested = turn.get("id")
        if isinstance(nested, str):
            return nested
    tid = params.get("turnId")
    if isinstance(tid, str):
        return tid
    snake = params.get("turn_id")
    if isinstance(snake, str):
        return snake
    return None


# JSON-RPC method-not-found code (matches the spec used by codex
# app-server's own error replies). Used when we cannot service an
# unknown server-initiated request and need to fail it explicitly so
# Codex doesn't sit waiting on a reply we'll never send.
_METHOD_NOT_FOUND: int = -32601


def _extract_completed_mcp_tool(
    params: Mapping[str, Any],
) -> tuple[str | None, Mapping[str, Any]] | None:
    """Return ``(tool_name, arguments)`` for a Codex 0.130 ``item/completed`` mcpToolCall.

    Returns ``None`` if the payload is not an mcpToolCall item. The
    Codex 0.130 schema (live-verified via probe v4) is::

        params.item = {
            "type": "mcpToolCall",
            "server": "jarvis-tools",
            "tool": "submit_report",
            "status": "completed",
            "arguments": {...},
            "result": {...},
        }
    """
    item = params.get("item")
    if not isinstance(item, Mapping):
        return None
    if item.get("type") != "mcpToolCall":
        return None
    tool_name = item.get("tool")
    args = item.get("arguments")
    if not isinstance(args, Mapping):
        args = {}
    return (tool_name if isinstance(tool_name, str) else None, args)


def _auto_respond_server_request(
    client: CodexAppServerClient, req: Mapping[str, Any]
) -> None:
    """Auto-respond to Codex server-initiated JSON-RPC so the turn doesn't hang.

    Codex 0.130 sends ``mcpServer/elicitation/request`` for every
    external-MCP tool call when the ``tool_call_mcp_elicitation``
    feature is on (always-on as of 0.130). Without a reply the tool
    never executes and the turn hangs to the timeout (B-0013).

    ``approval_policy=never`` (set in :func:`_build_extra_args`)
    suppresses *exec* approvals, but MCP elicitation is a separate gate
    that ``never`` does not cover, so we still receive elicitation
    requests for ``submit_report`` and must answer them. We auto-accept
    because the only MCP tool exposed is ``submit_report`` (a
    jarvis-injected report sink) and the worker already runs inside
    Codex's ``workspace-write`` sandbox.

    Unknown methods get a JSON-RPC ``method not found`` error so Codex
    doesn't sit waiting on a reply that will never come.
    """
    req_id = req.get("id")
    method_obj = req.get("method")
    method = method_obj if isinstance(method_obj, str) else ""
    if req_id is None:
        # Malformed request (no id) — nothing to respond to.
        return
    # Suppress send failures: if codex died mid-turn the next
    # take_notification tick handles the bailout cleanly; we don't want
    # a broken pipe here to mask the real timeout/crash error.
    with contextlib.suppress(Exception):
        if method == "mcpServer/elicitation/request":
            client.respond(
                req_id, {"action": "accept", "content": None, "_meta": None}
            )
        elif "approval" in method:
            client.respond(req_id, {"decision": "approve"})
        else:
            client.respond_error(
                req_id,
                code=_METHOD_NOT_FOUND,
                message=f"method not implemented: {method}",
            )


class _ProtocolError(RuntimeError):
    """Raised on malformed JSON-RPC payloads from codex app-server."""


def _extract_thread_id(result: Mapping[str, Any]) -> str:
    """Return ``threadId`` from a ``thread/start`` response.

    Codex 0.130 returns the id nested as ``result["thread"]["id"]``. Older
    flat shapes (``threadId`` / ``thread_id``) are accepted as defensive
    fallbacks in case the schema changes again.
    """
    thread = result.get("thread")
    if isinstance(thread, Mapping):
        nested = thread.get("id")
        if isinstance(nested, str):
            return nested
    tid = result.get("threadId") or result.get("thread_id")
    if isinstance(tid, str):
        return tid
    msg = f"thread/start response missing threadId: {result!r}"
    raise _ProtocolError(msg)


def _capture_diff(cwd: Path) -> str:
    """Return the working-tree diff for ``cwd``, INCLUDING untracked new files.

    Day-2 verifies Codex's in-place edits. A plain ``git diff`` reports
    only tracked-file changes, so a Codex deliverable that is a NEW file
    (e.g. creating ``NOTES.md``) would be invisible — under-reporting the
    spec §8.9 "artifact changed (intended files touched)" signal, feeding
    the reviewer a lossy artifact (the B-0010 reviewer-hallucination root
    cause) and mis-deriving ``diff_nonempty``. The pre-task stash
    (:func:`jarvis.execution.diff_capture.isolate_pretask_changes`, ``-u``)
    removes any pre-existing untracked files, so every untracked path
    present post-Codex is Codex's own new work and is safe to include.

    Read-only: tracked changes via ``git diff``; each untracked file via
    ``git diff --no-index -- /dev/null <file>`` (emits a proper new-file
    unified diff, exit code 1 on difference — expected). No index
    mutation, so the surrounding stash/restore machinery is untouched.
    Step 7 captures the diff text only; Step 8 owns the artifact-write and
    dirty-tree stash handling. We swallow git failures here (return empty)
    because the driver must not raise — it returns a structured result.
    """
    try:
        tracked = subprocess.run(  # noqa: S603 - trusted argv, no shell
            ["git", "-C", str(cwd), "diff"],  # noqa: S607 - git on PATH by design
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout
        listing = subprocess.run(  # noqa: S603 - trusted argv, no shell
            # -z: NUL-separated raw names (space/unicode safe); --exclude-standard
            # honours .gitignore so build junk (e.g. __pycache__) stays out.
            ["git", "-C", str(cwd), "ls-files", "--others", "--exclude-standard", "-z"],  # noqa: S607 - git on PATH by design
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""

    parts: list[str] = [tracked] if tracked else []
    for rel in listing.split("\x00"):
        if not rel:
            continue
        try:
            shown = subprocess.run(  # noqa: S603 - trusted argv, no shell
                ["git", "-C", str(cwd), "diff", "--no-index", "--", os.devnull, rel],  # noqa: S607 - git on PATH by design
                capture_output=True,
                text=True,
                check=False,  # --no-index exits 1 when files differ (the normal case)
                timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if shown:
            parts.append(shown)
    return "".join(parts)


def ensure_codex_version_supported(codex_bin: str = "codex") -> None:
    """Raise :class:`CodexVersionTooLowError` if ``codex --version`` is below the floor.

    Called at handler registration (Step 10) before exposing ``spawn_worker``.
    The Step-7 module exposes the helper but does not invoke it on every
    run (handler registration is a one-shot pre-flight).
    """
    try:
        out = subprocess.run(  # noqa: S603 - trusted argv from caller
            [codex_bin, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except FileNotFoundError as exc:
        msg = f"codex CLI not found at {codex_bin!r}: {exc}"
        raise CodexVersionTooLowError(msg) from exc
    except subprocess.SubprocessError as exc:
        msg = f"codex --version invocation failed: {exc}"
        raise CodexVersionTooLowError(msg) from exc

    haystack = (out.stdout or "") + " " + (out.stderr or "")
    match = _VERSION_RE.search(haystack)
    if match is None:
        msg = f"could not parse codex --version output: {out.stdout!r}"
        raise CodexVersionTooLowError(msg)
    version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if version < _MIN_VERSION:
        msg = (
            f"codex version {'.'.join(map(str, version))} < required "
            f"{'.'.join(map(str, _MIN_VERSION))} — run "
            f"`npm i -g @openai/codex` to upgrade"
        )
        raise CodexVersionTooLowError(msg)


def run_codex_action(  # noqa: C901, PLR0912, PLR0913, PLR0915 - one-shot driver is naturally branchy and parameter-heavy; spec calls for 9 kwargs; splitting hurts readability
    *,
    task_goal: str,
    cwd: Path,
    timeout_s: float = 600.0,
    on_heartbeat: Callable[[dict[str, Any]], None] | None = None,
    heartbeat_interval_s: float = _HEARTBEAT_INTERVAL_S,
    codex_bin: str = "codex",
    model: str = "gpt-5.5",
    reasoning_effort: str = "xhigh",
    env: dict[str, str] | None = None,
) -> CodexActionResult:
    """Run one Codex turn and return a structured :class:`CodexActionResult`.

    Lifecycle (mirrors ADR-0002 § Codex contract Lifecycle):

    1. Build 8 ``-c`` flags (4 sandbox + 4 MCP-server injection).
    2. Construct :class:`CodexAppServerClient`, run ``initialize`` (5 s timeout).
    3. ``thread/start`` -> grab ``threadId``.
    4. ``turn/start`` with the task goal as a text input.
    5. Poll ``take_notification(0.25)`` until ``turn/completed`` or deadline.
       On every ``item/tool_call`` with ``tool_name == "submit_report"``,
       capture the structured arguments. On ``heartbeat_interval_s``
       (default 30 s) wall-clock idle, fire ``on_heartbeat``. On deadline,
       send ``turn/interrupt`` and bail.
    6. Capture ``git diff`` text (no artifact write — Step 8's concern).
    7. ``close(timeout=3)``.

    Args:
        task_goal: The natural-language task description for the worker.
        cwd: The repo root the Codex worker runs in (becomes the sandbox
            writable_root and the ``thread/start`` ``cwd`` parameter).
        timeout_s: Wall-clock budget for the turn; on overrun the driver
            interrupts and returns with ``error="codex_turn_timeout"``.
        on_heartbeat: Optional callable invoked every ``heartbeat_interval_s``
            of idle time with a summary dict (``summary``, ``elapsed_ms``,
            ``last_item_summary``). Step 10's ``spawn_worker_handler``
            uses this to emit ``worker.heartbeat`` events.
        heartbeat_interval_s: Wall-clock idle cadence between ``on_heartbeat``
            firings; defaults to 30 s. ``spawn_worker_handler`` lowers it via
            ``JARVIS_CODEX_HEARTBEAT_INTERVAL_S`` so the J4 heartbeat
            lifecycle is exercisable live in seconds.
        codex_bin: Path or PATH-name of the ``codex`` CLI binary.
        model: ``-c model=<name>`` flag value.
        reasoning_effort: ``-c model_reasoning_effort=<level>`` flag value.
        env: Optional environment overrides. ``RUST_LOG=warn`` is set if
            absent (matches Hermes' default). ``CODEX_HOME`` is set to a
            per-spawn empty temp dir unless explicitly supplied in this
            ``env`` mapping, so user-local or parent-process Codex config
            cannot contaminate the worker (P-0009). Pass
            ``env={"CODEX_HOME": ...}`` to override.

    Returns:
        :class:`CodexActionResult` with at least ``error`` and ``interrupted``
        set; on success ``error is None`` and the other fields populated.
    """
    extra_args = _build_extra_args(cwd=cwd, model=model, reasoning_effort=reasoning_effort)

    env_dict = os.environ.copy() if env is None else env.copy()
    env_dict.setdefault("RUST_LOG", "warn")

    # Per-spawn empty CODEX_HOME -- isolates the worker from
    # ~/.codex/AGENTS.md and ~/.codex/config.toml so user-local Codex
    # config cannot contaminate the worker's instruction stream
    # (P-0009). Only an explicit CODEX_HOME in the function's ``env``
    # parameter is an override. An ambient parent-process CODEX_HOME is
    # ignored, otherwise Jarvis would inherit this agent's own home and
    # skip the auth/tool seeding required for the verify pipeline.
    codex_home_dir: str | None = None
    explicit_codex_home = env is not None and env.get("CODEX_HOME") not in (None, "")
    if not explicit_codex_home:
        codex_home_dir = tempfile.mkdtemp(prefix=_CODEX_HOME_PREFIX)
        env_dict["CODEX_HOME"] = codex_home_dir

    start_mono = time.monotonic()
    # Seed the isolated CODEX_HOME and construct the client inside a try
    # block so a PermissionError / OSError / FileNotFoundError during
    # seeding or client construction does not leak the tempdir (which
    # contains a copy of ~/.codex/auth.json -- OpenAI session
    # credentials). See observation 12561. ``BaseException`` so
    # ``KeyboardInterrupt`` during a long copy still cleans. Bare
    # ``raise`` preserves the original traceback for
    # ``spawn_worker_handler``'s ``action.failed`` emission.
    try:
        if codex_home_dir is not None:
            # Codex 0.130 responses_websocket reads auth from
            # $CODEX_HOME/auth.json, not OPENAI_API_KEY (B-0004 live-verified:
            # empty home -> 401 on every request). Seed the isolated dir so
            # the worker can reach api.openai.com.
            source_auth = _canonical_auth_path()
            if source_auth.is_file():
                shutil.copy2(source_auth, Path(codex_home_dir) / "auth.json")
            # Inject the jarvis-controlled AGENTS.md (ADR-0002 §644
            # "system prompt in codex_action.py repeats the rule"). This is
            # the only working instruction-source channel in Codex 0.130
            # (see ``_JARVIS_AGENTS_MD`` for the live-verification note).
            # The per-spawn isolated home means the user's
            # ``~/.codex/AGENTS.md`` cannot leak in and our file is the
            # entire developer-role instruction stream.
            (Path(codex_home_dir) / "AGENTS.md").write_text(_JARVIS_AGENTS_MD)
            # B-0007 fix: register the jarvis-tools MCP server via
            # config.toml. Codex 0.130 silently ignores
            # ``mcp_servers.X.Y=Z`` dotted keys passed via ``-c`` flags;
            # the MCP registry is populated only from
            # ``[mcp_servers."<name>"]`` table headers in config.toml
            # (Hermes pattern, see
            # ``agent/transports/hermes_tools_mcp_server.py``). Without
            # this write the spawned worker only sees Codex's 15 built-in
            # tools and can never call submit_report -- live-verified
            # 2026-05-21: 0 ``jarvis-tools`` mentions in the 4MB session
            # log, 15-tool API requests across A4 r1-r5.
            config_toml = (
                '[mcp_servers."jarvis-tools"]\n'
                f"command = {_toml_str(sys.executable)}\n"
                'args = ["-m", "jarvis.execution.codex_mcp_tools"]\n'
                "startup_timeout_sec = 30.0\n"
                "tool_timeout_sec = 600.0\n"
            )
            (Path(codex_home_dir) / "config.toml").write_text(config_toml)

        client = CodexAppServerClient(codex_bin=codex_bin, extra_args=extra_args, env=env_dict)
    except BaseException:
        if codex_home_dir is not None:
            shutil.rmtree(codex_home_dir, ignore_errors=True)
        raise

    final_text_parts: list[str] = []
    submit_report_calls: list[Mapping[str, Any]] = []
    saw_any_item = False
    last_item_summary: str = ""
    turn_id_out: str | None = None
    tokens_in = 0
    tokens_out = 0
    error: str | None = None
    interrupted = False

    def _result(
        *,
        error: str | None,
        interrupted: bool,
        thread_id: str | None,
    ) -> CodexActionResult:
        # Close best-effort; never let close() failure clobber the real outcome.
        with contextlib.suppress(Exception):
            client.close(timeout=3.0)
        # Remove the per-spawn empty CODEX_HOME if we created one.
        # ``ignore_errors=True`` ensures a stuck file (e.g. NFS lock) never
        # masks the real result; the dir is a few bytes empty in steady state.
        # ``JARVIS_PRESERVE_CODEX_HOME=1`` keeps the dir on disk for
        # post-mortem inspection (rollout JSONL, ``AGENTS.md``,
        # ``instructionSources`` in ``logs.sqlite``) -- debug only.
        if codex_home_dir is not None:
            # B-0004 follow-up: if Codex rotated the OpenAI refresh token
            # inside the throwaway home, write it back BEFORE the dir is
            # removed — otherwise the canonical ~/.codex/auth.json keeps
            # the consumed token and dies on the next refresh cycle.
            _sync_rotated_auth(Path(codex_home_dir), _canonical_auth_path())
            if os.environ.get("JARVIS_PRESERVE_CODEX_HOME"):
                sys.stderr.write(f"[jarvis] preserved CODEX_HOME={codex_home_dir}\n")
            else:
                shutil.rmtree(codex_home_dir, ignore_errors=True)
        # ``thread_id`` is unused in the result but kept in the signature
        # so the closure is documented (Step 10 may want it for events).
        del thread_id
        return CodexActionResult(
            final_text="".join(final_text_parts),
            diff_text=_capture_diff(cwd),
            diff_path=None,
            turn_id=turn_id_out,
            error=error,
            interrupted=interrupted,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            elapsed_ms=int((time.monotonic() - start_mono) * 1000),
            submit_report=submit_report_calls[0] if submit_report_calls else None,
            submit_report_calls=tuple(submit_report_calls),
        )

    # Step 2: initialize handshake.
    try:
        client.initialize(timeout=5.0)
    except Exception as exc:  # noqa: BLE001 - any failure here is fatal-but-recoverable
        return _result(
            error=f"codex_initialize_failed: {exc}",
            interrupted=False,
            thread_id=None,
        )

    # Step 3: thread/start. The submit_report prompt-pressure layer is
    # delivered via ``$CODEX_HOME/AGENTS.md`` (written above) -- the
    # only working instruction-source channel in Codex 0.130
    # (live-verified 2026-05-18). Passing ``developerInstructions`` /
    # ``baseInstructions`` here is a no-op so we omit them.
    try:
        ts_result = client.request("thread/start", {"cwd": str(cwd)})
        thread_id = _extract_thread_id(ts_result)
    except Exception as exc:  # noqa: BLE001 - protocol failure surfaces as structured error
        return _result(
            error=f"codex_thread_start_failed: {exc}",
            interrupted=False,
            thread_id=None,
        )

    # Step 4: turn/start.
    try:
        client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": task_goal}],
            },
        )
    except Exception as exc:  # noqa: BLE001 - protocol failure surfaces as structured error
        return _result(
            error=f"codex_turn_start_failed: {exc}",
            interrupted=False,
            thread_id=thread_id,
        )

    # Step 5: notification poll loop.
    deadline = start_mono + timeout_s
    last_heartbeat_at = start_mono

    while True:
        now = time.monotonic()
        if now >= deadline:
            # Timeout — interrupt the turn and bail.
            with contextlib.suppress(Exception):
                client.request("turn/interrupt", {"threadId": thread_id})
            interrupted = True
            error = "codex_turn_timeout"
            break

        # Drain pending server-initiated requests first so an
        # outstanding mcpServer/elicitation/request can't block the next
        # tool dispatch (B-0013). Non-blocking — we only act on what's
        # already queued; if none, fall through to the notification poll.
        while True:
            req = client.take_server_request(timeout=0.0)
            if req is None:
                break
            _auto_respond_server_request(client, req)

        notif = client.take_notification(timeout=_POLL_INTERVAL_S)
        if notif is None:
            # The notification queue is drained this tick. If the
            # subprocess has exited before emitting turn/completed, that
            # is a crash, not a slow turn — surface the canonical
            # codex_subprocess_crashed tag immediately (J8 / ADR-0002
            # Negative-path appendix) instead of spinning out the full
            # deadline and mislabelling it codex_turn_timeout. Checked
            # AFTER take_notification so any buffered turn/completed is
            # processed first (a clean turn whose proc then exits is not
            # treated as a crash).
            if not client.is_alive():
                error = "codex_subprocess_crashed"
                break
            # No notification this tick — check heartbeat cadence.
            if on_heartbeat is not None and (now - last_heartbeat_at) >= heartbeat_interval_s:
                on_heartbeat(
                    {
                        "summary": "codex turn in progress",
                        "elapsed_ms": int((now - start_mono) * 1000),
                        "last_item_summary": last_item_summary,
                    }
                )
                last_heartbeat_at = now
            continue

        method = notif.get("method", "") or ""
        raw_params = notif.get("params") or {}
        params: Mapping[str, Any] = raw_params if isinstance(raw_params, Mapping) else {}

        if method.startswith("item/"):
            # Any streamed item (started/completed/delta/tool_call) proves
            # the model actually ran. A turn that completes with zero items
            # is the dead-auth signature (Codex 0.130 folds an unrefreshable
            # token into a ~2s task_complete with last_agent_message=null)
            # and is classified ``codex_empty_turn`` below.
            saw_any_item = True

        if method == "item/tool_call":
            # Legacy Codex 0.125-0.129 schema; kept as a back-compat fallback.
            tool_name = _extract_tool_name(params)
            if tool_name == "submit_report":
                submit_report_calls.append(_extract_tool_arguments(params))
            last_item_summary = f"tool_call:{tool_name or 'unknown'}"

        elif method == "item/completed":
            mcp = _extract_completed_mcp_tool(params)
            if mcp is not None:
                tool_name, args = mcp
                if tool_name == "submit_report":
                    submit_report_calls.append(args)
                last_item_summary = f"tool_call:{tool_name or 'unknown'}"
            else:
                text = _extract_text_item(params)
                if text:
                    final_text_parts.append(text)
                last_item_summary = method

        elif method.startswith("item/"):
            text = _extract_text_item(params)
            if text:
                final_text_parts.append(text)
            last_item_summary = method

        elif method == "thread/tokenUsage/updated":
            # Codex 0.130 streams cumulative usage here; turn/completed no
            # longer carries a usage field. Each update overwrites the
            # previous one (totals are cumulative), so the last update
            # before turn/completed is the turn's final count.
            usage_update = _extract_token_usage_update(params)
            if usage_update is not None:
                tokens_in, tokens_out = usage_update

        elif method == "turn/completed":
            # Legacy (0.125-0.129) flat usage on turn/completed is
            # authoritative when present; never clobber streamed
            # tokenUsage totals with the 0.130 shape's missing field.
            legacy_in, legacy_out = _extract_usage(params)
            if legacy_in or legacy_out:
                tokens_in, tokens_out = legacy_in, legacy_out
            turn_id_out = _extract_turn_id(params)
            if not saw_any_item:
                # Zero-item turn — tool-level success is not goal evidence
                # (C5); surface a structured error so spawn_worker_handler
                # 7b folds it into action.failed + Limitation Claim instead
                # of the silent task.no_op + report_missing shape.
                error = "codex_empty_turn"
            break

        # Unknown notification methods are ignored on purpose; the
        # server-request queue is drained at the top of every iteration.

    return _result(error=error, interrupted=interrupted, thread_id=thread_id)
