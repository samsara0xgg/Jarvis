"""The daemon's uvicorn server: the app it serves, its stop signal, and the sockets of a brain."""

from __future__ import annotations

import contextlib
import functools
import signal
import socket
import threading
from typing import TYPE_CHECKING

import uvicorn

from jarvis.state.device_tokens import device_token_matches
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_server import create_app, require_local_key

if TYPE_CHECKING:
    from collections.abc import Generator

    from jarvis.runtime import JarvisRuntime
    from jarvis.surface.inherent_server import InherentDeps


class Server(uvicorn.Server):
    """uvicorn whose stop signal ends ``serve`` and leaves the process to ``serve_inherent``.

    uvicorn re-raises the signal once it has shut down: SIGTERM then kills the process and
    SIGINT cancels the main task, both before the daemon's teardown has run.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        """Take SIGINT and SIGTERM for ``handle_exit`` while serving, and give them back."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        stops = (signal.SIGINT, signal.SIGTERM)
        before = {sig: signal.signal(sig, self.handle_exit) for sig in stops}
        try:
            yield
        finally:
            for sig, handler in before.items():
                signal.signal(sig, handler)


def build_server(runtime: JarvisRuntime, deps: InherentDeps, *, host: str, port: int) -> Server:
    """The Inherent app over ``deps``, behind the local key, as a server not yet serving.

    ``lifespan="off"`` because ``serve_inherent`` owns the lifecycle, ``log_level="warning"``
    so uvicorn's per-request noise does not drown the watchers' logs.
    """
    app = create_app(deps)
    # The desktop, the CLI and the Claude Code / Codex hooks read this key
    # from the runtime root; nothing else on the machine can call the daemon.
    require_local_key(
        app,
        functools.partial(local_key_matches, local_key(runtime.runtime_paths.root)),
        extra_hosts=runtime.config.listen_hosts,
        device_token_matches=(
            functools.partial(device_token_matches, runtime.runtime_paths.root)
            if runtime.config.listen_addresses
            else None
        ),
        open_claim=bool(runtime.config.listen_addresses),
    )
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",
        lifespan="off",
    )
    return Server(config)


def listen_sockets(host: str, port: int, addresses: tuple[str, ...]) -> list[socket.socket]:
    """Listening sockets for ``host`` and each private address (ADR 0170).

    uvicorn binds one host, so a brain with more addresses hands it the sockets.
    """
    sockets: list[socket.socket] = []
    for address in (host, *addresses):
        try:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockets.append(socket.create_server((address, port), family=family, backlog=2048))
        except OSError as exc:
            for sock in sockets:
                sock.close()
            msg = f"cannot listen on {address}:{port}: {exc}"
            raise OSError(msg) from exc
    return sockets
