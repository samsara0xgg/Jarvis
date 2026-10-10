"""ADR 0170 step 4a: a brain hands its device-bound tool calls to a connected terminal.

Acceptance checks, each against the real code: the brain's menu keeps every device tool as a
proxy with the same definition, and one machine's menu is untouched; a proxy returns what a
fake in-process terminal answers, says plainly that the device is not connected, times out,
and fails when the terminal drops mid-call; `/terminal/ws` takes only a paired device's
token; `screen_look` is split so the terminal only captures and the brain describes (the
brain half against a fake vision client, no model); the terminal's runner executes the real
handlers; and a real terminal client talks to a real server, reconnects, and stops on a
refused token.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import functools
import json
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from jarvis.cli import main
from jarvis.deployment.night_power import MacPower
from jarvis.execution import tools as tools_module
from jarvis.execution.tools import (
    ActionLifecycle,
    ToolRegistry,
    build_default_registry,
    make_screen_capture,
)
from jarvis.runtime import _make_entity_resolver, _sound_output, audio_output
from jarvis.runtime.night_run import NightRun, NightSettings
from jarvis.runtime.reminders import Reminders
from jarvis.runtime.terminal import _declared, make_executor
from jarvis.shared import ActionRequest, CallerPrincipal, RawResult
from jarvis.shared.device_link import DeviceCallError
from jarvis.state.device_tokens import (
    device_name_for_token,
    device_token_matches,
    pair_device,
    unpair_device,
)
from jarvis.state.event_log import open_event_log
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface import terminal_link
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.terminal_link import (
    TerminalHub,
    TerminalRefusedError,
    run_terminal_client,
)
from jarvis.surface.terminal_voice import BrainVoice
from tests.integration.test_terminal_voice import _NoRows

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from starlette.types import ASGIApp, Receive, Scope, Send

    from jarvis.runtime.job_mail import JobMail

REMOTE = "100.87.250.92"
TERMINAL_WS = "ws://127.0.0.1:8006/terminal/ws"
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 20
# The tools a brain keeps on its menu that act on a device (`write_file` has no caller).
DEVICE = {"open_path", "search_notes", "read_file", "read_clipboard", "open_url", "screen_look",
          "write_file"}


# `screen_look` is not a proxy: it takes the screenshot from the terminal and describes it here.
PROXIED = DEVICE - {"screen_look"} | {"start_night_run", "end_night_run"}


# --- helpers ---------------------------------------------------------------


@dataclass(frozen=True)
class _Paths:
    event_log: Path
    artifacts_root: Path


def _dispatch(
    registry: ToolRegistry,
    root: Path,
    tool: str,
    arguments: Mapping[str, Any],
    entity: str | None = None,
) -> RawResult:
    """Run one tool the way the decision layer does; always on a worker thread."""
    conn = open_event_log(root / "events.db")
    try:
        lifecycle = ActionLifecycle()
        action_id = uuid.uuid4().hex
        lifecycle.register(action_id)
        lifecycle.transition(action_id, "authorized")
        request = ActionRequest(
            action_id=action_id, tool_name=tool, target_entity_ref=entity,
            caller_principal=CallerPrincipal.JARVIS_LLM, risk_level="L0",
            arguments=dict(arguments), authorization_lease=None, run_id=None, turn_id=None,
        )
        paths = _Paths(root / "events.db", root / "artifacts")
        return registry.dispatch(request, conn, paths, lifecycle).slots[0]
    finally:
        conn.close()


class _FakeVision:
    """The brain's vision client, replaced: records what it was asked, answers a sentence."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str | None, bytes]] = []

    def describe_image(self, image_path: Path, *, question: str | None) -> str:
        self.calls.append((image_path, question, image_path.read_bytes()))
        return "a terminal window and a browser"


class _FakeTerminal:
    """A terminal in this process: answers each call with `behave(frame)`, or never."""

    def __init__(
        self,
        hub: TerminalHub,
        tools: set[str],
        behave: Callable[[dict[str, Any], _FakeTerminal], dict[str, Any] | None],
        name: str = "macbook",
    ) -> None:
        self.hub = hub
        self.behave = behave
        self.calls: list[dict[str, Any]] = []
        self.link = hub.attach(name, frozenset(tools), self.send)

    async def send(self, text: str) -> None:
        frame = json.loads(text)
        self.calls.append(frame)
        reply = self.behave(frame, self)
        if reply is not None:
            self.hub.deliver(self.link, {"type": "result", "id": frame["id"], **reply})


def _ok(output: dict[str, Any]) -> Callable[[dict[str, Any], _FakeTerminal], dict[str, Any]]:
    return lambda _frame, _terminal: {"ok": True, "output": output}


def _brain_registry(
    hub: TerminalHub, root: Path, vision: _FakeVision | None = None,
) -> ToolRegistry:
    return build_default_registry(
        device_link=hub.call, obsidian_vault_root=root / "vault",
        vision_client=vision,
    )


def _on_a_thread(scenario: Callable[[], Any]) -> Any:  # noqa: ANN401 — the scenario's result
    """Run an async scenario; its tool dispatches go to worker threads, as in the daemon."""
    return asyncio.run(scenario())


# --- the menu --------------------------------------------------------------


def test_the_brains_device_tools_are_proxies_with_the_one_machine_definitions(
    tmp_path: Path,
) -> None:
    """Same names, order, descriptions, schemas, risk and flags; only the handler is another."""
    night = NightRun(tmp_path / "night.db", NightSettings(), MacPower())
    own = build_default_registry(obsidian_vault_root=tmp_path / "vault", night=night)
    brain = _brain_registry(TerminalHub(), tmp_path)
    mine, theirs = own.get_definitions(), brain.get_definitions()
    assert [t.name for t in theirs] == [t.name for t in mine]
    assert {t.name for t in theirs} >= DEVICE
    for a, b in zip(mine, theirs, strict=True):
        assert {f.name: getattr(a, f.name) for f in dataclasses.fields(a) if f.name != "handler"} \
            == {f.name: getattr(b, f.name) for f in dataclasses.fields(b) if f.name != "handler"}
        # A closure is rebuilt per registry, so compare what each handler is, not identity.
        assert (a.handler.__qualname__ == b.handler.__qualname__) == (a.name not in PROXIED)


def test_one_machine_registers_the_device_tools_as_they_were(tmp_path: Path) -> None:
    """With no link nothing is proxied: the handlers are the real ones."""
    own = build_default_registry(obsidian_vault_root=tmp_path / "vault")
    by_name = {t.name: t for t in own.get_definitions()}
    assert by_name["open_path"] is tools_module.open_path
    assert by_name["read_file"].handler is tools_module.read_file_handler
    assert by_name["open_url"].handler is tools_module.open_url_handler
    assert by_name["read_clipboard"] is tools_module.read_clipboard
    assert "screen_capture" not in by_name


# --- the proxy -------------------------------------------------------------


def test_a_proxy_returns_what_the_terminal_answers_and_keeps_the_tools_semantics(
    tmp_path: Path,
) -> None:
    """The result is the terminal's output, as an observation (`open_url`: an ack)."""

    async def scenario() -> tuple[RawResult, RawResult, RawResult, _FakeTerminal]:
        hub = TerminalHub(call_timeout_s=5)
        registry = _brain_registry(hub, tmp_path)
        outputs = {
            "read_file": {"path": "/Users/a/n.txt", "content": "hello", "truncated": False},
            "open_url": {"url": "https://example.com/"},
            "read_clipboard": {"content": "copied", "total_bytes": 6},
        }
        terminal = _FakeTerminal(hub, set(outputs), lambda f, _t: {
            "ok": True, "output": outputs[f["tool"]],
        })
        results = [
            await asyncio.to_thread(
                _dispatch, registry, tmp_path, tool, args, entity)
            for tool, args, entity in (
                ("read_file", {"target": "notes"}, "file:/Users/a/n.txt"),
                ("open_url", {"url": "https://example.com/"}, None),
                ("read_clipboard", {}, None),
            )
        ]
        return (*results, terminal)  # type: ignore[return-value]

    read, opened, clip, terminal = _on_a_thread(scenario)
    assert (read.error, read.semantics, read.payload["content"]) == (None, "observation", "hello")
    assert (opened.error, opened.semantics) == (None, "ack")
    assert clip.payload == {"content": "copied", "total_bytes": 6}
    assert json.loads(read.tool_output)["path"] == "/Users/a/n.txt"
    frame = terminal.calls[0]
    assert (frame["type"], frame["tool"], frame["arguments"], frame["target_entity_ref"]) == (
        "call", "read_file", {"target": "notes"}, "file:/Users/a/n.txt",
    )


def test_a_terminals_own_failure_reaches_the_model_with_its_code(tmp_path: Path) -> None:
    """`ok: false` becomes the tool's error result, code and sentence unchanged."""

    async def scenario() -> RawResult:
        hub = TerminalHub(call_timeout_s=5)
        _FakeTerminal(hub, {"read_file"}, lambda _f, _t: {
            "ok": False, "code": "file_not_found", "message": "read_file: nope is not a file",
        })
        return await asyncio.to_thread(
            _dispatch, _brain_registry(hub, tmp_path), tmp_path, "read_file", {}, "file:/nope",
        )

    result = _on_a_thread(scenario)
    assert result.error == "file_not_found"
    assert json.loads(result.tool_output) == {
        "error": "read_file: nope is not a file", "code": "file_not_found",
    }


def test_with_no_terminal_the_result_says_the_device_is_not_connected(tmp_path: Path) -> None:
    """A plain sentence the model can relay; after a terminal has been seen it names it."""

    async def scenario() -> tuple[RawResult, RawResult, RawResult]:
        hub = TerminalHub(call_timeout_s=5)
        registry = _brain_registry(hub, tmp_path)
        first = await asyncio.to_thread(_dispatch, registry, tmp_path, "read_clipboard", {})
        terminal = _FakeTerminal(hub, {"read_clipboard"}, _ok({"content": "", "total_bytes": 0}))
        while_connected = await asyncio.to_thread(
            _dispatch, registry, tmp_path, "read_clipboard", {},
        )
        hub.detach(terminal.link)
        after = await asyncio.to_thread(_dispatch, registry, tmp_path, "read_clipboard", {})
        return first, while_connected, after

    first, while_connected, after = _on_a_thread(scenario)
    assert while_connected.error is None
    assert first.error == "device_not_connected"
    assert json.loads(first.tool_output)["error"] == (
        "the terminal is not connected right now, so read_clipboard cannot run"
    )
    assert json.loads(after.tool_output)["error"] == (
        "macbook is not connected right now, so read_clipboard cannot run"
    )


def test_a_terminal_that_did_not_declare_the_tool_is_not_asked(tmp_path: Path) -> None:
    """Calls go only to a terminal that declared the tool."""

    async def scenario() -> tuple[RawResult, _FakeTerminal]:
        hub = TerminalHub(call_timeout_s=5)
        terminal = _FakeTerminal(hub, {"read_clipboard"}, _ok({}))
        result = await asyncio.to_thread(
            _dispatch, _brain_registry(hub, tmp_path), tmp_path, "open_url", {"url": "x"},
        )
        return result, terminal

    result, terminal = _on_a_thread(scenario)
    assert result.error == "device_not_connected"
    assert terminal.calls == []


def test_a_call_times_out_when_the_terminal_does_not_answer(tmp_path: Path) -> None:
    """No answer within the limit: a plain timeout error, and the hub is clean afterwards."""

    async def scenario() -> tuple[RawResult, float, int]:
        hub = TerminalHub(call_timeout_s=0.3)
        terminal = _FakeTerminal(hub, {"read_clipboard"}, lambda _f, _t: None)
        began = time.monotonic()
        result = await asyncio.to_thread(
            _dispatch, _brain_registry(hub, tmp_path), tmp_path, "read_clipboard", {},
        )
        return result, time.monotonic() - began, len(terminal.link.pending)

    result, took, pending = _on_a_thread(scenario)
    assert result.error == "device_timeout"
    assert json.loads(result.tool_output)["error"] == (
        "macbook did not answer read_clipboard within 0.3 s"
    )
    assert 0.25 < took < 3
    assert pending == 0


def test_a_terminal_that_drops_mid_call_fails_the_call_at_once(tmp_path: Path) -> None:
    """The socket ending while a call is out resolves it as a disconnect, not a timeout."""

    async def scenario() -> tuple[RawResult, float]:
        hub = TerminalHub(call_timeout_s=30)

        def drop(_frame: dict[str, Any], terminal: _FakeTerminal) -> None:
            terminal.hub.detach(terminal.link)

        _FakeTerminal(hub, {"read_clipboard"}, drop)
        began = time.monotonic()
        result = await asyncio.to_thread(
            _dispatch, _brain_registry(hub, tmp_path), tmp_path, "read_clipboard", {},
        )
        return result, time.monotonic() - began

    result, took = _on_a_thread(scenario)
    assert result.error == "device_disconnected"
    assert json.loads(result.tool_output)["error"] == (
        "macbook disconnected while running read_clipboard"
    )
    assert took < 5


def test_the_most_recently_connected_terminal_gets_the_call() -> None:
    """Two terminals declare a tool: the newer one runs it; when it leaves, the older does."""

    async def scenario() -> list[str]:
        hub = TerminalHub(call_timeout_s=5)
        older = _FakeTerminal(hub, {"read_clipboard"}, _ok({"content": "old", "total_bytes": 3}))
        newer = _FakeTerminal(
            hub, {"read_clipboard"}, _ok({"content": "new", "total_bytes": 3}), name="laptop",
        )
        seen = [(await asyncio.to_thread(hub.call, "read_clipboard", {}, None))["content"]]
        hub.detach(newer.link)
        seen.append((await asyncio.to_thread(hub.call, "read_clipboard", {}, None))["content"])
        assert [name for name, _ in hub.connected()] == ["macbook"]
        assert older.link in hub._links  # noqa: SLF001 — the older link is still the one held
        return seen

    assert _on_a_thread(scenario) == ["new", "old"]


def test_a_call_for_what_she_says_unprompted_goes_to_the_voice_terminal() -> None:
    """The voice terminal that connected last plays it, not the terminal connected last.

    With no voice terminal it is the newest terminal that declared the tool, and a slow
    terminal is waited for only as long as the caller says.
    """

    async def scenario() -> list[str]:
        hub = TerminalHub(call_timeout_s=30)
        speaker = _FakeTerminal(hub, {"timesink_read"}, _ok({"result": "speaker"}), name="macbook")
        other = _FakeTerminal(hub, {"timesink_read"}, _ok({"result": "other"}), name="laptop")
        async def played() -> str:
            reply = await asyncio.to_thread(hub.call_player, "timesink_read", {}, None, 5)
            return str(reply["result"])

        seen = [await played()]
        hub.voice = BrainVoice(cast("Any", object()), _NoRows())
        speaker.link.speech = hub.voice.attach("macbook", speaker.send, None)
        seen.append(await played())
        hub.detach(speaker.link)
        seen.append(await played())
        hub.detach(other.link)
        _FakeTerminal(hub, {"timesink_read"}, lambda _f, _t: None, name="slow")
        began = time.monotonic()
        with pytest.raises(DeviceCallError) as slow:
            await asyncio.to_thread(hub.call_player, "timesink_read", {}, None, 0.3)
        assert slow.value.code == "device_timeout"
        assert time.monotonic() - began < 5
        return seen

    assert _on_a_thread(scenario) == ["other", "speaker", "other"]


def test_a_brain_asks_its_terminal_where_sound_would_come_out_and_one_machine_asks_itself(
    tmp_path: Path,
) -> None:
    """The wiring gives job mail and reminders the terminal's answer on a brain, else the Mac's."""
    reminders = Reminders(tmp_path / "events.db")
    assert _sound_output(None, None, reminders) is audio_output.current_output
    assert reminders.output is audio_output.current_output
    mail = cast("JobMail", SimpleNamespace(output=None))
    output = _sound_output(TerminalHub(), mail, reminders)
    assert isinstance(output, audio_output.TerminalOutput)
    assert reminders.output is output
    assert mail.output is output
    assert output()["private"] is False  # no terminal connected


def test_a_device_call_on_the_loop_that_serves_the_terminal_is_refused() -> None:
    """A handler running on the loop would deadlock its own answer; it fails instead."""

    async def scenario() -> str:
        hub = TerminalHub(call_timeout_s=5)
        _FakeTerminal(hub, {"read_clipboard"}, _ok({}))
        with pytest.raises(DeviceCallError) as raised:
            hub.call("read_clipboard", {}, None)
        return raised.value.code

    assert _on_a_thread(scenario) == "device_call_on_loop"


# --- the entity resolver on a brain ---------------------------------------


def test_a_brain_resolves_a_spoken_file_name_on_the_terminal(tmp_path: Path) -> None:
    """The name goes to the terminal; its path is the entity. No terminal is a miss."""

    async def scenario() -> tuple[Any, Any, Any]:
        hub = TerminalHub(call_timeout_s=5)
        conn = open_event_log(tmp_path / "events.db")
        resolve = _make_entity_resolver(conn, hub)
        before = await asyncio.to_thread(resolve, "my notes")
        _FakeTerminal(hub, {"resolve_file"}, _ok({"path": "/Users/a/notes.md", "source": "search"}))
        found = await asyncio.to_thread(resolve, "my notes")
        _FakeTerminal(
            hub, {"resolve_file"}, lambda _f, _t: {"ok": False, "code": "target_not_found",
                                                    "message": "no file matched"},
            name="other",
        )
        missed = await asyncio.to_thread(resolve, "my notes")
        conn.close()
        return before, found, missed

    before, found, missed = _on_a_thread(scenario)
    assert before is None
    assert (found.entity_id, found.canonical, found.confidence) == (
        "file:/Users/a/notes.md", "/Users/a/notes.md", "fuzzy",
    )
    assert missed is None


# --- screen_look split -----------------------------------------------------


def _shot(image: bytes = PNG, **extra: Any) -> dict[str, Any]:  # noqa: ANN401 — reply fields
    return {"image_b64": base64.b64encode(image).decode("ascii"), "bytes": len(image), **extra}


def test_screen_look_on_a_brain_describes_what_the_terminal_captured(tmp_path: Path) -> None:
    """The terminal's PNG is saved on the brain and the brain's vision client describes it."""
    vision = _FakeVision()
    calls: list[tuple[str, Mapping[str, Any], str | None]] = []

    def link(tool: str, arguments: Mapping[str, Any], entity: str | None) -> dict[str, Any]:
        calls.append((tool, arguments, entity))
        return _shot()

    registry = build_default_registry(
        device_link=link, vision_client=vision, obsidian_vault_root=None,
    )
    result = _dispatch(registry, tmp_path, "screen_look", {"question": " what is open? "})
    assert result.error is None
    assert calls == [("screen_capture", {}, None)]
    assert result.payload["description"] == "a terminal window and a browser"
    [(saved, question, seen)] = vision.calls
    assert (question, seen) == ("what is open?", PNG)
    assert result.payload["artifact_path"] == str(saved)
    assert saved.parent == tmp_path / "artifacts" / "screen_artifacts"


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"image_b64": 5},
        {"image_b64": "not base64 !!"},
        _shot(b"GIF89a" + b"x" * 50),
        _shot(PNG + b"x" * (tools_module.SCREEN_CAPTURE_MAX_IMAGE_BYTES + 1)),
    ],
)
def test_a_screenshot_that_is_not_a_png_within_the_cap_never_reaches_vision(
    tmp_path: Path, reply: dict[str, Any],
) -> None:
    """A malformed or oversize reply is a plain error; the model is never called."""
    vision = _FakeVision()
    registry = build_default_registry(
        device_link=lambda *_: reply, vision_client=vision, obsidian_vault_root=None,
    )
    result = _dispatch(registry, tmp_path, "screen_look", {})
    assert result.error == "screen_capture_failed"
    assert vision.calls == []


def test_screen_look_on_a_brain_with_no_terminal_says_so_and_does_not_describe(
    tmp_path: Path,
) -> None:
    """No terminal: the tool's error is the not-connected sentence; vision is not called."""
    vision = _FakeVision()
    registry = _brain_registry(TerminalHub(), tmp_path, vision)
    result = _dispatch(registry, tmp_path, "screen_look", {})
    assert result.error == "device_not_connected"
    assert "is not connected right now" in json.loads(result.tool_output or "")["error"]
    assert vision.calls == []


def test_the_terminals_screen_capture_returns_the_image_and_holds_no_vision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capture tool saves nothing, takes no vision client, and returns the PNG as base64."""
    seen: list[int] = []

    def capture(path: Path, *, timeout_s: float) -> subprocess.CompletedProcess[bytes]:  # noqa: ARG001
        path.write_bytes(PNG)
        return subprocess.CompletedProcess(["screencapture"], 0, b"", b"")

    def downscale(
        path: Path, width: int, *, timeout_s: float,  # noqa: ARG001
    ) -> subprocess.CompletedProcess[bytes]:
        seen.append(width)
        return subprocess.CompletedProcess(["sips"], 0, b"", b"")

    monkeypatch.setattr(tools_module, "_run_screencapture", capture)
    monkeypatch.setattr(tools_module, "_run_sips_downscale", downscale)
    tool = make_screen_capture(900)
    assert "vision" not in tool.handler.__code__.co_names
    registry = ToolRegistry()
    registry.register(tool)
    result = _dispatch(registry, tmp_path, "screen_capture", {})
    assert result.error is None
    assert base64.b64decode(result.payload["image_b64"]) == PNG
    assert seen == [900]
    assert not (tmp_path / "artifacts").exists()

    monkeypatch.setattr(tools_module, "SCREEN_CAPTURE_MAX_IMAGE_BYTES", len(PNG) - 1)
    assert _dispatch(registry, tmp_path, "screen_capture", {}).error == "screen_image_too_large"


# --- the terminal's runner -------------------------------------------------


def _terminal_registry(tmp_path: Path, *, vault: bool = False) -> ToolRegistry:
    (tmp_path / "vault").mkdir(exist_ok=True)
    registry = build_default_registry(obsidian_vault_root=tmp_path / "vault" if vault else None)
    registry.register(make_screen_capture(800))
    return registry


def test_a_terminal_declares_the_device_tools_it_can_run(tmp_path: Path) -> None:
    """The six it may run, the file-name resolver and the three device reads (ADR 0170).

    Never write_file or a menu tool.
    """
    assert _declared(_terminal_registry(tmp_path)) == {
        "open_path", "read_file", "read_clipboard", "open_url", "screen_capture", "resolve_file",
        "timesink_read", "git_read", "claude_read",
    }
    assert "search_notes" in _declared(_terminal_registry(tmp_path, vault=True))


def test_the_runner_executes_the_real_handlers_and_reports_their_results(
    tmp_path: Path,
) -> None:
    """read_file and search_notes run as on one machine; failures carry code and sentence."""
    execute = make_executor(_terminal_registry(tmp_path, vault=True))
    note = tmp_path / "note.txt"
    note.write_text("alpha beta\n", encoding="utf-8")
    (tmp_path / "vault" / "a.md").write_text("the quick brown fox\n", encoding="utf-8")

    read = execute("read_file", {"target": "note"}, f"file:{note}")
    assert read["ok"] is True
    assert (read["output"]["content"], read["output"]["total_bytes"]) == ("alpha beta\n", 11)

    missing = execute("read_file", {"target": "x"}, f"file:{tmp_path / 'gone.txt'}")
    assert missing["ok"] is False
    assert missing["code"] == "file_not_found"
    assert "does not exist" in missing["message"]

    assert execute("read_file", {"target": "x"}, None)["code"] == "no_resolved_target"
    found = execute("search_notes", {"query": "brown"}, None)
    assert found["ok"] is True
    assert "a.md" in json.dumps(found["output"])

    opened = execute("open_url", {"url": "file:///etc/passwd"}, None)
    assert opened["ok"] is False
    assert opened["code"] != "unknown_tool"

    nothing = execute("resolve_file", {"query": "zz-no-such-file-anywhere-1234"}, None)
    assert (nothing["ok"], nothing["code"]) == (False, "target_not_found")


def test_the_runner_refuses_what_it_did_not_declare(tmp_path: Path) -> None:
    """A tool off the declared set (a web tool, write_file) never runs on a terminal."""
    execute = make_executor(_terminal_registry(tmp_path))
    for tool in ("web_fetch", "write_file", "create_memo", "search_notes", "nope"):
        reply = execute(tool, {}, None)
        assert (reply["ok"], reply["code"]) == (False, "unknown_tool")


# --- /terminal/ws ----------------------------------------------------------


def _terminal_client(
    tmp_path: Path, *, peer: str = REMOTE, wired: bool = True,
) -> tuple[TestClient, TerminalHub]:
    hub = TerminalHub(call_timeout_s=5)
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            terminals=hub if wired else None,
            device_name=functools.partial(device_name_for_token, tmp_path) if wired else None,
        ),
    )
    require_local_key(
        app,
        functools.partial(local_key_matches, local_key(tmp_path)),
        device_token_matches=functools.partial(device_token_matches, tmp_path),
    )
    return TestClient(app, base_url="http://127.0.0.1:8006", client=(peer, 50000)), hub


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _refused(client: TestClient, headers: dict[str, str]) -> int:
    """The close code of a socket the server would not accept."""
    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(
        TERMINAL_WS, headers=headers,
    ):
        pass
    return int(refused.value.code)


def test_terminal_ws_takes_only_a_paired_devices_token(tmp_path: Path) -> None:
    """No token, a wrong one, a revoked one, the local key, and a loopback peer all fail."""
    token = pair_device(tmp_path, "macbook")
    gone = pair_device(tmp_path, "old")
    unpair_device(tmp_path, "old")
    client, hub = _terminal_client(tmp_path)
    for headers in (
        {}, _bearer("wrong"), _bearer(gone), _bearer(local_key(tmp_path)), {"Authorization": token},
    ):
        assert _refused(client, headers) == 1008, headers
    assert hub.connected() == []

    # A peer on loopback is checked against the local key, which is no device token.
    loopback, loopback_hub = _terminal_client(tmp_path, peer="127.0.0.1")
    for headers in (_bearer(token), _bearer(local_key(tmp_path)), {}):
        assert _refused(loopback, headers) == 1008, headers
    assert loopback_hub.connected() == []


def test_a_brain_with_no_device_token_wiring_has_no_terminal_route(tmp_path: Path) -> None:
    """Without `terminals` and `device_name` the route is not registered, for anyone."""
    token = pair_device(tmp_path, "macbook")
    client, _hub = _terminal_client(tmp_path, wired=False)
    assert _refused(client, _bearer(token)) == 1000  # no such route: closed before accept


def test_a_paired_terminal_says_hello_is_named_by_its_token_and_answers_calls(
    tmp_path: Path,
) -> None:
    """Hello, then ready with the paired name; a brain call arrives as a frame; result returns."""
    token = pair_device(tmp_path, "macbook")
    client, hub = _terminal_client(tmp_path)
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(json.dumps({"type": "hello", "tools": ["read_clipboard"]}))
        assert ws.receive_json() == {"type": "ready", "device": "macbook"}
        assert hub.connected() == [("macbook", frozenset({"read_clipboard"}))]

        outcome: list[Any] = []
        caller = threading.Thread(
            target=lambda: outcome.append(hub.call("read_clipboard", {}, None)),
        )
        caller.start()
        frame = ws.receive_json()
        assert (frame["type"], frame["tool"]) == ("call", "read_clipboard")
        ws.send_text(json.dumps({"type": "result", "id": frame["id"], "ok": True,
                                 "output": {"content": "hi", "total_bytes": 2}}))
        caller.join(timeout=5)
        assert outcome == [{"content": "hi", "total_bytes": 2}]

        ws.send_text(json.dumps({"type": "result", "id": "unknown", "ok": True, "output": {}}))
        ws.send_text("not json")
    assert hub.connected() == []


@pytest.mark.parametrize(
    "hello",
    ['{"type": "result"}', "nonsense", '{"type": "hello", "tools": "read_file"}',
     '{"type": "hello", "tools": [1]}'],
)
def test_a_first_frame_that_is_not_a_hello_closes_the_socket(tmp_path: Path, hello: str) -> None:
    """The terminal must declare its tools first; anything else is refused."""
    token = pair_device(tmp_path, "macbook")
    client, hub = _terminal_client(tmp_path)
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(hello)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1008
    assert hub.connected() == []


# --- a real terminal client against a real server --------------------------


class _AsRemote:
    """ASGI shim: the server sees every peer as a device on the tailnet, not on loopback."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            scope = {**scope, "client": (REMOTE, 50000)}
        await self.app(scope, receive, send)


class _Brain:
    """A real uvicorn server with the terminal route and device-token check."""

    def __init__(self, tmp_path: Path) -> None:
        self.hub = TerminalHub(call_timeout_s=10)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                terminals=self.hub,
                device_name=functools.partial(device_name_for_token, tmp_path),
            ),
        )
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(tmp_path)),
            device_token_matches=functools.partial(device_token_matches, tmp_path),
        )
        self.server = uvicorn.Server(
            uvicorn.Config(_AsRemote(app), host="127.0.0.1", port=self.port,
                           log_level="warning", lifespan="off"),
        )
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve()), daemon=True,
        )

    def __enter__(self) -> Self:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server never started"
            time.sleep(0.05)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class _Client:
    """`run_terminal_client` on its own thread and loop, stoppable like Ctrl-C."""

    def __init__(self, url: str, token: str, execute: terminal_link.Execute) -> None:
        self.loop = asyncio.new_event_loop()
        self.error: BaseException | None = None
        self.task: asyncio.Task[None] | None = None
        self._args = (url, token, frozenset({"read_file", "read_clipboard"}), execute)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def main() -> None:
            url, token, tools, execute = self._args
            self.task = asyncio.create_task(
                run_terminal_client(url, token, tools=tools, execute=execute),
            )
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except BaseException as exc:  # noqa: BLE001 — handed to the test
                self.error = exc

        self.loop.run_until_complete(main())

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(lambda: self.task and self.task.cancel())
        self.thread.join(timeout=10)

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + 10
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def test_a_real_terminal_runs_a_brains_call_and_the_brain_notices_when_it_stops(
    tmp_path: Path,
) -> None:
    """Pair, connect, dispatch read_file through the proxy to the terminal, stop the terminal."""
    token = pair_device(tmp_path, "macbook")
    note = tmp_path / "note.txt"
    note.write_text("on the mac\n", encoding="utf-8")
    execute = make_executor(_terminal_registry(tmp_path))
    with _Brain(tmp_path) as brain, _Client(brain.url, token, execute) as terminal:
        _wait_for(lambda: bool(brain.hub.connected()), "the terminal never connected")
        [(name, tools)] = brain.hub.connected()
        assert (name, tools) == ("macbook", frozenset({"read_file", "read_clipboard"}))

        registry = _brain_registry(brain.hub, tmp_path)
        result = _dispatch(registry, tmp_path, "read_file", {"target": "note"}, f"file:{note}")
        assert result.error is None
        assert result.payload["content"] == "on the mac\n"
        gone = _dispatch(registry, tmp_path, "read_file", {"target": "x"}, f"file:{tmp_path}/no")
        assert gone.error == "file_not_found"

        terminal.stop()
        _wait_for(lambda: not brain.hub.connected(), "the brain never saw the terminal leave")
        stopped = _dispatch(registry, tmp_path, "read_file", {"target": "x"}, f"file:{note}")
        assert stopped.error == "device_not_connected"
        assert json.loads(stopped.tool_output or "")["error"] == (
            "macbook is not connected right now, so read_file cannot run"
        )
        assert terminal.error is None


def test_a_terminal_with_an_unpaired_or_revoked_token_stops_with_a_reason(
    tmp_path: Path,
) -> None:
    """HTTP 403 from the brain ends the client; it does not retry a token that cannot work."""
    with _Brain(tmp_path) as brain, _Client(brain.url, "not-paired", lambda *_: {}) as terminal:
        terminal.thread.join(timeout=10)
        assert isinstance(terminal.error, TerminalRefusedError)
        assert "HTTP 403" in str(terminal.error)


def test_a_terminal_reconnects_with_backoff_when_the_link_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped socket, then a brain that is down, then back: the client keeps trying."""
    monkeypatch.setattr(terminal_link, "_RECONNECT_FIRST_S", 0.05)
    monkeypatch.setattr(terminal_link, "_RECONNECT_LAST_S", 0.2)

    async def scenario() -> tuple[int, dict[str, Any]]:
        connections = 0
        answered: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        async def brain(ws: Any) -> None:  # noqa: ANN401 — a websockets connection
            nonlocal connections
            connections += 1
            hello = json.loads(await ws.recv())
            assert hello["type"] == "hello"
            await ws.send(json.dumps({"type": "ready", "device": "macbook"}))
            if connections == 1:
                await ws.close()
                return
            await ws.send(json.dumps({"type": "call", "id": "c1", "tool": "read_clipboard",
                                      "arguments": {}, "target_entity_ref": None}))
            answered.set_result(json.loads(await ws.recv()))

        port_holder = socket.socket()
        port_holder.bind(("127.0.0.1", 0))
        port = port_holder.getsockname()[1]
        port_holder.close()  # nothing listens yet: the first attempts are refused
        client = asyncio.create_task(run_terminal_client(
            f"http://127.0.0.1:{port}", "t", tools=frozenset({"read_clipboard"}),
            execute=lambda *_: {"ok": True, "output": {"content": "x", "total_bytes": 1}},
        ))
        await asyncio.sleep(0.2)
        async with serve(brain, "127.0.0.1", port):
            reply = await asyncio.wait_for(answered, 10)
        client.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client
        return connections, reply

    connections, reply = asyncio.run(scenario())
    assert connections == 2
    assert reply == {"type": "result", "id": "c1", "ok": True,
                     "output": {"content": "x", "total_bytes": 1}}


def test_a_tool_that_raises_on_the_terminal_is_an_error_answer_not_a_dead_link() -> None:
    """The runner's exception becomes a `terminal_error` result and the link carries on."""

    async def scenario() -> list[dict[str, Any]]:
        got: list[dict[str, Any]] = []

        async def brain(ws: Any) -> None:  # noqa: ANN401 — a websockets connection
            await ws.recv()
            await ws.send(json.dumps({"type": "ready", "device": "d"}))
            for call_id in ("1", "2"):
                await ws.send(json.dumps({"type": "call", "id": call_id, "tool": "t",
                                          "arguments": {}, "target_entity_ref": None}))
                got.append(json.loads(await ws.recv()))

        def execute(_tool: str, _args: Mapping[str, Any], _entity: str | None) -> dict[str, Any]:
            if not got:
                msg = "boom"
                raise RuntimeError(msg)
            return {"ok": True, "output": {}}

        async with serve(brain, "127.0.0.1", 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            client = asyncio.create_task(run_terminal_client(
                f"http://127.0.0.1:{port}", "t", tools=frozenset({"t"}), execute=execute,
            ))
            while len(got) < 2:  # noqa: ASYNC110 — a short poll in a test
                await asyncio.sleep(0.02)
            client.cancel()
        return got

    got = asyncio.run(scenario())
    assert got[0] == {"type": "result", "id": "1", "ok": False, "code": "terminal_error",
                      "message": "RuntimeError"}
    assert got[1]["ok"] is True


# --- the command ------------------------------------------------------------


def test_the_terminal_command_refuses_a_missing_or_loose_token_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """No brain is contacted without a 0600 token; the reason is on stderr, exit 1."""
    loose = tmp_path / "loose"
    loose.write_text("secret-token\n", encoding="utf-8")
    loose.chmod(0o644)
    for token_file, reason in ((tmp_path / "missing", "pair this device first"),
                               (loose, "readable by others")):
        code = main(["terminal", "--brain", "http://127.0.0.1:1", "--brain-token-file",
                     str(token_file)])
        err = capsys.readouterr().err
        assert code == 1
        assert reason in err
        assert "secret-token" not in err
    assert main(["terminal", "--brain", "jarvis:8006", "--brain-token-file", str(loose)]) == 1
    assert "--brain must look like" in capsys.readouterr().err


def test_ctrl_c_stops_the_terminal_command_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """KeyboardInterrupt while connected is exit 0, not a traceback."""
    token = tmp_path / "token"
    token.write_text("t\n", encoding="utf-8")
    token.chmod(0o600)

    async def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("jarvis.runtime.terminal.run_terminal_client", interrupted)
    code = main(["terminal", "--brain", "http://127.0.0.1:1", "--brain-token-file", str(token),
                 "--runtime-root", str(tmp_path)])
    assert code == 0
    assert "Traceback" not in capsys.readouterr().err


def test_a_refused_token_makes_the_command_exit_with_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Against a real brain: an unpaired token is exit 1 and a sentence, no retry loop."""
    token = tmp_path / "token"
    token.write_text("not-paired\n", encoding="utf-8")
    token.chmod(0o600)
    with _Brain(tmp_path) as brain:
        code = main(["terminal", "--brain", brain.url, "--brain-token-file", str(token),
                     "--runtime-root", str(tmp_path)])
    assert code == 1
    assert "refused this device's token (HTTP 403)" in capsys.readouterr().err
