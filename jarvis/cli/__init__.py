"""Command-line entry point — ``python -m jarvis "<utterance>"``.

Drives one full conversation turn round-trip through the runtime
composition root. Day-1 is a single-shot CLI: parse argv, bootstrap,
run one turn, exit.

Layer rules: ``jarvis.cli`` imports ``jarvis.runtime`` (and via that,
the whole stack). It MUST NOT import the middle-layer siblings
directly — ``.importlinter`` keeps cli above runtime, runtime above
the four siblings.
"""

from __future__ import annotations

import argparse
import logging
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
    "Allen's state-centric personal runtime — Day-1 single-shot CLI. "
    "Pass an utterance string; Jarvis emits surface.user_intent, drives the "
    "decide() loop, and writes the document-channel response to stdout."
)


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
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Returns 0 on success, non-zero on failure.

    Args:
        argv: argv-style list (without the program name). Default:
            ``sys.argv[1:]``.

    Returns:
        Exit code: 0 on a clean turn, 1 on
        :class:`jarvis.runtime.PreEmitTokenError` or other runtime errors,
        2 on argument-parsing failures (argparse default).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        runtime = bootstrap_runtime_app(
            config_path=args.config,
            prompt_path=args.prompt,
            runtime_root=args.runtime_root,
        )
    except RuntimeBootstrapError as exc:
        sys.stderr.write(f"jarvis: bootstrap failed: {exc}\n")
        return 1
    except (OSError, ValueError) as exc:
        # File-not-found / YAML / sqlite / config-shape issues.
        sys.stderr.write(f"jarvis: bootstrap failed: {exc}\n")
        return 1

    try:
        run_turn(runtime, utterance=args.utterance)
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
        # Best-effort cleanup; an exception here must not mask the
        # original exit code. ``LOGGER.exception`` records the trace.
        try:
            runtime.conn.close()
        except OSError:
            LOGGER.exception("failed to close Event Log connection on exit")

    return 0


__all__ = ["main"]
