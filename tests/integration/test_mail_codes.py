"""ADR 0266 — a verification code in new mail reaches the Mac clipboard.

Data-driven: ``find_code`` over realistic mails (input -> expected code or none). Then the real
poller over a fake ``gmail`` client and a fake device link: a fresh code mail makes one
``copy_code`` call with the right code, the same mail next cycle none, an old mail none, and a
terminal that is away is tried again. The terminal's own op runs with ``pbcopy`` and ``osascript``
replaced, to see the clipboard text, the notification arguments and what a bad code does.
"""

from __future__ import annotations

import json
import re
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
from tests.canary._helpers import repo_root

if TYPE_CHECKING:
    from collections.abc import Mapping

SAMPLES = json.loads(
    (repo_root() / "tests/fixtures/mail_codes/samples.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: s["subject"] or s["body"][:20])
def test_find_code(sample: dict[str, Any]) -> None:
    """Each sample mail gives its code, or none for a receipt, newsletter, phone, link ..."""
    assert find_code(sample["subject"], sample["body"]) == sample["expected"]


NOW = datetime.now(UTC)


def _mail(key: str, subject: str, body: str, age: timedelta) -> dict[str, Any]:
    return {
        "id": key, "threadId": key, "from": "GitHub <noreply@github.com>", "subject": subject,
        "date": format_datetime(NOW - age), "body": body,
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


class _Connections:
    def __init__(self, gmail: _Gmail) -> None:
        self.gmail = gmail

    def client_for(self, server: str) -> _Gmail:
        if server != "gmail":
            msg = "not connected"
            raise ToolError(msg, code="mcp_server")
        return self.gmail


def test_poller_hands_over_each_fresh_code_once() -> None:
    """Fresh code mail: one copy_code call; same mail again, an old mail, plain mail: none."""
    gmail = _Gmail([
        _mail("fresh", "Your code", "Verification code: 482913", timedelta(minutes=1)),
        _mail("old", "Your code", "Verification code: 111222", timedelta(minutes=30)),
        _mail("plain", "Lunch?", "Noon works for me.", timedelta(minutes=1)),
    ])
    sent: list[tuple[str, Mapping[str, Any]]] = []

    def device(op: str, args: Mapping[str, Any], target: str | None) -> dict[str, Any]:
        assert target is None
        sent.append((op, dict(args)))
        return {"copied": True}

    poller = MailCodes(MailCodesSettings(20, 10), _Connections(gmail), device)  # type: ignore[arg-type]
    assert poller.poll_once() == 1
    assert sent == [(COPY_CODE, {"code": "482913", "sender": "GitHub"})]
    assert poller.poll_once() == 0
    assert len(sent) == 1
    search = next(args for tool, args in gmail.calls if tool == "gmail_search")
    assert re.fullmatch(r"after:\d{10} -in:sent -in:drafts", search["query"])
    assert {tool for tool, _ in gmail.calls} == {"gmail_search", "gmail_get"}
    reads = [a["messageId"] for tool, a in gmail.calls if tool == "gmail_get"]
    assert reads.count("fresh") == 1


def test_poller_retries_while_the_terminal_is_away() -> None:
    """A terminal that is not connected is tried again; once delivered, never again."""
    mail = _mail("fresh", "Your code", "Verification code: 482913", timedelta(minutes=1))
    gmail = _Gmail([mail])
    away = [True]
    sent: list[str] = []

    def device(_op: str, args: Mapping[str, Any], _target: str | None) -> dict[str, Any]:
        if away[0]:
            msg = "the Mac is not connected"
            raise DeviceCallError(msg, code="device_not_connected")
        sent.append(args["code"])
        return {}

    poller = MailCodes(MailCodesSettings(20, 10), _Connections(gmail), device)  # type: ignore[arg-type]
    assert poller.poll_once() == 0
    away[0] = False
    assert poller.poll_once() == 1
    assert poller.poll_once() == 0
    assert sent == ["482913"]


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
