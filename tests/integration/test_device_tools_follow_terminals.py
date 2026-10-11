"""ADR 0225: a brain offers the device tools its paired terminals declare, and none of its own.

Built from the real registry, the real hub and the real runtime-root files: the declarations a
terminal's ``hello`` carries reach the menu through the hub's own ``attach``, survive a restart in
``terminal-tools.json``, and are forgotten with the pairing.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.execution.tools import ToolRegistry, build_default_registry
from jarvis.runtime import bootstrap_runtime_app, device_tools
from jarvis.runtime.inherent_loop import _claude_sessions_served
from jarvis.shared import CallerPrincipal
from jarvis.shared.device_link import DeviceCallError
from jarvis.state.device_tokens import pair_device, unpair_device
from jarvis.surface.terminal_link import TerminalHub

if TYPE_CHECKING:
    from pathlib import Path

DEVICE_MENU = {
    "open_path", "search_notes", "read_file", "read_clipboard", "open_url", "screen_look",
    "write_file", "start_night_run", "end_night_run",
}


async def _send(_frame: str) -> None:
    return None


def _hello(hub: TerminalHub, name: str, tools: set[str]) -> None:
    """A terminal says hello through the hub's own ``attach``, as ``serve_terminal`` does."""

    async def connect() -> None:
        hub.attach(name, frozenset(tools), _send)

    asyncio.run(connect())


def _menu(registry: ToolRegistry) -> set[str]:
    return {t.name for t in registry.for_caller(CallerPrincipal.JARVIS_LLM)} & DEVICE_MENU


def _brain(root: Path) -> tuple[ToolRegistry, TerminalHub]:
    """A brain's registry with no vault named, following the hub, as the runtime wires it."""
    hub = TerminalHub()
    registry = build_default_registry(device_link=hub.call, obsidian_vault_root=None)
    device_tools.follow_terminals(hub, root, registry)
    return registry, hub


def test_the_menu_offers_what_a_terminal_declares_and_nothing_else(tmp_path: Path) -> None:
    """None before a hello; the declared ones (`screen_look` via `screen_capture`); then fewer."""
    pair_device(tmp_path, "macbook")
    registry, hub = _brain(tmp_path)
    assert _menu(registry) == set()

    _hello(hub, "macbook", {"open_path", "search_notes", "screen_capture", "git_read"})
    assert _menu(registry) == {"open_path", "search_notes", "screen_look"}

    _hello(hub, "macbook", {"open_path", "screen_capture"})  # reconnects without search_notes
    assert _menu(registry) == {"open_path", "screen_look"}
    names = [t.name for t in registry.get_definitions()]
    assert len(names) == len(set(names))


def test_an_unchanged_declaration_leaves_the_menu_object_alone(tmp_path: Path) -> None:
    """A reconnect that declares the same tools does not rebuild the menu (the cached prefix)."""
    pair_device(tmp_path, "macbook")
    registry, hub = _brain(tmp_path)
    _hello(hub, "macbook", {"open_url", "read_file"})
    before = registry.get_definitions()
    written = (tmp_path / "terminal-tools.json").stat().st_mtime_ns
    _hello(hub, "macbook", {"read_file", "open_url"})
    assert registry.get_definitions() == before
    assert (tmp_path / "terminal-tools.json").stat().st_mtime_ns == written


def test_the_tools_of_every_paired_terminal_are_offered_together(tmp_path: Path) -> None:
    """The menu is the union over the paired devices, connected or not."""
    for name in ("macbook", "mini"):
        pair_device(tmp_path, name)
    registry, hub = _brain(tmp_path)
    _hello(hub, "macbook", {"open_path"})
    _hello(hub, "mini", {"read_clipboard"})
    assert _menu(registry) == {"open_path", "read_clipboard"}


def test_declarations_outlive_the_process_and_an_unpaired_device_is_forgotten(
    tmp_path: Path,
) -> None:
    """A fresh registry on the same root offers them before any terminal connects."""
    pair_device(tmp_path, "macbook")
    pair_device(tmp_path, "mini")
    _, hub = _brain(tmp_path)
    _hello(hub, "macbook", {"open_path", "search_notes"})
    _hello(hub, "mini", {"open_url"})
    stored = json.loads((tmp_path / "terminal-tools.json").read_text(encoding="utf-8"))
    assert stored == {"devices": {
        "macbook": ["open_path", "search_notes"], "mini": ["open_url"],
    }}

    restarted, _ = _brain(tmp_path)
    assert _menu(restarted) == {"open_path", "search_notes", "open_url"}

    unpair_device(tmp_path, "mini")
    after_unpair, _ = _brain(tmp_path)
    assert _menu(after_unpair) == {"open_path", "search_notes"}
    stored = json.loads((tmp_path / "terminal-tools.json").read_text(encoding="utf-8"))
    assert set(stored["devices"]) == {"macbook"}


def test_a_row_of_a_device_that_is_no_longer_paired_is_not_offered(tmp_path: Path) -> None:
    """Even a stale file (a pairing removed by hand) cannot put a tool on the menu."""
    pair_device(tmp_path, "macbook")
    (tmp_path / "terminal-tools.json").write_text(
        json.dumps({"devices": {"macbook": ["open_url"], "gone": ["read_file"]}}),
        encoding="utf-8",
    )
    registry, _ = _brain(tmp_path)
    assert _menu(registry) == {"open_url"}


def test_a_call_says_whether_no_terminal_is_here_or_the_one_here_lacks_the_tool(
    tmp_path: Path,
) -> None:
    """`device_not_connected` for nobody; `device_lacks_tool`, naming the device, otherwise."""
    pair_device(tmp_path, "macbook")
    _, hub = _brain(tmp_path)
    with pytest.raises(DeviceCallError) as nobody:
        hub.call("search_notes", {}, None)
    assert nobody.value.code == "device_not_connected"

    _hello(hub, "macbook", {"open_path"})
    with pytest.raises(DeviceCallError) as lacking:
        hub.call("search_notes", {}, None)
    assert lacking.value.code == "device_lacks_tool"
    assert str(lacking.value) == "macbook is connected but does not offer search_notes"


def test_a_hello_that_cannot_be_remembered_does_not_stop_the_terminal() -> None:
    """A fault in the bookkeeping is logged; the terminal stays attached."""
    hub = TerminalHub()

    def broken(_device: str, _tools: frozenset[str]) -> None:
        message = "disk full"
        raise OSError(message)

    hub.on_hello = broken
    _hello(hub, "macbook", {"open_path"})
    assert hub.connected() == [("macbook", frozenset({"open_path"}))]


def _brain_runtime(root: Path, settings: str, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    root.mkdir()
    (root / "settings.yaml").write_text(settings, encoding="utf-8")
    monkeypatch.setenv("HOME", str(root.parent))
    return bootstrap_runtime_app(runtime_root=root)


def test_a_brain_boots_with_device_settings_that_only_a_terminal_could_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative open_path bookmark and a vault of its own change nothing on a brain."""
    settings = (
        "runtime:\n  role: brain\n"
        "tools:\n  open_path:\n    bookmarks:\n      notes: relative/notes\n"
        "  obsidian:\n    vault_root: /nowhere/at/all\n"
        "observer:\n  claude_sessions:\n    enabled: false\n"
    )
    brain = _brain_runtime(tmp_path / "brain", settings, monkeypatch)
    try:
        assert brain.config["tools"]["open_path"] is None  # the terminal's, not the brain's
        assert "search_notes" not in {t.name for t in brain.tool_registry.get_definitions()}
        pair_device(brain.runtime_paths.root, "macbook")
        _hello(brain.terminal_hub, "macbook", {"search_notes", "open_path"})
        assert {"search_notes", "open_path"} <= _menu(brain.tool_registry)
        # The same file is what the next boot starts from, with no terminal connected.
        restarted = _brain_runtime_again(brain.runtime_paths.root, monkeypatch)
        try:
            assert {"search_notes", "open_path"} <= _menu(restarted.tool_registry)
            assert restarted.terminal_hub.connected() == []
        finally:
            restarted.conn.close()
    finally:
        brain.conn.close()


def _brain_runtime_again(root: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    monkeypatch.setenv("HOME", str(root.parent))
    return bootstrap_runtime_app(runtime_root=root)


@pytest.mark.parametrize(
    ("settings", "served"),
    [
        ("runtime:\n  role: brain\n  listen_addresses: ['127.0.0.1']\n", True),
        ("runtime:\n  role: brain\n", False),
        ("assistant_name: Jarvis\n", False),
        ("observer:\n  claude_sessions:\n    enabled: true\n", True),
    ],
)
def test_the_agents_page_is_served_wherever_a_terminal_can_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: str, served: bool,  # noqa: FBT001
) -> None:
    """A brain ignores its own `observer.claude_sessions.enabled` (off here); `all` honors it."""
    runtime = _brain_runtime(tmp_path / "root", settings, monkeypatch)
    try:
        assert _claude_sessions_served(runtime) is served
    finally:
        runtime.conn.close()
