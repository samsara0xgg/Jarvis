"""ADR 0218: a card shows only on the device whose turn asked it.

The real app behind the real guard serves stand-in card reads that carry a ``device``, as the
daemon's own do. Requests come from two paired devices off loopback (a phone and a terminal) and
from the local key on loopback, this host's own UI. Which device a card belongs to is read from a
real event log with :func:`confirmation_device` and :func:`turn_origin`.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

from fastapi.testclient import TestClient

from jarvis.deployment import bootstrap_runtime
from jarvis.state.event_log import (
    MAC_NODE,
    confirmation_device,
    emit_event,
    open_event_log,
    turn_origin,
)
from jarvis.state.plugin_settings import local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

LOCAL_KEY = "local-key-for-this-test"
TOKENS = {"phone-token-for-this-test": "phone", "terminal-token-for-this-test": "macbook"}
REMOTE = ("100.64.0.7", 50123)  # a tailnet address: not on loopback


def _noop(text: str) -> None:
    del text


def _app(confirmation: dict[str, Any], question: dict[str, Any], seen: list[Any]) -> FastAPI:
    def decide(*args: object) -> str:
        seen.append(("decide", *args))
        return "T9"

    def answer(*args: object) -> str:
        seen.append(("answer", *args))
        return "T8"

    app = create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(),
        card_read=lambda: {"card": confirmation}, card_decide=decide,
        question_read=lambda: {"card": question}, question_answer=answer,
        device_name=TOKENS.get,
    ))
    require_local_key(
        app, functools.partial(local_key_matches, LOCAL_KEY),
        device_token_matches=lambda token: token in TOKENS,
    )
    return app


def _client(app: FastAPI, device: str | None) -> TestClient:
    """A paired device off loopback by its token, or (``None``) the local key on loopback."""
    if device is None:
        return TestClient(
            app, base_url="http://127.0.0.1", client=("127.0.0.1", 5),
            headers={"Authorization": f"Bearer {LOCAL_KEY}"},
        )
    token = next(t for t, name in TOKENS.items() if name == device)
    return TestClient(
        app, base_url="http://127.0.0.1", client=REMOTE,
        headers={"Authorization": f"Bearer {token}"},
    )


def _cards(client: TestClient) -> tuple[Any, Any, Any, Any]:
    waiting = client.get("/inherent/waiting").json()
    return (
        client.get("/inherent/confirmation").json()["card"],
        client.get("/inherent/clarification").json()["card"],
        waiting["confirmation"],
        waiting["clarification"],
    )


def test_each_card_is_served_only_to_the_device_whose_turn_asked_it() -> None:
    """The phone's card on the phone, the terminal's on the terminal, neither on the host's UI."""
    confirmation = {"id": "C1", "tool": "gmail_send", "args": {}, "device": "phone"}
    question = {"id": "Q1", "question": "Which stop?", "fields": [], "device": "macbook"}
    app = _app(confirmation, question, [])
    with _client(app, "phone") as phone, _client(app, "macbook") as mac, \
            _client(app, None) as host:
        assert _cards(phone) == (confirmation, None, confirmation, None)
        assert _cards(mac) == (None, question, None, question)
        assert _cards(host) == (None, None, None, None)


def test_a_card_no_turn_asked_is_everyones_and_mac_is_the_hosts_own_ui() -> None:
    """``device`` null: every reader. ``mac`` (a Mac running alone): the local key only."""
    confirmation: dict[str, Any] = {"id": "C1", "tool": "gmail_send", "args": {}, "device": None}
    question = {"id": "Q1", "question": "Which stop?", "fields": [], "device": MAC_NODE}
    app = _app(confirmation, question, [])
    with _client(app, "phone") as phone, _client(app, None) as host:
        assert _cards(phone) == (confirmation, None, confirmation, None)
        assert _cards(host) == (confirmation, question, confirmation, question)


def test_an_answer_starts_its_turn_under_the_device_that_gave_it() -> None:
    """The confirmation's button and the ask card's answers carry the answering device along."""
    confirmation = {"id": "C1", "tool": "gmail_send", "args": {}, "device": "phone"}
    question = {"id": "Q1", "question": "Which stop?", "fields": [], "device": "phone"}
    seen: list[Any] = []
    app = _app(confirmation, question, seen)
    with _client(app, "phone") as phone, _client(app, None) as host:
        pressed = phone.post(
            "/inherent/confirmation",
            json={"confirmation_id": "C1", "decision": "accept", "edits": {}},
        )
        assert pressed.json() == {"status": "accepted", "turn_id": "T9"}
        filled = host.post(
            "/inherent/clarification",
            json={"clarification_id": "Q1", "answers": {"stop": "UVic"}},
        )
        assert filled.json() == {"status": "accepted", "turn_id": "T8"}
    assert seen == [
        ("decide", "C1", "accept", {}, "phone"),
        ("answer", "Q1", {"stop": "UVic"}, MAC_NODE),
    ]


def test_a_cards_device_is_the_device_that_opened_the_turn_that_asked_it(tmp_path: Path) -> None:
    """Read from the log as the daemon's card reads and the push sender read it."""
    log = open_event_log(bootstrap_runtime(tmp_path).event_log)
    try:
        emit_event(
            log, type="surface.user_intent", ingestion_node="phone",
            payload={"transcript": "send it", "turn_id": "T1", "channel": "cli_stdin"},
            correlation={"turn_id": "T1"},
        )
        asked = (("C1", {"turn_id": "T1"}), ("C2", None), ("C3", {"turn_id": "T7"}))
        for cid, correlation in asked:
            emit_event(
                log, type="confirmation.requested", correlation=correlation,
                payload={
                    "confirmation_id": cid, "action_snapshot": {"tool_name": "gmail_send"},
                    "template_line": "Send?", "expires_at_ms": 1,
                },
            )
        # a phone turn; a card raised with no turn (ADR 0148); a turn with no opening row
        assert [confirmation_device(log, cid) for cid in ("C1", "C2", "C3")] == [
            "phone", None, None,
        ]
        assert turn_origin(log, "T1") == ("cli_stdin", "phone")
    finally:
        log.close()
