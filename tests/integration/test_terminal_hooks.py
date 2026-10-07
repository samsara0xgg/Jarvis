"""ADR 0183: Claude Code's hooks reach the brain through the terminal that serves the UI.

``scripts/claude_hook.py`` posts to 127.0.0.1:8006 with the local key, and on a Mac that runs a
terminal with ``--serve-ui`` that is the terminal. It forwards the post to the brain under its
device token, holds the request while the brain holds the prompt, and carries the brain's
decision back. The companion's stand-in reads the board and answers the card through the same
terminal. Real hook script, real terminal wiring (``_run``), a real brain app; the fake Claude
Code is the device's own home and ``claude`` binary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Self

import httpx
import pytest

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import build_default_registry, make_screen_capture
from jarvis.runtime.terminal import _declared, _run, _Ui, bind_ui, make_executor
from jarvis.state.device_tokens import pair_device
from jarvis.state.plugin_settings import local_key
from jarvis.surface.claude_sessions import ClaudeSessions
from tests.integration.test_claude_hooks import HOOK, SUGGESTION
from tests.integration.test_terminal_claude import BUSY, WAITING, _device_home
from tests.integration.test_terminal_observers import _free_port, _wait_for
from tests.integration.test_terminal_reads import _Brain

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.types import ASGIApp, Receive, Scope, Send


class _Tap:
    """What reached the brain's hook routes, and under which credential."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.seen: list[tuple[str, str | None]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(
            ("/inherent/claude-hook", "/inherent/claude-requests"),
        ):
            headers = dict(scope["headers"])
            auth = headers.get(b"authorization")
            self.seen.append((scope["path"], None if auth is None else auth.decode()))
        await self.app(scope, receive, send)


class _Mac:
    """The real terminal (link, executor and UI on loopback) over the device's Claude Code."""

    def __init__(self, url: str, token: str, root: Path, port: int) -> None:
        registry = build_default_registry(obsidian_vault_root=None)
        registry.register(make_screen_capture(800))
        self._args = (
            url, token, _declared(registry),
            make_executor(registry, claude=ClaudeSessions()),
            _Ui(bind_ui(port), {}, root),
        )
        self.loop = asyncio.new_event_loop()
        self.task: asyncio.Task[None] | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def run() -> None:
            url, token, tools, execute, ui = self._args
            self.task = asyncio.create_task(_run(
                url, token, tools=tools, execute=execute, watched=None, ui=ui,
            ))
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

        self.loop.run_until_complete(run())

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.loop.call_soon_threadsafe(lambda: self.task and self.task.cancel())
        self.thread.join(timeout=15)
        assert not self.thread.is_alive()


class _Rig:
    """A brain, a terminal serving the UI in front of it, and a companion's HTTP client."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _device_home(tmp_path / "mac", monkeypatch)
        self.root = tmp_path / "terminal-root"
        self.key = local_key(self.root)
        self.token = pair_device(tmp_path, "macbook")
        self.tap: _Tap | None = None

        def tap(app: ASGIApp) -> ASGIApp:
            self.tap = _Tap(app)
            return self.tap

        log = bootstrap_runtime(tmp_path / "brain-root").event_log
        self.brain = _Brain(
            tmp_path, log, deps=lambda _hub: {"claude_sessions_read": True}, tap=tap,
        )
        self.port = _free_port()
        self.mac = _Mac(self.brain.url, self.token, self.root, self.port)
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}", timeout=10,
            headers={"Authorization": f"Bearer {self.key}"},
        )

    def __enter__(self) -> Self:
        self.brain.__enter__()
        self.mac.__enter__()
        _wait_for(lambda: bool(self.brain.hub.connected()), "the terminal never connected")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.http.close()
        self.mac.__exit__()
        self.brain.__exit__()

    def hook(self, event: str, **fields: object) -> subprocess.Popen[str]:
        """The repo's hook script, as Claude Code starts it: the payload on stdin."""
        payload = {"hook_event_name": event, "session_id": WAITING, "cwd": "/x/jarvis", **fields}
        proc = subprocess.Popen(  # noqa: S603 — the repo's own hook script
            [sys.executable, str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env={**os.environ, "JARVIS_INHERENT_BRIDGE_PORT": str(self.port),
                 "JARVIS_RUNTIME_ROOT": str(self.root)},
        )
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
        return proc

    def row(self, session: str = WAITING) -> dict[str, Any]:
        rows = self.http.get("/inherent/claude-sessions").json()["sessions"]
        return next(r for r in rows if r["session_id"] == session)

    def held(self) -> dict[str, Any]:
        """The prompt as the companion sees it on the session's row."""
        for _ in range(100):
            if request := self.row()["request"]:
                return dict(request)
            time.sleep(0.05)
        msg = "the prompt never reached the board"
        raise AssertionError(msg)


def _decision(proc: subprocess.Popen[str]) -> dict[str, Any]:
    assert proc.stdout is not None
    out = proc.stdout.read()
    proc.wait(timeout=10)
    return dict(json.loads(out)["hookSpecificOutput"]["decision"]) if out else {}


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    """A running rig; the hook's posts and the card's answers go through the terminal."""
    with _Rig(tmp_path, monkeypatch) as running:
        yield running


def test_a_held_prompt_is_answered_from_the_card_through_the_terminal_both_ways(
    rig: _Rig,
) -> None:
    """The decision comes back to the hook process, for always and for deny; credential: device."""
    rig.row()  # a companion reading the board is what lets a prompt be held
    allowed = rig.hook(
        "PermissionRequest", tool_name="Bash", tool_input={"command": "npm run build"},
        permission_suggestions=[SUGGESTION],
    )
    request = rig.held()
    assert (request["tool"], request["input"]) == ("Bash", {"command": "npm run build"})
    assert request["always"] == "Don't ask again for Bash(npm run build:*)"
    assert allowed.poll() is None  # held: Claude Code is waiting on the card
    answered = rig.http.post(
        f"/inherent/claude-requests/{request['id']}", json={"decision": "always"},
    )
    assert answered.json() == {"ok": True}
    assert _decision(allowed) == {"behavior": "allow", "updatedPermissions": [SUGGESTION]}

    denied = rig.hook("PermissionRequest", tool_name="Bash", tool_input={"command": "rm -rf x"})
    again = rig.held()
    assert again["id"] != request["id"]
    rig.http.post(
        f"/inherent/claude-requests/{again['id']}", json={"decision": "deny", "message": "no"},
    )
    assert _decision(denied) == {"behavior": "deny", "message": "no"}

    # An answer to a prompt that is gone is the brain's 404, passed back as it is.
    assert rig.http.post(
        f"/inherent/claude-requests/{again['id']}", json={"decision": "allow"},
    ).status_code == 404

    # Every hook route reached the brain as the device (its token), never under the local key.
    assert rig.tap is not None
    assert {path for path, _ in rig.tap.seen} == {
        "/inherent/claude-hook", "/inherent/claude-requests/" + request["id"],
        "/inherent/claude-requests/" + again["id"],
    }
    assert {auth for _, auth in rig.tap.seen} == {f"Bearer {rig.token}"}


def test_a_hook_that_only_reports_marks_the_session_on_the_brain(rig: _Rig) -> None:
    """PreCompact needs no answer: the terminal passes it on and the brain's board shows it."""
    rig.row()
    hook = rig.hook("PreCompact", session_id=BUSY)
    assert hook.wait(timeout=10) == 0
    for _ in range(100):
        if rig.row(BUSY)["compacting"]:
            break
        time.sleep(0.05)
    assert rig.row(BUSY)["compacting"] is True


def test_a_hook_that_goes_away_lets_the_brain_drop_the_prompt(rig: _Rig) -> None:
    """Claude Code answered in its own dialog and the hook process ended: the card goes too.

    On a daemon the hook's socket closing is how the held prompt learns; behind the terminal that
    socket is the terminal's, so the terminal has to end its own request to the brain.
    """
    rig.row()
    hook = rig.hook("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"})
    rig.held()
    hook.kill()
    hook.wait(timeout=10)
    for _ in range(80):  # the brain looks about once a second
        if not rig.row()["request"]:
            return
        time.sleep(0.1)
    msg = "the brain still holds a prompt whose hook is gone"
    raise AssertionError(msg)


def test_with_the_brain_away_the_hook_leaves_the_prompt_to_claude_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The terminal answers 503 for the brain; the hook prints nothing and exits at once."""
    _device_home(tmp_path / "mac", monkeypatch)
    root = tmp_path / "terminal-root"
    local_key(root)  # the hook reads the terminal's own key from its root
    port = _free_port()
    with _Mac("http://127.0.0.1:9", "unused", root, port):
        payload = {"hook_event_name": "PermissionRequest", "session_id": WAITING,
                   "cwd": "/x/jarvis", "tool_name": "Bash", "tool_input": {}}
        done = subprocess.run(  # noqa: S603 — the repo's own hook script
            [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
            text=True, timeout=20, check=False,
            env={**os.environ, "JARVIS_INHERENT_BRIDGE_PORT": str(port),
                 "JARVIS_RUNTIME_ROOT": str(root)},
        )
    assert (done.returncode, done.stdout) == (0, "")
