"""ADR 0241: a tool on the menu can supersede deferred tools, and ``tool_search`` says so.

Fake deferred Gmail tools and the real ``mail`` tool, searched through the registry the daemon
uses; nothing is called. The same mechanism is checked on made-up names, since the next merged
tools (ADR 0241's successors) declare theirs the same way.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from typing import Any, cast

from jarvis.execution.mail_tool import build_mail_tool
from jarvis.execution.tool_search import build_tool_search
from jarvis.execution.tools import Tool, ToolContext, ToolRegistry
from jarvis.shared import CallerPrincipal

GMAIL = {  # the Workspace server's descriptions, as the exam's recorded definitions list them
    "search": "Search for emails in Gmail using query parameters.",
    "get": "Get the full content of a specific email message.",
    "listLabels": "List all labels in the Gmail account.",
    "send": "Send an email message.",
    "modify": "Add or remove labels on one message.",
}
READS = {"search", "get", "listLabels"}
SEARCH = "gmail_search is replaced by mail, which is already in your tools: call mail(query=...)."
GET = "gmail_get is replaced by mail, which is already in your tools: call mail(read=<id>)."


def _fake(name: str, *, read_only: bool, text: str = "") -> Tool:
    return Tool(
        name=name, description=text or f"{name} mail", input_schema={"type": "object"},
        handler=lambda _a, _c: {}, allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L0" if read_only else "L3", read_only=read_only, deferred=True,
    )


def _gmail() -> list[Tool]:
    return [
        _fake(f"mcp__gmail__gmail_{one}", read_only=one in READS, text=text)
        for one, text in GMAIL.items()
    ]


def _mail() -> Tool:
    (mail,) = build_mail_tool(dict, lambda *_: {}, str, UTC)
    return mail


def _search(registry: ToolRegistry, tools: list[Tool], query: str) -> dict[str, Any]:
    (search,) = build_tool_search(tools, {}, registry.superseded)
    return dict(search.handler({"query": query}, cast("ToolContext", None)))


def test_the_registry_reports_what_the_menu_tools_supersede() -> None:
    """Only a tool on the menu replaces others; a deferred one replaces nothing."""
    registry = ToolRegistry()
    registry.register(_mail())
    assert registry.superseded() == {
        "mcp__gmail__gmail_search": ("mail", "mail(query=...)"),
        "mcp__gmail__gmail_get": ("mail", "mail(read=<id>)"),
    }
    later = ToolRegistry()
    later.register(replace(_mail(), deferred=True))
    assert later.superseded() == {}


def test_a_search_for_a_superseded_tool_loads_nothing_and_names_the_replacement() -> None:
    """The search and the read are found, neither is loaded, and the note says what to call."""
    registry, tools = ToolRegistry(), _gmail()
    registry.register(_mail())
    assert _search(registry, tools, "search") == {"loaded_tools": [], "note": SEARCH}
    assert _search(registry, tools, "content") == {"loaded_tools": [], "note": GET}
    both = _search(registry, tools, "search content")
    assert both["loaded_tools"] == []
    assert SEARCH in both["note"]
    assert GET in both["note"]


def test_the_write_tools_of_the_same_server_still_load() -> None:
    """The write tools load as before; the server's other reads come along, the replaced do not."""
    registry, tools = ToolRegistry(), _gmail()
    registry.register(_mail())
    assert _search(registry, tools, "send") == {
        "loaded_tools": ["mcp__gmail__gmail_send", "mcp__gmail__gmail_listLabels"],
    }
    modify = _search(registry, tools, "modify")
    assert modify["loaded_tools"][0] == "mcp__gmail__gmail_modify"
    assert "mcp__gmail__gmail_search" not in modify["loaded_tools"]
    assert "note" not in modify


def test_without_the_menu_tool_nothing_is_replaced() -> None:
    """The deferred reads load as they always did when `mail` is not registered."""
    registry, tools = ToolRegistry(), _gmail()
    out = _search(registry, tools, "search")
    assert out["loaded_tools"][0] == "mcp__gmail__gmail_search"
    assert "mcp__gmail__gmail_get" in out["loaded_tools"]  # the server's other read-only tools
    assert "note" not in out


def test_any_menu_tool_can_supersede_deferred_names() -> None:
    """The mechanism is the field, not mail: another merged tool declares its own."""
    registry = ToolRegistry()
    registry.register(replace(
        _fake("conversation", read_only=True),
        deferred=False, supersedes={"search_records": "conversation(query=...)"},
    ))
    tools = [_fake("search_records", read_only=True, text="search records of conversations")]
    out = _search(registry, tools, "search records")
    assert out == {
        "loaded_tools": [],
        "note": "search_records is replaced by conversation, which is already in your tools: "
                "call conversation(query=...).",
    }
