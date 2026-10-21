"""``GET /inherent/day``: the day's timeline as the phone and the desktop draw it (ADR 0199, 0302).

One day of one fixed world, folded by the real route from what a daemon holds: the phone's rows in
the event log (sleep, stays, a walk, steps), a reminder fired and one to come, Outlook through the
real mirror (a meeting and an all-day label), TimeSink's spans and calls, the talks, and a run of
the doing writer stored for the day, with a question in it. A second day with none of those stores
shows what a client reads when a source is missing.
"""

from __future__ import annotations

import functools
import json
import re
from contextlib import closing
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from jarvis.decision.cost_guard import CostRecorder
from jarvis.execution.tools import ToolError
from jarvis.runtime.doing_run import compose_run, read_brief, store_run
from jarvis.runtime.inherent_loop import _serve_day
from jarvis.runtime.things_mirror import ThingsMirror
from jarvis.state import things
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import append_day_draft, append_highlight_mark
from tests.support import day_line as rows
from tests.support.contracts import Contracts, brain_at, fixed_uuids
from tests.support.doing import SETTINGS, Writer, client, tile
from tests.support.timeline import peers_with

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import pytest

    from jarvis.state.day_line import DaySources

ZONE = ZoneInfo("America/Vancouver")
DAY = date(2026, 10, 14)
MIDNIGHT = datetime(2026, 10, 14, tzinfo=ZONE)
ORIGIN = int(MIDNIGHT.timestamp() * 1000)
NOW = datetime(2026, 10, 14, 14, 30, tzinfo=ZONE)
NOW_MS = int(NOW.timestamp() * 1000)
ROUTE = "GET /inherent/day"
MINUTE = 60_000
QUESTION = {
    "text": "What were you working on?", "options": ["The thesis", "Jarvis", "Something else"],
}


class _Microsoft:
    """The connected ``microsoft`` server, answering Graph with a meeting and an all-day event."""

    def call(self, server: str, tool: str, _args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "microsoft"
        answers: dict[str, list[dict[str, Any]]] = {
            "get-calendar-view": [
                {
                    "id": "AAMk-standup", "subject": "Standup", "isAllDay": False,
                    "isCancelled": False,
                    "start": {"dateTime": "2026-10-14T16:00:00.0000000", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-10-14T16:30:00.0000000", "timeZone": "UTC"},
                },
                {
                    "id": "AAMk-reading-week", "subject": "Reading week", "isAllDay": True,
                    "isCancelled": False,
                    "start": {"dateTime": "2026-10-12T00:00:00.0000000", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-10-17T00:00:00.0000000", "timeZone": "UTC"},
                },
            ],
            "list-todo-task-lists": [],
            "list-todo-tasks": [],
        }
        return {"text": json.dumps({"value": answers[tool]})}


class _Connections:
    def client_for(self, server: str) -> _Microsoft:
        if server != "microsoft":
            msg = f"mcp server {server!r} is not connected"
            raise ToolError(msg, code="mcp_server")
        return _Microsoft()


def _phone_rows(log: Path) -> None:
    """Sleep, three stays and a walk, the morning's steps, and two reminders: minutes from 00:00."""
    with closing(open_event_log(log)) as conn:
        rows.load_rows(
            conn,
            [
                rows.health("sleep-1", -30, 410, 90),
                rows.health("steps-1", 400, 800, 6200, "steps"),
                rows.visit("stay-home", 410, 500, "Home"),
                rows.motion("walk-1", "walking", 500, 515),
                rows.visit("stay-cafe", 515, 545, "Corner Cafe"),
                rows.visit("stay-library", 560, None, "UVic Library"),
                *rows.remind("reminder-1", 600),
                rows.fired("reminder-1", 600),
                *rows.remind("reminder-2", 930),
            ],
            ORIGIN,
        )
    mirror = ThingsMirror(cast("Any", _Connections()), log, ZONE)
    mirror.now = lambda: datetime.fromtimestamp(ORIGIN / 1000, UTC)
    mirror.sync()


def _what_he_did(prompt: str) -> str:
    """The doing writer's answer: unknown for the first stretch, a question on the second."""
    answer = json.loads(tile(prompt))
    first, second = answer["spans"][:2]
    first.update(kind="unknown", title=None, category="home", confidence="low")
    second["question"] = QUESTION
    cited = re.findall(r"^(c\d+) \|", prompt, re.MULTILINE)[:2]  # the clues the writer is shown
    answer["spans"][2]["clues"] = cited
    return json.dumps(answer)


def _the_day(tmp_path: Path) -> tuple[Path, DaySources]:
    """The log and the stores of the whole day, and the doing run stored for it."""
    tmp_path.mkdir()
    sources = rows.make_stores(
        tmp_path, ZONE, origin=ORIGIN,
        spans=(rows.Spun(550, 690, "Ghostty", "jarvis"), rows.Spun(690, 720, "Chrome")),
        calls=(rows.Call(780, 810),),
        talks=(rows.Said(555), rows.Said(556, "jarvis")),
    )
    log = tmp_path / "events.db"
    _phone_rows(log)
    brief = read_brief(DAY, sources, log, NOW)
    assert brief is not None
    with closing(open_event_log(tmp_path / "cost.db")) as cost:
        composed = compose_run(
            brief, SETTINGS, client(Writer(_what_he_did)), CostRecorder(cost, pricing_table={}),
        )
    assert composed.spans is not None, composed.why
    assert sources.memory_db is not None
    store_run(sources.memory_db, brief, composed)
    append_day_draft(
        sources.memory_db, day=DAY.isoformat(), model="m", input_chars=1, output_chars=1,
        line="A quiet morning, mostly at the library.",
        highlights=[{
            "id": "stay-library", "label": "The long stretch at the library",
            "start_ms": ORIGIN + 560 * MINUTE,
        }],
    )
    append_highlight_mark(
        sources.memory_db, day=DAY.isoformat(), item_id="stay-cafe", on=True,
        label="Best flat white", kind="stay", start_ms=ORIGIN + 515 * MINUTE,
    )
    return log, sources


def _served(root: Path, read: Any) -> dict[str, Any]:  # noqa: ANN401 - the route's day_read
    """``GET /inherent/day`` for the day, as a paired phone, at the world's clock."""
    root.mkdir()
    peers = peers_with(root, day_read=read)
    with brain_at(NOW_MS):
        answer = peers.paired.get(f"/inherent/day?date={DAY.isoformat()}")
    assert answer.status_code == 200, answer.text
    body: dict[str, Any] = answer.json()
    return body


def test_the_days_timeline_with_every_source_and_with_none_of_them(
    tmp_path: Path, contracts: Contracts, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items by lane, the spans of what he did and where, coverage, labels and missing sources."""
    fixed_uuids(monkeypatch, things)  # the ledger mints the id of every thing from Outlook
    with brain_at(NOW_MS):  # what is written with a timestamp is written at the world's clock
        log, sources = _the_day(tmp_path / "full")
    full = _served(
        tmp_path / "full-keys",
        functools.partial(_serve_day, log, ZONE, sources.memory_db, sources.timesink),
    )
    empty = tmp_path / "empty.db"
    open_event_log(empty).close()
    bare = _served(tmp_path / "bare-keys", functools.partial(_serve_day, empty, ZONE, None, None))

    contracts.output(
        "day/timeline",
        examples={"full": full, "bare": bare},
        exact=("date", "tz", "lane", "kind", "state", "basis", "confidence", "category"),
        now_ms=NOW_MS, route=ROUTE,
        description=(
            "One day from `date` (default today in the owner's zone `tz`) between `start_ms` and "
            "`end_ms`, as of `now_ms`. `items` are the facts, by start then id, each in a `lane` "
            "(you, her) with a `kind` and the fields of that kind, `state` done, now or next and "
            "`end_ms` null while it lasts; `labels` are the day's all-day events; `missing` names "
            "the sources that could not be read, whose items are not there. `spans` (ADR 0302) "
            "cut the day by what he was doing, where he was, what he used and his body, each "
            "with `title`, `category`, `basis`, `confidence`, `clues`, `question` (null, or "
            "`{id, text, options}` that `POST /inherent/day/answer` answers) and the `item_ids` "
            "it is made of; `coverage` says how much of his waking time they account for. "
            "`line` and `highlights` are the day's words to him and his marks; `summary` is the "
            "phone's steps and heart numbers (null when unreadable); `weather` is the day's "
            "card or null. A client keeps what it does not know: new kinds and fields come."
        ),
    )

    lanes = {span["lane"] for span in full["spans"]}
    assert lanes == {"body", "doing", "place", "use"}
    assert {item["kind"] for item in full["items"]} >= {
        "sleep", "stay", "move", "work", "talk", "reminder", "call", "event",
    }
    assert [label["title"] for label in full["labels"]] == ["Reading week"]
    assert full["missing"] == []
    assert [(h["id"], h["by"]) for h in full["highlights"]] == [
        ("stay-cafe", "him"), ("stay-library", "her"),
    ]
    assert full["line"]["text"].startswith("A quiet morning")
    assert any(span["question"] for span in full["spans"] if span["lane"] == "doing")
    assert full["summary"]["steps"] == 6200
    assert set(full["coverage"]) == {"waking_start_ms", "waking_end_ms", "waking_ms", "filled_ms"}
    assert bare["items"] == [] or "mac" in bare["missing"]
    assert bare["missing"], "an empty brain names the sources it has none of"
