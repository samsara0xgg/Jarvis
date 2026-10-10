"""ADR 0217: each turn says which device it came from, the devices there are, and the phone's place.

Facts in the state block, like the other live context; what she makes of them is hers. Built by the
real runtime from a real event log, real pairings and push registrations, and the brain's real
terminal table, through the function ``drive_turn`` calls.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import TYPE_CHECKING, Any

from jarvis.runtime import _whereabouts_lines, bootstrap_runtime_app
from jarvis.state.device_tokens import pair_device
from jarvis.state.event_log import MAC_NODE, emit_event
from jarvis.state.push_tokens import register_body

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

HOUR_MS = 3_600_000


def _runtime(root: Path, settings: str, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    root.mkdir()
    (root / "settings.yaml").write_text(settings, encoding="utf-8")
    monkeypatch.setenv("HOME", str(root.parent))
    return bootstrap_runtime_app(runtime_root=root)


def _turn(runtime: Any, turn_id: str, device: str) -> None:  # noqa: ANN401
    """Open ``turn_id`` with words typed on ``device`` (ADR 0212)."""
    emit_event(
        runtime.conn, type="surface.user_intent", ingestion_node=device,
        payload={"transcript": "where am I", "turn_id": turn_id, "channel": "cli_stdin"},
        correlation={"turn_id": turn_id},
    )


def _visit(runtime: Any, place: str | None, *, ago_ms: int, left: bool = False) -> None:  # noqa: ANN401
    """The phone reports a visit that began, or (``left``) ended, ``ago_ms`` ago."""
    at = int(time.time() * 1000) - ago_ms
    times = {"arrived_at_ms": at - HOUR_MS, "departed_at_ms": at} if left else {"arrived_at_ms": at}
    emit_event(
        runtime.conn, type="phone.visit_observed", ingestion_node="iphone",
        payload={"lat": 48.46, "lng": -123.31, "accuracy_m": 40.0, **times,
                 **({"place": place} if place else {})},
    )


def test_a_mac_running_alone_names_itself_and_a_paired_phone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its own turns came from "this Mac"; the phone's say so, with the phone in the list."""
    mac = _runtime(tmp_path / "a", "assistant_name: Jarvis\n", monkeypatch)
    try:
        assert _whereabouts_lines(mac, "T0")[0] in {
            "Where you run: on this Mac.", "Where you run: on this machine.",
        }
        pair_device(mac.runtime_paths.root, "iphone")
        register_body(mac.runtime_paths.root, "iphone", {
            "device_token": "a1" * 32, "environment": "sandbox",
        })
        _turn(mac, "T1", MAC_NODE)
        _turn(mac, "T2", "iphone")
        own, devices = _whereabouts_lines(mac, "T1")
        assert own == "This turn came from this Mac, where you run."
        assert devices.endswith(". His devices: iphone, his phone, not connected now.")
        assert _whereabouts_lines(mac, "T2")[0] == (
            "This turn came from iphone, his phone; your answer goes back to that device."
        )
    finally:
        mac.conn.close()


def test_a_brain_names_its_host_its_terminals_and_the_phone_with_its_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list follows the terminals and the phone's socket as they come and go."""
    brain = _runtime(tmp_path / "b", "runtime:\n  role: brain\n", monkeypatch)
    try:
        host = socket.gethostname()
        assert _whereabouts_lines(brain, "T0") == (f"Where you run: on the brain host {host}.",)
        for name in ("macbook", "iphone"):
            pair_device(brain.runtime_paths.root, name)
        _turn(brain, "T1", "macbook")
        _turn(brain, "T2", "iphone")
        _turn(brain, "T3", MAC_NODE)

        async def send(_frame: str) -> None:
            return None

        async def connect() -> None:
            brain.terminal_hub.attach("macbook", frozenset({"open_url"}), send, voice=True)

        asyncio.run(connect())
        brain.whereabouts.phone_open = lambda device: device == "iphone"
        origin, devices = _whereabouts_lines(brain, "T1")
        assert origin == (
            "This turn came from macbook, a computer; your answer goes back to that device."
        )
        assert devices == (
            f"Where you run: on the brain host {host}. His devices: macbook, a computer, "
            "connected (speaks); iphone, his phone, its conversation is open."
        )
        assert _whereabouts_lines(brain, "T2")[0] == (
            "This turn came from iphone, his phone; your answer goes back to that device."
        )
        # `mac` on a brain is the brain's own default row, not a device: no origin is claimed.
        assert _whereabouts_lines(brain, "T3") == (devices,)
    finally:
        brain.conn.close()


def test_the_phones_last_place_is_named_with_its_age_and_never_its_coordinates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arrival puts him there, a departure says he left, a nameless fix gives only its age."""
    brain = _runtime(tmp_path / "c", "runtime:\n  role: brain\n", monkeypatch)
    try:
        assert len(_whereabouts_lines(brain, "T0")) == 1  # no report: no place line
        _visit(brain, "Home", ago_ms=2 * HOUR_MS)
        assert _whereabouts_lines(brain, "T0")[-1] == "His phone last put him at Home, 2 h ago."
        _visit(brain, "UVic", ago_ms=12 * 60_000, left=True)
        assert _whereabouts_lines(brain, "T0")[-1] == (
            "His phone last reported him leaving UVic, 12 min ago."
        )
        _visit(brain, None, ago_ms=0)
        last = _whereabouts_lines(brain, "T0")[-1]
        assert last == "His phone's last position report, under a minute ago, has no place name."
        said = " ".join(_whereabouts_lines(brain, "T0"))
        assert "48.46" not in said
        assert "123.31" not in said
    finally:
        brain.conn.close()
