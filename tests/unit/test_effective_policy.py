"""Unit tests for ``jarvis.decision.policy.effective_policy`` (ADR § Stub strategy).

Day-1 ships the Collaborate preset only. Validate:

- Mode is exactly ``"Collaborate"``.
- autonomy_ceiling = ``"L2"``.
- confirmation_required_at_or_above = ``"L3"``.
- allowed_tools_per_caller has ``spawn_worker`` for JARVIS_LLM.
- allowed_tools_per_caller has ``verify_diff`` for OBSERVER.
- Custom surface override is honored.
- ``risk_rank`` orders the ladder correctly.
"""

from __future__ import annotations

import dataclasses

import pytest

from jarvis.decision.policy import EffectivePolicy, effective_policy, risk_rank
from jarvis.shared import CallerPrincipal


def test_effective_policy_default_collaborate_preset():
    """Default policy is the Collaborate preset with the documented ladder."""
    policy = effective_policy()
    assert isinstance(policy, EffectivePolicy)
    assert policy.mode == "Collaborate"
    assert policy.autonomy_ceiling == "L2"
    assert policy.confirmation_required_at_or_above == "L3"


def test_effective_policy_jarvis_llm_has_spawn_worker_and_verify_diff():
    """JARVIS_LLM's tool surface includes both Day-1 tools."""
    policy = effective_policy()
    surface = policy.allowed_tools_per_caller[CallerPrincipal.JARVIS_LLM]
    assert "spawn_worker" in surface
    assert "verify_diff" in surface


def test_effective_policy_observer_only_verify_diff():
    """OBSERVER may verify but not spawn workers."""
    policy = effective_policy()
    surface = policy.allowed_tools_per_caller[CallerPrincipal.OBSERVER]
    assert "verify_diff" in surface
    assert "spawn_worker" not in surface


def test_effective_policy_custom_surface_is_honored():
    """Explicit allowed_tools_per_caller overrides the Day-1 fallback."""
    custom = {
        CallerPrincipal.JARVIS_LLM: frozenset({"only_this_tool"}),
    }
    policy = effective_policy(allowed_tools_per_caller=custom)
    assert policy.allowed_tools_per_caller[CallerPrincipal.JARVIS_LLM] == frozenset(
        {"only_this_tool"},
    )


def test_risk_rank_orders_l0_through_l4_ascending():
    """L0..L4 ladder ranks: L0 < L1 < L2 < L3 < L4."""
    assert risk_rank("L0") < risk_rank("L1") < risk_rank("L2") < risk_rank("L3") < risk_rank("L4")


def test_policy_is_frozen():
    """EffectivePolicy is a frozen dataclass — mutating raises."""
    policy = effective_policy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(policy, "mode", "Safe")  # noqa: B010 — frozen-dataclass FrozenInstanceError requires the descriptor path.
