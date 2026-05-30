"""Unit tests for :mod:`jarvis.decision.pre_emit_phrases`.

ADR-0002 Step 13. Verifies the canonical limitation / completion
regex sets:

- compile cleanly (constant-load time);
- match seeded text samples covering the F4/F5 acceptance surface;
- preserve the ``已完成`` vs ``已完成报告`` negative-lookahead semantics
  that distinguishes a Day-1 completion claim from a Step-12 Report-
  grade evidence row;
- carry the frozen ``Final[tuple[...]]`` contract — downstream call
  sites assume tuple-stability so the audit hash of pattern source-
  text is constant across the process lifetime.
"""

from __future__ import annotations

import re

import pytest

from jarvis.decision.pre_emit_phrases import COMPLETION_REGEXES, LIMITATION_REGEXES


@pytest.mark.parametrize(
    "text",
    [
        "agent reported on this",
        "reported, not verified",
        "reported not verified",
        "task 未验证",
        "no 没验证",
        "测试都没过",
        "还没验完",
    ],
)
def test_limitation_regexes_match(text: str) -> None:
    """Each seeded limitation phrase trips at least one canonical regex."""
    assert any(p.search(text) for p in LIMITATION_REGEXES), text


@pytest.mark.parametrize(
    "text",
    [
        "完成了任务",       # ^完成
        "已完成",
        "verified by tests",
        "VERIFIED",
        "done!",
        "DONE",
    ],
)
def test_completion_regexes_match(text: str) -> None:
    """Each seeded completion phrase trips at least one canonical regex."""
    assert any(p.search(text) for p in COMPLETION_REGEXES), text


def test_completion_negative_lookahead_excludes_completion_report() -> None:
    r"""``已完成报告`` must NOT match the ``已完成(?!\s*报告)`` pattern.

    Distinguishes a Day-1 completion claim ("已完成") from a Step-12
    Report-grade evidence row ("已完成报告") — both phrases appear in
    the canonical scenario, and only the former should trigger the
    Pre-emit Gate's force-limitation path.
    """
    pattern = next(p for p in COMPLETION_REGEXES if "已完成" in p.pattern)
    assert pattern.search("已完成") is not None
    assert pattern.search("已完成报告") is None
    assert pattern.search("已完成 报告") is None  # whitespace tolerance


def test_completion_caret_anchors_bare_wancheng() -> None:
    r"""``^完成`` matches at start of string but NOT mid-text.

    CJK has no ``\b`` word boundary, so unrooted ``完成`` would
    false-match phrases like ``完成度`` / ``完成情况``; the canonical
    anchor keeps the detection honest.
    """
    pattern = next(p for p in COMPLETION_REGEXES if p.pattern == r"^完成")
    assert pattern.search("完成了任务") is not None
    assert pattern.search("任务完成") is None
    assert pattern.search("完成度评估") is not None  # still starts with 完成


@pytest.mark.parametrize(
    "text",
    [
        "completely fine",          # 'completely' should not trip \bverified\b/\bdone\b
        "predone but unverified",   # word-boundary keeps redone/predone safe
        "redone",
        "agent finished",
    ],
)
def test_completion_regexes_do_not_overmatch(text: str) -> None:
    """Word-boundary anchors keep ``done``/``verified`` from false-matching."""
    assert not any(p.search(text) for p in COMPLETION_REGEXES), text


def test_verified_is_case_insensitive() -> None:
    r"""``\bverified\b`` carries ``re.IGNORECASE`` — F5 invariant."""
    pattern = next(p for p in COMPLETION_REGEXES if "verified" in p.pattern)
    assert pattern.flags & re.IGNORECASE
    assert pattern.search("VERIFIED") is not None
    assert pattern.search("Verified") is not None


def test_done_is_case_insensitive() -> None:
    r"""``\bdone\b`` carries ``re.IGNORECASE`` — F5 invariant."""
    pattern = next(p for p in COMPLETION_REGEXES if "done" in p.pattern)
    assert pattern.flags & re.IGNORECASE
    assert pattern.search("DONE") is not None


def test_reported_not_verified_is_case_insensitive() -> None:
    r"""The ``reported,?\s*not\s+verified`` pattern is case-insensitive."""
    pattern = next(p for p in LIMITATION_REGEXES if "not" in p.pattern)
    assert pattern.flags & re.IGNORECASE
    assert pattern.search("REPORTED, NOT VERIFIED") is not None
    assert pattern.search("reported not verified") is not None


def test_constants_are_tuples() -> None:
    """Frozen contract: tuples, not lists.

    Downstream call sites (canary, scrub, scenario F4/F5) consume the
    constants by iteration; pinning to ``tuple`` keeps the audit
    surface immutable.
    """
    assert isinstance(LIMITATION_REGEXES, tuple)
    assert isinstance(COMPLETION_REGEXES, tuple)


def test_constants_have_expected_arity() -> None:
    """Pinned arity — the ADR's canonical sets are 8 + 4.

    B-0003c added two limitation patterns for the spawn_worker terminal
    failure paths (``超时.{0,4}未完成`` / ``跑挂``), lifting the
    LIMITATION_REGEXES arity from 6 to 8.
    """
    assert len(LIMITATION_REGEXES) == 8
    assert len(COMPLETION_REGEXES) == 4


def test_constants_contain_only_compiled_patterns() -> None:
    """Every entry is an already-compiled ``re.Pattern[str]``.

    Pre-compiling at module import means the first per-turn gate call
    doesn't pay for ``re.compile`` overhead; the type guard locks that
    in.
    """
    for pat in (*LIMITATION_REGEXES, *COMPLETION_REGEXES):
        assert isinstance(pat, re.Pattern)
