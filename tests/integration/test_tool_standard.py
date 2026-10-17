"""The tool standard's mechanical rules hold for every built-in tool (ADR 0290).

Runs ``scripts.check_tools`` over the menu this checkout offers, so a new tool that breaks a
rule, or a rebuilt one still listed as pre-standard, fails here.
"""

from __future__ import annotations

from scripts.check_tools import PRE_STANDARD, broken_rules, checked_tools, failures


def test_every_built_in_tool_keeps_the_standard_or_is_listed_as_pre_standard() -> None:
    """No rule is broken outside the pre-standard list, and the list has no stale entry."""
    assert failures(checked_tools()) == []


def test_each_rule_is_caught() -> None:
    """Name, length, language, "the user" and an undescribed parameter each fail."""
    broken = broken_rules(
        "MyTool",
        "x" * 601 + " 中文 the user",
        {"properties": {"when": {"type": "string"}, "kind": {"description": "which"}}},
    )
    assert broken.keys() == {"name", "long", "not-english", "the-user", "undescribed"}
    assert broken["undescribed"] == "when"


def test_a_rebuilt_tool_must_leave_the_pre_standard_list() -> None:
    """A listed tool that now keeps a rule is a failure until its entry drops that rule."""
    name = next(iter(PRE_STANDARD))
    found = failures({name: {}})
    assert any(line.startswith(f"{name}: keeps ") for line in found)
