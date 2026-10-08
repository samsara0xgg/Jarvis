"""ADR 0192 on a brain: the night run is the terminal's, and so are its routes and tools.

A brain has no Mac to keep awake. The terminal holds the run (``NightRun`` over its own small
log, the Mac recorded by ``_Mac``), answers the companion's ``/inherent/night`` itself, and
declares the two night tools; the brain's registry holds them as proxies that run there. The
clock is the night-run tests' own: 2026-09-29 23:00 in Vancouver.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from jarvis.execution.tools import build_default_registry
from jarvis.runtime.terminal import _declared, _Speech, _start_night, make_executor
from jarvis.shared.device_link import DeviceCallError
from jarvis.surface.terminal_ui import Device
from tests.integration.test_night_run import _Fixture, _ms
from tests.integration.test_terminal_link import _dispatch
from tests.integration.test_terminal_ui import _rig

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

NIGHT_TOOLS = {"start_night_run", "end_night_run"}


def _link_to(execute: Any) -> Any:  # noqa: ANN401 — the terminal's runner, as the hub calls it.
    def link(tool: str, arguments: Mapping[str, Any], entity: str | None) -> dict[str, Any]:
        reply = execute(tool, arguments, entity)
        if not reply["ok"]:
            raise DeviceCallError(reply["message"], code=reply["code"])
        return dict(reply["output"])

    return link


def test_a_brain_starts_and_ends_the_night_on_its_terminal(tmp_path: Path) -> None:
    """The brain's tool reaches the Mac that holds the night: awake, then put back."""
    fx = _Fixture(tmp_path / "terminal", at=_ms(23))
    try:
        held = build_default_registry(obsidian_vault_root=None, night=fx.night)
        assert _declared(held) >= NIGHT_TOOLS
        execute = make_executor(held)
        brain = build_default_registry(device_link=_link_to(execute), obsidian_vault_root=None)
        assert {t.name for t in brain.get_definitions()} >= NIGHT_TOOLS

        started = _dispatch(brain, tmp_path, "start_night_run", {"hours": 1})
        assert started.error is None
        assert started.payload["status"] == "started"
        assert ("hold", 3600) in fx.mac.calls
        assert fx.night.snapshot()["night"] is not None

        ended = _dispatch(brain, tmp_path, "end_night_run", {})
        assert ended.error is None
        assert ended.payload["status"] == "cancelled"  # before the screen went dark
        assert fx.night.snapshot()["night"] is None
        assert not fx.mac.held
    finally:
        fx.close()


def test_a_brain_without_its_terminal_says_the_device_is_not_there(tmp_path: Path) -> None:
    """No terminal connected is the tool's plain error, not a run kept on the brain."""

    def nobody(_tool: str, _arguments: Mapping[str, Any], _entity: str | None) -> dict[str, Any]:
        message = "the owner's device is not connected"
        raise DeviceCallError(message, code="device_not_connected")

    brain = build_default_registry(device_link=nobody, obsidian_vault_root=None)
    result = _dispatch(brain, tmp_path, "start_night_run", {})
    assert result.error == "device_not_connected"


def test_the_terminal_answers_the_night_routes_and_never_asks_the_brain(tmp_path: Path) -> None:
    """The companion's read and buttons act on this Mac's run; nothing travels."""
    fx = _Fixture(tmp_path / "terminal", at=_ms(23))
    try:
        with _rig(tmp_path / "ui", Device(night=fx.night)) as rig:
            idle = rig.get("/inherent/night")
            assert idle.status_code == 200
            assert idle.json()["night"] is None
            assert idle.json()["last"] is None

            shown = rig.post("/inherent/night", json={"action": "start", "hours": 1})
            assert shown.status_code == 200
            assert shown.json()["night"]["phase"] == "starting"
            assert fx.mac.held

            ended = rig.post("/inherent/night", json={"action": "end"})
            assert ended.json()["night"] is None
            assert rig.brain.seen == []
    finally:
        fx.close()


def test_a_terminal_without_a_run_leaves_the_route_to_the_brain(tmp_path: Path) -> None:
    """Unclassed means forwarded, whatever the brain answers (a real one has no run: 404)."""
    with _rig(tmp_path, Device()) as rig:
        rig.get("/inherent/night")
        assert [seen["path"] for seen in rig.brain.seen] == ["/inherent/night"]


def test_the_terminal_ticks_the_run_so_a_start_is_served(tmp_path: Path) -> None:
    """Before the loop picks the log up a start is unavailable; once it runs, it is served."""
    fx = _Fixture(tmp_path, at=_ms(23))

    async def scenario() -> tuple[str, str]:
        night = fx.controller(fx.mac)
        before = night.start(hours=1, until=None, source="conversation")["status"]
        task = _start_night(night, _Speech())
        await asyncio.sleep(0.3)
        after = night.start(hours=1, until=None, source="conversation")["status"]
        assert task is not None
        task.cancel()
        await asyncio.wait({task})
        return before, after

    try:
        assert asyncio.run(scenario()) == ("unavailable", "started")
    finally:
        fx.close()
