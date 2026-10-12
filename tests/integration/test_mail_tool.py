"""ADR 0242: the model reads, searches and opens his mail with one tool.

A scripted Gmail layer answers like the Workspace server (JSON in a text block, a failure as
``{"error": ...}``) behind the real ``Home``, so the tool runs the path the daemon wires. Each
check asserts what the tool returns to the model or what it asked Gmail for; no network, no key.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
import yaml

from jarvis.decision.turn_guards import REPEATED_CALL, TurnCall, call_key, repeated_call
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.mail_tool import build_mail_tool
from jarvis.execution.tools import ActionLifecycle, Tool, ToolContext, ToolError, ToolRegistry
from jarvis.runtime import _fetched_mail
from jarvis.runtime.home import Home, mail_text
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state.event_log import emit_event, open_event_log
from scripts import tool_exam
from tests.integration.test_home_routes import (
    ZONE,
    _Connections,
    _Gmail,
    _Jev,
    _Microsoft,
    jev,  # noqa: F401 - the fixture.
)

if TYPE_CHECKING:
    from collections.abc import Mapping

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 26, 19, 0, tzinfo=UTC)  # Saturday 12:00 in Victoria
PAGE = 8
LEE = '"Prof. Lee" <lee@uvic.ca>'


def _message(n: int, **fields: Any) -> dict[str, Any]:  # noqa: ANN401 - Gmail's JSON fields.
    return {
        "id": f"m{n:02d}", "threadId": f"t{n:02d}", "labelIds": ["INBOX"],
        "subject": f"Co-op posting {n}", "from": f"Recruiter {n} <r{n}@corp.example>",
        "to": "allen@example.com", "date": "Fri, 25 Sep 2026 14:40:00 -0700",
        "snippet": f"Posting {n} closes soon", "body": f"Details of posting {n}.",
        "attachments": [], **fields,
    }


class _Mailbox:
    """The Workspace server's ``gmail_search`` and ``gmail_get`` over a scripted mailbox.

    ``results`` maps a query to the ids it finds, newest first; a page is ``maxResults`` of them
    and the page token is the offset of the next. ``barrier`` makes every metadata read wait for
    the others, which only a parallel fetch gets past.
    """

    def __init__(self, messages: list[dict[str, Any]], results: dict[str, list[str]]) -> None:
        self.messages = {one["id"]: one for one in messages}
        self.results = results
        self.error: str | None = None
        self.barrier: threading.Barrier | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "gmail"
        with self._lock:
            self.calls.append((tool, dict(args)))
        if self.error:
            return {"text": json.dumps({"error": self.error})}
        if tool == "gmail_search":
            ids = self.results.get(args["query"], [])
            start = int(args.get("pageToken") or 0)
            page = ids[start : start + args["maxResults"]]
            body: dict[str, Any] = {
                "messages": [{"id": i, "threadId": self.messages[i]["threadId"]} for i in page],
                "resultSizeEstimate": len(ids),
            }
            if start + len(page) < len(ids):
                body["nextPageToken"] = str(start + len(page))
            return {"text": json.dumps(body)}
        assert tool == "gmail_get", f"mail may only read: {tool}"
        one = self.messages.get(args["messageId"])
        if one is None:
            return {"text": json.dumps({"error": "Requested entity was not found."})}
        if args["format"] == "metadata":
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            return {"text": json.dumps({**one, "body": "", "attachments": []})}
        return {"text": json.dumps(one)}

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _mail(mailbox: _Mailbox | None) -> Tool:
    """The ``mail`` tool over a real ``Home`` whose Gmail is ``mailbox`` (None: not connected)."""
    home = Home(
        _Connections(_Microsoft(), mailbox),  # type: ignore[arg-type]
        (ZONE, ZoneInfo(ZONE)), None,
    )
    (tool,) = build_mail_tool(home.mail, home.gmail, mail_text, ZoneInfo(ZONE), lambda: NOW)
    return tool


def _call(tool: Tool, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the model's arguments.
    return dict(tool.handler(args, cast("ToolContext", None)))


def _failure(tool: Tool, **args: Any) -> ToolError:  # noqa: ANN401 - the model's arguments.
    with pytest.raises(ToolError) as caught:
        _call(tool, **args)
    return caught.value


def _co_op() -> _Mailbox:
    """Eleven co-op letters on one query: a full page of eight and a second of three."""
    messages = [_message(n) for n in range(1, 12)]
    return _Mailbox(messages, {"co-op": [one["id"] for one in messages]})


def test_the_tool_is_a_small_read_only_native_menu_entry() -> None:
    """One L0 read for the model, on the menu from the first request, with a short brief."""
    tool = _mail(None)
    assert tool.name == "mail"
    assert tool.risk_level == "L0"
    assert tool.read_only
    assert not tool.deferred
    assert tool.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert len(tool.description) <= 600
    assert tool.description.isascii()
    assert set(tool.input_schema["properties"]) == {"query", "page", "read"}
    assert tool.input_schema["additionalProperties"] is False


def test_no_arguments_lists_the_marked_letters_without_junk_bodies_or_addresses(
    jev: _Jev,  # noqa: F811
) -> None:
    """The overview is mail_inbox's: marks and importance ride along, the junk is counted."""
    gmail = _Gmail()
    home = Home(
        _Connections(_Microsoft(), gmail),  # type: ignore[arg-type]
        (ZONE, ZoneInfo(ZONE)), None, jev.reply(importance=True),
    )
    (tool,) = build_mail_tool(home.mail, home.gmail, mail_text, ZoneInfo(ZONE), lambda: NOW)
    result = _call(tool)
    assert result["junk_left_out"] == 1  # the Shop Deals letter, 199a1c0d4105
    letters = {one["id"]: one for one in result["unread"]}
    assert "199a1c0d4105" not in letters
    assert len(letters) == 5
    assert result["summary"] == "5 unread in Primary; 2 need a reply (Prof. Lee, Sam)."
    assert next(iter(result)) == "summary"
    assert letters["199a1c0d4101"] == {
        "id": "199a1c0d4101", "from": "Prof. Lee", "subject": "Office hours move to Thursday",
        "when": "yesterday (Fri) 2:40 pm", "reply": "yes",
        "importance": pytest.approx(2.2), "category": "school",
    }
    assert letters["199a1c0d4104"].get("reply") is None  # unrated reply is absent, not null
    # Only a sender with no display name shows its address, as on the Mail page; no address field.
    assert json.dumps(result).count("@") == 1
    assert all("address" not in one and "thread_id" not in one for one in letters.values())
    # One Home.mail read: one search and a metadata read per hit, no bodies.
    assert [name for name, _ in gmail.calls].count("gmail_search") == 1
    assert all(args.get("format") != "full" for _, args in gmail.calls)


def test_an_empty_inbox_says_so() -> None:
    """Nothing unread is a sentence, not an empty list alone."""
    box = _Mailbox([], {"in:inbox category:primary is:unread": []})
    result = _call(_mail(box))
    assert result == {"summary": "No unread mail in Primary.", "unread": [], "junk_left_out": 0}


def test_a_search_returns_eight_summarized_messages_and_a_more_flag() -> None:
    """One search, one parallel metadata read per hit; rows are short and carry no body."""
    box = _co_op()
    box.barrier = threading.Barrier(PAGE)  # all eight reads must be in flight together
    result = _call(_mail(box), query="co-op")
    assert list(result) == ["summary", "messages", "more"]
    assert result["more"] is True
    assert result["summary"] == 'Messages 1-8 of about 11 for "co-op", newest first.'
    assert [one["id"] for one in result["messages"]] == [f"m{n:02d}" for n in range(1, 9)]
    assert result["messages"][0] == {
        "id": "m01", "from": "Recruiter 1", "subject": "Co-op posting 1",
        "when": "yesterday (Fri) 2:40 pm", "snippet": "Posting 1 closes soon",
    }
    assert box.names() == ["gmail_search"] + ["gmail_get"] * PAGE
    assert box.calls[0][1] == {"query": "co-op", "maxResults": PAGE}
    assert all(args["format"] == "metadata" for name, args in box.calls if name == "gmail_get")
    assert "Details of posting" not in json.dumps(result)
    assert len(json.dumps(result)) < 4000


def _twenty() -> _Mailbox:
    """Twenty co-op letters: pages of eight, eight and four."""
    messages = [_message(n) for n in range(1, 21)]
    return _Mailbox(messages, {"co-op": [one["id"] for one in messages]})


def test_page_two_continues_the_query_with_tokens_the_server_holds() -> None:
    """page=2 is the rest of the same query; no cursor is shown, and the end is a sentence."""
    box = _co_op()
    tool = _mail(box)
    _call(tool, query="co-op")
    second = _call(tool, query="co-op", page=2)
    assert [one["id"] for one in second["messages"]] == ["m09", "m10", "m11"]
    assert second["more"] is False
    assert second["summary"] == 'Messages 9-11 for "co-op", newest first.'
    assert box.calls[-4][1] == {"query": "co-op", "maxResults": PAGE, "pageToken": "8"}
    assert "nextPageToken" not in json.dumps(second)
    calls = len(box.calls)
    done = _call(tool, query="co-op", page=3)
    assert done == {
        "summary": 'There is no page 3 of "co-op"; its results end at page 2.',
        "messages": [], "more": False,
    }
    assert len(box.calls) == calls  # the end was already known: nothing was asked of Gmail


def test_a_page_not_reached_yet_is_walked_to_with_id_only_searches() -> None:
    """page=3 as the first call of a query: searches skip pages 1 and 2, only page 3 is read."""
    box = _twenty()
    tool = _mail(box)
    third = _call(tool, query="co-op", page=3)
    assert [one["id"] for one in third["messages"]] == ["m17", "m18", "m19", "m20"]
    assert third["summary"] == 'Messages 17-20 for "co-op", newest first.'
    assert third["more"] is False
    assert [args.get("pageToken") for name, args in box.calls if name == "gmail_search"] == [
        None, "8", "16",
    ]
    assert box.names().count("gmail_get") == 4  # headers of page 3 alone
    # The tokens it passed are held: page 2 now costs its own search and reads, no walk.
    box.calls.clear()
    second = _call(tool, query="co-op", page=2)
    assert [one["id"] for one in second["messages"]] == [f"m{n:02d}" for n in range(9, 17)]
    assert second["more"] is True
    assert box.names() == ["gmail_search"] + ["gmail_get"] * PAGE
    assert box.calls[0][1]["pageToken"] == "8"


def test_another_query_drops_the_held_pages() -> None:
    """The tokens belong to the last query; a page of an earlier one is walked to again."""
    box = _twenty()
    box.results["career fair"] = ["m03"]
    tool = _mail(box)
    _call(tool, query="co-op", page=2)
    _call(tool, query="career fair")
    box.calls.clear()
    again = _call(tool, query="co-op", page=2)
    assert [one["id"] for one in again["messages"]][:1] == ["m09"]
    assert [args.get("pageToken") for name, args in box.calls if name == "gmail_search"] == [
        None, "8",
    ]


def test_a_page_past_the_end_is_a_sentence_not_an_error() -> None:
    """The walk finds the last page: nothing is read, and the answer says where it ends."""
    box = _co_op()
    result = _call(_mail(box), query="co-op", page=9)
    assert result == {
        "summary": 'There is no page 9 of "co-op"; its results end at page 2.',
        "messages": [], "more": False,
    }
    assert box.names() == ["gmail_search", "gmail_search"]


def test_every_page_call_differs_from_the_last_for_the_repeat_guard() -> None:
    """ADR 0240: page=3 is not a repeat of page=2, while asking for page=2 again is."""
    turn = [
        TurnCall(name="mail", read_only=True, ok=True, key=call_key({"query": "co-op"})),
        TurnCall(name="mail", read_only=True, ok=True, key=call_key({"query": "co-op", "page": 2})),
    ]
    assert repeated_call(turn, "mail", call_key({"query": "co-op", "page": 3})) is None
    assert repeated_call(turn, "mail", call_key({"query": "co-op", "page": 2})) == REPEATED_CALL


def test_a_search_with_no_hits_says_so() -> None:
    """No match is a sentence and an empty list, with nothing fetched."""
    box = _co_op()
    result = _call(_mail(box), query="unicorns")
    assert result == {"summary": 'No messages match "unicorns".', "messages": [], "more": False}
    assert box.names() == ["gmail_search"]


@pytest.mark.parametrize(
    ("date", "when"),
    [
        ("Sat, 26 Sep 2026 09:05:00 -0700", "today (Sat) 9:05 am"),
        ("Sat, 26 Sep 2026 12:00:00 -0700", "today (Sat) 12:00 pm"),
        ("Fri, 25 Sep 2026 23:59:00 -0700", "yesterday (Fri) 11:59 pm"),
        # 06:30 UTC on the 27th is 23:30 on the 26th in Victoria: his day, not Gmail's.
        ("Sun, 27 Sep 2026 06:30:00 +0000", "today (Sat) 11:30 pm"),
        ("Mon, 21 Sep 2026 14:40:00 -0700", "Mon Sep 21, 2:40 pm"),
        ("Tue, 30 Dec 2025 00:10:00 -0800", "Tue Dec 30 2025, 12:10 am"),
        ("", None),
        ("not a date", None),
    ],
)
def test_times_are_his_local_time_with_weekday_and_today_or_yesterday(
    date: str, when: str | None,
) -> None:
    """Gmail's Date header becomes the words he would say; no usable header, no time."""
    box = _Mailbox([_message(1, date=date)], {"x": ["m01"]})
    (row,) = _call(_mail(box), query="x")["messages"]
    assert row.get("when") == when


def test_a_snippet_loses_gmail_s_html_escapes_and_is_capped() -> None:
    """Gmail escapes its snippets; the model reads words, at most 200 characters."""
    box = _Mailbox(
        [_message(1, snippet="Don&#39;t miss &amp; apply  today " + "x" * 400, subject="s" * 300)],
        {"x": ["m01"]},
    )
    (row,) = _call(_mail(box), query="x")["messages"]
    assert row["snippet"].startswith("Don't miss & apply today x")
    assert len(row["snippet"]) == 200
    assert row["snippet"].endswith("…")
    assert len(row["subject"]) == 120


def test_read_returns_clean_text_with_the_headers_a_reply_needs() -> None:
    """HTML becomes text, the quoted reply and the signature go, the attachments are named."""
    html_body = (
        "<div>Thursday 3pm works.<br>Bring the form.</div><br>"
        '<div class="gmail_quote">On Fri, Sep 25, 2026 at 2:40 PM Allen &lt;allen@example.com&gt;'
        " wrote:<br><blockquote>Can we meet?</blockquote></div>"
    )
    letter = _message(
        7, subject="Re: Office hours", **{"from": LEE}, body=html_body,
        attachments=[{"filename": "form.pdf", "mimeType": "application/pdf", "size": 1234}],
    )
    box = _Mailbox([letter], {})
    result = _call(_mail(box), read="m07")
    assert result == {
        "summary": "From Prof. Lee, yesterday (Fri) 2:40 pm: Re: Office hours. 1 attachment.",
        "id": "m07", "thread_id": "t07", "from": LEE, "subject": "Re: Office hours",
        "to": "allen@example.com", "when": "yesterday (Fri) 2:40 pm",
        "at": "2026-09-25T14:40-07:00", "attachments": ["form.pdf"],
        "body": "Thursday 3pm works.\nBring the form.\n(quoted replies and signature removed)",
    }
    assert box.calls == [("gmail_get", {"messageId": "m07", "format": "full"})]


@pytest.mark.parametrize(
    ("body", "clean"),
    [
        ("Sounds good.\n\nOn Fri, Sep 25, 2026 at 2:40 PM Lee <lee@uvic.ca> wrote:\n> Hi\n> there",
         "Sounds good.\n(quoted replies and signature removed)"),
        ("Sounds good.\n\nOn Sep 25, 2026, at 2:40 PM, Prof Lee <lee@uvic.ca>\nwrote:\nHi",
         "Sounds good.\n(quoted replies and signature removed)"),
        ("Yes.\n> old text\nThanks", "Yes.\nThanks\n(quoted replies and signature removed)"),
        ("Thanks,\nAllen\n-- \nAllen Shi\nUVic",
         "Thanks,\nAllen\n(quoted replies and signature removed)"),
        ("On my way.\n\nSent from my iPhone",
         "On my way.\n(quoted replies and signature removed)"),
        # A forwarded letter is the content: nothing is cut.
        ("FYI\n---------- Forwarded message ---------\nFrom: HR\nYour offer", None),
        # A body that is nothing but a quote stays as it is.
        ("> only a quote", None),
        ("On Monday we agreed; nothing was wrote down.", None),
    ],
)
def test_quoted_replies_and_signatures_are_trimmed_where_it_is_safe(
    body: str, clean: str | None,
) -> None:
    """Quoted lines, the older message under 'On ... wrote:' and signatures; never a forward."""
    box = _Mailbox([_message(1, body=body)], {})
    result = _call(_mail(box), read="m01")
    assert result["body"] == (body if clean is None else clean)


def test_a_long_body_is_cut_at_four_thousand_characters_and_says_so() -> None:
    """Never more than 4000 characters of body, and the cut is named."""
    box = _Mailbox([_message(1, body="word " * 2000)], {})
    result = _call(_mail(box), read="m01")
    assert result["body"].endswith("\n(truncated)")
    assert len(result["body"]) <= 4000 + len("\n(truncated)")
    assert len(json.dumps(result)) < 4600


def test_a_message_with_no_text_says_so() -> None:
    """An empty body is a word the model can relay, not a missing key."""
    box = _Mailbox([_message(1, body="", subject="")], {})
    result = _call(_mail(box), read="m01")
    assert result["body"] == "(no text)"
    assert result["summary"].endswith("(no subject).")


def test_an_unknown_id_names_the_next_step() -> None:
    """Gmail's not-found becomes the sentence that sends the model to a search."""
    error = _failure(_mail(_co_op()), read="nope")
    assert str(error) == "No mail with id nope; search with mail(query=...) first"
    assert error.code == "not_found"


@pytest.mark.parametrize(
    ("arguments", "words"),
    [
        ({"read": "m01", "query": "x"}, "not both"),
        ({"read": "m01", "page": 2}, "not both"),
        ({"page": 2}, "pass the same query"),
        ({"query": "x", "page": 0}, "whole number"),
        ({"query": "x", "page": "2"}, "whole number"),
        ({"query": "x", "page": True}, "whole number"),
        ({"query": "x", "page": 2.5}, "whole number"),
    ],
)
def test_arguments_that_do_not_fit_together_are_refused_with_the_fix(
    arguments: dict[str, Any], words: str,
) -> None:
    """The error says what was wrong and which argument to pass instead."""
    box = _co_op()
    error = _failure(_mail(box), **arguments)
    assert words in str(error)
    assert error.code == "invalid_arguments"
    assert box.calls == []


@pytest.mark.parametrize("arguments", [{}, {"query": "co-op"}, {"read": "m01"}])
def test_without_gmail_every_form_is_a_clear_error(arguments: dict[str, Any]) -> None:
    """Gmail not connected: an error that names it, which the model can relay."""
    error = _failure(_mail(None), **arguments)
    assert "Gmail is not connected" in str(error)
    assert error.code == "unavailable"


@pytest.mark.parametrize(
    ("said", "words", "code"),
    [
        ("invalid_grant: token expired, sign in again", "Gmail needs reconnecting", "unavailable"),
        ("Request had insufficient authentication scopes", "Gmail needs reconnecting",
         "unavailable"),
        ("Backend Error", "Gmail did not answer (", "network_error"),
    ],
)
def test_a_gmail_failure_is_worded_for_the_model_with_what_the_server_said(
    said: str, words: str, code: str,
) -> None:
    """A sign-in problem says Gmail needs reconnecting, any other failure that it did not answer."""
    box = _co_op()
    box.error = said
    for arguments in ({}, {"query": "co-op"}, {"read": "m01"}):
        error = _failure(_mail(box), **arguments)
        assert words in str(error)
        assert said in str(error)
        assert error.code == code
        assert "retry" not in str(error).lower()


def test_only_gmail_s_read_tools_are_ever_called() -> None:
    """Every form of the tool leaves only gmail_search and gmail_get in Gmail's log."""
    box = _co_op()
    tool = _mail(box)
    _call(tool)
    _call(tool, query="co-op")
    _call(tool, query="co-op", page=2)
    _call(tool, read="m01")
    assert set(box.names()) <= {"gmail_search", "gmail_get"}


def test_a_message_read_by_the_tool_still_shows_as_the_mail_card(tmp_path: Path) -> None:
    """ADR 0063: the one message a turn read whole is a mail record, as with gmail_get."""
    paths = bootstrap_runtime(tmp_path)
    conn = open_event_log(paths.event_log)
    registry = ToolRegistry()
    registry.register(_mail(_Mailbox([_message(7, **{"from": LEE}, subject="Re: Hours")], {})))
    lifecycle = ActionLifecycle()
    lifecycle.register("A1")
    lifecycle.transition("A1", "authorized")
    arguments = {"read": "m07"}
    emit_event(
        conn, type="action.proposed", correlation={"turn_id": "T1"},
        payload={
            "turn_id": "T1", "action_id": "A1", "tool_name": "mail", "arguments": arguments,
            "caller_principal": "jarvis_llm", "risk_level": "L0",
        },
    )
    registry.dispatch(
        ActionRequest(
            action_id="A1", tool_name="mail", target_entity_ref=None,
            caller_principal=CallerPrincipal.JARVIS_LLM, risk_level="L0", arguments=arguments,
            authorization_lease=None, run_id=None, turn_id="T1",
        ),
        conn, paths, lifecycle,
    )
    record = _fetched_mail(conn, "T1")
    assert record is not None
    assert record[1] == (
        f"From: {LEE}\nTo: allen@example.com\nDate: 2026-09-25T14:40-07:00\nSubject: Re: Hours\n"
        "Thread-Id: t07\nGmail-Id: m07\n\nDetails of posting 7."
    )
    conn.close()


def test_the_menu_offers_mail_and_not_the_three_tools_it_replaces(tmp_path: Path) -> None:
    """The shipped config: `mail` is on the menu, the old overview and both Gmail reads are not."""
    recorded = tmp_path / "mcp-tools.json"
    recorded.write_text(json.dumps({
        name: {"name": name, "description": "d", "input_schema": {"type": "object"}}
        for name in ("mcp__gmail__gmail_search", "mcp__gmail__gmail_get", "mcp__gmail__gmail_send")
    }), encoding="utf-8")
    config = yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    names = {one["name"] for one in tool_exam.offered_tools(config, recorded)}
    assert "mail" in names
    assert not names & {"mail_inbox", "mcp__gmail__gmail_search", "mcp__gmail__gmail_get"}
    assert "tool_search" in names  # the deferred Gmail tools are still found through it


def test_tool_search_in_the_shipped_registry_sends_gmail_reads_to_mail(tmp_path: Path) -> None:
    """ADR 0242: tool_search finds the Gmail reads, loads only the write tool, and names `mail`."""
    recorded = tmp_path / "mcp-tools.json"
    recorded.write_text(json.dumps({
        name: {"name": name, "description": "d", "input_schema": {"type": "object"}}
        for name in ("mcp__gmail__gmail_search", "mcp__gmail__gmail_get", "mcp__gmail__gmail_send")
    }), encoding="utf-8")
    config = yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    registry = tool_exam._exam_registry(config, recorded)  # noqa: SLF001
    search = next(one for one in registry.get_definitions() if one.name == "tool_search")
    handler = cast("Tool", search).handler
    out = dict(handler({"query": "gmail_search"}, cast("ToolContext", None)))
    # The write tool loads; both reads are found but not loaded, and the note names `mail`.
    assert "mcp__gmail__gmail_send" in out["loaded_tools"]
    assert not {"mcp__gmail__gmail_search", "mcp__gmail__gmail_get"} & set(out["loaded_tools"])
    tail = "is replaced by mail, which is already in your tools: call"
    assert f"gmail_search {tail} mail(query=...)." in out["note"]
    assert f"gmail_get {tail} mail(read=<id>)." in out["note"]
