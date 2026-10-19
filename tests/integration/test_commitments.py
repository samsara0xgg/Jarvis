"""ADR 0274: what he has to go to is read out of his mail into one log every reader shares.

Data-driven checks over the real code and a real event log and memory.db, with a fake Gmail and a
fake model (no key, no network, no provider). The examples are invented.

- the local filter: letters with and without a sign of a date or an hour, zh and en;
- the model's answer: each shape of action that is kept or dropped;
- the fold: a weekly series, a skipped day, a drop, an update that wins, a clock change;
- the poller (ADR 0299): filtered mail never reaches the model, interview and never-rule mail
  neither, a reminder mail moves or changes the thing it is about and does not duplicate, a cancel
  closes it, one unreadable mail does not stop the cycle, a backfill is paged and read oldest
  first; the rings are the ledger's and nothing arms ``reminder-cm*``;
- the readers: ``my_schedule``, the state block, ``GET /inherent/day``'s fold and ``keep``.
"""

# ruff: noqa: RUF001 - the letters under test include Chinese, with fullwidth punctuation
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision import commitment_mail as triage
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.keep_tool import build_keep_tools
from jarvis.execution.schedule_tool import build_schedule_tool
from jarvis.execution.tools import ActionLifecycle, ToolError, ToolRegistry
from jarvis.runtime.commitment_intake import CommitmentIntake, CommitmentSettings
from jarvis.runtime.now_block import calendar_line, next_appointments
from jarvis.shared import ActionRequest, CallerPrincipal, lang
from jarvis.state import commitments, things
from jarvis.state import job_ledger as ledger
from jarvis.state import reminders as reminder_state
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

ZONE = ZoneInfo("America/Los_Angeles")  # UTC-7 until Nov 1 2026, then UTC-8
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=ZONE)  # a Wednesday
POTTERY = "Pottery class | Oct 20 to Nov 24"


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ZONE)


# --- the local filter ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("Class starts", "See you Wednesday 6:30 PM at Studio 4", True),
        ("Reminder", "Your visit is on Oct 21.", True),
        ("Reminder", "Your visit is on October 21st.", True),
        ("Reminder", "21 October at the front desk", True),
        ("Booked", "Date: 2026-10-21", True),
        ("Booked", "10/21 at the clinic", True),
        ("Booked", "Date: 2026/10/21", True),
        ("Booked", "meet at 18:30", True),
        ("Booked", "doors at 6pm", True),
        ("Booked", "doors at 6 p.m.", True),
        ("Booked", "Tomorrow, bring your card", True),
        ("Booked", "Tonight only", True),
        ("Booked", "Fri night", True),
        ("课程通知", "10月21日上课", True),
        ("课程通知", "下午三点在教室", True),
        ("课程通知", "晚上7点开始", True),
        ("课程通知", "每周三上课", True),
        ("课程通知", "星期三见", True),
        ("课程通知", "礼拜五见", True),
        ("课程通知", "明天带上证件", True),
        ("Weekly deals", "Big sale on shoes. Free shipping.", False),
        ("Your receipt", "Thank you for your order. Total $12.40", False),
        ("你好", "谢谢你的来信，祝好", False),
        ("Password changed", "If this was not you, contact support.", False),
    ],
)
def test_the_filter_lets_a_sign_of_time_through_and_holds_the_rest(
    subject: str, body: str, expected: bool,  # noqa: FBT001
) -> None:
    """The filter lets a sign of time through and holds the rest."""
    assert triage.may_hold_time(subject, body) is expected


# --- the model's answer --------------------------------------------------------------------

_TODAY = date(2026, 10, 14)
_NEW = {
    "action": "new", "title": "Pottery class", "start_at": "2026-10-20T19:00:00-07:00",
    "weekly_until": "2026-11-24", "place": "Studio 4", "private": False,
    "evidence": "Tuesdays 7 PM",
}


def _parse(answer: object, known: set[str] | None = None) -> list[triage.Action]:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return triage.parse_actions(text, known or {"commitment-k"}, zone=ZONE, today=_TODAY)


@pytest.mark.parametrize(
    ("answer", "kept"),
    [
        ([], []),
        ("not json at all", []),
        ('{"action": "new"}', []),
        ("```json\n" + json.dumps([_NEW]) + "\n```", ["new"]),
        ([_NEW], ["new"]),
        ([_NEW, {"action": "frobnicate"}, 7, None], ["new"]),
        ([{**_NEW, "title": ""}], []),
        ([{**_NEW, "start_at": "next tuesday"}], []),
        ([{**_NEW, "start_at": "2031-10-20T19:00:00-07:00"}], []),
        ([{**_NEW, "end_at": "2026-10-20T18:00:00-07:00"}], []),
        ([{**_NEW, "weekly_until": "2026-10-01"}], []),
        ([{**_NEW, "link": "javascript:alert(1)"}], []),
        ([{**_NEW, "link": "https://meet.example/abc"}], ["new"]),
        ([{**_NEW, "private": "yes"}], []),
        ([{**_NEW, "skip_dates": ["2026-10-27", "oops"]}], []),
        ([{"action": "update", "commitment_id": "commitment-k", "place": "Studio 5"}], ["update"]),
        ([{"action": "update", "commitment_id": "commitment-zz", "place": "x"}], []),
        ([{"action": "update", "commitment_id": "commitment-k"}], []),
        ([{"action": "cancel", "commitment_id": "commitment-k"}], ["cancel"]),
        ([{"action": "cancel", "commitment_id": "commitment-zz"}], []),
    ],
)
def test_the_answer_is_checked_and_what_does_not_parse_is_dropped(
    answer: object, kept: list[str],
) -> None:
    """The answer is checked and what does not parse is dropped."""
    assert [a.action for a in _parse(answer)] == kept


def test_a_time_with_no_offset_is_read_in_his_zone_and_text_is_cut() -> None:
    """A time with no offset is read in his zone and text is cut."""
    (action,) = _parse([{**_NEW, "start_at": "2026-10-20T19:00:00", "evidence": "x" * 500}])
    assert action.fields["start_at"] == "2026-10-20T19:00:00-07:00"
    assert len(action.fields["evidence"]) == 200


# --- the fold ------------------------------------------------------------------------------


class _Log:
    def __init__(self, root: Path) -> None:
        self.paths = bootstrap_runtime(root)
        self.conn = open_event_log(self.paths.event_log)
        self.memory = root / "memory.db"
        open_memory_db(self.memory).close()


@pytest.fixture
def log(tmp_path: Path) -> Iterator[_Log]:
    """A runtime root with a real event log and memory.db; the language is English."""
    before = lang.language()
    lang.set_language("en")
    one = _Log(tmp_path)
    yield one
    one.conn.close()
    lang.set_language(before)


def _record(start: datetime, **extra: Any) -> commitments.Commitment:  # noqa: ANN401 - record fields
    fields: dict[str, Any] = {
        "commitment_id": commitments.new_id(), "title": "Pottery class",
        "start_at": start.isoformat(timespec="seconds"), "source": "mail", "confirmed": False,
        "private": False,
    }
    return commitments.Commitment(**(fields | extra))


def _days(log: _Log, first: datetime, last: datetime) -> list[str]:
    return [
        o.day for o in commitments.occurrences(log.conn, _ms(first), _ms(last), zone=ZONE)
    ]


def test_a_weekly_series_runs_to_its_last_day_less_the_skipped_ones(log: _Log) -> None:
    """A weekly series runs to its last day less the skipped ones."""
    commitments.note(log.conn, _record(
        _at(10, 20, 19), weekly_until="2026-11-10", skip_dates=("2026-10-27",),
    ))
    assert _days(log, _at(10, 1, 0), _at(12, 1, 0)) == ["2026-10-20", "2026-11-03", "2026-11-10"]
    # a window inside one occurrence still finds it; one that ends as it starts does not
    assert _days(log, _at(10, 20, 19, 30), _at(10, 20, 20)) == ["2026-10-20"]
    assert _days(log, _at(10, 20, 0), _at(10, 20, 19)) == []
    assert _days(log, _at(10, 20, 20), _at(10, 21, 0)) == []


def test_an_occurrence_lasts_until_end_at_else_an_hour(log: _Log) -> None:
    """An occurrence lasts until end at else an hour."""
    commitments.note(log.conn, _record(_at(10, 20, 19), end_at=_at(10, 20, 21, 30).isoformat()))
    commitments.note(log.conn, _record(_at(10, 21, 19), title="Other"))
    one, two = commitments.occurrences(log.conn, _ms(_at(10, 1, 0)), _ms(_at(12, 1, 0)))
    assert [(o.end_ms - o.start_ms) // 60_000 for o in (one, two)] == [150, 60]


def test_a_weekly_series_keeps_its_wall_clock_across_the_clock_change(log: _Log) -> None:
    """A weekly series keeps its wall clock across the clock change."""
    commitments.note(log.conn, _record(_at(10, 28, 18, 30), weekly_until="2026-11-11"))
    found = commitments.occurrences(log.conn, _ms(_at(10, 1, 0)), _ms(_at(12, 1, 0)), zone=ZONE)
    assert [o.day for o in found] == ["2026-10-28", "2026-11-04", "2026-11-11"]
    local = [datetime.fromtimestamp(o.start_ms / 1000, ZONE) for o in found]
    assert [(t.hour, t.minute) for t in local] == [(18, 30)] * 3  # Nov 1 was the change
    assert found[1].start_ms - found[0].start_ms == (7 * 24 + 1) * 3_600_000


def test_the_last_note_wins_and_a_drop_removes_the_commitment(log: _Log) -> None:
    """The last note wins and a drop removes the commitment."""
    first = _record(_at(10, 20, 19))
    commitments.note(log.conn, first)
    changed = commitments.Commitment(**{**first.payload(), "skip_dates": (), "place": "Studio 5"})
    commitments.note(log.conn, changed)
    (one,) = commitments.live(log.conn)
    assert one.place == "Studio 5"
    commitments.drop(log.conn, first.commitment_id, "cancelled")
    assert commitments.live(log.conn) == []
    assert commitments.fold(log.conn)[first.commitment_id].dropped == "cancelled"
    with pytest.raises(ValueError, match="drop reason"):
        commitments.drop(log.conn, first.commitment_id, "whim")


def test_the_last_sender_rule_wins(log: _Log) -> None:
    """The last sender rule wins."""
    commitments.set_sender_rule(log.conn, "Studio.EXAMPLE", "always")
    commitments.set_sender_rule(log.conn, "shop.example", "never")
    commitments.set_sender_rule(log.conn, "studio.example", "never")
    assert commitments.sender_rules(log.conn) == {
        "studio.example": "never", "shop.example": "never",
    }


def test_a_record_that_cannot_be_read_is_refused(log: _Log) -> None:
    """A record that cannot be read is refused."""
    for bad in (
        _record(_at(10, 20, 19), source="rumour"),
        _record(_at(10, 20, 19), start_at="2026-10-20T19:00:00"),
        _record(_at(10, 20, 19), weekly_until="soon"),
    ):
        with pytest.raises(ValueError, match=r"source|offset|date|Invalid"):
            commitments.note(log.conn, bad)
    assert commitments.live(log.conn) == []


# --- the poller ----------------------------------------------------------------------------


def _mail(
    key: str, sender: str, subject: str, body: str, day: int,
) -> dict[str, Any]:
    stamp = datetime(2026, 10, day, 8, 0, tzinfo=UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return {
        "id": key, "threadId": key, "from": sender, "subject": subject, "date": stamp,
        "body": body,
    }


class _Gmail:
    """A mailbox that answers like the gmail server: newest first, two to a page."""

    def __init__(self, mails: list[dict[str, Any]]) -> None:
        self.mails = mails
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.broken: set[str] = set()

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "gmail"
        self.calls.append((tool, dict(args)))
        assert tool in {"gmail_search", "gmail_get"}, f"the poller may only read: {tool}"
        if tool == "gmail_search":
            listed = sorted(self.mails, key=lambda m: int(m["id"][1:]), reverse=True)
            first = int(args.get("pageToken") or 0)
            page = listed[first : first + 2]
            reply: dict[str, Any] = {"messages": [{"id": m["id"]} for m in page]}
            if first + 2 < len(listed):
                reply["nextPageToken"] = str(first + 2)
            return {"text": json.dumps(reply)}
        if args["messageId"] in self.broken and args["format"] == "full":
            msg = "mailbox hiccup"
            raise ToolError(msg, code="mcp_tool_error")
        mail = next(m for m in self.mails if m["id"] == args["messageId"])
        body = mail if args["format"] == "full" else {k: v for k, v in mail.items() if k != "body"}
        return {"text": json.dumps(body)}

    def searches(self) -> int:
        return sum(tool == "gmail_search" for tool, _ in self.calls)


class _Connections:
    def __init__(self, gmail: _Gmail) -> None:
        self.gmail = gmail

    def client_for(self, server: str) -> _Gmail:
        assert server == "gmail"
        return self.gmail


class _Model:
    """Answers by the letter's subject; remembers what it was asked."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def __call__(self, _system: str, user: str) -> str:
        subject = next(line for line in user.splitlines() if line.startswith("Subject: "))
        self.asked.append(subject.removeprefix("Subject: "))
        return json.dumps(self.answers.get(self.asked[-1], []))


def _intake(
    log: _Log, gmail: _Gmail, model: Callable[[str, str], str], cap: int = 60,
) -> CommitmentIntake:
    settings = CommitmentSettings(300, date(2026, 9, 10), cap, "haiku-bg", 60)
    intake = CommitmentIntake(
        settings, _Connections(gmail), log.memory, log.paths.event_log, model,  # type: ignore[arg-type]
    )
    intake.zone = "America/Los_Angeles"
    intake.now = lambda: NOW.astimezone(UTC)
    return intake


def _verdicts(log: _Log) -> dict[str, str]:
    with closing(open_memory_db(log.memory)) as conn:
        rows = conn.execute("SELECT message_id, verdict FROM commitment_mail").fetchall()
    return dict(rows)


_CLASS = "Welcome to pottery class"
_REMIND = "Pottery reminder"
_MAILS = [
    _mail("m1", "Weekly Deals <news@shop.example>", "Weekly deals", "Big sale on shoes.", 1),
    _mail("m2", "Studio <hello@studio.example>", _CLASS, "Tuesdays 7 PM, Oct 20 to Nov 24.", 2),
    _mail("m3", "Talent <jobs@acme.example>", "Interview Friday 2 PM", "See you Friday 2 PM.", 3),
    _mail("m4", "Cafe <hi@cafe.example>", "Event Saturday 3 PM", "Join us Saturday 3 PM.", 4),
    _mail("m5", "Studio <hello@studio.example>", _REMIND, "See you Tuesday 7 PM at Studio 5.", 5),
]
_NEW_CLASS = {
    "action": "new", "title": "Pottery class", "start_at": "2026-10-20T19:00:00-07:00",
    "weekly_until": "2026-11-24", "place": "Studio 4", "private": False,
    "evidence": "Tuesdays 7 PM, Oct 20 to Nov 24.",
}


def _setup_cycle(log: _Log) -> _Gmail:
    ledger.upsert_mail(log.memory, {
        "message_id": "m3", "thread_id": "m3", "received_at": "2026-10-03T08:00:00+00:00",
        "sender_name": "Talent", "sender_domain": "acme.example", "subject": "Interview",
        "kind": "interview", "company": "Acme", "confidence": 0.9, "extracted_by": "local",
    }, NOW)
    commitments.set_sender_rule(log.conn, "cafe.example", "never")
    return _Gmail([dict(m) for m in _MAILS])


def test_a_cycle_reads_each_mail_by_its_own_rule_and_a_reminder_mail_updates(log: _Log) -> None:
    """A cycle reads each mail by its own rule and a reminder mail updates."""
    gmail = _setup_cycle(log)
    model = _Model({_CLASS: [_NEW_CLASS]})

    def ask(system: str, user: str) -> str:
        if _REMIND in user:  # read after the class mail: the known list names what it made
            (held,) = things.open_things(log.conn)
            key = held.thing_id.removeprefix(things.ID_PREFIX)
            assert key in user
            model.answers[_REMIND] = [
                {"action": "update", "commitment_id": key, "place": "Studio 5"},
            ]
        return model(system, user)

    intake = _intake(log, gmail, ask)
    assert intake.poll_once() == 5
    # the letters the model never saw: no sign of time, an interview, a never-rule sender
    assert model.asked == [_CLASS, _REMIND]
    assert _verdicts(log) == {
        "m1": "filtered", "m2": "noted", "m3": "interview", "m4": "filtered", "m5": "noted",
    }
    (held,) = things.open_things(log.conn)
    assert (held.title, held.kind, held.place, held.confirmed, held.source) == (
        "Pottery class", "go", "Studio 5", False, "mail",
    )
    assert (held.source_ref, held.sender, held.weekly_until, held.rings) == (
        "m2", "studio.example", "2026-11-24", ("-60m",),
    )
    assert [(s.what, s.by, s.ref) for s in held.history] == [
        ("added", "mail", "m2"), ("changed", "mail", "m5"),
    ]
    assert intake.poll_once() == 0  # nothing is read twice
    assert model.asked == [_CLASS, _REMIND]
    assert {tool for tool, _ in gmail.calls} == {"gmail_search", "gmail_get"}


def test_his_claude_daily_letter_is_filtered_before_the_model(log: _Log) -> None:
    """ADR 0295: a letter naming a time (his chat said "Friday 2 PM") is not read as a mail."""
    daily = _mail(
        "m6", "Allen <me@example.com>", "Jarvis Claude 日报 2026-10-18",
        "## Job chat\n- summary: interview Friday 2 PM", 6,
    )
    gmail = _Gmail([daily])
    model = _Model({})
    _intake(log, gmail, model).poll_once()

    assert model.asked == []
    assert _verdicts(log) == {"m6": "filtered"}
    assert not any(tool == "gmail_get" and a["format"] == "full" for tool, a in gmail.calls)


def test_a_new_that_repeats_a_held_time_is_not_a_second_commitment(log: _Log) -> None:
    """A new that repeats a held time is not a second commitment."""
    gmail = _Gmail([
        _mail("m2", "Studio <hello@studio.example>", _CLASS, "Tuesdays 7 PM.", 2),
        _mail("m5", "Studio <hello@studio.example>", _REMIND, "Tuesday 7 PM.", 5),
    ])
    model = _Model({_CLASS: [_NEW_CLASS], _REMIND: [{**_NEW_CLASS, "weekly_until": ""}]})
    assert _intake(log, gmail, model).poll_once() == 2
    assert _verdicts(log) == {"m2": "noted", "m5": "none"}
    assert len(things.open_things(log.conn)) == 1


def test_a_sender_marked_always_is_confirmed_and_a_cancel_closes(log: _Log) -> None:
    """A sender marked always is confirmed and a cancel closes the thing as cancelled."""
    commitments.set_sender_rule(log.conn, "studio.example", "always")
    gmail = _Gmail([
        _mail("m2", "Studio <hello@studio.example>", _CLASS, "Tuesdays 7 PM.", 2),
        _mail("m6", "Studio <hello@studio.example>", "Class cancelled", "No class Tuesday 7 PM", 6),
    ])

    def ask(_system: str, user: str) -> str:
        if "Class cancelled" in user:
            (held,) = things.open_things(log.conn)
            key = held.thing_id.removeprefix(things.ID_PREFIX)
            return json.dumps([{"action": "cancel", "commitment_id": key}])
        return json.dumps([_NEW_CLASS])

    assert _intake(log, gmail, ask).poll_once() == 2
    (record,) = things.fold(log.conn).values()
    assert (record.confirmed, record.state) == (True, "cancelled")
    assert (record.history[-1].what, record.history[-1].by, record.history[-1].why) == (
        "cancelled", "mail", "called off in a mail",
    )
    assert things.open_things(log.conn) == []


def test_one_unreadable_mail_does_not_stop_the_cycle_and_is_tried_three_times(log: _Log) -> None:
    """One unreadable mail does not stop the cycle and is tried three times."""
    gmail = _Gmail([
        _mail("m2", "Studio <hello@studio.example>", _CLASS, "Tuesdays 7 PM.", 2),
        _mail("m3", "Clinic <hello@clinic.example>", "Visit Friday 2 PM", "Friday 2 PM.", 3),
        _mail("m4", "Cafe <hi@cafe.example>", "Event Saturday 3 PM", "Saturday 3 PM.", 4),
    ])
    gmail.broken = {"m3"}
    model = _Model({_CLASS: [_NEW_CLASS]})
    intake = _intake(log, gmail, model)
    assert intake.poll_once() == 3
    assert _verdicts(log) == {"m2": "noted", "m3": "error", "m4": "none"}
    assert intake.poll_once() == 1  # only the error is read again
    assert intake.poll_once() == 1
    assert intake.poll_once() == 0  # third try was the last
    assert len(things.open_things(log.conn)) == 1


def test_a_model_that_does_not_answer_ends_the_cycle_after_one_try(log: _Log) -> None:
    """A model that does not answer ends the cycle after one try."""
    gmail = _Gmail([
        _mail("m2", "Studio <hello@studio.example>", _CLASS, "Tuesdays 7 PM.", 2),
        _mail("m3", "Clinic <hello@clinic.example>", "Visit Friday 2 PM", "Friday 2 PM.", 3),
    ])
    asked: list[str] = []

    def down(_system: str, user: str) -> str:
        asked.append(user)
        msg = "overloaded"
        raise RuntimeError(msg)

    assert _intake(log, gmail, down).poll_once() == 0
    assert len(asked) == 1  # not one call per mail of the backlog
    assert _verdicts(log) == {"m2": "error"}


def test_the_backfill_is_paged_and_read_oldest_first_a_cap_at_a_time(log: _Log) -> None:
    """The backfill is paged and read oldest first a cap at a time."""
    gmail = _Gmail([
        _mail(f"m{n}", f"S <a@s{n}.example>", f"Visit Friday {n} PM", f"Friday {n} PM.", n)
        for n in range(1, 6)
    ])
    model = _Model({})
    intake = _intake(log, gmail, model, cap=3)
    assert intake.poll_once() == 3
    assert model.asked == ["Visit Friday 1 PM", "Visit Friday 2 PM", "Visit Friday 3 PM"]
    first = gmail.searches()
    assert first == 3  # the listing was paged: two to a page, five mails
    query = next(a["query"] for tool, a in gmail.calls if tool == "gmail_search")
    assert query == "after:2026/09/10 -in:sent -in:drafts"
    assert intake.poll_once() == 2
    assert model.asked[3:] == ["Visit Friday 4 PM", "Visit Friday 5 PM"]
    assert intake.poll_once() == 0
    assert gmail.searches() - first == 2 + 1  # page one of the second cycle, then a page of seen


def test_a_poller_with_no_model_reads_nothing(log: _Log) -> None:
    """A poller with no model reads nothing."""
    gmail = _Gmail([_mail("m2", "S <a@s.example>", "Visit Friday 2 PM", "Friday 2 PM.", 2)])
    settings = CommitmentSettings(300, date(2026, 9, 10), 60, "haiku-bg", 60)
    intake = CommitmentIntake(
        settings, _Connections(gmail), log.memory, log.paths.event_log, None,  # type: ignore[arg-type]
    )
    assert intake.poll_once() == 0
    assert gmail.calls == []


# --- the rings -----------------------------------------------------------------------------


def _rings(log: _Log) -> dict[str, str]:
    """The pending reminders as ``due -> words``; all are the ledger's, none ``reminder-cm*``."""
    pending = reminder_state.pending(log.conn)
    assert all(r.reminder_id.startswith(things.RING_PREFIX) for r in pending)
    assert not [
        r for r in reminder_state.fold(log.conn) if r.startswith(commitments.REMINDER_PREFIX)
    ]
    return {r.due_at_local: r.text for r in pending}


def test_a_noted_thing_rings_by_the_ledger_and_a_move_and_a_cancel_follow(log: _Log) -> None:
    """The poller arms nothing: the ring is the thing's, moved with it and gone with its cancel."""
    studio = "Studio <hello@studio.example>"
    gmail = _Gmail([
        _mail("m2", studio, _CLASS, "Tomorrow 7 PM.", 2),
        _mail("m5", studio, _REMIND, "Moved to 8 PM.", 5),
        _mail("m6", studio, "Class cancelled", "No class tomorrow 7 PM", 6),
    ])
    once = {k: v for k, v in _NEW_CLASS.items() if k != "weekly_until"}
    once["start_at"] = "2026-10-15T19:00:00-07:00"

    def ask(_system: str, user: str) -> str:
        if _CLASS in user:
            return json.dumps([once])
        (held,) = things.open_things(log.conn)
        key = held.thing_id.removeprefix(things.ID_PREFIX)
        if _REMIND in user:
            at = "2026-10-15T20:00:00-07:00"
            return json.dumps([{"action": "update", "commitment_id": key, "start_at": at}])
        return json.dumps([{"action": "cancel", "commitment_id": key}])

    intake = _intake(log, gmail, ask, cap=1)
    assert intake.poll_once() == 1
    assert _rings(log) == {"2026-10-15T18:00:00-07:00": "19:00 Pottery class · Studio 4"}
    assert intake.poll_once() == 1
    (held,) = things.open_things(log.conn)
    assert (held.start_at, [m.start_at for m in held.moves]) == (
        "2026-10-15T20:00:00-07:00", ["2026-10-15T19:00:00-07:00"],
    )
    assert _rings(log) == {"2026-10-15T19:00:00-07:00": "20:00 Pottery class · Studio 4"}
    assert intake.poll_once() == 1
    assert things.open_things(log.conn) == []
    assert _rings(log) == {}


def test_an_update_that_gives_a_day_an_hour_swaps_its_ring_for_the_hour_before(log: _Log) -> None:
    """A whole day rings the evening before; once a mail says the hour, it rings before that."""
    gmail = _Gmail([
        _mail("m2", "Bank <a@bank.example>", "Security", "Enable 2FA by Oct 15.", 2),
        _mail("m5", "Bank <a@bank.example>", "Security call", "Your call is Oct 15 at 10 AM.", 5),
    ])
    due = {**_DUE, "start_at": "2026-10-15"}

    def ask(_system: str, user: str) -> str:
        if "Security call" not in user:
            return json.dumps([due])
        (held,) = things.open_things(log.conn)
        key = held.thing_id.removeprefix(things.ID_PREFIX)
        timed = {
            "action": "update", "commitment_id": key, "all_day": False,
            "start_at": "2026-10-15T10:00:00-07:00",
        }
        return json.dumps([timed])

    intake = _intake(log, gmail, ask, cap=1)
    assert intake.poll_once() == 1
    (held,) = things.open_things(log.conn)
    assert (held.on_day, held.start_at, held.rings) == ("2026-10-15", "", ("-1d@20:00",))
    assert _rings(log) == {"2026-10-14T20:00:00-07:00": "Tomorrow: Enable 2FA"}
    assert intake.poll_once() == 1
    (held,) = things.open_things(log.conn)
    assert (held.on_day, held.start_at, held.rings) == (
        "", "2026-10-15T10:00:00-07:00", ("-60m",),
    )
    assert _rings(log) == {"2026-10-15T09:00:00-07:00": "10:00 Enable 2FA"}


# --- the readers ---------------------------------------------------------------------------


def _no_microsoft(_tool: str, _args: Mapping[str, Any]) -> list[dict[str, Any]]:
    msg = "microsoft is not connected"
    raise LookupError(msg)


def _schedule(log: _Log, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the model's arguments
    (tool,) = build_schedule_tool(_no_microsoft, ZONE, lambda: NOW)
    registry = ToolRegistry()
    registry.register(tool)
    request = ActionRequest(
        action_id="S1", tool_name="my_schedule", target_entity_ref=None,
        caller_principal=CallerPrincipal.JARVIS_LLM, risk_level="L0", arguments=args,
        authorization_lease=None, run_id=None, turn_id=None,
    )
    lifecycle = ActionLifecycle()
    lifecycle.register("S1")
    lifecycle.transition("S1", "authorized")
    slot = registry.dispatch(request, log.conn, log.paths, lifecycle).slots[0]
    assert slot.tool_output is not None
    out: dict[str, Any] = json.loads(slot.tool_output)
    return out


def test_my_schedule_lists_appointments_with_where_they_came_from_and_if_confirmed(
    log: _Log,
) -> None:
    """My schedule lists appointments with where they came from and if confirmed."""
    mail = _record(_at(10, 14, 18, 30), place="Studio 4", link="https://meet.example/a")
    said = _record(_at(10, 15, 10), title="Dentist", source="said", confirmed=True)
    for one in (mail, said):
        commitments.note(log.conn, one)
    out = _schedule(log)
    rows = {r["id"]: r for r in out["items"] if r["source"] in {"mail", "said"}}
    assert rows[mail.commitment_id]["what"] == "Pottery class (from a mail, not confirmed yet)"
    assert rows[mail.commitment_id]["confirmed"] is False
    assert rows[mail.commitment_id]["where"] == "Studio 4"
    assert rows[mail.commitment_id]["link"] == "https://meet.example/a"
    assert rows[said.commitment_id]["what"] == "Dentist"
    assert rows[said.commitment_id]["confirmed"] is True
    assert "2 appointments" in out["summary"]
    assert [r["what"].split(" (")[0] for r in out["items"]] == ["Pottery class", "Dentist"]
    # Microsoft is not connected: that is named, and the appointments are still there
    assert any("calendar" in line for line in out["missing"])
    assert "appointments" in out["read"] or "reminders" in out["read"]


def test_the_state_block_names_the_next_appointments_without_a_calendar(log: _Log) -> None:
    """The state block names the next appointments without a calendar."""
    commitments.note(log.conn, _record(_at(10, 14, 18, 30)))
    group = _record(_at(10, 14, 20), title="Group", private=True, confirmed=True)
    commitments.note(log.conn, group)
    line = calendar_line(
        None, ZONE, lambda: NOW.astimezone(UTC),
        lambda now: next_appointments(log.paths.event_log, ZONE, now),
    )
    assert line == (
        'Next on his calendar (read just now): 6:30 pm "Pottery class (not confirmed)";'
        ' 8:00 pm "something private".'
    )
    # with a broken calendar the same line; with no appointments and no calendar, none
    assert calendar_line(None, ZONE, lambda: NOW.astimezone(UTC)) is None


def test_the_day_gets_each_occurrence_as_an_event_with_its_source(log: _Log) -> None:
    """The day gets each occurrence as an event with its source."""
    one = _record(_at(10, 14, 18, 30), weekly_until="2026-10-21", place="Studio 4", private=True)
    commitments.note(log.conn, one)
    first, last = day_window(date(2026, 10, 14), ZONE)
    day = fold_day(log.conn, first, last, _ms(NOW), DaySources(ZONE))
    (item,) = [i for i in day.items if i["kind"] == "event"]
    assert item == {
        "id": f"{one.commitment_id}@2026-10-14", "lane": "you", "kind": "event",
        "start_ms": _ms(_at(10, 14, 18, 30)), "end_ms": _ms(_at(10, 14, 19, 30)), "state": "next",
        "title": "Pottery class", "source": "mail", "confirmed": False, "private": True,
        "place": "Studio 4",
    }
    assert "commitments" not in day.missing
    nxt, _ = day_window(date(2026, 10, 21), ZONE)
    week = fold_day(log.conn, nxt, nxt + 86_400_000, _ms(NOW), DaySources(ZONE))
    assert [i["id"] for i in week.items] == [f"{one.commitment_id}@2026-10-21"]


# --- keep and kept -------------------------------------------------------------------------


class _Keep:
    def __init__(self, log: _Log) -> None:
        self.log = log
        self.registry = ToolRegistry()
        for tool in build_keep_tools(None, ZONE, lambda: NOW.astimezone(UTC)):
            self.registry.register(tool)
        self.n = 0

    def call(self, name: str, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the model's arguments
        self.n += 1
        action_id = f"K{self.n}"
        request = ActionRequest(
            action_id=action_id, tool_name=name, target_entity_ref=None,
            caller_principal=CallerPrincipal.JARVIS_LLM, risk_level="L1", arguments=args,
            authorization_lease=None, run_id=None, turn_id=None,
        )
        lifecycle = ActionLifecycle()
        lifecycle.register(action_id)
        lifecycle.transition(action_id, "authorized")
        slot = self.registry.dispatch(request, self.log.conn, self.log.paths, lifecycle).slots[0]
        assert slot.tool_output is not None
        out: dict[str, Any] = json.loads(slot.tool_output)
        return out


def test_keep_makes_a_confirmed_appointment_and_kept_lists_it(log: _Log) -> None:
    """Keep makes a confirmed appointment and kept lists it."""
    keep = _Keep(log)
    made = keep.call(
        "keep", kind="appointment", text="Dentist", at="2026-10-15 10:00", place="Smile Clinic",
    )
    assert made["summary"].startswith("Appointment kept for tomorrow (Thu) 10:00 am")
    again = keep.call(
        "keep", kind="appointment", text="dentist", at="2026-10-15 10:00", place="Smile Clinic",
    )
    assert again["id"] == made["id"]
    assert again["summary"].startswith("Already kept")
    weekly = keep.call(
        "keep", kind="appointment", text="Choir", at="2026-10-21 19:00", until="2026-11-11",
    )
    assert "every week until 2026-11-11" in weekly["summary"]
    (dentist, choir) = commitments.live(log.conn)
    assert (dentist.source, dentist.confirmed, dentist.place) == ("said", True, "Smile Clinic")
    assert choir.weekly_until == "2026-11-11"
    listed = keep.call("kept", kind="appointment")
    assert [(r["text"], r["confirmed"]) for r in listed["items"]] == [
        ("Dentist (Smile Clinic)", True), ("Choir", True),
    ]
    assert "2 appointments" in listed["summary"]
    assert keep.call("kept")["items"][0]["kind"] == "appointment"


@pytest.mark.parametrize(
    ("args", "words"),
    [
        ({"kind": "appointment", "text": "x"}, "an appointment needs at"),
        ({"kind": "appointment", "text": "x", "at": "2026-10-15 10:00", "until": "later"}, "until"),
        (
            {"kind": "appointment", "text": "x", "at": "2026-10-15 10:00", "until": "2026-10-01"},
            "until",
        ),
        ({"kind": "memo", "text": "x", "until": "2026-10-21"}, "goes only with kind=appointment"),
        ({"kind": "memo", "text": "x", "place": "here"}, "goes only with kind=appointment"),
        ({"text": "x", "whole_sender": True}, "whole_sender goes only with"),
        ({"confirm": "commitment-zz"}, "no appointment has the id"),
        ({"cancel": "commitment-zz"}, "no appointment has the id"),
        ({"confirm": "commitment-zz", "text": "x"}, "stands alone"),
    ],
)
def test_a_bad_appointment_call_is_refused_with_the_fix(
    log: _Log, args: dict[str, Any], words: str,
) -> None:
    """A bad appointment call is refused with the fix."""
    refused = _Keep(log).call("keep", **args)
    assert words in refused["error"]
    assert commitments.live(log.conn) == []


def test_confirm_and_cancel_change_a_mail_made_appointment_and_may_set_the_sender_rule(
    log: _Log,
) -> None:
    """Confirm and cancel change a mail made appointment and may set the sender rule."""
    keep = _Keep(log)
    one = _record(_at(10, 20, 19), sender_domain="studio.example")
    two = _record(_at(10, 21, 19), title="Other", sender_domain="cafe.example")
    stray = _record(_at(10, 22, 19), title="Stray", source="said", confirmed=True)
    for rec in (one, two, stray):
        commitments.note(log.conn, rec)
    done = keep.call("keep", confirm=one.commitment_id, whole_sender=True)
    assert done["summary"].startswith('Confirmed the appointment "Pottery class".')
    assert "studio.example will always count" in done["summary"]
    held = {c.commitment_id: c for c in commitments.live(log.conn)}
    assert held[one.commitment_id].confirmed
    assert commitments.sender_rules(log.conn) == {"studio.example": "always"}
    assert "Already confirmed" in keep.call("keep", confirm=one.commitment_id)["summary"]
    gone = keep.call("keep", cancel=two.commitment_id, whole_sender=True)
    assert gone["summary"].startswith('Dropped the appointment "Other".')
    assert commitments.fold(log.conn)[two.commitment_id].dropped == "not_his"
    assert commitments.sender_rules(log.conn)["cafe.example"] == "never"
    plain = keep.call("keep", cancel=one.commitment_id)
    assert plain["summary"].startswith("Dropped")
    assert commitments.fold(log.conn)[one.commitment_id].dropped == "he_dropped"
    assert "no sender" in keep.call("keep", cancel=stray.commitment_id, whole_sender=True)["error"]
    assert [c.commitment_id for c in commitments.live(log.conn)] == [stray.commitment_id]


# --- the replay script ---------------------------------------------------------------------


def test_the_replay_script_filters_offline_and_carries_commitments_forward(tmp_path: Path) -> None:
    """The replay script filters offline and carries commitments forward."""
    from scripts import commitment_replay  # noqa: PLC0415 - only this test needs it

    rows = [
        {"message_id": "a", "sender_name": "Studio", "sender_domain": "Studio.example",
         "subject": _CLASS, "received_at": "2026-10-02T08:00:00+00:00", "body": "Tuesdays 7 PM."},
        {"message_id": "b", "sender_name": "Shop", "sender_domain": "shop.example",
         "subject": "Deals", "received_at": "2026-10-01T08:00:00+00:00", "body": "Big sale."},
        {"message_id": "c", "sender_name": "Studio", "sender_domain": "studio.example",
         "subject": _REMIND, "received_at": "2026-10-05T08:00:00+00:00",
         "body": "Tuesday 7 PM, Studio 5."},
    ]
    source, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    source.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert commitment_replay.main([str(source), str(out)]) == 0  # no --live: the filter alone
    offline = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [(r["message_id"], r["filter"], r["asked"], r["actions"]) for r in offline] == [
        ("b", False, False, None), ("a", True, False, None), ("c", True, False, None),
    ]

    seen_known: list[bool] = []

    def ask(_system: str, user: str) -> str:
        if _REMIND in user:
            seen_known.append("replay-1" in user)  # the class mail made it, as the poller would
            return json.dumps([{"action": "update", "commitment_id": "replay-1", "place": "5"}])
        return json.dumps([_NEW_CLASS])

    lines = commitment_replay.replay(rows, "America/Los_Angeles", _TODAY, ask)
    assert seen_known == [True]
    assert [(r["message_id"], r["asked"], [a["action"] for a in r["actions"] or []])
            for r in lines] == [("b", False, []), ("a", True, ["new"]), ("c", True, ["update"])]


# --- follow-up 1: all-day items, the interview race, quiet readers, replay days -----------

_DUE = {
    "action": "new", "title": "Enable 2FA", "start_at": "2026-10-31", "all_day": True,
    "private": False, "evidence": "by Oct 31",
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_DUE, ("2026-10-31T00:00:00-07:00", True)),
        ({**_DUE, "start_at": "2026-10-31T17:45:00-07:00"}, ("2026-10-31T00:00:00-07:00", True)),
        ({**_DUE, "end_at": "2026-11-01T00:00:00-07:00"}, ("2026-10-31T00:00:00-07:00", True)),
        ({**_DUE, "all_day": False, "start_at": "2026-10-31T17:45:00"},
         ("2026-10-31T17:45:00-07:00", False)),
        ({**_DUE, "all_day": "yes"}, None),
        ({**_DUE, "start_at": "someday"}, None),
    ],
)
def test_an_all_day_item_is_the_local_midnight_of_its_day(
    raw: dict[str, Any], expected: tuple[str, bool] | None,
) -> None:
    """A date with no hour parses to that day's midnight; a bad flag drops the action."""
    found = _parse([raw])
    if expected is None:
        assert found == []
        return
    (action,) = found
    assert (action.fields["start_at"], action.fields["all_day"]) == expected
    assert "end_at" not in action.fields or not expected[1]


def test_the_prompt_asks_for_dated_obligations_and_not_sales_deadlines() -> None:
    """The wording the live exam depends on: the all-day rule and the sales exclusion."""
    system, _user = triage.build_prompt(
        triage.Mail("S", "s.example", "Subject", "2026-10-01T00:00:00+00:00", "Body"),
        [], zone="America/Los_Angeles", today=_TODAY,
    )
    assert '"all_day": true' in system
    assert "date but no time" in system
    assert '"offer ends"' in system
    assert "cannot tell the time" not in system


def test_an_all_day_occurrence_is_its_whole_day(log: _Log) -> None:
    """It overlaps any window inside its day and none outside; a weekly one keeps the rule."""
    one = _record(_at(10, 31, 0), all_day=True, weekly_until="2026-11-07")
    commitments.note(log.conn, one)
    assert _days(log, _at(10, 31, 22), _at(11, 1, 1)) == ["2026-10-31"]
    assert _days(log, _at(11, 1, 0), _at(11, 7, 0)) == []
    assert _days(log, _at(11, 7, 12), _at(11, 8, 0)) == ["2026-11-07"]
    (first, *_) = commitments.occurrences(log.conn, _ms(_at(10, 1, 0)), _ms(_at(12, 1, 0)))
    assert first.end_ms - first.start_ms == 24 * 3_600_000


def test_the_poller_notes_an_all_day_item_as_a_day_and_its_ring_is_the_evening_before(
    log: _Log,
) -> None:
    """A day with no hour is the thing's ``on_day``; its ring is 20:00 the evening before."""
    gmail = _Gmail([_mail("m2", "Bank <a@bank.example>", "Security", "Enable 2FA by Oct 31.", 2)])
    assert _intake(log, gmail, _Model({"Security": [_DUE]})).poll_once() == 1
    (held,) = things.open_things(log.conn)
    assert (held.on_day, held.start_at, held.end_at) == ("2026-10-31", "", "")
    assert (held.kind, held.rings) == ("go", ("-1d@20:00",))
    assert _rings(log) == {}  # Oct 30 is more than a week away: the ring waits
    things.sync_rings(log.conn, zone=ZONE, now=_at(10, 30, 9).astimezone(UTC))
    assert _rings(log) == {"2026-10-30T20:00:00-07:00": "Tomorrow: Enable 2FA"}


def test_a_private_all_day_ring_says_no_more_than_that_something_is_to_do(log: _Log) -> None:
    """A mail that makes a thing private gives the ring no title."""
    private = {**_DUE, "private": True, "start_at": "2026-10-15"}
    gmail = _Gmail([_mail("m2", "Bank <a@bank.example>", "Security", "Enable 2FA by Oct 15.", 2)])
    assert _intake(log, gmail, _Model({"Security": [private]})).poll_once() == 1
    assert _rings(log) == {"2026-10-14T20:00:00-07:00": "you have something to do tomorrow"}


def _old_ring(log: _Log) -> None:
    """A ``reminder-cm`` ring as an earlier build armed it: still pending, and not an item."""
    due = _at(10, 14, 20)
    action = {"action_id": "commitment-reminders"}
    emit_event(
        log.conn, type="reminder.scheduled", correlation=action,
        payload={
            "reminder_id": f"{commitments.REMINDER_PREFIX}old-1", "due_at_epoch_ms": _ms(due),
            "due_at_local": due.isoformat(timespec="seconds"), "text": "Tomorrow: Enable 2FA",
            **action,
        },
    )


def test_the_readers_show_an_all_day_item_as_due_and_leave_its_ring_out(log: _Log) -> None:
    """my_schedule says due; the day has a label, not an item; the state block lists it."""
    one = _record(_at(10, 15, 0), title="Enable 2FA", all_day=True, place="bank.example")
    timed = _record(_at(10, 15, 10), title="Dentist", confirmed=True)
    for rec in (one, timed):
        commitments.note(log.conn, rec)
    _old_ring(log)
    rows = {r["id"]: r for r in _schedule(log)["items"] if "id" in r}
    assert rows[one.commitment_id]["when"] == "due tomorrow (Thu), all day"
    assert not [r for r in _schedule(log)["items"] if r["source"] == "reminder"]
    first, last = day_window(date(2026, 10, 15), ZONE)
    day = fold_day(log.conn, first, last, _ms(NOW), DaySources(ZONE))
    assert [i["kind"] for i in day.items] == ["event"]  # the dentist; no ring, no all-day item
    assert day.labels == [{
        "title": "Enable 2FA", "source": "mail", "confirmed": False, "private": False,
        "place": "bank.example", "id": f"{one.commitment_id}@2026-10-15",
    }]
    line = calendar_line(
        None, ZONE, lambda: NOW.astimezone(UTC),
        lambda now: next_appointments(log.paths.event_log, ZONE, now),
    )
    assert line == (
        'Next on his calendar (read just now): due tomorrow (Thu) "Enable 2FA (not confirmed)";'
        ' tomorrow (Thu) 10:00 am "Dentist".'
    )
    # kept can still list the ring
    kept = _Keep(log).call("kept", kind="reminder")
    assert kept["items"]


def test_the_state_block_lists_a_deadline_due_today(log: _Log) -> None:
    """An all-day item of today is still ahead at noon."""
    commitments.note(log.conn, _record(_at(10, 14, 0), title="Hand in form", all_day=True))
    now = _at(10, 14, 12).astimezone(UTC)
    line = calendar_line(
        None, ZONE, lambda: now, lambda at: next_appointments(log.paths.event_log, ZONE, at),
    )
    assert line == (
        'Next on his calendar (read just now): due today "Hand in form (not confirmed)".'
    )


def _mail_at(key: str, subject: str, when: datetime) -> dict[str, Any]:
    stamp = when.astimezone(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return {
        "id": key, "threadId": key, "from": "Studio <hello@studio.example>", "subject": subject,
        "date": stamp, "body": "Tuesday 7 PM.",
    }


def test_a_young_mail_the_job_ledger_has_not_seen_waits_and_an_old_one_does_not(log: _Log) -> None:
    """Under 30 minutes and no job_seen row: unread and unrecorded; seen or older: read."""
    gmail = _Gmail([
        _mail_at("m1", "Old unseen", NOW - timedelta(minutes=45)),
        _mail_at("m2", "Young unseen", NOW - timedelta(minutes=5)),
        _mail_at("m3", "Young seen", NOW - timedelta(minutes=6)),
    ])
    ledger.record_seen(log.memory, "m3", "not_job", NOW)
    model = _Model({})
    intake = _intake(log, gmail, model)
    assert intake.poll_once() == 2
    assert sorted(model.asked) == ["Old unseen", "Young seen"]
    assert _verdicts(log) == {"m1": "none", "m3": "none"}  # m2 left no record
    intake.now = lambda: (NOW + timedelta(minutes=40)).astimezone(UTC)
    assert intake.poll_once() == 1  # half an hour on, job mail or not, it is read
    assert "Young unseen" in model.asked
    assert _verdicts(log)["m2"] == "none"


def test_the_replay_gives_each_mail_its_own_day_with_today_received(tmp_path: Path) -> None:
    """``--today received`` tells the model the mail's received date as today."""
    from scripts import commitment_replay  # noqa: PLC0415 - only this test needs it

    rows = [{
        "message_id": "a", "sender_name": "Bank", "sender_domain": "bank.example",
        "subject": "Security", "received_at": "2026-10-02T20:00:00+00:00",
        "body": "Enable 2FA by Oct 31.",
    }]
    told: list[str] = []

    def ask(_system: str, user: str) -> str:
        told.append(next(x for x in user.splitlines() if x.startswith("His time zone")))
        return "[]"

    commitment_replay.replay(rows, "America/Los_Angeles", None, ask)
    commitment_replay.replay(rows, "America/Los_Angeles", _TODAY, ask)
    assert told == [
        "His time zone: America/Los_Angeles. Today: 2026-10-02.",
        "His time zone: America/Los_Angeles. Today: 2026-10-14.",
    ]
    source = tmp_path / "in.jsonl"
    source.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    out = tmp_path / "o.jsonl"
    assert commitment_replay.main([str(source), str(out), "--today", "received"]) == 0
