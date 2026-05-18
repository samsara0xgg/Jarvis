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

Layer rules: ``jarvis.cli`` imports ``jarvis.runtime`` (and via that,
the whole stack). It MUST NOT import the middle-layer siblings
directly — ``.importlinter`` keeps cli above runtime, runtime above
the four siblings.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from jarvis.deployment import DEFAULT_RUNTIME_ROOT_LITERAL
from jarvis.runtime import (
    PreEmitTokenError,
    RuntimeBootstrapError,
    TriggerWaitTimeout,
    bootstrap_runtime_app,
    run_turn,
)

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

    Synchronous path (classifier did not match, or ``no_detach``):
    Day-1 behavior preserved — bootstrap, run_turn, return 0.

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
    if _utterance_implies_long_run(utterance) and not no_detach:
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


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Returns 0 on success, non-zero on failure.

    Day-2: parses argv and routes through :func:`main_with_detach`.
    The argparse / exit-code contract is unchanged from Day-1 so
    existing tests (``tests/unit/test_cli_main.py``) keep passing.

    Args:
        argv: argv-style list (without the program name). Default:
            ``sys.argv[1:]``.

    Returns:
        Exit code: 0 on a clean turn, 1 on
        :class:`jarvis.runtime.PreEmitTokenError` or other runtime
        errors, 2 on argument-parsing failures (argparse default).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    return main_with_detach(
        args.utterance,
        config_path=args.config,
        prompt_path=args.prompt,
        runtime_root=args.runtime_root,
        no_detach=args.no_detach,
    )


__all__ = [
    "main",
    "main_with_detach",
]
