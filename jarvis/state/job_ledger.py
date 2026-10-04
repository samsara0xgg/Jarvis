"""L2 job ledger (ADR 0155, 0157): typed job-hunt mail facts, alerts, feedback, what Jev saw.

Additive tables next to the memory tables (``CREATE TABLE IF NOT EXISTS``, so no schema version
bump). The ledger keeps only the typed facts of a mail (sender name and domain, subject, kind,
company, role, an event sentence and time); ``job_decision`` also keeps the body start Jev was
shown (ADR 0157). Never an address. Every function opens its own short-lived connection, so any
thread may call it.

Layer rules: stdlib + L2 siblings + ``jarvis.shared``; no wiring.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.shared import lang
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

KINDS: Final[tuple[str, ...]] = ("offer", "interview", "rejection", "receipt", "job_other")
LEVELS: Final[tuple[str, ...]] = ("card", "card_sound", "speak")
MAX_ERROR_TRIES: Final[int] = 3
# A held-back mail is shown in the audit list from this probability of being job mail.
AUDIT_MIN: Final[float] = 0.2
AUDIT_LIMIT: Final[int] = 30
# The message id of a channel-health alert, which belongs to no mail.
HEALTH_ID: Final[str] = "health"
ALERT_KEEP: Final[timedelta] = timedelta(days=7)
# A pending alert this old was waited on while the client could not show it (a held quiet level).
HELD_AFTER: Final[timedelta] = timedelta(seconds=30)
SOUNDING: Final[frozenset[str]] = frozenset({"card_sound", "speak"})
# Digest order: what Allen wants first.
_RANK: Final[dict[str, int]] = {"offer": 0, "interview": 1, "rejection": 2}
_OTHER_RANK: Final[int] = 3

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS job_mail (
    message_id    TEXT PRIMARY KEY,
    thread_id     TEXT,
    received_at   TEXT,
    sender_name   TEXT,
    sender_domain TEXT,
    subject       TEXT,
    kind          TEXT,
    company       TEXT,
    role          TEXT,
    event_at      TEXT,
    event_text    TEXT,
    confidence    REAL,
    extracted_by  TEXT,
    created_at    TEXT,
    deleted       INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS job_seen (
    message_id    TEXT PRIMARY KEY,
    at            TEXT,
    verdict       TEXT,
    tries         INTEGER DEFAULT 0,
    p_job         REAL,
    received_at   TEXT,
    sender_name   TEXT,
    sender_domain TEXT,
    subject       TEXT
);
CREATE TABLE IF NOT EXISTS job_alert (
    id         TEXT PRIMARY KEY,
    message_id TEXT,
    level      TEXT,
    title      TEXT,
    line       TEXT,
    state      TEXT,
    created_at TEXT,
    shown_at   TEXT,
    spoken     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS job_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id    TEXT,
    level_shown TEXT,
    reaction    TEXT,
    at          TEXT
);
CREATE TABLE IF NOT EXISTS job_decision (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    TEXT,
    at            TEXT,
    stage         TEXT,
    sender_name   TEXT,
    sender_domain TEXT,
    subject       TEXT,
    received_at   TEXT,
    probabilities TEXT,
    judge         TEXT,
    body_excerpt  TEXT,
    verdict       TEXT
);
CREATE INDEX IF NOT EXISTS job_decision_message ON job_decision (message_id);
CREATE TABLE IF NOT EXISTS attention_log (
    id            TEXT PRIMARY KEY,
    at            TEXT,
    source        TEXT,
    event_id      TEXT,
    pack_json     TEXT,
    judge_id      TEXT,
    judge_version TEXT,
    level         TEXT,
    reason        TEXT,
    delivery      TEXT,
    feedback_json TEXT
);
"""


@contextmanager
def _db(path: Path) -> Iterator[sqlite3.Connection]:
    """A connection with the tables made, committed on a clean exit and always closed."""
    conn = open_memory_db(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


def _stamp(moment: datetime) -> str:
    """UTC ISO text, so the stored times order as strings; callers pass ``datetime.now(UTC)``."""
    return moment.isoformat(timespec="seconds")


def _moment(text: object) -> datetime | None:
    """An ISO time read back; a time with no offset is Allen's local one."""
    try:
        return datetime.fromisoformat(str(text)).astimezone()
    except ValueError:
        return None


def record_seen(  # noqa: PLR0913 - the row's fields
    path: Path,
    message_id: str,
    verdict: str,
    now: datetime,
    *,
    p_job: float | None = None,
    audit: Mapping[str, str] | None = None,
) -> None:
    """Remember a mail was triaged (``not_job``, ``job``, ``error``); an error counts its tries.

    ``p_job`` is Jev's probability that it is job mail. ``audit`` (received_at, name, domain,
    subject) is kept only for a held-back mail that is close enough to job mail to be worth a
    second look; it is never the body and never an address.
    """
    failed = 1 if verdict == "error" else 0
    given = audit or {}
    with _db(path) as conn:
        conn.execute(
            "INSERT INTO job_seen (message_id, at, verdict, tries, p_job, received_at,"
            " sender_name, sender_domain, subject) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(message_id) DO UPDATE SET at = excluded.at, verdict = excluded.verdict,"
            " tries = job_seen.tries + ?, p_job = excluded.p_job,"
            " received_at = excluded.received_at,"
            " sender_name = excluded.sender_name, sender_domain = excluded.sender_domain,"
            " subject = excluded.subject",
            (
                message_id,
                _stamp(now),
                verdict,
                failed,
                p_job,
                given.get("received_at"),
                given.get("name"),
                given.get("domain"),
                given.get("subject"),
                failed,
            ),
        )


def list_skipped(path: Path) -> list[dict[str, Any]]:
    """The newest held-back mails that were at least ``AUDIT_MIN`` likely to be job mail."""
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT message_id, received_at, sender_name, sender_domain, subject, p_job"
            " FROM job_seen WHERE verdict = 'not_job' AND p_job >= ? AND subject IS NOT NULL"
            " ORDER BY received_at DESC, message_id LIMIT ?",
            (AUDIT_MIN, AUDIT_LIMIT),
        ).fetchall()
    return [dict(row) for row in rows]


def record_decision(  # noqa: PLR0913 - the row's fields
    path: Path,
    message_id: str,
    stage: str,
    verdict: str,
    now: datetime,
    *,
    head: Mapping[str, str] | None = None,
    probabilities: Mapping[str, float] | None = None,
    judge: str | None = None,
    body_excerpt: str | None = None,
) -> None:
    """One snapshot of what Jev was asked and answered at ``header`` or ``body`` (ADR 0157).

    ``head`` is (received_at, name, domain, subject), never an address; ``body_excerpt`` is the
    body start Jev saw, present only for the ``body`` stage. Appended, never changed.
    """
    given = head or {}
    with _db(path) as conn:
        conn.execute(
            "INSERT INTO job_decision (message_id, at, stage, sender_name, sender_domain, subject,"
            " received_at, probabilities, judge, body_excerpt, verdict)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                _stamp(now),
                stage,
                given.get("name"),
                given.get("domain"),
                given.get("subject"),
                given.get("received_at"),
                None if probabilities is None else json.dumps(probabilities),
                judge,
                body_excerpt,
                verdict,
            ),
        )


def seen_ids(path: Path) -> set[str]:
    """Ids not to triage again: decided ones, and errors retried ``MAX_ERROR_TRIES`` times."""
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT message_id FROM job_seen WHERE verdict != 'error' OR tries >= ?",
            (MAX_ERROR_TRIES,),
        )
        return {row[0] for row in rows}


def upsert_mail(path: Path, mail: dict[str, Any], now: datetime) -> None:
    """Write one typed mail; a mail Allen deleted from the ledger stays deleted."""
    columns = (
        "message_id",
        "thread_id",
        "received_at",
        "sender_name",
        "sender_domain",
        "subject",
        "kind",
        "company",
        "role",
        "event_at",
        "event_text",
        "confidence",
        "extracted_by",
    )
    values = [mail.get(name) for name in columns]
    with _db(path) as conn:
        conn.execute(
            f"INSERT INTO job_mail ({', '.join(columns)}, created_at, deleted)"  # noqa: S608 - fixed names
            f" VALUES ({', '.join('?' * len(columns))}, ?, 0)"
            " ON CONFLICT(message_id) DO UPDATE SET "
            + ", ".join(f"{name} = excluded.{name}" for name in columns[1:]),
            (*values, _stamp(now)),
        )


def delete_mail(path: Path, message_id: str) -> bool:
    """Hide a mail from the ledger and its alerts; False when the id is unknown."""
    with _db(path) as conn:
        return (
            conn.execute(
                "UPDATE job_mail SET deleted = 1 WHERE message_id = ?",
                (message_id,),
            ).rowcount
            > 0
        )


def list_ledger(path: Path, now: datetime) -> list[dict[str, Any]]:
    """One group per company, newest first: latest kind and role, next event, mails newest first."""
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT message_id, received_at, subject, kind, company, role, event_at, event_text"
            " FROM job_mail WHERE deleted = 0 ORDER BY received_at DESC, message_id",
        ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(row["company"]).casefold(), []).append(row)
    ledger = []
    for mails in groups.values():
        newest = mails[0]
        upcoming = sorted(
            (m["event_at"] for m in mails if (at := _moment(m["event_at"])) and at >= now),
            key=lambda text: _moment(text) or now,
        )
        ledger.append(
            {
                "company": newest["company"],
                "role": next((m["role"] for m in mails if m["role"]), ""),
                "kind": newest["kind"],
                "last_at": newest["received_at"],
                "next_event_at": upcoming[0] if upcoming else None,
                "count": len(mails),
                "mails": [
                    {
                        "message_id": m["message_id"],
                        "kind": m["kind"],
                        "received_at": m["received_at"],
                        "subject": m["subject"],
                        "event_at": m["event_at"],
                        "event_text": m["event_text"],
                    }
                    for m in mails
                ],
            }
        )
    return sorted(ledger, key=lambda one: one["last_at"] or "", reverse=True)


def create_alert(  # noqa: PLR0913 - the row's fields
    path: Path,
    message_id: str,
    level: str,
    title: str,
    line: str,
    now: datetime,
) -> str:
    """A pending alert for a mail; returns its id."""
    alert_id = uuid.uuid4().hex[:12]
    with _db(path) as conn:
        conn.execute(
            "INSERT INTO job_alert (id, message_id, level, title, line, state, created_at, spoken)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, 0)",
            (alert_id, message_id, level, title, line, _stamp(now)),
        )
    return alert_id


def mark_spoken(path: Path, alert_id: str) -> None:
    """Note that the one spoken line for this alert was said."""
    with _db(path) as conn:
        conn.execute("UPDATE job_alert SET spoken = 1 WHERE id = ?", (alert_id,))


def get_alert(path: Path, alert_id: str) -> dict[str, Any] | None:
    """One alert row, or None."""
    with _db(path) as conn:
        row = conn.execute("SELECT * FROM job_alert WHERE id = ?", (alert_id,)).fetchone()
    return None if row is None else dict(row)


def mark_alert(path: Path, alert_id: str, state: str, now: datetime) -> bool:
    """Move an alert to ``shown`` (stamping when) or ``done``; False when the id is unknown."""
    with _db(path) as conn:
        if state == "shown":
            sql, args = (
                "UPDATE job_alert SET state = 'shown', shown_at = ? WHERE id = ?",
                (_stamp(now), alert_id),
            )
            _touch(conn, alert_id, delivery=("shown", _stamp(now)))
        else:
            sql, args = "UPDATE job_alert SET state = ? WHERE id = ?", (state, alert_id)
        return conn.execute(sql, args).rowcount > 0


def add_feedback(path: Path, alert_id: str, level_shown: str, reaction: str, now: datetime) -> None:
    """One reaction of Allen's to an alert: right, level:<name>, dismissed or ignored."""
    with _db(path) as conn:
        conn.execute(
            "INSERT INTO job_feedback (alert_id, level_shown, reaction, at) VALUES (?, ?, ?, ?)",
            (alert_id, level_shown, reaction, _stamp(now)),
        )
        _touch(conn, alert_id, feedback=(level_shown, reaction, _stamp(now)))


def expire_shown(path: Path, now: datetime, after: timedelta) -> int:
    """Alerts shown and left untouched for ``after`` end as ``ignored``; returns how many."""
    cut = _stamp(now - after)
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT id, level FROM job_alert WHERE state = 'shown' AND shown_at <= ?",
            (cut,),
        ).fetchall()
        for row in rows:
            conn.execute("UPDATE job_alert SET state = 'done' WHERE id = ?", (row["id"],))
            conn.execute(
                "INSERT INTO job_feedback (alert_id, level_shown, reaction, at)"
                " VALUES (?, ?, 'ignored', ?)",
                (row["id"], row["level"], _stamp(now)),
            )
            _touch(conn, row["id"], feedback=(row["level"], "ignored", _stamp(now)))
    return len(rows)


def shown_level(level: str, quiet: str) -> str:
    """The level the client is given at this quiet level: quiet takes the sound off."""
    return "card" if quiet == "quiet" and level in SOUNDING else level


def _notice(row: sqlite3.Row, quiet: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": "mail",
        "title": row["title"],
        "line": row["line"],
        "level": shown_level(row["level"], quiet),
        "text": f"{row['title']} {row['line']}",
        "at": row["created_at"],
        "company": row["company"],
        "role": row["role"],
        "event_at": row["event_at"],
        "mail_kind": row["kind"],
    }


def alerts_for_client(path: Path, quiet: str, now: datetime) -> list[dict[str, Any]]:
    """The pending alerts a client may show at this quiet level (ADR 0153), as notices.

    ``no-pop`` and ``dnd`` return nothing and leave every alert pending. ``quiet`` returns the
    cards with the sound taken off. Back at ``off``, two or more alerts that waited are one digest
    (offers and interviews first); otherwise each alert is its own notice. Alerts older than
    seven days, and those of a mail Allen deleted, are not returned.
    """
    if quiet in ("no-pop", "dnd"):
        return []
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT a.id, a.level, a.title, a.line, a.created_at,"
            " coalesce(m.company, '') AS company, coalesce(m.role, '') AS role, m.event_at,"
            " coalesce(m.kind, 'health') AS kind"
            " FROM job_alert a LEFT JOIN job_mail m ON m.message_id = a.message_id"
            " WHERE a.state = 'pending' AND coalesce(m.deleted, 0) = 0 AND a.created_at >= ?"
            " ORDER BY a.created_at, a.id",
            (_stamp(now - ALERT_KEEP),),
        ).fetchall()
    held = [row for row in rows if row["created_at"] <= _stamp(now - HELD_AFTER)]
    if quiet != "off" or len(held) < 2:  # noqa: PLR2004 - a digest is two or more
        return [_notice(row, quiet) for row in rows]
    items = [
        _notice(row, quiet) for row in sorted(rows, key=lambda r: _RANK.get(r["kind"], _OTHER_RANK))
    ]
    title = lang.t("job.digest.title", n=len(items))
    sound = any(item["level"] in SOUNDING for item in items)
    return [
        {
            "id": "digest-" + "-".join(item["id"] for item in items),
            "kind": "digest",
            "title": title,
            "line": items[0]["title"],
            "level": "card_sound" if sound else "card",
            "text": f"{title} {items[0]['title']}",
            "at": max(item["at"] for item in items),
            "company": "",
            "role": "",
            "event_at": None,
            "mail_kind": items[0]["mail_kind"],
            "items": items,
        }
    ]


def alert_ids(notice_id: str) -> Sequence[str]:
    """The alert ids a notice id stands for: itself, or the members of a digest."""
    if notice_id.startswith("digest-"):
        return notice_id.split("-")[1:]
    return [notice_id]


def last_health_alert(path: Path) -> datetime | None:
    """When the channel-health alert was last raised, or None."""
    with _db(path) as conn:
        (latest,) = conn.execute(
            "SELECT max(created_at) FROM job_alert WHERE message_id = ?",
            (HEALTH_ID,),
        ).fetchone()
    return None if latest is None else _moment(latest)


# --- the attention log (ADR 0155): one row per decision, the pack exactly as the judge saw it ---


def log_decision(  # noqa: PLR0913 - the row's fields
    path: Path,
    *,
    source: str,
    event_id: str,
    pack_json: str,
    judge_id: str,
    judge_version: str,
    level: str,
    reason: str,
    now: datetime,
) -> str:
    """Append one decision; returns its id. Delivery and feedback are filled in as they happen."""
    decision_id = uuid.uuid4().hex[:12]
    with _db(path) as conn:
        conn.execute(
            "INSERT INTO attention_log (id, at, source, event_id, pack_json, judge_id,"
            " judge_version, level, reason, delivery, feedback_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '[]')",
            (
                decision_id,
                _stamp(now),
                source,
                event_id,
                pack_json,
                judge_id,
                judge_version,
                level,
                reason,
            ),
        )
    return decision_id


def note_delivery(path: Path, event_id: str, state: str, now: datetime) -> None:
    """Record what became of the latest decision on an event: a state and when it happened."""
    with _db(path) as conn:
        _touch_event(conn, event_id, delivery=(state, _stamp(now)))


def note_audio(path: Path, event_id: str, device: dict[str, Any]) -> None:
    """Record the output device (name and private flag) a decision that would sound met."""
    audio = {"device": device["name"], "private": device["private"]}
    with _db(path) as conn:
        _touch_event(conn, event_id, audio=audio)


def list_attention(path: Path, source: str | None = None) -> list[dict[str, Any]]:
    """Every logged decision, oldest first, with delivery and feedback read back as JSON."""
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT * FROM attention_log WHERE ? IS NULL OR source = ? ORDER BY at, rowid",
            (source, source),
        ).fetchall()
    return [
        {
            **dict(row),
            "delivery": json.loads(row["delivery"]),
            "feedback": json.loads(row["feedback_json"]),
        }
        for row in rows
    ]


def _touch(
    conn: sqlite3.Connection,
    alert_id: str,
    *,
    delivery: tuple[str, str] | None = None,
    feedback: tuple[str, str, str] | None = None,
) -> None:
    row = conn.execute("SELECT message_id FROM job_alert WHERE id = ?", (alert_id,)).fetchone()
    if row is not None:
        _touch_event(conn, row["message_id"], delivery=delivery, feedback=feedback)


def _touch_event(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    delivery: tuple[str, str] | None = None,
    feedback: tuple[str, str, str] | None = None,
    audio: dict[str, Any] | None = None,
) -> None:
    """Add a delivery state, an output device or a reaction to ``event_id``'s latest decision."""
    row = conn.execute(
        "SELECT id, delivery, feedback_json FROM attention_log WHERE event_id = ?"
        " ORDER BY at DESC, rowid DESC LIMIT 1",
        (event_id,),
    ).fetchone()
    if row is None:
        return
    states, reactions = json.loads(row["delivery"]), json.loads(row["feedback_json"])
    if delivery is not None:
        states[delivery[0]] = delivery[1]
    if audio is not None:
        states["audio"] = audio
    if feedback is not None:
        reactions.append({"level_shown": feedback[0], "reaction": feedback[1], "at": feedback[2]})
    conn.execute(
        "UPDATE attention_log SET delivery = ?, feedback_json = ? WHERE id = ?",
        (json.dumps(states), json.dumps(reactions, ensure_ascii=False), row["id"]),
    )
