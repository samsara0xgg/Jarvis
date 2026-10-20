"""ADR 0302, 0310 — the doing lane's writer: clues, the prompt, the gates, the run, the schedule.

Real memory.db, Event Log, day-line fold and cut in ``tmp_path``; the model is the only fake, a
client that answers from a function of the prompt it was shown and records what it was asked.
The checks, as tables of input to expected output:

- the Mac's foreground: the minutes each app, site or document was in front in each quarter hour,
  the seconds typed in it, another device's spans left out;
- the clues: each kind of day-line item that holds one, his words and hers, the work state's
  screen, interview mail and his answers (this day's, and earlier ones near a place he stayed at),
  bounded in count and in length, and a source that fails leaving the rest;
- what the writer is shown: the times it may name, the segments with their facts, the clues
  with ids and local times, the language of the titles;
- the gates, one rejection each, and what an accepted answer is stored as;
- the run: appended with its draft flag, cover, model and digest, served by the day page up to
  what it covers, his answers over it; a rejected or failed call stores nothing and the run before
  it stays;
- the digest, which the clock does not move;
- a short run (ADR 0310): the spans before the last kept and shown as context only, the window
  from where the last one starts, the questions counted over the day;
- the schedule, on a fake clock: a run at most every two hours and only when the inputs changed,
  after the first one only the newest part, at once when he asks (her tool and the timeline's
  route), a final run once the day is over, a rewrite when late rows change a past day;
- the replay script, with and without ``--dry-run``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.doing_run import (
    MAX_QUESTIONS,
    MAX_SPAN_CLUES,
    TITLE_MAX_CHARS,
    Brief,
    build_messages,
    inputs_digest,
    narrow,
    read_run,
    times,
)
from jarvis.decision.llm import ChatResult
from jarvis.execution.tidy_tool import build_tidy_tool
from jarvis.runtime import doing_run as runtime
from jarvis.runtime.doing_run import (
    CHECK_EVERY,
    SHORT_EVERY,
    DoingSchedule,
    DoingSettings,
    compose_run,
    read_brief,
    store_run,
)
from jarvis.runtime.inherent_loop import _read_day
from jarvis.state import core_memory, doing_about, places, routines
from jarvis.state import picture_notes as note_records
from jarvis.state.attachments import AttachmentRef
from jarvis.state.day_line import DaySources, day_window
from jarvis.state.doing import CATEGORIES, CLUE_KINDS, cut, fact_spans
from jarvis.state.doing_clues import (
    CLUE_TEXT_MAX_CHARS,
    MAX_CLUES_PER_KIND,
    MAX_WORDS_CLUES,
    Clue,
    gather,
)
from jarvis.state.doing_signals import SIGNALS_MAX_CHARS, Quarter, foreground
from jarvis.state.doing_signals import gather as gather_signals
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.job_ledger import upsert_mail
from jarvis.state.memory_db import (
    append_doing_answer,
    append_doing_run,
    latest_doing_run,
    open_memory_db,
    remember_fact,
)
from jarvis.state.shipments import Shipments
from scripts import doing_replay
from tests.integration.test_timeline_actions import _peers, _refused_without_credentials

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

VANCOUVER = ZoneInfo("America/Vancouver")
DAY = date(2026, 10, 7)
KEY = DAY.isoformat()
MIN = 60_000
CONFIG = Path(__file__).resolve().parents[2] / "config" / "jarvis.yaml"


def _at(hour: int, minute: int = 0, *, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hour, minute), VANCOUVER)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp()) * 1000


NOW = _at(15)


# --- the world ---------------------------------------------------------------------------------


@dataclass
class World:
    """The stores of one day, and the sources the day is folded from."""

    memory: Path
    log: Path
    sources: DaySources


def _visit(  # noqa: PLR0913 — a visit is these six things
    log: Path, uid: str, arrived: datetime, departed: datetime | None, place: str,
    lat: float = 1.0,
) -> None:
    payload: dict[str, Any] = {
        "lat": lat, "lng": 2.0, "accuracy_m": 30.0, "arrived_at_ms": _ms(arrived), "place": place,
    }
    if departed is not None:
        payload["departed_at_ms"] = _ms(departed)
    with closing(open_event_log(log)) as conn:
        emit_event(
            conn, type="phone.visit_observed", payload=payload, ts_epoch_ms=_ms(arrived),
            event_uid=uid, ingestion_node="phone",
        )


def _say(memory: Path, rid: str, moment: datetime, source: str, text: str) -> None:
    with closing(open_memory_db(memory)) as conn, conn:
        conn.execute(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
            (rid, moment.isoformat(timespec="seconds"), source, text),
        )


def _screen(log: Path, moment: datetime, now: str | None, *activities: str) -> None:
    item = {
        "now": None if now is None else {"text": now, "basis": "observed", "refs": []},
        "activities": [{"text": text} for text in activities],
    }
    with closing(open_event_log(log)) as conn:
        emit_event(
            conn, type="work_state.revised", ts_epoch_ms=_ms(moment),
            payload={"trigger": "test", "item": item},
        )


def _picture(world: World, name: str, moment: datetime, note: str) -> None:
    with closing(open_event_log(world.log)) as conn:
        emit_event(
            conn, type="surface.user_intent", ts_epoch_ms=_ms(moment),
            correlation={"turn_id": f"T-{name}"},
            payload={"transcript": "x", "turn_id": f"T-{name}", "attachments": [name]},
        )
    ref = AttachmentRef(name, "image", "IMG_1.png", 1, "image/png")
    note_records.append(world.memory, ref, note)


@pytest.fixture
def world(tmp_path: Path) -> World:
    """10-07: the library 08:00-11:30, a cafe 12:00-13:00, the library 13:30-14:45; it is 15:00.

    He said what he was doing at 09:05, the work state saw his screen at 10:00, and a picture of
    noodles came at 12:20.
    """
    memory, log = tmp_path / "memory.db", tmp_path / "events.db"
    open_memory_db(memory).close()
    open_event_log(log).close()
    places.save(memory, name="UVic Library", lat=1.0, lng=2.0, role="school", origin="user")
    _visit(log, "v1", _at(8), _at(11, 30), "UVic Library")
    _visit(log, "v2", _at(12), _at(13), "Corner Cafe", lat=1.01)
    _visit(log, "v3", _at(13, 30), _at(14, 45), "UVic Library")
    _say(memory, "r1", _at(9, 5), "allen", "我在图书馆写论文")
    _say(memory, "r2", _at(9, 6), "jarvis", "好的，加油")
    _screen(log, _at(10), "Writing the thesis in Overleaf")  # what he is doing lately
    world = World(memory, log, DaySources(VANCOUVER, None, memory))
    _picture(world, "noodles", _at(12, 20), "A bowl of noodles on a wooden table")
    return world


@pytest.fixture
def brief(world: World) -> Brief:
    """Brief."""
    found = read_brief(DAY, world.sources, world.log, NOW)
    assert found is not None
    return found


# --- the fake model ----------------------------------------------------------------------------


class _Writer:
    """Stands in for the doing preset's client: ``reply`` maps the prompt to its answer."""

    provider = "anthropic"
    model = "fake-haiku"

    def __init__(self, reply: Callable[[str], str]) -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.asked: list[list[dict[str, Any]]] = []

    def chat(self, *, messages: list[dict[str, Any]], system: str, **_kwargs: object) -> ChatResult:
        prompt = str(messages[0]["content"])
        self.prompts.append(prompt)
        self.asked.append(messages)
        self.systems.append(system)
        return ChatResult(
            text=self.reply(prompt), tool_calls=(), finish_reason="stop", input_tokens=100,
            output_tokens=50, raw={}, model_used=self.model, tokens_in=100, tokens_out=50,
        )


def _labels(prompt: str) -> dict[str, str]:
    """``"HH:MM:SS"`` to the ``tN`` the prompt gave it."""
    return {m[2]: m[1] for m in re.finditer(r"^(t\d+) (\d\d:\d\d:\d\d)$", prompt, re.MULTILINE)}


def _clue(prompt: str, needle: str) -> str:
    """The id of the clue whose line holds ``needle``."""
    return re.search(rf"^(c\d+) \|[^\n]*{re.escape(needle)}", prompt, re.MULTILINE)[1]  # type: ignore[index]


def _span(prompt: str, start: str, end: str, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    labels = _labels(prompt)
    return {
        "from": labels[start], "to": labels[end], "kind": "doing", "title": "Write the thesis",
        "detail": None, "category": "study", "basis": "inferred", "confidence": "medium",
        "clues": [], "question": None, **fields,
    }


def _good(prompt: str) -> dict[str, Any]:
    """An answer for the world at 15:00 that passes every gate."""
    return {"spans": [
        _span(prompt, "06:00:00", "08:00:00", kind="unknown", title=None, category="home",
              confidence="low"),
        _span(prompt, "08:00:00", "11:30:00", title="写论文", basis="stated", confidence="high",
              clues=[_clue(prompt, "图书馆写论文"), _clue(prompt, "Overleaf")],
              detail="He said so at 09:05."),
        _span(prompt, "11:30:00", "12:00:00", kind="unknown", title=None, category="unknown",
              confidence="low", question={"text": "11:30 到 12:00 你在做什么？",
                                          "options": ["走路", "买咖啡"]}),
        _span(prompt, "12:00:00", "13:00:00", title="吃午饭", category="meal",
              clues=[_clue(prompt, "noodles")]),
        _span(prompt, "13:00:00", "14:45:00", basis="observed", title="写论文"),
        _span(prompt, "14:45:00", "15:00:00", kind="unknown", title=None, category="unknown",
              confidence="low"),
    ]}


def _tile(prompt: str) -> str:
    """An answer for any day: one span per segment, said plainly."""
    spans = [
        {
            "from": one[1], "to": one[2], "kind": "doing", "title": "Work on it", "detail": None,
            "category": "work", "basis": "inferred", "confidence": "low", "clues": [],
            "question": None,
        }
        for one in re.finditer(r"^(t\d+)-(t\d+) \|", prompt, re.MULTILINE)
    ]
    return json.dumps({"spans": spans})


def _good_text(prompt: str) -> str:
    return json.dumps(_good(prompt), ensure_ascii=False)


def _recorder(tmp_path: Path) -> tuple[CostRecorder, sqlite3.Connection]:
    conn = open_event_log(tmp_path / "cost.db")
    return CostRecorder(conn, pricing_table={}), conn


SETTINGS = DoingSettings(preset="p", prompt="write what he was doing")
CLOCKED = replace(SETTINGS, clock_drafts=True)


# --- the clues ---------------------------------------------------------------------------------

WINDOW = (_ms(_at(6)), _ms(_at(15)))
ITEM_CASES = {
    "a picture with its note": (
        {"kind": "picture", "start_ms": _ms(_at(12, 20)), "end_ms": _ms(_at(12, 20)),
         "note": "A bowl of noodles"},
        [Clue("picture", _ms(_at(12, 20)), "A bowl of noodles")],
    ),
    "a picture with no note": (
        {"kind": "picture", "start_ms": _ms(_at(12, 20)), "end_ms": _ms(_at(12, 20))},
        [Clue("picture", _ms(_at(12, 20)), "sent a picture")],
    ),
    "a calendar event": (
        {"kind": "event", "start_ms": _ms(_at(9)), "end_ms": _ms(_at(10)), "title": "CS lecture"},
        [Clue("calendar", _ms(_at(9)), "CS lecture (09:00-10:00)")],
    ),
    "an item of the plan": (
        {"kind": "event", "source": "plan", "place": "Gym", "start_ms": _ms(_at(14)),
         "end_ms": _ms(_at(14, 45)), "title": "Workout"},
        [Clue("plan", _ms(_at(14)), "Workout at Gym (14:00-14:45)")],
    ),
    "a commitment made from mail": (
        {"kind": "event", "source": "mail", "place": "1150 McKenzie", "start_ms": _ms(_at(13, 30)),
         "end_ms": _ms(_at(14, 15)), "title": "ICBC knowledge test"},
        [Clue("mail", _ms(_at(13, 30)), "ICBC knowledge test at 1150 McKenzie (13:30-14:15)")],
    ),
    "a commitment he told her of": (
        {"kind": "event", "source": "said", "start_ms": _ms(_at(14)), "end_ms": None,
         "title": "Dinner with Pat"},
        [Clue("words", _ms(_at(14)), "he told her: Dinner with Pat")],
    ),
    "a payment": (
        {"kind": "payment", "start_ms": _ms(_at(10)), "end_ms": _ms(_at(10)), "direction": "out",
         "amount": "12.50", "currency": "CAD", "counterparty": "Starbucks", "text": "latte"},
        [Clue("payment", _ms(_at(10)), "out 12.50 CAD Starbucks latte")],
    ),
    "a Claude chat": (
        {"kind": "claude_chat", "start_ms": _ms(_at(10)), "end_ms": _ms(_at(10, 40)),
         "text": "Thesis outline", "messages": 12},
        [Clue("chat", _ms(_at(10)), "Claude chat Thesis outline, 12 messages (10:00-10:40)")],
    ),
    "the night's letter on his Claude chats": (
        {"kind": "claude_day", "start_ms": _ms(_at(9)), "end_ms": _ms(_at(11)),
         "text": "Thesis; Visa"},
        [Clue("chat", _ms(_at(9)), "Claude chats of the day: Thesis; Visa")],
    ),
    "a Claude Code session and its recap": (
        {"kind": "code", "start_ms": _ms(_at(9)), "end_ms": _ms(_at(11)), "project": "Jarvis",
         "title": "Doing lane", "detail": "Wrote the gates"},
        [
            Clue("code", _ms(_at(9)), "Claude Code session in Jarvis: Doing lane (09:00-11:00)"),
            Clue("code", _ms(_at(11)), "Wrote the gates"),
        ],
    ),
    "his TimeSink note for a stretch away from his Mac": (
        {"kind": "away_note", "start_ms": _ms(_at(12)), "end_ms": _ms(_at(13)),
         "title": "dinner"},
        [Clue("away_note", _ms(_at(12)), "he wrote in TimeSink: dinner (12:00-13:00)")],
    ),
    "a stay is a fact, not a clue": (
        {"kind": "stay", "start_ms": _ms(_at(8)), "end_ms": None, "lat": 1.0, "lng": 2.0}, [],
    ),
    "a work block is a fact": (
        {"kind": "work", "start_ms": _ms(_at(9)), "end_ms": _ms(_at(10))}, [],
    ),
    "a talk says only how many turns": (
        {"kind": "talk", "start_ms": _ms(_at(9)), "end_ms": _ms(_at(9, 10)), "turns": 4}, [],
    ),
    "an item before the window is not of this day's waking part": (
        {"kind": "event", "start_ms": _ms(_at(5)), "end_ms": _ms(_at(5, 30)), "title": "Early"},
        [],
    ),
}


@pytest.mark.parametrize("name", ITEM_CASES)
def test_an_item_that_holds_a_clue_gives_it_and_a_fact_gives_none(
    name: str, tmp_path: Path,
) -> None:
    """An item that holds a clue gives it and a fact gives none."""
    item, expected = ITEM_CASES[name]
    with closing(open_event_log(tmp_path / "e.db")) as conn:
        found = gather(conn, None, DAY, [item], WINDOW, VANCOUVER)
    assert found == expected


def test_the_clue_kinds_are_the_closed_ones() -> None:
    """The clue kinds are the closed ones."""
    assert {c.kind for _, expected in ITEM_CASES.values() for c in expected} <= set(CLUE_KINDS)


def _clues(world: World, items: list[dict[str, Any]] | None = None) -> list[Clue]:
    with closing(open_event_log(world.log)) as conn:
        return gather(conn, world.memory, DAY, items or [], WINDOW, VANCOUVER)


def test_what_was_said_is_a_clue_of_his_and_of_hers_without_markup_or_notes(world: World) -> None:
    """What was said is a clue of his and of hers without markup or notes."""
    for rid, moment, source, text in (
        ("w1", _at(7), "allen", "  early\nbird  "),
        ("w2", _at(7, 1), "jarvis_live", "morning"),
        ("w3", _at(7, 2), "picture_note", "[Picture note] a mug"),
        ("w4", _at(7, 3), "allen_reaction", "liked"),
        ("w5", _at(7, 4), "allen", "\n[attached: image IMG_1.png]"),
        ("w6", _at(5, 59), "allen", "before the window"),
        ("w7", _at(15), "allen", "after the window"),
        ("w8", _at(7, 5), "allen", "长" * 300),
    ):
        _say(world.memory, rid, moment, source, text)
    said = [c for c in _clues(world) if c.kind == "words" and _ms(_at(7)) <= c.at_ms < _ms(_at(8))]
    assert [c.text for c in said[:2]] == ["he: early bird", "she: morning"]
    assert len(said) == 3  # the note, the reaction, a turn of files alone and the outside: none
    assert said[2].text == "he: " + "长" * (CLUE_TEXT_MAX_CHARS - len("he: ") - 1) + "…"
    assert len(said[2].text) == CLUE_TEXT_MAX_CHARS


def test_what_the_work_state_says_he_is_doing_is_a_clue_where_it_changes(world: World) -> None:
    """Each revision that changes what he is doing lately is a clue; the day's recap is not."""
    _screen(world.log, _at(5, 30), "Before the window")
    _screen(world.log, _at(11), "Writing the thesis in Overleaf", "Earlier: Slack")
    _screen(world.log, _at(11, 30), None)
    _screen(world.log, _at(12), "Writing the thesis in Overleaf")
    _screen(world.log, _at(12, 30), "Writing the thesis in Overleaf", "Slack")
    _screen(world.log, _at(15, 30), "After the window")
    screen = [(c.at_ms, c.text) for c in _clues(world) if c.kind == "screen"]
    assert screen == [
        (_ms(_at(10)), "Writing the thesis in Overleaf"),
        (_ms(_at(12)), "Writing the thesis in Overleaf"),
    ]


def test_interview_mail_is_a_clue_at_the_time_it_names(world: World) -> None:
    """Interview mail is a clue at the time it names."""
    def mail(mid: str, event_at: str | None, **fields: str) -> None:
        upsert_mail(world.memory, {
            "message_id": mid, "received_at": _at(8).isoformat(), "kind": "interview",
            "company": "Delta", "role": "Intern", "event_text": "Wed 11:00 AM",
            "event_at": event_at, **fields,
        }, datetime.now(UTC))

    mail("m1", _at(11).isoformat())
    mail("m2", _at(16).isoformat())  # after the window
    mail("m3", None)  # no time named
    mail("m4", _at(12).isoformat(), kind="receipt")  # not an interview
    assert [(c.at_ms, c.text) for c in _clues(world) if c.kind == "mail"] == [
        (_ms(_at(11)), "interview mail: Delta, Intern (Wed 11:00 AM)"),
    ]


def test_his_answers_are_clues_this_days_and_earlier_ones_near_where_he_stayed(
    world: World,
) -> None:
    """His answers are clues this days and earlier ones near where he stayed."""
    def answer(day: str, moment: datetime, lat: float, **words: str) -> None:
        append_doing_answer(
            world.memory, day=day, span_id="x", start_ms=_ms(moment), end_ms=_ms(moment) + 30 * MIN,
            choice=words.get("choice"), text=words.get("text"), lat=lat, lng=2.0, place_id=None,
            origin="timeline",
        )

    answer(KEY, _at(9), 1.0, text="写论文")
    answer("2026-10-01", _at(10, day=date(2026, 10, 1)), 1.0, text="打羽毛球")
    answer("2026-10-02", _at(10, day=date(2026, 10, 2)), 1.0, choice="Lunch")
    answer("2026-10-03", _at(10, day=date(2026, 10, 3)), 1.5, text="far away")
    answer("2026-10-09", _at(10, day=date(2026, 10, 9)), 1.0, text="after this stay began")
    stay = {"kind": "stay", "id": "v1", "start_ms": _ms(_at(8)), "end_ms": _ms(_at(11, 30)),
            "lat": 1.0, "lng": 2.0}
    found = [(c.at_ms, c.text) for c in _clues(world, [stay]) if c.kind == "answer"]
    assert found == [
        (_ms(_at(10, day=date(2026, 10, 1))), "on 10-01 at this place he said it was: 打羽毛球"),
        (_ms(_at(10, day=date(2026, 10, 2))), "on 10-02 at this place he said it was: Lunch"),
        (_ms(_at(9)), "he said 09:00 was: 写论文"),
    ]


def test_the_clues_are_bounded_in_number_and_thinned_evenly(world: World) -> None:
    """The clues are bounded in number and thinned evenly."""
    for n in range(300):
        _say(world.memory, f"b{n}", _at(7) + timedelta(seconds=n), "allen", f"line {n:03d}")
    payments = [
        {"kind": "payment", "start_ms": _ms(_at(7)) + n * MIN, "end_ms": _ms(_at(7)),
         "direction": "out", "amount": str(n), "currency": "CAD"}
        for n in range(100)
    ]
    found = _clues(world, payments)
    said = [c for c in found if c.kind == "words" and c.text.startswith("he: line")]
    paid = [c for c in found if c.kind == "payment"]
    assert 0 < len(said) <= MAX_WORDS_CLUES
    assert 0 < len(paid) <= MAX_CLUES_PER_KIND
    for kept in (said, paid):
        steps = {b.at_ms - a.at_ms for a, b in pairwise(kept)}
        assert len(steps) == 1  # every n-th through the day
    assert said[0].text == "he: line 000"


def test_a_source_that_fails_leaves_the_rest(
    world: World, caplog: pytest.LogCaptureFixture,
) -> None:
    """A source that fails leaves the rest."""
    broken = {"kind": "code", "start_ms": _ms(_at(9)), "detail": "no end_ms key"}
    fine = {"kind": "picture", "start_ms": _ms(_at(12)), "end_ms": _ms(_at(12)), "note": "ok"}
    found = _clues(world, [broken, fine])
    assert Clue("picture", _ms(_at(12)), "ok") in found
    assert any(c.kind == "words" for c in found)
    assert "could not be read" in caplog.text


def test_without_a_memory_store_only_the_day_line_and_the_screen_speak(world: World) -> None:
    """Without a memory store only the day line and the screen speak."""
    with closing(open_event_log(world.log)) as conn:
        found = gather(conn, None, DAY, [], WINDOW, VANCOUVER)
    assert {c.kind for c in found} == {"screen"}


def test_the_brief_holds_the_days_facts_clues_and_the_language_of_his_words(
    world: World, brief: Brief,
) -> None:
    """The brief holds the days facts clues and the language of his words."""
    assert (brief.day, brief.weekday, brief.final) == (KEY, "Wednesday", False)
    assert (brief.waking.start_ms, brief.waking.end_ms) == (_ms(_at(6)), _ms(NOW))
    assert [(s.start_ms, s.end_ms) for s in brief.segments] == [
        (_ms(_at(*hm)), _ms(_at(*end)))
        for hm, end in (
            ((6, 0), (8, 0)), ((8, 0), (11, 30)), ((11, 30), (12, 0)), ((12, 0), (13, 0)),
            ((13, 0), (13, 30)), ((13, 30), (14, 45)), ((14, 45), (15, 0)),
        )
    ]
    assert [(c.kind, c.text) for c in brief.clues] == [
        ("words", "he: 我在图书馆写论文"), ("words", "she: 好的，加油"),
        ("screen", "Writing the thesis in Overleaf"),
        ("picture", "A bowl of noodles on a wooden table"),
    ]
    assert brief.language == "Simplified Chinese"
    with closing(open_memory_db(world.memory)) as conn, conn:
        conn.execute("UPDATE records SET text = 'writing at the library' WHERE id = 'r1'")
    again = read_brief(DAY, world.sources, world.log, NOW)
    assert again is not None
    assert again.language == "English"


def test_a_day_with_no_waking_window_has_no_brief(world: World) -> None:
    """A day with no waking window has no brief."""
    assert read_brief(DAY, world.sources, world.log, _at(5)) is None  # waking starts at 06:00


# --- what the writer is shown ------------------------------------------------------------------


def test_the_times_are_the_edges_of_the_window_the_segments_the_facts_and_what_dates_his_doing(
    brief: Brief,
) -> None:
    """The times are the edges of the window, the segments, the facts and what dates his doing."""
    clocks = [datetime.fromtimestamp(t / 1000, VANCOUVER).strftime("%H:%M") for t in times(brief)]
    assert clocks == [
        "06:00", "08:00", "09:05", "10:00", "11:30", "12:00", "12:20", "13:00", "13:30", "14:45",
        "15:00",
    ]  # 09:05 his words, 10:00 the screen, 12:20 his picture; her 09:06 reply is not one


def test_the_prompt_gives_the_times_the_segments_with_their_facts_and_the_clues(
    brief: Brief,
) -> None:
    """The prompt gives the times the segments with their facts and the clues."""
    (message,) = build_messages(brief)
    lines = message["content"].split("\n")
    assert lines[0] == (
        "DAY: 2026-10-07 Wednesday, timezone America/Vancouver; "
        "the day is under way; it is 15:00 now"
    )
    assert "TITLE LANGUAGE: Simplified Chinese" in lines
    assert "WAKING WINDOW: t0 06:00:00 to t10 15:00:00" in lines
    assert [line for line in lines if re.fullmatch(r"t\d+ \d\d:\d\d:\d\d", line)] == [
        "t0 06:00:00", "t1 08:00:00", "t2 09:05:00", "t3 10:00:00", "t4 11:30:00",
        "t5 12:00:00", "t6 12:20:00", "t7 13:00:00", "t8 13:30:00", "t9 14:45:00",
        "t10 15:00:00",
    ]
    assert "t0-t1 | 06:00-08:00 | place: none known | facts: none" in lines
    assert (
        "t1-t4 | 08:00-11:30 | place: UVic Library [study] | "
        "facts: stay UVic Library 08:00-11:30; talk 09:05-09:05 | inner times: t2 t3"
    ) in lines
    assert (
        "t5-t7 | 12:00-13:00 | place: Corner Cafe [unknown] | "
        "facts: stay Corner Cafe 12:00-13:00 | inner times: t6"
    ) in lines
    assert "c1 | 09:05 | words | he: 我在图书馆写论文" in lines
    assert "c4 | 12:20 | picture | A bowl of noodles on a wooden table" in lines


def test_a_final_day_says_so_and_offers_the_edge_of_a_short_stay_as_a_time_inside_a_segment(
    world: World,
) -> None:
    """A final day says so and offers the edge of a short stay as a time inside a segment."""
    _visit(world.log, "v4", _at(16), _at(16, 5), "Corner Cafe", lat=1.01)  # five minutes
    final = read_brief(DAY, world.sources, world.log, _at(0, 1, day=DAY + timedelta(days=1)))
    assert final is not None
    assert final.final
    lines = build_messages(final)[0]["content"].split("\n")
    assert any("the day is over; this is its final account" in line for line in lines)
    # too short to cut the day, the stay is folded into the stretch it sits in
    held = next(line for line in lines if "| 16:00-00:00 |" in line)
    assert "inner times: t" in held
    assert "stay Corner Cafe 16:00-16:05" in held


def test_phone_or_tablet_use_reads_with_its_device_and_its_apps(brief: Brief) -> None:
    """A block of phone or tablet use reads with the device it was on and the apps used most."""
    item = {
        "id": "screen-iPad-1", "lane": "you", "kind": "screen", "device": "iPad",
        "start_ms": brief.waking.start_ms, "end_ms": brief.waking.start_ms + 30 * 60_000,
        "state": "done", "active_s": 1500,
        "apps": [{"name": "抖音", "bundle": "b", "minutes": 20, "category": "Entertainment"},
                 {"name": "微信", "bundle": "w", "minutes": 5, "category": "Social Networking"}],
    }
    facts = sorted([*brief.facts, *fact_spans([item])], key=lambda s: (s["start_ms"], s["id"]))
    shown = replace(
        brief, items=[*brief.items, item], facts=facts, segments=cut(facts, brief.waking),
    )
    content = build_messages(shown)[0]["content"]
    assert "screen iPad: 抖音, 微信 06:00-06:30" in content


def test_the_writer_is_told_how_far_his_phone_and_tablet_use_is_known(brief: Brief) -> None:
    """None of it, all of it, or each device that lags with where its records stop."""
    def said(shown: Brief) -> str:
        content = build_messages(shown)[0]["content"]
        return next(
            line for line in content.split("\n") if line.startswith("PHONE AND TABLET APP USE:")
        )

    assert brief.screen is None  # no shipped store: nothing of it reaches the brain
    assert said(brief).endswith("none of it reaches Jarvis; its absence shows nothing")
    assert said(replace(brief, screen={})).endswith("known through the window")
    lagging = replace(brief, screen={"iPad": _ms(_at(10, 34)), "iPhone": _ms(_at(2, 38))})
    assert said(lagging) == (
        "PHONE AND TABLET APP USE: iPad known only up to 10-07 10:34; iPhone known only up to "
        "10-07 02:38; later use on it is not known yet"
    )
    digests = {inputs_digest(b) for b in (brief, replace(brief, screen={}), lagging)}
    assert len(digests) == 3


def test_an_unnamed_place_reads_as_unnamed(world: World) -> None:
    """An unnamed place reads as unnamed."""
    _visit(world.log, "v5", _at(14, 45), _at(14, 59), "", lat=1.2)
    found = read_brief(DAY, world.sources, world.log, NOW)
    assert found is not None
    assert "place: unnamed [unknown]" in build_messages(found)[0]["content"]


# --- the digest --------------------------------------------------------------------------------


def test_the_digest_follows_the_facts_and_clues_and_not_the_clock(
    world: World, brief: Brief,
) -> None:
    """The digest follows the facts and clues and not the clock."""
    later = read_brief(DAY, world.sources, world.log, NOW + timedelta(minutes=7))
    assert later is not None
    assert later.waking.end_ms != brief.waking.end_ms
    assert inputs_digest(later) == inputs_digest(brief)
    _say(world.memory, "late", _at(14, 50), "allen", "a late row")
    changed = read_brief(DAY, world.sources, world.log, NOW)
    assert changed is not None
    assert inputs_digest(changed) != inputs_digest(brief)
    _visit(world.log, "v9", _at(14, 55), None, "Corner Cafe", lat=1.01)
    moved = read_brief(DAY, world.sources, world.log, NOW)
    assert moved is not None
    assert inputs_digest(moved) not in (inputs_digest(brief), inputs_digest(changed))


# --- the background ----------------------------------------------------------------------------


ABOUT_BEFORE = DAY - timedelta(days=3)


def _run(memory: Path, day: date, *spans: tuple[int, int, str | None]) -> None:
    """A stored run of ``day`` whose spans are (start hour, end hour, title)."""
    append_doing_run(
        memory, day=day.isoformat(), draft=False, covers_until_ms=_ms(_at(23, day=day)),
        model="m", inputs_digest="d", spans=[
            {"id": f"doing-{start}", "start_ms": _ms(_at(start, day=day)),
             "end_ms": _ms(_at(end, day=day)), "title": title}
            for start, end, title in spans
        ],
    )


def _told(memory: Path, day: date, hour: int, *, text: str | None = None, choice: str | None = None,
          ) -> None:
    append_doing_answer(
        memory, day=day.isoformat(), span_id=f"doing-{hour}", start_ms=_ms(_at(hour, day=day)),
        end_ms=_ms(_at(hour + 1, day=day)), choice=choice, text=text, lat=None, lng=None,
        place_id=None, origin="test",
    )


def _about(world: World, mentioned: str = "") -> doing_about.About:
    with closing(open_event_log(world.log)) as conn:
        return doing_about.gather(world.memory, conn, DAY, VANCOUVER, mentioned)


def test_the_background_holds_his_projects_the_people_the_day_names_and_his_dated_corrections(
    world: World,
) -> None:
    """Projects as stored; people only when named; each correction one dated instance."""
    goals = core_memory.GOALS_SECTION
    remember_fact(world.memory, "Thesis", "chapter three due in December", goals)
    remember_fact(world.memory, "Spanish", "twenty minutes a day", goals)
    remember_fact(world.memory, "小明", "roommate, plays badminton", core_memory.PEOPLE_SECTION)
    remember_fact(world.memory, "Dana", "supervisor", core_memory.PEOPLE_SECTION)
    remember_fact(world.memory, "X", "a single letter", core_memory.PEOPLE_SECTION)
    remember_fact(world.memory, "Mum", "lives in Shanghai", core_memory.DEFAULT_SECTION)
    _run(world.memory, DAY - timedelta(days=1), (10, 11, "写论文"))
    _run(world.memory, ABOUT_BEFORE, (12, 13, "旧的标题"))
    _run(world.memory, ABOUT_BEFORE, (12, 13, "午饭"), (13, 14, "休息"))  # the newest run wins
    _told(world.memory, ABOUT_BEFORE, 12, text="和朋友吃饭聊天")
    _told(world.memory, DAY - timedelta(days=1), 10, text="写论文")  # as it was written
    _told(world.memory, DAY - timedelta(days=2), 9, choice="Reading")  # no run that day
    _told(world.memory, DAY - timedelta(days=31), 9, text="too old")
    _told(world.memory, DAY, 9, text="today is not a correction")
    about = _about(world, "he: 小明 says hi\nx met X at noon")
    assert about.projects == (
        "Thesis: chapter three due in December", "Spanish: twenty minutes a day",
    )  # as core memory holds them
    assert about.people == ("小明: roommate, plays badminton",)  # Dana is not named; X is too short
    assert about.corrections == (  # oldest first; the last was written as he said it
        '10-04 12:00-13:00: written as "午饭"; he said "和朋友吃饭聊天"',
        '10-05 09:00-10:00: he said "Reading"',
        '10-06 10:00-11:00: he said "写论文"',
    )
    assert about.routines == ()


def test_the_routines_of_the_weekday_are_told_with_the_weeks_they_were_counted_over(
    world: World,
) -> None:
    """A habit of four Wednesdays reads with its times, place and weeks; nothing else is made up."""
    for number, ago in enumerate((7, 14, 21, 28)):
        past = DAY - timedelta(days=ago)
        _visit(world.log, f"w{number}", _at(12, day=past), _at(13, day=past), "Corner Cafe", 1.01)
    places.save(world.memory, name="Corner Cafe", lat=1.01, lng=2.0, role="other", origin="user")
    assert _about(world).routines == ("12:00-13:00 Corner Cafe, seen 4 of 4 weeks",)


def test_the_background_parts_are_capped_and_the_corrections_are_the_newest_twenty(
    world: World,
) -> None:
    """Items are left off the end to fit; of the corrections the oldest go."""
    for number in range(40):
        remember_fact(world.memory, f"Project {number:02d}", "x" * 200, core_memory.GOALS_SECTION)
    everything = core_memory.GOALS_SECTION
    with closing(open_memory_db(world.memory)) as conn:
        stored = [i["text"] for i in core_memory.current(conn, "2026-10-07").doc[everything]]
    about = _about(world)
    assert 0 < len(about.projects) < len(stored)
    assert about.projects == tuple(stored[: len(about.projects)])
    shown = "\n".join(f"- {line}" for line in about.projects)
    assert len(shown) <= doing_about.ABOUT_SECTION_MAX_CHARS
    for number in range(25):  # 25 answers on 25 different days, all within the 30
        _told(world.memory, DAY - timedelta(days=number + 1), 9, text=f"said {number:02d}")
    corrections = _about(world).corrections
    assert len(corrections) == doing_about.MAX_CORRECTIONS == 20
    assert corrections[0].endswith('he said "said 19"')  # oldest of the newest twenty first
    assert corrections[-1].endswith('he said "said 00"')


def test_a_background_part_that_cannot_be_read_is_empty_and_the_rest_stands(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing read is logged and leaves its part empty."""
    remember_fact(world.memory, "Thesis", "chapter three", core_memory.GOALS_SECTION)

    def failing(*_args: object, **_kwargs: object) -> object:
        raise OSError

    monkeypatch.setattr(routines, "for_day", failing)
    monkeypatch.setattr(doing_about, "doing_answers_between", failing)
    about = _about(world)
    assert about.projects == ("Thesis: chapter three",)
    assert (about.routines, about.corrections) == ((), ())
    with closing(open_event_log(world.log)) as conn:
        assert doing_about.gather(None, conn, DAY, VANCOUVER, "x") == doing_about.About(
            (), (), (), (),
        )


def test_the_brief_carries_the_background_and_the_prompt_tells_it_after_the_title_language(
    world: World,
) -> None:
    """``read_brief`` fills it from the day's clues and facts; each empty part reads (none)."""
    remember_fact(world.memory, "Thesis", "chapter three", core_memory.GOALS_SECTION)
    remember_fact(world.memory, "Dana", "supervisor", core_memory.PEOPLE_SECTION)
    _told(world.memory, DAY - timedelta(days=2), 9, choice="Reading")
    found = read_brief(DAY, world.sources, world.log, NOW)
    assert found is not None
    assert found.about is not None
    assert found.about.projects == ("Thesis: chapter three",)
    assert found.about.people == ()  # Dana is in no clue text or fact title of the day
    content = build_messages(found)[0]["content"]
    lines = content.split("\n")
    at = lines.index("TITLE LANGUAGE: Simplified Chinese")
    assert lines[at + 2 : at + 15] == [
        "ABOUT HIM (background from his memory and his past answers; it helps tell what things"
        " are, it never shows what he did today):",
        "",
        "PROJECTS AND ONGOING THINGS:",
        "- Thesis: chapter three",
        "",
        "ROUTINE ON THIS WEEKDAY (counted over past weeks):",
        "(none)",
        "",
        "PEOPLE NAMED TODAY:",
        "(none)",
        "",
        "HIS EARLIER CORRECTIONS (each one day only):",
        '- 10-05 09:00-10:00: he said "Reading"',
    ]
    assert lines[at + 15] == ""
    assert "ABOUT HIM" not in build_messages(replace(found, about=None))[0]["content"]


def test_the_background_does_not_move_the_digest(brief: Brief) -> None:
    """Background must not make the day be written again."""
    about = doing_about.About(("p",), ("r",), ("q",), ("c",))
    assert inputs_digest(replace(brief, about=about)) == inputs_digest(brief)
    other = replace(brief, about=doing_about.About((), (), (), ()))
    assert inputs_digest(other) == inputs_digest(replace(brief, about=about))


# --- the signals -------------------------------------------------------------------------------


def _phone(world: World, uid: str, kind: str, payload: dict[str, Any], ts: datetime) -> None:
    with closing(open_event_log(world.log)) as conn:
        emit_event(
            conn, type=f"phone.{kind}_observed", payload=payload, ts_epoch_ms=_ms(ts),
            event_uid=uid, ingestion_node="phone",
        )


def _motion(
    world: World, activity: str, start: datetime, end: datetime | None,
    confidence: str = "high",
) -> None:
    payload: dict[str, Any] = {
        "activity": activity, "confidence": confidence, "started_at_ms": _ms(start),
    }
    if end is not None:
        payload["ended_at_ms"] = _ms(end)
    uid = f"m-{activity}-{_ms(start)}"
    _phone(world, uid, "motion", payload, end or start)


def _steps(world: World, start: datetime, end: datetime, value: float, uid: str) -> None:
    payload = {"metric": "steps", "start_ms": _ms(start), "end_ms": _ms(end), "value": value}
    _phone(world, uid, "health", payload, end)


def _heart(world: World, at: datetime, value: float, context: str | None) -> None:
    payload: dict[str, Any] = {
        "metric": "heart_rate", "start_ms": _ms(at), "end_ms": _ms(at), "value": value,
        "unit": "count/min", "sample_uuid": f"hr-{_ms(at)}",
    }
    if context is not None:
        payload["meta"] = {"motion_context": context}
    _phone(world, f"hr-{_ms(at)}", "measure", payload, at)


def _marks(directory: Path, marks: list[tuple[datetime, str]]) -> DaySources:
    """TimeSink ``stateEvent`` rows (UTC as GRDB writes them), shipped to ``directory``."""
    lines = [
        {
            "key": f"stateEvent:{number}:{'a' * 12}", "kind": "stateEvent", "zone": "UTC",
            "at_ms": _ms(at), "text": "",
            "fields": {
                "id": number, "kind": kind,
                "at": at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.000"),
            },
        }
        for number, (at, kind) in enumerate(marks, 1)
    ]
    Shipments(directory, min_free_bytes=0).receive_entries(
        "timesink", b"\n".join(json.dumps(line).encode() for line in lines),
    )
    return DaySources(VANCOUVER, None, None, shipped=directory)


def _signalled(world: World, directory: Path, marks: list[tuple[datetime, str]]) -> World:
    """The world with his Mac's marks shipped, and the phone and watch rows of 12:00-13:00."""
    _motion(world, "stationary", _at(12), _at(12, 40))
    _motion(world, "walking", _at(12, 40), _at(12, 46), "medium")
    _motion(world, "automotive", _at(12, 46), _at(12, 50), "low")  # doubtful: not counted
    _motion(world, "stationary", _at(12, 50), None)  # still going: to the segment's end
    _steps(world, _at(11), _at(12), 30, "s1")
    _steps(world, _at(12), _at(13), 9, "s2")
    _steps(world, _at(12), _at(13), 18, "s2b")  # the hour reported again: the later total
    _steps(world, _at(13), _at(14), 120, "s3")
    for at, value, context in (
        (_at(12, 5), 58, "sedentary"), (_at(12, 30), 68, "sedentary"), (_at(12, 45), 55, "not_set"),
        (_at(13, 0), 90, "active"),  # the next segment's
    ):
        _heart(world, at, value, context)
    return replace(world, sources=_marks(directory, marks))


MARKS = [
    (_at(12, 10), "idle"), (_at(12, 25), "lock"), (_at(12, 30), "active"), (_at(12, 50), "unlock"),
]


def _start(brief: Brief, hour: int, minute: int = 0) -> int:
    return next(s.start_ms for s in brief.segments if s.start_ms == _ms(_at(hour, minute)))


def test_a_segment_carries_the_signals_of_his_mac_phone_and_watch_in_a_fixed_order(
    world: World, tmp_path: Path,
) -> None:
    """Each part is computed from stored rows; the ones with no data are left out."""
    signalled = _signalled(world, tmp_path / "imports", MARKS)
    found = read_brief(DAY, signalled.sources, world.log, NOW)
    assert found is not None
    assert found.signals == {
        # idle 12:10-12:30 and locked 12:25-12:50: the lock names the time both cover
        _ms(_at(12)): (
            "Mac idle 12:10-12:25, locked 12:25-12:50; phone: still 50m, walking 6m; "
            "~18 steps; heart 55-68 sedentary"
        ),
        # the 11:00 hour's 30 steps, half in the segment before 11:30 and half in the one after
        _ms(_at(8)): "~15 steps",
        _ms(_at(11, 30)): "~15 steps",
        # the still that began at 12:50 has no end, so it goes on through the rest of the day;
        # the 13:00 hour's 120 steps fall half in 13:00-13:30 and half in 13:30-14:45
        _ms(_at(13)): "phone: still 30m; ~60 steps; heart 90 active",
        _ms(_at(13, 30)): "phone: still 75m; ~60 steps",
        _ms(_at(14, 45)): "phone: still 15m",
    }
    line = next(
        row for row in build_messages(found)[0]["content"].split("\n")
        if "| 12:00-13:00 |" in row
    )
    assert line.split(" | ", 1)[1] == (
        "12:00-13:00 | place: Corner Cafe [unknown] | facts: stay Corner Cafe 12:00-13:00 | "
        "signals: " + found.signals[_ms(_at(12))]
    )
    assert len(found.signals[_ms(_at(12))]) <= SIGNALS_MAX_CHARS


def test_a_segment_with_no_signals_has_no_signals_part(brief: Brief) -> None:
    """With no phone rows and no shipped store the segment lines are what they were."""
    assert brief.signals == {}
    assert "signals:" not in build_messages(brief)[0]["content"]


def _foreground(directory: Path, spans: list[tuple[datetime, datetime, dict[str, Any]]]) -> None:
    """TimeSink ``span`` rows (UTC as GRDB writes them), shipped to ``directory``."""
    lines = [
        {
            "key": f"span:{number}:{'b' * 12}", "kind": "span", "zone": "UTC",
            "at_ms": _ms(start), "text": "",
            "fields": {
                "id": number, "deviceID": None, "appName": "Claude", "domain": None,
                "document": None, "title": None, "keySeconds": 0, **fields,
                "start": start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.000"),
                "end": end.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.000"),
            },
        }
        for number, (start, end, fields) in enumerate(spans, 1)
    ]
    Shipments(directory, min_free_bytes=0).receive_entries(
        "timesink", b"\n".join(json.dumps(line).encode() for line in lines),
    )


def test_the_brief_tells_what_was_in_front_at_his_mac_each_quarter_hour(
    world: World, tmp_path: Path,
) -> None:
    """Minutes and typing per app, site or document; a span over two quarters is shared out."""
    directory = tmp_path / "imports"
    second = timedelta(seconds=1)
    _foreground(directory, [
        (_at(12, 10), _at(12, 20), {"document": "Jarvis - Claude Code", "keySeconds": 60}),
        (_at(12, 20), _at(12, 22), {
            "appName": "Google Chrome", "domain": "olevod.com", "title": "挽救计划",
        }),
        # two short visits to one window are summed: 40 s together are worth naming
        (_at(12, 22), _at(12, 22) + 20 * second, {"appName": "WeChat", "title": "Weixin"}),
        (_at(12, 23), _at(12, 23) + 20 * second, {"appName": "WeChat", "title": "Weixin"}),
        (_at(12, 30), _at(12, 40), {"deviceID": "ipad", "appName": "Safari"}),  # not his Mac
    ])
    found = read_brief(DAY, replace(world.sources, shipped=directory), world.log, NOW)
    assert found is not None
    assert found.foreground == [
        Quarter(
            _ms(_at(12)), _ms(_at(12, 15)), "Claude Jarvis - Claude Code 5m typed 30s",
            "Claude Jarvis - Claude Code",
        ),
        Quarter(_ms(_at(12, 15)), _ms(_at(12, 30)), (
            "Claude Jarvis - Claude Code 5m typed 30s, Google Chrome olevod.com (挽救计划) 2m, "
            "WeChat Weixin 1m"
        ), "Claude Jarvis - Claude Code"),
    ]
    lines = build_messages(found)[0]["content"].split("\n")
    at = lines.index(
        "MAC FOREGROUND (local | lead: what held over half its minutes, or mixed | what was in "
        "front of him, minutes in front, seconds typed):",
    )
    assert lines[at + 1] == (
        "12:00-12:15 | lead Claude Jarvis - Claude Code | Claude Jarvis - Claude Code 5m typed 30s"
    )
    assert inputs_digest(found) != inputs_digest(replace(found, foreground=[]))


def test_a_quarter_names_its_lead_only_when_one_held_more_than_half_of_its_time_in_front(
    world: World, tmp_path: Path,
) -> None:
    """Over half of the quarter's whole time in front, short visits counted in the total."""
    directory = tmp_path / "imports"
    minute, second = timedelta(minutes=1), timedelta(seconds=1)
    _foreground(directory, [
        # 12:00 Claude 8m of 15m: its lead
        (_at(12), _at(12) + 8 * minute, {"appName": "Claude"}),
        (_at(12, 8), _at(12, 15), {"appName": "Safari"}),
        # 12:15 7m 30s each: exactly half is not over half
        (_at(12, 15), _at(12, 15) + 7 * minute + 30 * second, {"appName": "Claude"}),
        (_at(12, 22) + 30 * second, _at(12, 30), {"appName": "Safari"}),
        # 12:30 7m, 7m and 20 s too short to be named: the 20 s still count in the total
        (_at(12, 30), _at(12, 37), {"appName": "Claude"}),
        (_at(12, 37), _at(12, 44), {"appName": "Safari"}),
        (_at(12, 44), _at(12, 44) + 20 * second, {"appName": "WeChat"}),
        # 12:45 one site 10m of 11m: the lead is named like the text names it
        (_at(12, 45), _at(12, 55), {"appName": "Google Chrome", "domain": "olevod.com"}),
        (_at(12, 55), _at(12, 56), {"appName": "Claude"}),
    ])
    window = (_ms(_at(12)), _ms(_at(13)))
    got = foreground(replace(world.sources, shipped=directory), window)
    assert [(_clock(q.start_ms), q.lead) for q in got] == [
        ("12:00", "Claude"), ("12:15", None), ("12:30", None),
        ("12:45", "Google Chrome olevod.com"),
    ]
    assert got[2].text == "Claude 7m, Safari 7m"  # the 20 s is under half a minute: not named
    assert isinstance(got[0], Quarter)
    assert (got[0].start_ms, got[0].end_ms) == (_ms(_at(12)), _ms(_at(12, 15)))


def _clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, VANCOUVER).strftime("%H:%M")


def test_the_edges_of_the_quarters_at_his_mac_inside_the_window_are_times_and_inner_times(
    brief: Brief,
) -> None:
    """Only a quarter in the brief gives its edges, and only those strictly inside the window."""
    quarters = [
        Quarter(_ms(_at(12, 15)), _ms(_at(12, 30)), "Claude 9m", "Claude"),
        Quarter(_ms(_at(12, 30)), _ms(_at(12, 45)), "Claude 5m, Safari 5m", None),
        Quarter(_ms(_at(14, 45)), _ms(_at(15)), "Claude 12m", "Claude"),  # the window's end
    ]
    assert [_clock(t) for t in times(brief)] == [
        "06:00", "08:00", "09:05", "10:00", "11:30", "12:00", "12:20", "13:00", "13:30", "14:45",
        "15:00",
    ]
    held = replace(brief, foreground=quarters)
    assert [_clock(t) for t in times(held)] == [
        "06:00", "08:00", "09:05", "10:00", "11:30", "12:00", "12:15", "12:20", "12:30", "12:45",
        "13:00", "13:30", "14:45", "15:00",
    ]  # 12:20 stays once; 13:00 is a fact edge; no quarter at 11:00 or 13:15 gives a time
    lines = build_messages(held)[0]["content"].split("\n")
    clock_of = dict(
        line.split(" ") for line in lines if re.fullmatch(r"t\d+ \d\d:\d\d:\d\d", line)
    )
    cafe = next(line for line in lines if line.startswith("t") and "| 12:00-13:00 |" in line)
    inner = cafe.rsplit("inner times: ", 1)[1].split()
    assert [clock_of[label][:5] for label in inner] == ["12:15", "12:20", "12:30", "12:45"]
    # a short run keeps what is inside its window only
    short = narrow(held, _written(held))
    assert [_clock(t) for t in times(short)] == ["14:45", "15:00"]


def test_each_quarter_is_told_with_its_lead_or_as_mixed(brief: Brief) -> None:
    """``HH:MM-HH:MM | lead <name> | <text>`` or ``| mixed |``, under the header that says so."""
    quarters = [
        Quarter(_ms(_at(12, 15)), _ms(_at(12, 30)), "Claude Jarvis 9m typed 40s", "Claude Jarvis"),
        Quarter(_ms(_at(12, 30)), _ms(_at(12, 45)), "Claude 5m, Safari 5m", None),
    ]
    lines = build_messages(replace(brief, foreground=quarters))[0]["content"].split("\n")
    at = lines.index(
        "MAC FOREGROUND (local | lead: what held over half its minutes, or mixed | what was in "
        "front of him, minutes in front, seconds typed):",
    )
    assert lines[at + 1 : at + 3] == [
        "12:15-12:30 | lead Claude Jarvis | Claude Jarvis 9m typed 40s",
        "12:30-12:45 | mixed | Claude 5m, Safari 5m",
    ]


def test_narrowing_clips_a_quarter_and_keeps_its_lead_and_the_background(brief: Brief) -> None:
    """The quarter that crosses where the window starts begins there and keeps its lead."""
    about = doing_about.About(("p",), ("r",), ("q",), ("c",))
    wide = replace(brief, about=about, foreground=[
        Quarter(_ms(_at(14, 30)), _ms(_at(14, 45)), "WeChat 4m", None),
        Quarter(_ms(_at(14, 40)), _ms(_at(15)), "Claude 12m", "Claude"),
    ])
    short = narrow(wide, _written(wide))
    assert short.waking.start_ms == _ms(_at(14, 45))
    assert short.foreground == [Quarter(_ms(_at(14, 45)), _ms(_at(15)), "Claude 12m", "Claude")]
    assert short.about is about


def test_a_mac_stretch_is_closed_by_its_mark_or_by_timesinks_own_stop_else_runs_to_the_end(
    world: World, tmp_path: Path,
) -> None:
    """The ways a stretch ends, and an asleep Mac naming the time an idle one also covers."""
    cases = {
        "open": ([(_at(11, 40), "idle")], "Mac idle 12:00-13:00"),
        "stopped": ([(_at(11, 40), "idle"), (_at(12, 30), "stop")], "Mac idle 12:00-12:30"),
        "woken": (
            [(_at(11, 40), "idle"), (_at(12, 10), "sleep"), (_at(12, 40), "wake"),
             (_at(12, 45), "active")],
            "Mac idle 12:00-12:10, asleep 12:10-12:40, idle 12:40-12:45",
        ),
        "a blip": ([(_at(12, 10), "idle"), (_at(12, 10), "active")], None),
        "before the day's reach": ([(_at(13, 5), "idle")], None),
    }
    for number, (marks, expected) in enumerate(cases.values()):
        sources = _marks(tmp_path / f"case-{number}", marks)
        found = read_brief(DAY, sources, world.log, NOW)
        assert found is not None
        assert found.signals.get(_ms(_at(12))) == expected


def test_the_signals_stay_within_the_cap_by_dropping_the_last_parts(
    world: World, tmp_path: Path,
) -> None:
    """Mac stretches, a long title, activities, steps, heart rate: the title is cut, heart goes."""
    marks = [
        (_at(8, 10), "idle"), (_at(8, 40), "active"), (_at(9), "lock"), (_at(9, 30), "unlock"),
        (_at(10), "sleep"), (_at(10, 30), "wake"),
    ]
    signalled = replace(world, sources=_marks(tmp_path / "imports", marks))
    _motion(signalled, "stationary", _at(8), _at(10))
    _motion(signalled, "walking", _at(10), _at(10, 20))
    _steps(signalled, _at(8), _at(9), 100, "a")
    _steps(signalled, _at(9), _at(10), 100, "b")
    _steps(signalled, _at(10), _at(11), 100, "c")
    _steps(signalled, _at(11), _at(12), 100, "d")
    _heart(signalled, _at(9), 48, "sedentary")
    _heart(signalled, _at(10), 120, "active")
    found = read_brief(DAY, signalled.sources, world.log, NOW)
    assert found is not None
    played = [{
        "id": "media-1", "kind": "media", "start_ms": _ms(_at(8)), "end_ms": _ms(_at(8, 45)),
        "app": "Podcasts", "titles": [{"title": "A Very Long Episode Title " * 2, "artist": None}],
    }]
    with closing(open_event_log(world.log)) as conn:
        signals = gather_signals(conn, signalled.sources, found.segments, played)
    text = signals[_ms(_at(8))]
    assert text == (
        "Mac idle 08:10-08:40, locked 09:00-09:30, asleep 10:00-10:30; "
        "playing Podcasts 45m: A Very Long Episode Title A V…; "
        "phone: still 120m, walking 20m; ~350 steps"
    )
    assert len(text) <= SIGNALS_MAX_CHARS


def test_what_he_sent_and_what_played_are_signals_and_what_he_received_is_not(
    world: World,
) -> None:
    """Messages he sent per app, received ones left out; the app played most, with its title."""
    def burst(n: int, app: str, start: datetime, end: datetime, sent: int) -> dict[str, Any]:
        return {
            "id": f"messages-{n}", "kind": "messages", "start_ms": _ms(start), "end_ms": _ms(end),
            "app": app, "sent": sent, "received": 9,
        }

    items = [
        burst(1, "WeChat", _at(12, 5), _at(12, 20), 3),
        burst(2, "Messages", _at(12, 50), _at(13, 10), 1),  # runs into the next segment too
        burst(3, "Xiaohongshu", _at(12, 30), _at(12, 30), 0),  # only notifications came in
        {"id": "media-1", "kind": "media", "start_ms": _ms(_at(11, 50)),
         "end_ms": _ms(_at(12, 20)), "app": "Music",
         "titles": [{"title": "Song A", "artist": "X"}]},
        {"id": "media-2", "kind": "media", "start_ms": _ms(_at(12, 30)),
         "end_ms": _ms(_at(12, 35)), "app": "Podcasts", "titles": []},
    ]
    found = read_brief(DAY, world.sources, world.log, NOW)
    assert found is not None
    with closing(open_event_log(world.log)) as conn:
        signals = gather_signals(conn, world.sources, found.segments, items)
    assert signals[_ms(_at(12))] == "sent 4 messages (WeChat, Messages); playing Music 20m: Song A"
    assert signals[_ms(_at(13))] == "sent 1 message (Messages)"


def test_a_source_that_cannot_be_read_leaves_the_other_parts(
    world: World, tmp_path: Path,
) -> None:
    """A shipped store that is not a database loses the Mac part and nothing else."""
    directory = tmp_path / "imports"
    directory.mkdir()
    (directory / "shipped.db").write_bytes(b"this is not a database" * 100)
    broken = replace(world, sources=DaySources(VANCOUVER, None, world.memory, shipped=directory))
    _heart(broken, _at(12, 30), 61, None)
    found = read_brief(DAY, broken.sources, world.log, NOW)
    assert found is not None
    assert found.signals == {_ms(_at(12)): "heart 61"}


def test_the_digest_changes_when_a_late_heart_rate_arrives(world: World, tmp_path: Path) -> None:
    """The signals are part of what is shown, so a watch that syncs late asks for a rewrite."""
    signalled = _signalled(world, tmp_path / "imports", MARKS)
    before = read_brief(DAY, signalled.sources, world.log, NOW)
    assert before is not None
    _heart(signalled, _at(12, 20), 75, "active")
    after = read_brief(DAY, signalled.sources, world.log, NOW)
    assert after is not None
    assert after.signals[_ms(_at(12))].endswith("heart 55-75 sedentary")
    assert inputs_digest(after) != inputs_digest(before)
    again = read_brief(DAY, signalled.sources, world.log, NOW)
    assert again is not None
    assert inputs_digest(again) == inputs_digest(after)


# --- the gates ---------------------------------------------------------------------------------


def _answer(brief: Brief) -> dict[str, Any]:
    return _good(build_messages(brief)[0]["content"])


def _spans(answer: dict[str, Any]) -> list[dict[str, Any]]:
    return answer["spans"]  # type: ignore[no-any-return]


def test_a_good_answer_is_stored_as_the_spans_a_run_holds(brief: Brief) -> None:
    """A good answer is stored as the spans a run holds."""
    got = read_run(json.dumps(_answer(brief), ensure_ascii=False), "stop", brief)
    assert not isinstance(got, str), got
    assert [(s["id"], s["kind"], s["title"], s["category"], s["basis"]) for s in got] == [
        (f"doing-{_ms(_at(6))}", "unknown", None, "home", "inferred"),
        (f"doing-{_ms(_at(8))}", "doing", "写论文", "study", "stated"),
        (f"doing-{_ms(_at(11, 30))}", "unknown", None, "unknown", "inferred"),
        (f"doing-{_ms(_at(12))}", "doing", "吃午饭", "meal", "inferred"),
        (f"doing-{_ms(_at(13))}", "doing", "写论文", "study", "observed"),
        (f"doing-{_ms(_at(14, 45))}", "unknown", None, "unknown", "inferred"),
    ]
    studied = got[1]
    assert (studied["start_ms"], studied["end_ms"]) == (_ms(_at(8)), _ms(_at(11, 30)))
    assert studied["clues"] == [
        {"kind": "words", "at_ms": _ms(_at(9, 5)), "text": "he: 我在图书馆写论文"},
        {"kind": "screen", "at_ms": _ms(_at(10)), "text": "Writing the thesis in Overleaf"},
    ]
    assert studied["place_id"] is not None  # the kept place the library stays are in
    assert got[3]["place_id"] is None  # a cafe he keeps no place for
    assert got[2]["question"] == {
        "id": f"q:doing-{_ms(_at(11, 30))}", "text": "11:30 到 12:00 你在做什么？",
        "options": ["走路", "买咖啡"],
    }
    assert got[0]["question"] is None
    assert got[4]["start_ms"] == _ms(_at(13))  # two segments joined into one span
    assert got[4]["end_ms"] == _ms(_at(14, 45))


def test_a_span_keeps_the_first_clues_it_cites_up_to_the_cap(brief: Brief) -> None:
    """Citing more clues than a span keeps is not wrong; the first ones are kept."""
    extra = [Clue("words", _ms(_at(12, 30)), f"he: 第{n}口面") for n in range(MAX_SPAN_CLUES)]
    many = replace(brief, clues=[*brief.clues, *extra])
    prompt = build_messages(many)[0]["content"]
    answer = _good(prompt)
    given = re.findall(r"^(c\d+) \|", prompt, re.MULTILINE)
    assert len(given) > MAX_SPAN_CLUES
    answer["spans"][3]["clues"] = [*given, *given]  # every one, and every one again
    got = read_run(json.dumps(answer, ensure_ascii=False), "stop", many)
    assert not isinstance(got, str), got
    kept = [many.clues[int(label[1:]) - 1] for label in given[:MAX_SPAN_CLUES]]
    assert got[3]["clues"] == [{"kind": c.kind, "at_ms": c.at_ms, "text": c.text} for c in kept]


def test_a_fenced_answer_is_still_the_answer(brief: Brief) -> None:
    """A fenced answer is still the answer."""
    text = "```json\n" + json.dumps(_answer(brief), ensure_ascii=False) + "\n```"
    assert not isinstance(read_run(text, "stop", brief), str)


def test_a_span_may_split_a_segment_at_a_fact_edge_inside_it(brief: Brief) -> None:
    """A span may split a segment at a fact edge inside it."""
    prompt = build_messages(brief)[0]["content"]
    answer = _good(prompt)
    answer["spans"][1:2] = [  # 09:05 is where his talk with her was, inside 08:00-11:30
        _span(prompt, "08:00:00", "09:05:00", confidence="low"),
        _span(prompt, "09:05:00", "11:30:00", basis="stated", clues=[_clue(prompt, "图书馆")]),
    ]
    got = read_run(json.dumps(answer, ensure_ascii=False), "stop", brief)
    assert not isinstance(got, str), got
    assert [(s["start_ms"], s["end_ms"]) for s in got[1:3]] == [
        (_ms(_at(8)), _ms(_at(9, 5))), (_ms(_at(9, 5)), _ms(_at(11, 30))),
    ]


def _set(index: int, **fields: Any) -> Callable[[dict[str, Any], str], None]:  # noqa: ANN401
    def change(answer: dict[str, Any], _prompt: str) -> None:
        _spans(answer)[index].update(fields)

    return change


def _drop(index: int) -> Callable[[dict[str, Any], str], None]:
    def change(answer: dict[str, Any], _prompt: str) -> None:
        del _spans(answer)[index]

    return change


def _questions(count: int) -> Callable[[dict[str, Any], str], None]:
    def change(answer: dict[str, Any], _prompt: str) -> None:
        for span in _spans(answer)[:count]:
            span["question"] = {"text": "What was it?", "options": []}

    return change


def _stretch(answer: dict[str, Any], prompt: str) -> None:
    _spans(answer)[1]["to"] = _labels(prompt)["12:00:00"]  # runs over the next span


REJECTIONS: dict[str, tuple[Callable[[dict[str, Any], str], None], str]] = {
    "a time that is not given": (_set(1, to="t99"), "is not between two times given"),
    "a time written as a clock": (_set(1, to="11:30:00"), "is not between two times given"),
    "an end before the start": (_set(1, **{"from": "t5"}), "is not between two times given"),
    "a kind outside the vocabulary": (_set(1, kind="nap"), "kind must be one of"),
    "a category outside the vocabulary": (_set(1, category="chores"), "category must be one of"),
    "a basis the model may not claim": (_set(1, basis="planned"), "basis must be one of"),
    "a confidence outside the vocabulary": (_set(1, confidence="sure"), "confidence must be"),
    "an unknown stretch with a title": (_set(0, title="Resting"), "unknown span has a title"),
    "something he did with no title": (_set(1, title=None), "has no title"),
    "a title that is a place name": (_set(1, title="UVic Library"), "says where, not what"),
    "a title that is a place name with at": (_set(1, title="at uvic library"), "says where"),
    "a title that is a place name with 在": (_set(1, title="在UVic Library"), "says where"),
    "a title that is the cafe": (_set(3, title="Corner Cafe"), "says where, not what"),
    "a title that is a role word": (_set(1, title="Home"), "says where, not what"),
    "a title that is a category word": (_set(1, title="study."), "says where, not what"),
    "a title over the cap": (_set(1, title="x" * (TITLE_MAX_CHARS + 1)), "title is over"),
    "a title that is not text": (_set(1, title=7), "title is not text"),
    "a detail over the cap": (_set(1, detail="x" * 201), "detail is over"),
    "stated with no words behind it": (_set(1, clues=[]), "stated span cites none"),
    "stated on a picture alone": (_set(1, clues=["c4"]), "stated span cites none"),
    "observed with no fact under it": (_set(2, basis="observed"), "no fact under it"),
    "a clue that does not exist": (_set(3, clues=["c99"]), "is not one of the clues given"),
    "clues that are not a list": (_set(3, clues="c1"), "clues are not a list"),
    "stated on her words alone": (
        lambda a, p: _spans(a)[1].update(clues=[_clue(p, "好的，加油")]), "stated span cites none",
    ),
    "a question with no text": (_set(2, question={"text": " ", "options": []}), "no text"),
    "a question with five options": (
        _set(2, question={"text": "Which?", "options": list("abcde")}), "no text, or its options",
    ),
    "an option over the cap": (
        _set(2, question={"text": "Which?", "options": ["x" * 25]}), "option is over",
    ),
    "a repeated option": (
        _set(2, question={"text": "Which?", "options": ["a", "a"]}), "empty or repeated",
    ),
    "a question that is not an object": (_set(2, question="why?"), "not an object"),
    f"more than {MAX_QUESTIONS} questions": (
        _questions(MAX_QUESTIONS + 1), "more than 5 questions",
    ),
    "a span that is not an object": (lambda a, _p: _spans(a).append("x"), "span is not an object"),
    "a span laid over the next": (_stretch, "spans overlap at 11:30:00"),
    "a hole in the day": (_drop(3), "no span covers 12:00:00"),
    "a day that does not start with the window": (_drop(0), "no span covers 06:00:00"),
    "a day that stops short of the window": (_drop(-1), "no span covers 14:45:00"),
}


def test_a_span_may_not_join_time_at_his_mac_with_time_away_from_it_unless_he_said_so(
    brief: Brief,
) -> None:
    """Time his Mac shows him working and time it does not are two things, unless he said so."""
    [work] = fact_spans([{
        "id": "work-1", "lane": "you", "kind": "work", "start_ms": _ms(_at(13, 30)),
        "end_ms": _ms(_at(14, 45)), "state": "done", "projects": [{"name": "thesis"}], "apps": [],
    }])
    at_mac = replace(brief, facts=[*brief.facts, work])
    prompt = build_messages(at_mac)[0]["content"]
    assert "| 13:30-14:45 | place: UVic Library [study] | at his Mac | facts:" in prompt
    assert "| 13:00-13:30 | place: none known | facts:" in prompt
    joined = _good(prompt)  # 13:00-14:45 holds 13:00-13:30, away, and 13:30-14:45, at his Mac
    got = read_run(json.dumps(joined, ensure_ascii=False), "stop", at_mac)
    assert got == (
        "the span from 13:00 to 14:45 joins time his Mac shows him working (13:30-14:45) with "
        "time it does not; split it there"
    )
    joined["spans"][4].update(basis="stated", clues=[_clue(prompt, "图书馆写论文")])
    assert not isinstance(read_run(json.dumps(joined, ensure_ascii=False), "stop", at_mac), str)
    split = _good(prompt)
    split["spans"][4:5] = [
        _span(prompt, "13:00:00", "13:30:00", kind="unknown", title=None, confidence="low"),
        _span(prompt, "13:30:00", "14:45:00", basis="observed", title="写论文"),
    ]
    assert not isinstance(read_run(json.dumps(split, ensure_ascii=False), "stop", at_mac), str)


@pytest.mark.parametrize("name", REJECTIONS)
def test_each_gate_rejects_its_answer_whole(name: str, brief: Brief) -> None:
    """Each gate rejects its answer whole."""
    mutate, reason = REJECTIONS[name]
    prompt = build_messages(brief)[0]["content"]
    answer = _good(prompt)
    mutate(answer, prompt)
    got = read_run(json.dumps(answer, ensure_ascii=False), "stop", brief)
    assert isinstance(got, str)
    assert reason in got


def test_an_answer_that_is_not_the_object_asked_for_is_rejected(brief: Brief) -> None:
    """An answer that is not the object asked for is rejected."""
    good = json.dumps(_answer(brief), ensure_ascii=False)
    assert read_run(good, "length", brief) == "cut off by the output limit"
    assert read_run("{", "stop", brief) == "not valid JSON"
    assert read_run("[]", "stop", brief) == "not a JSON object"
    assert read_run(None, "stop", brief) == "not valid JSON"
    assert read_run("{}", "stop", brief) == 'no "spans" list'
    assert read_run('{"spans": []}', "stop", brief) == 'no "spans" list'


def test_a_title_may_name_a_place_inside_a_phrase(brief: Brief) -> None:
    """A title may name a place inside a phrase."""
    prompt = build_messages(brief)[0]["content"]
    answer = _good(prompt)
    _spans(answer)[1]["title"] = "Write at UVic Library"
    assert not isinstance(read_run(json.dumps(answer), "stop", brief), str)


def test_no_category_word_is_missing_from_the_gate(brief: Brief) -> None:
    """No category word is missing from the gate."""
    prompt = build_messages(brief)[0]["content"]
    for category in CATEGORIES:
        answer = _good(prompt)
        _spans(answer)[1]["title"] = category
        got = read_run(json.dumps(answer), "stop", brief)
        assert isinstance(got, str)
        assert "says where, not what" in got


# --- a short run (ADR 0310) ---------------------------------------------------------------------


def _written(brief: Brief) -> list[dict[str, Any]]:
    """The spans of the good answer, as a stored run holds them."""
    got = read_run(_good_text(build_messages(brief)[0]["content"]), "stop", brief)
    assert not isinstance(got, str), got
    return got


def test_a_short_run_keeps_the_spans_before_the_last_and_writes_from_where_it_starts(
    brief: Brief,
) -> None:
    """The window starts where the last written span does; the ones before are context only."""
    written = _written(brief)
    short = narrow(brief, written)
    assert short.kept == written[:-1]
    assert short.waking == (_ms(_at(14, 45)), brief.waking.end_ms)
    lines = build_messages(short)[0]["content"].split("\n")
    assert "WRITTEN ALREADY (local | title | category; kept as they are):" in lines
    assert "08:00-11:30 | 写论文 | study" in lines
    assert "11:30-12:00 | unknown | unknown" in lines
    assert "WAKING WINDOW: t0 14:45:00 to t1 15:00:00" in lines
    assert not any(line.startswith("c") and "图书馆" in line for line in lines)  # 09:05 is kept
    got = read_run(_tile(build_messages(short)[0]["content"]), "stop", short)
    assert not isinstance(got, str), got
    assert [(s["start_ms"], s["end_ms"]) for s in got] == [(_ms(_at(14, 45)), _ms(_at(15)))]


def test_a_short_run_tells_the_mac_foreground_from_where_its_window_starts(brief: Brief) -> None:
    """The quarter hours that end by the time the window starts are left out."""
    quarters = [
        Quarter(_ms(_at(14, 15)), _ms(_at(14, 30)), "Claude 9m", "Claude"),
        Quarter(_ms(_at(14, 30)), _ms(_at(14, 45)), "WeChat Weixin 4m", None),
        Quarter(_ms(_at(14, 45)), _ms(_at(15)), "Claude Jarvis 12m typed 15s", "Claude Jarvis"),
    ]
    short = narrow(replace(brief, foreground=quarters), _written(brief))
    assert short.foreground == [quarters[2]]


def test_a_run_that_does_not_tile_up_to_its_last_span_leaves_the_whole_day(brief: Brief) -> None:
    """Late facts that moved where the day starts, or a single span: nothing is kept."""
    written = _written(brief)
    assert narrow(brief, written[1:]) is brief  # it no longer starts where the window does
    assert narrow(brief, written[:1]) is brief  # one span: it is the open one


def test_the_questions_of_a_short_run_count_with_the_ones_it_keeps(brief: Brief) -> None:
    """The day's cap holds over the kept spans and the new ones together."""
    written = _written(brief)
    asking = {"id": "q:x", "text": "What was it?", "options": ["a", "b"]}
    kept_full = [{**span, "question": asking} for span in written[:MAX_QUESTIONS]]
    short = narrow(brief, [*kept_full, written[-1]])
    assert sum(s["question"] is not None for s in short.kept) == MAX_QUESTIONS
    answer = json.loads(_tile(build_messages(short)[0]["content"]))
    assert not isinstance(read_run(json.dumps(answer), "stop", short), str)  # none new: fine
    answer["spans"][0]["question"] = {"text": "What was it?", "options": ["a", "b"]}
    assert read_run(json.dumps(answer), "stop", short) == (
        f"more than {MAX_QUESTIONS} questions in the day ({MAX_QUESTIONS} kept)"
    )


# --- the run: stored, served, and what stays when a call does not land -------------------------


def _latest(memory: Path) -> Any:  # noqa: ANN401 — the stored run
    with closing(open_memory_db(memory)) as conn:
        return latest_doing_run(conn, KEY)


def _rows(memory: Path) -> list[tuple[Any, ...]]:
    with closing(open_memory_db(memory)) as conn:
        return conn.execute(
            "SELECT day, draft, covers_until_ms, model, inputs_digest, spans FROM doing_runs "
            "ORDER BY rowid",
        ).fetchall()


def test_a_run_is_asked_gated_and_appended_with_what_it_rests_on(
    world: World, brief: Brief, tmp_path: Path,
) -> None:
    """A run is asked gated and appended with what it rests on."""
    writer = _Writer(_good_text)
    recorder, cost_log = _recorder(tmp_path)
    with closing(cost_log):
        composed = compose_run(brief, SETTINGS, writer, recorder)
        costs = cost_log.execute("SELECT payload_json FROM events WHERE type = 'cost.recorded'")
        accounted = [json.loads(row[0]) for row in costs]
    assert composed.spans is not None
    assert (composed.model, composed.tokens_in, composed.tokens_out) == ("fake-haiku", 100, 50)
    assert writer.systems == ["write what he was doing"]
    assert [a["kind"] for a in accounted] == ["doing_run"]
    store_run(world.memory, brief, composed)
    ((day, draft, covers, model, digest, spans),) = _rows(world.memory)
    assert (day, draft, covers, model) == (KEY, 1, _ms(NOW), "fake-haiku")
    assert digest == inputs_digest(brief)
    assert json.loads(spans) == composed.spans


def test_a_rejected_answer_is_asked_again_once_and_a_second_failure_stores_nothing(
    world: World, brief: Brief, tmp_path: Path,
) -> None:
    """A rejected answer is asked again once and a second failure stores nothing."""
    asked: list[str] = []

    def second_time_lucky(prompt: str) -> str:
        asked.append(prompt)
        return "not json" if len(asked) == 1 else _good_text(prompt)

    writer = _Writer(second_time_lucky)
    recorder, cost_log = _recorder(tmp_path)
    with closing(cost_log):
        composed = compose_run(brief, SETTINGS, writer, recorder)
        assert composed.spans is not None
        assert (len(writer.prompts), composed.tokens_in) == (2, 200)
        # the second ask shows the refused answer and why, after the same day
        assert [m["role"] for m in writer.asked[1]] == ["user", "assistant", "user"]
        assert writer.asked[1][0] == writer.asked[0][0]
        assert writer.asked[1][1]["content"] == "not json"
        assert "The gates refused that answer: not valid JSON." in writer.asked[1][2]["content"]
        always = _Writer(lambda _p: "not json")
        failed = compose_run(brief, SETTINGS, always, recorder)
    assert (failed.spans, failed.why, len(always.prompts)) == (None, "not valid JSON", 2)
    with pytest.raises(ValueError, match="rejected"):
        store_run(world.memory, brief, failed)
    assert _rows(world.memory) == []


def test_the_day_serves_the_stored_run_up_to_what_it_covers_and_his_words_over_it(
    world: World, brief: Brief, tmp_path: Path,
) -> None:
    """The day serves the stored run up to what it covers and his words over it."""
    recorder, cost_log = _recorder(tmp_path)
    with closing(cost_log):
        composed = compose_run(brief, SETTINGS, _Writer(_good_text), recorder)
    store_run(world.memory, brief, composed)

    def served(now: datetime) -> list[dict[str, Any]]:
        day = _read_day(world.log, VANCOUVER, world.memory, None, DAY, _ms(now))
        return [s for s in day["spans"] if s["lane"] == "doing" and s["basis"] != "planned"]

    spans = served(NOW)
    assert [(s["title"], s["category"], s["basis"]) for s in spans] == [
        (None, "home", "inferred"), ("写论文", "study", "stated"), (None, "unknown", "inferred"),
        ("吃午饭", "meal", "inferred"), ("写论文", "study", "observed"),
        (None, "unknown", "inferred"),
    ]
    assert spans[1]["clues"][0]["kind"] == "words"
    assert spans[2]["question"]["options"] == ["走路", "买咖啡"]
    assert spans[1]["place_id"] is not None
    later = served(_at(16))  # an hour on: the run speaks to 15:00, the program's cut after it
    assert [s["title"] for s in later[:6]] == [s["title"] for s in spans]
    assert later[6]["start_ms"] == _ms(NOW)
    assert (later[6]["basis"], later[6]["confidence"]) == ("observed", "low")
    append_doing_answer(
        world.memory, day=KEY, span_id=spans[2]["id"], start_ms=_ms(_at(11, 30)),
        end_ms=_ms(_at(12)), choice=None, text="买咖啡", lat=None, lng=None, place_id=None,
        origin="timeline",
    )
    answered = served(NOW)[2]
    assert (answered["title"], answered["basis"], answered["confirmed"], answered["question"]) == (
        "买咖啡", "stated", True, None,
    )


def test_a_newer_run_replaces_the_older_one_and_the_older_one_stays(
    world: World, brief: Brief,
) -> None:
    """A newer run replaces the older one and the older one stays."""
    for title in ("First", "Second"):
        spans = [
            {"id": s.id, "start_ms": s.start_ms, "end_ms": s.end_ms, "kind": "doing",
             "title": title, "detail": None, "category": "work", "basis": "inferred",
             "confidence": "low", "clues": [], "question": None, "place_id": None}
            for s in brief.segments
        ]
        append_doing_run(
            world.memory, day=KEY, draft=True, covers_until_ms=_ms(NOW), model="m",
            inputs_digest="x", spans=spans,
        )
    with closing(open_memory_db(world.memory)) as conn:
        newest = latest_doing_run(conn, KEY)
    assert newest is not None
    assert newest.spans[0]["title"] == "Second"
    assert [json.loads(row[5])[0]["title"] for row in _rows(world.memory)] == ["First", "Second"]


# --- the settings ------------------------------------------------------------------------------

SETTINGS_CASES = {
    "no block": (None, None),
    "not enabled": ({"preset": "p", "prompt": "x"}, None),
    "disabled": ({"enabled": False, "preset": "p", "prompt": "x"}, None),
    "enabled without a prompt": ({"enabled": True, "preset": "p"}, None),
    "enabled with a blank prompt": ({"enabled": True, "prompt": "  "}, None),
    "a preset and a prompt": (
        {"enabled": True, "preset": "p", "prompt": " write \n"}, DoingSettings("p", "write"),
    ),
    "the default preset": (
        {"enabled": True, "prompt": "write"}, DoingSettings("haiku-bg", "write"),
    ),
    "drafts on the clock": (
        {"enabled": True, "prompt": "write", "clock_drafts": True},
        DoingSettings("haiku-bg", "write", clock_drafts=True),
    ),
    "drafts on the clock only when true": (
        {"enabled": True, "prompt": "write", "clock_drafts": "yes"},
        DoingSettings("haiku-bg", "write"),
    ),
}


@pytest.mark.parametrize("name", SETTINGS_CASES)
def test_the_doing_block_of_the_config(name: str) -> None:
    """The doing block of the config."""
    raw, expected = SETTINGS_CASES[name]
    assert DoingSettings.from_config(raw) == expected


def test_the_shipped_block_is_on_on_opus_and_holds_the_prompt() -> None:
    """The shipped block is on, on Opus (ADR 0310), and holds the prompt."""
    shipped = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    settings = DoingSettings.from_config(shipped["doing"])
    assert settings is not None
    assert settings.preset == "opus-long"
    assert not settings.clock_drafts  # today is written when he asks and at night (ADR 0311)
    assert shipped["llm"]["presets"]["opus-long"]["model"] == "claude-opus-5-5"
    for needle in (
        "TITLE LANGUAGE", "never says where", "at most 5", "unknown", "stated", "WRITTEN ALREADY",
    ):
        assert needle in settings.prompt


# --- the schedule ------------------------------------------------------------------------------


@dataclass
class Clock:
    """The moment the brain's clock shows; a test moves it."""

    now: datetime


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """The brain's clock, which stamps each run's ``written_at``."""
    ticking = Clock(NOW)
    monkeypatch.setattr("jarvis.state.memory_db.local_now", lambda: ticking.now)
    monkeypatch.setattr("jarvis.runtime.doing_run.local_now", lambda: ticking.now)
    return ticking


def _down(_prompt: str) -> str:
    msg = "provider down"
    raise TimeoutError(msg)


def _schedule(world: World, writer: _Writer, settings: DoingSettings = CLOCKED) -> DoingSchedule:
    return DoingSchedule(
        settings=settings, sources=world.sources, memory_db=world.memory, event_log_path=world.log,
        llm_config={}, pricing_table={}, client=writer,  # type: ignore[arg-type]
    )


def test_the_schedule_writes_every_two_hours_and_then_only_the_newest_part(
    world: World, clock: Clock,
) -> None:
    """At most every two hours, only on changed inputs; after the first run, from the last span."""
    writer = _Writer(_tile)
    schedule = _schedule(world, writer)

    def tick(now: datetime) -> dict[str, str]:
        clock.now = now
        return schedule.tick(now)

    assert tick(NOW) == {KEY: "landed (draft, 7 spans, 0 questions)"}  # the other days: no inputs
    first = _latest(world.memory).spans
    assert tick(NOW + timedelta(minutes=5)) == {}  # looked at a moment ago
    assert tick(NOW + CHECK_EVERY) == {}  # nothing changed
    _say(world.memory, "n1", _at(15, 10), "allen", "back at the library")
    assert tick(_at(16, 30)) == {}  # changed, but the last run is not two hours old
    assert len(writer.prompts) == 1
    assert tick(NOW + SHORT_EVERY + CHECK_EVERY) == {KEY: "landed (short, 7 spans, 0 questions)"}
    shown = writer.prompts[1]
    assert "WRITTEN ALREADY (local | title | category; kept as they are):" in shown
    assert "13:30-14:45 | Work on it | work" in shown  # a span it keeps, as context
    assert "WAKING WINDOW: t0 14:45:00 to t2 17:15:00" in shown  # from where the last one starts
    assert "我在图书馆写论文" not in shown  # a clue of the kept part is not shown again
    second = _latest(world.memory).spans
    assert second[:-1] == first[:-1]  # the spans before the last are kept as written
    assert (second[-1]["start_ms"], second[-1]["end_ms"]) == (_ms(_at(14, 45)), _ms(_at(17, 15)))
    assert [row[1] for row in _rows(world.memory)] == [1, 1]
    assert tick(_at(17, 30)) == {}  # unchanged again


def test_without_clock_drafts_today_waits_for_his_asking_and_the_night(
    world: World, clock: Clock,
) -> None:
    """Off (as shipped, ADR 0311): the clock never writes today; he asks, and the night writes."""
    writer = _Writer(_tile)
    schedule = _schedule(world, writer, SETTINGS)

    def tick(now: datetime) -> dict[str, str]:
        clock.now = now
        return schedule.tick(now)

    assert tick(NOW) == {}
    _say(world.memory, "n1", _at(15, 10), "allen", "back at the library")
    assert tick(NOW + SHORT_EVERY + CHECK_EVERY) == {}  # changed, and two hours on: still not
    assert writer.prompts == []
    clock.now = _at(17, 30)
    assert schedule.tidy() == "landed (draft, 7 spans, 0 questions)"
    _say(world.memory, "n2", _at(17, 40), "allen", "still here")
    assert tick(_at(20)) == {}  # what he asked for stands until he asks again or the day ends
    next_morning = _at(8, day=DAY + timedelta(days=1))
    assert tick(next_morning) == {KEY: "landed (final, 7 spans, 0 questions)"}
    assert len(writer.prompts) == 2
    assert [row[1] for row in _rows(world.memory)] == [1, 0]


def test_his_asking_writes_today_at_once_and_twice_asking_writes_once(
    world: World, clock: Clock,
) -> None:
    """Her tool writes today's newest part whenever the last run was; unchanged inputs ask none."""
    writer = _Writer(_tile)
    schedule = _schedule(world, writer)
    clock.now = NOW
    schedule.tick(NOW)
    (tool,) = build_tidy_tool(schedule.tidy)
    assert tool.name == "tidy_day"
    assert not tool.read_only
    _say(world.memory, "n1", _at(15, 10), "allen", "back at the library")
    clock.now = _at(15, 20)  # twenty minutes after the last run
    assert tool.handler({}, None) == {  # type: ignore[arg-type]
        "done": True, "outcome": "landed (short, 7 spans, 0 questions)",
    }
    assert len(writer.prompts) == 2
    assert tool.handler({}, None) == {"done": False, "outcome": "unchanged"}  # type: ignore[arg-type]
    assert len(writer.prompts) == 2
    assert len(_rows(world.memory)) == 2


def test_the_timeline_asks_by_its_route_and_the_day_serves_what_was_written(
    world: World, clock: Clock, tmp_path: Path,
) -> None:
    """``POST /inherent/day/tidy`` writes today now; ``GET /inherent/day`` serves the new run."""
    writer = _Writer(_tile)
    schedule = _schedule(world, writer)
    clock.now = NOW
    peers = _peers(tmp_path, doing_tidy=functools.partial(asyncio.to_thread, schedule.tidy))
    _refused_without_credentials(peers, "/inherent/day/tidy", {})
    assert writer.prompts == []
    reply = peers.paired.post("/inherent/day/tidy")
    assert reply.status_code == 200
    assert reply.json() == {
        "ok": True, "written": True, "outcome": "landed (draft, 7 spans, 0 questions)",
    }
    served = _read_day(world.log, VANCOUVER, world.memory, None, DAY, _ms(NOW))
    titles = {
        s["title"] for s in served["spans"] if s["lane"] == "doing" and s["basis"] != "planned"
    }
    assert titles == {"Work on it"}  # what was just written, not the program's wordless cut
    assert peers.paired.post("/inherent/day/tidy").json() == {
        "ok": True, "written": False, "outcome": "unchanged",
    }


def test_a_day_that_is_over_gets_a_final_run_even_when_nothing_changed(
    world: World, clock: Clock,
) -> None:
    """A day that is over gets a final run even when nothing changed."""
    writer = _Writer(_tile)
    schedule = _schedule(world, writer)
    clock.now = NOW
    schedule.tick(NOW)
    next_morning = _at(8, day=DAY + timedelta(days=1))
    clock.now = next_morning
    assert schedule.tick(next_morning) == {KEY: "landed (final, 7 spans, 0 questions)"}
    assert [row[1] for row in _rows(world.memory)] == [1, 0]
    final = _rows(world.memory)[-1]
    assert final[2] == day_window(DAY, VANCOUVER)[1]  # it covers the waking part to midnight
    clock.now = next_morning + timedelta(minutes=20)
    assert schedule.tick(clock.now) == {}  # a final run on unchanged inputs stands
    assert len(writer.prompts) == 2


def test_late_rows_rewrite_a_past_day_and_a_failed_call_waits_two_hours(
    world: World, clock: Clock,
) -> None:
    """Late rows rewrite a past day, whole, and a failed call waits as long as a short run."""
    writer = _Writer(_tile)
    schedule = _schedule(world, writer)
    morning = _at(8, day=DAY + timedelta(days=1))
    clock.now = morning
    assert schedule.tick(morning) == {KEY: "landed (final, 7 spans, 0 questions)"}
    _say(world.memory, "late", _at(20), "allen", "a row that arrived late")
    clock.now = morning + timedelta(hours=2)
    assert schedule.tick(clock.now) == {KEY: "landed (final, 7 spans, 0 questions)"}
    assert len(_rows(world.memory)) == 2
    _say(world.memory, "later", _at(21), "allen", "and another")
    writer.reply = _down
    clock.now = morning + timedelta(hours=4)
    assert schedule.tick(clock.now) == {KEY: "failed"}
    assert len(_rows(world.memory)) == 2  # nothing stored; the run before stands
    clock.now += CHECK_EVERY
    assert schedule.tick(clock.now) == {}  # not tried again for two hours
    writer.reply = lambda _p: "not json"
    clock.now = morning + timedelta(hours=6, minutes=1)
    outcome = schedule.tick(clock.now)
    assert outcome == {KEY: "rejected (not valid JSON)"}
    assert len(_rows(world.memory)) == 2
    writer.reply = _tile
    clock.now = morning + timedelta(hours=8, minutes=2)
    assert schedule.tick(clock.now) == {KEY: "landed (final, 7 spans, 0 questions)"}
    assert "WRITTEN ALREADY" not in writer.prompts[-1]  # a final run writes the whole day
    assert len(_rows(world.memory)) == 3


def test_only_the_last_days_are_looked_at_and_a_day_with_nothing_is_not_asked(
    world: World, clock: Clock,
) -> None:
    """Only the last days are looked at and a day with nothing is not asked."""
    writer = _Writer(_tile)
    schedule = _schedule(world, writer)
    long_after = _at(8, day=DAY + timedelta(days=5))
    clock.now = long_after
    assert schedule.tick(long_after) == {}  # 10-07 is more than three days back
    assert writer.prompts == []
    assert set(schedule._looked) == {  # noqa: SLF001 — what was looked at
        (DAY + timedelta(days=n)).isoformat() for n in (2, 3, 4, 5)
    }


def test_the_schedule_builds_its_client_from_the_preset_when_none_is_given(
    world: World, clock: Clock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The schedule builds its client from the preset when none is given."""
    built: list[str] = []
    writer = _Writer(_tile)

    def build(_llm_config: object, preset: str) -> _Writer:
        built.append(preset)
        return writer

    monkeypatch.setattr(runtime, "build_compact_client", build)
    schedule = DoingSchedule(
        settings=CLOCKED, sources=world.sources, memory_db=world.memory, event_log_path=world.log,
        llm_config={"presets": {"p": {}}}, pricing_table={},
    )
    clock.now = NOW
    assert schedule.tick(NOW)
    assert built == ["p"]


# --- the replay script -------------------------------------------------------------------------


def _replay(world: World, writer: _Writer, *, dry_run: bool) -> list[dict[str, Any]]:
    return doing_replay.replay(
        [DAY, DAY - timedelta(days=1)], world.sources, world.log, world.memory, SETTINGS, writer,  # type: ignore[arg-type]
        NOW, dry_run=dry_run, pricing_table={},
    )


def test_a_dry_run_prints_the_gated_spans_and_stores_nothing(world: World) -> None:
    """A dry run prints the gated spans and stores nothing."""
    writer = _Writer(_good_text)
    found, nothing = _replay(world, writer, dry_run=True)
    assert nothing == {
        "date": "2026-10-06", "outcome": "skipped (no waking window or no inputs)",
    }
    assert (found["date"], found["final"], found["segments"], found["clues"]) == (KEY, False, 7, 4)
    assert found["outcome"] == "gated (dry run)"
    assert found["spans"][1] == "08:00-11:30 写论文 [study/stated/high]"
    assert found["spans"][2] == (
        "11:30-12:00 (unknown) [unknown/inferred/low]"
        " ? 11:30 到 12:00 你在做什么？ ['走路', '买咖啡']"
    )
    assert (found["tokens_in"], found["tokens_out"]) == (100, 50)
    assert _rows(world.memory) == []
    with closing(open_event_log(world.log)) as conn:  # the day's own log is only read
        assert conn.execute("SELECT count(*) FROM events WHERE type = 'cost.recorded'").fetchone()[
            0
        ] == 0


def test_a_replay_stores_the_run_unless_it_is_a_dry_run_and_a_rejection_stores_none(
    world: World,
) -> None:
    """A replay stores the run unless it is a dry run and a rejection stores none."""
    (found, _skipped) = _replay(world, _Writer(_good_text), dry_run=False)
    assert found["outcome"].startswith("gated (doing-run:")
    assert len(_rows(world.memory)) == 1
    (rejected, _skipped) = _replay(world, _Writer(lambda _p: "nope"), dry_run=False)
    assert rejected["outcome"] == "rejected (not valid JSON)"
    assert len(_rows(world.memory)) == 1


def test_a_brief_only_replay_prints_the_writers_input_and_asks_no_model(world: World) -> None:
    """A brief-only replay prints the writer's input and asks no model."""
    found, _skipped = doing_replay.replay(
        [DAY, DAY - timedelta(days=1)], world.sources, world.log, world.memory, SETTINGS, None,
        NOW, dry_run=True, pricing_table={},
    )
    assert (found["outcome"], found["segments"], found["clues"]) == ("brief only", 7, 4)
    assert found["brief"].startswith(f"DAY: {KEY} ")
    assert "\nSEGMENTS (the program's cut of the window;" in found["brief"]
    assert "\nABOUT HIM (background from his memory and his past answers;" in found["brief"]
    assert "spans" not in found
    assert _rows(world.memory) == []


def test_the_days_to_replay_are_the_dates_and_the_range_together() -> None:
    """The days to replay are the dates and the range together."""
    import argparse  # noqa: PLC0415 — only this test builds the namespace by hand

    args = argparse.Namespace(
        date=[date(2026, 10, 1)], first=date(2026, 10, 5), last=date(2026, 10, 7),
    )
    assert doing_replay._days(args) == [  # noqa: SLF001
        date(2026, 10, 1), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7),
    ]
    for bad in (
        argparse.Namespace(date=None, first=None, last=None),
        argparse.Namespace(date=None, first=date(2026, 10, 5), last=None),
    ):
        with pytest.raises(SystemExit):
            doing_replay._days(bad)  # noqa: SLF001
