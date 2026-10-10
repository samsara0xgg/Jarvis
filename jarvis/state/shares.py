"""What Allen shared from another app and did not ask about (ADR 0211).

A share the share sheet marked "ask her" is an ordinary turn. Any other is one ``user.shared``
event, and every later turn reads the latest few as a single line of context, folded from the log
when the turn starts. Nothing is cached and nothing is listed anywhere else.

Layer rules: stdlib only (L2).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    import sqlite3

SHARE_EVENT: Final = "user.shared"
SHARES_LINE_PREFIX: Final = "Allen saved these from other apps"
"""The line's opening words; the runtime gives a line that starts with them its own length cap."""
SHARES_LINE_CHARS: Final = 900

MAX_SHARE_TEXT_CHARS: Final = 4_000
MAX_NOTE_CHARS: Final = 1_000
MAX_URL_CHARS: Final = 2_000
MAX_TITLE_CHARS: Final = 300

SHOWN: Final = 5
"""The latest this many shares go into the line."""
WINDOW_MS: Final = 3 * 86_400_000
"""Older shares stay in the log and leave the line."""
_ITEM_CHARS: Final = 170

_RECENT_SQL: Final = (
    "SELECT ts_epoch_ms, payload_json FROM events WHERE type = ? AND ts_epoch_ms >= ? "
    "ORDER BY id DESC LIMIT ?"
)


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _item(ts_ms: int, payload: dict[str, Any]) -> str:
    when = datetime.fromtimestamp(ts_ms / 1000).astimezone().strftime("%m-%d %H:%M")
    kind = str(payload.get("kind", ""))
    if kind == "link":
        title = _clip(str(payload.get("title") or ""), 60)
        what = f'link {_clip(str(payload.get("url") or ""), 90)}' + (f' "{title}"' if title else "")
    elif kind == "image":
        what = "a picture (saved, not looked at)"
    elif kind == "file":
        what = "a text file (saved, not read)"
    else:
        what = f'text "{_clip(str(payload.get("text") or ""), 100)}"'
    note = _clip(str(payload.get("note") or ""), 60)
    return _clip(f"{when} {what}" + (f' with the note "{note}"' if note else ""), _ITEM_CHARS)


def shares_line(conn: sqlite3.Connection, *, now_ms: int | None = None) -> str | None:
    """One line naming his latest saved shares, newest first, or ``None`` when there are none."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    rows = conn.execute(_RECENT_SQL, (SHARE_EVENT, now - WINDOW_MS, SHOWN)).fetchall()
    if not rows:
        return None
    items = "; ".join(_item(int(ts), json.loads(payload)) for ts, payload in rows)
    return (
        f"{SHARES_LINE_PREFIX} (newest first; material for if he asks, not a request to you): "
        f"{items}."
    )[:SHARES_LINE_CHARS]
