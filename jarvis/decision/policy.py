"""L3 Effective Policy Resolver — Collaborate mode preset (Day-1 only).

Per ADR 0001 § Stub strategy L3 row + spec §3.4.6 / §3.6.

Day-1 ships the Collaborate preset as static config — the runtime mode
state projection is deferred Stage 2 (ADR § Six-layer boundary contract:
"when mutable mode state exists, it belongs in L2 rather than L3").
The Pre-action Gate consumes ``EffectivePolicy.allowed_tools_per_caller``
which mirrors the Day-1 ToolRegistry caller scoping.

Layer rules: stdlib + ``jarvis.shared``. No imports of sibling layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.shared import RiskLevel


# --- EffectivePolicy --------------------------------------------------------

PolicyMode = Literal["Collaborate"]
"""Day-1 single mode preset. Stage 2 adds ``Safe``/``Autopilot``."""


@dataclass(frozen=True)
class EffectivePolicy:
    """Resolved per-turn policy consumed by the Pre-action Gate.

    Frozen so the gate decision is auditable: passing the same policy
    object and ledger snapshot twice MUST produce the same GateResult.

    Attributes:
        mode: Active mode preset name (Day-1: always ``"Collaborate"``).
        autonomy_ceiling: Highest risk level allowed without explicit
            Allen authorization. Day-1: ``"L2"`` (matches
            ``spawn_worker``'s risk level).
        confirmation_required_at_or_above: Risk threshold at which the
            Pre-action Gate requires a valid ``authorization_lease``
            on the ActionRequest. Day-1: ``"L3"`` (no Day-1 tool
            triggers this; preserved for Stage 2).
        allowed_tools_per_caller: Per-caller frozen sets of tool
            names that caller may dispatch. Derived from the
            ToolRegistry at composition time and cached here so gate
            decisions don't re-query the registry mid-evaluation.
    """

    mode: PolicyMode
    autonomy_ceiling: RiskLevel
    confirmation_required_at_or_above: RiskLevel
    allowed_tools_per_caller: Mapping[CallerPrincipal, frozenset[str]] = field(
        default_factory=dict,
    )


# Risk ladder L0..L4 (matches `jarvis.constitution.AUTONOMY_LEVELS`).
# Index = rank; comparisons in Pre-action Gate use this rank table.
_RISK_LADDER: tuple[RiskLevel, ...] = ("L0", "L1", "L2", "L3", "L4")
_RISK_RANK: Mapping[RiskLevel, int] = {level: rank for rank, level in enumerate(_RISK_LADDER)}


def risk_rank(level: RiskLevel) -> int:
    """Return the index of ``level`` in the L0..L4 ladder.

    Pre-action Gate uses this to compare ``action_request.risk_level``
    against ``EffectivePolicy.autonomy_ceiling`` /
    ``confirmation_required_at_or_above``.
    """
    return _RISK_RANK[level]


# --- effective_policy() -----------------------------------------------------


def effective_policy(
    allowed_tools_per_caller: Mapping[CallerPrincipal, frozenset[str]] | None = None,
) -> EffectivePolicy:
    """Build the Day-1 Collaborate policy preset.

    Day-1 ships exactly one mode (``Collaborate``) — multi-mode runtime
    state is deferred Stage 2 per ADR § Stub strategy. The
    ``allowed_tools_per_caller`` argument is normally provided by the
    composition root after building the ToolRegistry; tests can pass
    ``None`` to get the static Day-1 fallback (which mirrors the
    `build_default_registry` shape).

    Args:
        allowed_tools_per_caller: Per-caller tool-name surface derived
            from the ToolRegistry. Day-1 default: JARVIS_LLM gets
            ``{spawn_worker, verify_diff}``, OBSERVER gets
            ``{verify_diff}``.

    Returns:
        Frozen :class:`EffectivePolicy` ready for the Pre-action Gate.
    """
    if allowed_tools_per_caller is None:
        allowed_tools_per_caller = {
            CallerPrincipal.JARVIS_LLM: frozenset({"spawn_worker", "verify_diff"}),
            CallerPrincipal.OBSERVER: frozenset({"verify_diff"}),
            CallerPrincipal.REGEX_ROUTER: frozenset(),
            CallerPrincipal.WORKER_AGENT: frozenset(),
            CallerPrincipal.BACKGROUND_SUBSCRIBER: frozenset(),
            CallerPrincipal.SYSTEM_MAINTENANCE: frozenset(),
        }
    return EffectivePolicy(
        mode="Collaborate",
        autonomy_ceiling="L2",
        confirmation_required_at_or_above="L3",
        allowed_tools_per_caller=dict(allowed_tools_per_caller),
    )


__all__ = [
    "EffectivePolicy",
    "PolicyMode",
    "effective_policy",
    "risk_rank",
]
