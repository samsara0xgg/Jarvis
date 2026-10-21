"""ADR 0179, 0299 — owner-set reminders: the ring of a thing, the tick that fires it, the routes.

The real ``schedule`` tool runs through ``ToolRegistry.dispatch`` onto a real event log and arms the
ring of the thing it keeps; the real ``Reminders`` tick folds that log under a fake clock, and the
real ``/inherent/notices`` routes serve what it fired. Each check asserts the events the log
holds, what was said, and what the routes serve.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from jarvis.execution.tools import build_default_registry
from jarvis.runtime.reminders import Reminders
from jarvis.shared import CallerPrincipal, lang
from jarvis.state import things
from tests.support.reminders import PRIVATE, ReminderWorld

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

SPEAKERS = {"name": "Speakers", "private": False}


@pytest.fixture
def world(tmp_path: Path) -> Iterator[ReminderWorld]:
    """A fresh world with Chinese fixed text, as the daemon runs."""
    before = lang.language()
    lang.set_language("zh")
    made = ReminderWorld(tmp_path)
    yield made
    made.conn.close()
    lang.set_language(before)


def test_set_list_fire_once_with_card_and_one_spoken_line(world: ReminderWorld) -> None:
    """Set -> pending -> not yet due -> due fires once, one line and a silent card."""
    made = world.set(30)
    assert made["id"].startswith(things.RING_PREFIX)
    assert made["summary"].startswith('Kept "和 employer 一对一" for ')
    assert [(r["id"], r["text"]) for r in world.waiting()] == [(made["id"], made["line"])]

    world.clock = world.start + timedelta(minutes=29)
    assert world.reminders.tick() == 0
    assert not world.said

    world.clock = world.start + timedelta(minutes=30, seconds=10)
    assert world.reminders.tick() == 1
    assert world.said == [f"提醒：{made['line']}"]
    (fired,) = world.fired()
    assert (fired["reminder_id"], fired["delivered"]) == (made["id"], "speak")
    assert fired["late_ms"] == 10_000

    client = world.app()
    (card,) = client.get("/inherent/notices").json()["notices"]
    assert (card["id"], card["level"], card["title"]) == (made["id"], "card", made["line"])

    world.clock += timedelta(minutes=5)
    assert world.reminders.tick() == 0  # exactly once
    assert len(world.said) == 1
    assert len(world.fired()) == 1
    assert world.waiting() == []

    assert (
        client.post(f"/inherent/notices/{card['id']}", json={"action": "seen"}).status_code == 200
    )
    assert [c["id"] for c in client.get("/inherent/notices").json()["notices"]] == [card["id"]]
    assert (
        client.post(f"/inherent/notices/{card['id']}", json={"action": "dismissed"}).status_code
        == 200
    )
    assert client.get("/inherent/notices").json()["notices"] == []
    assert (
        client.post("/inherent/notices/reminder-nope", json={"action": "seen"}).status_code == 404
    )


def test_cancel_prevents_firing_and_only_an_unknown_id_is_an_error(world: ReminderWorld) -> None:
    """A cancelled thing's ring never rings; cancelling twice changes nothing."""
    kept, dropped = world.set(10, "keep"), world.set(10, "drop")
    cancelled = world.call("schedule", {"action": "cancel", "id": dropped["thing"]})
    assert cancelled["summary"] == 'Cancelled "drop".'
    assert [r["id"] for r in world.waiting()] == [kept["id"]]
    world.clock = world.start + timedelta(minutes=11)
    assert world.reminders.tick() == 1
    assert world.said == [f"Reminder: {kept['line']}"]
    again = world.call("schedule", {"action": "cancel", "id": dropped["thing"]})
    assert again["summary"] == 'Already cancelled, nothing changed: "drop".'
    unknown = world.call("schedule", {"action": "cancel", "id": "zzzz9"})
    assert unknown["code"] == "not_found"
    assert "schedule() lists what is open" in unknown["error"]


def test_schedule_refuses_a_bad_time(world: ReminderWorld) -> None:
    """The past, more than a year ahead, not a time, no words: nothing is scheduled."""
    local = world.start.astimezone()
    past = (local - timedelta(minutes=5)).isoformat()
    far = (local + timedelta(days=400)).isoformat()
    for at, code in (
        (past, "time_in_past"),
        (far, "too_far_ahead"),
        ("soon", "invalid_time"),
        ("tomorrow 9am", "invalid_time"),
    ):
        refused = world.call("schedule", {"action": "add", "at": at, "what": "x"})
        assert refused["code"] == code, at
    blank = world.call(
        "schedule", {"action": "add", "at": world.start.isoformat(), "what": " "},
    )
    assert blank["code"] == "empty_text"
    assert world.waiting() == []


def test_a_reminder_missed_while_down_fires_late_and_says_so(world: ReminderWorld) -> None:
    """The daemon was off or the Mac asleep: the first tick after fires, once, and says how late."""
    first, second = world.set(10), world.set(11, "call mum")
    world.clock = world.start + timedelta(minutes=20)  # nothing ticked meanwhile
    restarted = Reminders(world.paths.event_log)  # a fresh daemon: nothing in memory
    restarted.now = lambda: world.clock
    restarted.may_speak = lambda: True
    restarted.say = world.said.append
    restarted.output = lambda fresh=False: dict(PRIVATE)  # noqa: ARG005
    assert restarted.tick() == 2
    assert world.said == [
        f"提醒：{first['line']}（晚了 10 分钟）",
        f"Reminder: {second['line']} (9 minutes late)",
    ]
    assert restarted.tick() == 0
    assert world.reminders.tick() == 0  # the first daemon sees it already fired


def test_more_than_twelve_hours_late_is_a_card_only(world: ReminderWorld) -> None:
    """It still fires, as a card that says how late, and says nothing aloud."""
    world.set(10)
    world.clock = world.start + timedelta(hours=13)
    assert world.reminders.tick() == 1
    assert not world.said
    assert world.fired()[0]["delivered"] == "card_sound"
    (card,) = world.app().get("/inherent/notices").json()["notices"]
    assert "晚了 13 小时" in card["line"]


def test_quiet_and_hold_do_not_stop_the_card_and_a_call_stops_only_the_voice(
    world: ReminderWorld,
) -> None:
    """Job mail at dnd or in a call serves nothing; the reminder card is served all the same."""
    world.set(1)
    world.hold = "call"
    world.clock = world.start + timedelta(minutes=2)
    assert world.reminders.tick() == 1
    assert not world.said
    assert world.fired()[0]["delivered"] == "card_sound"

    mail = SimpleNamespace(  # what job mail answers at quiet dnd or in a call: no notice, a hold
        notices=lambda: {"notices": [], "audio_private": True, "hold": "call"},
        act=lambda *_a: None,
        delete=lambda *_a: None,
        flag=lambda *_a: None,
        ledger=dict,
    )
    body = world.app(mail).get("/inherent/notices").json()
    assert [n["level"] for n in body["notices"]] == ["card_sound"]
    assert body["hold"] == "call"


def test_spoken_on_speakers_too_and_a_card_with_sound_when_not_allowed(
    world: ReminderWorld,
) -> None:
    """Speakers still speak; a mute or a live conversation make the reminder a card with sound."""
    world.device = dict(SPEAKERS)
    speakers = world.set(1, "on speakers")
    world.clock = world.start + timedelta(minutes=2)
    world.reminders.tick()
    assert world.said == [f"Reminder: {speakers['line']}"]
    assert world.fired()[0]["delivered"] == "speak"

    world.reminders.may_speak = lambda: False  # muted, or a conversation is live
    muted = world.set(1, "while muted")
    world.clock += timedelta(minutes=2)
    world.reminders.tick()
    assert len(world.said) == 1
    assert world.fired()[1]["delivered"] == "card_sound"
    levels = {
        n["title"]: n["level"] for n in world.app().get("/inherent/notices").json()["notices"]
    }
    assert levels == {speakers["line"]: "card", muted["line"]: "card_sound"}


def test_schedule_is_the_only_reminder_tool_on_the_menu() -> None:
    """The model and the Tier 0 router reach it; the tools it replaces are gone."""
    registry = build_default_registry()
    menu = {t.name for t in registry.for_caller(CallerPrincipal.JARVIS_LLM)}
    router = {t.name for t in registry.for_caller(CallerPrincipal.REGEX_ROUTER)}
    old = {
        "create_memo",
        "list_memos",
        "set_reminder",
        "list_reminders",
        "cancel_reminder",
        "my_schedule",
        "remember",
    }
    assert {"schedule", "keep", "kept"} <= menu & router
    assert not (menu | router | {t.name for t in registry.get_definitions()}) & old
