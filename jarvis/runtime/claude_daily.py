"""The Claude daily poller (ADR 0295): reads the nightly letter on his claude.ai chats from Gmail.

Every ``poll_s`` it lists the mail since ``backfill_since`` whose subject holds ``Jarvis Claude``
(drafts, sent and received copies alike), keeps the ones whose subject starts with
:data:`~jarvis.state.import_parsers.DAILY_SUBJECT`, and for each it has not kept yet: reads it
whole, reads its entries into memory.db (ADR 0259, source ``claude_daily``) and keeps the two Gmail
answers raw under ``imports/claude_daily/``. The raw file is written last and named by the Gmail
message id, so a letter is read again until it is fully kept, never twice. The only Gmail tools
used are ``gmail_search`` and ``gmail_get`` (the guard of :func:`job_mail.gmail_read`). No model
is asked; nothing of a letter's content is logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import ToolError
from jarvis.runtime.home import MAIL_SERVER, _letter, mail_text
from jarvis.runtime.job_mail import gmail_read
from jarvis.state import imports
from jarvis.state.import_parsers import claude_daily, is_daily_subject
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.execution.mcp_tools import McpServers
    from jarvis.runtime.plugin_connections import PluginConnections

LOGGER = logging.getLogger(__name__)

SOURCE: Final[str] = "claude_daily"
# Gmail's default search covers drafts, sent and received mail (not spam or trash). The phrase is
# only a net: the subject is checked in code, because Gmail's matching of the Chinese part is not
# something to lean on.
_QUERY: Final[str] = 'subject:"Jarvis Claude" after:{day:%Y/%m/%d}'
_SEARCH_LIMIT: Final[int] = 100
_MAX_PAGES: Final[int] = 30


@dataclass(frozen=True)
class ClaudeDailySettings:
    """The ``claude_daily:`` block of ``config/jarvis.yaml`` once validated."""

    poll_s: float
    backfill_since: date
    max_letters_per_cycle: int


class ClaudeDailyIntake:
    """The poller; built once at boot."""

    def __init__(
        self,
        settings: ClaudeDailySettings,
        connections: PluginConnections,
        db_path: Path,
        imports_dir: Path,
    ) -> None:
        """``db_path`` is memory.db, ``imports_dir`` the raw archive of ADR 0259."""
        self._settings, self._connections = settings, connections
        self._db, self._dir = db_path, imports_dir
        self._warned = False

    async def run(self) -> None:
        """Poll every ``poll_s``, first at once; a failed cycle is logged, never ends the loop."""
        LOGGER.info("claude daily started (every %.0f s)", self._settings.poll_s)
        try:
            while True:
                try:
                    await asyncio.to_thread(self.poll_once)
                except Exception:
                    LOGGER.exception("claude daily: cycle failed; trying again next time")
                await asyncio.sleep(self._settings.poll_s)
        except asyncio.CancelledError:
            LOGGER.info("claude daily cancelled")
            raise

    def poll_once(self) -> int:
        """One cycle on the calling thread; returns how many letters it kept."""
        try:
            servers = self._connections.client_for(MAIL_SERVER)
        except ToolError:
            if not self._warned:
                self._warned = True
                LOGGER.warning("claude daily: the gmail server is not connected; nothing is read")
            return 0
        kept = 0
        # oldest first, so the letter written last is the one added last (imports.latest_daily)
        for message_id in self._unkept(servers)[: self._settings.max_letters_per_cycle]:
            try:
                kept += self._keep(servers, message_id)
            except Exception as exc:  # noqa: BLE001 - one letter must not stop the others
                LOGGER.warning("claude daily: cannot read %s (%s)", message_id, type(exc).__name__)
        return kept

    def _unkept(self, servers: McpServers) -> list[str]:
        """Listed ids with no raw file yet, oldest first."""
        query = _QUERY.format(day=self._settings.backfill_since)
        held = {
            name.partition("-")[2].removesuffix(".json")
            for name in (p.name for p in (self._dir / SOURCE).glob("*-*.json"))
        }
        found: list[str] = []
        page = ""
        for _ in range(_MAX_PAGES):
            args: dict[str, Any] = {"query": query, "maxResults": _SEARCH_LIMIT}
            if page:
                args["pageToken"] = page
            reply = gmail_read(servers, "gmail_search", args)
            found += [str(one["id"]) for one in reply.get("messages") or []]
            page = str(reply.get("nextPageToken") or "")
            if not page:
                break
        return [one for one in reversed(list(dict.fromkeys(found))) if one not in held]

    def _keep(self, servers: McpServers, message_id: str) -> int:
        """Keep one letter and read it; 0 when its subject is not a daily letter's."""
        meta = gmail_read(servers, "gmail_get", {"messageId": message_id, "format": "metadata"})
        head = _letter(meta)
        if not is_daily_subject(head["subject"]):
            return 0
        full = gmail_read(servers, "gmail_get", {"messageId": message_id, "format": "full"})
        data = json.dumps(
            {"message_id": message_id, "metadata": meta, "full": full},
            ensure_ascii=False, sort_keys=True, indent=1,
        ).encode()
        digest = hashlib.sha256(data).hexdigest()
        body = mail_text(str(full.get("body") or ""))
        received = (
            datetime.fromisoformat(head["received"]) if head["received"] else datetime.now(UTC)
        )
        at_ms = int(received.timestamp() * 1000)
        parsed = list(claude_daily(head["subject"], body.encode(), at_ms))
        counts = imports.Counts()
        with contextlib.closing(open_memory_db(self._db)) as conn:
            imports.add_entries(conn, digest, parsed, counts)
            conn.commit()
        imports.keep_raw(self._dir, SOURCE, f"{message_id}.json", data)
        if not parsed[0].fields["parsed"]:
            LOGGER.warning(
                "claude daily: letter %s has no chat sections; kept raw, read as one day entry",
                digest[:16],
            )
        LOGGER.info(
            "claude daily: letter %s kept: %d entries added, %d already held",
            digest[:16], counts.added, counts.held,
        )
        return 1
