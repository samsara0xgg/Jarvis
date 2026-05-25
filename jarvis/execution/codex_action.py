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

The ``take_notification`` loop captures every ``item/tool_call`` event where
``tool_name == "submit_report"`` and surfaces the structured arguments in
:class:`CodexActionResult`. The actual ``spawn_worker_handler`` wiring
(event-log emission, dirty-tree stash, artifact write) is Step 10's concern;
this module is the pure driver with no L2 access.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
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
            or ``None`` on success.
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
    """Return ``(tokens_in, tokens_out)`` from a ``turn/completed`` params dict."""
    usage = params.get("usage")
    if not isinstance(usage, Mapping):
        return (0, 0)
    tokens_in = usage.get("input_tokens") or usage.get("tokens_in") or 0
    tokens_out = usage.get("output_tokens") or usage.get("tokens_out") or 0
    return (int(tokens_in), int(tokens_out))


def _extract_turn_id(params: Mapping[str, Any]) -> str | None:
    """Return ``turnId`` from a ``turn/completed`` params dict."""
    tid = params.get("turnId")
    if isinstance(tid, str):
        return tid
    snake = params.get("turn_id")
    if isinstance(snake, str):
        return snake
    return None


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
    """Return ``git -C cwd diff`` stdout, or empty string on failure.

    Step 7 captures the diff text only; Step 8 owns the artifact-write and
    dirty-tree stash handling. We swallow git failures here (return empty)
    because the driver must not raise — it returns a structured result.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - trusted argv, no shell
            ["git", "-C", str(cwd), "diff"],  # noqa: S607 - git on PATH by design
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout


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


def run_codex_action(  # noqa: C901, PLR0912, PLR0913, PLR0915 - one-shot driver is naturally branchy and parameter-heavy; spec calls for 8 kwargs; splitting hurts readability
    *,
    task_goal: str,
    cwd: Path,
    timeout_s: float = 600.0,
    on_heartbeat: Callable[[dict[str, Any]], None] | None = None,
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
       capture the structured arguments. On 30 s wall-clock idle, fire
       ``on_heartbeat``. On deadline, send ``turn/interrupt`` and bail.
    6. Capture ``git diff`` text (no artifact write — Step 8's concern).
    7. ``close(timeout=3)``.

    Args:
        task_goal: The natural-language task description for the worker.
        cwd: The repo root the Codex worker runs in (becomes the sandbox
            writable_root and the ``thread/start`` ``cwd`` parameter).
        timeout_s: Wall-clock budget for the turn; on overrun the driver
            interrupts and returns with ``error="codex_turn_timeout"``.
        on_heartbeat: Optional callable invoked every 30 s of idle time
            with a summary dict (``summary``, ``elapsed_ms``,
            ``last_item_summary``). Step 10's ``spawn_worker_handler``
            uses this to emit ``worker.heartbeat`` events.
        codex_bin: Path or PATH-name of the ``codex`` CLI binary.
        model: ``-c model=<name>`` flag value.
        reasoning_effort: ``-c model_reasoning_effort=<level>`` flag value.
        env: Optional environment overrides. ``RUST_LOG=warn`` is set if
            absent (matches Hermes' default). ``CODEX_HOME`` is set to a
            per-spawn empty temp dir if absent, so user-local Codex
            config (``~/.codex/AGENTS.md``) cannot contaminate the
            worker (P-0009). Pre-set ``CODEX_HOME`` to override.

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
    # (P-0009). If the caller pre-set CODEX_HOME on ``env`` we respect
    # that and skip the temp dir; otherwise we create one and tear it
    # down in the result-finalize path.
    codex_home_dir: str | None = None
    if env_dict.get("CODEX_HOME") in (None, ""):
        codex_home_dir = tempfile.mkdtemp(prefix=_CODEX_HOME_PREFIX)
        env_dict["CODEX_HOME"] = codex_home_dir
        # Codex 0.130 responses_websocket reads auth from
        # $CODEX_HOME/auth.json, not OPENAI_API_KEY (B-0004 live-verified:
        # empty home -> 401 on every request). Seed the isolated dir so
        # the worker can reach api.openai.com.
        source_auth = Path("~/.codex/auth.json").expanduser()
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

    start_mono = time.monotonic()
    client = CodexAppServerClient(codex_bin=codex_bin, extra_args=extra_args, env=env_dict)

    final_text_parts: list[str] = []
    submit_report_calls: list[Mapping[str, Any]] = []
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

        notif = client.take_notification(timeout=_POLL_INTERVAL_S)
        if notif is None:
            # No notification this tick — check heartbeat cadence.
            if on_heartbeat is not None and (now - last_heartbeat_at) >= _HEARTBEAT_INTERVAL_S:
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

        if method == "item/tool_call":
            tool_name = _extract_tool_name(params)
            if tool_name == "submit_report":
                submit_report_calls.append(_extract_tool_arguments(params))
            last_item_summary = f"tool_call:{tool_name or 'unknown'}"

        elif method.startswith("item/"):
            text = _extract_text_item(params)
            if text:
                final_text_parts.append(text)
            last_item_summary = method

        elif method == "turn/completed":
            tokens_in, tokens_out = _extract_usage(params)
            turn_id_out = _extract_turn_id(params)
            break

        # Other notifications (server-initiated requests, unknown methods) are
        # surfaced via the client's separate queues; this driver ignores them
        # since Step 7 does not implement approval bridging.

    return _result(error=error, interrupted=interrupted, thread_id=thread_id)
