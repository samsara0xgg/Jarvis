"""Unit tests for the CLI entry (``jarvis.cli``).

Covers (no LLM call — that's the live_llm scenario test in Step 12/13):
- ``main(["--help"])`` prints the argparse help and exits 0.
- ``main`` returns nonzero with a reasonable error message when the
  config file is missing.
- B-NEW-5: fail-fast when a daemon owns the DEFAULT runtime root and
  the operator passes ``--runtime-root <other>``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.cli import main
from jarvis.deployment import DEFAULT_RUNTIME_ROOT_LITERAL


def test_main_help_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``main(["--help"])`` prints argparse help and exits 0."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    # argparse exits 0 on --help.
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert "utterance" in captured.out
    assert "--config" in captured.out
    assert "--prompt" in captured.out
    assert "--runtime-root" in captured.out


def test_main_missing_config_returns_nonzero(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing --config path must yield a nonzero exit code + stderr message."""
    # HOME redirect: the D4 lock probe resolves ~/.jarvis under a clean
    # root, so the test reaches the bootstrap path even while a real
    # daemon runs on this machine (always-resident per ADR-0009).
    monkeypatch.setenv("HOME", str(tmp_path))
    bogus = "/tmp/nonexistent-jarvis-config-xyz.yaml"
    code = main(["--config", bogus, "hello"])
    assert code != 0
    captured = capsys.readouterr()
    # The message should hint at where the bootstrap failed.
    assert "bootstrap failed" in captured.err


# --- B-NEW-5: runtime-root conflict fail-fast -----------------------------


def test_oneshot_fails_fast_on_conflicting_runtime_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the home daemon lock is held + ``--runtime-root`` differs, refuse.

    B-NEW-5: without this guard the one-shot CLI silently forks into a
    parallel SQLite state. The fail-fast keeps the operator out of the
    confusing "where did my events go?" debug loop.

    Monkeypatch :func:`jarvis.deployment.process_lock.is_held` so the
    home (default) runtime root's lock reports as held by a live
    daemon, but the user-requested runtime root is free. Assert the CLI
    exits 2 with the expected stderr message before reaching the
    bootstrap path.
    """
    # The user's requested runtime root, distinct from the default home root.
    user_root = (tmp_path / "alt-root").resolve()
    home_root = Path(DEFAULT_RUNTIME_ROOT_LITERAL).expanduser().resolve()

    is_held_calls: list[Path] = []

    def fake_is_held(lock_path: Path) -> bool:
        is_held_calls.append(lock_path)
        # Report held ONLY for the home daemon lock; the user root's
        # lock is free (so the first D4 probe passes).
        return lock_path == home_root / "daemon.lock"

    def fake_holder_pid(lock_path: Path) -> int | None:
        if lock_path == home_root / "daemon.lock":
            return 12345
        return None

    monkeypatch.setattr("jarvis.cli.process_lock.is_held", fake_is_held)
    monkeypatch.setattr("jarvis.cli.process_lock.holder_pid", fake_holder_pid)

    code = main(["--runtime-root", str(user_root), "hello"])

    assert code == 2, f"expected exit 2 on conflict, got {code}"
    captured = capsys.readouterr()
    assert "default runtime root" in captured.err
    assert "pid 12345" in captured.err
    assert str(user_root) in captured.err
    assert "Stop the daemon or omit --runtime-root" in captured.err
    # Both probes ran: the user-root probe first (D4), then the home probe.
    assert (user_root / "daemon.lock") in is_held_calls
    assert (home_root / "daemon.lock") in is_held_calls


def test_oneshot_no_conflict_when_runtime_roots_match(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``--runtime-root`` equals the default home root, skip the conflict probe.

    The B-NEW-5 fail-fast only fires when the requested root DIFFERS
    from the default home root. If the operator explicitly opts back
    into the default via ``--runtime-root ~/.jarvis``, the D4 probe is
    the only refusal mechanism — and even that lets the request through
    when no daemon is running.
    """
    home_root = Path(DEFAULT_RUNTIME_ROOT_LITERAL).expanduser().resolve()

    # Neither lock is held: the D4 probe and the B-NEW-5 probe both
    # report no live daemon, so the CLI falls through to bootstrap.
    monkeypatch.setattr("jarvis.cli.process_lock.is_held", lambda _path: False)

    # Bootstrap-failure on a bogus config gives us a clean exit point
    # before any SQLite work — we just want to assert we got PAST the
    # fail-fast guard.
    code = main(
        [
            "--runtime-root",
            str(home_root),
            "--config",
            "/tmp/nonexistent-jarvis-cfg-b-new-5.yaml",
            "hello",
        ]
    )
    captured = capsys.readouterr()
    # We expect bootstrap-failure (exit != 0) and NOT the conflict
    # message — verifying the conflict guard did not fire on equal roots.
    assert "default runtime root" not in captured.err
    assert code != 0  # bootstrap_failed path
