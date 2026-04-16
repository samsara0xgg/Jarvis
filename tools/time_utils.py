"""Time tools — current time/date and countdown timers."""

import logging
import threading
from datetime import datetime
from typing import Any

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
    return datetime.now().strftime("Current time: %Y-%m-%d %H:%M:%S (%A)")


@jarvis_tool(read_only=False)
def set_timer(seconds: int, label: str = "timer") -> str:
    """Set a countdown timer. When it fires, Jarvis will announce it."""
    if seconds <= 0:
        return "Timer duration must be positive."
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
    return f"Timer set: '{label}' for {display}."


def cancel_all() -> None:
    """Cancel all active timers."""
    for timer in _active_timers.values():
        timer.cancel()
    _active_timers.clear()
