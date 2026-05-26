"""H10 — Resolver is LLM-free and emits non-empty candidates.

Per ADR 0001 § Acceptance criterion H10:

> AST scan: the resolver module / function does not import or call
> ``jarvis.decision.llm`` (LLM-free). And: every emitted
> ``entity.resolved`` event with ``outcome in {"resolved", "ambiguous"}``
> has a non-empty ``candidates`` field — empty candidates with
> non-None ``resolved_to`` is a contract violation.

Two parts:

- **Part A (AST):** ``jarvis/decision/resolver.py`` contains no
  ``Import`` / ``ImportFrom`` referencing ``jarvis.decision.llm`` and
  no :class:`ast.Call` whose dotted path includes
  ``jarvis.decision.llm``.
- **Part B (runtime shape):** exercise :func:`resolve_task_ref` against
  empty / single / multi-task snapshots and assert that whenever the
  result maps to ``outcome in {"resolved", "ambiguous"}`` the
  ``candidates`` tuple is non-empty.

The Part B test does not call any LLM and does not import
``unittest.mock``; the snapshots are built from real L2 projection
records.
"""

from __future__ import annotations

import ast

from jarvis.decision.resolver import ResolverResult, resolve_task_ref
from jarvis.state.projections import (
    ClaimEvidenceProjection,
    TaskLedgerRecord,
    TaskLedgerSnapshot,
)
from tests.canary._helpers import parse, repo_root

# --- Part A: AST scan -------------------------------------------------------


def _attribute_chain(node: ast.AST) -> list[str]:
    """Render an :class:`ast.Attribute` chain as a dotted list of names."""
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return list(reversed(parts))


def test_resolver_module_imports_no_llm() -> None:
    """``jarvis/decision/resolver.py`` has no LLM imports or calls."""
    path = repo_root() / "jarvis" / "decision" / "resolver.py"
    module = parse(path)

    import_violations: list[str] = []
    call_violations: list[str] = []

    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            import_violations.extend(
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name == "jarvis.decision.llm"
                or alias.name.startswith("jarvis.decision.llm.")
            )
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.module == "jarvis.decision.llm" or node.module.startswith(
                "jarvis.decision.llm."
            ):
                import_violations.append(
                    f"line {node.lineno}: from {node.module} import ..."
                )
        elif isinstance(node, ast.Call):
            chain = _attribute_chain(node.func)
            joined = ".".join(chain)
            if "jarvis.decision.llm" in joined:
                call_violations.append(
                    f"line {node.lineno}: call to {joined}(...)"
                )

    assert not import_violations, (
        "H10 part A — resolver imports jarvis.decision.llm:\n  "
        + "\n  ".join(import_violations)
    )
    assert not call_violations, (
        "H10 part A — resolver calls into jarvis.decision.llm:\n  "
        + "\n  ".join(call_violations)
    )


# --- Part B: runtime shape ---------------------------------------------------


def _make_snapshot(records: list[TaskLedgerRecord]) -> TaskLedgerSnapshot:
    """Build a frozen :class:`TaskLedgerSnapshot` for the given records.

    Uses an empty Claim/Evidence projection — `derive_status` for an
    open task with no worker reports / no task.verified events returns
    ``"open"`` per :mod:`jarvis.state.projections`. This puts every
    seeded record into ``open_tasks()``.
    """
    claim_evidence = ClaimEvidenceProjection(
        claims_by_id={},
        evidence_by_claim_id={},
        claim_ids_by_subject_ref={},
    )
    return TaskLedgerSnapshot(
        records_by_task_id={r.task_id: r for r in records},
        claim_evidence=claim_evidence,
    )


def _make_record(task_id: str, goal: str, ts: int) -> TaskLedgerRecord:
    return TaskLedgerRecord(
        task_id=task_id,
        goal=goal,
        created_event_uid=task_id + "-uid",
        created_ts_epoch_ms=ts,
    )


def _outcome_from(result: ResolverResult) -> str:
    """Mirror of ``jarvis.decision._resolver_outcome`` outcome mapping.

    The canary does not import the private ``_resolver_outcome`` helper
    (it's intentionally underscore-prefixed); we replicate the table
    here so the canary stays self-contained per ADR § H stricter rules.
    The 0-candidate outcome is ``"not_found"`` per ADR § Resolver
    contract (ladder = ``{resolved, ambiguous, not_found}``).
    """
    if result.confidence in ("exact", "high"):
        return "resolved"
    if result.confidence == "fuzzy":
        if result.resolved_to is not None:
            return "resolved"
        return "ambiguous"
    return "not_found"


def test_resolver_emits_non_empty_candidates_on_resolved_or_ambiguous() -> None:
    """Every ``resolved`` / ``ambiguous`` outcome has at least one candidate."""
    # Single-task snapshot — yields "resolved".
    single = _make_snapshot([_make_record("task_alpha", "do alpha", 1000)])
    res_single = resolve_task_ref("alpha", single)
    if _outcome_from(res_single) in {"resolved", "ambiguous"}:
        assert res_single.candidates, (
            "H10 part B — single-task `resolved` outcome had empty candidates"
        )

    # Multi-task snapshot — yields "ambiguous" (resolved_to is None).
    multi = _make_snapshot(
        [
            _make_record("task_alpha", "do alpha", 1000),
            _make_record("task_beta", "do beta", 2000),
        ]
    )
    res_multi = resolve_task_ref("yesterday's task", multi)
    if _outcome_from(res_multi) in {"resolved", "ambiguous"}:
        assert res_multi.candidates, (
            "H10 part B — multi-task `ambiguous` outcome had empty candidates"
        )


def test_resolver_never_pairs_non_none_resolved_to_with_empty_candidates() -> None:
    """``resolved_to`` non-None implies ``candidates`` non-empty (universal)."""
    snapshots: list[TaskLedgerSnapshot] = [
        _make_snapshot([]),  # no tasks
        _make_snapshot([_make_record("task_solo", "g", 1)]),  # single
        _make_snapshot(
            [
                _make_record("task_a", "ga", 1),
                _make_record("task_b", "gb", 2),
            ]
        ),  # multi
    ]
    refs = ["", "alpha", "yesterday's task"]
    for snapshot in snapshots:
        for ref in refs:
            result = resolve_task_ref(ref, snapshot)
            if result.resolved_to is not None:
                assert result.candidates, (
                    "H10 part B — resolver returned non-None resolved_to with "
                    f"empty candidates (ref={ref!r}, result={result!r})"
                )
