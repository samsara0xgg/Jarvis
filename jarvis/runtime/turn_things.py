"""The post-turn pass (ADR 0300): after each record of his is answered, one call reads it.

Every ``POLL_S`` the loop marks the records of his that have no row yet as pending
(:mod:`jarvis.state.turn_things`; memory.db is where every record of his lands, typed, spoken,
live voice and phone alike, so nothing has to be hooked into the turn driver) and reads the
pending ones whose wait is over: her answer is there, or ``ANSWER_WAIT_S`` has passed, and the
notes of its pictures exist, or their window (ADR 0223) has passed. At start and every
``RESCAN_S`` the records of the last day with no row are marked too, and on the first start
(no row at all) those of the last ``backfill_days``. A model call that fails marks its record an
error, tried again after ``RETRY_AFTER_S`` up to three times, and stops the loop for that long: an
outage costs one record one try, not the backlog.

One ``preset`` call per record is given the record, the messages before it, her answer, the notes
of its pictures, the open things, today's plan, what her tools wrote in the turn and his local
time (:func:`jarvis.decision.turn_things.build_prompt`). What passes the checks of
:func:`jarvis.decision.turn_things.parse_answer` is written: ledger actions through
``things.add``, ``move`` and ``close`` or ``confirm`` as the pass, with his quote as evidence and
the record id as ref; stated doings as one ``doings.stated`` set. The row keeps what the model was
given and said. Nothing is written by the model: a ledger write that is refused is dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from jarvis.decision import turn_things as decision
from jarvis.runtime.picture_notes import RETRY_AFTER_S as PICTURE_RETRY_S
from jarvis.runtime.picture_notes import WINDOW_S as PICTURE_WINDOW_S
from jarvis.state import plan, stated_doings, things
from jarvis.state import turn_things as marks
from jarvis.state.event_log import iter_events_of_types, open_runtime_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.shared import Event
    from jarvis.state.turn_things import Turn

LOGGER = logging.getLogger(__name__)

POLL_S: Final[float] = 5.0
RESCAN_S: Final[float] = 600.0
RESCAN_DAYS: Final[int] = 1
ANSWER_WAIT_S: Final[float] = 600.0
"""A record she has not answered is read after this long, with whatever she has said."""
PICTURE_WAIT_S: Final[float] = PICTURE_WINDOW_S + 2 * PICTURE_RETRY_S
"""How long after the turn ends the note of a picture is waited for: the window for his next
words, then the note's own request and its one retry."""
RETRY_AFTER_S: Final[float] = 600.0
MAX_PER_POLL: Final[int] = 20
_CATCH_UP_S: Final[float] = 0.5
_LOG_WAIT_S: Final[float] = 5.0
_WHAT: Final[dict[str, str]] = {
    "thing.added": "added", "thing.changed": "changed", "thing.moved": "moved",
    "thing.reopened": "reopened",
}


@dataclass(frozen=True)
class TurnThingsSettings:
    """The ``turn_things:`` block of ``config/jarvis.yaml`` once validated."""

    backfill_days: int
    preset: str


@dataclass(frozen=True)
class Outcome:
    """What reading one record came to: the row's verdict, what was written and what was dropped."""

    verdict: str
    wrote: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    given: str = ""
    said: str = ""


class ModelFailed(Exception):  # noqa: N818 - the model did not answer; the loop waits
    """The model call raised; the record is marked an error."""


def _zone_name(zone: tzinfo) -> str:
    return zone.key if isinstance(zone, ZoneInfo) else "UTC"


class TurnThings:
    """The post-turn pass; built once at boot."""

    def __init__(
        self,
        settings: TurnThingsSettings,
        db_path: Path,
        event_log: Path,
        ask: Callable[[str, str], str] | None,
        zone: tzinfo,
    ) -> None:
        """``db_path`` is memory.db, ``event_log`` the log; ``ask(system, user)`` is the model.

        With no ``ask`` (the preset is missing or malformed) nothing is read.
        """
        self._settings, self._db, self._event_log = settings, db_path, event_log
        self._ask, self.zone = ask, zone
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._next_row: int | None = None  # None until the first poll has queued the window
        self._rescan_at = datetime.min.replace(tzinfo=UTC)
        self._cool_until = datetime.min.replace(tzinfo=UTC)
        self._warned = False

    async def run(self) -> None:
        """Poll every ``POLL_S``; a failed poll is logged, never ends the loop."""
        LOGGER.info("turn things started (every %.0f s)", POLL_S)
        try:
            while True:
                read = 0
                try:
                    read = await asyncio.to_thread(self.poll_once)
                except Exception:
                    LOGGER.exception("turn things: poll failed; trying again next time")
                await asyncio.sleep(_CATCH_UP_S if read >= MAX_PER_POLL else POLL_S)
        except asyncio.CancelledError:
            LOGGER.info("turn things cancelled")
            raise

    # --- what is owed --------------------------------------------------------------------

    def poll_once(self) -> int:
        """Queue new records and read the ones that are ready; how many were read."""
        if self._ask is None:
            if not self._warned:
                self._warned = True
                LOGGER.warning("turn things: the preset is not usable; nothing is read")
            return 0
        now = self.now()
        self._queue(now)
        if now < self._cool_until:
            return 0
        read = 0
        for record_id in marks.owed(self._db, now - timedelta(seconds=RETRY_AFTER_S)):
            if read >= MAX_PER_POLL:
                break
            try:
                outcome = self.read(record_id, now, when_ready=True)
            except ModelFailed:
                LOGGER.warning("turn things: the model did not answer", exc_info=True)
                self._cool_until = now + timedelta(seconds=RETRY_AFTER_S)
                break
            except Exception:
                LOGGER.exception("turn things: cannot read %s", record_id)
                read += 1
                continue
            if outcome is not None:
                read += 1
        return read

    def _queue(self, now: datetime) -> None:
        """Mark records of his without a row as pending.

        The window is the last ``backfill_days`` on a first start, else the last day, at start and
        at each rescan; between rescans it is the records since the last poll.
        """
        if self._next_row is None:
            days = RESCAN_DAYS if marks.has_marks(self._db) else self._settings.backfill_days
            first = marks.rowid_since(self._db, now - timedelta(days=days))
        elif now >= self._rescan_at:
            first = marks.rowid_since(self._db, now - timedelta(days=RESCAN_DAYS))
        else:
            first = self._next_row
        if self._next_row is None or now >= self._rescan_at:
            self._rescan_at = now + timedelta(seconds=RESCAN_S)
        queued, self._next_row = marks.queue(self._db, now, from_rowid=first)
        if queued:
            LOGGER.info("turn things: %d records of his are owed", queued)

    @staticmethod
    def ready(turn: Turn, now: datetime) -> bool:
        """Whether to read the record now: it is answered, or has waited, and its notes are in."""
        spoken = turn.answered_at
        if spoken is None and (now - turn.at).total_seconds() < ANSWER_WAIT_S:
            return False
        waited = (now - (spoken or turn.at)).total_seconds()
        return len(turn.notes) >= len(turn.pictures) or waited >= PICTURE_WAIT_S

    # --- reading one record --------------------------------------------------------------

    def read(
        self, record_id: str, now: datetime, *, when_ready: bool = False,
    ) -> Outcome | None:
        """Read one record of his: ask, check, write, mark. ``None`` if it is not ready yet.

        ``when_ready`` leaves a record whose answer or picture notes are still awaited alone; a
        replay reads without it. Raises :class:`ModelFailed` when the model did not answer, after
        marking the record an error; an answer that is no JSON object is marked an error too and
        returned as one.
        """
        turn = marks.read(self._db, record_id)
        if turn is None:
            marks.mark(self._db, record_id, "none", now)  # not a record of his
            return Outcome("none")
        if when_ready and not self.ready(turn, now):
            return None
        if not (turn.words or turn.notes):
            marks.mark(self._db, record_id, "none", now)  # nothing in it to read
            return Outcome("none")
        assert self._ask is not None  # noqa: S101 - poll_once returned early without it
        given = said = ""
        with contextlib.closing(self._log()) as conn:
            try:
                folded = things.fold(conn)
                shown = _unique(things.open_things(conn))
                written = self._written(conn, turn, folded, now)
                system, user = decision.build_prompt(
                    turn, now=now, zone=self.zone, zone_name=_zone_name(self.zone),
                    plan=plan.for_day(conn, turn.at.astimezone(self.zone).date()),
                    open_things=shown, written=written,
                )
                given = json.dumps({"user": user}, ensure_ascii=False)
                try:
                    said = self._ask(system, user)
                except Exception as exc:
                    raise ModelFailed from exc
                found = decision.parse_answer(
                    said, turn, zone=self.zone, known={things.short_id(one): one for one in shown},
                )
                wrote, dropped = self._apply(conn, found, turn, written, now)
                stated = stated_doings.write(
                    conn, turn.record_id, decision.EXTRACTOR, int(turn.at.timestamp() * 1000),
                    found.doings, now=now,
                )
            except decision.AnswerError as why:
                LOGGER.warning("turn things: %s: %s", record_id, why)
                self._finish(record_id, "error", now, given, said)
                return Outcome("error", dropped=(str(why),), given=given, said=said)
            except Exception:
                self._finish(record_id, "error", now, given, said)
                raise
        verdict = "written" if wrote or stated else "none"
        self._finish(record_id, verdict, now, given, said)
        LOGGER.info(
            "turn things: %s %s: %d written, %d doings, %d dropped",
            record_id, verdict, len(wrote), len(found.doings) if stated else 0, len(dropped),
        )
        for line in dropped:
            LOGGER.info("turn things: %s dropped %s", record_id, line)
        return Outcome(verdict, tuple(wrote), tuple(dropped), given, said)

    def _finish(self, record_id: str, verdict: str, now: datetime, given: str, said: str) -> None:
        marks.mark(
            self._db, record_id, verdict, now, extractor=decision.EXTRACTOR, input_json=given,
            output_json=said,
        )

    def _written(
        self, conn: sqlite3.Connection, turn: Turn, folded: dict[str, things.Thing],
        now: datetime,
    ) -> list[decision.Written]:
        """What her tool wrote between his record and her answer, and the pass wrote for it."""
        until = int(turn.answered_at.timestamp() * 1000) + 999 if turn.answered_at else (
            int(now.timestamp() * 1000)
        )
        found: list[decision.Written] = []
        for event in iter_events_of_types(
            conn, things.TYPES, since_epoch_ms=int(turn.at.timestamp() * 1000),
        ):
            payload = event.payload
            ours = payload.get("by") == "pass" and payload.get("ref") == turn.record_id
            hers = payload.get("by") == "her" and event.ts_epoch_ms <= until
            if (one := folded.get(str(payload.get("thing_id")))) is not None and (ours or hers):
                found.append(decision.Written(_what(event), one))
        return found

    def _apply(
        self, conn: sqlite3.Connection, found: decision.Read, turn: Turn,
        written: list[decision.Written], now: datetime,
    ) -> tuple[list[str], list[str]]:
        """Write the actions that passed; what the ledger refuses or already has is dropped."""
        wrote: list[str] = []
        dropped = list(found.dropped)
        for action in found.actions:
            try:
                line = self._write(conn, action, turn, written, now)
            except (ValueError, LookupError) as why:
                dropped.append(f"action {action.action} {action.title or action.thing}: {why}")
                continue
            if line is None:
                dropped.append(
                    f"action {action.action} {action.title or action.thing}: already there",
                )
            else:
                wrote.append(line)
        return wrote, dropped

    def _write(
        self, conn: sqlite3.Connection, action: decision.Action, turn: Turn,
        written: list[decision.Written], now: datetime,
    ) -> str | None:
        """One action through the ledger's write path; ``None`` when it was there already."""
        write: dict[str, Any] = {
            "by": "pass", "evidence": action.quote, "ref": turn.record_id, "zone": self.zone,
            "now": now,
        }
        if action.action == "new":
            if decision.made_already(action, written):
                return None
            one, created = things.add(
                conn,
                things.Thing(
                    things.new_id(), action.title, action.kind, start_at=action.start_at,
                    end_at=action.end_at, on_day=action.on_day, due=action.due,
                    weekly_until=action.weekly_until, place=action.place,
                    confirmed=action.sure,
                    source="picture" if action.source == "picture" else "said",
                    source_ref=turn.record_id,
                ),
                **write,
            )
            return f"added {things.short_id(one)} {one.title}" if created else None
        if action.action == "move":
            one = things.move(
                conn, action.thing, start_at=action.start_at, end_at=action.end_at,
                on_day=action.on_day, due=action.due, occurrence=action.occurrence, **write,
            )
            return f"moved {things.short_id(one)} {one.title}"
        if action.action == "confirmed":
            if not things.confirm(conn, action.thing, **write):
                return None
            one = things.get(conn, action.thing)
            return f"confirmed {things.short_id(one)} {one.title}"
        one = things.close(
            conn, action.thing, action.action, occurrence=action.occurrence,
            why=action.quote if action.action == "cancelled" else "", **write,
        )
        return f"{action.action} {things.short_id(one)} {one.title}"

    def _log(self) -> sqlite3.Connection:
        return open_runtime_event_log(self._event_log, deadline=time.monotonic() + _LOG_WAIT_S)


def _what(event: Event) -> str:
    """What a write to a thing did: added, changed, moved, reopened, or the state it closed to."""
    return str(event.payload["state"]) if event.type == "thing.closed" else _WHAT[event.type]


def _unique(open_things: list[things.Thing]) -> list[things.Thing]:
    """The open things whose short id names only them: the model answers by short id."""
    counts = Counter(things.short_id(one) for one in open_things)
    return [one for one in open_things if counts[things.short_id(one)] == 1]
