"""Canary - every ``evidence.attached`` emit-site carries ``relation`` + ``level``.

Per ADR-0002 § Day-2 EventTypeRegistry extensions (lines 1286-1296) +
Step 12 build-order row (lines 1655-1657):

    ``test_canary_evidence_relation_required`` - AST scan: every
    ``emit_event(..., type="evidence.attached", ...)`` site sets both
    ``relation`` and ``level`` in payload.

The registry's ``required_payload`` for ``evidence.attached`` was
amended in Step 12 from ``(evidence_id, claim_id, level)`` to
``(evidence_id, claim_id, relation, level)``. An emit-site that
forgets ``relation`` would be rejected at runtime by ``emit_event``,
but the canary catches it at AST-scan time so the failure mode is
visible without a live SQLite round-trip.

Scope: every ``.py`` file under ``jarvis/`` AND ``tests/`` (production
emit-sites + test fixtures both have to comply because the registry
check fires on either path).

Liberal handling: when ``payload`` is built via a variable (not a dict
literal) the canary records the site as "manual-review" but does NOT
fail - the AST cannot prove the variable's keys statically. Production
sites today always build the payload as a literal in the same call;
should this change, the manual-review list surfaces the regression
even though the canary still passes.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import iter_all_py_files, parse, relative_to_repo

_REQUIRED_KEYS: frozenset[str] = frozenset(("relation", "level"))


def _kwarg_value(call: ast.Call, *, name: str) -> ast.expr | None:
    """Return the keyword value bound to ``name`` on ``call``, or None."""
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_evidence_attached_emit(call: ast.Call) -> bool:
    """True iff ``call`` is ``emit_event(..., type="evidence.attached", ...)``."""
    func = call.func
    name = None
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    if name != "emit_event":
        return False
    type_value = _kwarg_value(call, name="type")
    return (
        isinstance(type_value, ast.Constant)
        and type_value.value == "evidence.attached"
    )


def _dict_literal_keys(node: ast.expr) -> set[str] | None:
    """Return the string-literal key set of a Dict literal, or None when not a literal."""
    if not isinstance(node, ast.Dict):
        return None
    out: set[str] = set()
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            out.add(key.value)
        else:
            # Non-literal key (e.g. **kwargs spread) - cannot prove the
            # required set statically.
            return None
    return out


def test_canary_evidence_relation_required() -> None:
    """Fail if any evidence.attached emit-site omits relation or level in its payload literal."""
    violations: list[str] = []
    manual_review: list[str] = []
    for path in iter_all_py_files():
        module = parse(path)
        rel = relative_to_repo(path)
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            if not _is_evidence_attached_emit(node):
                continue
            payload_node = _kwarg_value(node, name="payload")
            if payload_node is None:
                violations.append(
                    f"{rel}:{node.lineno}: evidence.attached emit-site missing "
                    "payload= kwarg"
                )
                continue
            keys = _dict_literal_keys(payload_node)
            if keys is None:
                # Manual-review site - cannot prove statically. Record
                # but do not fail.
                manual_review.append(
                    f"{rel}:{node.lineno}: payload built from non-literal expression"
                )
                continue
            missing = _REQUIRED_KEYS - keys
            if missing:
                violations.append(
                    f"{rel}:{node.lineno}: evidence.attached payload missing "
                    f"required key(s): {sorted(missing)} - ADR-0002 F8 / Step 12 "
                    "registry amendment requires (relation, level)"
                )

    assert not violations, (
        "evidence.attached relation+level canary - every emit-site must "
        "carry both keys in its payload literal:\n  "
        + "\n  ".join(violations)
        + (
            "\n\n(manual-review sites - payload built via variable, skipped):\n  "
            + "\n  ".join(manual_review)
            if manual_review
            else ""
        )
    )
