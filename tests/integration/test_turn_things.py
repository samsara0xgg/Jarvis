"""ADR 0300: what he says and shows in a turn is read into the ledger by one pass after it.

Data-driven checks over the real code, a real event log and a real memory.db, with a fake model
that answers canned JSON (no key, no network, no provider). The examples are invented.

- the model's answer: each shape of action that is kept or dropped, and why;
- the pass: a go thing with a time, a thing marked paid, a line that is not his, a close that names
  no open thing, a new thing already past, her tool having written it already, unsure and stated;
- cited lines and private (ADR 0329, 0331): his lines are numbered and copied as the evidence, a
  private thing is written private, and a thing the pass adds never rings;
- no doings: what he says he did is read only as the actions it makes (ADR 0328);
- when a record is read: after her answer, a picture waits for its note or its window, an
  unanswered record waits, live-voice rows are covered, a first start reads seven days back, the
  rescan, errors tried three times, an outage costs one record one try;
- the replay script: it writes only to its scratch copy and refuses the live runtime root;
- the boot: off unless enabled.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from jarvis.decision import turn_things as decision
from jarvis.deployment import bootstrap_runtime
from jarvis.runtime import RuntimeBootstrapError, bootstrap_runtime_app
from jarvis.runtime import turn_things as runtime
from jarvis.runtime.turn_things import ModelFailed, TurnThings, TurnThingsSettings
from jarvis.shared import lang
from jarvis.state import picture_notes, plan, things
from jarvis.state import turn_things as marks
from jarvis.state.attachments import AttachmentRef
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.state.memory_db import open_memory_db
from scripts import turn_things_replay
from tests.support.runtime_log import RuntimeLog

if TYPE_CHECKING:
    from collections.abc import Iterator

ZONE = ZoneInfo("America/Vancouver")  # UTC-7 until Nov 1 2026
SAID = datetime(2026, 10, 14, 14, 0, tzinfo=ZONE)  # a Wednesday, 14:00
SECOND = timedelta(seconds=1)
NOTHING: dict[str, list[Any]] = {"actions": []}
DENTIST = "2026-10-15T15:00:00-07:00"


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


# --- the model's answer --------------------------------------------------------------------

_HELD = things.Thing(
    "thing-ab12cd" + "0" * 20, "Pay rent", "do", due="2026-10-31", confirmed=False,
)
_CLASS = things.Thing(
    "thing-cd34ef" + "0" * 20, "Pottery", "go", start_at="2026-10-20T19:00:00-07:00",
    weekly_until="2026-11-24",
)
_KNOWN = {things.short_id(_HELD): _HELD, things.short_id(_CLASS): _CLASS}
_PAID = {"action": "paid", "thing": "ab12cd", "lines": ["m1"], "sure": True}
_NEW = {
    "action": "new", "kind": "go", "title": "Dentist", "start_at": "2026-10-15T15:00:00",
    "lines": ["m2"], "sure": True,
}
_WORDS = (
    "I paid the rent. Dentist tomorrow at 3pm, the pottery class moved. I had lunch with Mia at "
    "noon. I'm at the library since 13:00. Maybe pay the deposit, get the form by the 20th."
)
_NOTE = "Screenshot of an email: your road test is booked for 2026-10-22 at 09:30."
_RENT = "I paid the rent."
_DENTIST_LINE = "Dentist tomorrow at 3pm, the pottery class moved."
_LUNCH = "I had lunch with Mia at noon."


def _turn(words: str = _WORDS, notes: tuple[str, ...] = (_NOTE,), answer: str = "") -> marks.Turn:
    return marks.Turn("r1", SAID, words, ("a1",), notes, (), answer, None)


def _said(fields: dict[str, Any]) -> dict[str, Any]:
    """The fields that say something: an empty text and not private are not given."""
    return {
        k: v for k, v in fields.items()
        if v != "" and not (k == "private" and v is False)
    }


def _read(
    answer: object, *, words: str = _WORDS, notes: tuple[str, ...] = (_NOTE,),
    answer_text: str = "",
) -> decision.Read:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return decision.parse_answer(
        text, _turn(words, notes, answer_text), zone=ZONE, known=_KNOWN,
    )


def _actions(*items: object) -> dict[str, list[Any]]:
    return {"actions": list(items)}


def _with(base: dict[str, Any], **changes: Any) -> dict[str, Any]:  # noqa: ANN401 - fields
    return {k: v for k, v in (base | changes).items() if v is not None}


_NEW_KEPT = {
    "action": "new", "quote": _DENTIST_LINE, "sure": True, "source": "said",
    "kind": "go", "title": "Dentist", "start_at": DENTIST,
}


@pytest.mark.parametrize(
    ("answer", "kept", "dropped"),
    [
        (NOTHING, [], None),
        # a new thing
        (_actions(_NEW), [_NEW_KEPT], None),
        (_actions(_with(_NEW, start_at=DENTIST)), [_NEW_KEPT], None),
        (_actions(_with(_NEW, end_at="2026-10-15T16:00:00")), [
            _NEW_KEPT | {"end_at": "2026-10-15T16:00:00-07:00"},
        ], None),
        (_actions(_with(_NEW, start_at=None, on_day="2026-10-14")), [
            _NEW_KEPT | {"start_at": "", "on_day": "2026-10-14"},
        ], None),
        (_actions(_with(_NEW, start_at=None, due="2026-10-20", sure=False, kind="do")), [
            _NEW_KEPT | {"start_at": "", "due": "2026-10-20", "sure": False, "kind": "do"},
        ], None),
        (_actions(_with(_NEW, start_at=None, due="2026-10-20T09:00:00")), [
            _NEW_KEPT | {"start_at": "", "due": "2026-10-20T09:00:00-07:00"},
        ], None),
        (_actions(_with(_NEW, weekly_until="2026-11-24")), [
            _NEW_KEPT | {"weekly_until": "2026-11-24"},
        ], None),
        (_actions(_with(_NEW, start_at=None)), [_NEW_KEPT | {"start_at": ""}], None),
        (_actions(_with(_NEW, place="the clinic")), [_NEW_KEPT | {"place": "the clinic"}], None),
        (_actions(_with(_NEW, lines=["p1"])), [
            _NEW_KEPT | {"quote": _NOTE, "source": "picture"},
        ], None),
        (_actions(_with(_NEW, lines=[" [M2] "])), [_NEW_KEPT], None),
        # what it cites: his lines copied in their order, "…" where one is skipped
        (_actions(_with(_NEW, lines=["m2", "m1"])), [
            _NEW_KEPT | {"quote": f"{_RENT} {_DENTIST_LINE}"},
        ], None),
        (_actions(_with(_NEW, lines=["m3", "m1", "m1"])), [
            _NEW_KEPT | {"quote": f"{_RENT} … {_LUNCH}"},
        ], None),
        (_actions(_with(_NEW, lines=["m2", "p1"])), [
            _NEW_KEPT | {"quote": f"{_NOTE} \u2026 {_DENTIST_LINE}", "source": "picture"},
        ], None),
        # a private thing; whether to tell him is not the pass's to say
        (_actions(_with(_NEW, private=True)), [_NEW_KEPT | {"private": True}], None),
        (_actions(_with(_NEW, remind=[60])), [_NEW_KEPT], None),
        ("```json\n" + json.dumps(_actions(_NEW)) + "\n```", [_NEW_KEPT], None),
        # a new thing that does not pass
        (_actions(_with(_NEW, lines=["m9"])), [], "'m9' is not one of his lines"),
        (_actions(_with(_NEW, lines=["a1"])), [], "not one of his lines"),
        (_actions(_with(_NEW, lines=[2])), [], "not one of his lines"),
        (_actions(_with(_NEW, lines=[])), [], "cites none"),
        (_actions(_with(_NEW, lines="m2")), [], "cites none"),
        (_actions(_with(_NEW, lines=None, quote="dentist tomorrow at 3pm")), [], "cites none"),
        (_actions(_with(_NEW, lines=["m1", "m2", "m3", "m4", "m5", "p1", "m1"])), [],
         "more than 6"),
        (_actions(_with(_NEW, private="yes")), [], "private"),
        (_actions(_with(_NEW, start_at="2026-10-13T15:00:00")), [], "already past"),
        (_actions(_with(_NEW, start_at="2026-10-14T13:59:00")), [], "already past"),
        (_actions(_with(_NEW, start_at=None, on_day="2026-10-13")), [], "already past"),
        (_actions(_with(_NEW, start_at=None, due="2026-10-14T07:00:00")), [], "already past"),
        (_actions(_with(_NEW, start_at=None, due="2026-10-13")), [], "already past"),
        (_actions(_with(_NEW, start_at="tomorrow afternoon")), [], "does not read"),
        (_actions(_with(_NEW, start_at=None, due="soon")), [], "does not read"),
        (_actions(_with(_NEW, start_at=None, on_day="2026-13-40")), [], "not a date"),
        (_actions(_with(_NEW, due="2026-10-20")), [], "more than one kind"),
        (_actions(_with(_NEW, end_at="2026-10-15T14:00:00")), [], "not after"),
        (_actions(_with(_NEW, start_at=None, end_at="2026-10-15T16:00:00")), [], "needs start_at"),
        (_actions(_with(_NEW, start_at=None, weekly_until="2026-11-24")), [], "weekly_until"),
        (_actions(_with(_NEW, weekly_until="2026-10-01")), [], "before the first day"),
        (_actions(_with(_NEW, start_at="2028-10-15T15:00:00")), [], "more than a year"),
        (_actions(_with(_NEW, kind="dream")), [], "kind"),
        (_actions(_with(_NEW, title=" ")), [], "no title"),
        (_actions(_with(_NEW, sure=None)), [], "sure"),
        (_actions(_with(_NEW, sure="yes")), [], "sure"),
        (_actions(_with(_NEW, place=7)), [], "place"),
        (_actions(_with(_NEW, action="delete")), [], "not an action"),
        (_actions(7), [], "not an action"),
        # a thing he closes, moves or confirms
        (_actions(_PAID), [{
            "action": "paid", "quote": _RENT, "sure": True, "source": "said",
            "thing": _HELD.thing_id,
        }], None),
        (_actions(_with(_PAID, thing="AB12CD ", action="done")), [{
            "action": "done", "quote": _RENT, "sure": True, "source": "said",
            "thing": _HELD.thing_id,
        }], None),
        (_actions(_with(_PAID, action="cancelled")), [{
            "action": "cancelled", "quote": _RENT, "sure": True, "source": "said",
            "thing": _HELD.thing_id,
        }], None),
        (_actions(_with(_PAID, thing="ffffff")), [], "no open thing"),
        (_actions(_with(_PAID, thing=None)), [], "no open thing"),
        (_actions(_with(_PAID, thing=7)), [], "no open thing"),
        (_actions(_with(_PAID, sure=False)), [], "plainly"),
        (_actions(_with(_PAID, lines=["m7"])), [], "not one of his lines"),
        (_actions(_with(_PAID, action="confirmed")), [{
            "action": "confirmed", "quote": _RENT, "sure": True, "source": "said",
            "thing": _HELD.thing_id,
        }], None),
        (_actions(_with(_PAID, action="confirmed", thing="cd34ef")), [], "confirmed already"),
        (_actions(_with(_PAID, action="move", thing="cd34ef", start_at="2026-10-22T19:00:00")), [{
            "action": "move", "quote": _RENT, "sure": True, "source": "said",
            "thing": _CLASS.thing_id, "start_at": "2026-10-22T19:00:00-07:00",
        }], None),
        (_actions(_with(_PAID, action="move", start_at=None, due="2026-11-05")), [{
            "action": "move", "quote": _RENT, "sure": True, "source": "said",
            "thing": _HELD.thing_id, "due": "2026-11-05",
        }], None),
        (_actions(_with(_PAID, action="move", start_at=None, due="2026-10-31")), [], "that time"),
        (_actions(_with(_PAID, action="move")), [], "needs the new time"),
        (_actions(_with(_PAID, action="move", due="2026-10-13")), [], "already past"),
        (_actions(_with(_PAID, action="move", thing="cd34ef", start_at="2026-10-20T19:00:00")), [],
         "that time"),
        (_actions(_with(
            _PAID, action="move", thing="cd34ef", occurrence="2026-10-27",
            start_at="2026-10-28T19:00:00",
        )), [{
            "action": "move", "quote": _RENT, "sure": True, "source": "said",
            "thing": _CLASS.thing_id, "start_at": "2026-10-28T19:00:00-07:00",
            "occurrence": "2026-10-27",
        }], None),
        (_actions(_with(
            _PAID, action="move", thing="cd34ef", occurrence="2026-10-27", due="2026-11-05",
        )), [], "moves to a start_at"),
        (_actions(_with(_PAID, thing="cd34ef", occurrence="2026-10-27")), [{
            "action": "paid", "quote": _RENT, "sure": True, "source": "said",
            "thing": _CLASS.thing_id, "occurrence": "2026-10-27",
        }], None),
        (_actions(_with(_PAID, thing="cd34ef", occurrence="next week")), [], "not a date"),
        # the shape of the answer, and what is read of it
        (_actions(_NEW, _with(_NEW, lines=["m8"]), _PAID), [_NEW_KEPT, {
            "action": "paid", "quote": _RENT, "sure": True, "source": "said",
            "thing": _HELD.thing_id,
        }], "not one of his lines"),
        (_actions(*[_NEW] * 12), [_NEW_KEPT] * 10, None),
    ],
)
def test_the_answer_is_checked_and_what_does_not_pass_is_dropped_with_its_reason(
    answer: object, kept: list[dict[str, Any]], dropped: str | None,
) -> None:
    """Each action is kept as it reads or dropped, and the drop says why."""
    found = _read(answer)
    assert [_said(asdict(one)) for one in found.actions] == [_said(one) for one in kept]
    if dropped is None:
        assert found.dropped == ()
    else:
        assert len(found.dropped) == 1
        assert dropped in found.dropped[0]


def test_only_his_message_and_the_notes_of_his_pictures_are_numbered() -> None:
    """The model cites lines; her answer and the messages before his have none to cite."""
    turn = marks.Turn(
        "r1", SAID, _WORDS + "\n\u597d\u7684\uff01\u660e\u5929\u89c1", ("a1",),
        (_NOTE, "A receipt. Total $40"), (), "Done: your appointment is booked.", None,
    )
    assert decision.numbered(turn) == (
        ("p1", "Screenshot of an email: your road test is booked for 2026-10-22 at 09:30."),
        ("p2", "A receipt."), ("p3", "Total $40"),
        ("m1", _RENT), ("m2", _DENTIST_LINE), ("m3", _LUNCH),
        ("m4", "I'm at the library since 13:00."),
        ("m5", "Maybe pay the deposit, get the form by the 20th."),
        ("m6", "\u597d\u7684\uff01"), ("m7", "\u660e\u5929\u89c1"),
    )
    _, user = decision.build_prompt(
        turn, now=SAID, zone=ZONE, zone_name="America/Vancouver", plan=None, open_things=[],
        written=[],
    )
    assert f"[m1] {_RENT}\n[m2] {_DENTIST_LINE}" in user
    assert f"[p1] {_NOTE}\n[p2] A receipt." in user
    assert "Done: your appointment is booked." in user
    assert "] Done: your appointment" not in user


@pytest.mark.parametrize(
    "bad",
    [
        "not json at all", "[]", "7", '{"actions": 3}', '{"doings": []}', '{"actions": {}}', "{",
        "",
    ],
)
def test_an_answer_that_is_not_the_object_asked_for_is_refused(bad: str) -> None:
    """No JSON object with an actions list is an error, not an empty answer."""
    with pytest.raises(decision.AnswerError):
        _read(bad)


def test_the_extractor_names_the_instructions() -> None:
    """The version a set carries changes whenever a word of the instructions does."""
    digest = hashlib.sha256(decision.INSTRUCTIONS.encode()).hexdigest()
    assert f"turn-things-{digest[:8]}" == decision.EXTRACTOR
    assert decision.INSTRUCTIONS.startswith("You read one message that Allen sent")


@pytest.mark.parametrize(
    ("written", "action", "made"),
    [
        ([], _NEW_KEPT, False),
        ([("added", {"start_at": DENTIST})], _NEW_KEPT, True),
        ([("added", {"start_at": "2026-10-15T22:00:00+00:00"})], _NEW_KEPT, True),
        ([("added", {"start_at": "2026-10-15T16:00:00-07:00"})], _NEW_KEPT, False),
        ([("moved", {"start_at": DENTIST})], _NEW_KEPT, False),
        ([("added", {})], _NEW_KEPT | {"start_at": ""}, False),
        (
            [("added", {"on_day": "2026-10-15"})],
            _NEW_KEPT | {"start_at": "", "on_day": "2026-10-15"}, True,
        ),
        ([("added", {"start_at": DENTIST})], {"action": "paid", "thing": "x"}, False),
        # her tool kept it as an hour, the pass reads it as a time to be done by: one moment
        ([("added", {"start_at": DENTIST})], _NEW_KEPT | {"start_at": "", "due": DENTIST}, True),
        ([("added", {"due": "2026-10-15"})], _NEW_KEPT | {"start_at": "", "on_day": "2026-10-15"},
         True),
        ([("added", {"due": "2026-10-15"})], _NEW_KEPT, False),
    ],
)
def test_a_timed_new_thing_her_tool_wrote_in_the_turn_is_made_already(
    written: list[tuple[str, dict[str, Any]]], action: dict[str, Any], made: bool,  # noqa: FBT001
) -> None:
    """Same time, whatever the title: it is the thing her tool wrote; untimed ones are not told."""
    fields: dict[str, Any] = {"quote": "q", "sure": True, **action}
    one = decision.Action(**fields)
    seen = [
        decision.Written(what, things.Thing("thing-1", "Appointment", "go", **when))
        for what, when in written
    ]
    assert decision.made_already(one, seen) is made


# --- the pass over a real log --------------------------------------------------------------


class _Log(RuntimeLog):
    """The runtime root of these tests, with his memory records written into it."""

    def say(self, record_id: str, source: str, text: str, at: datetime) -> None:
        with closing(open_memory_db(self.memory)) as conn, conn:
            conn.execute(
                "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
                (record_id, at.isoformat(timespec="seconds"), source, text),
            )

    def picture(self, record_id: str, attachment_id: str, at: datetime, words: str = "") -> None:
        """A record of his with an image: the marker line is in its text, the link in its table."""
        self.say(record_id, "allen", f"{words}\n[attached: image p.jpg]".strip(), at)
        with closing(open_memory_db(self.memory)) as conn, conn:
            conn.execute(
                "INSERT INTO record_attachments (record_id, pos, attachment_id, kind, name) "
                "VALUES (?, 0, ?, 'image', 'p.jpg')",
                (record_id, attachment_id),
            )

    def note(self, attachment_id: str, body: str) -> None:
        picture_notes.append(
            self.memory, AttachmentRef(attachment_id, "image", "p.jpg", 1, "image/jpeg"), body,
        )

    def turn(self, words: str, *, at: datetime = SAID, answer: str | None = "Noted.") -> str:
        """His record at ``at`` and, unless ``answer`` is None, her answer 20 seconds on."""
        record_id = f"r{len(self.records()) + 1}"
        self.say(record_id, "allen", words, at)
        if answer is not None:
            self.say(f"a{record_id}", "jarvis", answer, at + 20 * SECOND)
        return record_id

    def records(self) -> list[str]:
        with closing(open_memory_db(self.memory)) as conn:
            return [str(rid) for (rid,) in conn.execute(
                "SELECT id FROM records WHERE source = 'allen' ORDER BY rowid",
            )]

    def hold(self, title: str, kind: str = "do", **fields: Any) -> things.Thing:  # noqa: ANN401
        made, _ = things.add(
            self.conn, things.Thing(things.new_id(), title, kind, **fields), by="mail",
            evidence="a mail", zone=ZONE, now=SAID - timedelta(days=1),
        )
        return made

    def verdicts(self) -> dict[str, tuple[str, int]]:
        """Every record's row: its verdict and how many times it failed."""
        marks.has_marks(self.memory)  # makes the table when the pass has not run
        with closing(open_memory_db(self.memory)) as conn:
            rows = conn.execute("SELECT record_id, verdict, tries FROM turn_things").fetchall()
        return {str(rid): (str(verdict), int(tries)) for rid, verdict, tries in rows}

    def row(self, record_id: str) -> tuple[Any, ...]:
        with closing(open_memory_db(self.memory)) as conn:
            found = conn.execute(
                "SELECT verdict, tries, extractor, input_json, output_json FROM turn_things "
                "WHERE record_id = ?", (record_id,),
            ).fetchone()
        assert found is not None
        return tuple(found)

    def pass_events(self) -> list[tuple[str, str, str, str]]:
        return [
            (e.type, e.payload["by"], e.payload["ref"], e.payload["evidence"])
            for e in iter_events_of_types(self.conn, things.TYPES)
            if e.payload.get("by") == "pass"
        ]

    def ledger(self) -> list[tuple[Any, ...]]:
        return [
            (t.title, t.kind, t.state, t.start_at or t.on_day or t.due, t.confirmed, t.weekly_until)
            for t in things.fold(self.conn).values()
        ]


@pytest.fixture
def log(tmp_path: Path) -> Iterator[_Log]:
    """A runtime root with a real event log and memory.db; the language is English."""
    before = lang.language()
    lang.set_language("en")
    one = _Log(tmp_path)
    yield one
    one.conn.close()
    lang.set_language(before)


_LINE = re.compile(r"^\[([mp]\d+)\] (.*)$", re.MULTILINE)


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


def _cite(answer: object, lines: dict[str, str]) -> object:
    """An answer written with quotes, as the model gives it, cited by line.

    Each quote becomes the labels of the numbered lines that hold it, or of a line that is not there
    when none does.
    """
    if not isinstance(answer, dict) or not isinstance(answer.get("actions"), list):
        return answer
    cited = []
    for one in answer["actions"]:
        if isinstance(one, dict) and isinstance(one.get("quote"), str):
            quote = _flat(one["quote"])
            labels = [
                label for label, text in lines.items()
                if quote and (quote in _flat(text) or _flat(text) in quote)
            ]
            cited.append(
                {k: v for k, v in one.items() if k != "quote"} | {"lines": labels or ["m99"]},
            )
        else:
            cited.append(one)
    return answer | {"actions": cited}


class _Model:
    """Answers by what he said; remembers what it was asked and what it was told."""

    def __init__(self, answers: dict[str, object] | None = None) -> None:
        self.answers = answers or {}
        self.held: list[things.Thing] = []
        self.asked: list[str] = []
        self.prompts: list[str] = []
        self.said: list[str] = []

    def __call__(self, _system: str, user: str) -> str:
        lines = dict(_LINE.findall(user))
        words = " ".join(text for label, text in lines.items() if label.startswith("m"))
        self.asked.append(words)
        self.prompts.append(user)
        answer = {_flat(k): v for k, v in self.answers.items()}.get(_flat(words), NOTHING)
        if isinstance(answer, BaseException):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(_cite(answer, lines))
        for number, one in enumerate(self.held):
            text = text.replace(f"{{h{number}}}", things.short_id(one))
        self.said.append(text)
        return text


def _pass(log: _Log, model: _Model, clock: list[datetime], days: int = 7) -> TurnThings:
    one = TurnThings(
        TurnThingsSettings(days, "haiku-bg"), log.memory, log.paths.event_log, model, ZONE,
    )
    one.now = lambda: clock[0]
    return one


_PAID_IT = {"action": "paid", "thing": "{h0}", "quote": "I paid the rent", "sure": True}
_CASES: dict[str, dict[str, Any]] = {
    "a record that adds a go thing with a time": {
        "words": "Dentist tomorrow at 3pm", "held": [],
        "answer": _actions(_with(_NEW, quote="Dentist tomorrow at 3pm")),
        "ledger": [("Dentist", "go", "open", DENTIST, True, "")], "wrote": ["added"],
        "dropped": [], "verdict": "written",
    },
    "one that marks an open thing paid": {
        "words": "I paid the rent",
        "held": [("Pay rent", "do", {"due": "2026-10-31"})],
        "answer": _actions(_PAID_IT),
        "ledger": [("Pay rent", "do", "paid", "2026-10-31", True, "")], "wrote": ["paid"],
        "dropped": [], "verdict": "written",
    },
    "done": {
        "words": "I returned the book",
        "held": [("Return book", "do", {})],
        "answer": _actions(_with(_PAID_IT, action="done", quote="returned the book")),
        "ledger": [("Return book", "do", "done", "", True, "")], "wrote": ["done"],
        "dropped": [], "verdict": "written",
    },
    "cancelled, with his words as the reason": {
        "words": "The dentist is off",
        "held": [("Dentist", "go", {"start_at": DENTIST})],
        "answer": _actions(_with(_PAID_IT, action="cancelled", quote="The dentist is off")),
        "ledger": [("Dentist", "go", "cancelled", DENTIST, True, "")], "wrote": ["cancelled"],
        "dropped": [], "verdict": "written",
    },
    "a move": {
        "words": "Dentist moved to Friday 4pm",
        "held": [("Dentist", "go", {"start_at": DENTIST})],
        "answer": _actions({
            "action": "move", "thing": "{h0}", "start_at": "2026-10-16T16:00:00",
            "quote": "Dentist moved to Friday 4pm", "sure": True,
        }),
        "ledger": [("Dentist", "go", "open", "2026-10-16T16:00:00-07:00", True, "")],
        "wrote": ["moved"], "dropped": [], "verdict": "written",
    },
    "a confirmation of a thing a mail made": {
        "words": "Yes, the pottery class is mine",
        "held": [("Pottery", "go", {"start_at": DENTIST, "confirmed": False})],
        "answer": _actions(_with(
            _PAID_IT, action="confirmed", quote="the pottery class is mine",
        )),
        "ledger": [("Pottery", "go", "open", DENTIST, True, "")], "wrote": ["confirmed"],
        "dropped": [], "verdict": "written",
    },
    "unsure is written unconfirmed": {
        "words": "Maybe get the visa form in by the 20th",
        "held": [],
        "answer": _actions({
            "action": "new", "kind": "do", "title": "Visa form", "due": "2026-10-20",
            "quote": "get the visa form in by the 20th", "sure": False,
        }),
        "ledger": [("Visa form", "do", "open", "2026-10-20", False, "")], "wrote": ["added"],
        "dropped": [], "verdict": "written",
    },
    "a weekly thing with no clock hour on its day": {
        "words": "Pottery every Tuesday until Nov 24",
        "held": [],
        "answer": _actions({
            "action": "new", "kind": "go", "title": "Pottery", "on_day": "2026-10-20",
            "weekly_until": "2026-11-24", "quote": "Pottery every Tuesday", "sure": True,
        }),
        "ledger": [("Pottery", "go", "open", "2026-10-20", True, "2026-11-24")],
        "wrote": ["added"], "dropped": [], "verdict": "written",
    },
    "a close and a new in one answer are both written": {
        "words": "I paid the rent and the dentist is tomorrow at 3pm",
        "held": [("Pay rent", "do", {"due": "2026-10-31"})],
        "answer": _actions(_PAID_IT, _with(_NEW, quote="the dentist is tomorrow at 3pm")),
        "ledger": [
            ("Pay rent", "do", "paid", "2026-10-31", True, ""),
            ("Dentist", "go", "open", DENTIST, True, ""),
        ],
        "wrote": ["paid", "added"], "dropped": [], "verdict": "written",
    },
    "an action that cites none of his lines is dropped": {
        "words": "Dentist tomorrow at 3pm", "held": [],
        "answer": _actions(_with(_NEW, quote="the dentist, Friday")),
        "ledger": [], "wrote": [], "dropped": ["not one of his lines"], "verdict": "none",
    },
    "a close naming no open thing is dropped": {
        "words": "I paid the rent",
        "held": [("Pay rent", "do", {"due": "2026-10-31"})],
        "answer": _actions(_with(_PAID_IT, thing="ffffff")),
        "ledger": [("Pay rent", "do", "open", "2026-10-31", True, "")], "wrote": [],
        "dropped": ["no open thing"], "verdict": "none",
    },
    "a close naming a thing that is closed already is dropped": {
        "words": "I paid the rent",
        "held": [("Pay rent", "do", {"due": "2026-10-31"}, "done")],
        "answer": _actions(_PAID_IT),
        "ledger": [("Pay rent", "do", "done", "2026-10-31", True, "")], "wrote": [],
        "dropped": ["no open thing"], "verdict": "none",
    },
    "a new thing already past when said is not new": {
        "words": "Dentist yesterday at 3pm", "held": [],
        "answer": _actions(_with(
            _NEW, start_at="2026-10-13T15:00:00", quote="Dentist yesterday at 3pm",
        )),
        "ledger": [], "wrote": [], "dropped": ["already past"], "verdict": "none",
    },
    "a close he did not say plainly is dropped": {
        "words": "I think I paid the rent",
        "held": [("Pay rent", "do", {"due": "2026-10-31"})],
        "answer": _actions(_with(_PAID_IT, sure=False, quote="I think I paid the rent")),
        "ledger": [("Pay rent", "do", "open", "2026-10-31", True, "")], "wrote": [],
        "dropped": ["plainly"], "verdict": "none",
    },
    "an open thing with the same words and time is not added again": {
        "words": "Dentist tomorrow at 3pm",
        "held": [("Dentist", "go", {"start_at": DENTIST})],
        "answer": _actions(_with(_NEW, title="dentist", quote="Dentist tomorrow at 3pm")),
        "ledger": [("Dentist", "go", "open", DENTIST, True, "")], "wrote": [],
        "dropped": ["already there"], "verdict": "none",
    },
    "a move the ledger cannot read back is dropped": {
        "words": "The rent is moved to the 2nd",
        "held": [("Pay rent", "do", {"due": "2026-10-31"})],
        "answer": _actions({
            "action": "move", "thing": "{h0}", "end_at": "2026-11-02T10:00:00",
            "start_at": "2026-11-02T09:00:00", "occurrence": "2026-11-02",
            "quote": "The rent is moved to the 2nd", "sure": True,
        }),
        "ledger": [("Pay rent", "do", "open", "2026-10-31", True, "")], "wrote": [],
        "dropped": ["not weekly"], "verdict": "none",
    },
}


def _hold_all(log: _Log, held: list[tuple[Any, ...]]) -> list[things.Thing]:
    out = []
    for title, kind, fields, *closed in held:
        one = log.hold(title, kind, **fields)
        if closed:
            things.close(
                log.conn, one.thing_id, closed[0], by="him", evidence="done", zone=ZONE,
                now=SAID - timedelta(hours=5),
            )
        out.append(one)
    return out


@pytest.mark.parametrize("case", _CASES.values(), ids=list(_CASES))
def test_a_record_is_read_after_her_answer_and_only_what_passes_reaches_the_ledger(
    log: _Log, case: dict[str, Any],
) -> None:
    """The ledger fold and the event rows say what was written; the row says what was asked."""
    model = _Model({case["words"]: case["answer"]})
    model.held = _hold_all(log, case["held"])
    record = log.turn(case["words"])
    clock = [SAID + 60 * SECOND]
    tt = _pass(log, model, clock)

    outcome = tt.read(record, clock[0], when_ready=True)

    assert outcome is not None
    assert [line.split()[0] for line in outcome.wrote] == case["wrote"]
    for reason in case["dropped"]:
        assert any(reason in line for line in outcome.dropped), (reason, outcome.dropped)
    assert len(outcome.dropped) == len(case["dropped"])
    assert log.ledger() == case["ledger"]
    # every write is the pass's, backed by his words, for his record
    assert len(log.pass_events()) == len(case["wrote"])
    for _, by, ref, evidence in log.pass_events():
        assert (by, ref) == ("pass", record)
        assert evidence.casefold() in case["words"].casefold()
    # the row keeps what the model was given and said, and which instructions asked
    verdict, tries, extractor, given, said = log.row(record)
    assert (verdict, tries, extractor) == (case["verdict"], 0, decision.EXTRACTOR)
    assert verdict == outcome.verdict
    assert case["words"] in json.loads(given)["user"]
    assert said == model.said[0]
    # nothing is read twice
    assert tt.poll_once() == 0
    assert model.asked == [case["words"]]


def test_what_the_pass_wrote_is_in_the_event_log_with_his_quote_and_the_record(log: _Log) -> None:
    """A write names the pass, the record that proved it, and his words."""
    words = "Dentist tomorrow at 3pm"
    model = _Model({words: _actions(_with(_NEW, quote=words))})
    record = log.turn(words)

    assert _pass(log, model, [SAID + 60 * SECOND]).poll_once() == 1

    (made,) = things.fold(log.conn).values()
    assert (made.source, made.source_ref, made.added_by) == ("said", record, "pass")
    assert made.rings == ()  # a pass makes no rings (ADR 0331): telling him is hers to set
    (step,) = made.history
    assert (step.what, step.by, step.evidence, step.ref) == ("added", "pass", words, record)
    assert log.verdicts() == {record: ("written", 0)}


def test_a_private_thing_is_kept_private(log: _Log) -> None:
    """A thing the model calls private is written private, so a ring for it says only its time."""
    words = "Dentist tomorrow at 3pm"
    model = _Model({words: _actions(_with(_NEW, quote=words, private=True))})
    log.turn(words)

    assert _pass(log, model, [SAID + 60 * SECOND]).poll_once() == 1

    (made,) = things.fold(log.conn).values()
    assert made.private


def test_a_cancel_keeps_his_words_as_the_reason(log: _Log) -> None:
    """A cancel is written with the quote as its reason."""
    held = log.hold("Dentist", "go", start_at=DENTIST)
    model = _Model({"The dentist is off": _actions(
        _with(
            _PAID_IT, thing=things.short_id(held), action="cancelled", quote="The dentist is off",
        ),
    )})
    log.turn("The dentist is off")

    _pass(log, model, [SAID + 60 * SECOND]).poll_once()

    (closed,) = [s for s in things.get(log.conn, held.thing_id).history if s.by == "pass"]
    assert (closed.what, closed.why) == ("cancelled", "The dentist is off")


# --- her tool wrote it already -------------------------------------------------------------


def test_a_thing_her_tool_wrote_in_the_turn_is_not_written_again(log: _Log) -> None:
    """Her tool's add sits between his record and her answer; the pass finds it and adds nothing."""
    words = "Dentist tomorrow at 3pm"
    log.say("r1", "allen", words, SAID)
    things.add(
        log.conn,
        things.Thing(things.new_id(), "Appointment at the dentist", "go", start_at=DENTIST),
        by="her", evidence="he asked", zone=ZONE, now=SAID + 5 * SECOND,
    )
    log.say("a1", "jarvis", "Kept it.", SAID + 20 * SECOND)
    model = _Model({words: _actions(_with(_NEW, quote=words, title="Dentist"))})
    tt = _pass(log, model, [SAID + 60 * SECOND])

    outcome = tt.read("r1", SAID + 60 * SECOND, when_ready=True)

    assert outcome is not None
    assert log.ledger() == [("Appointment at the dentist", "go", "open", DENTIST, True, "")]
    assert log.pass_events() == []
    assert outcome.verdict == "none"
    assert any("already there" in line for line in outcome.dropped)
    assert "Appointment at the dentist" in model.prompts[0]  # the model is told what she wrote
    assert '"what": "added"' in model.prompts[0]


def test_what_her_tool_wrote_before_his_record_is_not_the_turns(log: _Log) -> None:
    """Only the writes between his record and her answer are the turn's; the rest is just open."""
    words = "Dentist tomorrow at 3pm"
    things.add(
        log.conn, things.Thing(things.new_id(), "Appointment", "go", start_at=DENTIST),
        by="her", evidence="earlier", zone=ZONE, now=SAID - 5 * SECOND,
    )
    log.turn(words)
    model = _Model({words: _actions(_with(_NEW, quote=words, title="Dentist"))})

    _pass(log, model, [SAID + 60 * SECOND]).poll_once()

    assert '"what": "added"' not in model.prompts[0]
    assert "Appointment" in model.prompts[0]  # it is among the open things
    assert [row[0] for row in log.ledger()] == ["Appointment", "Dentist"]
    assert len(log.pass_events()) == 1


def test_a_record_read_again_does_not_write_what_it_wrote(log: _Log) -> None:
    """A crash between the ledger and the row: the second read finds its own writes."""
    words = "Dentist tomorrow at 3pm"
    model = _Model({words: _actions(_with(_NEW, quote=words, title="Dentist"))})
    record = log.turn(words)
    tt = _pass(log, model, [SAID + 60 * SECOND])

    first = tt.read(record, SAID + 60 * SECOND)
    again = tt.read(record, SAID + 120 * SECOND)

    assert first is not None
    assert again is not None
    assert first.verdict == "written"
    assert again.verdict == "none"
    assert len(log.pass_events()) == 1
    assert [row[0] for row in log.ledger()] == ["Dentist"]


# --- no doings ----------------------------------------------------------------------------


def test_what_he_did_is_read_only_as_the_actions_it_makes(log: _Log) -> None:
    """ADR 0328: no doings are asked for or written; a doings key in an answer is not read."""
    words = "I paid the rent. I had lunch with Mia at noon."
    held = log.hold("Pay rent", "do", due="2026-10-31")
    model = _Model({words: {
        "actions": [_with(_PAID, thing=things.short_id(held))],
        "doings": [{"quote": "I had lunch with Mia at noon", "tense": "past"}],
    }})
    log.turn(words)

    assert "doing" not in decision.INSTRUCTIONS
    assert _pass(log, model, [SAID + 60 * SECOND]).poll_once() == 1

    assert log.verdicts() == {"r1": ("written", 0)}
    assert [(row[0], row[2]) for row in log.ledger()] == [("Pay rent", "paid")]
    assert list(iter_events_of_types(log.conn, ("doings.stated",))) == []


# --- when a record is read -----------------------------------------------------------------


def test_what_the_model_is_given_is_the_record_the_few_before_it_her_answer_and_the_state(
    log: _Log,
) -> None:
    """The question holds his words, the messages before, her answer, things, tools and the plan."""
    held = log.hold("Pay rent", "do", due="2026-10-31", confirmed=False)
    plan.draft(
        log.conn, SAID.date(), [plan.PlanItem("item:1", "Gym", "fixed", "his", start="18:00")],
        "digest", "model",
    )
    log.say("p1", "allen", "Good morning", SAID - timedelta(hours=5))
    log.say("p2", "jarvis", "Morning. Rent is due soon.", SAID - timedelta(hours=5) + SECOND)
    log.say("p3", "mail", "A mail that is not a message", SAID - timedelta(hours=4))
    log.say("r1", "allen", "I paid the rent", SAID)
    log.say("a1", "jarvis", "Good, marked.", SAID + 10 * SECOND)
    log.say("a2", "jarvis_live", "Anything else?", SAID + 15 * SECOND)
    log.say("r2", "allen", "No", SAID + 30 * SECOND)
    log.say("a3", "jarvis", "Fine.", SAID + 40 * SECOND)
    model = _Model()

    _pass(log, model, [SAID + 60 * SECOND]).poll_once()

    assert model.asked == ["Good morning", "I paid the rent", "No"]  # each record of his
    first = model.prompts[1]
    assert "Now: 2026-10-14T14:01:00-07:00, Wednesday, America/Vancouver." in first
    assert "His message was sent at 2026-10-14T14:00:00-07:00, Wednesday." in first
    assert f'"id": "{things.short_id(held)}"' in first
    assert '"unconfirmed": true' in first
    assert 'Today\'s plan: [{"start": "18:00", "title": "Gym", "done": false}]' in first
    assert "him (2026-10-14T09:00:00-07:00): Good morning" in first
    assert "her (2026-10-14T09:00:01-07:00): Morning. Rent is due soon." in first
    assert "A mail that is not a message" not in first
    assert "in this turn: []" in first
    assert first.endswith(
        "His message, one numbered line per sentence:\n[m1] I paid the rent\n\n"
        "The assistant's answer to it:\nGood, marked.\n"
        "Anything else?",
    )
    second = model.prompts[2]
    assert "him (2026-10-14T14:00:00-07:00): I paid the rent" in second
    assert second.endswith(
        "His message, one numbered line per sentence:\n[m1] No\n\n"
        "The assistant's answer to it:\nFine.",
    )


def test_the_model_is_shown_what_he_could_mean_not_the_outings_that_are_past() -> None:
    """A past thing to go to stays open in the ledger, but no answer can be about it."""
    day = SAID.date()

    def go(  # noqa: PLR0913 - the times a thing can have
        title: str, *, start_at: str = "", end_at: str = "", on_day: str = "",
        last_day: str = "", weekly_until: str = "",
    ) -> things.Thing:
        return things.Thing(
            things.new_id(), title, "go", start_at=start_at, end_at=end_at, on_day=on_day,
            last_day=last_day, weekly_until=weekly_until,
        )

    ended = go("Party", start_at="2026-10-13T20:00:00-07:00", end_at="2026-10-14T01:00:00-07:00")
    trip = go("Trip", on_day="2026-10-12", last_day="2026-10-14")
    gone = [
        go("Dinner", start_at="2026-10-13T19:00:00-07:00"),
        go("Fair", on_day="2026-10-13"),
        go("Course", start_at="2026-09-01T10:00:00-07:00", weekly_until="2026-10-13"),
    ]
    late = things.Thing(things.new_id(), "Fix the bike", "do", start_at="2026-10-01T09:00:00-07:00")
    weekly = go("Pottery", start_at="2026-09-02T19:00:00-07:00", weekly_until="2026-11-24")
    soon = go("Dentist", start_at="2026-10-14T09:00:00-07:00")
    bill = things.Thing(things.new_id(), "Pay rent", "do", due="2026-10-31")
    untimed = go("See Sam")
    held = [*gone, late, weekly, ended, trip, soon, bill, untimed]

    assert decision.shown(held, day, ZONE) == [late, weekly, ended, trip, soon, bill, untimed]

    # Over the limit, the outings furthest ahead give way; every to-do stays.
    ahead = [
        go(f"Class {n}", start_at=(SAID + timedelta(days=n)).isoformat()) for n in range(1, 120)
    ]
    chores = [things.Thing(things.new_id(), f"Chore {n}", "do") for n in range(5)]
    picked = decision.shown([*ahead, *chores], day, ZONE)
    assert len(picked) == decision.MAX_OPEN
    assert picked == [*ahead[: decision.MAX_OPEN - 5], *chores]


def test_a_record_is_read_as_soon_as_she_has_answered_and_after_ten_minutes_without_one(
    log: _Log,
) -> None:
    """No answer yet: the record stays pending until she speaks, or until ANSWER_WAIT_S."""
    log.turn("I paid the rent", answer=None)
    model = _Model()
    clock = [SAID + 30 * SECOND]
    tt = _pass(log, model, clock)

    assert tt.poll_once() == 0
    assert log.verdicts() == {"r1": ("pending", 0)}

    log.say("a1", "jarvis", "Marked.", SAID + 40 * SECOND)
    clock[0] = SAID + 41 * SECOND
    assert tt.poll_once() == 1
    assert model.asked == ["I paid the rent"]
    assert model.prompts[0].endswith("The assistant's answer to it:\nMarked.")

    later = SAID + timedelta(hours=1)
    log.turn("Dentist tomorrow at 3pm", answer=None, at=later)
    clock[0] = later + timedelta(seconds=runtime.ANSWER_WAIT_S - 1)
    assert tt.poll_once() == 0
    clock[0] = later + timedelta(seconds=runtime.ANSWER_WAIT_S)
    assert tt.poll_once() == 1
    assert model.asked == ["I paid the rent", "Dentist tomorrow at 3pm"]
    assert model.prompts[1].endswith(
        "His message, one numbered line per sentence:\n[m1] Dentist tomorrow at 3pm\n\n"
        "The assistant's answer to it:\n",
    )


def test_a_record_with_a_picture_waits_for_its_note_or_for_the_window_to_pass(log: _Log) -> None:
    """With the note in it is read at once, and a line of the note may back it; else it waits."""
    quote = "your road test is booked for 2026-10-22 at 09:30"
    road_test = {
        "action": "new", "kind": "go", "title": "Road test", "start_at": "2026-10-22T09:30:00",
        "quote": quote, "sure": True,
    }
    log.picture("r1", "att1", SAID, "Passed the written one")
    log.say("a1", "jarvis", "Congratulations.", SAID + 20 * SECOND)
    log.picture("r2", "att2", SAID + timedelta(hours=1))
    log.say("a2", "jarvis", "Is that the road test?", SAID + timedelta(hours=1, seconds=20))
    model = _Model({"Passed the written one": _actions(road_test)})
    clock = [SAID + 60 * SECOND]
    tt = _pass(log, model, clock)

    assert tt.poll_once() == 0
    assert model.asked == []
    assert log.verdicts()["r1"] == ("pending", 0)

    log.note("att1", f"A screenshot. Text: {quote}.")
    assert tt.poll_once() == 1
    assert f"[p1] A screenshot.\n[p2] Text: {quote}.\n" in model.prompts[0]
    (made,) = things.fold(log.conn).values()
    assert (made.title, made.source, made.source_ref, made.start_at) == (
        "Road test", "picture", "r1", "2026-10-22T09:30:00-07:00",
    )
    assert made.history[0].evidence == f"Text: {quote}."

    # the second picture has no note: it waits out the window after the turn ended
    answered = SAID + timedelta(hours=1, seconds=20)
    clock[0] = answered + timedelta(seconds=runtime.PICTURE_WAIT_S - 1)
    assert tt.poll_once() == 0
    clock[0] = answered + timedelta(seconds=runtime.PICTURE_WAIT_S)
    assert tt.poll_once() == 1  # no words and no note: nothing to read, so no question
    assert model.asked == ["Passed the written one"]
    assert log.verdicts()["r2"] == ("none", 0)


def test_a_picture_with_words_and_no_note_is_read_by_its_words_once_the_window_has_passed(
    log: _Log,
) -> None:
    """The window over, a record is read by its words alone."""
    log.picture("r1", "att1", SAID, "I paid the rent")
    log.say("a1", "jarvis", "Marked.", SAID + 20 * SECOND)
    model = _Model()
    clock = [SAID + 20 * SECOND + timedelta(seconds=runtime.PICTURE_WAIT_S)]

    assert _pass(log, model, clock).poll_once() == 1
    assert model.asked == ["I paid the rent"]
    assert (
        "Notes on the pictures he sent with his message, one numbered line per sentence:\n\n"
        "His message, one numbered line per sentence:\n[m1] I paid the rent"
    ) in model.prompts[0]


def test_live_voice_answers_and_two_records_before_one_answer_are_read_all_the_same(
    log: _Log,
) -> None:
    """Two rows of his in a row, then her spoken answer: each is read, and each sees the other."""
    log.say("r1", "allen", "I paid the rent", SAID)
    log.say("r2", "allen", "and the dentist is tomorrow at 3pm", SAID + 6 * SECOND)
    log.say("z1", "jarvis_live", "Noted both.", SAID + 20 * SECOND)
    model = _Model({
        "and the dentist is tomorrow at 3pm": _actions(
            _with(_NEW, quote="the dentist is tomorrow at 3pm"),
        ),
    })

    assert _pass(log, model, [SAID + 60 * SECOND]).poll_once() == 2

    assert model.asked == ["I paid the rent", "and the dentist is tomorrow at 3pm"]
    assert all(p.endswith("Noted both.") for p in model.prompts)
    assert "him (2026-10-14T14:00:00-07:00): I paid the rent" in model.prompts[1]
    assert [row[0] for row in log.ledger()] == ["Dentist"]
    assert log.verdicts() == {"r1": ("none", 0), "r2": ("written", 0)}


def test_a_record_with_nothing_in_it_is_marked_without_a_question(log: _Log) -> None:
    """Filler or an empty record is nothing to read."""
    log.say("r1", "allen", "", SAID)
    log.say("r2", "allen", lang.t("attach.no_words"), SAID + 5 * SECOND)
    log.say("a1", "jarvis", "Hello.", SAID + 20 * SECOND)
    model = _Model()

    assert _pass(log, model, [SAID + 60 * SECOND]).poll_once() == 2

    assert model.asked == []
    assert log.verdicts() == {"r1": ("none", 0), "r2": ("none", 0)}


def test_only_his_records_are_taken_up(log: _Log) -> None:
    """Her rows, mail rows and picture notes are never read as his."""
    log.say("m1", "mail", "A mail", SAID)
    log.say("a1", "jarvis", "Hello", SAID + SECOND)
    log.say("n1", "picture_note", "[Picture note]\nA note", SAID + 2 * SECOND)
    log.say("l1", "jarvis_live", "Live", SAID + 3 * SECOND)
    model = _Model()

    assert _pass(log, model, [SAID + 60 * SECOND]).poll_once() == 0
    assert log.verdicts() == {}
    assert model.asked == []


def test_a_first_start_reads_seven_days_back_and_a_later_start_only_the_last_day(
    log: _Log,
) -> None:
    """No row at all: the last backfill days. Rows exist: the last day."""
    now = SAID + timedelta(days=8)
    log.turn("eight days ago", at=now - timedelta(days=8, minutes=1))
    log.turn("six days ago", at=now - timedelta(days=6))
    log.turn("this morning", at=now - timedelta(hours=3))
    model = _Model()
    clock = [now]

    assert _pass(log, model, clock).poll_once() == 2
    assert model.asked == ["six days ago", "this morning"]

    # the daemon is off for three days; when it comes back only the last day is looked through
    log.turn("while it was off", at=now + timedelta(hours=1))
    clock[0] = now + timedelta(days=3)
    log.turn("an hour ago", at=clock[0] - timedelta(hours=1))
    restarted = _pass(log, model, clock)
    assert restarted.poll_once() == 1
    assert model.asked[2:] == ["an hour ago"]
    assert "r4" not in log.verdicts()


def test_the_rescan_takes_up_a_record_of_the_last_day_that_has_no_row(log: _Log) -> None:
    """Every ten minutes the last day is looked through for records the loop does not hold."""
    record = log.turn("I paid the rent")
    model = _Model()
    clock = [SAID + 60 * SECOND]
    tt = _pass(log, model, clock)
    assert tt.poll_once() == 1

    with closing(open_memory_db(log.memory)) as conn, conn:
        conn.execute("DELETE FROM turn_things WHERE record_id = ?", (record,))  # the row is lost
    clock[0] += timedelta(seconds=runtime.RESCAN_S - 1)
    assert tt.poll_once() == 0
    clock[0] += 2 * SECOND
    assert tt.poll_once() == 1
    assert model.asked == ["I paid the rent", "I paid the rent"]
    assert log.verdicts() == {record: ("none", 0)}


def test_an_outage_costs_one_record_one_try_and_a_record_is_tried_three_times(log: _Log) -> None:
    """A failed call marks its record an error and stops the loop; errors come back after a wait."""
    log.turn("I paid the rent")
    log.turn("Dentist tomorrow at 3pm")
    model = _Model({"I paid the rent": RuntimeError("down")})
    clock = [SAID + 60 * SECOND]
    tt = _pass(log, model, clock)

    assert tt.poll_once() == 0
    assert model.asked == ["I paid the rent"]
    assert log.verdicts() == {"r1": ("error", 1), "r2": ("pending", 0)}
    assert tt.poll_once() == 0  # the loop waits
    assert model.asked == ["I paid the rent"]

    clock[0] += timedelta(seconds=runtime.RETRY_AFTER_S)
    assert tt.poll_once() == 0
    assert model.asked == ["I paid the rent", "I paid the rent"]  # the older record comes first
    clock[0] += timedelta(seconds=runtime.RETRY_AFTER_S)
    assert tt.poll_once() == 0
    assert log.verdicts()["r1"] == ("error", 3)
    assert model.asked.count("I paid the rent") == 3
    assert model.asked.count("Dentist tomorrow at 3pm") == 0

    clock[0] += timedelta(seconds=runtime.RETRY_AFTER_S)
    assert tt.poll_once() == 1  # r1 is given up; the next record is read
    assert model.asked.count("I paid the rent") == 3
    assert log.verdicts() == {"r1": ("error", 3), "r2": ("none", 0)}
    verdict, tries, extractor, given, said = log.row("r1")
    assert (verdict, tries, extractor, said) == ("error", 3, decision.EXTRACTOR, "")
    assert "I paid the rent" in json.loads(given)["user"]  # what it was given is kept


def test_an_answer_that_is_no_json_object_is_an_error_tried_again(log: _Log) -> None:
    """The model answered with prose: the row keeps what it said, and a later try may do better."""
    log.turn("I paid the rent")
    model = _Model({"I paid the rent": "Sure! I paid the rent is a fine sentence."})
    clock = [SAID + 60 * SECOND]
    tt = _pass(log, model, clock)

    assert tt.poll_once() == 1
    assert log.row("r1")[0:2] == ("error", 1)
    assert log.row("r1")[4] == "Sure! I paid the rent is a fine sentence."
    model.answers["I paid the rent"] = NOTHING
    assert tt.poll_once() == 0
    clock[0] += timedelta(seconds=runtime.RETRY_AFTER_S)
    assert tt.poll_once() == 1
    assert log.row("r1")[0:2] == ("none", 1)
    assert log.row("r1")[4] == json.dumps(NOTHING)


def test_a_write_the_ledger_cannot_take_is_dropped_not_a_failure(log: _Log) -> None:
    """The ledger refuses what cannot be read back; the pass says so and goes on."""
    held = log.hold("Pottery", "go", start_at="2026-10-20T19:00:00-07:00")
    model = _Model({"The class is moved": _actions(
        {"action": "move", "thing": things.short_id(held), "start_at": "2026-10-27T19:00:00",
         "occurrence": "2026-10-27", "quote": "The class is moved", "sure": True},
        _with(_NEW, quote="The class is moved", title="Studio"),
    )})
    log.turn("The class is moved")
    tt = _pass(log, model, [SAID + 60 * SECOND])

    outcome = tt.read("r1", SAID + 60 * SECOND)

    assert outcome is not None
    assert outcome.verdict == "written"
    assert [row[0] for row in log.ledger()] == ["Pottery", "Studio"]
    assert any("not weekly" in line for line in outcome.dropped)
    assert log.row("r1")[0] == "written"


def test_a_pass_with_no_model_reads_nothing(log: _Log) -> None:
    """The preset is missing or malformed: nothing is marked and nothing is read."""
    log.turn("I paid the rent")
    tt = TurnThings(
        TurnThingsSettings(7, "gone"), log.memory, log.paths.event_log, None, ZONE,
    )
    tt.now = lambda: SAID + 60 * SECOND

    assert tt.poll_once() == 0
    assert log.verdicts() == {}


def test_read_stops_when_the_model_is_down(log: _Log) -> None:
    """``read`` raises ``ModelFailed`` after marking the error, for a caller that replays."""
    record = log.turn("I paid the rent")
    tt = _pass(log, _Model({"I paid the rent": RuntimeError("down")}), [SAID])
    with pytest.raises(ModelFailed):
        tt.read(record, SAID + 60 * SECOND)
    assert log.verdicts() == {record: ("error", 1)}


@pytest.mark.parametrize(
    ("answered", "pictures", "notes", "age_s", "ready"),
    [
        (True, 0, 0, 0, True),
        (False, 0, 0, runtime.ANSWER_WAIT_S - 1, False),
        (False, 0, 0, runtime.ANSWER_WAIT_S, True),
        (True, 1, 0, 0, False),
        (True, 1, 1, 0, True),
        (True, 2, 1, 0, False),
        (True, 2, 1, runtime.PICTURE_WAIT_S, True),
        (False, 1, 0, runtime.ANSWER_WAIT_S, False),
        (False, 1, 0, runtime.PICTURE_WAIT_S, True),
    ],
)
def test_a_record_is_ready_when_answered_with_its_notes_in_or_when_the_waits_are_over(
    answered: bool, pictures: int, notes: int, age_s: float, ready: bool,  # noqa: FBT001
) -> None:
    """The two waits: her answer (600 s) and the picture notes (the window and the retry)."""
    turn = marks.Turn(
        "r1", SAID, "words", tuple(f"a{n}" for n in range(pictures)),
        tuple(f"n{n}" for n in range(notes)), (), "ok" if answered else "",
        SAID if answered else None,
    )
    assert TurnThings.ready(turn, SAID + timedelta(seconds=age_s)) is ready


def test_the_picture_wait_covers_the_window_and_the_note_retry() -> None:
    """The note job waits ten minutes for his next words and retries once a minute later."""
    assert runtime.PICTURE_WAIT_S == 600 + 2 * 60


# --- the replay ----------------------------------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_replay_reads_the_stretch_on_a_scratch_copy_and_touches_nothing_else(
    log: _Log, tmp_path: Path,
) -> None:
    """Records are read against the ledger as it stood; writes go to the scratch copies only."""
    held = log.hold("Pay rent", "do", due="2026-10-31")
    model = _Model({
        "I paid the rent": _actions(_with(_PAID_IT, thing=things.short_id(held))),
        "Dentist tomorrow at 3pm": _actions(_with(_NEW, quote="Dentist tomorrow at 3pm")),
    })
    log.turn("An old line", at=SAID - timedelta(days=3))
    log.turn("I paid the rent", at=SAID)
    log.turn("Dentist tomorrow at 3pm", at=SAID + timedelta(hours=1))
    log.conn.close()
    before = (_sha(log.paths.event_log), _sha(log.memory))
    scratch = tmp_path / "scratch"

    lines = turn_things_replay.replay(
        log.paths.event_log, log.memory, scratch, since=SAID - timedelta(hours=1),
        until=SAID + timedelta(days=1), zone=ZONE, ask=model,
    )

    assert [(line["words"], line["verdict"], len(line["wrote"])) for line in lines] == [
        ("I paid the rent", "written", 1), ("Dentist tomorrow at 3pm", "written", 1),
    ]
    assert lines[0]["wrote"][0].startswith("paid ")
    assert lines[0]["asked"] is True
    assert (_sha(log.paths.event_log), _sha(log.memory)) == before
    with closing(open_event_log(scratch / "events.db")) as copy:
        folded = {t.title: t for t in things.fold(copy).values()}
    assert folded["Pay rent"].state == "paid"
    assert folded["Pay rent"].history[-1].by == "pass"
    assert folded["Dentist"].added_by == "pass"
    with closing(open_event_log(log.paths.event_log)) as live:
        assert {t.title: t.state for t in things.fold(live).values()} == {"Pay rent": "open"}
    assert marks.has_marks(scratch / "memory.db")


def test_the_replay_without_a_model_asks_nothing_and_says_how_large_each_question_is(
    log: _Log, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The default run answers nothing and writes nothing but its scratch copy."""
    log.turn("I paid the rent")
    log.conn.close()

    code = turn_things_replay.main([
        "--event-log", str(log.paths.event_log), "--memory", str(log.memory),
        "--since", "2026-10-14", "--until", "2026-10-14", "--zone", "America/Vancouver",
        "--out", str(tmp_path / "out"),
    ])

    printed = capsys.readouterr().out.splitlines()
    assert code == 0
    line = json.loads(printed[0])
    assert (line["words"], line["asked"], line["verdict"], line["wrote"]) == (
        "I paid the rent", True, "none", [],
    )
    assert line["est_input_tokens"] > 0
    summary = json.loads(printed[1])
    assert (summary["records"], summary["asked"], summary["wrote"]) == (1, 1, 0)
    assert printed[2].startswith("no model was called")


def test_a_live_replay_fills_the_key_from_the_runtime_roots_saved_keys(
    log: _Log, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With --live the saved keys are loaded first, as the daemon does; a dry run loads none."""
    log.turn("I paid the rent")
    log.conn.close()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    loaded: list[Path] = []
    monkeypatch.setattr(turn_things_replay, "load_env_file", loaded.append)
    monkeypatch.setattr(turn_things_replay, "_asker", lambda _config, _preset: _Model())
    flags = [
        "--event-log", str(log.paths.event_log), "--memory", str(log.memory),
        "--since", "2026-10-14", "--zone", "America/Vancouver",
    ]

    assert turn_things_replay.main([*flags, "--out", str(tmp_path / "dry")]) == 0
    assert loaded == []
    assert turn_things_replay.main([*flags, "--live", "--out", str(tmp_path / "live")]) == 0
    assert loaded == [tmp_path / "home" / ".jarvis"]


def test_the_replay_refuses_the_live_runtime_root(
    log: _Log, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files under the default runtime root, or a scratch folder there, are never replayed."""
    home = tmp_path / "home"
    live = home / ".jarvis"
    live.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (live / "memory.db").write_bytes(log.memory.read_bytes())
    (live / "mac_events.db").write_bytes(log.paths.event_log.read_bytes())
    scratch = tmp_path / "scratch"

    for event_log, memory, out in (
        (live / "mac_events.db", log.memory, scratch),
        (log.paths.event_log, live / "memory.db", scratch),
        (log.paths.event_log, log.memory, live / "scratch"),
    ):
        with pytest.raises(SystemExit, match="live runtime root"):
            turn_things_replay.replay(
                event_log, memory, out, since=SAID, until=SAID + timedelta(days=1), zone=ZONE,
                ask=_Model(),
            )
    with pytest.raises(SystemExit, match="live runtime root"):
        turn_things_replay.main([
            "--event-log", str(live / "mac_events.db"), "--memory", str(log.memory),
            "--since", "2026-10-14",
        ])
    assert not scratch.exists()
    assert not (live / "scratch").exists()


# --- the boot ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "built"),
    [
        ("", False),
        ("turn_things:\n  enabled: false\n  preset: haiku-bg\n  backfill_days: 7\n", False),
        ("turn_things:\n  enabled: true\n  preset: haiku-bg\n  backfill_days: 3\n", True),
    ],
)
def test_the_pass_is_built_only_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: str, built: bool,  # noqa: FBT001
) -> None:
    """Off in the shipped config: no pass is built and none runs."""
    root = tmp_path / "root"
    bootstrap_runtime(root)
    (root / "settings.yaml").write_text(settings, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime_app = bootstrap_runtime_app(runtime_root=root)
    try:
        assert isinstance(runtime_app.turn_things, TurnThings) is built
    finally:
        runtime_app.conn.close()


@pytest.mark.parametrize(
    "block",
    [
        "backfill_days: 0",
        "backfill_days: 61",
        "backfill_days: seven",
        "preset: ''",
    ],
)
def test_a_bad_value_stops_the_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: str,
) -> None:
    """Bad settings are named at boot, not met at the first record."""
    root = tmp_path / "root"
    bootstrap_runtime(root)
    (root / "settings.yaml").write_text(
        f"turn_things:\n  enabled: true\n  {block}\n", encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(RuntimeBootstrapError, match="turn_things"):
        bootstrap_runtime_app(runtime_root=root)


def test_the_shipped_config_has_the_pass_off_and_on_sonnet() -> None:
    """``config/jarvis.yaml``: his words go to the provider, so the pass is off until he says so."""
    shipped = Path(__file__).resolve().parents[2] / "config" / "jarvis.yaml"
    config = yaml.safe_load(shipped.read_text(encoding="utf-8"))
    assert config["turn_things"] == {"enabled": False, "preset": "sonnet", "backfill_days": 7}
    assert config["llm"]["presets"]["sonnet"]["model"].startswith("claude-sonnet-")
