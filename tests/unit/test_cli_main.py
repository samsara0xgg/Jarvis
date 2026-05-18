"""Unit tests for the CLI entry (``jarvis.cli``).

Covers (no LLM call — that's the live_llm scenario test in Step 12/13):
- ``main(["--help"])`` prints the argparse help and exits 0.
- ``main`` returns nonzero with a reasonable error message when the
  config file is missing.
"""

from __future__ import annotations

import pytest

from jarvis.cli import main


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
) -> None:
    """A missing --config path must yield a nonzero exit code + stderr message."""
    bogus = "/tmp/nonexistent-jarvis-config-xyz.yaml"
    code = main(["--config", bogus, "hello"])
    assert code != 0
    captured = capsys.readouterr()
    # The message should hint at where the bootstrap failed.
    assert "bootstrap failed" in captured.err
