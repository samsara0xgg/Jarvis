"""L3 Effective Policy Resolver — Collaborate mode preset (Day-1 only).

Per ADR 0001 § Stub strategy L3 row + spec §3.4.6 / §3.6. Extended by
ADR 0011 D1: ``EffectivePolicy`` now carries all nine spec §3.4.5
output fields, six of them declared placeholders pending Phase 4.

Day-1 ships the Collaborate preset as static config — the runtime mode
state projection is deferred Stage 2 (ADR § Six-layer boundary contract:
"when mutable mode state exists, it belongs in L2 rather than L3").
``ModeRuntimeState`` below is a v0 *constant*, not mutable state, so
that contract is not yet triggered; ADR-0011 §10's Module Map places
it in this file for now. It moves to L2 as an event-sourced
projection once the policy-engine ADR lands.
The Pre-action Gate consumes ``EffectivePolicy.allowed_tool_surface``
which mirrors the Day-1 ToolRegistry caller scoping.

Layer rules: stdlib + ``jarvis.shared``. No imports of sibling layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Protocol

from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from jarvis.shared import RiskLevel


# --- EffectivePolicy --------------------------------------------------------

PolicyMode = Literal["Collaborate"]
"""Day-1 single mode preset. Stage 2 adds ``Safe``/``Autopilot``."""


@dataclass(frozen=True)
class EffectivePolicy:
    """Resolved per-turn policy consumed by the Pre-action Gate.

    Frozen so the gate decision is auditable: passing the same policy
    object and ledger snapshot twice MUST produce the same GateResult.

    Carries ``mode`` plus all nine spec §3.4.5 output fields (ADR
    0011 D1 added the nine; ``mode`` predates them). Four fields are
    live-consumed today: ``mode`` (interpolated into the
    ``caller_allowed`` refuse-reason string, ``gates.py:205``),
    ``autonomy_ceiling``, ``confirmation_threshold``, and
    ``allowed_tool_surface``. The remaining six §3.4.5 fields are
    declared placeholders — each holds its v1 constant and names the
    ADR that will give it a real consumer.

    Attributes:
        mode: Active mode preset name (Day-1: always ``"Collaborate"``).
        autonomy_ceiling: Highest risk level the Pre-action Gate will
            let be *proposed* at all (check 3, ``gates.py`` ~line
            228) — anything ranked above it is refused outright, and
            no lease can rescue it. This is a separate axis from
            ``confirmation_threshold`` (check 4, ``gates.py`` ~line
            238), which independently decides what needs a lease.
            v1 pairs both at ``"L3"`` (ADR 0011 D1 — raised from
            ``"L2"`` so L3 proposals are reachable at all): an L3
            action IS proposable, but under that same pairing it
            always needs a valid lease before it can pass.
        confirmation_threshold: Risk threshold at which the
            Pre-action Gate requires a valid ``authorization_lease``
            on the ActionRequest. v1: ``"L3"``. Per invariant I7,
            confirmation for L3/L4 is an invariant floor:
            ``confirmation_threshold`` is a dial that may move only
            below L3 — no mode or policy engine may ever set L3+ to
            no-confirm.
        allowed_tool_surface: Per-caller frozen sets of tool
            names that caller may dispatch. Derived from the
            ToolRegistry at composition time and cached here so gate
            decisions don't re-query the registry mid-evaluation.
        output_form: Declared placeholder carrying its v1 constant
            ``"conversational"``. Future consumer: the response
            rendering layer, wired when the policy engine (Phase 4)
            lands per ADR 0011 D1.
        verification_level: Declared placeholder carrying its v1
            constant ``"standard"``. Future consumer: verification
            depth selection, wired when the policy engine (Phase 4)
            lands per ADR 0011 D1.
        interrupt_policy: Declared placeholder carrying its v1
            constant ``"collaborate_default"``. Future consumer:
            interrupt handling, wired when the policy engine
            (Phase 4) lands per ADR 0011 D1.
        memory_write_policy: Declared placeholder carrying its v1
            constant ``"propose_only"``. Future consumer: memory
            write gating, wired when the policy engine (Phase 4)
            lands per ADR 0011 D1.
        task_policy: Declared placeholder carrying its v1 constant
            ``"explicit_only"``. Future consumer: task-creation
            gating, wired when the policy engine (Phase 4) lands per
            ADR 0011 D1.
        attention_defaults: Declared placeholder carrying its v1
            constant ``{}`` (empty map). Value type ``object`` is
            provisional: ``attention_policy()`` (``gates.py`` ~line
            482) actually computes an ``AttentionChannel`` from the
            situation packet, the claim-evidence projection, and two
            review flags — not a str-to-str mapping — so no real
            shape is settled until Phase 4 folds ``attention_policy()``
            into the policy engine (ADR 0011 D1).
    """

    mode: PolicyMode
    autonomy_ceiling: RiskLevel
    confirmation_threshold: RiskLevel
    allowed_tool_surface: Mapping[CallerPrincipal, frozenset[str]] = field(
        default_factory=dict,
    )
    output_form: str = "conversational"
    verification_level: str = "standard"
    interrupt_policy: str = "collaborate_default"
    memory_write_policy: str = "propose_only"
    task_policy: str = "explicit_only"
    attention_defaults: Mapping[str, object] = field(default_factory=dict)


# Risk ladder L0..L4 (matches `jarvis.constitution.AUTONOMY_LEVELS`).
# Index = rank; comparisons in Pre-action Gate use this rank table.
_RISK_LADDER: tuple[RiskLevel, ...] = ("L0", "L1", "L2", "L3", "L4")
_RISK_RANK: Mapping[RiskLevel, int] = {level: rank for rank, level in enumerate(_RISK_LADDER)}


def risk_rank(level: RiskLevel) -> int:
    """Return the index of ``level`` in the L0..L4 ladder.

    Pre-action Gate uses this to compare ``action_request.risk_level``
    against ``EffectivePolicy.autonomy_ceiling`` /
    ``confirmation_threshold``.
    """
    return _RISK_RANK[level]


# --- Mode Runtime State (v0, constant) --------------------------------------


@dataclass(frozen=True)
class ModeRuntimeState:
    """Mode Runtime State — v0 constant, not yet event-sourced.

    Spec §3.3.6 defines Mode Runtime State as an event-sourced State
    Object projection folded from ``mode.transitioned`` /
    ``lens.enabled`` / ``override.applied``. v0 (this ADR, 0011)
    ships it as a frozen constant instead: those three event types
    have no registration and no fold in this ADR. Registration is
    deferred to the policy-engine ADR (ADR-0011 §11 defer table).

    Spec §11 requires the Policy Resolver to stay a pure function
    that only interprets state and config, never creates state —
    ``effective_policy()`` below honors that by taking this object as
    a parameter rather than reading any mutable source itself.
    """

    mode: PolicyMode = "Collaborate"
    lenses: tuple[str, ...] = ()
    overrides: tuple[str, ...] = ()


COLLABORATE_MODE_STATE: Final[ModeRuntimeState] = ModeRuntimeState()
"""The only Mode Runtime State value that exists until the policy engine ADR.

ADR-0011 §3 D1 refers to this constant as ``COLLABORATE_CONSTANT``; the
code keeps ``COLLABORATE_MODE_STATE`` as the more descriptive name.
"""


# --- effective_policy() -----------------------------------------------------


def effective_policy(
    allowed_tool_surface: Mapping[CallerPrincipal, frozenset[str]] | None = None,
    *,
    mode_state: ModeRuntimeState = COLLABORATE_MODE_STATE,
) -> EffectivePolicy:
    """Build the Collaborate policy preset (ADR 0011 D1: nine §3.4.5 fields).

    Day-1 ships exactly one mode (``Collaborate``) — multi-mode runtime
    state is deferred Stage 2 per ADR § Stub strategy. The
    ``allowed_tool_surface`` argument is normally provided by the
    composition root after building the ToolRegistry; tests can pass
    ``None`` to get the static Day-1 fallback (which mirrors the
    `build_default_registry` shape).

    Per invariant I7, confirmation for L3/L4 is an invariant floor:
    ``confirmation_threshold`` is a dial that may move only below L3;
    no mode or policy engine may ever set L3+ to no-confirm.

    Args:
        allowed_tool_surface: Per-caller tool-name surface derived
            from the ToolRegistry. Day-1 default: JARVIS_LLM gets
            ``{spawn_worker, verify_diff}``, OBSERVER gets
            ``{verify_diff}``.
        mode_state: Mode Runtime State to resolve against. Defaults
            to the only value that exists pre-policy-engine,
            :data:`COLLABORATE_MODE_STATE`.

    Returns:
        Frozen :class:`EffectivePolicy` ready for the Pre-action Gate.
    """
    if allowed_tool_surface is None:
        allowed_tool_surface = {
            CallerPrincipal.JARVIS_LLM: frozenset({"spawn_worker", "verify_diff"}),
            CallerPrincipal.OBSERVER: frozenset({"verify_diff"}),
            CallerPrincipal.REGEX_ROUTER: frozenset(),
            CallerPrincipal.WORKER_AGENT: frozenset(),
            CallerPrincipal.BACKGROUND_SUBSCRIBER: frozenset(),
            CallerPrincipal.SYSTEM_MAINTENANCE: frozenset(),
        }
    return EffectivePolicy(
        mode=mode_state.mode,
        autonomy_ceiling="L3",
        confirmation_threshold="L3",
        allowed_tool_surface=dict(allowed_tool_surface),
        output_form="conversational",
        verification_level="standard",
        interrupt_policy="collaborate_default",
        memory_write_policy="propose_only",
        task_policy="explicit_only",
        attention_defaults={},
    )


# --- surface_for() -----------------------------------------------------------
#
# `policy.py` (L3) must not import `jarvis.execution` (sibling layer) and
# cannot import from `jarvis.decision.__init__` (import cycle:
# decision/__init__ -> gates -> policy). The structural Protocols below
# are the layer seam: the real L4 `ToolDefinition` / `ToolRegistry`
# satisfy them structurally, and a frozen dataclass attribute satisfies a
# read-only `@property` in a Protocol. Kept to exactly what `surface_for`
# and `validate_requires_confirmation` below read — not a mirror of
# `jarvis.decision.ToolDefinitionLike`.
#
# `_RegistryLike` is generic in the tool type so `surface_for` returns
# whatever concrete/structural tool type the caller's registry already
# promises (e.g. `jarvis.decision.ToolDefinitionLike`, which carries
# `description` / `input_schema` that `_ToolLike` deliberately omits)
# instead of narrowing every caller down to the two fields filtering
# needs.

class _ToolLike(Protocol):
    """Structural view of a registered tool — name + risk_level only."""

    @property
    def name(self) -> str:
        """Tool name (matches an entry in ``allowed_tool_surface``)."""
        ...

    @property
    def risk_level(self) -> RiskLevel:
        """Risk level compared against ``autonomy_ceiling``."""
        ...


class _ConfirmationToolLike(_ToolLike, Protocol):
    """``_ToolLike`` plus ``requires_confirmation``, for the boot validator."""

    @property
    def requires_confirmation(self) -> bool:
        """Whether dispatch needs a valid ``authorization_lease``."""
        ...


class _RegistryLike[ToolT: _ToolLike](Protocol):
    """Structural view of L4 ``ToolRegistry`` — only what filtering needs."""

    def for_caller(self, caller_principal: CallerPrincipal) -> tuple[ToolT, ...]:
        """Return the tools ``caller_principal`` is intrinsically allowed to call."""
        ...


def surface_for[ToolT: _ToolLike](
    policy: EffectivePolicy,
    registry: _RegistryLike[ToolT],
    caller: CallerPrincipal,
) -> tuple[ToolT, ...]:
    """Return the least-capability tool menu for ``caller`` (ADR 0011 D2).

    Wraps ``registry.for_caller(caller)`` (intrinsic caller scoping,
    unchanged) and keeps only tools that are BOTH:

    1. named in ``policy.allowed_tool_surface.get(caller, frozenset())``, and
    2. within the autonomy ceiling: ``risk_rank(tool.risk_level) <=
       risk_rank(policy.autonomy_ceiling)``.

    Tools above the ceiling simply do not appear in the returned tuple —
    spec §14: least-capability surface; §14.8: the registry must really
    filter, not the prompt. This is the call site
    ``jarvis/decision/__init__.py``'s Tier 2 loop uses to build the
    JARVIS_LLM menu; other ``for_caller`` call sites (Tier 0 boot
    validation, Tier 0 name resolution) are untouched by this function.
    """
    allowed_names = policy.allowed_tool_surface.get(caller, frozenset())
    ceiling_rank = risk_rank(policy.autonomy_ceiling)
    return tuple(
        tool
        for tool in registry.for_caller(caller)
        if tool.name in allowed_names and risk_rank(tool.risk_level) <= ceiling_rank
    )


# --- Boot validation: requires_confirmation consistency ----------------------


class PolicyConsistencyError(ValueError):
    """Raised when a tool's ``requires_confirmation`` drifts from risk vs threshold.

    The composition root (``jarvis.runtime``) converts this into
    ``RuntimeBootstrapError`` so a misdeclared tool fails the daemon
    start loudly instead of silently under- or over-gating dispatch.
    """


def validate_requires_confirmation(
    tool_defs: Iterable[_ConfirmationToolLike],
    confirmation_threshold: RiskLevel,
) -> None:
    """Assert every tool's ``requires_confirmation`` matches its risk rank.

    Per ADR 0011 D2 (and V6): ``requires_confirmation`` is stored
    explicitly on ``ToolDefinition`` rather than always recomputed, so
    ADR-0012 can render its confirmation template line without
    recomputing policy — but that redundancy only stays safe if it can
    never drift from ``risk_rank(risk_level) >=
    risk_rank(confirmation_threshold)``. This pure function is the boot-time
    check; it takes ``confirmation_threshold`` as an explicit
    :class:`RiskLevel` rather than a whole policy object to stay a
    narrow, single-purpose check.

    Raises:
        PolicyConsistencyError: naming the offending tool and both the
            actual and expected ``requires_confirmation`` values.
    """
    threshold_rank = risk_rank(confirmation_threshold)
    for tool in tool_defs:
        expected = risk_rank(tool.risk_level) >= threshold_rank
        if tool.requires_confirmation != expected:
            msg = (
                f"tool {tool.name!r}: requires_confirmation="
                f"{tool.requires_confirmation!r} but risk_level="
                f"{tool.risk_level!r} vs confirmation_threshold="
                f"{confirmation_threshold!r} implies requires_confirmation="
                f"{expected!r}"
            )
            raise PolicyConsistencyError(msg)


__all__ = [
    "COLLABORATE_MODE_STATE",
    "EffectivePolicy",
    "ModeRuntimeState",
    "PolicyConsistencyError",
    "PolicyMode",
    "effective_policy",
    "risk_rank",
    "surface_for",
    "validate_requires_confirmation",
]
