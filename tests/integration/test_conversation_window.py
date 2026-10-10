"""The Resonance conversation window reads the conversation of record (spec §18.3).

Observables: the rows ``conversation_rows`` returns over a seeded memory.db,
and what ``GET /inherent/conversation`` answers over the same store.

- rows come oldest first with a ``seq`` cursor; ``after=0`` is the newest
  page, ``after=<seq>`` only what landed past it;
- the history floor (``session.history_since``) applies, so the window and
  the backend's prompt start at the same row;
- a retired ``<voice>``/``<document>`` envelope renders as its words;
- ``before=<seq>`` pages back, oldest first, past the floor to the first record (ADR 0215);
  ``has_more`` says older rows exist, floor or not; ``limit`` is capped at 500; a page holds at
  most 512 KB of text and at least one row; ``after`` with ``before`` is a 400.
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

    page, more = conversation_rows(db, since=SINCE)
    assert more, "old-1 is older than the page, though it is under the floor"

    assert [row["id"] for row in page] == ["new-1", "new-2", "live:s1:jarvis_live:1"]
    assert [row["seq"] for row in page] == [2, 3, 4]
    assert page[1]["text"] == "明天多云。\n最高 18 度。", "envelope tags reached the window"
    assert page[2]["source"] == "jarvis_live"
    assert [row["id"] for row in conversation_rows(db, since=SINCE, after=3)[0]] == [
        "live:s1:jarvis_live:1",
    ]
    assert conversation_rows(db, since=SINCE, after=4) == ([], False)
    short, more = conversation_rows(db, since=SINCE, limit=2)
    assert [row["id"] for row in short] == ["new-2", "live:s1:jarvis_live:1"], (
        "the newest page is the last rows, still oldest first"
    )
    assert more

    # The null surface: without the floor the same store shows the old row.
    everything, more = conversation_rows(db, since="")
    assert everything[0]["id"] == "old-1"
    assert not more


def test_http_route_answers_rows_past_the_cursor(tmp_path: Path) -> None:
    """The route hands ``after`` and ``limit`` to the read and answers its rows."""
    db = _memory_db(tmp_path)

    def read(after: int, limit: int, before: int) -> dict[str, Any]:
        rows, has_more = conversation_rows(
            db, since=SINCE, after=after, before=before, limit=limit,
        )
        return {"since": SINCE, "rows": rows, "has_more": has_more}

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


def _many(tmp_path: Path, count: int, *, since_after: int = 0) -> Path:
    """A store of ``count`` rows named ``r1``..; the first ``since_after`` are before SINCE."""
    path = tmp_path / "memory.db"
    with open_memory_db(path) as conn, conn:
        conn.executemany(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
            [
                (
                    f"r{n}",
                    "2026-09-12T07:00:00-07:00" if n <= since_after
                    else f"2026-09-15T02:{n % 60:02d}:00-07:00",
                    "allen" if n % 2 else "jarvis",
                    f"row {n}",
                )
                for n in range(1, count + 1)
            ],
        )
    return path


def test_before_pages_back_oldest_first_and_passes_the_floor(tmp_path: Path) -> None:
    """Newest page, then the rows older than its first seq, to the first record."""
    db = _many(tmp_path, 12, since_after=4)  # r1..r4 are under the floor, r5..r12 above it

    newest, more = conversation_rows(db, since=SINCE, limit=3)
    assert [row["id"] for row in newest] == ["r10", "r11", "r12"]
    assert more

    middle, more = conversation_rows(db, since=SINCE, before=newest[0]["seq"], limit=3)  # type: ignore[arg-type]
    assert [row["id"] for row in middle] == ["r7", "r8", "r9"], "oldest first within the page"
    assert more

    # `before` is the floor-free read: the page crosses the floor into r4..r2 and has_more holds
    across, more = conversation_rows(db, since=SINCE, before=middle[0]["seq"], limit=4)  # type: ignore[arg-type]
    assert [row["id"] for row in across] == ["r3", "r4", "r5", "r6"]
    assert more

    first, more = conversation_rows(db, since=SINCE, before=across[0]["seq"], limit=4)  # type: ignore[arg-type]
    assert [row["id"] for row in first] == ["r1", "r2"]
    assert not more, "the first record is the history's start"
    assert conversation_rows(db, since=SINCE, before=1, limit=4) == ([], False)

    # the default page keeps the floor; has_more still says older rows exist below it
    above, more = conversation_rows(db, since=SINCE, limit=500)
    assert above[0]["id"] == "r5"
    assert more, "r1..r4 are older, under the floor"
    # and `after` keeps the floor too
    assert [row["id"] for row in conversation_rows(db, since=SINCE, after=2, limit=2)[0]] == [
        "r5", "r6",
    ]
    assert conversation_rows(db, since=SINCE, after=2, limit=2)[1], "more rows past the page"
    assert not conversation_rows(db, since=SINCE, after=10, limit=2)[1]


def test_a_page_stops_at_512_kb_of_text_and_always_has_a_row(tmp_path: Path) -> None:
    """The budget cuts the oldest rows of a newest-first page; an oversized row still comes."""
    path = tmp_path / "memory.db"
    sizes = [100, 200_000, 200_000, 200_000]
    with open_memory_db(path) as conn, conn:
        conn.executemany(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
            [
                (f"b{n}", f"2026-09-15T03:0{n}:00-07:00", "allen", "x" * size)
                for n, size in enumerate(sizes)
            ],
        )
    page, more = conversation_rows(path, since=SINCE, limit=10)
    assert [row["id"] for row in page] == ["b2", "b3"], "512 KB holds two of the big rows"
    assert more, "b0 and b1 are older"
    older, more = conversation_rows(path, since=SINCE, before=page[0]["seq"], limit=10)  # type: ignore[arg-type]
    assert [row["id"] for row in older] == ["b0", "b1"], "400 KB and a little"
    assert not more

    forward, more = conversation_rows(path, since=SINCE, after=1, limit=10)
    assert [row["id"] for row in forward] == ["b1", "b2"], "forward pages cut at the budget too"
    assert more

    huge = tmp_path / "huge.db"
    with open_memory_db(huge) as conn, conn:
        conn.execute(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
            ("only", "2026-09-15T03:00:00-07:00", "allen", "y" * 600_000),
        )
    [row], more = conversation_rows(huge, since=SINCE)
    assert len(str(row["text"])) == 600_000
    assert not more


def test_http_before_limit_cap_and_has_more(tmp_path: Path) -> None:
    """The route hands ``before`` to the read, caps ``limit`` at 500 and refuses after+before."""
    db = _many(tmp_path, 30)
    calls: list[tuple[int, int, int]] = []

    def read(after: int, limit: int, before: int) -> dict[str, Any]:
        calls.append((after, limit, before))
        rows, has_more = conversation_rows(db, after=after, before=before, limit=limit)
        return {"since": "", "rows": rows, "has_more": has_more}

    deps = InherentDeps(
        submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(),
        conversation_read=read,
    )
    with TestClient(create_app(deps)) as client:
        first = client.get("/inherent/conversation", params={"limit": 10}).json()
        assert [row["id"] for row in first["rows"]] == [f"r{n}" for n in range(21, 31)]
        assert first["has_more"] is True
        seen = [row["id"] for row in first["rows"]]
        cursor = first["rows"][0]["seq"]
        for _ in range(2):
            page = client.get("/inherent/conversation", params={"before": cursor, "limit": 10})
            body = page.json()
            seen = [row["id"] for row in body["rows"]] + seen
            cursor = body["rows"][0]["seq"]
        assert seen == [f"r{n}" for n in range(1, 31)], "three pages cover the store once"
        assert body["has_more"] is False
        assert calls[-1] == (0, 10, 11)

        both = client.get("/inherent/conversation", params={"after": 3, "before": 9})
        assert both.status_code == 400
        for bad in ({"limit": 0}, {"limit": 501}, {"before": -1}):
            assert client.get("/inherent/conversation", params=bad).status_code == 422, bad
        assert client.get("/inherent/conversation", params={"limit": 500}).status_code == 200
        assert calls[-1][1] == 500
        # the default is 200 and `after` without `before` is the Resonance poll
        client.get("/inherent/conversation", params={"after": 5})
        assert calls[-1] == (5, 200, 0)
