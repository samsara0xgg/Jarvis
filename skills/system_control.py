"""System control skill — macOS volume, app launch, system info."""

from __future__ import annotations

import logging
import platform
import shlex
import subprocess
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)

# Allowlist of safe command base names
_ALLOWED_COMMANDS = {
    "ls", "cat", "pwd", "whoami", "df", "ps", "top", "uptime",
    "sw_vers", "date", "hostname", "which", "echo", "wc",
    "head", "tail", "du", "uname", "free", "env", "printenv",
}


class SystemControlSkill(Skill):
    """macOS system control: volume, app launch, system info.

    Restricted to owner role for safety.
    """

    def __init__(self, config: dict) -> None:
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "system_control"

    def get_required_role(self) -> str:
        return "owner"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "set_system_volume",
                "description": "Set the macOS system volume (0-100).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "percent": {
                            "type": "integer",
                            "description": "Volume level 0-100.",
                        },
                    },
                    "required": ["percent"],
                },
            },
            {
                "name": "open_application",
                "description": "Open a macOS application by name.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "app_name": {
                            "type": "string",
                            "description": "Application name, e.g. 'Safari', 'Terminal', 'Spotify'.",
                        },
                    },
                    "required": ["app_name"],
                },
            },
            {
                "name": "get_system_info",
                "description": "Get basic system information: OS, hostname, uptime, CPU, memory.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "run_shell_command",
                "description": (
                    "Run a shell command and return its output. "
                    "Use with caution — only for safe, read-only commands."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to execute.",
                        },
                    },
                    "required": ["command"],
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        if tool_name == "set_system_volume":
            return self._set_volume(tool_input)
        if tool_name == "open_application":
            return self._open_app(tool_input)
        if tool_name == "get_system_info":
            return self._system_info()
        if tool_name == "run_shell_command":
            return self._run_command(tool_input)
        return f"Unknown system tool: {tool_name}"

    def _set_volume(self, tool_input: dict[str, Any]) -> str:
        percent = max(0, min(100, int(tool_input.get("percent", 50))))
        # macOS volume is 0-7 in osascript, map 0-100 to 0-100 output volume
        try:
            subprocess.run(
                ["osascript", "-e", f"set volume output volume {percent}"],
                check=True,
                capture_output=True,
                timeout=5,
            )
            return f"System volume set to {percent}%."
        except subprocess.CalledProcessError as exc:
            return f"Failed to set volume: {exc}"
        except FileNotFoundError:
            return "Volume control is only available on macOS."

    def _open_app(self, tool_input: dict[str, Any]) -> str:
        app_name = str(tool_input.get("app_name", "")).strip()
        if not app_name:
            return "Application name is required."
        try:
            subprocess.run(
                ["open", "-a", app_name],
                check=True,
                capture_output=True,
                timeout=10,
            )
            return f"Opened {app_name}."
        except subprocess.CalledProcessError:
            return f"Failed to open '{app_name}'. Make sure the app name is correct."
        except FileNotFoundError:
            return "App launching is only available on macOS."

    def _system_info(self) -> str:
        info_parts = [
            f"OS: {platform.system()} {platform.release()}",
            f"Machine: {platform.machine()}",
            f"Hostname: {platform.node()}",
            f"Python: {platform.python_version()}",
        ]
        try:
            uptime = subprocess.run(
                ["uptime"], capture_output=True, text=True, timeout=5,
            )
            if uptime.returncode == 0:
                info_parts.append(f"Uptime: {uptime.stdout.strip()}")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "\n".join(info_parts)

    def _run_command(self, tool_input: dict[str, Any]) -> str:
        command = str(tool_input.get("command", "")).strip()
        if not command:
            return "Command cannot be empty."

        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return f"Invalid command syntax: {exc}"

        if not argv:
            return "Command cannot be empty."

        base_cmd = argv[0]
        if base_cmd not in _ALLOWED_COMMANDS:
            return f"Command '{base_cmd}' is not allowed. Allowed: {sorted(_ALLOWED_COMMANDS)}"

        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout.strip()
            if result.returncode != 0 and result.stderr.strip():
                output += f"\nSTDERR: {result.stderr.strip()}"
            return output[:2000] if output else f"Command completed (exit code {result.returncode})."
        except subprocess.TimeoutExpired:
            return "Command timed out after 30 seconds."
        except Exception as exc:
            return f"Command failed: {exc}"
