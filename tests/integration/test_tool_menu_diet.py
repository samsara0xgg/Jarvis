"""ADR 0246: tools kept off the voice menu stay found through tool_search and callable by name."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml

from jarvis.execution.tools import Tool, ToolContext
from jarvis.shared import CallerPrincipal
from scripts import tool_exam
from tests.integration.test_flat_tool_dispatch import _chain, _Fixture, _request
from tests.integration.test_plugin_connections import _service
from tests.integration.test_plugin_connections import fixture as fixture  # noqa: PLC0414

if TYPE_CHECKING:
    from jarvis.execution.tools import ToolRegistry

REPO = Path(__file__).resolve().parents[2]
OFF_MENU = ("save_briefing", "save_knowledge", "search_knowledge", "record_application")
PLUGIN_TOOLS = ("list_plugins", "open_plugin", "disable_plugin", "remove_plugin")
# What he might need, in the model's English keywords, and the tool tool_search must bring.
SEARCHES = (
    ("save a daily briefing", "save_briefing"),
    ("save a knowledge item", "save_knowledge"),
    ("search saved knowledge", "search_knowledge"),
    ("record a job application", "record_application"),
)


@pytest.fixture(scope="module")
def shipped(tmp_path_factory: pytest.TempPathFactory) -> tuple[ToolRegistry, set[str]]:
    """The offline registry the exam builds from the shipped config, and the names on its menu."""
    recorded = tmp_path_factory.mktemp("mcp") / "mcp-tools.json"
    recorded.write_text(json.dumps({
        name: {"name": name, "description": "d", "input_schema": {"type": "object"}}
        for name in ("mcp__gmail__gmail_search", "mcp__gmail__gmail_send")
    }), encoding="utf-8")
    config = yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    menu = {one["name"] for one in tool_exam.offered_tools(config, recorded)}
    return tool_exam._exam_registry(config, recorded), menu  # noqa: SLF001


def test_the_four_tools_are_registered_deferred_and_not_on_the_menu(
    shipped: tuple[ToolRegistry, set[str]],
) -> None:
    """Registered for the model to call, but its menu carries none of them."""
    registry, menu = shipped
    held = {t.name: t for t in registry.for_caller(CallerPrincipal.JARVIS_LLM)}
    for name in OFF_MENU:
        assert name in held, name
        assert held[name].deferred, name
        assert name not in menu, name
    assert "tool_search" in menu
    assert not menu & {"withdraw_card", "close_question"}
    assert "dismiss_card" in menu
    # Connecting an app by voice is a direct flow the prompt names: no search round first.
    for name in PLUGIN_TOOLS:
        assert name in menu, name
        assert not held[name].deferred, name


@pytest.mark.parametrize(("query", "wanted"), SEARCHES)
def test_tool_search_finds_each_and_the_description_names_them_all(
    shipped: tuple[ToolRegistry, set[str]], query: str, wanted: str,
) -> None:
    """The query in English brings the tool; the description lists every one by name."""
    registry, _ = shipped
    search = cast("Tool", next(t for t in registry.get_definitions() if t.name == "tool_search"))
    found = search.handler({"query": query}, cast("ToolContext", None))["loaded_tools"]
    assert wanted in found
    for name in OFF_MENU:
        assert name in search.description
    assert "Jarvis's own tools, not listed upfront:" in search.description


def test_a_tool_off_the_menu_is_still_dispatched_by_name(
    shipped: tuple[ToolRegistry, set[str]], tmp_path: Path,
) -> None:
    """Background code and a card's button reach a deferred tool through the registry as before."""
    registry, _ = shipped
    tools = tuple(t for t in registry.get_definitions() if t.name in OFF_MENU)
    fx = _Fixture(tmp_path, tools=tools)
    try:
        calls = {"record_application": {"company": "Acme"}}
        for number, (name, args) in enumerate(calls.items()):
            fx.dispatch(_request(name, f"A{number}", arguments=args))
            assert "error" not in json.loads(_chain(fx, f"A{number}")["tool_output"]), name
    finally:
        fx.close()


def test_refresh_search_indexes_a_built_in_registered_after_the_last_publish(
    tmp_path: Path, fixture: _Fixture,
) -> None:
    """The runtime registers the job and mail tools after the plugins; one refresh finds them."""
    service = _service(tmp_path, fixture)
    try:
        late = replace(
            Tool(
                name="late_note", description="Keep a late note.", input_schema={"type": "object"},
                handler=lambda _a, _c: {}, allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
                risk_level="L0", read_only=True,
            ),
            deferred=True,
        )
        fixture.registry.register(late)
        service.refresh_search()
        search = cast(
            "Tool", next(t for t in fixture.registry.get_definitions() if t.name == "tool_search"),
        )
        assert "late_note" in search.description
        found: Any = search.handler({"query": "late note"}, cast("ToolContext", None))
        assert found["loaded_tools"] == ["late_note"]
        service.refresh_search()  # a second refresh changes nothing and does not collide
    finally:
        service.stop()
