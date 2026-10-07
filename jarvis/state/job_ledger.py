"""L2 job ledger (ADR 0155, 0157, 0160): typed job-hunt mail facts, alerts, feedback, what Jev saw.

Additive tables next to the memory tables (``CREATE TABLE IF NOT EXISTS``, so no schema version
bump). The ledger keeps only the typed facts of a mail (sender name and domain, subject, kind,
company, role, an event sentence and time, never an address or a body); ``job_decision`` alone
keeps, for every scanned mail, the sender address and the body start too (ADR 0162).
``notice_feedback`` and the ``attention_log`` rows of ``card:`` sources are the same record for
every other proactive card (ADR 0160). Every function opens its own short-lived connection, so any
thread may call it.

Layer rules: stdlib + L2 siblings + ``jarvis.shared``; no wiring.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.shared import lang
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from pathlib import Path

KINDS: Final[tuple[str, ...]] = ("offer", "interview", "rejection", "receipt", "job_other", "other")
GLOW: Final[str] = "glow"
LEVELS: Final[tuple[str, ...]] = (GLOW, "card", "card_sound", "speak")
MAX_ERROR_TRIES: Final[int] = 3
# The audit list of held-back mail shows this many, newest first, whatever Jev's probability.
AUDIT_LIMIT: Final[int] = 50
# The message id of a channel-health alert, which belongs to no mail.
HEALTH_ID: Final[str] = "health"
# Allen's "this held-back mail was job mail" (job_feedback.reaction).
FLAG_REACTION: Final[str] = "flag:should_alert"
ALERT_KEEP: Final[timedelta] = timedelta(days=7)
# A pending alert this old was waited on while the client could not show it (a held quiet level).
HELD_AFTER: Final[timedelta] = timedelta(seconds=30)
SOUNDING: Final[frozenset[str]] = frozenset({"card_sound", "speak"})
# Three or more alerts made within this span are one summary card, never a card each, and
# never a spoken line (ADR 0158).
BURST_WINDOW: Final[timedelta] = timedelta(seconds=90)
BURST_SIZE: Final[int] = 3
INTERVIEWING: Final[frozenset[str]] = frozenset({"offer", "interview"})
# Digest order: what Allen wants first.
_RANK: Final[dict[str, int]] = {"offer": 0, "interview": 1, "rejection": 2}
_OTHER_RANK: Final[int] = 3
# An interview time that was not read as a date shows on a summary row only when it is this short.
EVENT_TEXT_MAX: Final[int] = 24
# ADR 0177: an application's status; a mail of these kinds moves it, any other kind leaves it.
APPLICATION_STATUSES: Final[tuple[str, ...]] = (
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "no_reply",
)
_KIND_STATUS: Final[dict[str, str]] = {
    "receipt": "applied",
    "interview": "interviewing",
    "offer": "offer",
    "rejection": "rejected",
}
# An application still ``applied`` whose newest mail is older than this is ``no_reply``.
NO_REPLY_AFTER: Final[timedelta] = timedelta(days=21)
# The tracker's order: offers and interviews first, then waiting, then silent, then rejected.
_STATUS_RANK: Final[dict[str, int]] = {
    "offer": 0,
    "interviewing": 0,
    "applied": 1,
    "no_reply": 2,
    "rejected": 3,
}
_EPOCH: Final[datetime] = datetime.fromtimestamp(0, UTC)

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
CREATE TABLE IF NOT EXISTS job_application (
    id         TEXT PRIMARY KEY,
    company    TEXT,
    role       TEXT,
    applied_at TEXT,
    status     TEXT,
    note       TEXT,
    source     TEXT,
    hidden     INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
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
CREATE TABLE IF NOT EXISTS notice_feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id    TEXT,
    kind         TEXT,
    level_shown  TEXT,
    reaction     TEXT,
    judge        TEXT,
    context_json TEXT,
    at           TEXT
);
CREATE INDEX IF NOT EXISTS notice_feedback_notice ON notice_feedback (notice_id);
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
CREATE TABLE IF NOT EXISTS job_interview_reminder (
    app_id      TEXT PRIMARY KEY,
    event_at    TEXT,
    evening_id  TEXT,
    before_id   TEXT,
    outlook_id  TEXT,
    outlook_sig TEXT,
    cancelled   INTEGER DEFAULT 0
);
"""


# The columns ADR 0162 and 0161 added to ``job_decision``, as appended to a table made before them.
_SNAPSHOT_COLUMNS: Final[tuple[str, ...]] = ("sender_address", "body_status", "moment_json")
BODY_STATUSES: Final[tuple[str, ...]] = ("read", "unavailable", "not_read")


def _add_snapshot_columns(conn: sqlite3.Connection) -> None:
    """Add the ADR 0162 snapshot columns to a ``job_decision`` that lacks them."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(job_decision)")}
    for column in _SNAPSHOT_COLUMNS:
        if column not in have:
            conn.execute(f"ALTER TABLE job_decision ADD COLUMN {column} TEXT")


@contextmanager
def _db(path: Path) -> Iterator[sqlite3.Connection]:
    """A connection with the tables made, committed on a clean exit and always closed."""
    conn = open_memory_db(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        _add_snapshot_columns(conn)
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
    subject) is what the audit list of held-back mail shows; never the body, never an address.
    """
    failed = 1 if verdict == "error" else 0
    given = audit or {}
    with _db(path) as conn:
        conn.execute(
            "INSERT INTO job_seen (message_id, at, verdict, tries, p_job, received_at,"
            " sender_name, sender_domain, subject) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(message_id) DO UPDATE SET at = excluded.at, verdict = excluded.verdict,"
            " tries = job_seen.tries + ?, p_job = coalesce(excluded.p_job, job_seen.p_job),"
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
    """The newest ``AUDIT_LIMIT`` held-back mails, whatever their chance of being job mail."""
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT message_id, received_at, sender_name, sender_domain, subject, p_job"
            " FROM job_seen WHERE verdict = 'not_job' AND subject IS NOT NULL"
            " ORDER BY received_at DESC, message_id LIMIT ?",
            (AUDIT_LIMIT,),
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
    body_status: str = "not_read",
    moment: Mapping[str, Any] | None = None,
) -> None:
    """One snapshot of what Jev was asked and answered at ``header`` or ``body`` (ADR 0157).

    ``head`` is (received_at, name, domain, subject) and may add ``address``, which is kept here
    and nowhere else (ADR 0162). ``body_excerpt`` is the local plain-text body start, ``None``
    when ``body_status`` is ``unavailable`` (a read failed) or ``not_read`` (none was tried).
    ``moment`` is the situation and its 现况 doc at that time (ADR 0161), kept as JSON.
    Appended, never changed.
    """
    if body_status not in BODY_STATUSES:
        msg = f"not a body status: {body_status!r}"
        raise ValueError(msg)
    given = head or {}
    with _db(path) as conn:
        conn.execute(
            "INSERT INTO job_decision (message_id, at, stage, sender_name, sender_domain, subject,"
            " received_at, probabilities, judge, body_excerpt, verdict, sender_address,"
            " body_status, moment_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                given.get("address"),
                body_status,
                None if moment is None else json.dumps(moment, ensure_ascii=False),
            ),
        )


def add_flag(path: Path, message_id: str, now: datetime) -> None:
    """Record Allen's "this was job mail" for a mail, on its latest logged decision too.

    A mail the repair pass hid as not job mail comes back to the ledger (ADR 0158).
    """
    with _db(path) as conn:
        conn.execute("UPDATE job_mail SET deleted = 0 WHERE message_id = ?", (message_id,))
        conn.execute(
            "INSERT INTO job_feedback (alert_id, level_shown, reaction, at) VALUES (?, '', ?, ?)",
            (message_id, FLAG_REACTION, _stamp(now)),
        )
        _touch_event(conn, message_id, feedback=("", FLAG_REACTION, _stamp(now)))


def is_flagged(path: Path, message_id: str) -> bool:
    """Whether Allen already flagged this mail as job mail."""
    with _db(path) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM job_feedback WHERE alert_id = ? AND reaction = ?",
                (message_id, FLAG_REACTION),
            ).fetchone()
            is not None
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


_EDITABLE: Final[tuple[str, ...]] = ("company", "role", "kind", "deleted")


def company_sites(path: Path) -> list[tuple[str, str]]:
    """(company, sender host) of every visible ledger mail, for finding a company's own sites."""
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT company, sender_domain FROM job_mail"
            " WHERE deleted = 0 AND coalesce(company, '') != ''",
        ).fetchall()
    return [(row["company"], row["sender_domain"] or "") for row in rows]


def mail_rows(path: Path) -> list[dict[str, Any]]:
    """Every ledger row's stored facts, hidden ones too, for the repair pass (ADR 0158)."""
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT message_id, received_at, sender_name, sender_domain, subject, kind, company,"
            " role, deleted, event_at, event_text FROM job_mail ORDER BY received_at, message_id",
        ).fetchall()
    return [dict(row) for row in rows]


def body_excerpt(path: Path, message_id: str) -> str:
    """The newest local body start kept for a mail in its decision snapshots, or ''."""
    with _db(path) as conn:
        row = conn.execute(
            "SELECT body_excerpt FROM job_decision"
            " WHERE message_id = ? AND body_excerpt IS NOT NULL ORDER BY id DESC LIMIT 1",
            (message_id,),
        ).fetchone()
    return row["body_excerpt"] if row else ""


def bodyless_mails(path: Path, kinds: Sequence[str], limit: int) -> list[dict[str, Any]]:
    """The newest visible mails of these kinds with no kept body, at most ``limit`` (ADR 0184)."""
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT message_id, received_at, sender_name, sender_domain, subject, event_at,"
            " event_text FROM job_mail m"
            " WHERE deleted = 0 AND kind IN (SELECT value FROM json_each(?))"
            " AND NOT EXISTS (SELECT 1 FROM job_decision d"
            " WHERE d.message_id = m.message_id AND d.body_excerpt IS NOT NULL)"
            " ORDER BY received_at DESC, message_id LIMIT ?",
            (json.dumps(list(kinds)), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def set_event(path: Path, message_id: str, event_at: str | None, event_text: str | None) -> None:
    """Write the event time and sentence read from a mail's body again (ADR 0184)."""
    with _db(path) as conn:
        conn.execute(
            "UPDATE job_mail SET event_at = ?, event_text = ? WHERE message_id = ?",
            (event_at, event_text, message_id),
        )


def update_mail(path: Path, message_id: str, fields: Mapping[str, str | int]) -> None:
    """Change the typed facts of one ledger row (``company``, ``role``, ``kind``, ``deleted``)."""
    names = [name for name in fields if name in _EDITABLE]
    if len(names) != len(fields):
        msg = f"not editable: {sorted(set(fields) - set(_EDITABLE))}"
        raise ValueError(msg)
    with _db(path) as conn:
        conn.execute(
            f"UPDATE job_mail SET {', '.join(f'{name} = ?' for name in names)}"  # noqa: S608 - fixed names
            " WHERE message_id = ?",
            (*fields.values(), message_id),
        )


def drop_alerts(path: Path, message_id: str) -> int:
    """End a mail's pending alerts as ``done`` (not deleted); returns how many."""
    with _db(path) as conn:
        return conn.execute(
            "UPDATE job_alert SET state = 'done' WHERE message_id = ? AND state = 'pending'",
            (message_id,),
        ).rowcount


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


def _next_event(mails: Sequence[sqlite3.Row], now: datetime) -> str | None:
    """The earliest event time among a company's mails that is still ahead, or None."""
    upcoming = sorted(
        (m["event_at"] for m in mails if (at := _moment(m["event_at"])) and at >= now),
        key=lambda text: _moment(text) or now,
    )
    return upcoming[0] if upcoming else None


def _mail_view(m: sqlite3.Row) -> dict[str, Any]:
    return {
        "message_id": m["message_id"],
        "thread_id": m["thread_id"],
        "kind": m["kind"],
        "received_at": m["received_at"],
        "subject": m["subject"],
        "event_at": m["event_at"],
        "event_text": m["event_text"],
    }


def list_ledger(path: Path, now: datetime) -> list[dict[str, Any]]:
    """One group per company, newest first: latest kind and role, next event, mails newest first."""
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT message_id, thread_id, received_at, subject, kind, company, role, event_at,"
            " event_text"
            " FROM job_mail WHERE deleted = 0 ORDER BY received_at DESC, message_id",
        ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(row["company"]).casefold(), []).append(row)
    ledger = []
    for mails in groups.values():
        newest = mails[0]
        ledger.append(
            {
                "company": newest["company"],
                "role": next((m["role"] for m in mails if m["role"]), ""),
                "kind": newest["kind"],
                "last_at": newest["received_at"],
                "next_event_at": _next_event(mails, now),
                "count": len(mails),
                "mails": [_mail_view(m) for m in mails],
            }
        )
    return sorted(ledger, key=lambda one: one["last_at"] or "", reverse=True)


# --- applications (ADR 0177) ---------------------------------------------------------


def application_id(company: str) -> str:
    """The stable id of a mail-derived application: a hash of its company, case-folded."""
    return hashlib.sha1(company.casefold().encode(), usedforsecurity=False).hexdigest()[:12]


# The fields of ``details(body)`` (ADR 0182) that describe an interview, and those that are links.
_INTERVIEW_FIELDS: Final[tuple[str, ...]] = (
    "mode",
    "platform",
    "join_url",
    "location",
    "interviewers",
)
_LINK_FIELDS: Final[tuple[str, ...]] = ("portal_url", "posting_url")
_STEP_KINDS: Final[dict[str, str]] = {
    "interview": "interview_invite",
    "offer": "offer",
    "rejection": "rejection",
}
_NO_LINKS: Final[dict[str, None]] = dict.fromkeys(_LINK_FIELDS)


def _interview_step(
    mails: Sequence[sqlite3.Row], upcoming: str | None, status: str
) -> dict[str, Any] | None:
    """The interview itself: the next event ahead (not once rejected), else the newest past one.

    ``message_id`` is the mail whose ``event_at`` it is.
    """
    if upcoming and status != "rejected":
        at, future = upcoming, True
    else:
        held = [m["event_at"] for m in mails if m["kind"] == "interview" and _moment(m["event_at"])]
        at, future = max(held, key=lambda text: _moment(text) or _EPOCH, default=None), False
    if not at:
        return None
    mail = next(m for m in reversed(mails) if m["event_at"] == at)
    return {"kind": "interview", "at": at, "future": future, "message_id": mail["message_id"]}


def _timeline(
    applied_at: str | None, mails: Sequence[sqlite3.Row], step: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Applied, then each interview invitation, offer and rejection by date, and the interview.

    Mails in a row of the same kind are one step at the first one's date: an invitation, his
    reply and their confirmation are one "invited", not three. Each step's ``message_id`` is
    the mail it came from (None for a manual applied date).
    """
    later = [
        {
            "kind": _STEP_KINDS[m["kind"]],
            "at": m["received_at"],
            "future": False,
            "message_id": m["message_id"],
        }
        for m in mails
        if m["kind"] in _STEP_KINDS
    ]
    later += [step] if step else []
    later.sort(key=lambda one: _moment(one["at"]) or _EPOCH)
    # Allen's own applied date is no mail's; the mail one is the oldest mail.
    first = mails[0] if mails and applied_at == mails[0]["received_at"] else None
    steps = [
        {
            "kind": "applied",
            "at": applied_at,
            "future": False,
            "message_id": first["message_id"] if first else None,
        }
    ]
    for one in later:
        if one["kind"] != steps[-1]["kind"]:
            steps.append(one)
    return steps


def _interview(
    mails: Sequence[sqlite3.Row],
    read: Mapping[str, Mapping[str, Any]],
    step: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The interview's time and what its newest mail with any detail says, else None."""
    invites = [m for m in reversed(mails) if m["kind"] == "interview"]
    if not invites:
        return None
    said = next(
        (
            got
            for m in invites
            if (got := read.get(m["message_id"]))
            and any(got.get(name) for name in _INTERVIEW_FIELDS)
        ),
        {},
    )
    if not said and step is None:
        return None
    return {
        "at": step["at"] if step else None,
        **{
            name: said.get(name) or ([] if name == "interviewers" else None)
            for name in _INTERVIEW_FIELDS
        },
    }


def _links(mails: Sequence[sqlite3.Row], read: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The newest portal and posting address any mail carries."""
    got = [read.get(m["message_id"]) or {} for m in reversed(mails)]
    return {name: next((one[name] for one in got if one.get(name)), None) for name in _LINK_FIELDS}


def _mail_application(
    company: str,
    mails: list[sqlite3.Row],
    edit: sqlite3.Row | None,
    now: datetime,
    read: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """One application out of its mails (oldest first) and Allen's edit row, if he made one.

    The company shown is the newest mail's that names it (a merged-in ATS mail names its ATS),
    the role the longest one any mail read, the most specific. ``read`` is what ``details`` read
    from each mail's kept body start (ADR 0182), by message id.
    """
    status = "applied"
    for m in mails:
        status = _KIND_STATUS.get(m["kind"], status)
    newest = mails[-1]
    last = _moment(newest["received_at"])
    if status == "applied" and last is not None and now - last > NO_REPLY_AFTER:
        status = "no_reply"
    auto, applied_at, note, hidden = True, mails[0]["received_at"], "", False
    if edit is not None:
        since = _moment(edit["updated_at"])
        # His status stands until a mail that decides one arrives after he set it.
        later = any(
            m["kind"] in _KIND_STATUS and since and (at := _moment(m["received_at"])) and at > since
            for m in mails
        )
        if edit["status"] and not later:
            status, auto = edit["status"], False
        applied_at = edit["applied_at"] or applied_at
        note, hidden = edit["note"] or "", bool(edit["hidden"])
    upcoming = _next_event(mails, now)
    step = _interview_step(mails, upcoming, status)
    return {
        "id": application_id(company),
        "company": next(
            m["company"] or ""
            for m in reversed(mails)
            if (m["company"] or "").casefold() == company
        ),
        "role": max((m["role"] for m in reversed(mails) if m["role"]), key=len, default=""),
        "status": status,
        "status_auto": auto,
        "applied_at": applied_at,
        "last_at": newest["received_at"],
        "next_event_at": upcoming,
        "count": len(mails),
        "mails": [_mail_view(m) for m in reversed(mails)],
        "timeline": _timeline(applied_at, mails, step),
        "interview": _interview(mails, read, step),
        "links": _links(mails, read),
        "note": note,
        "source": "mail",
        "hidden": hidden,
    }


def _applications(
    path: Path,
    now: datetime,
    is_ats: Callable[[str], bool] = lambda _: False,
    details: Callable[[str], Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every application, hidden ones too: the visible ledger mails grouped, plus Allen's rows.

    One application per company, case-folded. A mail whose company ``is_ats`` (only an
    applicant-tracking system's name) joins the other company's application with the same role,
    else it stays its own. A mail-derived application has Allen's ``edit`` row of its company (the
    newest, when there are several) override its fields; a ``manual`` row is an application of its
    own. ``details`` reads the interview and links out of a body start (ADR 0182).
    """
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT message_id, thread_id, received_at, subject, kind, company, role, event_at,"
            " event_text"
            " FROM job_mail WHERE deleted = 0",
        ).fetchall()
        mine = conn.execute("SELECT * FROM job_application").fetchall()
        kept = (
            conn.execute(
                "SELECT d.message_id, d.body_excerpt FROM job_decision d JOIN job_mail m"
                " ON m.message_id = d.message_id"
                " WHERE m.deleted = 0 AND d.body_excerpt IS NOT NULL ORDER BY d.id",
            ).fetchall()
            if details
            else []
        )
    # The newest kept body of each mail is the last row of its id.
    read = {row["message_id"]: details(row["body_excerpt"]) for row in kept if details}
    edits: dict[str, sqlite3.Row] = {}
    for row in sorted(
        (r for r in mine if r["source"] == "edit"), key=lambda r: r["updated_at"] or ""
    ):
        edits[(row["company"] or "").casefold()] = row  # the newest edit of a company is last
    by_company: dict[str, list[sqlite3.Row]] = {}
    for m in rows:
        by_company.setdefault((m["company"] or "").casefold(), []).append(m)
    roles = {
        k: {r for g in group if (r := (g["role"] or "").casefold())}
        for k, group in by_company.items()
        if k and not is_ats(k)
    }
    for key in [k for k in by_company if k and is_ats(k)]:
        for m in by_company[key][:]:
            role = (m["role"] or "").casefold()
            home = next((k for k, held in roles.items() if role and role in held), None)
            if home:
                by_company[key].remove(m)
                by_company[home].append(m)
    found = []
    for company, mails in by_company.items():
        if mails:
            mails.sort(key=lambda m: (_moment(m["received_at"]) or _EPOCH, m["message_id"]))
            found.append(_mail_application(company, mails, edits.get(company), now, read))
    found.extend(
        {
            "id": row["id"],
            "company": row["company"],
            "role": row["role"] or "",
            "status": row["status"],
            "status_auto": False,
            "applied_at": row["applied_at"],
            "last_at": row["updated_at"],
            "next_event_at": None,
            "count": 0,
            "mails": [],
            "timeline": [
                {
                    "kind": "applied",
                    "at": row["applied_at"],
                    "future": False,
                    "message_id": None,
                }
            ],
            "interview": None,
            "links": dict(_NO_LINKS),
            "note": row["note"] or "",
            "source": "manual",
            "hidden": bool(row["hidden"]),
        }
        for row in mine
        if row["source"] == "manual"
    )
    return found


def list_applications(
    path: Path,
    now: datetime,
    is_ats: Callable[[str], bool] = lambda _: False,
    details: Callable[[str], Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The tracker's rows (ADR 0177): offers and interviews first, then applied, no reply, rejected.

    Newest activity first within a status. Hidden applications are left out.
    """
    shown = [app for app in _applications(path, now, is_ats, details) if not app.pop("hidden")]
    return sorted(
        shown,
        key=lambda a: (
            _STATUS_RANK[a["status"]],
            -(_moment(a["last_at"]) or _EPOCH).timestamp(),
        ),
    )


def _check_application(status: str | None, applied_at: str | None) -> None:
    if status is not None and status not in APPLICATION_STATUSES:
        msg = f"not a status: {status!r}"
        raise ValueError(msg)
    if applied_at:
        try:
            date.fromisoformat(applied_at)
        except ValueError:
            msg = f"applied_at must be YYYY-MM-DD: {applied_at!r}"
            raise ValueError(msg) from None


def add_application(  # noqa: PLR0913 - the row's fields
    path: Path,
    now: datetime,
    *,
    company: str,
    role: str = "",
    applied_at: str | None = None,
    status: str | None = None,
    note: str = "",
) -> str:
    """Allen's own application, one with no mail; returns its id. A bad value is a ValueError."""
    company = company.strip()
    if not company:
        msg = "company is required"
        raise ValueError(msg)
    _check_application(status, applied_at)
    app_id = uuid.uuid4().hex[:12]
    stamp = _stamp(now)
    with _db(path) as conn:
        conn.execute(
            "INSERT INTO job_application (id, company, role, applied_at, status, note, source,"
            " hidden, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'manual', 0, ?, ?)",
            (
                app_id,
                company,
                role.strip(),
                applied_at or now.astimezone().date().isoformat(),
                status or "applied",
                note,
                stamp,
                stamp,
            ),
        )
    return app_id


_APPLICATION_EDITABLE: Final[tuple[str, ...]] = ("status", "applied_at", "note", "hidden")


def edit_application(
    path: Path,
    now: datetime,
    app_id: str,
    fields: Mapping[str, Any],
    is_ats: Callable[[str], bool] = lambda _: False,
) -> None:
    """Change ``status``, ``applied_at``, ``note`` or ``hidden`` of an application.

    A mail-derived application gets an ``edit`` row (keyed by its id) on its first edit. On an
    edit row ``updated_at`` is when the status was last set by hand, so only a status edit moves
    it. An unknown id is a LookupError, a bad value a ValueError.
    """
    given = {name: fields[name] for name in _APPLICATION_EDITABLE if name in fields}
    _check_application(given.get("status"), given.get("applied_at"))
    with _db(path) as conn:
        row = conn.execute("SELECT source FROM job_application WHERE id = ?", (app_id,)).fetchone()
    base = None
    if row is None:
        base = next((a for a in _applications(path, now, is_ats) if a["id"] == app_id), None)
        if base is None:
            msg = f"no such application: {app_id}"
            raise LookupError(msg)
    stamp = _stamp(now)
    sets = {name: (int(bool(v)) if name == "hidden" else v) for name, v in given.items()}
    if (row is not None and row["source"] == "manual") or "status" in sets:
        sets["updated_at"] = stamp
    with _db(path) as conn:
        if base is not None:
            conn.execute(
                "INSERT INTO job_application (id, company, role, source, hidden, created_at,"
                " updated_at) VALUES (?, ?, ?, 'edit', 0, ?, ?)",
                (app_id, base["company"], base["role"], stamp, stamp),
            )
        if sets:
            conn.execute(
                f"UPDATE job_application SET {', '.join(f'{name} = ?' for name in sets)}"  # noqa: S608 - fixed names
                " WHERE id = ?",
                (*sets.values(), app_id),
            )


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


def has_alert(path: Path, message_id: str) -> bool:
    """Whether any alert, in any state, was made for ``message_id`` (a mail id or a card key)."""
    with _db(path) as conn:
        return conn.execute(
            "SELECT 1 FROM job_alert WHERE message_id = ? LIMIT 1", (message_id,)
        ).fetchone() is not None


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
    """The level the client is given at this quiet level: quiet takes the sound off.

    A glow has no sound to take off and stays a glow at every level.
    """
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


def recent_alerts(path: Path, now: datetime) -> int:
    """How many mail alerts were made within ``BURST_WINDOW`` before ``now``; a glow is no card."""
    with _db(path) as conn:
        (count,) = conn.execute(
            "SELECT count(*) FROM job_alert WHERE message_id != ? AND level != ?"
            " AND created_at >= ?",
            (HEALTH_ID, GLOW, _stamp(now - BURST_WINDOW)),
        ).fetchone()
    return int(count)


def _summarised(rows: list[sqlite3.Row], now: datetime) -> list[sqlite3.Row]:
    """The mail alerts that are one summary: all of them when two or more waited, else bursts.

    A burst is any ``BURST_SIZE`` alerts made within ``BURST_WINDOW`` of each other. ``rows``
    are oldest first.
    """
    if sum(row["created_at"] <= _stamp(now - HELD_AFTER) for row in rows) >= 2:  # noqa: PLR2004 - two or more waited
        return rows
    at = [_moment(row["created_at"]) or now for row in rows]
    marked = {
        j
        for i in range(len(rows) - BURST_SIZE + 1)
        if at[i + BURST_SIZE - 1] - at[i] <= BURST_WINDOW
        for j in range(i, i + BURST_SIZE)
    }
    return [rows[i] for i in sorted(marked)]


def _rank(row: sqlite3.Row) -> int:
    return _RANK.get(row["kind"], _OTHER_RANK)


def _group_item(group: list[sqlite3.Row], quiet: str) -> dict[str, Any]:
    """One summary row for the alerts of one company and thread (``group`` is best kind first).

    Kind and title are the most important mail's; the role is the longest one stored in the
    group (the most specific); ``count`` is the mails; ``at`` the latest; the event is the
    latest mail's time, else its short sentence, else none.
    """
    item = _notice(group[0], quiet)
    latest = sorted(group, key=lambda row: row["created_at"], reverse=True)
    sounding = [row for row in group if shown_level(row["level"], quiet) in SOUNDING]
    dated = next((row for row in latest if row["event_at"]), None)
    said = next((row for row in latest if 0 < len(_short(row)) <= EVENT_TEXT_MAX), None)
    return {
        **item,
        "level": shown_level(sounding[0]["level"], quiet) if sounding else item["level"],
        "role": max((row["role"] for row in group), key=len),
        "at": latest[0]["created_at"],
        "event_at": dated["event_at"] if dated else None,
        "event_text": None if dated or not said else _short(said),
        "count": len(group),
    }


def _short(row: sqlite3.Row) -> str:
    return str(row["event_text"] or "").strip()


def _summary(rows: list[sqlite3.Row], quiet: str) -> dict[str, Any]:
    """The one summary notice for these mail alerts, grouped by company and thread."""
    rows = sorted(rows, key=_rank)
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        thread = row["thread_id"] or row["message_id"]
        groups.setdefault((row["company"].casefold(), thread), []).append(row)
    items = [_group_item(group, quiet) for group in groups.values()]
    kinds = {row["kind"] for row in rows}
    companies = {row["company"].casefold() for row in rows}
    interviews = sum(row["kind"] in INTERVIEWING for row in rows)
    if len(kinds) == 1 and len(companies) == 1 and rows[0]["company"]:
        kind = rows[0]["kind"] if rows[0]["kind"] in KINDS else "job_other"
        title = lang.t(
            "job.digest.title_company",
            company=rows[0]["company"],
            kind=lang.t(f"job.digest.kind.{kind}"),
            n=len(rows),
        )
    elif interviews:
        title = lang.t("job.digest.title_interviews", n=len(rows), x=interviews)
    else:
        title = lang.t("job.digest.title", n=len(rows))
    sound = any(item["level"] in SOUNDING for item in items)
    return {
        "id": "digest-" + "-".join(row["id"] for row in rows),
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
        "link": "jobs",
        "items": items,
    }


def alerts_for_client(
    path: Path, quiet: str, now: datetime, *, marks_only: bool = False
) -> list[dict[str, Any]]:
    """The pending alerts a client may show at this quiet level (ADR 0153, 0158), as notices.

    ``dnd`` returns nothing and leaves every alert pending. ``no-pop`` returns only the glows, and
    so does ``marks_only`` (the moment hold, ADR 0163): a glow is a mark, not a card, so only
    ``dnd`` keeps it back, and the cards it leaves pending come back when the hold ends. ``quiet``
    returns the cards with the sound taken off. Card mail alerts are one summary notice when two
    or more waited or three came within ``BURST_WINDOW`` (the summary never speaks and links to
    the ledger; its rows are one per company and thread, ADR 0159); a glow is never in a summary,
    and other alerts are their own notices. Alerts older than seven days, and those of a mail
    Allen deleted, are not returned.
    """
    if quiet == "dnd":
        return []
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT a.id, a.message_id, a.level, a.title, a.line, a.created_at,"
            " coalesce(m.company, '') AS company, coalesce(m.role, '') AS role, m.event_at,"
            " m.event_text, m.thread_id, coalesce(m.kind, 'health') AS kind"
            " FROM job_alert a LEFT JOIN job_mail m ON m.message_id = a.message_id"
            " WHERE a.state = 'pending' AND coalesce(m.deleted, 0) = 0 AND a.created_at >= ?"
            " ORDER BY a.created_at, a.id",
            (_stamp(now - ALERT_KEEP),),
        ).fetchall()
    if marks_only or quiet == "no-pop":
        rows = [row for row in rows if row["level"] == GLOW]
    mails = [row for row in rows if row["kind"] != "health" and row["level"] != GLOW]
    merged = _summarised(mails, now)
    ids = {row["id"] for row in merged}
    notices = [_notice(row, quiet) for row in rows if row["id"] not in ids]
    return [*notices, _summary(merged, quiet)] if merged else notices


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


def resolve_health(path: Path) -> int:
    """Mark the pending channel-health alert done (a recovery); a shown one is left alone."""
    with _db(path) as conn:
        return conn.execute(
            "UPDATE job_alert SET state = 'done' WHERE message_id = ? AND state = 'pending'",
            (HEALTH_ID,),
        ).rowcount


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


# --- the other proactive cards (ADR 0160): a snapshot when shown, one row per reaction ---

CARD_SOURCE: Final[str] = "card:"
# A card shown and left without any reaction for this long ends as ``ignored``.
CARD_IGNORED_AFTER: Final[timedelta] = timedelta(minutes=30)


def card_snapshot(path: Path, card_id: str) -> dict[str, Any] | None:
    """The attention-log row of a card the client showed, or None when none was logged."""
    with _db(path) as conn:
        row = conn.execute(
            "SELECT * FROM attention_log WHERE event_id = ? AND source LIKE ?"
            " ORDER BY at DESC, rowid DESC LIMIT 1",
            (card_id, f"{CARD_SOURCE}%"),
        ).fetchone()
    return None if row is None else dict(row)


def snapshot_card(  # noqa: PLR0913 - the row's fields
    path: Path,
    *,
    kind: str,
    card_id: str,
    pack_json: str,
    judge_id: str,
    judge_version: str,
    level: str,
    reason: str,
    now: datetime,
) -> bool:
    """Log the pack of a card as it was shown; False (nothing written) when it already is.

    Cards shown and left without a reaction for ``CARD_IGNORED_AFTER`` end as ``ignored`` here, at
    the next card, so a card nobody answers still lands in the feedback table.
    """
    expire_cards(path, now)
    if card_snapshot(path, card_id) is not None:
        return False
    log_decision(
        path,
        source=f"{CARD_SOURCE}{kind}",
        event_id=card_id,
        pack_json=pack_json,
        judge_id=judge_id,
        judge_version=judge_version,
        level=level,
        reason=reason,
        now=now,
    )
    note_delivery(path, card_id, "shown", now)
    return True


def _card_reaction(
    conn: sqlite3.Connection, row: Mapping[str, Any], reaction: str, now: datetime
) -> None:
    conn.execute(
        "INSERT INTO notice_feedback (notice_id, kind, level_shown, reaction, judge, context_json,"
        " at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["event_id"],
            str(row["source"]).removeprefix(CARD_SOURCE),
            row["level"],
            reaction,
            f"{row['judge_id']}/{row['judge_version']}",
            row["pack_json"],
            _stamp(now),
        ),
    )
    _touch_event(conn, row["event_id"], feedback=(row["level"], reaction, _stamp(now)))


def add_card_feedback(path: Path, card_id: str, reaction: str, now: datetime) -> bool:
    """One reaction to a shown card; False when no snapshot of that card exists."""
    with _db(path) as conn:
        row = conn.execute(
            "SELECT * FROM attention_log WHERE event_id = ? AND source LIKE ?"
            " ORDER BY at DESC, rowid DESC LIMIT 1",
            (card_id, f"{CARD_SOURCE}%"),
        ).fetchone()
        if row is None:
            return False
        _card_reaction(conn, row, reaction, now)
    return True


def expire_cards(path: Path, now: datetime, after: timedelta = CARD_IGNORED_AFTER) -> int:
    """Cards shown ``after`` ago with no reaction at all end as ``ignored``; returns how many."""
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT * FROM attention_log WHERE source LIKE ? AND at <= ? AND feedback_json = '[]'",
            (f"{CARD_SOURCE}%", _stamp(now - after)),
        ).fetchall()
        for row in rows:
            _card_reaction(conn, row, "ignored", now)
    return len(rows)


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


# --- interview reminders (ADR 0186): what was armed for each application's interview time -------

_INTERVIEW_COLUMNS: Final[tuple[str, ...]] = (
    "event_at",
    "evening_id",
    "before_id",
    "outlook_id",
    "outlook_sig",
    "cancelled",
)


def interview_rows(path: Path) -> dict[str, dict[str, Any]]:
    """Every armed interview by application id: its time, reminder ids, Outlook event id."""
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT * FROM job_interview_reminder WHERE event_at IS NOT NULL"
        ).fetchall()
    return {row["app_id"]: dict(row) for row in rows}


def put_interview(path: Path, app_id: str, row: Mapping[str, Any]) -> None:
    """Keep (replace) the armed state of one application's interview."""
    with _db(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO job_interview_reminder"  # noqa: S608 - fixed names
            f" (app_id, {', '.join(_INTERVIEW_COLUMNS)})"
            f" VALUES (?{', ?' * len(_INTERVIEW_COLUMNS)})",
            (app_id, *(row.get(name) for name in _INTERVIEW_COLUMNS)),
        )


def drop_interview(path: Path, app_id: str) -> None:
    """Forget an application's armed state (a row with no time is none; no row is deleted)."""
    with _db(path) as conn:
        conn.execute(
            "UPDATE job_interview_reminder SET event_at = NULL, evening_id = NULL,"
            " before_id = NULL, outlook_id = NULL, outlook_sig = NULL, cancelled = 0"
            " WHERE app_id = ?",
            (app_id,),
        )
