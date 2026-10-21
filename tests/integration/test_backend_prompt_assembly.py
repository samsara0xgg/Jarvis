"""The backend request after the 2026-09-21 prompt round: what the model receives.

Observables: the rendered blocks from memory.db and the ``messages`` /
``system`` handed to the LLM client for one turn.

- history starts at ``session.history_since``; earlier rows stay in the
  store (the ``since=""`` render still shows them, so the cutoff is what
  removes them, not their absence);
- history replays one message per record, the role read off the row's
  source, adjacent rows of one role joined into one message;
- an answer written with the retired ``<voice>``/``<document>`` envelope
  renders as its words;
- the profile is its own block, for the system prompt;
- the per-turn state rides at the head of this turn's user message under
  one header, so a request carries the history and exactly one more user
  message, and no ``[system context]`` note; a history ending on an
  unanswered user row folds into that message.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from jarvis.state.memory_db import open_memory_db, render_context
from tests.support.prompt_assembly import HISTORY, REPLY_LINE, STATUS, drive_one_turn

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


SINCE = "2026-09-14T00:00:00-07:00"
NOW = datetime.fromisoformat("2026-09-21T15:37:00-07:00")

ROWS = (
    ("old-1", "2026-09-12T07:18:51-07:00", "allen", "1001夜赶一下"),
    (
        "old-2",
        "2026-09-12T07:18:55-07:00",
        "jarvis",
        "<voice>\n旧的语音回答\n</voice>\n<document>\n旧文档\n</document>",
    ),
    ("new-1", "2026-09-15T02:50:02-07:00", "allen", "明天天气怎么样"),
    (
        "new-2",
        "2026-09-15T02:50:09-07:00",
        "jarvis",
        "<voice>\n明天多云。\n</voice>\n<document>\n最高 18 度。\n</document>",
    ),
    # An answer that copied the old history label into its own text.
    (
        "new-3",
        "2026-09-15T02:50:12-07:00",
        "jarvis_live",
        "[2026-09-15T02:50:10-07:00] jarvis: 记得带伞。",
    ),
    ("new-4", "2026-09-15T02:51:00-07:00", "allen", "好"),
    ("new-5", "2026-09-15T02:51:03-07:00", "allen", "还有呢"),
    ("new-6", "2026-09-21T15:00:00-07:00", "allen", "谢谢"),
)


def _memory_db(tmp_path: Path) -> Path:
    path = tmp_path / "memory.db"
    with open_memory_db(path) as conn, conn:
        conn.executemany(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)", ROWS,
        )
        conn.executemany(
            "INSERT INTO profile (id, ts, text) VALUES (?, ?, ?)",
            [("p1", SINCE, "用户叫 Allen。"), ("p2", SINCE, "默认用中文。")],
        )
    return path


def _flat(history: Sequence[dict[str, str]]) -> str:
    return "\n".join(turn["content"] for turn in history)


def test_history_replays_records_by_role_from_since(tmp_path: Path) -> None:
    """Rows before the cutoff stay out; roles follow the source; same-role rows join."""
    db = _memory_db(tmp_path)

    ctx = render_context(db, exclude_id="new-6", since=SINCE, now=NOW)

    # ADR 0201: each core-memory line carries its first-seen date and kind.
    assert ctx.profile == (
        "[About the user]\n### 经历\n- (2026-09-14, set) 用户叫 Allen。"
        "\n- (2026-09-14, set) 默认用中文。"
    )
    # ADR 0044: words only, no [ts] source: label; one date line opens a day.
    assert ctx.history == (
        {"role": "user", "content": "[9月15日 周二]\n明天天气怎么样"},
        {"role": "assistant", "content": "明天多云。\n最高 18 度。\n记得带伞。"},
        {"role": "user", "content": "好\n还有呢"},
    )
    assert ctx.now == "Time: 2026-09-21T15:37-07:00 Monday · 6 d 12 h since the last exchange"

    # The null surface: without the cutoff the same store shows the old rows,
    # and the next day's first user row gets its own date line.
    unbounded = render_context(db, exclude_id="new-6", since="", now=NOW).history
    assert unbounded[0]["content"] == "[9月12日 周六]\n1001夜赶一下"
    assert unbounded[2]["content"] == "[9月15日 周二]\n明天天气怎么样"
    assert "<voice>" not in _flat(unbounded)


def test_empty_store_renders_empty_profile_and_no_history(tmp_path: Path) -> None:
    """An empty profile is no block at all; an empty history is no message at all."""
    db = tmp_path / "memory.db"
    open_memory_db(db).close()

    ctx = render_context(db, exclude_id="", since=SINCE, now=NOW)

    assert ctx.profile == ""
    assert ctx.history == ()
    assert ctx.now == "Time: 2026-09-21T15:37-07:00 Monday"


def test_turn_request_is_history_by_role_then_one_user_message_with_status(
    tmp_path: Path,
) -> None:
    """One turn hands the model the history as turns, then one user message with the state."""
    system, messages = drive_one_turn(tmp_path, HISTORY)

    assert system.endswith("[About the user]\n- 用户叫 Allen。")
    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert messages[:2] == list(HISTORY)
    assert messages[2]["content"] == f"{STATUS}\n后天呢\n\n{REPLY_LINE}"
    assert not any("[system context]" in message["content"] for message in messages)


def test_connected_apps_ride_this_turns_state_after_the_history(tmp_path: Path) -> None:
    """The apps connected now sit in this turn's state, after any older answer about them."""
    stale = (
        {"role": "user", "content": "开灯"},
        {"role": "assistant", "content": "我没有控制灯的能力。"},
    )
    apps = "Connected apps: Philips Hue (Control your Hue lights)"

    _, messages = drive_one_turn(tmp_path, stale, connected_apps=apps)

    assert messages[:2] == list(stale)
    assert messages[2]["content"] == f"{STATUS}{apps}\n\n后天呢\n\n{REPLY_LINE}"


def test_history_ending_on_a_user_row_folds_into_this_turn(tmp_path: Path) -> None:
    """An unanswered user row joins this turn's message ahead of the state header."""
    unanswered = {"role": "user", "content": "[2026-09-21T15:30:00-07:00] allen: 还有呢"}

    _, messages = drive_one_turn(tmp_path, (*HISTORY, unanswered))

    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert messages[2]["content"] == f"{unanswered['content']}\n\n{STATUS}\n后天呢\n\n{REPLY_LINE}"


def test_live_context_ends_the_state_block_and_leaves_the_cached_prefix_alone(
    tmp_path: Path,
) -> None:
    """ADR 0148: a live line follows the apps and precedes the words; the prefix is unchanged."""
    apps = "Connected apps: Philips Hue (Control your Hue lights)"
    focus = 'Dashboard: Allen has this letter open: "Lunch" (Gmail id 19a).'
    plain_system, plain = drive_one_turn(tmp_path, HISTORY, connected_apps=apps)
    system, messages = drive_one_turn(
        tmp_path, HISTORY, connected_apps=apps, live_context=(focus, "Second line."),
    )

    assert system == plain_system
    assert messages[:2] == plain[:2] == list(HISTORY)
    assert (
        messages[2]["content"] == f"{STATUS}{apps}\n{focus}\nSecond line.\n\n后天呢\n\n{REPLY_LINE}"
    )
