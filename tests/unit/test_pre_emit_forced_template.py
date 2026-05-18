"""Unit tests for the Pre-emit forced-limitation safety net (F2 fix).

Covers the two-line defense in depth added to
:mod:`jarvis.decision.__init__`:

- :func:`_scrub_completion_keywords` — redacts completion-class
  language out of the LLM's draft before it is embedded into
  ``_FORCED_LIMITATION_TEMPLATE``.
- :func:`_hard_refusal_plan` — fixed limitation ResponsePlan with no
  LLM-supplied text; last line of defense when the scrubbed-and-
  templated text still trips the Pre-emit Gate.

Regression: before F2, the forced template embedded the LLM draft
verbatim, so an adversarial draft of ``"done"`` survived into the
surface output even though ``permission=force_limitation_language``.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

import jarvis.decision as decision_pkg
from jarvis.decision import (
    _COMPLETION_SCRUB_PATTERNS,
    ResponsePlan,
    _hard_refusal_plan,
    _scrub_completion_keywords,
)
from jarvis.decision.gates import _COMPLETION_KEYWORDS

# F5 gate completion-detection patterns (verbatim from
# ``jarvis.decision.gates._COMPLETION_KEYWORDS``). The hard-refusal text
# must match NONE of these.
_GATE_COMPLETION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"完成"),
    re.compile(r"已完成"),
    re.compile(r"\bverified\b", re.IGNORECASE),
    re.compile(r"\bdone\b", re.IGNORECASE),
)


# --- _scrub_completion_keywords --------------------------------------------


def test_scrub_completion_keywords_redacts_english() -> None:
    """English completion words are replaced by the redaction marker."""
    scrubbed = _scrub_completion_keywords("Status: done")
    assert "[redacted-completion-claim]" in scrubbed
    # ``done`` must not survive as a whole word — case-insensitive.
    assert re.search(r"\bdone\b", scrubbed, re.IGNORECASE) is None


def test_scrub_completion_keywords_redacts_chinese() -> None:
    """Chinese completion phrase is replaced by the redaction marker."""
    scrubbed = _scrub_completion_keywords("任务已完成")
    assert "已完成" not in scrubbed
    assert "[redacted-completion-claim]" in scrubbed


def test_scrub_completion_keywords_preserves_negations() -> None:
    r"""``\bverified\b`` matches after ``not`` — that's fine for F4/F5.

    The scrub redacts "verified" even when preceded by "not"; the
    resulting "agent reported, not [redacted-completion-claim]" still
    satisfies F4 (limitation language present via ``not``) and F5 (no
    bare completion keyword — the gate's regex won't match the marker).
    """
    scrubbed = _scrub_completion_keywords("agent reported, not verified")
    assert "not" in scrubbed
    assert "[redacted-completion-claim]" in scrubbed
    # ``verified`` as a whole word must not survive.
    assert re.search(r"\bverified\b", scrubbed, re.IGNORECASE) is None


def test_scrub_completion_keywords_passthrough() -> None:
    """Text with no completion keywords is returned unchanged."""
    assert _scrub_completion_keywords("Hello world") == "Hello world"


# --- _hard_refusal_plan ----------------------------------------------------


def test_hard_refusal_plan_shape() -> None:
    """Hard-refusal plan is scrub-safe and shaped per ResponsePlan."""
    plan = _hard_refusal_plan("task_X")

    assert isinstance(plan, ResponsePlan)
    assert plan.permission == "force_limitation_language"
    assert plan.downgrade_required is False
    # F4 limitation language present (via "未验证" + "unverified" +
    # "agent reported" — all sit in the negative-test's
    # ``_LIMITATION_PATTERNS`` / ``_NEGATION_MARKERS`` sets).
    assert "未验证" in plan.text
    assert "unverified" in plan.text

    # F5: text must NOT match any of the gate's completion-detection
    # patterns. If this fails, the hard-refusal would itself be
    # downgrade-required if re-evaluated through the gate — a
    # regression. Note ``\bverified\b`` matches "verified" but NOT
    # "unverified" (no word boundary between ``n`` and ``v``).
    for pat in _GATE_COMPLETION_PATTERNS:
        assert pat.search(plan.text) is None, (
            f"hard-refusal text matched completion pattern {pat.pattern!r}: "
            f"{plan.text!r}"
        )

    # response_hash is sha256 hex of the text (64 hex chars).
    assert len(plan.response_hash) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", plan.response_hash) is not None
    assert plan.response_hash == hashlib.sha256(plan.text.encode("utf-8")).hexdigest()


# --- _finalize_response wiring (AST verification) --------------------------


def test_finalize_response_wires_scrub_and_hard_refusal() -> None:
    """``_finalize_response`` must call both new helpers (regression guard).

    AST walk over the function body confirms the two defense-in-depth
    helpers are wired in. This is the automated regression test that
    would have caught the F2 bug — the original code embedded the
    draft verbatim and unconditionally took the forced_plan.
    """
    source_path = Path(decision_pkg.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    finalize_fn: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_finalize_response":
            finalize_fn = node
            break
    assert finalize_fn is not None, "_finalize_response not found in module AST"

    called_names: set[str] = set()
    for sub in ast.walk(finalize_fn):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            called_names.add(sub.func.id)

    assert "_scrub_completion_keywords" in called_names, (
        "F2 regression: _finalize_response no longer scrubs the LLM draft "
        "before embedding it into _FORCED_LIMITATION_TEMPLATE."
    )
    assert "_hard_refusal_plan" in called_names, (
        "F2 regression: _finalize_response no longer falls back to "
        "_hard_refusal_plan when the scrubbed forced text still trips "
        "the Pre-emit Gate."
    )


# --- Completion-keyword coverage drift guard -------------------------------
#
# Two completion-keyword regex lists live in the codebase by design:
#
# - ``jarvis.decision.gates._COMPLETION_KEYWORDS`` — what the gate
#   detects to decide whether to force limitation language.
# - ``jarvis.decision._COMPLETION_SCRUB_PATTERNS`` — what the forced
#   template scrubs out before re-embedding the LLM draft.
#
# Detection and rendering are different responsibilities (the gate may
# want to detect synonyms it doesn't itself redact; the scrub may want
# to redact synonyms the gate doesn't detect because they're stylistic
# variants). To keep that intentional asymmetry honest, every gate
# pattern must have an explicit scrub-coverage decision below. Adding
# a new gate keyword without updating this mapping fails Tier 1.

_GATE_TO_SCRUB_COVERAGE: dict[str, str] = {
    # Bare ``完成`` — gate detects mid-text; scrub only redacts at the
    # start of the string because CJK has no ``\b`` word boundary and
    # mid-text ``完成`` is a false-positive magnet ("完成度", "完成情况").
    # Asymmetric by design: when the gate trips on mid-text ``完成``, the
    # forced template re-trips and ``_hard_refusal_plan`` is the final
    # defense.
    r"完成": r"^完成",
    # ``已完成`` with a "报告" allowance — identical on both sides.
    r"已完成": r"已完成(?!\s*报告)",
    # English completion keywords — symmetric.
    r"\bverified\b": r"\bverified\b",
    r"\bdone\b": r"\bdone\b",
}

# Scrub patterns with no gate counterpart — synonyms the scrub redacts
# defensively even though the gate's detection set doesn't trigger on
# them. Adding patterns here is the explicit "scrub-only by design"
# decision the drift guard requires.
_SCRUB_ONLY_PATTERNS: frozenset[str] = frozenset(
    {
        # English completion synonyms the gate doesn't detect today but
        # the scrub redacts anyway so the forced template doesn't carry
        # them through to the surface verbatim.
        r"\bcompleted\b",
        r"\bfinished\b",
    },
)


def test_completion_scrub_covers_every_gate_keyword() -> None:
    """Every gate completion keyword must declare a scrub-coverage decision."""
    gate_pattern_strings = {pat.pattern for pat in _COMPLETION_KEYWORDS}
    scrub_pattern_strings = set(_COMPLETION_SCRUB_PATTERNS)

    # 1) Every gate pattern is declared in the coverage table.
    undeclared_gate = gate_pattern_strings - _GATE_TO_SCRUB_COVERAGE.keys()
    assert not undeclared_gate, (
        "_COMPLETION_KEYWORDS added these patterns without scrub-coverage "
        f"decision: {sorted(undeclared_gate)!r}. Update "
        "`_GATE_TO_SCRUB_COVERAGE` in this test to declare whether each "
        "new gate keyword should be scrubbed identically, scrubbed via a "
        "variant, or deliberately not scrubbed (in which case the entry "
        "should still exist with a documenting comment)."
    )

    # 2) Every declared counterpart actually lives in the scrub set.
    declared_scrub = set(_GATE_TO_SCRUB_COVERAGE.values())
    missing_scrub = declared_scrub - scrub_pattern_strings
    assert not missing_scrub, (
        "_COMPLETION_SCRUB_PATTERNS is missing the declared "
        f"counterparts: {sorted(missing_scrub)!r}. The drift guard's "
        "coverage table got ahead of the runtime list — sync them."
    )

    # 3) Every scrub pattern is either a declared gate counterpart or
    #    in the documented scrub-only set. A new scrub entry without
    #    one of those two homes is an undeclared addition — could be
    #    intentional, but must be made explicit.
    accounted_for = declared_scrub | _SCRUB_ONLY_PATTERNS
    undeclared_scrub = scrub_pattern_strings - accounted_for
    assert not undeclared_scrub, (
        "_COMPLETION_SCRUB_PATTERNS contains undeclared entries: "
        f"{sorted(undeclared_scrub)!r}. Each scrub pattern must either "
        "(a) appear in `_GATE_TO_SCRUB_COVERAGE` as a gate counterpart, "
        "or (b) appear in `_SCRUB_ONLY_PATTERNS` with a comment "
        "documenting why it's scrub-only."
    )
