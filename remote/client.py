"""WebSocket client for communicating with a remote agent."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets

from remote.protocol import MSG_AUTH_OK, MSG_PONG, MSG_RESULT, make_auth, make_command

LOGGER = logging.getLogger(__name__)


class RemoteClient:
    """WebSocket client for communicating with a remote agent.

    Designed to be used from both async and sync contexts.  Sync callers
    should use :pymethod:`send_command_sync`.
    """

    def __init__(self, config: dict) -> None:
        remote_cfg = config.get("remote", {})
        self.host: str = remote_cfg.get("agent_host", remote_cfg.get("host", "localhost"))
        self.port: int = int(remote_cfg.get("agent_port", remote_cfg.get("port", 8765)))
        self.token: str = remote_cfg.get("token", "")
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._lock = asyncio.Lock()
        self._connected = False
        self.logger = LOGGER

    @property
    def uri(self) -> str:
        return f"ws://{self.host}:{self.port}"

    async def connect(self) -> bool:
        """Connect and authenticate with the remote agent.

        Returns True on success, False on failure.
        """
        try:
            self._ws = await websockets.connect(self.uri)
            # Send auth
            await self._ws.send(json.dumps(make_auth(self.token)))
            raw = await asyncio.wait_for(self._ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get("type") == MSG_AUTH_OK:
                self._connected = True
                self.logger.info("Connected and authenticated to %s", self.uri)
                return True
            else:
                self.logger.warning("Auth failed: %s", msg)
                await self._ws.close()
                self._ws = None
                self._connected = False
                return False
        except Exception as exc:
            self.logger.error("Connection failed: %s", exc)
            self._ws = None
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Close the connection."""
        self._connected = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def send_command(self, action: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        """Send a command and wait for the result.

        Auto-reconnects once if the connection was lost.

        Args:
            action: The action to execute (must be in ACTIONS).
            params: Optional parameters for the action.
            timeout: Max seconds to wait for a response.

        Returns:
            The result dict from the agent.
        """
        # Auto-reconnect if needed
        if not self.is_connected():
            ok = await self.connect()
            if not ok:
                return {"success": False, "error": "Failed to connect to remote agent."}

        cmd = make_command(action, params)
        request_id = cmd["request_id"]

        async with self._lock:
            try:
                await self._ws.send(json.dumps(cmd))

                # Wait for the matching result
                deadline = asyncio.get_event_loop().time() + timeout
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        return {"success": False, "error": "Command timed out."}

                    raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                    msg = json.loads(raw)

                    # Skip pong messages
                    if msg.get("type") == MSG_PONG:
                        continue

                    if msg.get("type") == MSG_RESULT and msg.get("request_id") == request_id:
                        return msg

            except asyncio.TimeoutError:
                return {"success": False, "error": "Command timed out."}
            except websockets.ConnectionClosed:
                self._connected = False
                self._ws = None
                return {"success": False, "error": "Connection lost during command."}
            except Exception as exc:
                return {"success": False, "error": f"Command failed: {exc}"}

    def send_command_sync(self, action: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        """Synchronous wrapper around send_command.

        Safe to call from non-async code. Creates or reuses an event loop.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're inside an existing event loop — use a new thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self.send_command(action, params, timeout))
                return future.result(timeout=timeout + 5)
        else:
            return asyncio.run(self.send_command(action, params, timeout))

    def is_connected(self) -> bool:
        """Check if connected to agent."""
        return self._connected and self._ws is not None and self._ws.open
