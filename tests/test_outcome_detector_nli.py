"""Tests for outcome_detector.py — NLI-only path (regex deprecated)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from memory.cold.outcome_detector import detect_outcome


def test_nli_none_returns_none_for_positive():
    """No NLI instance: always None — regex no longer used as fallback."""
    assert detect_outcome("好的") is None


def test_nli_none_returns_none_for_negative():
    assert detect_outcome("不对") is None


def test_nli_none_returns_none_for_medium():
    assert detect_outcome("嗯不太对吧") is None


def test_nli_positive():
    nli = MagicMock()
    nli.detect_outcome.return_value = 1
    assert detect_outcome("我觉得很好", nli=nli) == 1


def test_nli_negative():
    nli = MagicMock()
    nli.detect_outcome.return_value = -1
    assert detect_outcome("嗯不太对吧", nli=nli) == -1


def test_nli_returns_none():
    nli = MagicMock()
    nli.detect_outcome.return_value = None
    assert detect_outcome("嗯", nli=nli) is None


def test_length_filter_too_short():
    """len < 2 → None without NLI call."""
    nli = MagicMock()
    assert detect_outcome("嗯", nli=nli) is None  # 1 char
    nli.detect_outcome.assert_not_called()


def test_length_filter_empty():
    nli = MagicMock()
    assert detect_outcome("", nli=nli) is None
    nli.detect_outcome.assert_not_called()


def test_length_filter_too_long():
    """len > 500 → None without NLI call."""
    nli = MagicMock()
    assert detect_outcome("a" * 501, nli=nli) is None
    nli.detect_outcome.assert_not_called()


def test_length_at_boundary_500():
    """Exactly 500 chars → passes to NLI."""
    nli = MagicMock()
    nli.detect_outcome.return_value = None
    detect_outcome("a" * 500, nli=nli)
    nli.detect_outcome.assert_called_once()


def test_nli_exception_falls_to_none():
    nli = MagicMock()
    nli.detect_outcome.side_effect = RuntimeError("boom")
    assert detect_outcome("嗯不太对吧", nli=nli) is None


def test_whitespace_stripped_before_length_check():
    """Whitespace-only text becomes empty after strip → None."""
    nli = MagicMock()
    assert detect_outcome("   ", nli=nli) is None
    nli.detect_outcome.assert_not_called()
