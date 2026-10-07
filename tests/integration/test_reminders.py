"""ADR 0179 — owner-set reminders: the tools, the tick that fires them, and the notice routes.

The real tools run through ``ToolRegistry.dispatch`` onto a real event log; the real ``Reminders``
tick folds that log under a fake clock, and the real ``/inherent/notices`` routes serve what it
fired. Each check asserts the events the log holds, what was said, and what the routes serve.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime.inherent_loop import _notice_deps
from jarvis.runtime.reminders import Reminders
from jarvis.shared import ActionRequest, CallerPrincipal, lang
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

PRIVATE = {"name": "AirPods Pro", "private": True}
SPEAKERS = {"name": "Speakers", "private": False}


class _World:
    """One runtime root with the real tools, a fake clock and a recording voice."""

    def __init__(self, tmp_path: Path) -> None:
        self.paths = bootstrap_runtime(tmp_path)
        self.conn = open_event_log(self.paths.event_log)
        self.registry = build_default_registry()
        self.lifecycle = ActionLifecycle()
        self.start = datetime.now(UTC).replace(microsecond=0)
        self.clock = self.start
        self.said: list[str] = []
        self.device: dict[str, Any] = dict(PRIVATE)
        self.hold: str | None = None
        self.n = 0
        self.reminders = Reminders(
            self.paths.event_log,
            moment=SimpleNamespace(hold=lambda: self.hold),  # type: ignore[arg-type]
        )
        self.reminders.now = lambda: self.clock
        self.reminders.may_speak = lambda: True
        self.reminders.say = self.said.append
        self.reminders.output = lambda fresh=False: dict(self.device)  # noqa: ARG005

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.n += 1
        action_id = f"A{self.n}"
        request = ActionRequest(
            action_id=action_id,
            tool_name=name,
            target_entity_ref=None,
            caller_principal=CallerPrincipal.JARVIS_LLM,
            risk_level="L1",
            arguments=args,
            authorization_lease=None,
            run_id=None,
            turn_id=None,
        )
        self.lifecycle.register(action_id)
        self.lifecycle.transition(action_id, "authorized")
        bundle = self.registry.dispatch(request, self.conn, self.paths, self.lifecycle)
        out = json.loads(bundle.slots[0].tool_output or "{}")
        return out if isinstance(out, dict) else {"value": out}

    def set(self, minutes: float, text: str = "和 employer 一对一") -> dict[str, Any]:
        at = (self.start + timedelta(minutes=minutes)).astimezone().isoformat(timespec="seconds")
        return self.call("set_reminder", {"at": at, "text": text})

    def fired(self) -> list[dict[str, Any]]:
        return [dict(e.payload) for e in iter_events_of_types(self.conn, ("reminder.fired",))]

    def app(self, job_mail: object | None = None) -> TestClient:
        deps = _notice_deps(job_mail, self.reminders, None)  # type: ignore[arg-type]
        return TestClient(
            create_app(
                InherentDeps(
                    submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(), **deps
                )
            )
        )


@pytest.fixture
def world(tmp_path: Path) -> Iterator[_World]:
    """A fresh world with Chinese fixed text, as the daemon runs."""
    before = lang.language()
    lang.set_language("zh")
    made = _World(tmp_path)
    yield made
    made.conn.close()
    lang.set_language(before)


def test_set_list_fire_once_with_card_and_one_spoken_line(world: _World) -> None:
    """Set -> pending -> not yet due -> due fires once, a card with sound and one line."""
    made = world.set(30)
    assert made["reminder_id"].startswith("reminder-")
    assert made["due_at"] == (world.start + timedelta(minutes=30)).astimezone().isoformat(
        timespec="seconds"
    )
    assert made["due_spoken"]
    listed = world.call("list_reminders", {})
    assert [r["reminder_id"] for r in listed["reminders"]] == [made["reminder_id"]]

    world.clock = world.start + timedelta(minutes=29)
    assert world.reminders.tick() == 0
    assert not world.said

    world.clock = world.start + timedelta(minutes=30, seconds=10)
    assert world.reminders.tick() == 1
    assert world.said == ["提醒：和 employer 一对一"]
    (fired,) = world.fired()
    assert (fired["reminder_id"], fired["delivered"]) == (made["reminder_id"], "speak")
    assert fired["late_ms"] == 10_000

    client = world.app()
    (card,) = client.get("/inherent/notices").json()["notices"]
    assert (card["id"], card["level"], card["title"]) == (
        made["reminder_id"],
        "card_sound",
        "和 employer 一对一",
    )

    world.clock += timedelta(minutes=5)
    assert world.reminders.tick() == 0  # exactly once
    assert len(world.said) == 1
    assert len(world.fired()) == 1
    assert world.call("list_reminders", {})["count"] == 0

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


def test_cancel_prevents_firing_and_unknown_or_fired_ids_are_errors(world: _World) -> None:
    """A cancelled reminder never rings; cancelling one that rang or never existed is an error."""
    kept, dropped = world.set(10, "keep"), world.set(10, "drop")
    assert world.call("cancel_reminder", {"reminder_id": dropped["reminder_id"]})["text"] == "drop"
    world.clock = world.start + timedelta(minutes=11)
    assert world.reminders.tick() == 1
    assert world.said == ["Reminder: keep"]
    assert world.call("cancel_reminder", {"reminder_id": dropped["reminder_id"]})["code"] == (
        "no_such_reminder"
    )
    assert world.call("cancel_reminder", {"reminder_id": kept["reminder_id"]})["code"] == (
        "no_such_reminder"
    )
    assert world.call("cancel_reminder", {"reminder_id": "reminder-zzz"})["code"] == (
        "no_such_reminder"
    )


def test_set_reminder_refuses_a_bad_time(world: _World) -> None:
    """No offset, the past, more than a year ahead, not a time, no text: nothing is scheduled."""
    naive = (world.start + timedelta(hours=1)).astimezone().replace(tzinfo=None).isoformat()
    past = (world.start - timedelta(minutes=5)).isoformat()
    far = (world.start + timedelta(days=400)).isoformat()
    for at, code in (
        (naive, "missing_offset"),
        (past, "time_in_past"),
        (far, "too_far_ahead"),
        ("soon", "invalid_time"),
    ):
        assert world.call("set_reminder", {"at": at, "text": "x"})["code"] == code
    assert world.call("set_reminder", {"at": world.start.isoformat(), "text": " "})["code"] == (
        "empty_text"
    )
    assert world.call("list_reminders", {})["count"] == 0


def test_a_reminder_missed_while_down_fires_late_and_says_so(world: _World) -> None:
    """The daemon was off or the Mac asleep: the first tick after fires, once, and says how late."""
    world.set(10)
    world.set(10, "call mum")
    world.clock = world.start + timedelta(minutes=20)  # nothing ticked meanwhile
    restarted = Reminders(world.paths.event_log)  # a fresh daemon: nothing in memory
    restarted.now = lambda: world.clock
    restarted.may_speak = lambda: True
    restarted.say = world.said.append
    restarted.output = lambda fresh=False: dict(PRIVATE)  # noqa: ARG005
    assert restarted.tick() == 2
    assert world.said == [
        "提醒：和 employer 一对一（晚了 10 分钟）",
        "Reminder: call mum (10 minutes late)",
    ]
    assert restarted.tick() == 0
    assert world.reminders.tick() == 0  # the first daemon sees it already fired


def test_more_than_twelve_hours_late_is_a_card_only(world: _World) -> None:
    """It still fires, as a card that says how late, and says nothing aloud."""
    world.set(10)
    world.clock = world.start + timedelta(hours=13)
    assert world.reminders.tick() == 1
    assert not world.said
    assert world.fired()[0]["delivered"] == "card_sound"
    (card,) = world.app().get("/inherent/notices").json()["notices"]
    assert "晚了 13 小时" in card["line"]


def test_quiet_and_hold_do_not_stop_the_card_and_a_call_stops_only_the_voice(
    world: _World,
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


def test_speech_only_on_private_output_and_when_allowed(world: _World) -> None:
    """Speakers, a mute or a live conversation make the reminder a card with sound."""
    world.device = dict(SPEAKERS)
    world.set(1, "on speakers")
    world.clock = world.start + timedelta(minutes=2)
    world.reminders.tick()
    assert not world.said
    assert world.fired()[0]["delivered"] == "card_sound"

    world.device = dict(PRIVATE)
    world.reminders.may_speak = lambda: False  # muted, or a conversation is live
    world.set(1, "while muted")
    world.clock += timedelta(minutes=2)
    world.reminders.tick()
    assert not world.said
    assert world.fired()[1]["delivered"] == "card_sound"


def test_the_tools_are_the_llms_only_and_set_reminder_is_the_clock() -> None:
    """Only the model calls them, and a memo points to set_reminder for a time."""
    registry = build_default_registry()
    menu = {t.name for t in registry.for_caller(CallerPrincipal.JARVIS_LLM)}
    assert {"set_reminder", "list_reminders", "cancel_reminder", "create_memo"} <= menu
    router = {t.name for t in registry.for_caller(CallerPrincipal.REGEX_ROUTER)}
    assert not router & {"set_reminder", "list_reminders", "cancel_reminder"}
    memo = next(t for t in registry.get_definitions() if t.name == "create_memo")
    assert "set_reminder" in memo.description
