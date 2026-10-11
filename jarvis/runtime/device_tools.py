"""ADR 0225: a brain's menu of device tools follows what its paired terminals declare.

Every terminal says in its ``hello`` which tools and operations it runs. The brain keeps the
latest declaration of each paired device (:mod:`jarvis.state.terminal_tools`) and offers, on the
registry's menu, the device tools those declarations need: from the file at boot, before any
terminal connects, and again on each hello. A device that is away keeps its tools on the menu.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jarvis.state import device_tokens, terminal_tools

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.execution.tools import ToolRegistry
    from jarvis.surface.terminal_link import TerminalHub

LOGGER = logging.getLogger("jarvis.runtime.device_tools")


def offer_remembered(root: Path, registry: ToolRegistry) -> None:
    """Offer the device tools the still-paired devices' remembered declarations need."""
    try:
        paired = [name for name, _ in device_tokens.paired_devices(root)]
    except (OSError, ValueError):
        LOGGER.warning("devices.json is unreadable; no device tool is offered")
        paired = []
    registry.offer_device_tools(terminal_tools.declared(root, paired))


def follow_terminals(hub: TerminalHub | None, root: Path, registry: ToolRegistry) -> None:
    """Offer the remembered tools now, and re-offer on each hello after remembering its device.

    One machine has no hub and no device catalogue: nothing to follow.
    """
    if hub is None:
        return
    offer_remembered(root, registry)

    def on_hello(device: str, tools: frozenset[str]) -> None:
        terminal_tools.remember(root, device, tools)
        offer_remembered(root, registry)

    hub.on_hello = on_hello
