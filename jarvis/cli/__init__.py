"""Command-line entry point — ``python -m jarvis "<utterance>"`` hands one turn to a brain.

The one-shot command is a thin client (ADR 0341). With ``--brain URL`` the turn goes to that
brain; without it, to the daemon on this machine, found by the held ``daemon.lock`` of the runtime
root or by the installed LaunchAgent (ADR-0009 D2). It never assembles a runtime of its own: with
no brain to hand the turn to it exits non-zero and says to pass ``--brain``.

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
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

from jarvis.deployment import (
    DEFAULT_RUNTIME_ROOT_LITERAL,
    RuntimePaths,
    bootstrap_runtime,
    daemon_session,
    launchd,
    load_env_file,
    process_lock,
)
from jarvis.deployment.models import default_meaning_dir, fetch_meaning_model
from jarvis.deployment.process_lock import ProcessLockHeld
from jarvis.runtime import parse_response_channels
from jarvis.runtime.bootstrap.app import bootstrap_runtime_app
from jarvis.runtime.bootstrap.core import load_runtime_env_and_trace
from jarvis.runtime.commands import import_exports, mcp_login, open_voiceprints
from jarvis.runtime.config import (
    RuntimeBootstrapError,
    RuntimeConfig,
    load_runtime_config,
    system_language,
)
from jarvis.runtime.inherent_loop import serve_inherent
from jarvis.runtime.terminal import run_terminal
from jarvis.shared.lang import Language, t
from jarvis.shared.text import is_english
from jarvis.state import NewerDataError
from jarvis.state.device_tokens import (
    DeviceTokenError,
    pair_device,
    paired_devices,
    unpair_device,
)
from jarvis.state.log_invariants import (
    DEFAULT_EXAMPLES,
    DEFAULT_SINCE_DAYS,
    EVENT_LOG_FILENAME,
    MAX_SINCE_DAYS,
    LogCheckError,
    LogReport,
    check_event_log,
    iso,
)
from jarvis.state.plugin_settings import local_key
from jarvis.state.voiceprint import seed_study

LOGGER = logging.getLogger("jarvis.cli")

_PROG = "python -m jarvis"
# `serve` exits with this when a database was written by a newer Jarvis (ADR 0068).
EXIT_NEWER_DATA = 3
_DESC = (
    "Hand one utterance to a brain and print its answer: the --brain URL given, else the "
    "daemon on this machine. This command runs no runtime of its own."
)

# --- ADR-0009 D2: forward mode -------------------------------------------
#
# The daemon binds one address; the CLI is a thin client over the same
# wire the inherent-swift app speaks.
_DAEMON_PORT = 8006
_DAEMON_URL = f"http://127.0.0.1:{_DAEMON_PORT}"

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

# The daemon, local or a brain on the tailnet, is never behind an HTTP proxy.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_EXIT_REFUSED = 2
_EXIT_DAEMON_UNREACHABLE = 3
_EXIT_RESPONSE_TIMEOUT = 4
_EXIT_TURN_FAILED = 5
_BRAIN_TOKEN_FILE = "brain-token"  # noqa: S105 — a file name, not a secret.


class _DaemonUnreachableError(RuntimeError):
    """The daemon holds the lock (or the agent is installed) but nothing is bound."""


class _SubmitRejectedError(RuntimeError):
    """The daemon answered the POST with an error status — not a retry case."""


class _TurnFailedError(RuntimeError):
    """The daemon ended our turn with ``failed`` and no answer (``turn.failed``)."""


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


def _post_submit(url: str, utterance: str, key: str) -> str:
    """POST the utterance to ``/inherent/submit``; return the minted ``turn_id``.

    stdlib ``urllib`` on purpose — ADR-0009 adds zero runtime
    dependencies, and the CLI must stay importable on a machine where
    the daemon's optional extras are missing.

    Raises:
        _DaemonUnreachableError: Nothing is listening yet (the lock is
            taken before uvicorn binds).
    """
    payload = json.dumps({"text": utterance}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 — an http(s) URL checked by the caller.
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with _DIRECT.open(request, timeout=_POST_TIMEOUT_S) as response:
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
        _TurnFailedError: The daemon ended the turn with ``failed``.
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
        if op == "failed" and turn_id and payload.get("turn_id") == turn_id:
            reason = str(payload.get("reason") or "error")
            message = str(payload.get("message") or "")
            raise _TurnFailedError(f"{message} ({reason})" if message else reason)
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


async def _forward_attempt(utterance: str, *, timeout_s: float, key: str, base_url: str) -> str:
    """One WS-connect → POST → collect cycle. Returns the response text."""
    from websockets.asyncio.client import connect  # noqa: PLC0415 — keeps CLI import cheap.
    from websockets.exceptions import InvalidStatus  # noqa: PLC0415 — same.

    ws_url = f"ws{base_url.removeprefix('http')}/inherent/ws"
    post_url = f"{base_url}/inherent/submit"

    # WS FIRST. Reversing these two lines loses the response on any turn
    # that finishes before the POST's HTTP response is read.
    try:
        async with connect(
            ws_url,
            open_timeout=_WS_OPEN_TIMEOUT_S,
            proxy=None,
            additional_headers={"Authorization": f"Bearer {key}"},
        ) as ws:
            queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            reader = asyncio.create_task(_drain_ws(ws, queue))
            try:
                turn_id = await asyncio.to_thread(_post_submit, post_url, utterance, key)
                return await _collect_response(
                    queue, utterance=utterance, turn_id=turn_id, timeout_s=timeout_s
                )
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
    except InvalidStatus as exc:
        # 403: the daemon runs from another runtime root, or the brain did not take the token.
        msg = f"daemon refused the connection: HTTP {exc.response.status_code}"
        raise _SubmitRejectedError(msg) from None


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


async def _forward(
    utterance: str, *, timeout_s: float, key: str, base_url: str = _DAEMON_URL
) -> int:
    """D2 forward mode with the pinned retry / exit-code contract."""
    last_error = ""
    for attempt in range(1, _FORWARD_ATTEMPTS + 1):
        try:
            text = await _forward_attempt(
                utterance, timeout_s=timeout_s, key=key, base_url=base_url
            )
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
        except _TurnFailedError as exc:
            sys.stderr.write(f"jarvis: the turn failed: {exc}\n")
            return _EXIT_TURN_FAILED
        else:
            print(_stdout_text(text))  # noqa: T201 — the daemon's response IS this command's output.
            return 0

    sys.stderr.write(
        f"jarvis: daemon starting or hung ({last_error}); tried "
        f"{_FORWARD_ATTEMPTS}x. Try again, or run `jarvis daemon status`.\n"
    )
    return _EXIT_DAEMON_UNREACHABLE


def _forward_to_daemon(
    utterance: str, *, timeout_s: float, key: str, base_url: str = _DAEMON_URL
) -> int:
    """Sync wrapper — the one-shot CLI has no running event loop."""
    return asyncio.run(_forward(utterance, timeout_s=timeout_s, key=key, base_url=base_url))


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for ``python -m jarvis``."""
    parser = argparse.ArgumentParser(prog=_PROG, description=_DESC)
    parser.add_argument(
        "utterance",
        help="User utterance text (one positional). Wrap in quotes if it contains spaces.",
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
        "--brain",
        metavar="URL",
        default=None,
        help=(
            "Send the turn to this brain over the private network (ADR 0170), e.g. "
            "http://jarvis:8006, authenticated by this device's token "
            "(`jarvis pair` on the brain). No local daemon is involved."
        ),
    )
    parser.add_argument(
        "--brain-token-file",
        type=Path,
        default=None,
        help="This device's brain token (default: <runtime root>/brain-token, mode 0600).",
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


_CHECK_CONFIG_HELP = (
    "Load the config the way a start does and say whether it is accepted: one line, exit 0 or "
    "non-zero. Opens no database, starts no thread, binds no port, reads no key."
)


def _check_config(
    entry: str, config_path: Path | None, root: Path, *, prompt_path: Path | None = None,
) -> int:
    """``--check-config`` (ADR 0370): :func:`load_runtime_config` as the entry point calls it.

    Nothing else of the entry point runs: the root is not laid out or locked, the environment file
    is not read, no key is touched. The refusal is the loader's own text on stderr.
    """
    try:
        config = load_runtime_config(config_path, root, prompt_path=prompt_path)
    except (RuntimeBootstrapError, ValueError) as exc:
        reason = " ".join(str(exc).split())
        sys.stderr.write(
            f"jarvis {entry}: {t('cli.config_refused', lang=system_language(), reason=reason)}\n",
        )
        return 1
    good = t(
        "cli.config_ok", lang=config.language, config=config.config_path, root=root,
        role=config.role,
    )
    sys.stdout.write(f"jarvis {entry}: {good}\n")
    return 0


def _main_serve(argv: list[str]) -> int:
    """Parse serve-mode args, bootstrap runtime, call :func:`serve_inherent`.

    Owns the daemon process lifecycle (ADR-0003 Step 1). The daemon lock is
    taken by :func:`jarvis.deployment.daemon_session` before the erase and
    before any bootstrap (ADR 0318), so it is the OUTERMOST scope of the
    daemon's resource ladder and is released after the Event Log is closed.

    With ``--check-config`` it only loads the config the way a start would (ADR 0370) and says
    whether it is accepted; the lock, the root's layout, the environment file and every resource
    are left alone, so it can run beside a daemon that is up.

    Returns:
        0 on clean shutdown (or an accepted ``--check-config``), 1 on bootstrap failure (or a
        refused config), 2 if another live daemon already owns the per-runtime-root lock, 3 if a
        database was written by a newer Jarvis.
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
    parser.add_argument("--check-config", action="store_true", help=_CHECK_CONFIG_HELP)
    args = parser.parse_args(argv)
    if args.check_config:
        return _check_config(
            "serve", args.config, _resolve_runtime_root(args.runtime_root), prompt_path=args.prompt,
        )

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
        with daemon_session(args.runtime_root) as paths:
            try:
                load_runtime_env_and_trace(paths)
                config = load_runtime_config(args.config, paths.root, prompt_path=args.prompt)
                runtime = bootstrap_runtime_app(config, paths)
            except RuntimeBootstrapError as exc:
                sys.stderr.write(f"jarvis serve: bootstrap failed: {exc}\n")
                return 1
            except NewerDataError as exc:
                # ADR 0068: its own exit code, so the app can say "update Jarvis".
                sys.stderr.write(f"jarvis serve: {exc}\n")
                return EXIT_NEWER_DATA
            try:
                asyncio.run(serve_inherent(runtime, host=args.host, port=args.port))
            except KeyboardInterrupt:
                # SIGINT during serve — serve_inherent's teardown cancels
                # watchers; we just surface the interrupt to the operator
                # and exit cleanly.
                sys.stderr.write("jarvis serve: interrupted\n")
            finally:
                # Single close site (sqlite3.Connection.close raises ProgrammingError
                # if called twice; suppress only the documented OSError path that
                # the synchronous helpers above also catch).
                try:
                    runtime.conn.close()
                except OSError:
                    LOGGER.exception("failed to close Event Log connection on serve exit")
    except ProcessLockHeld as exc:
        sys.stderr.write(f"jarvis serve: daemon already running at pid {exc.holder_pid}\n")
        return 2

    return 0


def _render_launchctl_step(step: launchd.LaunchctlResult) -> str:
    """One ``launchctl`` line for the install / uninstall report."""
    return f"  launchctl   : {' '.join(step.argv)} -> exit {step.returncode}"


def _daemon_install() -> str:
    """Run :func:`launchd.install` and render its result for the operator."""
    result = launchd.install()
    written = "written" if result.plist_changed else "unchanged (idempotent re-install)"
    surface_written = (
        "written" if result.resonance_plist_changed else "unchanged (idempotent re-install)"
    )
    lines = [
        f"jarvis daemon install: {launchd.service_target()} and "
        f"{launchd.resonance_service_target()} bootstrapped",
        f"  plist       : {result.plist_path} ({written})",
        f"  interpreter : {result.interpreter}",
        f"  plist       : {result.resonance_plist_path} ({surface_written})",
        f"  node        : {result.node}",
        f"  logs        : {result.logs_dir}",
    ]
    lines.extend(_render_launchctl_step(step) for step in result.steps)
    return "\n".join(lines)


def _daemon_uninstall(*, keep_plist: bool) -> str:
    """Run :func:`launchd.uninstall` and render its result for the operator."""
    result = launchd.uninstall(remove_plist=not keep_plist)
    fate = "removed" if result.plist_removed else "left in place"
    surface_fate = "removed" if result.resonance_plist_removed else "left in place"
    lines = [
        f"jarvis daemon uninstall: {launchd.service_target()} and "
        f"{launchd.resonance_service_target()} booted out",
        f"  plist       : {result.plist_path} ({fate})",
        f"  plist       : {result.resonance_plist_path} ({surface_fate})",
    ]
    lines.extend(_render_launchctl_step(step) for step in result.steps)
    return "\n".join(lines)


def _daemon_restart() -> str:
    """Run :func:`launchd.restart` and render its result for the operator."""
    steps = launchd.restart()
    lines = [
        f"jarvis daemon restart: {launchd.service_target()} and "
        f"{launchd.resonance_service_target()} signalled TERM; KeepAlive respawns both",
    ]
    lines.extend(_render_launchctl_step(step) for step in steps)
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
            "Manage the com.allen.jarvis daemon and com.allen.jarvis.resonance "
            "surface LaunchAgents (ADR-0009 D1, ADR-0015 D3). install is "
            "idempotent; uninstall stops both jobs regardless of KeepAlive; "
            "restart signals TERM to both so KeepAlive respawns them on the merged "
            "checkout; status "
            "combines launchctl, the daemon lock, the interpreter and node "
            "checks, and log sizes."
        ),
    )
    parser.add_argument("verb", choices=("install", "uninstall", "restart", "status"))
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
        elif args.verb == "restart":
            report = _daemon_restart()
        else:
            report = launchd.format_status(launchd.status())
    except launchd.LaunchdError as exc:
        sys.stderr.write(f"jarvis daemon {args.verb}: {exc}\n")
        return 1

    print(report)  # noqa: T201 — operator-facing report is this verb's whole output.
    return 0


def _main_terminal_agent(argv: list[str]) -> int:
    """``jarvis terminal-agent install --brain URL|uninstall`` — ADR 0183's LaunchAgent.

    Exclusive with the daemon's agent: install refuses while ``com.allen.jarvis`` is
    loaded, and uninstall is the way back to it.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} terminal-agent",
        description=(
            f"Run `jarvis terminal --voice --serve-ui` under the {launchd.TERMINAL_LABEL} "
            "LaunchAgent (KeepAlive), in place of the daemon on this Mac. install is "
            "idempotent and refuses while the daemon's agent is loaded; uninstall stops "
            "and removes the agent so the daemon can come back."
        ),
    )
    parser.add_argument("verb", choices=("install", "uninstall"))
    parser.add_argument(
        "--brain", default=None, help="install only: the brain, e.g. http://jarvis:8006."
    )
    args = parser.parse_args(argv)
    if (args.verb == "install") != (args.brain is not None):
        parser.error("--brain goes with install, and only there")
    try:
        if args.verb == "install":
            installed = launchd.install_terminal(_brain_base_url(args.brain))
            written = "written" if installed.plist_changed else "unchanged (idempotent re-install)"
            lines = [
                f"jarvis terminal-agent install: {launchd.terminal_service_target()} bootstrapped",
                f"  plist       : {installed.plist_path} ({written})",
                f"  interpreter : {installed.interpreter}",
                f"  logs        : {installed.logs_dir}",
                *(_render_launchctl_step(step) for step in installed.steps),
            ]
        else:
            removed = launchd.uninstall_terminal()
            fate = "removed" if removed.plist_removed else "absent"
            lines = [
                f"jarvis terminal-agent uninstall: {launchd.terminal_service_target()} booted out",
                f"  plist       : {removed.plist_path} ({fate})",
                *(_render_launchctl_step(step) for step in removed.steps),
                "  next        : `jarvis daemon install` gives this Mac back to the daemon",
            ]
    except (launchd.LaunchdError, ValueError) as exc:
        sys.stderr.write(f"jarvis terminal-agent {args.verb}: {exc}\n")
        return 1
    print("\n".join(lines))  # noqa: T201 — operator-facing report is this verb's whole output.
    return 0


def _brain_base_url(url: str) -> str:
    """``http(s)://host[:port]`` for ``--brain``, without a trailing slash."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path.strip("/"):
        msg = f"--brain must look like http://jarvis:8006, not {url!r}"
        raise ValueError(msg)
    return f"{parts.scheme}://{parts.netloc}"


def _read_brain_token(path: Path) -> str:
    """This device's token; a missing, empty or group- or world-readable file is refused."""
    try:
        if path.stat().st_mode & 0o077:
            msg = f"the brain token {path} is readable by others; run chmod 600 on it"
            raise ValueError(msg)
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        msg = f"cannot read the brain token {path}: {exc.strerror}; pair this device first"
        raise ValueError(msg) from None
    if not token:
        msg = f"the brain token {path} is empty"
        raise ValueError(msg)
    return token


def _main_brain_turn(args: argparse.Namespace, runtime_root: Path) -> int:
    """ADR 0170: one typed turn to a remote brain; nothing about a local daemon applies."""
    try:
        base_url = _brain_base_url(args.brain)
        token = _read_brain_token(args.brain_token_file or runtime_root / _BRAIN_TOKEN_FILE)
    except ValueError as exc:
        sys.stderr.write(f"jarvis: {exc}\n")
        return 1
    return _forward_to_daemon(
        args.utterance, timeout_s=args.timeout, key=token, base_url=base_url
    )


def _main_terminal(argv: list[str]) -> int:
    """ADR 0170: `terminal --brain URL` runs this device's tools for a remote brain."""
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} terminal",
        description=(
            "Connect to a brain and run its device-bound tool calls (open, clipboard, "
            "files, screen) on this machine, and report the repos and TimeSink data it "
            "observes. Holds no model key and keeps no state."
        ),
    )
    parser.add_argument("--brain", default=None, help="The brain, e.g. http://jarvis:8006.")
    parser.add_argument(
        "--brain-token-file",
        type=Path,
        default=None,
        help="This device's brain token (default: <runtime root>/brain-token, mode 0600).",
    )
    parser.add_argument(
        "--runtime-root", type=Path, default=None, help="Override JARVIS_RUNTIME_ROOT."
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to jarvis.yaml.")
    parser.add_argument(
        "--no-observers",
        action="store_true",
        help="Do not observe this machine's repos and TimeSink for the brain.",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help=(
            "Also speak the brain's answers on this machine's speaker and, when this "
            "machine's config turns realtime.single_audio_ingress on, listen on its "
            "microphone and send what it hears to the brain (ADR 0172). Its own daemon's "
            "speaker can stay on; only the microphone has one owner."
        ),
    )
    parser.add_argument(
        "--serve-ui",
        action="store_true",
        help=(
            "Serve this device's UI, the desktop companion's interface, on 127.0.0.1 "
            "(ADR 0183): what is the brain's goes to the brain under this device's token, "
            "what is this machine's is answered here. Refuses to start while a daemon holds "
            "the port."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=None, help="With --serve-ui, the port (default 8006).",
    )
    parser.add_argument("--check-config", action="store_true", help=_CHECK_CONFIG_HELP)
    args = parser.parse_args(argv)
    if args.brain is None and not args.check_config:
        parser.error("the following arguments are required: --brain")
    if args.port is not None and not args.serve_ui:
        parser.error("--port only applies with --serve-ui")
    root = _resolve_runtime_root(args.runtime_root)
    try:
        base_url = _brain_base_url(args.brain) if args.brain is not None else ""
    except ValueError as exc:
        sys.stderr.write(f"jarvis terminal: {exc}\n")
        return 1
    if args.check_config:
        return _check_config("terminal", args.config, root)
    try:
        token = _read_brain_token(args.brain_token_file or root / _BRAIN_TOKEN_FILE)
    except ValueError as exc:
        sys.stderr.write(f"jarvis terminal: {exc}\n")
        return 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    try:
        config = load_runtime_config(args.config, root)
    except (RuntimeBootstrapError, ValueError) as exc:
        sys.stderr.write(f"jarvis terminal: {exc}\n")
        return 1
    return run_terminal(
        base_url, token, config=config, runtime_root=root,
        observers=not args.no_observers, voice=args.voice,
        serve_ui=(args.port or _DAEMON_PORT) if args.serve_ui else None,
    )


def _main_oneshot(argv: list[str]) -> int:
    """``jarvis "<utterance>"``: hand one turn to a brain or to the daemon here (ADR 0341).

    With ``--brain`` the turn goes to that brain. Otherwise ADR-0009 D2's forward: when the
    requested root's ``daemon.lock`` is held OR the LaunchAgent is installed (which also covers
    the respawn window, where the lock is momentarily free), this process is a thin client of the
    daemon on this machine. With neither, there is no brain to answer: the command exits
    non-zero and says to pass ``--brain``. It opens no runtime and writes nothing under any root.

    B-NEW-5 is evaluated before the forward: with ``--runtime-root <other>`` while a daemon holds
    the default (home) root's lock, the command refuses instead of being answered out of the
    wrong event log. :func:`process_lock.is_held` is the probe for both roots.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    requested_root = _resolve_runtime_root(args.runtime_root)
    if args.brain is not None:
        return _main_brain_turn(args, requested_root)

    lock_held = process_lock.is_held(requested_root / "daemon.lock")

    # B-NEW-5 — an explicit runtime root while a daemon owns the DEFAULT (home) root: forwarding
    # would be answered out of that daemon's event log, not the one the operator asked for.
    home_root = Path(DEFAULT_RUNTIME_ROOT_LITERAL).expanduser().resolve()
    if requested_root != home_root:
        home_lock = home_root / "daemon.lock"
        if process_lock.is_held(home_lock):
            pid = process_lock.holder_pid(home_lock)
            sys.stderr.write(
                f"jarvis: daemon at {home_root} holds the default runtime "
                f"root (pid {pid}); this command talks only to that daemon, so "
                f"--runtime-root {requested_root} would be answered out of the wrong "
                f"event log. Omit --runtime-root.\n"
            )
            return _EXIT_REFUSED

    if lock_held or launchd.is_agent_installed():
        return _forward_to_daemon(
            args.utterance, timeout_s=args.timeout, key=local_key(requested_root)
        )

    language: Language = "en" if is_english(args.utterance) else "zh"
    sys.stderr.write(f"jarvis: {t('cli.no_brain', lang=language)}\n")
    return _EXIT_DAEMON_UNREACHABLE


def _open_runtime(
    config_path: Path | None, runtime_root: Path | None,
) -> tuple[RuntimePaths, RuntimeConfig]:
    """The runtime root laid out, and the config parsed over it (ADR 0370)."""
    paths = bootstrap_runtime(runtime_root)
    return paths, load_runtime_config(config_path, paths.root)


def _main_mcp_login(argv: list[str]) -> int:
    """ADR 0032: `jarvis mcp-login <server>` opens the browser once; the daemon reuses the token."""
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} mcp-login",
        description="Log an `auth: oauth` MCP server in through the browser.",
    )
    parser.add_argument("server", help="Key under tools.mcp.servers in jarvis.yaml.")
    parser.add_argument("--config", type=Path, default=None, help="Path to jarvis.yaml.")
    parser.add_argument(
        "--runtime-root", type=Path, default=None, help="Override JARVIS_RUNTIME_ROOT."
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help=(
            "Print the authorization URL and open nothing (the default on a machine with "
            "no desktop). Open it on a machine that forwards 127.0.0.1:8789 here."
        ),
    )
    args = parser.parse_args(argv)
    try:
        paths, config = _open_runtime(args.config, args.runtime_root)
        load_env_file(paths.root)
        return mcp_login(args.server, config=config, paths=paths, open_browser=not args.no_browser)
    except (RuntimeBootstrapError, OSError, ValueError) as exc:
        sys.stderr.write(f"jarvis mcp-login: {exc}\n")
        return 1


def _main_meaning_model(argv: list[str]) -> int:
    """ADR 0260: `jarvis meaning-model` downloads the search-by-meaning model (118 MB), once."""
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} meaning-model",
        description="Download the model that lets search find lines by meaning.",
    )
    parser.add_argument(
        "--runtime-root", type=Path, default=None, help="Override JARVIS_RUNTIME_ROOT."
    )
    args = parser.parse_args(argv)
    try:
        directory = default_meaning_dir(_resolve_runtime_root(args.runtime_root))
        fetch_meaning_model(directory)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"jarvis meaning-model: {exc}\n")
        return 1
    sys.stdout.write(f"{directory}\n")
    return 0


def _main_import(argv: list[str]) -> int:
    """ADR 0259: `jarvis import <source> <path>...` keeps exports raw, reads them; counts only."""
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} import",
        description="Keep an export raw under the runtime root and read its entries.",
    )
    parser.add_argument("source", help="claude, claude_daily, wechat_pay, alipay or boc.")
    parser.add_argument("paths", nargs="+", type=Path, help="Files, or directories of files.")
    parser.add_argument("--config", type=Path, default=None, help="Path to jarvis.yaml.")
    parser.add_argument(
        "--runtime-root", type=Path, default=None, help="Override JARVIS_RUNTIME_ROOT."
    )
    args = parser.parse_args(argv)
    try:
        paths, config = _open_runtime(args.config, args.runtime_root)
        counts = import_exports(args.source, args.paths, config=config, paths=paths)
    except (RuntimeBootstrapError, OSError, ValueError) as exc:
        sys.stderr.write(f"jarvis import: {exc}\n")
        return 1
    sys.stdout.write(
        f"files: {counts.files_stored} stored, {counts.files_skipped} already held, "
        f"{counts.unreadable} unreadable; entries: {counts.added} added, "
        f"{counts.held} already held, {counts.conflicting} conflicting\n"
    )
    return 0


def _main_voiceprint(argv: list[str]) -> int:
    """ADR 0286: `jarvis voiceprint seed FILE --device D --mic KEY=NAME ...` and `... status`."""
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} voiceprint",
        description="Seed or read the profiles of the owner's voice, one per terminal microphone.",
    )
    parser.add_argument("command", choices=("seed", "status"))
    parser.add_argument(
        "file", nargs="?", type=Path,
        help='seed: the study\'s {"mics": {"<key>": {"n": int, "centroid": [192 numbers]}}}.',
    )
    parser.add_argument("--device", help="seed: the terminal's paired name, e.g. macbook.")
    parser.add_argument(
        "--mic", action="append", default=[], metavar="KEY=NAME",
        help="seed: a key of the file and the full name of its microphone (repeat per key).",
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to jarvis.yaml.")
    parser.add_argument(
        "--runtime-root", type=Path, default=None, help="Override JARVIS_RUNTIME_ROOT."
    )
    args = parser.parse_args(argv)
    try:
        book = open_voiceprints(_open_runtime(args.config, args.runtime_root)[1])
        if args.command == "status":
            for one in book.status():
                sys.stdout.write(
                    f"{one.device}\t{one.mic}\trows={one.rows}\tdays={one.days}\t"
                    f"ready={'yes' if one.ready else 'no'}\tlast_added={one.last_added}\n"
                )
            return 0
        names = dict(pair.split("=", 1) for pair in args.mic if "=" in pair)
        if args.file is None or not args.device or len(names) != len(args.mic):
            sys.stderr.write("jarvis voiceprint seed: needs FILE, --device and --mic KEY=NAME\n")
            return 2
        seeded = seed_study(
            book, args.device, json.loads(args.file.read_text(encoding="utf-8")), names,
        )
    except (RuntimeBootstrapError, NewerDataError, OSError, ValueError) as exc:
        sys.stderr.write(f"jarvis voiceprint: {exc}\n")
        return 1
    for mic, count in seeded:
        sys.stdout.write(f"{args.device}\t{mic}\tseeded n={count}\n")
    sys.stdout.write("restart the brain to load the new profiles\n")
    return 0


def _since_days(text: str) -> float:
    """``--since-days``: a number of days above 0 and at most ``MAX_SINCE_DAYS``."""
    try:
        days = float(text)
    except ValueError:
        days = math.nan
    if not 0 < days <= MAX_SINCE_DAYS:
        message = f"must be a number above 0 and at most {MAX_SINCE_DAYS:g}"
        raise argparse.ArgumentTypeError(message)
    return days


def _render_log_report(report: LogReport) -> str:
    """The check as text: each invariant, its count and a few ids; never a payload."""
    lines = [
        f"event log: {report.event_log}",
        f"window: {iso(report.since_ms)} .. {iso(report.until_ms)}, {report.events} events",
    ]
    for one in report.results:
        count = f": {one.violations} violation(s)" if one.violations else ""
        lines.append(f"{one.status.upper():9} {one.name}{count}")
        if one.status == "violated":
            lines.append(f"          rule: {one.rule}")
            lines.append(f"          see: {one.ref}")
        for example in one.examples:
            note = f" ({example.note})" if example.note else ""
            lines.append(f"          event {example.event_id} at {iso(example.ts_epoch_ms)}{note}")
        if one.detail:
            lines.append(f"          {one.detail}")
    lines.append(
        f"result: {len(report.violated)} of {len(report.results)} invariants violated"
        if report.violated
        else f"result: clean, {len(report.results)} invariants"
    )
    return "\n".join(lines) + "\n"


def _main_check_log(argv: list[str]) -> int:
    """`jarvis check-log`: ask the real event log what the spec and the code promise about it.

    Read-only (the log opens ``mode=ro``), takes no daemon lock, prints ids, types, counts and
    timestamps and never payload text. Exit 0: clean; 1: an invariant is violated; 2: usage
    error, or the log cannot be opened.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} check-log",
        description="Check the invariants of a runtime root's event log, read-only.",
    )
    parser.add_argument(
        "--runtime-root", type=Path, default=None,
        help=(
            "Root holding mac_events.db: the live one or a backup copy (default: "
            f"$JARVIS_RUNTIME_ROOT or {DEFAULT_RUNTIME_ROOT_LITERAL})."
        ),
    )
    parser.add_argument(
        "--since-days", type=_since_days, default=DEFAULT_SINCE_DAYS,
        help=f"Days of the log, back from its newest event (default {DEFAULT_SINCE_DAYS:g}).",
    )
    parser.add_argument(
        "--examples", type=int, default=DEFAULT_EXAMPLES, choices=range(21), metavar="N",
        help=f"Example event ids per violated invariant, 0-20 (default {DEFAULT_EXAMPLES}).",
    )
    parser.add_argument("--json", action="store_true", help="Print the report as one JSON object.")
    args = parser.parse_args(argv)
    root = _resolve_runtime_root(args.runtime_root)
    try:
        report = check_event_log(
            root / EVENT_LOG_FILENAME, root, since_days=args.since_days,
            max_examples=args.examples,
        )
    except LogCheckError as exc:
        sys.stderr.write(f"jarvis check-log: {exc}\n")
        if args.json:
            sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}) + "\n")
        return 2
    sys.stdout.write(
        json.dumps(report.to_dict(), indent=2) + "\n" if args.json else _render_log_report(report)
    )
    return 1 if report.violated else 0


def _main_devices(command: str, argv: list[str]) -> int:
    """ADR 0170, on the brain: `pair <name>`, `unpair <name>` and `devices`."""
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} {command}",
        description={
            "pair": "Pair a terminal: print its new token, the only output. Redirect it to a file.",
            "unpair": "Revoke a terminal's token; it takes effect at once.",
            "devices": "List the paired terminals, never their tokens.",
        }[command],
    )
    if command != "devices":
        parser.add_argument("name", help="The terminal's name, e.g. macbook.")
    parser.add_argument(
        "--runtime-root", type=Path, default=None, help="Override JARVIS_RUNTIME_ROOT."
    )
    args = parser.parse_args(argv)
    root = _resolve_runtime_root(args.runtime_root)
    try:
        if command == "pair":
            if sys.stdout.isatty():
                sys.stderr.write(
                    "jarvis pair: the token is printed once; redirect stdout to a file "
                    "(> brain-token) so it never shows on screen.\n"
                )
                return 1
            sys.stdout.write(pair_device(root, args.name) + "\n")
        elif command == "unpair":
            unpair_device(root, args.name)
            sys.stdout.write(f"unpaired {args.name}\n")
        else:
            for name, created_at in paired_devices(root):
                sys.stdout.write(f"{name}\t{created_at}\n")
    except (DeviceTokenError, OSError) as exc:
        sys.stderr.write(f"jarvis {command}: {exc}\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Routes ``serve`` to the daemon; otherwise to one-shot.

    Dispatch is purely positional: ``serve`` as the first argv element
    routes to :func:`_main_serve`, ``daemon`` to :func:`_main_daemon`
    (ADR-0009 D1); everything else goes through :func:`_main_oneshot`
    (which preserves the Day-1 argparse contract).

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
    commands = {
        "serve": _main_serve,
        "daemon": _main_daemon,
        "mcp-login": _main_mcp_login,
        "meaning-model": _main_meaning_model,
        "import": _main_import,
        "voiceprint": _main_voiceprint,
        "check-log": _main_check_log,
        "terminal": _main_terminal,
        "terminal-agent": _main_terminal_agent,
    }
    if argv and argv[0] in commands:
        return commands[argv[0]](argv[1:])
    if argv and argv[0] in {"pair", "unpair", "devices"}:
        return _main_devices(argv[0], argv[1:])
    return _main_oneshot(argv)


__all__ = [
    "main",
]
