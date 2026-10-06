"""ADR 0170: ``python -m jarvis terminal`` — a device's end of a brain's device-bound tools.

A terminal is a small client, not a daemon: no port, no event log of its own, no model key. It
connects outward to the brain, declares the tools this machine can run, and runs the calls the
brain sends with the very handlers the one-machine daemon uses. Each call gets a throwaway
in-memory log (the handlers write their own observation to one), which is dropped with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.execution.path_resolver import resolve as resolve_file_entity
from jarvis.execution.tools import (
    TERMINAL_TOOL_NAMES,
    ActionLifecycle,
    ToolRegistry,
    build_default_registry,
    make_screen_capture,
)
from jarvis.runtime import (
    _DEFAULT_CONFIG_FILENAME,
    RuntimeBootstrapError,
    _install_open_path,
    _load_full_config,
    _locate_repo_root,
    _obsidian_vault_root,
    _screen_tools_config,
)
from jarvis.runtime.settings import apply_settings
from jarvis.shared import ActionRequest
from jarvis.state.event_log import open_event_log
from jarvis.surface.terminal_link import Execute, TerminalRefusedError, run_terminal_client

if TYPE_CHECKING:
    from collections.abc import Mapping

LOGGER = logging.getLogger("jarvis.runtime.terminal")

RESOLVE_FILE = "resolve_file"
"""Not a menu tool: the brain asks the terminal to turn a spoken file name into a path here."""


@dataclass(frozen=True)
class _ScratchPaths:
    """What the handlers read from ``RuntimePaths``: nowhere a terminal keeps anything."""

    event_log: Path
    artifacts_root: Path


def _declared(registry: ToolRegistry) -> frozenset[str]:
    """The tools this machine can run: the device tools it holds that a caller may reach."""
    held = {tool.name for tool in registry.get_definitions() if tool.allowed_callers}
    return frozenset(held & TERMINAL_TOOL_NAMES) | {RESOLVE_FILE}


def make_executor(registry: ToolRegistry) -> Execute:
    """The runner a terminal hands its link: one call in, ``ok`` + ``output`` or a failure out.

    Only declared tools run. The brain has already gated and, where it must, confirmed the
    call; nothing here asks again.
    """
    declared = _declared(registry)
    definitions = {tool.name: tool for tool in registry.get_definitions()}

    def execute(
        tool: str, arguments: Mapping[str, Any], target_entity_ref: str | None,
    ) -> dict[str, Any]:
        reply = run(tool, arguments, target_entity_ref)
        LOGGER.info("ran %s: %s", tool, "ok" if reply["ok"] else reply["code"])
        return reply

    def run(
        tool: str, arguments: Mapping[str, Any], target_entity_ref: str | None,
    ) -> dict[str, Any]:
        if tool not in declared:
            return {"ok": False, "code": "unknown_tool", "message": f"this terminal has no {tool}"}
        conn = open_event_log(Path(":memory:"))  # opened here: a log belongs to its thread
        try:
            if tool == RESOLVE_FILE:
                found = resolve_file_entity(str(arguments.get("query", "")), "file", conn)
                if found is None:
                    return {"ok": False, "code": "target_not_found", "message": "no file matched"}
                return {"ok": True, "output": {"path": str(found.path), "source": found.source}}
            definition = definitions[tool]
            action_id = f"terminal-{uuid.uuid4().hex}"
            lifecycle = ActionLifecycle()
            lifecycle.register(action_id)
            lifecycle.transition(action_id, "authorized")
            request = ActionRequest(
                action_id=action_id,
                tool_name=tool,
                target_entity_ref=target_entity_ref,
                caller_principal=min(definition.allowed_callers, key=lambda c: c.value),
                risk_level=definition.risk_level,
                arguments=dict(arguments),
                authorization_lease=None,
                run_id=None,
                turn_id=None,
            )
            scratch = _ScratchPaths(Path(":memory:"), Path(tempfile.gettempdir()))
            result = registry.dispatch(request, conn, scratch, lifecycle).slots[0]
        finally:
            conn.close()
        if result.error is None:
            return {"ok": True, "output": dict(result.payload)}
        try:
            message = str(json.loads(result.tool_output or "")["error"])
        except (ValueError, KeyError, TypeError):
            message = result.error
        return {"ok": False, "code": result.error, "message": message}

    return execute


def run_terminal(
    base_url: str,
    token: str,
    *,
    runtime_root: Path,
    config_path: Path | None = None,
) -> int:
    """Hold this device's link to the brain until interrupted; the exit code of the command.

    Reads only this machine's own config (the shipped YAML and the runtime root's
    ``settings.yaml``); it opens no database, no env file and no key.
    """
    if config_path is None:
        config_path = _locate_repo_root(Path(__file__).parent) / _DEFAULT_CONFIG_FILENAME
    try:
        config = apply_settings(
            _load_full_config(config_path, runtime_root / "settings.yaml"), runtime_root,
        )
        _install_open_path(config)
    except RuntimeBootstrapError as exc:
        sys.stderr.write(f"jarvis terminal: {exc}\n")
        return 1
    _vision_preset, max_width_px = _screen_tools_config(config)
    registry = build_default_registry(obsidian_vault_root=_obsidian_vault_root(config))
    registry.register(make_screen_capture(max_width_px))
    tools = _declared(registry)
    LOGGER.info("this terminal runs %s", sorted(tools))
    try:
        asyncio.run(
            run_terminal_client(base_url, token, tools=tools, execute=make_executor(registry)),
        )
    except TerminalRefusedError as exc:
        sys.stderr.write(f"jarvis terminal: {exc}\n")
        return 1
    except KeyboardInterrupt:
        LOGGER.info("terminal stopped")
    return 0
