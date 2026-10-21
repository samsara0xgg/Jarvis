"""ADR 0266 — a verification code in new mail reaches the Mac clipboard.

Data-driven: ``find_code`` over realistic mails (input -> expected code or none), among them the
rows of ADR 0369: a bare "code" or "PIN" is a code only in a sentence that says what it is for, and
a number that the words just before it make an account, order, promo or other number is not the
code. Then the real poller over a fake ``gmail`` client, a fake device link and a real event log:
a fresh code mail makes one ``copy_code`` call with the right code and one ``mail.code_copied``
event, the same mail next cycle none, an old mail none, and a terminal that is away is tried
again. A poller built anew over the same log (the brain restarted inside the window) copies the
mail no second time and reads it no more, and the event holds the mail's id and its sender's
domain, never the code. The terminal's own op runs with ``pbcopy`` and ``osascript`` replaced, to
see the clipboard text, the notification arguments and what a bad code does.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.execution.tools import ToolError, build_default_registry
from jarvis.runtime.mail_codes import MailCodes, MailCodesSettings, find_code
from jarvis.runtime.terminal import _declared, make_executor
from jarvis.shared import lang
from jarvis.shared.device_link import COPY_CODE, DeviceCallError
from jarvis.state.event_log import iter_events, iter_events_of_types
from tests.canary._helpers import repo_root
from tests.support.clock import FakeClock
from tests.support.runtime_log import RuntimeLog

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

SAMPLES = json.loads(
    (repo_root() / "tests/fixtures/mail_codes/samples.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: s["subject"] or s["body"][:20])
def test_find_code(sample: dict[str, Any]) -> None:
    """Each sample mail gives its code, or none for a receipt, newsletter, phone, link ..."""
    assert find_code(sample["subject"], sample["body"]) == sample["expected"], sample.get("why")


START = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def _mail(
    key: str, subject: str, body: str, age: timedelta, sender: str = "GitHub <noreply@github.com>",
) -> dict[str, Any]:
    return {
        "id": key, "threadId": key, "from": sender, "subject": subject,
        "date": format_datetime(START - age), "body": body,
    }


class _Gmail:
    def __init__(self, mails: list[dict[str, Any]]) -> None:
        self.mails = mails
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "gmail"
        self.calls.append((tool, dict(args)))
        if tool == "gmail_search":
            return {"text": json.dumps({"messages": [{"id": m["id"]} for m in self.mails]})}
        assert tool == "gmail_get", f"mail codes must only read: {tool}"
        return {"text": json.dumps(next(m for m in self.mails if m["id"] == args["messageId"]))}

    def reads(self) -> list[str]:
        return [a["messageId"] for tool, a in self.calls if tool == "gmail_get"]


class _Connections:
    def __init__(self, gmail: _Gmail) -> None:
        self.gmail = gmail

    def client_for(self, server: str) -> _Gmail:
        if server != "gmail":
            msg = "not connected"
            raise ToolError(msg, code="mcp_server")
        return self.gmail


class _Terminal:
    """The terminal's end of the device link: keeps each ``copy_code`` it is asked for."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fails: str | None = None  # a DeviceCallError code, while set

    def __call__(self, op: str, args: Mapping[str, Any], target: str | None) -> dict[str, Any]:
        assert target is None
        if self.fails:
            msg = "the Mac did not copy it"
            raise DeviceCallError(msg, code=self.fails)
        self.sent.append((op, dict(args)))
        return {"copied": True}


@pytest.fixture
def log(tmp_path: Path) -> Iterator[RuntimeLog]:
    """A runtime root with a real event log: the poller's, and what a restart finds again."""
    one = RuntimeLog(tmp_path)
    yield one
    one.conn.close()


@pytest.fixture
def clock() -> FakeClock:
    """The poller's time, which the test moves."""
    return FakeClock(START)


def _poller(log: RuntimeLog, gmail: _Gmail, terminal: _Terminal, clock: FakeClock) -> MailCodes:
    """A poller as the daemon builds it at boot: a new one over the same log is a restart."""
    poller = MailCodes(
        MailCodesSettings(20, 10), _Connections(gmail), log.paths.event_log, terminal,  # type: ignore[arg-type]
    )
    poller.now = clock.now
    return poller


def _copies(log: RuntimeLog) -> list[Mapping[str, Any]]:
    """The payload of each ``mail.code_copied`` row, oldest first."""
    return [e.payload for e in iter_events_of_types(log.conn, ("mail.code_copied",))]


def test_poller_hands_over_each_fresh_code_once(
    log: RuntimeLog, clock: FakeClock,
) -> None:
    """Fresh code mail: one copy, one event; the same mail again, an old one, plain mail: none."""
    gmail = _Gmail([
        _mail("fresh", "Your code", "Verification code: 482913", timedelta(minutes=1)),
        _mail("old", "Your code", "Verification code: 111222", timedelta(minutes=30)),
        _mail("plain", "Lunch?", "Noon works for me.", timedelta(minutes=1)),
    ])
    terminal = _Terminal()
    poller = _poller(log, gmail, terminal, clock)
    assert poller.poll_once() == 1
    assert terminal.sent == [(COPY_CODE, {"code": "482913", "sender": "GitHub"})]
    assert poller.poll_once() == 0
    assert len(terminal.sent) == 1
    search = next(args for tool, args in gmail.calls if tool == "gmail_search")
    assert re.fullmatch(r"after:\d{10} -in:sent -in:drafts", search["query"])
    assert {tool for tool, _ in gmail.calls} == {"gmail_search", "gmail_get"}
    assert gmail.reads().count("fresh") == 1
    assert _copies(log) == [{"message_id": "fresh", "sender_domain": "github.com"}]


def test_poller_retries_while_the_terminal_is_away(log: RuntimeLog, clock: FakeClock) -> None:
    """A terminal that is not connected is tried again; once delivered, never again."""
    gmail = _Gmail([_mail("fresh", "Your code", "Verification code: 482913", timedelta(minutes=1))])
    terminal = _Terminal()
    terminal.fails = "device_not_connected"
    poller = _poller(log, gmail, terminal, clock)
    assert poller.poll_once() == 0
    assert _copies(log) == []
    terminal.fails = None
    assert poller.poll_once() == 1
    assert poller.poll_once() == 0
    assert [args["code"] for _op, args in terminal.sent] == ["482913"]
    assert len(_copies(log)) == 1


def test_a_restart_inside_the_window_copies_a_mail_once_and_reads_it_no_more(
    log: RuntimeLog, clock: FakeClock,
) -> None:
    """The brain restarts with the mails still inside the window: each copied once, read once."""
    gmail = _Gmail([
        _mail("first", "Your code", "Verification code: 482913", timedelta(minutes=2)),
        _mail("plain", "Lunch?", "Noon works for me.", timedelta(minutes=2)),
        _mail("second", "Sign in", "Your login code is 650318", timedelta(minutes=1)),
    ])
    terminal = _Terminal()
    assert _poller(log, gmail, terminal, clock).poll_once() == 2
    assert gmail.reads() == ["first", "plain", "second"]
    clock.advance(90)
    restarted = _poller(log, gmail, terminal, clock)
    gmail.mails.append(
        _mail("later", "Sign in", "Your login code is 731904", timedelta(seconds=30)),
    )
    assert restarted.poll_once() == 1  # the new mail is copied; the two already copied are not
    assert restarted.poll_once() == 0
    assert [args["code"] for _op, args in terminal.sent] == ["482913", "650318", "731904"]
    after_restart = gmail.reads()[3:]  # no gmail_get for a mail a code was copied from
    assert "first" not in after_restart
    assert "second" not in after_restart
    assert after_restart.count("later") == 1
    assert [one["message_id"] for one in _copies(log)] == ["first", "second", "later"]


def test_the_event_holds_the_mail_and_its_sender_and_never_the_code(
    log: RuntimeLog, clock: FakeClock,
) -> None:
    """One event per copied code, of the mail and its sender's domain; the code is in none."""
    gmail = _Gmail([
        _mail(
            "fresh", "Your code", "Verification code: 482913", timedelta(minutes=1),
            sender="Acme Support <Support@Mail.Acme.example>",
        ),
    ])
    assert _poller(log, gmail, _Terminal(), clock).poll_once() == 1
    assert _copies(log) == [{"message_id": "fresh", "sender_domain": "mail.acme.example"}]
    assert all("482913" not in json.dumps(e.payload) for e in iter_events(log.conn))


def test_a_mail_with_no_code_or_whose_code_was_not_copied_leaves_no_event(
    log: RuntimeLog, clock: FakeClock,
) -> None:
    """Only a copied code is recorded: not a plain mail, not a code the terminal failed to copy."""
    gmail = _Gmail([
        _mail("plain", "Lunch?", "Noon works for me.", timedelta(minutes=1)),
        _mail("failed", "Your code", "Verification code: 482913", timedelta(minutes=1)),
    ])
    terminal = _Terminal()
    terminal.fails = "clipboard_failed"
    poller = _poller(log, gmail, terminal, clock)
    assert poller.poll_once() == 0
    assert terminal.sent == []
    assert _copies(log) == []


def test_a_log_that_cannot_be_read_stops_the_cycle_before_any_mail_is_read(
    tmp_path: Path, clock: FakeClock,
) -> None:
    """Without the record of what was copied the poller cannot tell, so it reads nothing."""
    gmail = _Gmail([_mail("fresh", "Your code", "Verification code: 482913", timedelta(minutes=1))])
    terminal = _Terminal()
    poller = MailCodes(
        MailCodesSettings(20, 10), _Connections(gmail), tmp_path / "missing.db", terminal,  # type: ignore[arg-type]
    )
    poller.now = clock.now
    with pytest.raises(sqlite3.OperationalError):
        poller.poll_once()
    assert gmail.reads() == []
    assert terminal.sent == []


def test_terminal_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """The terminal declares copy_code, copies the code, notifies by arguments, refuses junk."""
    ran: list[tuple[list[str], bytes | None]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:  # noqa: ANN401
        ran.append((cmd, kwargs.get("input")))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    registry = build_default_registry()
    assert COPY_CODE in _declared(registry)
    execute = make_executor(registry)
    reply = execute(COPY_CODE, {"code": "000000", "sender": 'Eve" & quit'}, None)
    assert reply == {"ok": True, "output": {"copied": True, "notified": True}}
    assert ran[0] == (["/usr/bin/pbcopy"], b"000000")
    notify = ran[1][0]
    assert notify[0] == "/usr/bin/osascript"
    assert notify[-2:] == [lang.t("mail.code_copied", code="000000"), 'Eve" & quit']
    assert "000000" not in notify[2]  # the script holds no text of the mail
    bad = execute(COPY_CODE, {"code": "1; rm -rf /", "sender": ""}, None)
    assert not bad["ok"]
    assert bad["code"] == "bad_code"
    assert len(ran) == 2
