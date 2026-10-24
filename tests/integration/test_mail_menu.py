"""ADR 0362: in the shipped configuration the model's mail is the one ``mail`` tool.

The registry is the one the daemon registers (``scripts.tool_exam`` rebuilds it offline with the
recorded definitions of a Gmail server); nothing is called.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml

from jarvis.execution.mail_gmail import Mailbox
from jarvis.execution.mail_tool import build_mail_tool
from scripts import tool_exam, tool_exam_steps
from tests.support.gmail_fake import PRIMARY, Gmail
from tests.support.tool_world import ToolWorld, tool_request

if TYPE_CHECKING:
    from jarvis.execution.tools import ToolContext

REPO = Path(__file__).resolve().parents[2]
GMAIL = (
    "search", "get", "downloadAttachment", "listLabels", "createLabel", "createDraft", "sendDraft",
    "send", "modify", "batchModify", "modifyThread",
)
REPLACED = tuple(one for one in GMAIL if one != "createLabel")


def _recorded(tmp_path: Path) -> Path:
    path = tmp_path / "mcp-tools.json"
    path.write_text(json.dumps({
        f"mcp__gmail__gmail_{one}": {
            "name": f"mcp__gmail__gmail_{one}", "description": "d",
            "input_schema": {"type": "object"},
        }
        for one in GMAIL
    }), encoding="utf-8")
    return path


def _config() -> dict[str, object]:
    return cast(
        "dict[str, object]",
        yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8")),
    )


def test_the_menu_offers_mail_and_not_the_gmail_tools_it_replaces(tmp_path: Path) -> None:
    """`mail` is on the menu; the old overview and the Gmail reads and changes it does are not."""
    names = {one["name"] for one in tool_exam.offered_tools(_config(), _recorded(tmp_path))}
    assert "mail" in names
    assert not names & {"mail_inbox", *(f"mcp__gmail__gmail_{one}" for one in REPLACED)}
    assert "tool_search" in names  # the deferred Gmail tools are still found through it


def test_the_mail_entry_is_short_and_one_flat_object(tmp_path: Path) -> None:
    """What the model reads of it on every request: a description of at most 600 characters."""
    menu = tool_exam.offered_tools(_config(), _recorded(tmp_path))
    (entry,) = (one for one in menu if one["name"] == "mail")
    assert len(entry["description"]) <= 600
    assert entry["input_schema"]["properties"]["action"]["enum"] == [
        "list", "search", "read", "thread", "attachment", "draft", "send", "archive",
    ]


def test_tool_search_in_the_shipped_registry_sends_gmail_work_to_mail(tmp_path: Path) -> None:
    """ADR 0242: tool_search finds the Gmail tools, loads none but createLabel, names `mail`."""
    registry = tool_exam._exam_registry(_config(), _recorded(tmp_path))  # noqa: SLF001
    search = next(one for one in registry.get_definitions() if one.name == "tool_search")
    out = dict(search.handler({"query": "gmail_search"}, cast("ToolContext", None)))
    loaded = set(out["loaded_tools"])
    assert not loaded & {f"mcp__gmail__gmail_{one}" for one in REPLACED}
    labels = dict(search.handler({"query": "createLabel"}, cast("ToolContext", None)))
    assert labels["loaded_tools"] == ["mcp__gmail__gmail_createLabel"]  # `mail` makes no labels
    tail = "is replaced by mail, which is already in your tools: call"
    assert f'gmail_search {tail} mail(action="search", query=...).' in out["note"]
    assert f'gmail_get {tail} mail(action="read", message=<id>).' in out["note"]
    description = search.description
    assert 'Already in your tools for it: mail(action="search", query=...)' in description
    assert description.count('mail(action="archive", message=<id>)') == 1


def test_the_live_exam_runs_mail_reads_but_never_its_archive(tmp_path: Path) -> None:
    """The copied server holds his real mailbox: the sandbox answers an archive with a stub."""
    gmail = Gmail()
    gmail.add("a1", "Lunch", labels=PRIMARY)
    (mail,) = build_mail_tool(Mailbox(gmail.tool))
    world = ToolWorld(tmp_path, tools=(tool_exam_steps._guard_actions(mail),))  # noqa: SLF001
    try:
        asked = {"action": "search", "query": "lunch"}
        assert world.dispatch(tool_request("mail", "A1", arguments=asked)).error is None
        assert gmail.names()
        before = list(gmail.calls)
        archived = world.dispatch(
            tool_request("mail", "A2", arguments={"action": "archive", "message": "a1"}),
        )
        assert archived.payload == {"stubbed": True, "note": "sandbox: not run"}
        assert gmail.calls == before  # not one call reached the server
        assert "INBOX" in gmail.messages["a1"].labels
    finally:
        world.close()
