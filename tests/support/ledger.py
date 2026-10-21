"""The ledger world: its sources, clock, prose writer and the terminal link a brain reads."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from jarvis.decision.llm import ChatResult
from jarvis.runtime import terminal as terminal_runtime
from jarvis.runtime.ledger import ProseSettings, run_day_prose
from jarvis.shared.device_link import DeviceCallError
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.job_ledger import record_seen, upsert_mail
from jarvis.state.ledger import LedgerSources
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from pathlib import Path

ZONE = timezone(timedelta(hours=-7))


NOW = datetime(2026, 10, 8, 15, 0, tzinfo=ZONE)  # a Thursday


MIDNIGHT = datetime(2026, 10, 8, tzinfo=ZONE)


SPAN = (
    'CREATE TABLE "span" ("id" INTEGER PRIMARY KEY AUTOINCREMENT, "start" DATETIME NOT NULL, '
    '"end" DATETIME NOT NULL, "appBundleID" TEXT NOT NULL, "appName" TEXT NOT NULL, "title" TEXT, '
    '"url" TEXT, "domain" TEXT, "document" TEXT, "deviceID" TEXT, "originID" INTEGER, '
    '"remoteSeq" TEXT, "keySeconds" INTEGER NOT NULL DEFAULT 0)'
)


PROJECT = 'CREATE TABLE "project" ("id" INTEGER PRIMARY KEY, "name" TEXT NOT NULL)'


VERDICT = (
    'CREATE TABLE "jevProjectVerdict" ("appBundleID" TEXT, "domain" TEXT, "title" TEXT, '
    '"document" TEXT, "projectID" INTEGER)'
)


GHOSTTY = "com.mitchellh.ghostty"


CHROME = "com.google.Chrome"


def ledger_at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE)


def utc_text(moment: datetime) -> str:
    """GRDB's text form: UTC, a space, milliseconds, no suffix."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.000")


def _timesink(path: Path) -> None:
    spans = [  # (start, end, bundle, app) over three days
        (ledger_at(6, 9), ledger_at(6, 10, 30), GHOSTTY, "Ghostty"),
        (ledger_at(6, 9, 30), ledger_at(6, 10), CHROME, "Chrome"),  # inside the first: counted once
        (ledger_at(7, 13), ledger_at(7, 13, 45), CHROME, "Chrome"),
        (ledger_at(8, 10), ledger_at(8, 11), GHOSTTY, "Ghostty"),
    ]
    with closing(sqlite3.connect(path)) as conn, conn:
        for ddl in (SPAN, PROJECT, VERDICT):
            conn.execute(ddl)
        conn.execute("INSERT INTO project VALUES (1, 'jarvis')")
        conn.execute(
            "INSERT INTO jevProjectVerdict VALUES (?, '', 'repl', '', 1)",
            (GHOSTTY,),
        )
        conn.executemany(
            "INSERT INTO span (start, end, appBundleID, appName, title, document) "
            "VALUES (?, ?, ?, ?, 'repl', '')",
            [(utc_text(a), utc_text(b), bundle, app) for a, b, bundle, app in spans],
        )


def write_mail(path: Path, mid: str, when: datetime, **fields: str | None) -> None:
    mail = {
        "message_id": mid,
        "received_at": when.isoformat(),
        "sender_name": "Pat Lee",
        "sender_domain": "x.example",
        "subject": "s",
        "confidence": 0.9,
        "extracted_by": "test",
        **fields,
    }
    upsert_mail(path, mail, datetime.now(UTC))


def ledger_world(tmp_path: Path) -> LedgerSources:
    memory, log, ts = tmp_path / "memory.db", tmp_path / "events.db", tmp_path / "ts.db"
    _timesink(ts)
    with closing(open_memory_db(memory)) as conn, conn:
        conn.execute(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, 'allen', 'hi')",
            ("r1", ledger_at(8, 14).isoformat()),
        )
        conn.execute(
            "INSERT INTO day_summaries (id, day, ts, summary, model, record_count, input_chars, "
            "output_chars) VALUES ('s1', '2026-10-07', ?, ?, 'm', 1, 1, 1)",
            (
                ledger_at(8, 5).isoformat(),  # written after midnight
                "## 2026-10-07\n### Topics\n- resume polish [record_id=r1]\n"
                "### Decisions and facts the user stated\n- none\n### Unfinished\n- none",
            ),
        )
        conn.execute(
            "INSERT INTO day_prose (id, day, ts, text, model, input_chars, output_chars) "
            "VALUES ('p1', '2026-10-06', ?, 'He shipped the ledger and went to bed.', 'm', 1, 1)",
            (ledger_at(7, 5).isoformat(),),
        )
    write_mail(memory, "m1", ledger_at(5, 9), kind="receipt", company="Gamma Inc", role="Co-op")
    write_mail(
        memory, "m2", ledger_at(7, 14), kind="receipt", company="Acme Robotics", role="Co-op Dev"
    )
    write_mail(
        memory,
        "m3",
        ledger_at(6, 12),
        kind="interview",
        company="Delta Corp",
        role="Intern",
        event_at=ledger_at(9, 14).isoformat(),
        event_text="Friday 2:00 PM - 2:30 PM PT",
    )
    write_mail(
        memory, "m4", ledger_at(8, 11, 30), kind="interview", company="Beta Labs", role="Co-op"
    )
    record_seen(
        memory,
        "seen1",
        "not_job",
        datetime.now(UTC),
        audit={"received_at": ledger_at(8, 9).isoformat()},
    )
    conn = open_event_log(log)
    emit_event(
        conn,
        type="reminder.scheduled",
        ts_epoch_ms=int(ledger_at(8, 8).timestamp() * 1000),
        payload={
            "reminder_id": "reminder-1",
            "due_at_epoch_ms": int(ledger_at(9, 9).timestamp() * 1000),
            "due_at_local": "2026-10-09T09:00",
            "text": "call the dentist",
            "action_id": "a1",
        },
    )
    conn.close()
    return LedgerSources(memory, log, ts, ZONE)


class ProseWriter:
    """Stands in for the prose preset's client: answers from a script, records what it is asked."""

    provider = "openai"
    model = "fake-prose"

    def __init__(self, script: list[object]) -> None:
        self.script = script
        self.asked: list[str] = []

    def chat(self, *, messages: list[dict[str, Any]], **_kwargs: object) -> ChatResult:
        self.asked.append(str(messages[0]["content"]))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return ChatResult(
            text=str(step),
            tool_calls=(),
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
            raw={},
            model_used=self.model,
        )


def run_ledger(
    src: LedgerSources,
    writer: ProseWriter,
    days: int,
    sleeps: list[float],
) -> dict[str, str]:
    return run_day_prose(
        src,
        ProseSettings("p", "write the day", days, 200),
        writer,  # type: ignore[arg-type]
        event_log_path=src.event_log,
        pricing_table={},
        today=date(2026, 10, 8),
        sleep=sleeps.append,
    )


def ledger_rows(src: LedgerSources) -> dict[str, list[str]]:
    with closing(sqlite3.connect(src.memory_db)) as conn:
        out: dict[str, list[str]] = {}
        for day, text in conn.execute("SELECT day, text FROM day_prose ORDER BY rowid"):
            out.setdefault(day, []).append(text)
        return out


def terminal_link(store: Path | None, asked: list[str], *, up: list[bool]) -> Any:  # noqa: ANN401
    """A brain's device link whose far end is the real terminal reader on ``store``."""

    def link(op: str, arguments: Any, target: str | None) -> dict[str, Any]:  # noqa: ANN401
        del target
        asked.append(str(arguments["fn"]))
        if not up[0]:
            msg = "the Mac is not connected right now, so timesink_read cannot run"
            raise DeviceCallError(msg)
        reply = terminal_runtime._read_device(op, arguments, store, ())  # noqa: SLF001
        if not reply["ok"]:
            raise DeviceCallError(reply["message"], code=reply["code"])
        return dict(reply["output"])

    return link
