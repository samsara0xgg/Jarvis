"""What waits on Allen: the cards of ``GET /inherent/waiting`` and the answers posted back.

The confirmation card, the ask card (with its ``trip`` form, a bus card) and the sign-in a plugin
asks for are read by the iPhone's Waiting screen and by the desktop's companion. Their bodies come
out of the daemon's own reads, which only exist inside a booted ``serve_inherent``, so the
examples here are what that daemon serves a paired phone after the real tools put the cards up.
The answers go the other way: what each button posts, and what the brain takes of it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution import tools, transit_tool
from jarvis.execution.tools import (
    build_default_registry,
    register_live_action,
    release_turn_actions,
)
from jarvis.execution.transit_tool import TransitOffers, show_card
from jarvis.runtime import plugin_connections
from jarvis.state.event_log import emit_event, open_event_log
from tests.support.contracts import (
    WORLD_NOW_MS,
    WORLD_ZONE,
    Contracts,
    Inputs,
    brain_at,
    fixed_uuids,
)
from tests.support.daemon_app import Daemon, run_in_daemon

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

TURN, ACTION, CARD, QUESTION = "T1", "A1", "C1", "Q1"
LETTER = {"to": "pat@example.com", "subject": "Thursday", "body": "Can we move it to ten?"}
FIELDS: list[dict[str, Any]] = [
    {"label": "送餐地址", "value": "1 Test St"},
    {"label": "几位", "choices": ["1", "2", "3", "4"]},
]


def _fixed_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ids the tools mint, the same every run, and no running action to point a row at."""
    fixed_uuids(monkeypatch, tools, transit_tool, plugin_connections)
    monkeypatch.setattr(tools, "_get_running_event_uid", lambda *_: None)
    monkeypatch.setattr(transit_tool, "_get_running_event_uid", lambda *_: None)


def _phone_turn(log: Path) -> None:
    """A turn the phone opened: the cards it asks are the phone's own."""
    with closing(open_event_log(log)) as conn:
        emit_event(
            conn, type="utterance.received", ingestion_node="phone",
            payload={"transcript": "send Pat a note", "turn_id": TURN},
            correlation={"turn_id": TURN},
        )


def _confirmation(log: Path, card: str = CARD) -> None:
    """The card ``decide`` writes when a tool needs his word: the tool and its arguments."""
    with closing(open_event_log(log)) as conn:
        emit_event(
            conn, type="confirmation.requested", correlation={"turn_id": TURN},
            payload={
                "confirmation_id": card,
                "action_snapshot": {
                    "tool_name": "mcp__gmail__gmail_send", "caller": "jarvis_llm",
                    "canonical_target": "gmail", "target_entity_ref": None, "risk_level": "L3",
                    "args_meta": LETTER,
                },
                "template_line": "发这封邮件", "expires_at_ms": WORLD_NOW_MS + 600_000,
            },
        )


def _ask(log: Path, args: dict[str, Any]) -> None:
    """The ask card as the model puts it up: the real ``ask_user`` over the daemon's log."""
    (definition,) = (d for d in build_default_registry().get_definitions() if d.name == "ask_user")
    with closing(open_event_log(log)) as conn:
        ctx = SimpleNamespace(conn=conn, action_id=ACTION)
        definition.handler(args, ctx)  # type: ignore[arg-type]


def _bus_card(log: Path) -> None:
    """The bus card as ``bus`` puts it up: the offer the real ``TransitOffers`` keeps."""
    options = [
        {
            "leave_at": leave, "arrive_at": arrive,
            "legs": [{"route": route, "board_stop": stop, "departs": departs}],
        }
        for leave, arrive, route, stop, departs in (
            ("09:12", "09:41", "14", "Hillside Mall", "09:15"),
            ("09:27", "09:56", "14", "Hillside Mall", "09:30"),
            ("09:42", "10:08", "26", "Douglas at Hillside", "09:45"),
        )
    ]
    made = datetime.fromtimestamp(WORLD_NOW_MS / 1000, ZoneInfo(WORLD_ZONE))
    trip = TransitOffers().put(options, "UVic", made)
    with closing(open_event_log(log)) as conn:
        show_card(SimpleNamespace(conn=conn, action_id=ACTION), trip)  # type: ignore[arg-type]


async def _cards(daemon: Daemon) -> dict[str, Any]:
    """What a paired phone reads from the three routes, at each stage of the day."""
    async with daemon.device("phone") as phone:

        async def get(path: str) -> dict[str, Any]:
            answer = await phone.get(path)
            assert answer.status_code == 200, answer.text
            body: dict[str, Any] = answer.json()
            return body

        nothing = {
            "confirmation": await get("/inherent/confirmation"),
            "clarification": await get("/inherent/clarification"),
            "all": await get("/inherent/waiting"),
        }
        _phone_turn(daemon.log)
        _confirmation(daemon.log)
        letter = await get("/inherent/confirmation")
        _ask(daemon.log, {"question": "送到哪里", "fields": FIELDS})
        plugins = daemon.runtime.plugin_connections
        plugins.action("open", {"plugin_id": sorted(plugins.packages)[0]})
        everything = await get("/inherent/waiting")
        ask = await get("/inherent/clarification")
        _bus_card(daemon.log)
        trip = await get("/inherent/clarification")
    return {
        "nothing": nothing, "letter": letter, "everything": everything, "ask": ask, "trip": trip,
    }


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The cards at each stage, read from a booted daemon at the world's clock."""
    _fixed_ids(monkeypatch)
    register_live_action(turn_id=TURN, action_id=ACTION)
    try:
        with brain_at():
            return run_in_daemon(tmp_path, monkeypatch, _cards)
    finally:
        release_turn_actions(TURN)


def test_the_confirmation_card_the_phone_reads(
    served: dict[str, Any], contracts: Contracts,
) -> None:
    """A tool at the threshold asks: the card names the tool, what it does and its arguments."""
    contracts.output(
        "waiting/confirmation",
        examples={"card": served["letter"], "none": served["nothing"]["confirmation"]},
        now_ms=WORLD_NOW_MS, route="GET /inherent/confirmation",
        description=(
            "`card` is the card waiting for his button, or null. `id` is what the answer posts "
            "back; `tool` is the tool's name and `action` what it would do, in words; `source` "
            "the app of an `mcp__app__tool` ('' for a built-in tool); `letter` is true when it "
            "would send a letter, whose `to`, `subject` and `body` are in `args`; `args` are "
            "the arguments the model proposed (a client draws the short ones and never the "
            "large ones it is not shown). `device` is the paired device whose turn asked, or "
            "null when none did; a card is served only to that device and the host's own UI "
            "(ADR 0218), so a phone never reads another device's."
        ),
    )
    assert served["letter"]["card"]["id"] == CARD
    assert served["nothing"]["confirmation"] == {"card": None}


def test_the_ask_card_the_phone_reads_and_its_bus_form(
    served: dict[str, Any], contracts: Contracts,
) -> None:
    """A question with fields to fill in, or a bus card with options to read and no fields."""
    contracts.output(
        "waiting/clarification",
        examples={
            "ask": served["ask"], "trip": served["trip"],
            "none": served["nothing"]["clarification"],
        },
        now_ms=WORLD_NOW_MS, route="GET /inherent/clarification",
        description=(
            "`card` is the ask card to fill in, or null. `fields` are one to four details, each "
            "a `label` the answer is keyed by, `choices` when the answer is one of a few (typed "
            "otherwise) and `value` when the model pre-filled it; the card never says whether a "
            "field is remembered. A bus card (ADR 0205) has no fields and a `trip`: `to`, "
            "`made_at` and `options`, one row per bus still catchable, each with `leave_at`, "
            "`route`, `board_stop`, `departs`, `arrive_at` and `to`. `device` is as for the "
            "confirmation card."
        ),
    )
    assert [f["label"] for f in served["ask"]["card"]["fields"]] == [f["label"] for f in FIELDS]
    assert served["trip"]["card"]["fields"] == []
    assert len(served["trip"]["card"]["trip"]["options"]) == 3
    assert served["nothing"]["clarification"] == {"card": None}


def test_everything_that_waits_in_one_read(served: dict[str, Any], contracts: Contracts) -> None:
    """The phone asks once: the two cards, the notices, a plugin's sign-in, held Claude prompts."""
    contracts.output(
        "waiting/all",
        examples={"nothing": served["nothing"]["all"], "everything": served["everything"]},
        now_ms=WORLD_NOW_MS, route="GET /inherent/waiting",
        description=(
            "Each source in one body: `confirmation` and `clarification` are the `card` of the "
            "two routes above, `notices` is the body of `/inherent/notices` (reminders and job "
            "mail), `plugin_request` is the plugin whose connection or sign-in is asked (its "
            "`id` is what a plugin action posts back) and `claude_requests` the permission "
            "prompts Claude Code holds. Nothing waiting is null or empty, and so is a source "
            "this brain does not have; one source failing leaves the others readable. Reading "
            "this keeps Claude Code's prompts held for the phone."
        ),
    )
    everything = served["everything"]
    assert everything["confirmation"] == served["letter"]["card"]
    assert everything["clarification"] == served["ask"]["card"]
    assert everything["plugin_request"]["id"]
    assert served["nothing"]["all"]["confirmation"] is None
    assert served["nothing"]["all"]["claude_requests"] == []


# --- the answers --------------------------------------------------------------------------------


def _ask_up(log: Path) -> None:
    """The ask card of ``FIELDS`` waiting, as ``ask_user`` writes it (a fresh one each time)."""
    with closing(open_event_log(log)) as conn:
        emit_event(
            conn, type="clarification.requested", correlation={"turn_id": TURN},
            payload={
                "clarification_id": QUESTION, "question": "送到哪里", "turn_id": TURN,
                "fields": [{**field, "remember": False} for field in FIELDS],
            },
        )


def _post_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, bodies: list[Any],
    card_up: Callable[[Path], None],
) -> tuple[list[tuple[int, Any]], list[tuple[str, str, dict[str, Any]]]]:
    """Post each body as the phone, against a card put up again before each.

    Returns what the daemon answered, and the rows it wrote for the answers: type, the device
    that wrote it and the payload.
    """

    async def scenario(daemon: Daemon) -> tuple[list[tuple[int, Any]], Path]:
        _phone_turn(daemon.log)
        answered: list[tuple[int, Any]] = []
        async with daemon.device("phone") as phone:
            for body in bodies:
                card_up(daemon.log)
                reply = await phone.post(path, json=body)
                answered.append((reply.status_code, reply.json()))
        return answered, daemon.log

    _fixed_ids(monkeypatch)
    with brain_at():
        answered, log = run_in_daemon(tmp_path, monkeypatch, scenario)
    with closing(sqlite3.connect(log)) as conn:
        written = [
            (str(kind), str(node), json.loads(payload))
            for kind, node, payload in conn.execute(
                "SELECT type, ingestion_node, payload_json FROM events "
                "WHERE type IN ('surface.user_intent', 'surface.clarified', 'surface.dismissed') "
                "ORDER BY id",
            )
        ]
    return answered, written


def _statuses(inputs: Inputs, answered: list[tuple[int, Any]]) -> None:
    """Every valid body was taken (200) and every invalid one refused with its status."""
    taken = answered[: len(inputs.valid)]
    refused = answered[len(inputs.valid) :]
    assert {case.name: status for case, (status, _) in zip(inputs.valid, taken, strict=True)} == {
        case.name: 200 for case in inputs.valid
    }
    assert {case.name: str(status) for case, (status, _) in zip(
        inputs.invalid, refused, strict=True,
    )} == {case.name: case.refusal for case in inputs.invalid}


def test_the_brain_takes_each_button_of_a_confirmation_card_and_refuses_a_malformed_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contracts: Contracts,
) -> None:
    """Accept or reject, with the card's text edits; the answer starts a turn under the phone."""
    inputs = contracts.inputs(
        "waiting/confirmation_answer", route="POST /inherent/confirmation",
    )
    bodies = [case.value for case in [*inputs.valid, *inputs.invalid]]
    answered, written = _post_each(
        tmp_path, monkeypatch, "/inherent/confirmation", bodies, _confirmation,
    )
    _statuses(inputs, answered)
    assert all(set(body) == {"status", "turn_id"} for status, body in answered if status == 200)
    assert [(kind, node, row["confirmation_decision"]) for kind, node, row in written] == [
        ("surface.user_intent", "phone", {
            "confirmation_id": case.value["confirmation_id"],
            "decision": case.value["decision"], "edits": case.value.get("edits", {}),
        })
        for case in inputs.valid
    ]


def test_the_brain_takes_the_answers_or_the_close_button_of_an_ask_card_and_refuses_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contracts: Contracts,
) -> None:
    """Answers keyed by field label, or ``dismiss``; blank answers are not an answer."""
    inputs = contracts.inputs(
        "waiting/clarification_answer", route="POST /inherent/clarification",
    )
    bodies = [case.value for case in [*inputs.valid, *inputs.invalid]]
    answered, written = _post_each(
        tmp_path, monkeypatch, "/inherent/clarification", bodies, _ask_up,
    )
    _statuses(inputs, answered)
    states = {
        case.name: body["status"]
        for case, (status, body) in zip(inputs.valid, answered, strict=False)
        if status == 200
    }
    assert states == {
        case.name: "dismissed" if case.value.get("dismiss") else "accepted"
        for case in inputs.valid
    }
    closed = {"surface.clarified", "surface.dismissed"}  # the answer's turn is written beside
    assert [kind for kind, _node, _row in written if kind in closed] == [
        "surface.dismissed" if case.value.get("dismiss") else "surface.clarified"
        for case in inputs.valid
    ]
