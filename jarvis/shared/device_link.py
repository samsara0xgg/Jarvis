"""ADR 0170: the seam between a brain's device-bound tools and the terminal that runs them.

The brain's menu keeps every tool that acts on a device (open, clipboard, files, screen). Each
one's handler hands its call to a :data:`DeviceLink`, which the runtime binds to whichever
terminal is connected. Layer rules: stdlib only, so execution (the proxies) and surface (the
link) meet here without importing each other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class DeviceCallError(Exception):
    """A device-bound call that did not produce a result: no terminal, a timeout, a failure.

    ``code`` is the short tag the tool result carries; ``str(exc)`` is the plain sentence the
    model relays to the owner.
    """

    def __init__(self, message: str, *, code: str = "device_not_connected") -> None:
        """Keep ``code`` beside the message."""
        super().__init__(message)
        self.code = code


type DeviceLink = Callable[[str, Mapping[str, Any], str | None], dict[str, Any]]
"""``(tool name, arguments, resolved target) -> payload``; raises :class:`DeviceCallError`."""
