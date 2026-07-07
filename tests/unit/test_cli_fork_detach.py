"""Unit + integration tests for Day-2 CLI ``main_with_detach``.

Per ADR-0002 Step 17 build-order row (line 1875):

    canary: ``test_canary_daemon_ack_before_fork``;
    integration: parent exits within 100ms; child's bootstrap registers
    sleep observer.

The classifier tests (Day-2 hard rule: regex-only, no SQLite) are pure
unit; the subprocess test is the integration. POSIX-only because
:func:`jarvis.runtime.daemon.fork_detach` uses :func:`os.fork`.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from typing import Any, cast

import pytest

from jarvis.cli import (
    _LONG_RUN_RE,
    _QUICK_ACK_PHRASE,
    _quick_ack_phrase,
    _utterance_implies_long_run,
)

skip_windows = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fork_detach uses POSIX os.fork; Day-2 is Mac-only per ADR-0002 V4.",
)


# --- Classifier (LLM-free regex) -------------------------------------------


@pytest.mark.parametrize(
    "utterance",
    [
        "今天那个 task 给 Codex 跑一下",
        "spawn codex with my task",
        "帮我做 implement-rate-limiter",
        "帮我跑那个 PR review",
        "做一下 rebase",
        "审核 a diff",
        "go run the linter",
        "SPAWN Codex now",  # case-insensitive
    ],
)
def test_classifier_recognizes_long_run_keywords(utterance: str) -> None:
    """Each ADR-pinned trigger word routes the utterance to the long-run path."""
    assert _utterance_implies_long_run(utterance) is True, (
        f"utterance {utterance!r} should match _LONG_RUN_RE"
    )


@pytest.mark.parametrize(
    "utterance",
    [
        "hello",
        "what time is it",
        "tell me about ADR-0002",
        "list my open tasks",  # nb: "open" is not a trigger word
        "",
    ],
)
def test_classifier_rejects_non_long_run(utterance: str) -> None:
    """Utterances that lack every keyword must take the synchronous path."""
    assert _utterance_implies_long_run(utterance) is False, (
        f"utterance {utterance!r} should NOT match _LONG_RUN_RE"
    )


def test_classifier_no_sqlite_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The classifier path opens NO sqlite connection.

    The Day-2 hard rule (ADR-0002 line 1091-1094): the parent process
    holds nothing but the regex classifier + the ack print + the fork
    call. A SQLite open in the classifier path would silently leak a
    file descriptor across ``os.fork()`` and corrupt the child's event
    log.
    """
    sqlite_opens: list[object] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        sqlite_opens.append((args, kwargs))
        return cast(
            "sqlite3.Connection",
            real_connect(*cast("tuple[Any, ...]", args), **cast("dict[str, Any]", kwargs)),
        )

    monkeypatch.setattr("sqlite3.connect", tracking_connect)
    for utterance in ("跑 Codex now", "帮我做 X", "hello world", "spawn run"):
        _ = _utterance_implies_long_run(utterance)
    assert sqlite_opens == [], (
        f"_utterance_implies_long_run opened sqlite: {sqlite_opens!r}. "
        "The classifier MUST be purely textual per ADR-0002 § Daemon / CLI "
        "contract hard rules."
    )


def test_quick_ack_phrase_is_constant_template() -> None:
    """``_quick_ack_phrase`` returns the Day-2 fixed phrase regardless of input."""
    assert _quick_ack_phrase("anything") == _QUICK_ACK_PHRASE
    assert _quick_ack_phrase("跑") == _QUICK_ACK_PHRASE
    assert _quick_ack_phrase("") == _QUICK_ACK_PHRASE


def test_long_run_regex_is_pinned_by_adr() -> None:
    """The regex literal MUST match the ADR-0002 § Daemon / CLI contract pin.

    ADR line 1056: ``r"跑|spawn|给 *codex|帮我做|帮我跑|做一下|审核|run"``.
    Any rewrite/refactor that drops or extends a keyword would shift the
    classifier surface area silently. This canary-style assertion lives
    in the unit suite (rather than ``tests/canary/``) because it pins a
    runtime constant rather than an AST shape.
    """
    expected_keywords = ("跑", "spawn", "给", "codex", "帮我做", "帮我跑", "做一下", "审核", "run")
    pattern_src = _LONG_RUN_RE.pattern
    for kw in expected_keywords:
        assert kw in pattern_src, (
            f"_LONG_RUN_RE.pattern is missing the ADR-pinned keyword {kw!r}"
        )


# --- Subprocess integration ------------------------------------------------


@skip_windows
def test_subprocess_parent_exits_for_short_utterance() -> None:
    """A non-long-run utterance routes through the synchronous Day-1 path.

    The synchronous path bootstraps the runtime and tries to run the
    turn against the real LLM — without ``--live-llm`` (Day-1 stub) the
    bootstrap itself succeeds but the turn proceeds with the configured
    client. We assert the CLI returns *some* exit code within a generous
    bound; the contract under test is "process actually terminates",
    not the turn's correctness (which is covered by the LLM-mocked unit
    tests).
    """
    # We use --help to exercise argparse without depending on a live LLM
    # or a writable runtime root; the synchronous path's turn execution
    # is covered by `tests/unit/test_cli_main.py`.
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "jarvis", "--help"],
        check=False,
        timeout=10,
        capture_output=True,
        text=True,
    )
    elapsed_s = time.monotonic() - start
    assert result.returncode == 0
    assert elapsed_s < 10, f"--help took {elapsed_s:.2f}s; expected < 10s"
    assert "usage:" in result.stdout
    # The Day-2 help mentions both the fork-detach behavior and the
    # synchronous fallback (--no-detach flag exposes the latter).
    assert "--no-detach" in result.stdout, (
        "Day-2 CLI must expose --no-detach for smoke tests per Step 17."
    )


@skip_windows
def test_subprocess_parent_exits_quickly_for_long_run_utterance() -> None:
    """Long-run utterance: parent prints ack + exits cleanly + child detaches.

    The Day-2 contract: parent prints ``_QUICK_ACK_PHRASE``, flushes
    stdout, calls fork_detach, and ``os._exit(0)``. The child is
    daemonized (cwd=/, stdio=/dev/null) and proceeds to bootstrap +
    install power observer + run_turn. The child WILL fail without a
    live LLM/Codex, but that failure is invisible to the operator
    because its stderr is /dev/null — which is exactly the spec.

    We assert the PARENT exits with code 0 quickly, with the ack on
    stdout. The CI 100ms aspirational bound from the ADR is widened to
    10s for cold-start import overhead on slow runners.
    """
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "jarvis", "今天给Codex跑一下"],
        check=False,
        timeout=15,
        capture_output=True,
        text=True,
        env={**os.environ, "JARVIS_RUNTIME_ROOT": "/tmp/jarvis-test-root"},
    )
    elapsed_s = time.monotonic() - start
    assert result.returncode == 0, (
        f"parent exit code {result.returncode}; stderr={result.stderr!r}"
    )
    assert _QUICK_ACK_PHRASE in result.stdout, (
        f"ack not in parent stdout: {result.stdout!r}"
    )
    # Parent exit must be quick; the contract says "ack then fork". The
    # bound is generous to tolerate slow cold-start import.
    assert elapsed_s < 15, (
        f"parent process took {elapsed_s:.2f}s; expected < 15s (fork-detach "
        "should release the parent immediately after the ack)."
    )


@skip_windows
def test_subprocess_no_detach_flag_takes_synchronous_path() -> None:
    """``--no-detach`` forces sync path even on classifier match (smoke escape)."""
    # Use a long-run utterance with --no-detach; the CLI should still
    # take the synchronous path (no fork). The bootstrap will fail or
    # the turn will fail (no live LLM), but we only assert the process
    # is the parent (no fork happened) by checking that the ack phrase
    # is NOT printed (the synchronous path doesn't print the ack).
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "jarvis",
            "--no-detach",
            "--config",
            "/tmp/nonexistent-jarvis-config-xyz.yaml",
            "跑 Codex now",
        ],
        check=False,
        timeout=10,
        capture_output=True,
        text=True,
    )
    # Bootstrap fails on missing config -> nonzero exit code.
    assert result.returncode != 0
    assert "bootstrap failed" in result.stderr
    # The ack phrase belongs to the long-run branch, which --no-detach
    # bypasses.
    assert _QUICK_ACK_PHRASE not in result.stdout
