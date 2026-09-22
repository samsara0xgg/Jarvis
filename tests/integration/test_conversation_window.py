"""The Resonance conversation window reads the conversation of record (spec §18.3).

Observables: the rows ``conversation_rows`` returns over a seeded memory.db,
and what ``GET /inherent/conversation`` answers over the same store.

- rows come oldest first with a ``seq`` cursor; ``after=0`` is the newest
  page, ``after=<seq>`` only what landed past it;
- the history floor (``session.history_since``) applies, so the window and
  the backend's prompt start at the same row;
- a retired ``<voice>``/``<document>`` envelope renders as its words.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi.testclient import TestClient

from jarvis.state.memory_db import conversation_rows, open_memory_db
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from pathlib import Path

SINCE = "2026-09-14T00:00:00-07:00"

ROWS = (
    ("old-1", "2026-09-12T07:18:51-07:00", "allen", "1001夜赶一下"),
    ("new-1", "2026-09-15T02:50:02-07:00", "allen", "明天天气怎么样"),
    (
        "new-2",
        "2026-09-15T02:50:09-07:00",
        "jarvis",
        "<voice>\n明天多云。\n</voice>\n<document>\n最高 18 度。\n</document>",
    ),
    ("live:s1:jarvis_live:1", "2026-09-15T02:50:12-07:00", "jarvis_live", "记得带伞。"),
)


def _memory_db(tmp_path: Path) -> Path:
    path = tmp_path / "memory.db"
    with open_memory_db(path) as conn, conn:
        conn.executemany(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)", ROWS,
        )
    return path


def test_rows_follow_the_cursor_and_the_history_floor(tmp_path: Path) -> None:
    """Newest page first, then only the rows past the cursor; the floor hides older rows."""
    db = _memory_db(tmp_path)

    page = conversation_rows(db, since=SINCE)

    assert [row["id"] for row in page] == ["new-1", "new-2", "live:s1:jarvis_live:1"]
    assert [row["seq"] for row in page] == [2, 3, 4]
    assert page[1]["text"] == "明天多云。\n最高 18 度。", "envelope tags reached the window"
    assert page[2]["source"] == "jarvis_live"
    assert [row["id"] for row in conversation_rows(db, since=SINCE, after=3)] == [
        "live:s1:jarvis_live:1",
    ]
    assert conversation_rows(db, since=SINCE, after=4) == []
    assert [row["id"] for row in conversation_rows(db, since=SINCE, limit=2)] == [
        "new-2", "live:s1:jarvis_live:1",
    ], "the newest page is the last rows, still oldest first"

    # The null surface: without the floor the same store shows the old row.
    assert conversation_rows(db, since="")[0]["id"] == "old-1"


def test_http_route_answers_rows_past_the_cursor(tmp_path: Path) -> None:
    """The route hands ``after`` and ``limit`` to the read and answers its rows."""
    db = _memory_db(tmp_path)

    def read(after: int, limit: int) -> dict[str, Any]:
        rows = conversation_rows(db, since=SINCE, after=after, limit=limit)
        return {"since": SINCE, "rows": rows}

    deps = InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        conversation_read=read,
    )
    with TestClient(create_app(deps)) as client:
        first = client.get("/inherent/conversation").json()
        assert first["since"] == SINCE
        assert [row["id"] for row in first["rows"]] == ["new-1", "new-2", "live:s1:jarvis_live:1"]
        later = client.get("/inherent/conversation", params={"after": first["rows"][-1]["seq"]})
        assert later.json()["rows"] == []
        past_two = client.get("/inherent/conversation", params={"after": 2}).json()["rows"]
        assert [row["id"] for row in past_two] == ["new-2", "live:s1:jarvis_live:1"]

    with TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(),
    ))) as client:
        assert client.get("/inherent/conversation").status_code == 404, "no memory.db, no route"
