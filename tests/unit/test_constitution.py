"""Unit tests for L1 Constitution — identity + frozen principles + frozen axes."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from jarvis.constitution import (
    AUTONOMY_AXES,
    AUTONOMY_LEVELS,
    JARVIS_IDENTITY,
    NON_GOALS,
    PRINCIPLES,
    AutonomyAxis,
    ConstitutionalPrinciple,
)


def test_identity_label():
    """Product identity is the literal string `Jarvis` per ADR § Identity."""
    assert JARVIS_IDENTITY == "Jarvis"


def test_principles_c1_through_c6_exist():
    """Spec §3.2.1 enumerates C1..C6; all six must be present."""
    assert set(PRINCIPLES.keys()) == {"C1", "C2", "C3", "C4", "C5", "C6"}
    for code, principle in PRINCIPLES.items():
        assert isinstance(principle, ConstitutionalPrinciple)
        assert principle.code == code
        assert principle.statement.strip() != ""


def test_principles_mapping_is_frozen():
    """PRINCIPLES is a MappingProxyType — direct mutation must raise TypeError."""
    with pytest.raises(TypeError):
        PRINCIPLES["C7"] = ConstitutionalPrinciple(  # type: ignore[index]
            code="C7", title="bogus", statement="bogus",
        )
    with pytest.raises(TypeError):
        del PRINCIPLES["C1"]  # type: ignore[attr-defined]


def test_principle_attribute_assignment_raises():
    """ConstitutionalPrinciple is frozen — attribute set raises FrozenInstanceError."""
    c1 = PRINCIPLES["C1"]
    with pytest.raises(FrozenInstanceError):
        c1.title = "mutated"  # type: ignore[misc]


def test_non_goals_is_frozen_sequence():
    """NON_GOALS is a tuple — appending is not even an available method."""
    assert isinstance(NON_GOALS, tuple)
    assert len(NON_GOALS) == 6  # spec §3.2.2 enumerates 6
    assert not hasattr(NON_GOALS, "append")
    with pytest.raises(AttributeError):
        NON_GOALS.append("new non-goal")  # type: ignore[attr-defined]


def test_autonomy_levels_vocabulary():
    """5-level ladder L0..L4 — same vocabulary used by ActionRequest.risk_level."""
    assert AUTONOMY_LEVELS == ("L0", "L1", "L2", "L3", "L4")
    assert isinstance(AUTONOMY_LEVELS, tuple)


def test_autonomy_axes_five_axes_exist():
    """Spec §3.2.6 / ADR `C1-C6 + 5 autonomy axes` — exactly 5 named axes."""
    assert set(AUTONOMY_AXES.keys()) == {
        "Proactivity",
        "Confirmation",
        "Verification",
        "Execution",
        "MemoryWrite",
    }
    for name, axis in AUTONOMY_AXES.items():
        assert isinstance(axis, AutonomyAxis)
        assert axis.name == name


def test_autonomy_axes_mapping_is_frozen():
    """AUTONOMY_AXES is a MappingProxyType — mutation raises TypeError."""
    with pytest.raises(TypeError):
        AUTONOMY_AXES["NewAxis"] = AutonomyAxis(  # type: ignore[index]
            name="NewAxis", meaning="x", default="x",
        )


def test_autonomy_axis_attribute_assignment_raises():
    """AutonomyAxis is frozen — attribute set raises FrozenInstanceError."""
    axis = AUTONOMY_AXES["Proactivity"]
    with pytest.raises(FrozenInstanceError):
        axis.default = "mutated"  # type: ignore[misc]
