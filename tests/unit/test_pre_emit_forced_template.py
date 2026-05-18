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
    ResponsePlan,
    _hard_refusal_plan,
    _scrub_completion_keywords,
)

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
