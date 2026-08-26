"""Command-line entry point — ``python -m jarvis "<utterance>"``.

Day-2 rewrite per ADR-0002 § Daemon / CLI contract (lines 1046-1098):

- Parent process runs a cheap, LLM-free regex classifier on the utterance.
- Long-run match: print the ack BEFORE the fork, ``sys.stdout.flush()``,
  then :func:`jarvis.runtime.daemon.fork_detach`; parent ``os._exit(0)``
  and the child re-bootstraps the runtime, installs the macOS power
  observer, runs the turn, and delivers the surface.
- Non-long-run match: synchronous Day-1 path (bootstrap → run_turn →
  exit) preserved verbatim so existing tests continue to pass.

Hard rules (canary ``test_canary_daemon_ack_before_fork``):

- The parent process MUST NOT open SQLite. The regex classifier is
  purely textual.
- The ack MUST be printed AND ``sys.stdout`` flushed BEFORE
  :func:`fork_detach` so the operator sees Jarvis acknowledged the
  request even if the detached child later crashes.
- The child re-bootstraps via :func:`bootstrap_runtime_app` (which is
  the ONLY allowed sqlite-open site) and installs
  :func:`jarvis.deployment.sleep_wake.install_power_observer` before
  running the turn (spec §3.7.8 — child owns the power observer).

ADR-0009 D1 adds ``jarvis daemon install|uninstall|status``. Those verbs
are deliberately THIN: every path, plist key, and ``launchctl``
argument lives in :mod:`jarvis.deployment.launchd`, which is what keeps
canary H8 (the runtime-root literal stays inside ``jarvis/deployment/``)
green. This module only maps results to stdout and exit codes.

Layer rules: ``jarvis.cli`` imports ``jarvis.runtime`` (and via that,
the whole stack). It MUST NOT import the middle-layer siblings
directly — ``.importlinter`` keeps cli above runtime, runtime above
the four siblings. ``jarvis.deployment.launchd`` / ``process_lock`` are
the documented exception: cli sits above deployment too, and the lock
probe + daemon verbs are cli-owned operator surface.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

from jarvis.deployment import DEFAULT_RUNTIME_ROOT_LITERAL, launchd, process_lock
from jarvis.deployment.process_lock import ProcessLockHeld
from jarvis.runtime import (
    PreEmitTokenError,
    RuntimeBootstrapError,
    TriggerWaitTimeout,
    bootstrap_runtime_app,
    parse_response_channels,
    run_turn,
)
from jarvis.runtime.inherent_loop import serve_inherent

LOGGER = logging.getLogger("jarvis.cli")

_PROG = "python -m jarvis"
_DESC = (
    "Allen's state-centric personal runtime — Day-2 fork-detach CLI. "
    "Long-run utterances ack then fork-detach; synchronous utterances "
    "run inline as in Day-1. Pass an utterance string; Jarvis emits "
    "surface.user_intent, drives the decide() loop, and writes the "
    "document-channel response to stdout."
)

# Cheap LLM-free classifier — pinned by ADR-0002 line 1056. The regex
# is the SINGLE source of truth for what makes an utterance "long-run"
# enough to warrant ack-then-fork; do NOT extend or refactor without
# revising the ADR.
_LONG_RUN_RE: re.Pattern[str] = re.compile(
    r"跑|spawn|给 *codex|帮我做|帮我跑|做一下|审核|run",
    re.IGNORECASE,
)

# Fixed ack template per Day-2 § Daemon / CLI contract. Day-3 may
# personalize per match (the ack is intentionally a single Chinese
# sentence so Allen's CLI transcripts grep cleanly) but Day-2 pins
# the single phrase so the canary can assert ack-before-fork without
# parsing variants. RUF001 flags the fullwidth comma inside the
# Chinese phrase as an "ambiguous" Latin look-alike; the character
# is deliberate Chinese punctuation here.
_QUICK_ACK_PHRASE: str = "好的，跑起来了。"  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.

# --- ADR-0009 D2: forward mode -------------------------------------------
#
# The daemon binds one address; the CLI is a thin client over the same
# wire the inherent-swift app speaks.
_DAEMON_HOST = "127.0.0.1"
_DAEMON_PORT = 8006

# Contract table (D2). Connection-refused means "lock acquired, uvicorn
# not yet bound" — `acquire_exclusive` runs before bind with the voice
# preflight in between, and a launchd respawn re-opens that window every
# ThrottleInterval (~10s). Three tries a second apart covers it without
# hanging a shell.
_FORWARD_ATTEMPTS = 3
_FORWARD_RETRY_SLEEP_S = 1.0
_FORWARD_DEFAULT_TIMEOUT_S = 120.0
_WS_OPEN_TIMEOUT_S = 5.0
_POST_TIMEOUT_S = 10.0

_EXIT_REFUSED = 2
_EXIT_DAEMON_UNREACHABLE = 3
_EXIT_RESPONSE_TIMEOUT = 4


def _utterance_implies_long_run(utterance: str) -> bool:
    """Day-2 classifier: regex keyword match only. NO SQLite open.

    NO task-bound check — the resolver runs inside :func:`run_turn`,
    AFTER the fork. The classifier is intentionally cheap so the parent
    process holds nothing but argv parsing + a regex match + the ack
    print + the fork call (ADR-0002 § Daemon / CLI contract hard
    rules).
    """
    return bool(_LONG_RUN_RE.search(utterance))


def _quick_ack_phrase(utterance: str) -> str:
    """Cheap ack rendered from the regex match. Day-2: fixed template."""
    # The utterance is consulted only via the classifier; the Day-2
    # phrase is constant so future-Allen can grep the ack out of
    # transcripts without parsing variants.
    del utterance
    return _QUICK_ACK_PHRASE


class _DaemonUnreachableError(RuntimeError):
    """The daemon holds the lock (or the agent is installed) but nothing is bound."""


class _SubmitRejectedError(RuntimeError):
    """The daemon answered the POST with an error status — not a retry case."""


class _ResponseTimeoutError(RuntimeError):
    """No terminal response envelope arrived before the deadline.

    Carries the ``turn_id`` so the operator can find the turn later: the
    daemon keeps running it, so this is a client-side give-up, not a
    cancellation.
    """

    def __init__(self, turn_id: str) -> None:
        """Store the ``turn_id`` the operator needs to find the turn later."""
        super().__init__(f"no response envelope for turn {turn_id or '<unknown>'}")
        self.turn_id = turn_id


def _post_submit(url: str, utterance: str) -> str:
    """POST the utterance to ``/inherent/submit``; return the minted ``turn_id``.

    stdlib ``urllib`` on purpose — ADR-0009 adds zero runtime
    dependencies, and the CLI must stay importable on a machine where
    the daemon's optional extras are missing.

    Raises:
        _DaemonUnreachableError: Nothing is listening yet (the lock is
            taken before uvicorn binds).
    """
    payload = json.dumps({"text": utterance}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 — fixed http://127.0.0.1 URL built above.
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 — same fixed loopback URL.
            request, timeout=_POST_TIMEOUT_S
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except ConnectionRefusedError as exc:
        raise _DaemonUnreachableError(str(exc)) from None
    except urllib.error.HTTPError as exc:
        # The daemon answered but rejected the submit (400 empty text, 500
        # inside submit_callable). Not a retry case — surface it verbatim.
        msg = f"daemon rejected the submit: HTTP {exc.code} {exc.reason}"
        raise _SubmitRejectedError(msg) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            raise _DaemonUnreachableError(str(exc.reason)) from None
        msg = f"cannot reach the daemon: {exc.reason}"
        raise _SubmitRejectedError(msg) from None
    if not isinstance(body, dict):
        return ""
    return str(body.get("turn_id", "") or "")


def _envelope_opens_our_turn(
    payload: Mapping[str, object],
    *,
    utterance: str,
    turn_id: str,
) -> bool:
    """Decide whether an ``open`` envelope belongs to the turn we submitted.

    Two keys, in order of trust:

    1. ``payload["turn_id"]`` when both sides have one — exact
       correlation, the D2 contract, and since the wire completion the
       normal path: ``POST /inherent/submit`` returns the minted id and
       every ``open`` / ``append`` / ``done`` envelope carries it.
    2. ``payload["q"]`` (the response header's query text, which is the
       submitted transcript) otherwise. Retained for a daemon on the
       pre-completion wire, and inherently ambiguous when two turns
       carry byte-identical text — which is exactly why (1) exists.
    """
    envelope_turn = str(payload.get("turn_id", "") or "")
    if turn_id and envelope_turn:
        return envelope_turn == turn_id
    return str(payload.get("q", "") or "") == utterance


async def _collect_response(
    queue: asyncio.Queue[dict[str, object]],
    *,
    utterance: str,
    turn_id: str,
    timeout_s: float,
) -> str:
    """Filter the buffered + live envelope stream down to our turn's text.

    Consumes ``open`` → ``append``* → ``done``. Envelopes that arrived
    before the POST returned are already in ``queue`` (the reader task
    starts before the POST — that ordering is the whole point of D2),
    so a turn fast enough to open before the HTTP response is read is
    still matched.

    Raises:
        _ResponseTimeoutError: Deadline hit before ``done``.
    """
    deadline = time.monotonic() + timeout_s
    matched = False
    parts: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ResponseTimeoutError(turn_id)
        try:
            envelope = await asyncio.wait_for(queue.get(), timeout=remaining)
        except TimeoutError:
            raise _ResponseTimeoutError(turn_id) from None
        op = envelope.get("op")
        raw_payload = envelope.get("payload")
        payload: Mapping[str, object] = raw_payload if isinstance(raw_payload, dict) else {}
        if not matched:
            if op == "open":
                matched = _envelope_opens_our_turn(
                    payload, utterance=utterance, turn_id=turn_id
                )
            continue
        if op == "append":
            parts.append(str(payload.get("token", "") or ""))
        elif op == "done":
            return "".join(parts)


async def _drain_ws(ws: object, queue: asyncio.Queue[dict[str, object]]) -> None:
    """Push every decodable WS envelope into ``queue``.

    Started BEFORE the POST so nothing that lands between the submit and
    the first read is lost — the daemon has no late-subscriber replay
    (ADR-0003 deferred the replay queue deliberately).
    """
    async for raw in ws:  # type: ignore[attr-defined]
        with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError):
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                queue.put_nowait(decoded)


async def _forward_attempt(utterance: str, *, timeout_s: float) -> str:
    """One WS-connect → POST → collect cycle. Returns the response text."""
    from websockets.asyncio.client import connect  # noqa: PLC0415 — keeps CLI import cheap.

    ws_url = f"ws://{_DAEMON_HOST}:{_DAEMON_PORT}/inherent/ws"
    post_url = f"http://{_DAEMON_HOST}:{_DAEMON_PORT}/inherent/submit"

    # WS FIRST. Reversing these two lines loses the response on any turn
    # that finishes before the POST's HTTP response is read.
    async with connect(ws_url, open_timeout=_WS_OPEN_TIMEOUT_S) as ws:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        reader = asyncio.create_task(_drain_ws(ws, queue))
        try:
            turn_id = await asyncio.to_thread(_post_submit, post_url, utterance)
            return await _collect_response(
                queue, utterance=utterance, turn_id=turn_id, timeout_s=timeout_s
            )
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader


def _stdout_text(text: str) -> str:
    """Project the daemon's response onto the document channel.

    ``cli_stdout`` is a document-channel surface: the pre-0009 direct
    path reached it through
    :func:`jarvis.surface.cli_render.render_response`, which writes
    ``document_text``. Forward mode reassembles the raw chunk stream
    instead, and that stream still carries the
    ``<voice>``/``<document>`` markup the render layer would have
    split — printing it verbatim shows an operator both halves plus
    the tags, with the document half's line breaks flattened.

    Parsing happens here, after ``_collect_response`` has joined every
    ``append`` token, because a tag may straddle any chunk boundary.

    A response with no tags at all comes back whole (the parser
    returns it as both channels). A voice-only response falls back to
    the voice half rather than printing an empty line — an exit-0
    one-shot that emits nothing reads as a lost answer.
    """
    channels = parse_response_channels(text)
    return channels.document or channels.voice


async def _forward(utterance: str, *, timeout_s: float) -> int:
    """D2 forward mode with the pinned retry / exit-code contract."""
    last_error = ""
    for attempt in range(1, _FORWARD_ATTEMPTS + 1):
        try:
            text = await _forward_attempt(utterance, timeout_s=timeout_s)
        except (ConnectionRefusedError, _DaemonUnreachableError) as exc:
            last_error = str(exc)
            if attempt < _FORWARD_ATTEMPTS:
                await asyncio.sleep(_FORWARD_RETRY_SLEEP_S)
            continue
        except _ResponseTimeoutError as exc:
            sys.stderr.write(
                f"jarvis: no response within {timeout_s:.0f}s. The turn is still "
                f"running in the daemon; turn_id={exc.turn_id or '<unknown>'}. "
                "Raise --timeout or inspect the event log.\n"
            )
            return _EXIT_RESPONSE_TIMEOUT
        except _SubmitRejectedError as exc:
            sys.stderr.write(f"jarvis: {exc}\n")
            return 1
        else:
            print(_stdout_text(text))  # noqa: T201 — the daemon's response IS this command's output.
            return 0

    sys.stderr.write(
        f"jarvis: daemon starting or hung ({last_error}); tried "
        f"{_FORWARD_ATTEMPTS}x. Try again, or run `jarvis daemon status`.\n"
    )
    return _EXIT_DAEMON_UNREACHABLE


def _forward_to_daemon(utterance: str, *, timeout_s: float) -> int:
    """Sync wrapper — the one-shot CLI has no running event loop."""
    return asyncio.run(_forward(utterance, timeout_s=timeout_s))


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for ``python -m jarvis``."""
    parser = argparse.ArgumentParser(prog=_PROG, description=_DESC)
    parser.add_argument(
        "utterance",
        help="User utterance text (one positional). Wrap in quotes if it contains spaces.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to jarvis.yaml (default: ${repo}/config/jarvis.yaml).",
    )
    parser.add_argument(
        "--prompt",
        type=Path,
        default=None,
        help="Path to the system prompt markdown (default: ${repo}/prompts/jarvis_v1.md).",
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help=(
            "Override JARVIS_RUNTIME_ROOT for this invocation (default: "
            f"$JARVIS_RUNTIME_ROOT or {DEFAULT_RUNTIME_ROOT_LITERAL})."
        ),
    )
    parser.add_argument(
        "--no-detach",
        action="store_true",
        default=False,
        help=(
            "Force synchronous Day-1 path even for long-run utterances. "
            "Used by smoke tests; production callers omit this flag."
        ),
    )
    parser.add_argument(
        "--no-forward",
        action="store_true",
        default=False,
        help=(
            "Do not forward to a running daemon (ADR-0009 D2). Restores "
            "the pre-0009 exit-2 refusal when the daemon holds the lock; "
            "scripts that depend on that refusal pass this flag."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_FORWARD_DEFAULT_TIMEOUT_S,
        help=(
            "Seconds to wait for the forwarded turn's response before "
            f"giving up with exit 4 (default: {_FORWARD_DEFAULT_TIMEOUT_S:.0f}). "
            "The turn keeps running inside the daemon either way."
        ),
    )
    return parser


def main_with_detach(
    utterance: str,
    *,
    config_path: Path | None = None,
    prompt_path: Path | None = None,
    runtime_root: Path | None = None,
    no_detach: bool = False,
) -> int:
    """Day-2 entry: classify, ack-then-fork or run synchronously.

    Long-run path (classifier matched, ``no_detach`` false):

    1. Print :data:`_QUICK_ACK_PHRASE`, flush stdout.
    2. Call :func:`jarvis.runtime.daemon.fork_detach`.
    3. Parent: ``os._exit(0)`` immediately (no SQLite to close — none
       was opened).
    4. Child: re-bootstrap runtime via :func:`bootstrap_runtime_app`
       (the ONLY sqlite-open site in this CLI), install the macOS
       power observer (spec §3.7.8), run the turn, return 0.

    Synchronous path (classifier did not match, ``no_detach``, or the
    LaunchAgent is installed): Day-1 behavior preserved — bootstrap,
    run_turn, return 0.

    ADR-0009 D2 adds the third condition. With the agent installed, a
    detached child would race the launchd-respawned daemon: the child's
    actions live in ITS process's live-action set, so the daemon's
    ``_system_trigger_watcher`` sees the child's terminal rows as
    orphans and drives a second turn for them. No fork means no foreign
    live turn, which is why the guard is in the branch condition rather
    than in the caller alone.

    Args:
        utterance: User text.
        config_path: Optional explicit jarvis.yaml path.
        prompt_path: Optional explicit system prompt path.
        runtime_root: Optional override for the L6 runtime root.
        no_detach: Force synchronous path even on classifier match.
            Smoke tests use this to avoid the daemonization.

    Returns:
        Exit code: 0 on success, 1 on runtime errors.
    """
    # Classifier — purely textual, NO IO. Canary
    # ``test_canary_daemon_ack_before_fork`` enforces that no
    # ``bootstrap_runtime_app`` call appears in the parent before
    # ``fork_detach``.
    if (
        _utterance_implies_long_run(utterance)
        and not no_detach
        and not launchd.is_agent_installed()
    ):
        # Print ack BEFORE fork (canary `test_canary_daemon_ack_before_fork`
        # / ADR-0002 lines 1632-1635). `print` is the operator's visible
        # acknowledgement on stdout — using sys.stderr or logging would
        # not survive the daemonized child's /dev/null redirect, but
        # this print happens in the PARENT before fork, so it lands on
        # the operator terminal correctly. T201 is silenced because
        # `print` is the contract here, not an accidental debug stub.
        print(_quick_ack_phrase(utterance))  # noqa: T201 — operator ack per ADR-0002 § Daemon / CLI contract.
        sys.stdout.flush()

        # Fork-detach. Lazy import keeps the classifier import path
        # (and therefore unit-test imports) free of POSIX-fork wiring
        # on non-darwin runners.
        from jarvis.runtime.daemon import fork_detach  # noqa: PLC0415 — see docstring above.

        if fork_detach() == "parent":
            # No SQLite to close — parent never opened it.
            os._exit(0)

        # --- Child path -----------------------------------------------------
        #
        # The grandchild now has cwd=/, stdin/stdout/stderr → /dev/null
        # (per :func:`fork_detach`). Bootstrap re-opens SQLite in THIS
        # process; the connection never crosses the fork.
        from jarvis.deployment.sleep_wake import install_power_observer  # noqa: PLC0415

        return _child_run(
            utterance,
            config_path=config_path,
            prompt_path=prompt_path,
            runtime_root=runtime_root,
            install_observer=install_power_observer,
        )

    # Synchronous Day-1 path -------------------------------------------------
    return _sync_run(
        utterance,
        config_path=config_path,
        prompt_path=prompt_path,
        runtime_root=runtime_root,
    )


def _sync_run(
    utterance: str,
    *,
    config_path: Path | None,
    prompt_path: Path | None,
    runtime_root: Path | None,
) -> int:
    """Synchronous Day-1 path: bootstrap → run_turn → close → return.

    Carved out as a helper so :func:`main_with_detach` and the legacy
    :func:`main` argparse entry share the same body (and so the long-
    run vs. synchronous branches both go through a single close-on-exit
    site).
    """
    try:
        runtime = bootstrap_runtime_app(
            config_path=config_path,
            prompt_path=prompt_path,
            runtime_root=runtime_root,
        )
    except RuntimeBootstrapError as exc:
        sys.stderr.write(f"jarvis: bootstrap failed: {exc}\n")
        return 1
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"jarvis: bootstrap failed: {exc}\n")
        return 1

    try:
        run_turn(runtime, utterance=utterance)
    except PreEmitTokenError as exc:
        sys.stderr.write(f"jarvis: pre-emit token mismatch: {exc}\n")
        return 1
    except TriggerWaitTimeout as exc:
        sys.stderr.write(f"jarvis: trigger wait timed out: {exc}\n")
        return 1
    except RuntimeBootstrapError as exc:
        sys.stderr.write(f"jarvis: runtime error: {exc}\n")
        return 1
    finally:
        try:
            runtime.conn.close()
        except OSError:
            LOGGER.exception("failed to close Event Log connection on exit")

    return 0


def _child_run(
    utterance: str,
    *,
    config_path: Path | None,
    prompt_path: Path | None,
    runtime_root: Path | None,
    install_observer: object,  # Callable[[sqlite3.Connection], PowerObserver]
) -> int:
    """Child-process body: bootstrap, install observer, run turn.

    Mirrors :func:`_sync_run` but additionally installs the macOS power
    observer (spec §3.7.8 — every detached child owns its own observer).
    ``install_observer`` is parameter-injected so unit tests of the
    child path can pass a stub factory without monkeypatching the
    sleep_wake module.
    """
    try:
        runtime = bootstrap_runtime_app(
            config_path=config_path,
            prompt_path=prompt_path,
            runtime_root=runtime_root,
        )
    except RuntimeBootstrapError as exc:
        # stderr is /dev/null in the daemonized child, but emit anyway
        # for the no_detach smoke path that wires this helper in-process.
        sys.stderr.write(f"jarvis: bootstrap failed (child): {exc}\n")
        return 1
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"jarvis: bootstrap failed (child): {exc}\n")
        return 1

    # Install the power observer — child only (spec §3.7.8).
    try:
        install_observer(runtime.conn)  # type: ignore[operator]
    except (RuntimeError, OSError, ValueError):
        LOGGER.exception("failed to install power observer in child")
        # Continue — observer is best-effort; the turn must still run.

    try:
        run_turn(runtime, utterance=utterance)
    except PreEmitTokenError as exc:
        sys.stderr.write(f"jarvis: pre-emit token mismatch (child): {exc}\n")
        return 1
    except TriggerWaitTimeout as exc:
        sys.stderr.write(f"jarvis: trigger wait timed out (child): {exc}\n")
        return 1
    except RuntimeBootstrapError as exc:
        sys.stderr.write(f"jarvis: runtime error (child): {exc}\n")
        return 1
    finally:
        try:
            runtime.conn.close()
        except OSError:
            LOGGER.exception("failed to close Event Log connection on exit (child)")

    return 0


def _resolve_runtime_root(arg: Path | None) -> Path:
    """Pick the runtime root the same way :func:`jarvis.deployment.bootstrap_runtime` does.

    Explicit arg > ``JARVIS_RUNTIME_ROOT`` env var > built-in default.
    Mirrors :func:`jarvis.deployment._resolve_root` BUT without creating
    any directories — the lock probe must be a pure read so a missing
    runtime root doesn't get materialized as a side effect of running
    ``jarvis "hello"`` in a fresh shell.

    Args:
        arg: ``--runtime-root`` argparse value (None if not passed).

    Returns:
        Resolved absolute path (``expanduser().resolve()``); does NOT
        create the directory.
    """
    if arg is not None:
        return arg.expanduser().resolve()
    env_value = os.environ.get("JARVIS_RUNTIME_ROOT")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return Path(DEFAULT_RUNTIME_ROOT_LITERAL).expanduser().resolve()


def _main_serve(argv: list[str]) -> int:
    """Parse serve-mode args, bootstrap runtime, call :func:`serve_inherent`.

    Owns the daemon process lifecycle (ADR-0003 Step 1). The
    ``acquire_exclusive`` call is INSIDE ``serve_inherent`` so the lock
    file is the OUTERMOST scope of the daemon's resource ladder.

    Returns:
        0 on clean shutdown, 1 on bootstrap failure, 2 if another live
        daemon already owns the per-runtime-root lock.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} serve",
        description="Run the Inherent (text-only) daemon on localhost:8006 by default.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address.")
    parser.add_argument("--port", type=int, default=8006, help="Bind port.")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help=(
            "Override JARVIS_RUNTIME_ROOT (default: "
            f"$JARVIS_RUNTIME_ROOT or {DEFAULT_RUNTIME_ROOT_LITERAL})."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to jarvis.yaml (default: ${repo}/config/jarvis.yaml).",
    )
    parser.add_argument(
        "--prompt",
        type=Path,
        default=None,
        help="Path to system prompt markdown.",
    )
    parser.add_argument(
        "--force-manual",
        action="store_true",
        default=False,
        help=(
            "Serve manually even though the LaunchAgent is installed. "
            "Without this the command refuses (ADR-0009 D1) because a "
            "manual daemon fights the launchd respawn for the same lock."
        ),
    )
    args = parser.parse_args(argv)

    # ADR-0009 D1 — a manual serve while the agent is installed loses the
    # lock race against launchd's KeepAlive respawn (and vice versa). The
    # plist's presence is the signal; refuse unless explicitly forced.
    #
    # ...unless WE are the agent. launchd execs this exact argv, so the
    # plist-presence signal is true for its own child too; without the
    # marker check the daemon refuses itself and KeepAlive retries the
    # refusal every ThrottleInterval, forever.
    if launchd.is_agent_installed() and not args.force_manual and not launchd.spawned_by_agent():
        sys.stderr.write(
            f"jarvis serve: LaunchAgent {launchd.AGENT_LABEL} installed "
            f"({launchd.plist_path()}); manual serve will fight respawn — "
            "run `jarvis daemon uninstall` first, or pass --force-manual.\n"
        )
        return 2

    try:
        runtime = bootstrap_runtime_app(
            config_path=args.config,
            prompt_path=args.prompt,
            runtime_root=args.runtime_root,
        )
    except RuntimeBootstrapError as exc:
        sys.stderr.write(f"jarvis serve: bootstrap failed: {exc}\n")
        return 1

    lock_path = runtime.runtime_paths.root / "daemon.lock"

    try:
        asyncio.run(
            serve_inherent(
                runtime,
                host=args.host,
                port=args.port,
                lock_path=lock_path,
            )
        )
    except ProcessLockHeld as exc:
        sys.stderr.write(f"jarvis serve: daemon already running at pid {exc.holder_pid}\n")
        return 2
    except KeyboardInterrupt:
        # SIGINT during serve — serve_inherent's finally block cancels
        # watchers and releases the lock; we just surface the interrupt
        # to the operator and exit cleanly.
        sys.stderr.write("jarvis serve: interrupted\n")
    finally:
        # Single close site (sqlite3.Connection.close raises ProgrammingError
        # if called twice; suppress only the documented OSError path that
        # the synchronous helpers above also catch).
        try:
            runtime.conn.close()
        except OSError:
            LOGGER.exception("failed to close Event Log connection on serve exit")

    return 0


def _render_launchctl_step(step: launchd.LaunchctlResult) -> str:
    """One ``launchctl`` line for the install / uninstall report."""
    return f"  launchctl   : {' '.join(step.argv)} -> exit {step.returncode}"


def _daemon_install() -> str:
    """Run :func:`launchd.install` and render its result for the operator."""
    result = launchd.install()
    written = "written" if result.plist_changed else "unchanged (idempotent re-install)"
    lines = [
        f"jarvis daemon install: {launchd.service_target()} bootstrapped",
        f"  plist       : {result.plist_path} ({written})",
        f"  interpreter : {result.interpreter}",
        f"  logs        : {result.logs_dir}",
    ]
    lines.extend(_render_launchctl_step(step) for step in result.steps)
    return "\n".join(lines)


def _daemon_uninstall(*, keep_plist: bool) -> str:
    """Run :func:`launchd.uninstall` and render its result for the operator."""
    result = launchd.uninstall(remove_plist=not keep_plist)
    fate = "removed" if result.plist_removed else "left in place"
    lines = [
        f"jarvis daemon uninstall: {launchd.service_target()} booted out",
        f"  plist       : {result.plist_path} ({fate})",
    ]
    lines.extend(_render_launchctl_step(step) for step in result.steps)
    return "\n".join(lines)


def _main_daemon(argv: list[str]) -> int:
    """``jarvis daemon install|uninstall|status`` — ADR-0009 D1 verbs.

    Thin by construction: :mod:`jarvis.deployment.launchd` owns every
    path, plist key, and ``launchctl`` argument; this function only
    parses argv, prints, and maps failures to exit codes.

    Returns:
        0 on success, 1 on any :class:`launchd.LaunchdError` (bad
        interpreter, no GUI session, failed ``launchctl`` step).
    """
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} daemon",
        description=(
            "Manage the com.allen.jarvis LaunchAgent (ADR-0009 D1). "
            "install is idempotent; uninstall stops the job regardless of "
            "KeepAlive; status combines launchctl, the daemon lock, the "
            "interpreter check, and log sizes."
        ),
    )
    parser.add_argument("verb", choices=("install", "uninstall", "status"))
    parser.add_argument(
        "--keep-plist",
        action="store_true",
        default=False,
        help="uninstall only: stop the job but leave the plist on disk.",
    )
    args = parser.parse_args(argv)

    try:
        if args.verb == "install":
            report = _daemon_install()
        elif args.verb == "uninstall":
            report = _daemon_uninstall(keep_plist=args.keep_plist)
        else:
            report = launchd.format_status(launchd.status())
    except launchd.LaunchdError as exc:
        sys.stderr.write(f"jarvis daemon {args.verb}: {exc}\n")
        return 1

    print(report)  # noqa: T201 — operator-facing report is this verb's whole output.
    return 0


def _main_oneshot(argv: list[str]) -> int:
    """Existing one-shot flow with the ADR-0003 D4 lock-probe prepended.

    The probe runs BEFORE :func:`main_with_detach` so the daemon-refusal
    path fires before the regex classifier / fork-detach machinery —
    otherwise a long-run utterance would fork-detach into a child that
    then races the running daemon on the same SQLite event log.

    B-NEW-5: additionally fail-fast when the user passes
    ``--runtime-root <other>`` while a daemon holds the default (home)
    runtime root's lock. Without this guard the one-shot CLI silently
    forks into a parallel SQLite state — the daemon and the ad-hoc
    run drive separate event logs and the operator only sees one of
    them. The check uses the same :func:`process_lock.is_held` machinery
    as the D4 probe but targets the default-root lock specifically.

    ADR-0009 D2 turns the D4 refusal into a forward: when the lock is
    held OR the LaunchAgent is installed, this process becomes a thin
    client of the daemon. Order matters — **B-NEW-5 is evaluated before
    the forward decision** so a cross-root invocation still refuses
    instead of being answered out of the wrong event log. The requested
    root's own lock is still probed first so the probe order the D4
    tests assert on is unchanged.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # ADR-0003 D4 / F2 — probe the requested root's lock first (kept
    # first so the ordering the D4 tests observe does not change).
    requested_root = _resolve_runtime_root(args.runtime_root)
    lock_path = requested_root / "daemon.lock"
    lock_held = process_lock.is_held(lock_path)

    # B-NEW-5 — when the user explicitly overrides the runtime root but
    # a daemon already owns the DEFAULT (home) runtime root, refuse with
    # a clear error rather than silently forking into a parallel state.
    # Forwarding would be worse than forking here: the answer would come
    # out of the daemon's event log, not the one the operator asked for.
    home_root = Path(DEFAULT_RUNTIME_ROOT_LITERAL).expanduser().resolve()
    if requested_root != home_root:
        home_lock = home_root / "daemon.lock"
        if process_lock.is_held(home_lock):
            pid = process_lock.holder_pid(home_lock)
            sys.stderr.write(
                f"jarvis: daemon at {home_root} holds the default runtime "
                f"root (pid {pid}); --runtime-root {requested_root} would "
                f"create a conflicting parallel state. Stop the daemon or "
                f"omit --runtime-root.\n"
            )
            return _EXIT_REFUSED

    if args.no_forward:
        # Pre-0009 behavior, verbatim — scripts match on this string.
        if lock_held:
            pid = process_lock.holder_pid(lock_path)
            sys.stderr.write(
                f"jarvis: daemon running at pid {pid}; "
                f"stop it or POST to http://127.0.0.1:8006/inherent/submit\n"
            )
            return _EXIT_REFUSED
    elif lock_held or launchd.is_agent_installed():
        # D2 — thin client. The agent-installed half also covers the
        # ThrottleInterval respawn window, where the lock is momentarily
        # free but a daemon is about to own it again.
        return _forward_to_daemon(args.utterance, timeout_s=args.timeout)

    return main_with_detach(
        args.utterance,
        config_path=args.config,
        prompt_path=args.prompt,
        runtime_root=args.runtime_root,
        no_detach=args.no_detach,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Routes ``serve`` to the daemon; otherwise to one-shot.

    Dispatch is purely positional: ``serve`` as the first argv element
    routes to :func:`_main_serve`, ``daemon`` to :func:`_main_daemon`
    (ADR-0009 D1); everything else goes through :func:`_main_oneshot`
    (which preserves the Day-1 argparse contract so existing tests in
    ``tests/unit/test_cli_main.py`` keep passing).

    Args:
        argv: argv-style list (without the program name). Default:
            ``sys.argv[1:]``.

    Returns:
        Exit code: 0 on a clean turn, 1 on bootstrap / runtime errors,
        2 on argument-parsing failures (argparse default) or on a
        daemon-lock refusal (ADR-0003 D4).
    """
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "serve":
        return _main_serve(argv[1:])
    if argv and argv[0] == "daemon":
        return _main_daemon(argv[1:])
    return _main_oneshot(argv)


__all__ = [
    "main",
    "main_with_detach",
]
