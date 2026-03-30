"""Remote control agent — runs on the computer to be controlled.

Usage:
    python -m remote.agent --port 8765

Generates a one-time token on startup that Jarvis must use to authenticate.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import platform
import secrets
import subprocess
import tempfile
from typing import Any

import websockets
from websockets.server import serve

from remote.protocol import (
    ACTIONS,
    MSG_AUTH,
    MSG_AUTH_FAIL,
    MSG_AUTH_OK,
    MSG_COMMAND,
    MSG_PING,
    MSG_PONG,
    make_result,
)

LOGGER = logging.getLogger(__name__)

# Commands allowed for run_command action (prefix-matched).
ALLOWED_COMMANDS = [
    "ls",
    "cat",
    "pwd",
    "whoami",
    "df",
    "ps",
    "top -l 1",
    "uptime",
    "sw_vers",
    "system_profiler SPHardwareDataType",
    "sysctl -n hw.memsize",
    "sysctl -n hw.ncpu",
    "date",
    "hostname",
    "which",
    "echo",
    "wc",
    "head",
    "tail",
    "du -sh",
    "free",
    "uname",
]


class RemoteAgent:
    """WebSocket server that executes commands on the local machine."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self.token = secrets.token_urlsafe(32)
        self.logger = LOGGER
        self._authenticated: set[websockets.WebSocketServerProtocol] = set()

    async def start(self) -> None:
        """Start the WebSocket server and block until cancelled."""
        self.logger.info("Starting remote agent on %s:%s", self.host, self.port)
        print(f"\n{'=' * 50}")
        print(f"  Remote Agent running on ws://{self.host}:{self.port}")
        print(f"  Auth token: {self.token}")
        print(f"{'=' * 50}\n")
        async with serve(self.handle_connection, self.host, self.port):
            await asyncio.Future()  # run forever

    async def handle_connection(self, websocket: websockets.WebSocketServerProtocol) -> None:
        """Handle a single client connection."""
        peer = websocket.remote_address
        self.logger.info("New connection from %s", peer)
        try:
            # First message must be auth
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") != MSG_AUTH or msg.get("token") != self.token:
                await websocket.send(json.dumps({"type": MSG_AUTH_FAIL}))
                self.logger.warning("Auth failed from %s", peer)
                await websocket.close(1008, "Authentication failed")
                return

            self._authenticated.add(websocket)
            await websocket.send(json.dumps({"type": MSG_AUTH_OK}))
            self.logger.info("Client %s authenticated", peer)

            # Process commands
            async for raw_msg in websocket:
                try:
                    msg = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type")

                if msg_type == MSG_PING:
                    await websocket.send(json.dumps({"type": MSG_PONG}))
                    continue

                if msg_type == MSG_COMMAND:
                    action = msg.get("action", "")
                    params = msg.get("params", {})
                    request_id = msg.get("request_id", "")
                    result = await self.handle_command(action, params)
                    if isinstance(result, dict):
                        resp = make_result(request_id, True, data=result)
                    else:
                        resp = make_result(request_id, True, data=str(result))
                    await websocket.send(json.dumps(resp))
        except asyncio.TimeoutError:
            self.logger.warning("Auth timeout from %s", peer)
            await websocket.close(1008, "Auth timeout")
        except websockets.ConnectionClosed:
            self.logger.info("Connection closed from %s", peer)
        except Exception as exc:
            self.logger.exception("Error handling connection from %s: %s", peer, exc)
        finally:
            self._authenticated.discard(websocket)

    async def handle_command(self, action: str, params: dict) -> Any:
        """Dispatch and execute a command.

        Returns the result data (str, dict, or list) on success.
        Raises ValueError for unknown actions.
        """
        if action not in ACTIONS:
            raise ValueError(f"Unknown action: {action}")

        handler_map = {
            "open_app": lambda: self._open_app(params),
            "close_app": lambda: self._close_app(params),
            "set_volume": lambda: self._set_volume(params),
            "get_volume": lambda: self._get_volume(),
            "screenshot": lambda: self._screenshot(params),
            "lock_screen": lambda: self._lock_screen(),
            "system_info": lambda: self._system_info(),
            "run_command": lambda: self._run_command(params),
            "media_control": lambda: self._media_control(params),
            "open_url": lambda: self._open_url(params),
            "type_text": lambda: self._type_text(params),
            "get_active_window": lambda: self._get_active_window(),
            "list_running_apps": lambda: self._list_running_apps(),
        }

        handler = handler_map.get(action)
        if handler is None:
            raise ValueError(f"No handler for action: {action}")

        try:
            return await handler()
        except Exception as exc:
            self.logger.exception("Command %s failed: %s", action, exc)
            return f"Error: {exc}"

    async def _open_app(self, params: dict) -> str:
        app_name = params.get("app_name", "").strip()
        if not app_name:
            return "Error: app_name is required."
        proc = await asyncio.to_thread(
            subprocess.run,
            ["open", "-a", app_name],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return f"Opened {app_name}."
        return f"Failed to open {app_name}: {proc.stderr.strip()}"

    async def _close_app(self, params: dict) -> str:
        app_name = params.get("app_name", "").strip()
        if not app_name:
            return "Error: app_name is required."
        script = f'tell application "{app_name}" to quit'
        proc = await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return f"Closed {app_name}."
        return f"Failed to close {app_name}: {proc.stderr.strip()}"

    async def _set_volume(self, params: dict) -> str:
        level = max(0, min(100, int(params.get("level", 50))))
        proc = await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", f"set volume output volume {level}"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return f"Volume set to {level}%."
        return f"Failed to set volume: {proc.stderr.strip()}"

    async def _get_volume(self) -> str:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return f"Current volume: {proc.stdout.strip()}%"
        return f"Failed to get volume: {proc.stderr.strip()}"

    async def _screenshot(self, params: dict) -> str:
        region = params.get("region")  # optional: "x,y,w,h"
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            cmd = ["screencapture", "-x"]  # -x = no sound
            if region:
                cmd.extend(["-R", region])
            cmd.append(tmp_path)

            proc = await asyncio.to_thread(
                subprocess.run, cmd,
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                return f"Screenshot failed: {proc.stderr.strip()}"

            with open(tmp_path, "rb") as f:
                data = f.read()

            # Cap at 5 MB to keep WebSocket messages reasonable
            if len(data) > 5 * 1024 * 1024:
                return "Error: screenshot too large (>5 MB). Try capturing a smaller region."

            encoded = base64.b64encode(data).decode("ascii")
            return encoded
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    async def _lock_screen(self) -> str:
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                "osascript", "-e",
                'tell application "System Events" to keystroke "q" using {command down, control down}',
            ],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return "Screen locked."
        return f"Failed to lock screen: {proc.stderr.strip()}"

    async def _system_info(self) -> dict:
        info: dict[str, str] = {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "hostname": platform.node(),
            "python": platform.python_version(),
        }
        # Uptime
        try:
            proc = await asyncio.to_thread(
                subprocess.run, ["uptime"],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0:
                info["uptime"] = proc.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # CPU count
        info["cpu_count"] = str(os.cpu_count() or "unknown")
        return info

    async def _run_command(self, params: dict) -> str:
        command = params.get("command", "").strip()
        if not command:
            return "Error: command is required."

        # Split into argv for safe execution (no shell parsing)
        import shlex
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return f"Error: invalid command syntax: {exc}"

        if not argv:
            return "Error: empty command."

        # Check base command against allowlist
        base_cmd = argv[0]
        allowed = False
        for prefix in ALLOWED_COMMANDS:
            # Match exact command name (first word of allowlist entry)
            allowed_base = prefix.split()[0]
            if base_cmd == allowed_base:
                allowed = True
                break
        if not allowed:
            return f"Error: '{base_cmd}' not in allowlist. Allowed: {[c.split()[0] for c in ALLOWED_COMMANDS]}"

        proc = await asyncio.to_thread(
            subprocess.run,
            argv,
            capture_output=True, text=True, timeout=10,
        )
        output = proc.stdout.strip()
        if proc.returncode != 0 and proc.stderr.strip():
            output += f"\nSTDERR: {proc.stderr.strip()}"
        return output[:4000] if output else f"Command completed (exit code {proc.returncode})."

    async def _media_control(self, params: dict) -> str:
        action = params.get("action", "").strip().lower()
        valid_actions = {"play", "pause", "next", "previous", "playpause"}
        if action not in valid_actions:
            return f"Error: action must be one of {sorted(valid_actions)}."

        # Map to Media key events via osascript
        key_map = {
            "play": "play",
            "pause": "pause",
            "playpause": "playpause",
            "next": "next track",
            "previous": "previous track",
        }
        key_action = key_map[action]

        # Use System Events to send media key
        script = f"""
        tell application "System Events"
            key code {_media_key_code(action)}
        end tell
        """
        # Simpler approach: tell Music/Spotify directly if running, else use NowPlaying
        # We'll use a generic approach via osascript key codes
        # macOS media key codes: play/pause=16 (with fn), next=17, prev=18
        # Actually, let's use the simpler approach of telling the active player
        script = f"""
        tell application "System Events"
            set activeApp to name of first application process whose frontmost is true
        end tell
        try
            tell application "Music" to {key_action}
        on error
            try
                tell application "Spotify" to {key_action}
            end try
        end try
        """
        proc = await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return f"Media: {action} executed."
        return f"Media control failed: {proc.stderr.strip()}"

    async def _open_url(self, params: dict) -> str:
        url = params.get("url", "").strip()
        if not url:
            return "Error: url is required."
        proc = await asyncio.to_thread(
            subprocess.run,
            ["open", url],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return f"Opened URL: {url}"
        return f"Failed to open URL: {proc.stderr.strip()}"

    async def _type_text(self, params: dict) -> str:
        text = params.get("text", "")
        if not text:
            return "Error: text is required."
        # Escape double quotes and backslashes for AppleScript
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "System Events" to keystroke "{escaped}"'
        proc = await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return f"Typed {len(text)} characters."
        return f"Failed to type text: {proc.stderr.strip()}"

    async def _get_active_window(self) -> str:
        script = """
        tell application "System Events"
            set frontApp to name of first application process whose frontmost is true
            try
                tell process frontApp
                    set winTitle to name of front window
                end tell
                return frontApp & " - " & winTitle
            on error
                return frontApp
            end try
        end tell
        """
        proc = await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
        return f"Failed to get active window: {proc.stderr.strip()}"

    async def _list_running_apps(self) -> list:
        script = """
        tell application "System Events"
            set appNames to name of every application process whose background only is false
            set AppleScript's text item delimiters to "||"
            return appNames as text
        end tell
        """
        proc = await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            raw = proc.stdout.strip()
            return [a.strip() for a in raw.split("||") if a.strip()]
        return [f"Error: {proc.stderr.strip()}"]


def _media_key_code(action: str) -> int:
    """Return macOS virtual key code for media keys (unused in final impl but kept for reference)."""
    return {"play": 16, "pause": 16, "playpause": 16, "next": 17, "previous": 18}.get(action, 16)


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis Remote Control Agent")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    agent = RemoteAgent(host=args.host, port=args.port)
    try:
        asyncio.run(agent.start())
    except KeyboardInterrupt:
        print("\nAgent stopped.")


if __name__ == "__main__":
    main()
