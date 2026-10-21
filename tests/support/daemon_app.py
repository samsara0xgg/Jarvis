"""The daemon's own app, booted on a throwaway runtime root and called in-process.

``serve_inherent`` wires the card, day and settings reads as closures over the booted runtime, so
the only way to reach the bodies a client really decodes is to boot it. This runs the real boot
with uvicorn stood in for (no socket, no port), hands a scenario the app through ASGI as the
local key on loopback or as a paired device off it, and lets the daemon shut down when the
scenario returns. No turn runs and no model is asked: the turn driver is replaced by one that
records the turns it was handed in :attr:`Daemon.turns`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from jarvis.runtime import inherent_loop
from jarvis.runtime.daemon import intent_pump
from jarvis.state.device_tokens import pair_device
from jarvis.state.plugin_settings import local_key
from tests.support.runtime_config import boot_runtime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import pytest

HOST = "http://127.0.0.1"
LOOPBACK = ("127.0.0.1", 50000)
TAILNET = ("100.64.0.7", 50123)
"""Where a paired device calls from: an address that is not on loopback."""

_SETTINGS = "runtime:\n  role: brain\n  listen_addresses: ['127.0.0.1']\n"
"""A brain that also listens on a private address, so it admits a paired device's token."""


@dataclass
class Daemon:
    """The booted daemon a scenario talks to."""

    runtime: Any
    app: Any
    turns: list[dict[str, Any]]
    """The payloads of the turns the daemon was asked to run; none of them is run."""

    @property
    def log(self) -> Path:
        """The event log the daemon reads its cards from."""
        path: Path = self.runtime.runtime_paths.event_log
        return path

    def host(self) -> httpx.AsyncClient:
        """This machine's own UI: the local key, on loopback."""
        key = local_key(self.runtime.runtime_paths.root)
        return self._client(LOOPBACK, key)

    def device(self, name: str) -> httpx.AsyncClient:
        """A paired device off loopback: its token is made here, under the daemon's root."""
        token = pair_device(self.runtime.runtime_paths.root, name)
        return self._client(TAILNET, token)

    def _client(self, peer: tuple[str, int], bearer: str) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, client=peer)
        return httpx.AsyncClient(
            transport=transport, base_url=HOST, headers={"Authorization": f"Bearer {bearer}"},
        )


def run_in_daemon[T](
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: Callable[[Daemon], Awaitable[T]],
    *, settings: str = _SETTINGS,
) -> T:
    """Boot the daemon over ``tmp_path``, run ``scenario`` against its app, and shut it down.

    ``settings`` is the runtime's ``settings.yaml``. The scenario's answer is returned; what it
    raises is raised.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text(settings, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_PLUGIN_CATALOG", str(tmp_path / "no-catalog"))
    runtime = boot_runtime(root)
    answers: list[T] = []
    turns: list[dict[str, Any]] = []

    def record_turn(*_args: object, user_intent_event: Any, **_kwargs: object) -> None:  # noqa: ANN401
        turns.append(dict(user_intent_event.payload))

    class _Server:
        """Stands in for ``uvicorn.Server``: ``serve`` is the scenario."""

        def __init__(self, config: Any) -> None:  # noqa: ANN401 - a ``uvicorn.Config``
            self.app = config.app

        async def serve(self, sockets: object = None) -> None:  # noqa: ARG002
            answers.append(await scenario(Daemon(runtime, self.app, turns)))

    monkeypatch.setattr(inherent_loop, "_Server", _Server)
    monkeypatch.setattr(intent_pump, "drive_turn", record_turn)
    try:
        asyncio.run(inherent_loop.serve_inherent(runtime, port=0))
    finally:
        runtime.conn.close()
    return answers[0]
