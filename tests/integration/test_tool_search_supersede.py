"""ADR 0242: a tool on the menu can supersede deferred tools, and ``tool_search`` says so.

Fake deferred Gmail tools and the real ``mail`` tool, searched through the registry the daemon
uses; nothing is called. The same mechanism is checked on made-up names, since the next merged
tools declare theirs the same way.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

from jarvis.execution.mail_gmail import Mailbox
from jarvis.execution.mail_tool import build_mail_tool
from jarvis.execution.tool_search import build_tool_search
from jarvis.execution.tools import Tool, ToolContext, ToolRegistry
from jarvis.shared import CallerPrincipal

GMAIL = {  # the Workspace server's tools, described as it describes them
    "search": "Search for emails in Gmail using query parameters.",
    "get": "Get the full content of a specific email message.",
    "downloadAttachment": "Download one attachment of an email message.",
    "listLabels": "List all labels in the Gmail account.",
    "createDraft": "Create a draft email.",
    "send": "Send an email message.",
    "modify": "Add or remove labels on one message.",
    "batchModify": "Add or remove labels on many messages at once.",
    "modifyThread": "Add or remove labels on every message of a conversation.",
}
READS = {"search", "get", "downloadAttachment", "listLabels"}
SEARCH_HOW = 'mail(action="search", query=...)'
READ_HOW = 'mail(action="read", message=<id>)'
FILE_HOW = 'mail(action="attachment", message=<id>, file=...)'
ARCHIVE_HOW = 'mail(action="archive", message=<id>)'
TAIL = "is replaced by mail, which is already in your tools: call"
SEARCH = f"gmail_search {TAIL} {SEARCH_HOW}."
GET = f"gmail_get {TAIL} {READ_HOW}."
FILE = f"gmail_downloadAttachment {TAIL} {FILE_HOW}."
MODIFY = f"gmail_modify {TAIL} {ARCHIVE_HOW}."


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
    (mail,) = build_mail_tool(Mailbox(lambda *_: {}))
    return mail


def _search(registry: ToolRegistry, tools: list[Tool], query: str) -> dict[str, Any]:
    (search,) = build_tool_search(tools, {}, registry.superseded)
    return dict(search.handler({"query": query}, cast("ToolContext", None)))


def test_the_registry_reports_what_the_menu_tools_supersede() -> None:
    """Only a tool on the menu replaces others; a deferred one replaces nothing."""
    registry = ToolRegistry()
    registry.register(_mail())
    assert registry.superseded() == {
        "mcp__gmail__gmail_search": ("mail", SEARCH_HOW),
        "mcp__gmail__gmail_get": ("mail", READ_HOW),
        "mcp__gmail__gmail_downloadAttachment": ("mail", FILE_HOW),
        "mcp__gmail__gmail_modify": ("mail", ARCHIVE_HOW),
        "mcp__gmail__gmail_batchModify": ("mail", ARCHIVE_HOW),
        "mcp__gmail__gmail_modifyThread": ("mail", ARCHIVE_HOW),
    }
    later = ToolRegistry()
    later.register(replace(_mail(), deferred=True))
    assert later.superseded() == {}


def test_a_search_for_a_superseded_tool_loads_nothing_and_names_the_replacement() -> None:
    """The search, the read, the attachment and the change are found, none loaded, each named."""
    registry, tools = ToolRegistry(), _gmail()
    registry.register(_mail())
    assert _search(registry, tools, "search") == {"loaded_tools": [], "note": SEARCH}
    assert _search(registry, tools, "content") == {"loaded_tools": [], "note": GET}
    assert _search(registry, tools, "attachment") == {"loaded_tools": [], "note": FILE}
    assert _search(registry, tools, "modify") == {"loaded_tools": [], "note": MODIFY}
    both = _search(registry, tools, "search content")
    assert both["loaded_tools"] == []
    assert SEARCH in both["note"]
    assert GET in both["note"]


def test_the_batch_and_thread_changes_are_replaced_by_the_archive_as_well() -> None:
    """Searched by their own names, the batch and the thread change point at the same call."""
    registry, tools = ToolRegistry(), _gmail()
    registry.register(_mail())
    batch = _search(registry, tools, "batchModify")
    assert batch == {"loaded_tools": [], "note": f"gmail_batchModify {TAIL} {ARCHIVE_HOW}."}
    thread = _search(registry, tools, "modifyThread")
    assert thread == {"loaded_tools": [], "note": f"gmail_modifyThread {TAIL} {ARCHIVE_HOW}."}


def test_the_draft_send_and_label_tools_of_the_same_server_still_load() -> None:
    """What `mail` does not do stays the plugin's; the server's reads left over come along."""
    registry, tools = ToolRegistry(), _gmail()
    registry.register(_mail())
    assert _search(registry, tools, "send") == {
        "loaded_tools": ["mcp__gmail__gmail_send", "mcp__gmail__gmail_listLabels"],
    }
    assert _search(registry, tools, "draft") == {
        "loaded_tools": ["mcp__gmail__gmail_createDraft", "mcp__gmail__gmail_listLabels"],
    }
    assert _search(registry, tools, "list") == {"loaded_tools": ["mcp__gmail__gmail_listLabels"]}


def test_without_the_menu_tool_nothing_is_replaced() -> None:
    """The deferred reads load as they always did when `mail` is not registered."""
    registry, tools = ToolRegistry(), _gmail()
    out = _search(registry, tools, "search")
    assert out["loaded_tools"][0] == "mcp__gmail__gmail_search"
    assert "mcp__gmail__gmail_get" in out["loaded_tools"]  # the server's other read-only tools
    assert "note" not in out
    assert _search(registry, tools, "modify")["loaded_tools"] == ["mcp__gmail__gmail_modify", *(
        f"mcp__gmail__gmail_{one}" for one in ("search", "get", "downloadAttachment", "listLabels")
    )]


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


def test_the_description_names_the_menu_tool_on_its_source_line_once_per_call() -> None:
    """The gmail line says mail is already in the tools; three changes are one hint, not three."""
    registry = ToolRegistry()
    registry.register(_mail())
    (search,) = build_tool_search(_gmail(), {"gmail": "Gmail."}, registry.superseded)
    line = next(one for one in search.description.splitlines() if one.startswith("- gmail:"))
    assert line == (
        f"- gmail: Gmail. Already in your tools for it: {SEARCH_HOW}, {READ_HOW}, {FILE_HOW}, "
        f"{ARCHIVE_HOW}."
    )
    (bare,) = build_tool_search(_gmail(), {"gmail": "Gmail."}, ToolRegistry().superseded)
    assert "- gmail: Gmail.\n" in bare.description
