"""L3 Resolver — entity reference resolution (LLM-free).

Per ADR 0001 § Resolver contract. Spec §3.3.7 / §3.4.10 forbid the LLM
from inventing entity IDs; this module enforces that rule by being the
single function L3 calls to turn a natural-language reference (e.g.
``"yesterday's task"``) into a canonical ``task_id``.

Hard rules (also enforced by canary H10, Step 11):

- **No import of ``jarvis.decision.llm``** anywhere in this file. The
  resolver is a pure function over its two arguments.
- Returning a non-None ``resolved_to`` that is not in
  ``ledger_snapshot.open_tasks()`` is a contract violation.
- Single-candidate path: when exactly one open task exists and
  ``natural_ref`` is non-empty, the resolver must NOT return
  ``confidence="none"`` — the open-task universe is unambiguous.

Day-1 ranking is intentionally trivial (single-open-task wins; empty
ledger returns ``"none"``). Stage 2 will add multi-candidate fuzzy
ranking and a real time-window filter ("yesterday's task").

Layer rules: stdlib + ``jarvis.shared`` + ``jarvis.state``. No imports
from any sibling layer (``jarvis.execution`` / ``jarvis.surface`` /
``jarvis.deployment``) and notably no import of ``jarvis.decision.llm``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from jarvis.state.projections import TaskLedgerSnapshot


# --- Public types -----------------------------------------------------------

ResolverConfidence = Literal["exact", "high", "fuzzy", "none"]
"""Resolver confidence ladder per ADR § Resolver contract."""


@dataclass(frozen=True)
class ResolverResult:
    """Output of :func:`resolve_task_ref`.

    Shape lifted verbatim from ADR § Resolver contract (Day-1).

    Attributes:
        resolved_to: Canonical ``task_id`` when a match is selected,
            otherwise ``None``. Multi-candidate ``fuzzy`` cases set
            ``None`` to make the caller issue a ConfirmationRequest.
        confidence: One of ``"exact"`` / ``"high"`` / ``"fuzzy"`` /
            ``"none"``.
        candidates: Ranked candidate task_ids. Empty tuple when
            ``confidence == "none"``. MUST be non-empty when L3 emits
            ``entity.resolved.outcome in {"resolved", "ambiguous"}``
            (canary H10).
        match_basis: Short human-readable explanation of how the
            match was chosen (or why no match was made).
    """

    resolved_to: str | None
    confidence: ResolverConfidence
    candidates: tuple[str, ...]
    match_basis: str


# --- resolve_task_ref -------------------------------------------------------


def resolve_task_ref(
    natural_ref: str,
    ledger_snapshot: TaskLedgerSnapshot,
) -> ResolverResult:
    """Resolve ``natural_ref`` to a canonical ``task_id`` via the ledger.

    Day-1 rules per ADR § Resolver contract:

    - **Empty natural_ref** (defensive) -> ``confidence="none"`` (the
      caller should not have asked, but a no-match is the safest
      possible outcome).
    - **No open tasks** -> ``confidence="none"``,
      ``match_basis="no open task matched"``.
    - **Exactly one open task** -> ``confidence in {"exact", "high",
      "fuzzy"}`` per match heuristic; ``resolved_to`` is the
      task_id, ``candidates=(that_task_id,)``, never ``"none"``. The
      heuristic picks ``"high"`` when ``natural_ref`` is a substring
      of the task's ``goal`` or ``task_id`` (case-insensitive),
      ``"fuzzy"`` otherwise.
    - **Multi-candidate** -> ``confidence="fuzzy"``,
      ``resolved_to=None`` (caller must issue ConfirmationRequest;
      Stage 2 scenario), ``candidates=(ranked, ...)`` length > 1.

    Args:
        natural_ref: Free-text reference produced by the LLM
            (e.g. ``"yesterday's task"``, ``"the codex task"``).
        ledger_snapshot: Frozen Task Ledger snapshot. The resolver
            consults ``snapshot.open_tasks()`` and never widens to
            non-open tasks Day-1.

    Returns:
        A frozen :class:`ResolverResult`.
    """
    open_tasks = ledger_snapshot.open_tasks()
    natural = natural_ref.strip()

    # Defensive: empty natural ref always reports no match (the caller
    # bug is upstream; we don't paper over it by picking a default).
    if not natural:
        return ResolverResult(
            resolved_to=None,
            confidence="none",
            candidates=(),
            match_basis="empty natural ref",
        )

    if not open_tasks:
        return ResolverResult(
            resolved_to=None,
            confidence="none",
            candidates=(),
            match_basis="no open task matched",
        )

    if len(open_tasks) == 1:
        task = open_tasks[0]
        # ``high`` when the natural ref is a substring of goal/task_id
        # (case-insensitive); ``fuzzy`` otherwise. Either way the
        # single-open-task branch never returns ``"none"`` per ADR
        # contract.
        lowered = natural.lower()
        high_hit = lowered in task.goal.lower() or lowered in task.task_id.lower()
        confidence: ResolverConfidence = "high" if high_hit else "fuzzy"
        basis = (
            "single open task; natural ref substring of goal/task_id"
            if high_hit
            else "single open task; non-empty natural ref"
        )
        return ResolverResult(
            resolved_to=task.task_id,
            confidence=confidence,
            candidates=(task.task_id,),
            match_basis=basis,
        )

    # Multi-candidate path (Stage 2 scenario, but the function must
    # support it per ADR contract). Day-1 ranks by ``created_ts_epoch_ms``
    # descending so the most recent task tops the candidate list. The
    # resolver returns ``resolved_to=None`` to make the caller hit
    # ConfirmationRequest rather than guess.
    ranked = tuple(
        sorted(
            open_tasks,
            key=lambda r: r.created_ts_epoch_ms,
            reverse=True,
        )
    )
    candidates = tuple(record.task_id for record in ranked)
    return ResolverResult(
        resolved_to=None,
        confidence="fuzzy",
        candidates=candidates,
        match_basis="multiple open tasks; ambiguous",
    )


__all__ = [
    "ResolverConfidence",
    "ResolverResult",
    "resolve_task_ref",
]
