"""`GET /inherent/waiting`: everything waiting on Allen, read in one call by a paired phone.

The real app, behind the real guard, serves a confirmation card and an ask card (stand-in reads, the
daemon's own live in `serve_inherent`), a reminder the real `Reminders` fired, a plugin sign-in
request, and a Claude Code prompt that the real `scripts/claude_hook.py` payload holds in the real
`ClaudeHooks`. Requests come from a peer that is not on loopback with a paired device's token.
"""

from __future__ import annotations

import functools
import json
import os
import threading
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from jarvis.runtime.inherent_loop import _notice_deps
from jarvis.state.plugin_settings import local_key_matches
from jarvis.surface import claude_hooks, claude_sessions
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from tests.integration.test_claude_hooks import SUGGESTION
from tests.integration.test_reminders import _World

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from fastapi import FastAPI

LOCAL_KEY = "local-key-for-this-test"
DEVICE_TOKEN = "paired-device-token-for-this-test"  # noqa: S105 — a stand-in, not a secret.
PHONE = ("100.64.0.7", 50123)  # a tailnet address: not on loopback
EMPTY: dict[str, Any] = {
    "confirmation": None,
    "clarification": None,
    "notices": {"notices": [], "audio_private": False, "hold": None, "departure": None},
    "plugin_request": None,
    "claude_requests": [],
}
CONFIRMATION = {
    "id": "C1", "tool": "mcp__gmail__gmail_send", "action": "send the letter", "source": "gmail",
    "letter": True, "args": {"to": "a@example.com"},
}
CLARIFICATION = {
    "id": "Q1", "question": "Where to?",
    "fields": [{"label": "address", "choices": [], "value": ""}],
}
PLUGIN_REQUEST = {"plugin_id": "demo", "state": "authorizing", "auth_url": "https://example.com/go"}


def _noop(text: str) -> None:
    del text


def _guard(app: FastAPI) -> None:
    require_local_key(
        app,
        functools.partial(local_key_matches, LOCAL_KEY),
        device_token_matches=lambda token: token == DEVICE_TOKEN,
    )


def _phone(app: FastAPI) -> TestClient:
    return TestClient(
        app, base_url="http://127.0.0.1", client=PHONE,
        headers={"Authorization": f"Bearer {DEVICE_TOKEN}"},
    )


class _Claude:
    """A fake `claude` binary listing one waiting session, and a hook post that blocks."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(f"#!/bin/sh\ncat '{tmp_path / 'agents.json'}'\n")
        fake.chmod(0o755)
        transcript = tmp_path / ".claude" / "projects" / "-x" / "s1.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"type": "last-prompt", "lastPrompt": "build it"}))
        agent = {
            "sessionId": "s1", "kind": "interactive", "status": "waiting", "name": "build",
            "cwd": "/x/jarvis", "startedAt": 1,
        }
        (tmp_path / "agents.json").write_text(json.dumps([agent]))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(claude_sessions, "REFRESH_S", 0.0)
        self.decisions: list[dict[str, Any]] = []

    def prompt(self, client: TestClient, tool: str = "Bash") -> threading.Thread:
        """Post a PermissionRequest hook as Claude Code does; its answer lands in `decisions`."""
        payload = {
            "hook_event_name": "PermissionRequest", "session_id": "s1", "cwd": "/x/jarvis",
            "tool_name": tool, "tool_input": {"command": "npm run build"},
            "permission_suggestions": [SUGGESTION],
        }

        def post() -> None:
            self.decisions.append(client.post("/inherent/claude-hook", json=payload).json())

        thread = threading.Thread(target=post, daemon=True)
        thread.start()
        return thread


def _until[T](read: Callable[[], T], found: Callable[[T], bool]) -> T:
    """Poll `read()` until `found(result)`; the last result either way."""
    deadline = time.monotonic() + 5
    while True:
        result = read()
        if found(result) or time.monotonic() > deadline:
            return result
        time.sleep(0.05)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_World, _Claude]]:
    """A runtime root with a fired reminder, and a Claude Code home."""
    made = _World(tmp_path / "jarvis")
    made.set(1, "stand up")
    made.clock = made.start + timedelta(minutes=2)
    assert made.reminders.tick() == 1
    yield made, _Claude(tmp_path, monkeypatch)
    made.conn.close()


def test_one_read_holds_every_card_and_the_answers_stay_on_their_own_routes(
    world: tuple[_World, _Claude],
) -> None:
    """A confirmation, an ask, a fired reminder, a plugin sign-in and a held Claude prompt."""
    reminders, claude = world
    app = create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(), claude_sessions_read=True,
        card_read=lambda: {"card": CONFIRMATION}, card_decide=lambda *_a: "T1",
        question_read=lambda: {"card": CLARIFICATION}, question_answer=lambda *_a: "T2",
        plugin_read=lambda: {"plugins": [], "request": PLUGIN_REQUEST},
        plugin_action=lambda *_a: {},
        plugin_authorize=lambda header: header == f"Bearer {DEVICE_TOKEN}",
        **_notice_deps(None, reminders.reminders, None),
    ))
    _guard(app)
    with _phone(app) as phone:
        before = phone.get("/inherent/waiting")
        assert before.status_code == 200, before.text
        assert before.json()["claude_requests"] == []  # nothing held yet

        hook = claude.prompt(phone)
        body = _until(
            lambda: phone.get("/inherent/waiting").json(), lambda b: b["claude_requests"],
        )

        # The cards are the ones the existing routes serve, byte for byte.
        assert body["confirmation"] == phone.get("/inherent/confirmation").json()["card"]
        assert body["clarification"] == phone.get("/inherent/clarification").json()["card"]
        assert body["notices"] == phone.get("/inherent/notices").json()
        assert body["plugin_request"] == phone.get("/inherent/plugins").json()["request"]
        assert body["confirmation"] == CONFIRMATION
        assert body["clarification"] == CLARIFICATION
        assert body["plugin_request"] == PLUGIN_REQUEST
        (card,) = body["notices"]["notices"]
        assert (card["title"], card["level"]) == ("stand up", "card")
        assert body["notices"]["departure"] is None

        (request,) = body["claude_requests"]
        row = next(
            r for r in phone.get("/inherent/claude-sessions").json()["sessions"]
            if r["session_id"] == "s1"
        )
        assert {k: request[k] for k in ("id", "tool", "input", "cwd", "always")} == row["request"]
        assert request["input"] == {"command": "npm run build"}
        assert request["always"] == "Don't ask again for Bash(npm run build:*)"
        assert (request["session_id"], request["title"], request["project"]) == (
            "s1", "build", row["project"],
        )

        # Answering is the existing route; the held hook gets its decision, and it leaves the list.
        answered = phone.post(
            f"/inherent/claude-requests/{request['id']}", json={"decision": "allow"},
        )
        assert answered.json() == {"ok": True}
        hook.join(timeout=5)
        assert claude.decisions[0]["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
        assert phone.get("/inherent/waiting").json()["claude_requests"] == []


def test_the_guard_is_the_one_every_other_route_has() -> None:
    """A peer off loopback needs a paired device's token; the local key is no use there."""
    app = create_app(InherentDeps(submit_callable=_noop, broadcaster=InherentBroadcaster()))
    _guard(app)
    with TestClient(app, base_url="http://127.0.0.1", client=PHONE) as stranger:
        assert stranger.get("/inherent/waiting").status_code == 401
        key = {"Authorization": f"Bearer {LOCAL_KEY}"}
        assert stranger.get("/inherent/waiting", headers=key).status_code == 401
    with _phone(app) as phone:
        assert phone.get("/inherent/waiting").status_code == 200
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5)) as owner:
        key = {"Authorization": f"Bearer {LOCAL_KEY}"}
        assert owner.get("/inherent/waiting", headers=key).status_code == 200


def test_a_daemon_with_none_of_the_sources_has_nothing_waiting() -> None:
    """No memory, no cards, no plugins, no Claude sessions: every field empty, never an error."""
    app = create_app(InherentDeps(submit_callable=_noop, broadcaster=InherentBroadcaster()))
    _guard(app)
    with _phone(app) as phone:
        response = phone.get("/inherent/waiting")
    assert (response.status_code, response.json()) == (200, EMPTY)


def test_a_source_that_cannot_be_read_is_empty_and_the_rest_still_arrive() -> None:
    """Job mail or Microsoft failing does not hide the confirmation card from the phone."""

    async def broken() -> dict[str, Any]:
        msg = "gmail is down"
        raise RuntimeError(msg)

    app = create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(), notices_read=broken,
        card_read=lambda: {"card": CONFIRMATION}, card_decide=lambda *_a: "T1",
    ))
    _guard(app)
    with _phone(app) as phone:
        assert phone.get("/inherent/notices").status_code == 502
        body = phone.get("/inherent/waiting").json()
    assert body == {**EMPTY, "confirmation": CONFIRMATION}


def test_reading_waiting_is_listening_so_a_phone_with_the_app_open_keeps_prompts_held(
    world: tuple[_World, _Claude], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held while `/waiting` keeps being read; let go back to Claude Code's dialog once it stops."""
    _, claude = world
    monkeypatch.setattr(claude_hooks, "LISTENER_S", 0.6)
    app = create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(), claude_sessions_read=True,
    ))
    _guard(app)
    with _phone(app) as phone:
        # Nobody has read anything: the prompt is left to Claude Code at once.
        claude.prompt(phone).join(timeout=5)
        assert claude.decisions == [{}]

        phone.get("/inherent/waiting")
        hook = claude.prompt(phone)
        for _ in range(8):  # the app is open and polling: well past one listener window
            time.sleep(0.2)
            assert phone.get("/inherent/waiting").json()["claude_requests"]
            assert hook.is_alive()

        # The app closes: nobody reads, and the hook is given back within a poll or two.
        hook.join(timeout=5)
        assert not hook.is_alive()
        assert claude.decisions == [{}, {}]
        assert phone.get("/inherent/claude-sessions").json()["sessions"][0]["request"] is None
