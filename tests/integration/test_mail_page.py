"""ADR 0366: the Dashboard's draft under an open letter is a Gmail draft the ``mail`` tool wrote.

One fake Gmail (``tests.support.gmail_fake``), the real ``mail`` tool, the real page and the real
event log. The page finds the draft by the open letter's conversation, writes his edits as
revisions through the tool's own code, discards through its undo and raises the send card for it.
No network, no key, no model; every address is on example.com.
"""

from __future__ import annotations

import json
from contextlib import closing
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jarvis.execution.mail_gmail import Mailbox
from jarvis.execution.mail_tool import build_mail_tool
from jarvis.execution.tools import build_default_registry
from jarvis.runtime.daemon.routes import _send_draft, draft_deps
from jarvis.runtime.dashboard import DRAFT_LINE_CHARS, ViewState
from jarvis.runtime.home import Home
from jarvis.runtime.mail_page import MailPage
from jarvis.shared import lang
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps
from tests.support.clock import FakeClock
from tests.support.mail_rig import ME, NOW, SAM, ZONE, Rig, ago, mailbox

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.support.gmail_fake import Gmail

LETTER = "plan"
THREAD = "tl"


class _Connections:
    def __init__(self, gmail: Gmail) -> None:
        self._gmail = gmail

    def client_for(self, server: str) -> Gmail:
        assert server == "gmail"
        return self._gmail


class Page:
    """The page, the tool and the Gmail they share, over one event log."""

    def __init__(self, tmp_path: Path, gmail: Gmail) -> None:
        """Wire the tool, the home and the page over ``gmail`` and one event log."""
        self.gmail = gmail
        self.rig = Rig(tmp_path, gmail)
        self.view = ViewState()
        self.home = Home(
            _Connections(gmail),  # type: ignore[arg-type]
            ("America/Vancouver", ZoneInfo("America/Vancouver")), None, None, self.view,
        )
        self.log = self.rig.world.paths.event_log
        self.clock = FakeClock(NOW, zone=ZONE)
        self.page = MailPage(self.view, self.home, self.log, self.clock)
        self.view.set("mail", item=("mail", LETTER, "Sam Lee - Lunch"))

    def events(self, kind: str) -> list[Any]:
        """Every event of ``kind`` in the log, oldest first."""
        with closing(open_event_log(self.log)) as conn:
            return list(iter_events_of_types(conn, [kind]))


@pytest.fixture(autouse=True)
def _english() -> Iterator[None]:
    """The page's own words (who is copied) are English here; the language is put back."""
    before = lang.language()
    lang.set_language("en")
    yield
    lang.set_language(before)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[Page]:
    """A mailbox with his sent mail, and Sam's mail in conversation ``tl`` open on the page."""
    gmail = mailbox()
    gmail.add(LETTER, "Lunch", sender=SAM, to=ME, thread=THREAD, at=ago(hours=2))
    gmail.add("other", "Notes", sender=SAM, to=ME, thread="to", at=ago(hours=3))
    made = Page(tmp_path, gmail)
    yield made
    made.rig.close()


def _draft(world: Page, **more: Any) -> dict[str, Any]:  # noqa: ANN401 — the model's arguments.
    return world.rig.ok(action="draft", reply_to=LETTER, body="Thursday works.", **more)


def test_the_draft_under_a_letter_is_the_newest_open_draft_in_its_conversation(
    world: Page,
) -> None:
    """None first; the tool's draft by number, names and subject; another conversation has none."""
    assert world.page.view(LETTER) == {"draft": None}
    made = _draft(world)
    assert world.page.view(LETTER) == {"draft": {
        "revision": 1, "draft": made["draft"], "to": "Sam Lee <sam@example.com>",
        "subject": "Re: Lunch", "body": "Thursday works.", "by": "jarvis",
    }}
    assert world.page.view("other") == {"draft": None}
    again = world.rig.ok(
        action="draft", replaces=made["draft"], body="Thursday at noon works.",
    )
    shown = world.page.view(LETTER)["draft"]
    assert (shown["revision"], shown["draft"], shown["body"]) == (
        2, again["draft"], "Thursday at noon works.",
    )
    restarted = MailPage(world.view, world.home, world.log, world.clock)
    assert restarted.view(LETTER) == world.page.view(LETTER)  # the log holds it, not memory


def test_a_draft_that_goes_to_more_people_says_who_is_copied(world: Page) -> None:
    """Cc and Bcc follow the To, in the page's language."""
    _draft(world, cc="kim@example.com")
    assert world.page.view(LETTER)["draft"]["to"] == (
        "Sam Lee <sam@example.com> (Cc kim@example.com)"
    )


def test_his_edit_is_a_revision_the_tool_writes_and_keeps_what_he_did_not_change(
    world: Page,
) -> None:
    """A new Gmail draft in the thread replaces the old one; by owner; blank subject is kept."""
    made = _draft(world)
    saved = world.page.save(LETTER, "Thursday it is", "Yes, 3pm.")["draft"]
    assert (saved["revision"], saved["by"], saved["subject"], saved["body"]) == (
        2, "owner", "Thursday it is", "Yes, 3pm.",
    )
    assert saved["to"] == "Sam Lee <sam@example.com>"
    assert saved["draft"] != made["draft"]
    mine = world.gmail.of("gmail_createDraft")[-1]
    assert (mine["to"], mine["threadId"], mine["subject"]) == (
        "sam@example.com", THREAD, "Thursday it is",
    )
    assert [one["messageId"] for one in world.gmail.of("gmail_modify")] == ["d1"]  # to Trash
    (_, owned) = world.events("mail.draft_written")
    assert owned.payload["by"] == "owner"
    assert owned.payload["replaces"] == world.events("mail.draft_written")[0].event_uid
    world.page.save(LETTER, "  ", "Yes, 3pm, at the usual place.")  # a blank subject is kept
    current = world.page.view(LETTER)["draft"]
    assert current["subject"] == "Thursday it is"
    after = world.rig.ok(action="draft", replaces=current["draft"], body="Yes, 3pm works.")
    shown = world.page.view(LETTER)["draft"]
    assert (shown["by"], shown["subject"], shown["draft"]) == (
        "jarvis", "Thursday it is", after["draft"],
    )  # the tool's revision keeps the subject he set


def test_an_edit_that_changes_nothing_writes_nothing(world: Page) -> None:
    """The page saves on leaving the letter whatever it holds; the same letter is not rewritten."""
    _draft(world)
    before = len(world.gmail.of("gmail_createDraft"))
    again = world.page.save(LETTER, "Re: Lunch", "Thursday works.")
    assert again["draft"]["by"] == "jarvis"
    assert len(world.gmail.of("gmail_createDraft")) == before


def test_an_edit_with_no_draft_writes_a_reply_to_the_open_letter(world: Page) -> None:
    """The recipients, subject and thread are the reply's; an empty body is the route's 400."""
    with pytest.raises(ValueError, match="needs body"):
        world.page.save(LETTER, "", "  ")
    assert world.gmail.of("gmail_createDraft") == []
    shown = world.page.save(LETTER, "", "Friday?")["draft"]
    assert (shown["to"], shown["subject"], shown["by"]) == (
        "Sam Lee <sam@example.com>", "Re: Lunch", "owner",
    )
    assert world.gmail.of("gmail_createDraft")[0]["threadId"] == THREAD


def test_discard_moves_the_draft_to_trash_and_a_refusal_keeps_it(world: Page) -> None:
    """The draft's undo; Gmail refusing leaves it under the letter and says so."""
    _draft(world)
    world.gmail.fail["gmail_modify"] = "Insufficient permissions"
    with pytest.raises(RuntimeError, match="still in his Drafts"):
        world.page.discard(LETTER)
    assert world.page.view(LETTER)["draft"] is not None
    del world.gmail.fail["gmail_modify"]
    world.page.discard(LETTER)
    assert world.page.view(LETTER) == {"draft": None}
    assert world.gmail.drafted() == []
    (closed,) = world.events("mail.draft_closed")
    assert closed.payload["how"] == "discarded"
    world.page.discard(LETTER)  # nothing left: nothing happens


def test_the_live_line_names_the_draft_its_text_and_who_last_edited_it(world: Page) -> None:
    """The model's one line about the open letter's draft: the number it can revise by."""
    assert world.page.line() is None
    made = _draft(world)
    assert world.page.line() == (
        f"Draft reply under it (draft {made['draft']}, last edited by Jarvis): Thursday works."
    )
    saved = world.page.save(LETTER, "", "Yes, 3pm.")["draft"]
    assert world.page.line() == (
        f"Draft reply under it (draft {saved['draft']}, last edited by Allen): Yes, 3pm."
    )
    world.view.set("mail", item=None)
    assert world.page.line() is None  # no letter open
    world.view.set("mail", item=("mail", "nope", "x"))
    assert world.page.line() is None  # a letter Gmail does not know


def test_send_saves_his_edit_raises_the_card_and_the_button_sends_that_draft(
    world: Page, tmp_path: Path,
) -> None:
    """The card is ``mail`` ``send`` of the draft; the leased send sends it as it is."""
    _draft(world)
    registry = build_default_registry()
    registry.register(*build_mail_tool(Mailbox(world.gmail.tool)))
    runtime = SimpleNamespace(
        runtime_paths=SimpleNamespace(event_log=world.log, artifacts_root=tmp_path),
        tool_registry=registry,
        config=SimpleNamespace(confirmation=SimpleNamespace(ttl_ms=600_000)),
    )
    with pytest.raises(ValueError, match="empty"):
        _send_draft(runtime, world.page, LETTER, "Re: Lunch", "  ")  # type: ignore[arg-type]
    _send_draft(runtime, world.page, LETTER, "Re: Lunch", "Thursday at 3.")  # type: ignore[arg-type]
    number = world.page.view(LETTER)["draft"]["draft"]
    (card,) = [one.payload for one in world.events("confirmation.requested")]
    (frozen,) = card["calls"]
    assert frozen["args_meta"] == {
        "action": "send", "to": ["Sam Lee <sam@example.com>"], "cc": [], "bcc": [],
        "subject": "Re: Lunch", "body": "Thursday at 3.", "thread": THREAD, "draft": number,
        "you": [],
    }
    assert frozen["risk_level"] == "L3"
    made = len(world.gmail.of("gmail_createDraft"))
    assert world.gmail.of("gmail_sendDraft") == []  # nothing is sent before his button
    sent = world.rig.leased(**frozen["args_meta"])
    assert sent.error is None
    assert json.loads(sent.tool_output or "{}")["summary"] == (
        'Sent "Re: Lunch" to Sam Lee.'
    )
    assert len(world.gmail.of("gmail_createDraft")) == made  # the draft itself was sent
    assert [one["draftId"] for one in world.gmail.of("gmail_sendDraft")] == ["r2"]
    assert world.page.view(LETTER) == {"draft": None}  # a sent draft is no longer under it


def _client(world: Page, tmp_path: Path) -> TestClient:
    """The four draft routes over the page, wired as the daemon's build_deps does."""
    registry = build_default_registry()
    registry.register(*build_mail_tool(Mailbox(world.gmail.tool)))
    runtime = SimpleNamespace(
        mail_page=world.page,
        runtime_paths=SimpleNamespace(event_log=world.log, artifacts_root=tmp_path),
        tool_registry=registry,
        config=SimpleNamespace(confirmation=SimpleNamespace(ttl_ms=600_000)),
    )
    return TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(),
        **draft_deps(runtime),  # type: ignore[arg-type]
    )))


def test_the_draft_routes_keep_their_shapes_over_the_page(world: Page, tmp_path: Path) -> None:
    """GET, POST, send and discard: the answers the Mac page reads; an unknown letter is a 404."""
    client = _client(world, tmp_path)
    base = f"/inherent/mail/{LETTER}/draft"
    assert client.get(base).json() == {"draft": None}
    made = _draft(world)
    got = client.get(base).json()["draft"]
    assert set(got) == {"revision", "draft", "to", "subject", "body", "by"}
    assert (got["draft"], got["by"]) == (made["draft"], "jarvis")
    saved = client.post(base, json={"subject": "Thursday it is", "body": "Yes, 3pm."}).json()
    assert (saved["draft"]["by"], saved["draft"]["revision"]) == ("owner", 2)
    assert client.post(base, json={"subject": "", "body": " "}).status_code == 400
    assert client.post(f"{base}/send", json={"subject": "", "body": ""}).status_code == 400
    assert client.post(f"{base}/send", json={"subject": "", "body": "Yes, 3pm."}).status_code == 200
    assert len(world.events("confirmation.requested")) == 1
    assert client.post(f"{base}/discard").status_code == 200
    assert client.get(base).json() == {"draft": None}
    assert client.get("/inherent/mail/nope/draft").status_code == 404


def test_a_discard_gmail_refuses_is_a_502_and_the_draft_stays(world: Page, tmp_path: Path) -> None:
    """The draft is still in his Drafts, so the page still shows it."""
    client = _client(world, tmp_path)
    _draft(world)
    world.gmail.fail["gmail_modify"] = "Insufficient permissions"
    assert client.post(f"/inherent/mail/{LETTER}/draft/discard").status_code == 502
    assert client.get(f"/inherent/mail/{LETTER}/draft").json()["draft"] is not None


def test_the_routes_are_off_without_the_page_and_the_line_fits_its_cap(world: Page) -> None:
    """No page, no routes; a long draft's line is cut to the one size a line may run to."""
    assert draft_deps(SimpleNamespace(mail_page=None)) == {}  # type: ignore[arg-type]
    world.rig.ok(action="draft", reply_to=LETTER, body="word " * 3000)
    line = world.page.line()
    assert line is not None
    assert len(line) <= DRAFT_LINE_CHARS
