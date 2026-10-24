"""The tool standard's mechanical rules hold for every built-in tool (ADR 0317).

Runs ``scripts.check_tools`` over the menu this checkout offers, so a new tool that breaks a
rule, or a rebuilt one still listed as pre-standard, fails here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from jarvis.shared import CallerPrincipal
from scripts.check_tools import PRE_STANDARD, REPO, broken_rules, checked_tools, failures
from scripts.tool_exam import _exam_registry
from tests.support.runtime_config import boot_runtime

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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


# What the exam's stand-ins switch on that a keyless, deviceless, mail-less boot leaves off.
OFF_IN_A_BARE_BOOT = {
    "web_search", "weather", "map_search", "open_path", "search_notes",
    "read_file", "read_clipboard", "open_url", "screen_look", "start_night_run", "end_night_run",
    "jobs",
}


def test_a_booted_runtime_serves_the_exam_menu_in_the_exam_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One catalog: the daemon's tool names, in its order, are the exam's less what it gates off."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text("runtime:\n  role: brain\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = boot_runtime(root)
    try:
        booted = [t.name for t in runtime.tool_registry.for_caller(CallerPrincipal.JARVIS_LLM)]
    finally:
        runtime.conn.close()
    recorded = tmp_path / "mcp-tools.json"
    recorded.write_text("{}", encoding="utf-8")
    config = yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    exam = [t.name for t in _exam_registry(config, recorded).for_caller(CallerPrincipal.JARVIS_LLM)]
    assert [n for n in exam if n not in OFF_IN_A_BARE_BOOT] == booted
