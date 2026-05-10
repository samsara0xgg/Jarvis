"""Time tools — current time/date and countdown timers."""

import logging
import threading
from datetime import datetime
from typing import Any

from core.tool_result import FAILURE, SUCCESS, make_tool_result
from tools import jarvis_tool

LOGGER = logging.getLogger(__name__)

_timer_callback: Any = None
_active_timers: dict[str, threading.Timer] = {}


def init(tts_callback: Any = None) -> None:
    """Set TTS callback for timer announcements."""
    global _timer_callback
    _timer_callback = tts_callback


@jarvis_tool(read_only=True)
def get_current_time() -> str:
    """Get the current date and time."""
    now = datetime.now().astimezone()
    message = now.strftime("Current time: %Y-%m-%d %H:%M:%S (%A)")
    return make_tool_result(
        SUCCESS,
        message,
        data={
            "iso": now.isoformat(),
            "timezone": now.tzname(),
            "utc_offset": now.strftime("%z"),
        },
    )


@jarvis_tool(read_only=False)
def set_timer(seconds: int, label: str = "timer") -> str:
    """Set a countdown timer. When it fires, Jarvis will announce it."""
    if seconds <= 0:
        return make_tool_result(
            FAILURE,
            "Timer duration must be positive.",
            data={"seconds": seconds, "label": label},
            error_code="invalid_duration",
        )
    return _start_timer(seconds, label)


def _start_timer(seconds: int, label: str) -> str:
    """Create and start a daemon timer thread."""
    timer_id = f"{label}_{seconds}"

    def _on_fire() -> None:
        _active_timers.pop(timer_id, None)
        message = f"Timer '{label}' ({seconds} seconds) has finished!"
        LOGGER.info(message)
        if _timer_callback:
            try:
                _timer_callback(message)
            except Exception as exc:
                LOGGER.warning("Timer callback failed: %s", exc)

    if timer_id in _active_timers:
        _active_timers[timer_id].cancel()

    timer = threading.Timer(seconds, _on_fire)
    timer.daemon = True
    timer.start()
    _active_timers[timer_id] = timer

    if seconds >= 60:
        display = f"{seconds // 60} minutes {seconds % 60} seconds"
    else:
        display = f"{seconds} seconds"
    return make_tool_result(
        SUCCESS,
        f"Timer set: '{label}' for {display}.",
        data={"timer_id": timer_id, "seconds": seconds, "label": label},
    )


def cancel_all() -> None:
    """Cancel all active timers."""
    for timer in _active_timers.values():
        timer.cancel()
    _active_timers.clear()
