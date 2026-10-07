"""ADR 0170: how a brain reads the activity that stays on the owner's device.

TimeSink's store and the watched git repositories are files of the device, so a brain holds
neither. Its readers (the timeline tool, the daily report, the work state) ask the connected
terminal instead: each ask is one hidden terminal operation, ``timesink_read`` or ``git_read``,
which the terminal answers by running the very reader function the one-machine daemon runs,
against its own store and repositories, and returning the JSON result. This module is the brain's
side of that ask; the terminal's side lives in ``jarvis.runtime.terminal``.

Layer rules: state may import shared; the link is the :class:`DeviceLink` seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.shared.device_link import DeviceCallError
from jarvis.state.daily_contract import DailyError

if TYPE_CHECKING:
    from jarvis.shared.device_link import DeviceLink

TIMESINK_READ: Final = "timesink_read"
GIT_READ: Final = "git_read"
DEVICE_READS: Final = frozenset({TIMESINK_READ, GIT_READ})
"""The operations a terminal declares for this: not menu tools, the model never sees them."""
CLAUDE_READ: Final = "claude_read"
"""The Agents page's reads of Claude Code's own state on the device (ADR 0046): its session
board, one session's conversation, and the reply typed into an idle background session. Declared
beside :data:`DEVICE_READS`, answered only where that machine's own config allows reading."""
TERMINAL_READS: Final = DEVICE_READS | {CLAUDE_READ}

NOT_CONNECTED: Final = (
    "Activity on the owner's device (app and screen data, git commits) is unavailable: the "
    "device is not connected to this brain right now."
)
_TRANSPORT: Final = frozenset({"device_not_connected", "device_disconnected", "device_timeout"})


class DeviceUnavailable(DailyError):  # noqa: N818 — a DailyError the readers may degrade on.
    """The terminal could not be reached: not connected, gone mid-call, or too slow.

    A reader that has a coverage convention records this as a gap; one that does not lets the
    tool answer with the message, which says plainly that the device is not there.
    """


def watched_repos(
    link: DeviceLink | None, configured: tuple[str, ...]
) -> tuple[tuple[str, ...], str | None]:
    """The repositories the readers consider watched, and why not when a brain cannot say.

    One machine: its own configured list. A brain: the terminal's, or none plus the plain
    sentence for the gap when the terminal is not there.
    """
    if link is None:
        return configured, None
    try:
        return tuple(ask(link, GIT_READ, "repos")), None
    except DeviceUnavailable as exc:
        return (), str(exc)


def ask(link: DeviceLink, op: str, fn: str, /, **args: Any) -> Any:  # noqa: ANN401 — the reader's JSON.
    """Run reader ``fn`` on the terminal and return what it returned.

    Raises:
        DeviceUnavailable: no terminal, a timeout, or the terminal dropped mid-call.
        DailyError: the reader itself refused (its own message and code, as it would locally).
    """
    try:
        payload = link(op, {"fn": fn, "args": args}, None)
    except DeviceCallError as exc:
        if exc.code in _TRANSPORT:
            message = NOT_CONNECTED if exc.code == "device_not_connected" else str(exc)
            raise DeviceUnavailable(message, exc.code) from exc
        raise DailyError(str(exc), exc.code) from exc
    return payload.get("result")
