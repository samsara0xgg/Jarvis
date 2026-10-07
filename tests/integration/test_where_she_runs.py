"""She is told which machine she runs on, and which terminals are connected (ADR 0170).

A fact in the state block, like the other live context; what she makes of it is hers.
"""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING, Any

from jarvis.runtime import _live_lines, bootstrap_runtime_app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _runtime(root: Path, settings: str, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    root.mkdir()
    (root / "settings.yaml").write_text(settings, encoding="utf-8")
    monkeypatch.setenv("HOME", str(root.parent))
    return bootstrap_runtime_app(runtime_root=root)


def _where(runtime: Any) -> list[str]:  # noqa: ANN401
    return [line for line in _live_lines(runtime.live_context) if line.startswith("Where you run")]


def test_a_brain_says_its_host_and_who_is_connected_and_all_says_this_mac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The brain's line follows the hub as terminals come and go; `all` names no hostname."""
    mac = _runtime(tmp_path / "a", "assistant_name: Jarvis\n", monkeypatch)
    brain = _runtime(tmp_path / "b", "runtime:\n  role: brain\n", monkeypatch)
    try:
        [line] = _where(mac)
        assert line in {"Where you run: on this Mac.", "Where you run: on this machine."}
        assert socket.gethostname() not in line

        host = socket.gethostname()
        assert _where(brain) == [
            f"Where you run: on the brain host {host}. Allen's devices are terminals "
            "connected to it; connected now: none.",
        ]

        async def send(_frame: str) -> None:
            return None

        async def connect() -> list[str]:
            hub = brain.terminal_hub
            hub.attach("macbook", frozenset({"open_url"}), send, voice=True)
            hub.attach("watch", frozenset(), send)
            return _where(brain)

        [now] = asyncio.run(connect())
        assert now.endswith(
            "connected now: macbook (speaks); watch (runs device tools only).",
        )
    finally:
        mac.conn.close()
        brain.conn.close()
