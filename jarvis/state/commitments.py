"""What he has to go to, folded from the event log (ADR 0274).

There is no commitments table: a commitment is its last ``commitment.noted`` event, gone once a
``commitment.dropped`` follows, and a sender's standing rule is its last ``commitment.sender_rule``.
Since ADR 0299 his things are the ledger's (:mod:`jarvis.state.things`): this fold is read once,
by the ledger's carry-in, and what stays in use is the senders' rules the mail poller reads and
``schedule`` sets.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.event_log import emit_event, iter_events_of_types

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator, Mapping
    from datetime import tzinfo

TYPES: Final[tuple[str, ...]] = (
    "commitment.noted",
    "commitment.dropped",
    "commitment.sender_rule",
)
ID_PREFIX: Final[str] = "commitment-"
REMINDER_PREFIX: Final[str] = "reminder-cm"
"""Every reminder the commitment poller arms has this id prefix; the readers leave them out."""
SOURCES: Final[tuple[str, ...]] = ("mail", "said")
WHY: Final[tuple[str, ...]] = ("cancelled", "not_his", "he_dropped")
RULES: Final[tuple[str, ...]] = ("always", "never")
DEFAULT_MINUTES: Final[int] = 60
_DAY_MINUTES: Final[int] = 24 * 60
EVIDENCE_CHARS: Final[int] = 200
_WEEK: Final[timedelta] = timedelta(days=7)


@dataclass
class Commitment:
    """One thing he has to go to, as its last ``noted`` says."""

    commitment_id: str
    title: str
    start_at: str
    source: str
    confirmed: bool
    private: bool
    end_at: str = ""
    weekly_until: str = ""
    skip_dates: tuple[str, ...] = ()
    place: str = ""
    link: str = ""
    source_ref: str = ""
    sender_domain: str = ""
    evidence: str = ""
    # a date with no hour (a deadline, a check-in, an all-day event): ``start_at`` is that day's
    # local midnight and it lasts the day
    all_day: bool = False
    dropped: str | None = None

    @property
    def start(self) -> datetime:
        """The first start, aware (``start_at`` carries its UTC offset)."""
        return datetime.fromisoformat(self.start_at)

    @property
    def minutes(self) -> int:
        """How long it lasts: ``end_at`` less ``start_at``, else an hour."""
        if self.all_day:
            return _DAY_MINUTES
        if not self.end_at:
            return DEFAULT_MINUTES
        span = (datetime.fromisoformat(self.end_at) - self.start).total_seconds() / 60
        return int(span) if span > 0 else DEFAULT_MINUTES

    def payload(self) -> dict[str, Any]:
        """The ``commitment.noted`` payload that says this record again."""
        return {
            "commitment_id": self.commitment_id, "title": self.title, "start_at": self.start_at,
            "end_at": self.end_at, "weekly_until": self.weekly_until,
            "skip_dates": list(self.skip_dates), "place": self.place, "link": self.link,
            "source": self.source, "source_ref": self.source_ref,
            "sender_domain": self.sender_domain, "private": self.private,
            "confirmed": self.confirmed, "evidence": self.evidence, "all_day": self.all_day,
        }


@dataclass(frozen=True)
class Occurrence:
    """One time a commitment happens: its start and end in epoch milliseconds, and its day."""

    commitment: Commitment
    start_ms: int
    end_ms: int
    day: str


def new_id() -> str:
    """A fresh ``commitment-<uuid hex>`` id."""
    return ID_PREFIX + uuid.uuid4().hex


def _from_payload(payload: Mapping[str, Any]) -> Commitment:
    return Commitment(
        commitment_id=str(payload["commitment_id"]),
        title=str(payload["title"]),
        start_at=str(payload["start_at"]),
        source=str(payload["source"]),
        confirmed=bool(payload["confirmed"]),
        private=bool(payload["private"]),
        end_at=str(payload.get("end_at") or ""),
        weekly_until=str(payload.get("weekly_until") or ""),
        skip_dates=tuple(str(d) for d in payload.get("skip_dates") or ()),
        place=str(payload.get("place") or ""),
        link=str(payload.get("link") or ""),
        source_ref=str(payload.get("source_ref") or ""),
        sender_domain=str(payload.get("sender_domain") or ""),
        evidence=str(payload.get("evidence") or ""),
        all_day=bool(payload.get("all_day")),
    )


def fold(conn: sqlite3.Connection) -> dict[str, Commitment]:
    """Every commitment ever noted, by id, dropped ones marked with ``dropped`` (the reason)."""
    found: dict[str, Commitment] = {}
    dropped: dict[str, str] = {}
    for event in iter_events_of_types(conn, ("commitment.noted", "commitment.dropped")):
        cid = str(event.payload["commitment_id"])
        if event.type == "commitment.noted":
            found[cid] = _from_payload(event.payload)
        else:
            dropped[cid] = str(event.payload["why"])
    for cid, why in dropped.items():
        if cid in found:
            found[cid].dropped = why
    return found


def live(conn: sqlite3.Connection) -> list[Commitment]:
    """The commitments not dropped, soonest first start first."""
    return sorted(
        (c for c in fold(conn).values() if c.dropped is None), key=lambda c: c.start_at
    )


def sender_rules(conn: sqlite3.Connection) -> dict[str, str]:
    """``domain -> always | never``, the last rule written for each domain."""
    return {
        str(e.payload["domain"]): str(e.payload["rule"])
        for e in iter_events_of_types(conn, ("commitment.sender_rule",))
    }


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _starts(c: Commitment, zone: tzinfo | None) -> Iterator[datetime]:
    """The commitment's start times, in order: one, or every 7 days up to ``weekly_until``."""
    first = c.start if zone is None else c.start.astimezone(zone)
    if not c.weekly_until:
        yield first
        return
    until = date.fromisoformat(c.weekly_until)
    for k in range(10_000):  # a series is weeks long; the bound only stops a corrupt date
        if zone is None:
            at = first + k * _WEEK
        else:  # the same wall-clock time each week, so a clock change does not move it
            wall = datetime.combine(first.date() + k * _WEEK, first.timetz().replace(tzinfo=None))
            at = wall.replace(tzinfo=first.tzinfo)
        if at.date() > until:
            return
        yield at


def occurrences(
    conn: sqlite3.Connection, start_ms: int, end_ms: int, *, zone: tzinfo | None = None,
) -> list[Occurrence]:
    """Every occurrence of a live commitment that overlaps ``[start_ms, end_ms)``, by start.

    A weekly one repeats every 7 days from its first start up to ``weekly_until`` (a date,
    inclusive) less ``skip_dates``. With ``zone`` the repeats keep their wall-clock time in it
    across a clock change and the dates are its dates; without, they keep the start's own offset.
    """
    found: list[Occurrence] = []
    for c in live(conn):
        span = timedelta(minutes=c.minutes)
        for at in _starts(c, zone):
            if _ms(at) >= end_ms:
                break
            day = at.date().isoformat()
            if _ms(at + span) > start_ms and day not in c.skip_dates:
                found.append(Occurrence(c, _ms(at), _ms(at + span), day))
    return sorted(found, key=lambda o: (o.start_ms, o.commitment.commitment_id))


def note(
    conn: sqlite3.Connection, record: Commitment, *, actor: str = "jarvis_runtime",
) -> None:
    """Append one ``commitment.noted`` for ``record``; a record that cannot be read is refused."""
    if record.source not in SOURCES:
        msg = f"commitment source must be one of {SOURCES}, not {record.source!r}"
        raise ValueError(msg)
    record.start  # noqa: B018 - raises on a start that is not an ISO time
    if record.start.tzinfo is None:
        msg = "commitment start_at needs a UTC offset"
        raise ValueError(msg)
    if record.end_at:
        datetime.fromisoformat(record.end_at)
    for text in (record.weekly_until, *record.skip_dates):
        if text:
            date.fromisoformat(text)
    payload = record.payload() | {"evidence": record.evidence[:EVIDENCE_CHARS]}
    emit_event(conn, type="commitment.noted", payload=payload, actor=actor)


def drop(
    conn: sqlite3.Connection, commitment_id: str, why: str, *, actor: str = "jarvis_runtime",
) -> None:
    """Append ``commitment.dropped``; ``why`` is ``cancelled``, ``not_his`` or ``he_dropped``."""
    if why not in WHY:
        msg = f"drop reason must be one of {WHY}, not {why!r}"
        raise ValueError(msg)
    emit_event(
        conn, type="commitment.dropped", payload={"commitment_id": commitment_id, "why": why},
        actor=actor,
    )


def set_sender_rule(
    conn: sqlite3.Connection, domain: str, rule: str, *, actor: str = "jarvis_runtime",
) -> None:
    """Append ``commitment.sender_rule``: later mail from ``domain`` is ``always`` or ``never``."""
    if rule not in RULES:
        msg = f"sender rule must be one of {RULES}, not {rule!r}"
        raise ValueError(msg)
    emit_event(
        conn, type="commitment.sender_rule", payload={"domain": domain.lower(), "rule": rule},
        actor=actor,
    )
