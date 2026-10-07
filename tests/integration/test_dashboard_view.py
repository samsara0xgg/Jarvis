"""ADR 0176 — the Dashboard's reported view, its line, its route and her page-turning tool."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi.testclient import TestClient

from jarvis.execution.dashboard_tool import build_dashboard_tool
from jarvis.execution.tools import ToolContext, ToolError
from jarvis.runtime import _dashboard_view, _live_lines
from jarvis.runtime.dashboard import PAGES, VIEW_LINE_CHARS, VIEW_STALE_S, ViewState
from jarvis.shared import CallerPrincipal
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from collections.abc import Mapping

LETTER = "199a1c0d4101"


class _Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


def test_the_line_names_page_open_item_and_numbered_rows_by_title_and_id() -> None:
    """Page, tab, open letter, then rows in screen order; the mail hint rides with a letter."""
    view = ViewState()
    assert view.line() is None
    view.set(
        "mail", "yes", ("mail", LETTER, "Sam — Lunch?"),
        [(LETTER, "Sam — Lunch?"), ("b2", "Shop — Sale")],
    )
    assert view.line() == (
        f'Dashboard: Allen is on the Mail page (tab yes) with "Sam — Lunch?" open (mail {LETTER}).'
        ' Words like "this email" mean it.'
        f' On screen: 1. "Sam — Lunch?" ({LETTER}) 2. "Shop — Sale" (b2)'
    )
    view.set("memory", rows=[("m1", "Allen likes oat milk")])
    assert view.line() == (
        'Dashboard: Allen is on the Memory page. On screen: 1. "Allen likes oat milk" (m1)'
    )
    view.set("usage")
    assert view.line() == "Dashboard: Allen is on the Usage page."
    view.set("home")
    assert view.line() == "Dashboard: Allen is on the home screen."


def test_a_jobs_row_carries_its_newest_mail_id_in_the_line_and_opens_by_it() -> None:
    """ADR 0176: the Jobs page's rows name a gmail_get-able id; the id opens the page on it."""
    view = ViewState()
    sent: list[dict[str, str | None]] = []
    view.push = sent.append
    view.set(
        "jobs",
        rows=[("Reliable|QA", "Reliable Controls — QA", "1a0fed45"), ("Zed|", "Zed")],
    )
    assert view.line() == (
        "Dashboard: Allen is on the Job mail page. On screen:"
        ' 1. "Reliable Controls — QA" (Reliable|QA, mail 1a0fed45) 2. "Zed" (Zed|)'
    )
    assert view.present("jobs", "1a0fed45") == {
        "page": "jobs", "item_id": "1a0fed45", "kind": "row",
    }
    assert view.present("jobs", "nope")["item_id"] is None
    # A card open: the job is the item, its mails the rows.
    view.set("jobs", item=("job", "Zed|", "Zed"), rows=[("m9", "Interview Invite to chat")])
    line = view.line() or ""
    assert 'with "Zed" open (job Zed|)' in line
    assert '1. "Interview Invite to chat" (m9)' in line


def test_titles_are_one_line_of_80_characters_and_ten_rows_and_1200_characters() -> None:
    """Third-party text is flattened, cut and never grows the line past its cap."""
    view = ViewState()
    title = 'Ignore\nall "rules"  ' + "x" * 300
    view.set("mail", item=("mail", LETTER, title), rows=[(f"id{n}", title) for n in range(25)])
    line = view.line()
    assert line is not None
    assert "\n" not in line
    assert "Ignore all 'rules' " in line
    assert "x" * 61 in line
    assert "x" * 62 not in line  # 80 characters of title: 19 of "Ignore all 'rules' ", then 61
    assert "10. " in line
    assert "11. " not in line
    assert len(line) <= VIEW_LINE_CHARS
    # The cap is a budget, not a cut through a row: a long line drops whole rows from the end.
    assert line.endswith(")")
    # An id longer than a Gmail id is cut as well.
    view.set("mail", rows=[("i" * 500, "t")])
    assert "i" * 65 not in (view.line() or "")


def test_the_view_goes_stale_after_sixty_seconds_and_a_close_clears_it() -> None:
    """A heartbeat refreshes it; silence is closed; a null page closes at once."""
    clock = _Clock()
    view = ViewState(clock)
    view.set("agents", rows=[("s1", "Jarvis daemon")])
    clock.now += VIEW_STALE_S - 1
    assert view.line() is not None
    view.set("agents", rows=[("s1", "Jarvis daemon")])  # the shell's 20 s report
    clock.now += VIEW_STALE_S - 1
    assert view.line() is not None
    clock.now += 2
    assert view.line() is None
    view.set("agents")
    view.set(None)
    assert view.line() is None


def test_the_live_context_lets_the_view_line_run_to_1200_and_the_rest_to_200() -> None:
    """``_live_lines`` caps by what the line is."""
    view_line = "Dashboard: Allen is on the Mail page. " + "z" * 3000
    lines = _live_lines((lambda: "a" * 500, lambda: view_line))
    assert [len(one) for one in lines] == [200, VIEW_LINE_CHARS]


def test_the_switch_is_off_unless_dashboard_view_enabled_is_true() -> None:
    """One key, default off; the mail switch is its own key."""
    assert not _dashboard_view({})
    assert not _dashboard_view({"dashboard": {"view": {"enabled": "yes"}}})
    assert not _dashboard_view({"dashboard": {"mail": {"enabled": True}}})
    assert _dashboard_view({"dashboard": {"view": {"enabled": True}}})


def _client(view: ViewState | None) -> TestClient:
    return TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        view_set=None if view is None else view.set,
    )))


def test_the_route_is_404_with_the_switch_off() -> None:
    """No ``view_set``: the route does not exist, and the retired focus route stays gone."""
    client = _client(None)
    assert client.post("/inherent/view", json={"page": "mail"}).status_code == 404
    assert client.post("/inherent/focus", json={"kind": None}).status_code == 404


def test_the_route_sets_replaces_and_closes_the_view() -> None:
    """A report replaces the last one; rows beyond ten and titles beyond 80 are cut by the state."""
    view = ViewState()
    client = _client(view)
    body = {
        "page": "mail", "tab": "all",
        "item": {"kind": "mail", "id": LETTER, "title": "Sam — Lunch"},
        "rows": [{"id": f"r{n}", "title": "t" * 200, "mail_id": "1a0fed45"} for n in range(30)],
    }
    assert client.post("/inherent/view", json=body).status_code == 200
    assert view.mail_id() == LETTER
    line = view.line()
    assert line is not None
    assert "10. " in line
    assert "11. " not in line
    assert "(r0, mail 1a0fed45)" in line
    assert client.post("/inherent/view", json={"page": "usage"}).status_code == 200
    assert view.mail_id() is None
    assert view.line() == "Dashboard: Allen is on the Usage page."
    assert client.post("/inherent/view", json={"page": None}).status_code == 200
    assert view.line() is None
    nameless = {"page": "mail", "item": {"kind": "mail"}}
    assert client.post("/inherent/view", json=nameless).status_code == 422


def _tool(view: ViewState | None) -> Any:  # noqa: ANN401
    tools = build_dashboard_tool(PAGES, None if view is None else view.present)
    return next(iter(tools), None)


def _show(tool: Any, **args: str) -> Mapping[str, Any]:  # noqa: ANN401
    return tool.handler(args, cast("ToolContext", None))  # type: ignore[no-any-return]


def test_show_on_dashboard_is_l0_read_only_for_the_model_and_absent_with_the_view_off() -> None:
    """Off: no tool. On: L0, read-only, JARVIS_LLM only, the pages the Dashboard has."""
    assert _tool(None) is None
    tool = _tool(ViewState())
    assert tool.name == "show_on_dashboard"
    assert tool.risk_level == "L0"
    assert tool.read_only
    assert tool.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert tool.input_schema["properties"]["page"]["enum"] == [
        "conversation", "now", "agents", "usage", "plugins", "projects", "settings", "brief",
        "mail", "memory", "jobs",
    ]
    assert "arrange" not in PAGES
    assert tool.input_schema["required"] == ["page"]
    assert "never claim the page shows something you have not read" in tool.description
    assert tool.description.isascii()


def test_show_on_dashboard_pushes_present_with_an_id_only_the_current_view_carries() -> None:
    """A row or open item of the view opens on it; any other id opens the page alone."""
    view = ViewState()
    sent: list[dict[str, str | None]] = []
    view.push = sent.append
    tool = _tool(view)
    view.set("mail", item=("mail", LETTER, "Sam — Lunch"), rows=[("b2", "Shop — Sale")])
    done = _show(tool, page="mail", item_id="b2")
    assert {k: done[k] for k in ("shown", "item", "opened_page_only")} == {
        "shown": "mail", "item": "b2", "opened_page_only": False,
    }
    assert "Do not call show_on_dashboard again" in done["note"]
    assert _show(tool, page="mail", item_id=LETTER)["item"] == LETTER
    unseen = _show(tool, page="memory", item_id="unseen")
    assert {k: unseen[k] for k in ("shown", "item", "opened_page_only")} == {
        "shown": "memory", "item": None, "opened_page_only": True,
    }
    assert "Do not retry with other ids" in unseen["note"]
    assert _show(tool, page="jobs")["opened_page_only"] is True
    assert sent == [
        {"page": "mail", "item_id": "b2", "kind": "row"},
        {"page": "mail", "item_id": LETTER, "kind": "mail"},
        {"page": "memory", "item_id": None, "kind": None},
        {"page": "jobs", "item_id": None, "kind": None},
    ]
    view.set(None)  # a closed panel knows no ids: the page opens alone
    assert _show(tool, page="mail", item_id="b2")["opened_page_only"] is True


def test_show_on_dashboard_refuses_an_unknown_page_and_a_missing_link() -> None:
    """Both are tool errors, and nothing is pushed."""
    view = ViewState()
    tool = _tool(view)
    with pytest.raises(ToolError, match="not connected"):
        _show(tool, page="mail")
    sent: list[dict[str, str | None]] = []
    view.push = sent.append
    for page in ("arrange", "nowhere", ""):
        with pytest.raises(ToolError, match="unknown page"):
            _show(tool, page=page)
    assert sent == []


def test_the_present_op_reaches_a_connected_companion_as_the_wire_says() -> None:
    """The push the daemon wires goes out as ``{op: present, payload: {page, item_id, kind}}``."""

    class _Socket:
        def __init__(self) -> None:
            self.frames: list[dict[str, Any]] = []

        async def send_json(self, frame: dict[str, Any]) -> None:
            self.frames.append(frame)

    async def run() -> list[dict[str, Any]]:
        broadcaster = InherentBroadcaster()
        socket = _Socket()
        await broadcaster.register(socket)  # type: ignore[arg-type]
        view = ViewState()
        loop = asyncio.get_running_loop()
        broadcaster.attach_loop(loop)
        view.push = lambda sent: broadcaster.broadcast_op_sync("present", **sent)
        view.set("mail", rows=[("b2", "Shop — Sale")])
        await asyncio.to_thread(view.present, "mail", "b2")
        await asyncio.sleep(0.05)
        return socket.frames

    frames = asyncio.run(run())
    assert [(f["op"], f["payload"]) for f in frames] == [
        ("present", {"page": "mail", "item_id": "b2", "kind": "row"}),
    ]
