"""What the daemon's routes are served from: today, the day, mail, settings and the reads."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import sqlite3
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import request_confirmation
from jarvis.deployment.launchd import supervised
from jarvis.runtime.daemon.output_watchers import add_places, add_reactions, add_steps
from jarvis.runtime.daemon.restart import restart_when_idle, restarting
from jarvis.runtime.doing_day import page_reach, read_day_doing
from jarvis.runtime.job_mail import served_notices, settle_notice
from jarvis.shared import lang
from jarvis.state import footprint, phone_track, places, things
from jarvis.state.daily_report import resolve_zone
from jarvis.state.day_highlights import day_notes
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.departures import ID_PREFIX as PIN_PREFIX
from jarvis.state.doing import fact_spans
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.imports import IMPORTS_DIRNAME, shared_reads
from jarvis.state.ledger import TURN_WAIT_S, TerminalScreen
from jarvis.state.lifelog import DEFAULT_MIN_FREE_BYTES, LIFELOG_DIRNAME, Lifelog
from jarvis.state.memory_db import conversation_rows
from jarvis.state.phone_day import day_summary
from jarvis.state.reminders import ID_PREFIX
from jarvis.state.shipments import Shipments
from jarvis.state.turn_facts import recent_turn_timings
from jarvis.surface.terminal_link import SETTINGS_NOTICE

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date, tzinfo

    from jarvis.decision import ToolRegistryLike
    from jarvis.decision.turn_end_asks import TurnEndAsks
    from jarvis.runtime import JarvisRuntime
    from jarvis.runtime.card_feedback import CardFeedback
    from jarvis.runtime.doing_answers import DoingAnswers
    from jarvis.runtime.highlight_marks import HighlightMarks
    from jarvis.runtime.home import Home
    from jarvis.runtime.job_mail import JobMail
    from jarvis.runtime.mail_page import MailPage
    from jarvis.runtime.moment import Moment
    from jarvis.runtime.reminders import Reminders
    from jarvis.runtime.rides import Rides
    from jarvis.runtime.settings import Settings
    from jarvis.runtime.settings_owner import SettingsDrift
    from jarvis.runtime.things_mirror import ThingsMirror
    from jarvis.runtime.work_state import WorkStateService
    from jarvis.surface import voice_controls

LOGGER = logging.getLogger(__name__)


def _today_local(ms: int, zone: tzinfo) -> str:
    """Epoch milliseconds as ISO 8601 in his zone, to the second."""
    return datetime.fromtimestamp(ms / 1000, zone).isoformat(timespec="seconds")


def _today_event(found: things.Occurrence, midnight_ms: int, zone: tzinfo) -> dict[str, Any]:
    """One occurrence as the Today card words it.

    An all-day one starts at the day's local midnight, so one over several days is on each.
    """
    row: dict[str, Any] = {"id": found.thing.thing_id, "title": found.thing.title}
    if found.all_day:
        row["start"] = _today_local(max(found.start_ms, midnight_ms), zone)
        row["all_day"] = True
    else:
        row["start"] = _today_local(found.start_ms, zone)
        row["end"] = _today_local(found.end_ms, zone)
    return row


def _today_todo(one: things.Thing, zone: tzinfo) -> dict[str, Any]:
    """An open thing with no hour and no day as the Today card words it.

    The id is the To Do id (``<listId>|<taskId>``) for a thing the mirror made from a task, so a
    tick on it writes To Do. A date-only due is by the end of that local day (23:59), as To Do
    keeps it.
    """
    row: dict[str, Any] = {
        "id": one.source_ref if one.source == "todo" else one.thing_id, "title": one.title,
    }
    if one.due:
        due = datetime.fromisoformat(one.due)
        if due.tzinfo is None:  # a bare date
            due = due.replace(hour=23, minute=59, tzinfo=zone)
        row["due"] = due.astimezone(zone).isoformat()
    return row


def _read_today(
    event_log: Path, zone: tzinfo, now_ms: int, weather: Callable[[], dict[str, Any] | None],
) -> dict[str, Any]:
    """``GET /inherent/today`` (ADR 0055, 0299): today's events, his open to-dos and the weather.

    Events and to-dos are the ledger of his things, read on a connection of its own, so this runs
    on a worker thread. ``events`` are the open occurrences of the local day ``now_ms`` is in,
    soonest first: ``{id, title, start, end}``, or ``{id, title, start, all_day: true}`` with
    ``start`` the day's midnight. ``todos`` are the open things with no ``start_at`` and no
    ``on_day`` (a due date, or no time), in :func:`jarvis.state.things.open_things` order:
    ``{id, title, due?}``. An id is the thing's, but a to-do the mirror made from To Do keeps its
    To Do id. A thing a mail made that he has not confirmed is left out of both: the card has no
    way to mark it as his to confirm. ``weather`` is the home's line, ``None`` when it has none.
    """
    start_ms, end_ms = day_window(datetime.fromtimestamp(now_ms / 1000, zone).date(), zone)
    with contextlib.closing(open_runtime_event_log(event_log)) as conn:
        found = [
            occ for occ in things.occurrences(conn, start_ms, end_ms, zone=zone)
            if occ.thing.confirmed
        ]
        untimed = [
            one for one in things.open_things(conn)
            if one.confirmed and not (one.start_at or one.on_day)
        ]
    return {
        "events": [_today_event(one, start_ms, zone) for one in found],
        "todos": [_today_todo(one, zone) for one in untimed],
        "weather": weather(),
    }


async def serve_today(
    event_log: Path, zone: tzinfo, weather: Callable[[], dict[str, Any] | None],
) -> dict[str, Any]:
    """:func:`_read_today` off the loop thread, at the brain's clock."""
    return await asyncio.to_thread(_read_today, event_log, zone, int(time.time() * 1000), weather)


def _tick_thing(event_log: Path, zone: tzinfo, thing_id: str, done: bool) -> None:  # noqa: FBT001 — the route's body.
    """His tick on a thing of the Today card: done closes it, not done opens it again.

    Written by him as "ticked on the desktop"; a thing already as he asked is left alone and
    nothing is written. ``LookupError`` when no thing has the id.
    """
    with contextlib.closing(open_runtime_event_log(event_log)) as conn:
        state = things.get(conn, thing_id).state
        write: dict[str, Any] = {
            "by": "him", "evidence": "ticked on the desktop", "ref": thing_id, "zone": zone,
            "actor": "user",
        }
        if done and state == "open":
            things.close(conn, thing_id, "done", **write)
        elif not done and state != "open":
            things.reopen(conn, thing_id, **write)


async def set_todo(  # noqa: PLR0913 — the route's body, and the log and zone a thing's tick needs.
    home: Home, mirror: ThingsMirror | None, event_log: Path, zone: tzinfo, todo_id: str,
    done: bool,  # noqa: FBT001 — the route's body.
) -> None:
    """``POST /inherent/today/todo``: a thing's tick in the ledger, or a To Do task's write.

    A ``thing-<hex>`` id is the ledger's alone (:func:`_tick_thing`). Any other id is a To Do
    task's (ADR 0299): To Do is written first and the route answers by that write, then the thing
    mirrored for it follows if it can, and the mirror's next pass makes it agree if it cannot.
    Both are off the loop thread.
    """
    if todo_id.startswith(things.ID_PREFIX):
        await asyncio.to_thread(_tick_thing, event_log, zone, todo_id, done)
        return
    await asyncio.to_thread(functools.partial(home.set_todo, todo_id, done=done))
    if mirror is not None:
        await asyncio.to_thread(functools.partial(mirror.tick, todo_id, done=done))


async def archive_mail(home: Home, ids: list[str], archive: bool) -> None:  # noqa: FBT001 — the route's body.
    """``POST /inherent/mail/archive`` and ``/unarchive``: one Gmail label change, off the loop."""
    await asyncio.to_thread(functools.partial(home.archive, ids, undo=not archive))


def draft_deps(runtime: JarvisRuntime) -> dict[str, Any]:
    """The four draft routes' callables (ADR 0148, 0366), or none while the mail page is off."""
    page = runtime.mail_page
    if page is None:
        return {}

    async def read(letter_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(page.view, letter_id)

    async def save(letter_id: str, subject: str, body: str) -> dict[str, Any]:
        return await asyncio.to_thread(page.save, letter_id, subject, body)

    async def send(letter_id: str, subject: str, body: str) -> None:
        await asyncio.to_thread(_send_draft, runtime, page, letter_id, subject, body)

    async def discard(letter_id: str) -> None:
        await asyncio.to_thread(page.discard, letter_id)

    return {
        "mail_draft_read": read,
        "mail_draft_save": save,
        "mail_draft_send": send,
        "mail_draft_discard": discard,
    }


def read_day(  # noqa: PLR0913 — the route's stores, its zone, the day asked and the clock.
    event_log: Path, configured: tzinfo | None, memory_db: Path | None, timesink: Path | None,
    day: date | None, now_ms: int, terminal: TerminalScreen | None = None,
    attachments: Path | None = None, shipped: Path | None = None,
    weather: Callable[[date], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """``GET /inherent/day`` (ADR 0199): the day asked for, or today in the owner's zone.

    The zone is ``work_state.timezone`` else the machine's, as every other day window is cut. The
    log is read on a connection of its own, so this runs on a worker thread. ``memory_db`` (his
    talks) and ``timesink`` (his Mac) are the configured stores or None; the ``ledger:`` block
    does not gate them, and a store that is not there leaves its source in ``missing``. On a
    brain ``terminal`` is his Mac's TimeSink. His Outlook is not read here: its events are things
    in the log (ADR 0299). ``line`` and ``highlights`` (ADR 0235) are the day's line to him and
    its marked items, read from ``memory_db``. ``spans`` and ``coverage`` (ADR 0302) are the day's
    fact spans and what he was doing, cut from the items and worded by the day's latest stored
    run and his answers; before them come the day before's stretches that run past midnight
    (ADR 0313).
    ``shipped`` is the imports directory (ADR 0294): what his Mac copied to it fills in his Mac's
    lane when TimeSink cannot be read live, and gives his Claude Code sessions.
    ``summary`` is the day's steps, resting heart rate and heart rate variability from the phone
    (``None`` if the log cannot be read); ``weather`` is the day's weather card
    (``Home.day_weather_card``, kept per day), ``None`` when it has none.
    """
    zone_name, zone = resolve_zone(None, configured)
    on = datetime.fromtimestamp(now_ms / 1000, zone).date() if day is None else day
    start_ms, end_ms = day_window(on, zone)
    before_ms, after_ms = page_reach(on, zone)
    with (
        contextlib.closing(open_runtime_event_log(event_log)) as conn,
        shared_reads(before_ms=before_ms, after_ms=after_ms),
    ):
        sources = DaySources(zone, timesink, memory_db, terminal, attachments, shipped)
        line = fold_day(conn, start_ms, end_ms, now_ms, sources)
        try:
            summary: dict[str, Any] | None = day_summary(conn, start_ms, end_ms)
        except Exception:
            LOGGER.exception("day: the day's body numbers could not be read")
            summary = None
        doing = read_day_doing(conn, memory_db, on, line.items, sources, now_ms)
    told, highlights = day_notes(memory_db, on.isoformat(), line.items, zone)
    served = doing.served({item["id"] for item in line.items})
    spans = sorted([*fact_spans(line.items), *served], key=lambda s: (s["start_ms"], s["id"]))
    return {
        "date": on.isoformat(),
        "tz": zone_name,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "now_ms": now_ms,
        "items": line.items,
        "labels": line.labels,
        "missing": line.missing,
        "screen_synced_ms": line.synced,
        "line": told,
        "highlights": highlights,
        "spans": spans,
        "coverage": doing.own.doing.coverage,
        "summary": summary,
        "weather": None if weather is None else weather(on),
    }


async def serve_day(  # noqa: PLR0913 — the route's stores, its zone, the terminal and the day.
    event_log: Path, configured: tzinfo | None, memory_db: Path | None, timesink: Path | None,
    day: date | None, *, terminal: TerminalScreen | None = None, attachments: Path | None = None,
    shipped: Path | None = None, weather: Callable[[date], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """:func:`read_day` off the loop thread, at the brain's clock."""
    return await asyncio.to_thread(
        read_day, event_log, configured, memory_db, timesink, day, int(time.time() * 1000),
        terminal, attachments, shipped, weather,
    )


async def set_highlight(
    marks: HighlightMarks, day: date, item_id: str, on: bool, label: str | None,  # noqa: FBT001 — the route's argument.
) -> list[dict[str, str]]:
    """``POST /inherent/day/highlight`` (ADR 0297): his mark, written off the loop thread."""
    return await asyncio.to_thread(marks.set_item, day, item_id, on, label)


async def answer_doing(  # noqa: PLR0913 — the route's day, span or stretch, and his words.
    answers: DoingAnswers, day: date, span_id: str | None, choice: str | None, text: str | None,
    stretch: tuple[int, int] | None,
) -> dict[str, Any]:
    """``POST /inherent/day/answer`` (ADR 0302): his answer, written off the loop thread."""
    if stretch is not None and text is not None:
        return await asyncio.to_thread(answers.answer_stretch, day, stretch, text)
    if span_id is None:
        msg = "give span_id, or start_ms and end_ms with text"
        raise ValueError(msg)
    return await asyncio.to_thread(answers.answer, day, span_id, choice, text)


def day_sources(runtime: JarvisRuntime, memory_db: Path) -> DaySources:
    """The stores a day is folded from, as the routes that write about a day's items read them."""
    return DaySources(
        runtime.config.zone,
        runtime.config.observer.timesink_db_path,
        memory_db,
        (
            None if runtime.terminal_hub is None
            else TerminalScreen(runtime.terminal_hub.call, wait_s=TURN_WAIT_S)
        ),
        None if runtime.attachments is None else runtime.attachments.directory,
        shipped=runtime.runtime_paths.root / IMPORTS_DIRNAME,
    )


def _commit_act(event_log_path: Path, zone: tzinfo, commitment_hex: str, action: str) -> bool:
    """He confirms or cancels a thing the day line shows: ``thing-<hex>`` in the ledger.

    Whether anything was written; ``LookupError`` when no open thing has the id (none such, or
    one already closed). A cancel is closed as ``cancelled`` with the reason "not mine".
    """
    thing_id = things.ID_PREFIX + commitment_hex
    conn = open_runtime_event_log(event_log_path)
    try:
        held = things.get(conn, thing_id)
        if held.state != "open":
            msg = f"{thing_id} is {held.state}"
            raise LookupError(msg)
        if action == "confirm":
            return things.confirm(
                conn, thing_id, by="him", evidence="confirmed on the phone", zone=zone,
                actor="user",
            )
        things.close(
            conn, thing_id, "cancelled", why="not mine", by="him",
            evidence="cancelled on the phone", zone=zone, actor="user",
        )
        return True
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


async def act_commitment(
    event_log_path: Path, zone: tzinfo, commitment_hex: str, action: str,
) -> bool:
    """``POST /inherent/commitments/{hex}`` (ADR 0297, 0299), off the loop thread."""
    return await asyncio.to_thread(_commit_act, event_log_path, zone, commitment_hex, action)


async def ride_act(rides: Rides, body: dict[str, Any]) -> dict[str, Any]:
    """``POST /inherent/ride`` (ADR 0224): his answer to the boarding question, off the loop."""
    return await asyncio.to_thread(rides.act, body)


def notice_deps(
    job_mail: JobMail | None, reminders: Reminders | None, moment: Moment | None,
    alerts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The notice routes: job mail's (ADR 0155) plus the owner's fired reminders (ADR 0179).

    ``alerts`` stands in for job mail's notice routes while job mail is off (see
    :func:`alert_deps`), so a job-alert row that is not mail (the spend card) is still served.

    A reminder card is served whatever the quiet level and the hold, ahead of the job mail's, and
    ``POST /inherent/notices/{id}`` routes a ``reminder-`` id to the reminders.
    """
    deps = job_mail_deps(job_mail) or alerts or {}
    if reminders is None:
        return deps
    mail_read, mail_act = deps.get("notices_read"), deps.get("notice_act")

    async def read() -> dict[str, Any]:
        if mail_read is not None:
            body: dict[str, Any] = await mail_read()
        else:  # no job mail: the same answer /inherent/moment gives, plus whether sound is private
            hold = None if moment is None else await moment_hold(moment)
            private = bool((await asyncio.to_thread(reminders.output))["private"])
            body = {"notices": [], "audio_private": private, **(hold or {"hold": None})}
        cards = await asyncio.to_thread(reminders.notices)
        pin = await asyncio.to_thread(reminders.departures.view)
        return {**body, "notices": [*cards, *body["notices"]], "departure": pin}

    async def act(notice_id: str, action: str, reaction: str | None) -> None:
        if notice_id.startswith(PIN_PREFIX):
            await asyncio.to_thread(reminders.departures.unpin, notice_id)
        elif notice_id.startswith(ID_PREFIX):
            await asyncio.to_thread(reminders.acknowledge, notice_id, action)
        elif mail_act is not None:
            await mail_act(notice_id, action, reaction)
        else:
            msg = f"no such notice: {notice_id}"
            raise LookupError(msg)

    async def departure_act(body: dict[str, Any]) -> dict[str, Any]:
        """``POST /inherent/departure`` (ADR 0203, 0205): the bus card and the notch's."""
        return await asyncio.to_thread(reminders.departures.act, body)

    return {
        **deps, "notices_read": read, "notice_act": act,
        "departure_act": departure_act,
    }


def settings_notice_deps(
    deps: dict[str, Any], drift: SettingsDrift | None, idle: Callable[[], bool],
) -> dict[str, Any]:
    """ADR 0227: the settings-differ cards of the terminals, served and answered beside the rest.

    Choosing the terminal's values writes them to this brain's files, which a restart brings in:
    it waits for ``idle`` (no conversation, nothing playing) and happens only where a supervisor
    brings the daemon back.
    """
    if drift is None:
        return deps
    mail_read, mail_act = deps.get("notices_read"), deps.get("notice_act")

    async def read() -> dict[str, Any]:
        body: dict[str, Any] = (
            await mail_read() if mail_read is not None
            else {"notices": [], "audio_private": False, "hold": None, "departure": None}
        )
        return {**body, "notices": [*await asyncio.to_thread(drift.notices), *body["notices"]]}

    async def act(notice_id: str, action: str, reaction: str | None) -> str | None:
        if not notice_id.startswith(SETTINGS_NOTICE):
            if mail_act is None:
                msg = f"no such notice: {notice_id}"
                raise LookupError(msg)
            await mail_act(notice_id, action, reaction)
            return None
        if not await asyncio.to_thread(drift.act, notice_id, action, reaction):
            return None
        if not supervised():
            LOGGER.warning("settings: restart Jarvis to take in the settings just saved")
            return "manual"
        task = asyncio.create_task(restart_when_idle(idle), name="settings_restart")
        restarting.add(task)
        task.add_done_callback(restarting.discard)
        return "idle"

    return {**deps, "notices_read": read, "notice_act": act}


def alert_deps(
    db: Path, quiet: Callable[[], str], moment: Moment | None,
    output: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """The notice routes over the job-alert rows alone, for a daemon with job mail off (ADR 0185).

    The same functions ``JobMail.notices`` and ``JobMail.act`` run, so the quiet level, the moment
    hold and the digest rules are one pipeline.
    """

    def read() -> dict[str, Any]:
        return served_notices(
            db, quiet(), datetime.now(UTC), moment, output,
        )

    async def act(notice_id: str, action: str, reaction: str | None) -> None:
        await asyncio.to_thread(
            settle_notice, db, quiet(), datetime.now(UTC), notice_id, action, reaction,
        )

    return {"notices_read": functools.partial(asyncio.to_thread, read), "notice_act": act}


def job_mail_deps(job_mail: JobMail | None) -> dict[str, Any]:
    """The job-mail routes' callables (ADR 0155), or none while ``job_mail`` is off (404)."""
    if job_mail is None:
        return {}

    async def act(notice_id: str, action: str, reaction: str | None) -> None:
        await asyncio.to_thread(job_mail.act, notice_id, action, reaction)

    async def delete(message_id: str) -> None:
        await asyncio.to_thread(job_mail.delete, message_id)

    async def flag(message_id: str, reaction: str) -> None:
        await asyncio.to_thread(job_mail.flag, message_id, reaction)

    async def application_add(fields: dict[str, Any]) -> str:
        return await asyncio.to_thread(job_mail.add_application, fields)

    async def application_edit(app_id: str, fields: dict[str, Any]) -> None:
        await asyncio.to_thread(job_mail.edit_application, app_id, fields)

    async def application_cancel_reminders(app_id: str) -> None:
        await asyncio.to_thread(job_mail.cancel_reminders, app_id)

    return {
        "notices_read": functools.partial(asyncio.to_thread, job_mail.notices),
        "notice_act": act,
        "jobs_read": functools.partial(asyncio.to_thread, job_mail.ledger),
        "job_delete": delete,
        "job_flag": flag,
        "application_add": application_add,
        "application_edit": application_edit,
        "application_cancel_reminders": application_cancel_reminders,
    }


async def moment_hold(moment: Moment) -> dict[str, Any]:
    """``GET /inherent/moment`` (ADR 0163): ``{hold}``, one read of TimeSink off the loop thread."""
    return {"hold": await asyncio.to_thread(moment.client_hold)}


async def card_act(cards: CardFeedback, card_id: str, body: dict[str, Any]) -> None:
    """``POST /inherent/cards/{id}`` (ADR 0160): one snapshot or reaction, off the loop thread."""
    await asyncio.to_thread(cards.act, card_id, body)


def her_state(controls: voice_controls.VoiceControls) -> str:
    """ADR 0210: the Live Activity's word for her: ``conversation``, else the quiet level."""
    if controls.conversation:
        return "conversation"
    return "idle" if controls.quiet == "off" else controls.quiet


async def mail_letter(home: Home, message_id: str) -> dict[str, Any]:
    """``GET /inherent/mail/{id}`` (ADR 0148): one Gmail read, off the loop thread."""
    return await asyncio.to_thread(home.letter, message_id)


async def mail_summary(home: Home, message_id: str) -> dict[str, Any]:
    """``GET /inherent/mail/{id}/summary`` (ADR 0148): one model call, off the loop thread."""
    return {"summary": await asyncio.to_thread(home.summary, message_id)}


def _send_draft(
    runtime: JarvisRuntime, page: MailPage, letter_id: str, subject: str, body: str,
) -> None:
    """``POST /inherent/mail/{id}/draft/send``: save the edit, raise the send card (no model turn).

    The card is the ``mail`` tool's send of that Gmail draft (ADR 0361, 0366); his button on it is
    what sends. A ValueError (empty body) is the route's 400, a missing ``mail`` tool its 404, and
    a send the tool cannot complete the route's 502 with its reason.
    """
    args = {"action": "send", "draft": page.sendable(letter_id, subject, body)}
    conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    try:
        asked = request_confirmation(
            conn, cast("ToolRegistryLike", runtime.tool_registry), "mail", args,
            ttl_ms=runtime.config.confirmation.ttl_ms, runtime_paths=runtime.runtime_paths,
            lang=lang.language(),
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    if asked is None:
        msg = "the mail tool is not connected"
        raise LookupError(msg)


async def mark_mail_read(home: Home, ids: list[str], read: bool) -> None:  # noqa: FBT001 — the route's body.
    """``POST /inherent/mail/read`` and ``/unread`` (ADR 0148): one label change, off the loop."""
    await asyncio.to_thread(functools.partial(home.mark_read, ids, unread=not read))


async def mail_trash(home: Home, ids: list[str], trash: bool) -> None:  # noqa: FBT001 — the route's body.
    """``POST /inherent/mail/trash`` and ``/untrash`` (ADR 0148): one label change, off the loop."""
    await asyncio.to_thread(functools.partial(home.trash, ids, undo=not trash))


async def turn_end_asks(asks: TurnEndAsks, session_id: str, text: str) -> bool | None:
    """``POST /inherent/agents/turn-end``: Jev's answer, waited for off the loop thread."""
    return await asyncio.to_thread(asks.asks, session_id, text)


async def save_settings(settings: Settings, changes: dict[str, Any]) -> dict[str, Any]:
    """``POST /inherent/settings``: one file write, off the loop thread."""
    return await asyncio.to_thread(settings.update, changes)


def model_takes_images(config: Mapping[str, Any]) -> bool:
    """ADR 0211: whether pictures may go to the conversation model.

    OpenAI's and Anthropic's requests carry them, and only a preset that says ``images: true``
    (the model is known to read them) is trusted with one.
    """
    llm = config.get("llm")
    if not isinstance(llm, Mapping):
        return False
    presets = llm.get("presets")
    preset = presets.get(llm.get("default_preset")) if isinstance(presets, Mapping) else None
    if not isinstance(preset, Mapping):
        return False
    provider = str(preset.get("provider") or llm.get("provider") or "openai").lower()
    return provider in ("openai", "anthropic") and preset.get("images") is True


def open_lifelog(runtime: JarvisRuntime) -> Lifelog:
    """ADR 0238: the archive of his recorded days, at ``lifelog.dir`` or in the runtime root."""
    section = runtime.config.raw.get("lifelog")
    section = section if isinstance(section, dict) else {}
    where = section.get("dir")
    directory = (
        Path(where).expanduser() if isinstance(where, str) and where.strip()
        else runtime.runtime_paths.root / LIFELOG_DIRNAME
    )
    floor = section.get("min_free_gb")
    min_free = (
        int(floor * 1024 ** 3) if isinstance(floor, (int, float)) and floor >= 0
        else DEFAULT_MIN_FREE_BYTES
    )
    return Lifelog(directory, min_free_bytes=min_free)


def lifelog_coverage(runtime: JarvisRuntime, lifelog: Lifelog) -> Callable[[], dict[str, Any]]:
    """ADR 0292: the facts the phone decides by, read at each ask from the hub and the places."""

    def read() -> dict[str, Any]:
        hub = runtime.terminal_hub
        names = [] if hub is None else [name for name, _tools in hub.connected()]
        home: dict[str, float] | None = None
        kept = [] if runtime.memory is None else places.read_kept(runtime.memory.db_path)
        for one in reversed(kept):  # when two share the role, the newer one is the one he means
            if one.role == "home" and one.lat is not None and one.lng is not None:
                home = {"lat": one.lat, "lng": one.lng, "radius_m": one.radius_m}
                break
        return lifelog.coverage(names, home, int(time.time() * 1000))

    return read


def shipments(runtime: JarvisRuntime) -> Shipments:
    """ADR 0294: the intake for a terminal's scheduled copies, in the imports archive."""
    section = runtime.config.raw.get("imports")
    floor = section.get("min_free_gb") if isinstance(section, dict) else None
    min_free = (
        int(floor * 1024 ** 3) if isinstance(floor, (int, float)) and floor >= 0
        else DEFAULT_MIN_FREE_BYTES
    )
    return Shipments(runtime.runtime_paths.root / IMPORTS_DIRNAME, min_free_bytes=min_free)


async def refresh_work_state_now(service: WorkStateService) -> dict[str, Any]:
    """``POST /inherent/work-state/refresh``: the single-flight analysis on its own connection."""
    return await asyncio.to_thread(service.refresh_in_own_connection, trigger="dashboard")


def read_conversation(
    runtime: JarvisRuntime, after: int, limit: int, before: int,
) -> dict[str, Any]:
    """Spec §18.3: a page of the window's rows, the history floor, and ``has_more``."""
    window_memory = runtime.memory
    since = runtime.config.session.history_since
    if window_memory is None:
        return {"since": since, "rows": [], "has_more": False}
    rows, has_more = conversation_rows(
        window_memory.db_path, since=since, after=after, before=before, limit=limit,
        attachments=runtime.attachments,
    )
    add_steps(runtime.runtime_paths.event_log, rows)
    add_places(runtime.runtime_paths.event_log, rows)
    add_reactions(runtime.runtime_paths.event_log, rows)
    return {"since": since, "rows": rows, "has_more": has_more}


def _read_turn_timings(event_log_path: Path, since_ms: int, limit: int) -> dict[str, Any]:
    """``GET /inherent/turn-timings`` (ADR 0276): the newest ``limit`` timing facts, oldest first.

    ``since_ms`` is a time in epoch ms (0 for no lower bound); each fact is the event's payload
    with its ``event_uid`` and ``ts_epoch_ms`` (when the turn's answer was out) added. The log is
    read on a connection of its own, so this runs on a worker thread.
    """
    conn = open_runtime_event_log(event_log_path)
    try:
        timings = recent_turn_timings(conn, since_ms=since_ms, limit=limit)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    turns = [
        {"event_uid": event.event_uid, "ts_epoch_ms": event.ts_epoch_ms, **event.payload}
        for event in timings
    ]
    return {"now_ms": int(time.time() * 1000), "turns": turns}


def _read_track(event_log_path: Path, from_ms: int, to_ms: int) -> dict[str, Any]:
    """``GET /inherent/track`` (ADR 0312): the phones' dense track over a window.

    The log is read on a connection of its own, so this runs on a worker thread.
    """
    conn = open_runtime_event_log(event_log_path)
    try:
        tracks = phone_track.track(conn, from_ms, to_ms)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return {
        "from_ms": from_ms, "to_ms": to_ms, "columns": list(phone_track.COLUMNS), "tracks": tracks,
    }


async def serve_track(event_log_path: Path, from_ms: int, to_ms: int) -> dict[str, Any]:
    """:func:`_read_track` off the loop thread."""
    return await asyncio.to_thread(_read_track, event_log_path, from_ms, to_ms)


def _read_footprint(
    event_log: Path, configured: tzinfo | None, memory_db: Path | None, day: date | None,
    now_ms: int,
) -> dict[str, Any]:
    """``GET /inherent/footprint`` (ADR 0323): his footprint of the day asked for, or today.

    The day is cut in the owner's zone as ``GET /inherent/day`` cuts it, and the places he keeps
    are read from ``memory_db`` (none without it). The log is read on a connection of its own,
    so this runs on a worker thread.
    """
    zone_name, zone = resolve_zone(None, configured)
    on = datetime.fromtimestamp(now_ms / 1000, zone).date() if day is None else day
    start_ms, end_ms = day_window(on, zone)
    kept = [] if memory_db is None else places.read_kept(memory_db)
    conn = open_runtime_event_log(event_log)
    try:
        folded = footprint.fold(conn, start_ms, end_ms, now_ms, kept)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return {
        "date": on.isoformat(),
        "tz": zone_name,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "now_ms": now_ms,
        **folded,
    }


async def serve_footprint(
    event_log: Path, configured: tzinfo | None, memory_db: Path | None, day: date | None,
    *, clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """:func:`_read_footprint` off the loop thread, at the brain's ``clock``."""
    return await asyncio.to_thread(
        _read_footprint, event_log, configured, memory_db, day, int(clock() * 1000),
    )


async def serve_turn_timings(event_log_path: Path, since_ms: int, limit: int) -> dict[str, Any]:
    """:func:`_read_turn_timings` off the loop thread."""
    return await asyncio.to_thread(_read_turn_timings, event_log_path, since_ms, limit)
