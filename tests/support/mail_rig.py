"""One ``mail`` tool over a fake Gmail, dispatched as the daemon does: the rig the mail tests share.

The mailbox is invented (``tests.support.gmail_fake``), the dispatcher and event log are the real
ones (``tests.support.tool_world``), and the clock stands at 11:00 on Wednesday in Vancouver. No
network, no key; every address is on example.com.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis.execution.mail_gmail import Mailbox
from jarvis.execution.mail_tool import build_mail_tool
from tests.support.clock import FakeClock
from tests.support.gmail_fake import SENT, Gmail
from tests.support.tool_world import ToolWorld, tool_request

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.shared import RawResult

ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)  # 11:00 on Wednesday in Vancouver
ME = "Allen <allen@example.com>"
SAM = "Sam Lee <sam@example.com>"
KIM = "Kim Ray <kim@example.com>"
SHOP = "Shop <deals@shop.example.com>"
BANK = "Bank <alerts@bank.example.com>"
NEWS = {"List-Unsubscribe": "<mailto:u@shop.example.com>"}
BOT = {"Auto-Submitted": "auto-generated"}


def ago(**span: float) -> datetime:
    """The moment ``span`` before the clock's now."""
    return NOW - timedelta(**span)


def receipt_of(result: RawResult) -> dict[str, Any]:
    """The row form of the receipt a call ended in."""
    assert result.receipt is not None
    return result.receipt.to_json()


class Rig:
    """One ``mail`` tool over a fake Gmail, dispatched as the daemon does, with its event log."""

    def __init__(self, tmp_path: Path, gmail: Gmail, *, cap: int = 500) -> None:
        """Wire the tool, a dispatcher, an event log and a clock at 11:00 in Vancouver."""
        self.gmail = gmail
        self.box = Mailbox(gmail.tool, cap=cap)
        (self.tool,) = build_mail_tool(self.box)
        self.world = ToolWorld(tmp_path, tools=(self.tool,), clock=FakeClock(NOW, zone=ZONE))
        self._n = 0

    def run(self, **args: Any) -> RawResult:  # noqa: ANN401 — the model's arguments.
        """One call as the model makes it."""
        self._n += 1
        return self.world.dispatch(tool_request("mail", f"A{self._n}", arguments=args))

    def ok(self, **args: Any) -> dict[str, Any]:  # noqa: ANN401 — the model's arguments.
        """The answer of a call that worked."""
        result = self.run(**args)
        assert result.error is None, result.tool_output
        assert result.tool_output is not None
        assert len(result.tool_output) < 4400, len(result.tool_output)
        return dict(result.payload)

    def fails(self, **args: Any) -> dict[str, Any]:  # noqa: ANN401 — the model's arguments.
        """The ``{"error", "code", "kind"}`` the model reads for a call that failed."""
        result = self.run(**args)
        assert result.error is not None, result.tool_output
        assert result.receipt is not None
        assert result.receipt.status == "failed"
        assert result.tool_output is not None
        return dict(json.loads(result.tool_output))

    def searches(self) -> list[str]:
        """The queries of every search made so far."""
        return [str(args["query"]) for args in self.gmail.of("gmail_search")]

    def close(self) -> None:
        """Close the event log."""
        self.world.close()


def mailbox() -> Gmail:
    """A mailbox where one old sent message tells which address is his."""
    gmail = Gmail(now=NOW)
    gmail.add("sent0", "Hello", sender=ME, to=SAM, labels=SENT, at=ago(days=40))
    return gmail
