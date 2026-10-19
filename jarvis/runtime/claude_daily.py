"""The Claude daily poller (ADR 0295): reads the nightly letter on his claude.ai chats from Gmail.

Every ``poll_s`` it lists the mail since ``backfill_since`` whose subject holds ``Jarvis Claude``
(drafts, sent and received copies alike), keeps the ones whose subject starts with
:data:`~jarvis.state.import_parsers.DAILY_SUBJECT`, and for each it has not kept yet: reads it
whole, reads its entries into memory.db (ADR 0259, source ``claude_daily``) and keeps the two Gmail
answers raw under ``imports/claude_daily/``. The raw file is written last and named by the Gmail
message id, so a letter is read again until it is fully kept, never twice. The only Gmail tools
used are ``gmail_search`` and ``gmail_get`` (the guard of :func:`job_mail.gmail_read`). No model
is asked; nothing of a letter's content is logged.

The same intake also reads the day report of his Claude Code work (ADR 0299) that his Mac shipped
to ``imports/claude-code-daily/files/``: no Gmail is involved, so :meth:`read_code_reports` runs
every cycle whether or not Gmail is connected or ``claude_daily`` is enabled.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import ToolError
from jarvis.runtime.home import MAIL_SERVER, _letter, mail_text
from jarvis.runtime.job_mail import gmail_read
from jarvis.state import imports
from jarvis.state.import_parsers import (
    DIALOGUE_SOURCE,
    claude_daily,
    claude_dialogue,
    is_daily_subject,
)
from jarvis.state.memory_db import open_memory_db
from jarvis.state.shipments import CODE_DAY_SOURCE, DIALOGUE_DIRNAME, FILES_DIRNAME

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from jarvis.execution.mcp_tools import McpServers
    from jarvis.runtime.plugin_connections import PluginConnections
    from jarvis.state.import_parsers import Parsed

LOGGER = logging.getLogger(__name__)

SOURCE: Final[str] = "claude_daily"
CODE_SOURCE: Final[str] = "claude_code_daily"
"""The entries' source of the Mac's day report (ADR 0299); its shipped files are under
``imports/claude-code-daily/files/``."""
_CODE_POLL_S: Final[float] = 300.0
# Searched twice: as Gmail searches by default (sent and received copies), and in the drafts, which
# a default search is not relied on to cover. The phrase is only a net: the subject is checked in
# code, because Gmail's matching of the Chinese part is not something to lean on.
_QUERIES: Final[tuple[str, ...]] = (
    'subject:"Jarvis Claude" after:{day:%Y/%m/%d}',
    'subject:"Jarvis Claude" after:{day:%Y/%m/%d} in:drafts',
)
_SEARCH_LIMIT: Final[int] = 100
_MAX_PAGES: Final[int] = 30


@dataclass(frozen=True)
class ClaudeDailySettings:
    """The ``claude_daily:`` block of ``config/jarvis.yaml`` once validated."""

    poll_s: float
    backfill_since: date
    max_letters_per_cycle: int
    gmail: bool = True
    """False when only the Mac's day report is read (``claude_daily.enabled`` is off)."""


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

    @classmethod
    def code_reports_only(
        cls,
        connections: PluginConnections,
        db_path: Path,
        imports_dir: Path,
    ) -> ClaudeDailyIntake:
        """An intake that reads the Mac's day report and never touches Gmail (ADR 0299)."""
        return cls(
            ClaudeDailySettings(_CODE_POLL_S, date.min, 1, gmail=False),
            connections,
            db_path,
            imports_dir,
        )

    async def run(self) -> None:
        """Read the code reports every few minutes and poll Gmail every ``poll_s``, first at once.

        A failed cycle is logged, never ends the loop.
        """
        gmail = self._settings.gmail
        LOGGER.info(
            "claude daily started (gmail: %s, code reports every %.0f s)",
            gmail,
            _CODE_POLL_S,
        )
        last_mail = float("-inf")
        try:
            while True:
                try:
                    await asyncio.to_thread(self.read_code_reports)
                except Exception:
                    LOGGER.exception("claude daily: reading the code reports failed")
                if gmail and time.monotonic() - last_mail >= self._settings.poll_s:
                    last_mail = time.monotonic()
                    try:
                        await asyncio.to_thread(self.poll_once)
                    except Exception:
                        LOGGER.exception("claude daily: cycle failed; trying again next time")
                await asyncio.sleep(min(_CODE_POLL_S, self._settings.poll_s))
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

    def read_code_reports(self) -> int:
        """Read each new or changed file the Mac shipped; how many were read.

        Two kinds (ADR 0299): the day report (``<date>.md``, source ``claude_code_daily``) and the
        day's kept dialogue (``dialogue/<date>.md``, source ``claude_code_dialogue``, never on the
        day line). A file is read once per content: the entries are committed first, then the
        bytes are kept under ``imports/<source>/`` (named by SHA-256), so a crash in between is
        mended next cycle and a revision is never read twice. Oldest first, so the report written
        last is the one added last (:func:`imports.latest_daily`: the newest stands for the day).
        An edit back to an earlier text is not a new revision and does not stand.
        """
        shipped = self._dir / CODE_DAY_SOURCE / FILES_DIRNAME
        return self._read_shipped(
            shipped, CODE_SOURCE, lambda n, d, ms: claude_daily(n, d, ms, CODE_SOURCE)
        ) + (self._read_shipped(shipped / DIALOGUE_DIRNAME, DIALOGUE_SOURCE, claude_dialogue))

    def _read_shipped(
        self,
        folder: Path,
        source: str,
        parse: Callable[[str, bytes, int], Iterator[Parsed]],
    ) -> int:
        read = 0
        for path in sorted(folder.glob("*.md"), key=lambda p: (p.stat().st_mtime_ns, p.name)):
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if any((self._dir / source).glob(f"{digest[:16]}-*")):
                continue
            parsed = list(parse(path.name, data, int(path.stat().st_mtime * 1000)))
            counts = imports.Counts()
            with contextlib.closing(open_memory_db(self._db)) as conn:
                imports.add_entries(conn, digest, parsed, counts)
                conn.commit()
            imports.keep_raw(self._dir, source, path.name, data)
            read += 1
            LOGGER.info(
                "claude daily: %s file %s read: %d entries added, %d already held",
                source,
                digest[:16],
                counts.added,
                counts.held,
            )
        return read

    def _unkept(self, servers: McpServers) -> list[str]:
        """Listed ids with no raw file yet, oldest first."""
        held = {
            name.partition("-")[2].removesuffix(".json")
            for name in (p.name for p in (self._dir / SOURCE).glob("*-*.json"))
        }
        found: list[str] = []
        for query in _QUERIES:
            page = ""
            for _ in range(_MAX_PAGES):
                args: dict[str, Any] = {
                    "query": query.format(day=self._settings.backfill_since),
                    "maxResults": _SEARCH_LIMIT,
                }
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
            ensure_ascii=False,
            sort_keys=True,
            indent=1,
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
            digest[:16],
            counts.added,
            counts.held,
        )
        return 1
