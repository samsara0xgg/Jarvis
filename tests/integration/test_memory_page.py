"""ADR 0154 — the Dashboard's memory page: its routes and the semantics behind them.

Real memory.db in a tmp dir, the real nightly pass (``DaySummarySchedule`` with the same
fake provider seam test_core_memory uses), the real routes through ``TestClient`` and the
real ``Settings`` file for the cap. Nothing here edits a stored row to set a scene: every
version and summary comes from the pass or from the routes under test.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from jarvis.runtime.memory_page import MemoryPage
from jarvis.runtime.settings import Settings
from jarvis.state import core_memory
from jarvis.state.memory_db import (
    append_day_summary,
    current_core_memory,
    open_memory_db,
    remember_fact,
)
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.integration.test_core_memory import _changes, _Consolidator, _schedule
from tests.integration.test_day_summaries import TODAY, _day, _insert, _rid, _stamp, _Summariser

if TYPE_CHECKING:
    from pathlib import Path

MIGRATED_DAY = 6


class World:
    """One tmp store with a client on it."""

    def __init__(self, tmp_path: Path, *, since: str = "") -> None:
        """Wire the real page and Settings file onto a store under ``tmp_path``."""
        self.tmp = tmp_path
        self.path = tmp_path / "memory.db"
        self.settings = Settings(tmp_path, {"core_memory": {"max_chars": 4000}}, lambda _kind: [])
        self.page = MemoryPage(self.path, self.settings, history_since=since)
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: None,
                broadcaster=InherentBroadcaster(),
                memory_page=self.page,
            ),
        )
        self.client = TestClient(app)

    def get(self, path: str, **params: object) -> Any:  # noqa: ANN401 — a JSON body
        """GET a memory route; it must answer 200."""
        response = self.client.get(f"/inherent/memory{path}", params=params)  # type: ignore[arg-type]
        assert response.status_code == 200, response.text
        return response.json()

    def post(self, path: str, body: dict[str, Any], status: int = 200) -> Any:  # noqa: ANN401
        """POST a memory route and check its status."""
        response = self.client.post(f"/inherent/memory{path}", json=body)
        assert response.status_code == status, response.text
        return response.json()

    def overview(self) -> dict[str, Any]:
        """The memory screen."""
        return self.get("")  # type: ignore[no-any-return]

    def items(self) -> dict[str, dict[str, Any]]:
        """``{text: item}`` of every item now."""
        return {
            item["text"]: {**item, "section": section["name"]}
            for section in self.overview()["sections"]
            for item in section["items"]
        }

    def rows(self) -> list[tuple[str, str, str | None, str]]:
        """``(id, origin, upto_day, doc)`` of every version, oldest first."""
        with closing(open_memory_db(self.path)) as conn:
            return [
                (str(a), str(b), c, str(d))
                for a, b, c, d in conn.execute(
                    "SELECT id, origin, upto_day, doc FROM core_memory ORDER BY rowid",
                )
            ]


def _nightly(world: World, model: _Consolidator, today_ago: int = 0) -> None:
    schedule = _schedule(world.tmp, model)
    schedule.write(TODAY - timedelta(days=today_ago))


@pytest.fixture
def world(tmp_path: Path) -> World:
    """Three past days of talk, two remembered facts and one nightly pass over day 3 and day 2."""
    path = tmp_path / "memory.db"
    _insert(
        path,
        [
            (_rid(3, 1), _stamp(3, 9), "allen", "我现在住在 Victoria"),
            (_rid(3, 2), _stamp(3, 9, 1), "jarvis", "好的, 记下了"),
            (_rid(2, 1), _stamp(2, 20), "allen", "我周五要交稿"),
            (_rid(2, 2), _stamp(2, 20, 2), "allen", "我不喝咖啡了"),
            (_rid(2, 3), _stamp(2, 20, 3), "jarvis_live", "那我记住你不喝咖啡"),
        ],
    )
    remember_fact(path, "城市", "Vancouver")
    remember_fact(path, "饮食", "每天喝咖啡", "偏好")
    remember_fact(path, "项目", "写论文", "正在做的事")
    d3, d2 = _day(3).isoformat(), _day(2).isoformat()
    model = _Consolidator()
    model.script[d3] = [
        _changes({"op": "rewrite", "item": 1, "text": "城市: Victoria", "sources": ["r1"]}),
    ]
    model.script[d2] = [
        _changes(
            {"op": "add", "section": "承诺和待办", "text": "周五交稿", "sources": ["r1"]},
            {"op": "stale", "item": 2, "sources": ["r2"]},
            {"op": "rewrite", "item": 3, "text": "写论文: 第二章", "sources": ["r2"]},
        ),
    ]
    made = World(tmp_path)
    _nightly(made, model)
    return made


def test_the_page_lists_six_sections_and_what_last_night_did_with_its_quotes(world: World) -> None:
    """Counts, the six sections in order, one entry per nightly change, each with its source."""
    page = world.overview()
    assert [s["name"] for s in page["sections"]] == list(core_memory.SECTIONS)
    assert page["items"] == 3
    assert page["days"] == 2  # two past days kept word for word
    assert page["max_chars"] == page["booted_max_chars"] == 4000
    assert page["chars"] == len(core_memory.render(current_core_memory(world.path).doc))
    new = page["new"]
    assert new["day"] == _day(2).isoformat()
    kinds = {e["kind"]: e for e in new["entries"]}
    assert set(kinds) == {"add", "stale", "rewrite"}
    assert kinds["add"]["text"] == "周五交稿"
    assert kinds["add"]["quote"] == {"who": "user", "text": "我周五要交稿"}
    assert kinds["stale"]["text"] == "饮食: 每天喝咖啡"
    assert kinds["stale"]["quote"] == {"who": "user", "text": "我不喝咖啡了"}
    assert kinds["rewrite"]["text"] == "写论文: 第二章"
    assert kinds["rewrite"]["before"] == "项目: 写论文"
    # A rewritten item keeps its id; the stale one is no longer among the sections.
    assert kinds["rewrite"]["id"] in {i["id"] for s in page["sections"] for i in s["items"]}
    assert "饮食: 每天喝咖啡" not in world.items()


def test_an_item_opens_with_its_sources_as_words_and_its_history(world: World) -> None:
    """The cited record is resolved to who, when and the words; history says who wrote it."""
    rewritten = world.items()["城市: Victoria"]
    detail = world.get(f"/item/{rewritten['id']}")
    assert detail["section"] == "关于你"
    assert detail["number"] == 1
    assert detail["chars"] == len("城市: Victoria")
    assert [(s["who"], s["text"], s["day"]) for s in detail["sources"]] == [
        ("user", "我现在住在 Victoria", _day(3).isoformat()),
    ]
    assert detail["born"]["origin"] == "remember"
    assert [e["origin"] for e in detail["edits"]] == ["nightly"]
    assert detail["edits"][0]["day"] == _day(3).isoformat()
    assert detail["pinned"] is False
    assert world.client.get("/inherent/memory/item/nope").status_code == 404


def test_confirm_is_a_version_that_changes_no_text_and_takes_the_entry_off_the_new_list(
    world: World,
) -> None:
    """One user version with the same text; the entry is gone and the item is not pinned."""
    before = len(world.rows())
    entry = next(e for e in world.overview()["new"]["entries"] if e["kind"] == "add")
    world.post("/item/confirm", {"id": entry["id"]})
    rows = world.rows()
    assert len(rows) == before + 1
    assert rows[-1][1] == "user"
    assert rows[-1][3] == rows[-2][3]  # the document is the same text
    assert entry["id"] not in {e["id"] for e in world.overview()["new"]["entries"]}
    assert world.items()["周五交稿"]["pinned"] is False
    assert world.post("/item/confirm", {"id": "nope"}, status=404)
    # Undoing the confirm puts the entry back on the list (and writes a version, not a delete).
    top = world.get("/versions")["versions"][0]
    assert (top["kind"], top["undoable"]) == ("confirm", True)
    world.post("/undo", {"version": top["id"]})
    assert entry["id"] in {e["id"] for e in world.overview()["new"]["entries"]}
    assert len(world.rows()) == before + 2


def test_edit_and_move_append_user_versions_pin_the_item_and_never_touch_older_rows(
    world: World,
) -> None:
    """Every action is one new row of origin user; the rows before it stay byte for byte."""
    first = world.items()["写论文: 第二章"]
    snapshot = world.rows()
    world.post(
        "/item/edit", {"id": first["id"], "text": "  写论文: 第三章 \n", "section": "承诺和待办"}
    )
    after = world.rows()
    assert after[:-1] == snapshot  # append-only
    assert after[-1][1] == "user"
    moved = world.items()["写论文: 第三章"]
    assert (moved["section"], moved["pinned"], moved["id"]) == ("承诺和待办", True, first["id"])
    # Moving to a section is the same call with the same text: still one more row.
    world.post("/item/edit", {"id": first["id"], "text": "写论文: 第三章", "section": "偏好"})
    assert world.items()["写论文: 第三章"]["section"] == "偏好"
    assert len(world.rows()) == len(snapshot) + 2
    last = json.loads(world.rows()[-1][3])["sections"]["偏好"][-1]
    assert last["pinned"] is True
    for bad in (
        {"id": first["id"], "text": "  "},
        {"id": first["id"], "text": "x", "section": "没有"},
    ):
        assert world.post("/item/edit", bad, status=400)
    assert world.post("/item/edit", {"id": first["id"], "text": "x" * 401}, status=400)
    assert world.post("/item/edit", {"id": "nope", "text": "x"}, status=404)
    delete = world.post("/item/delete", {"id": first["id"]})
    assert delete["ok"] is True
    assert "写论文: 第三章" not in world.items()
    assert len(world.rows()) == len(snapshot) + 3


def test_the_night_leaves_a_pinned_item_alone_and_keeps_a_stale_proposal_as_a_reminder(
    world: World,
) -> None:
    """A rewrite of a pinned item is dropped and logged, a stale of it is stored but not applied."""
    pinned = world.items()["城市: Victoria"]
    world.post("/item/edit", {"id": pinned["id"], "text": "城市: Victoria (BC)", "section": None})
    free = world.items()["周五交稿"]
    d1 = _day(1).isoformat()
    _insert(world.path, [(_rid(1, 1), _stamp(1, 21), "allen", "我搬到 Seattle 了, 周五的稿也交了")])
    model = _Consolidator()
    model.script[d1] = [
        _changes(
            {"op": "rewrite", "item": 1, "text": "城市: Seattle", "sources": ["r1"]},
            {"op": "stale", "item": 1, "sources": ["r1"]},
            {"op": "stale", "item": 3, "sources": ["r1"]},
            {"op": "add", "section": "偏好", "text": "喜欢西雅图", "sources": ["r1"]},
        ),
    ]
    assert "[1] (pinned) 城市: Victoria (BC)" in _consolidation_input(world, model, d1)
    items = world.items()
    assert items["城市: Victoria (BC)"]["pinned"] is True  # untouched by the night
    assert "城市: Seattle" not in items
    assert items["喜欢西雅图"]["section"] == "偏好"  # a free change still lands
    assert "周五交稿" not in items  # the unpinned stale applied
    changes = json.loads(_changes_of_last(world))
    assert [c["op"] for c in changes] == ["stale", "add", "skipped", "suggest_stale"]
    suggestion = next(c for c in changes if c["op"] == "suggest_stale")
    assert (suggestion["id"], suggestion["text"]) == (pinned["id"], "城市: Victoria (BC)")
    skipped = next(c for c in changes if c["op"] == "skipped")
    assert (skipped["reason"], skipped["id"], skipped["change"]["op"]) == (
        "pinned",
        pinned["id"],
        "rewrite",
    )
    assert free["id"] not in {i["id"] for i in world.items().values()}
    # The page shows the suggestion as a reminder on the item and in the new list.
    entry = next(e for e in world.overview()["new"]["entries"] if e["kind"] == "suggest_stale")
    assert entry["id"] == pinned["id"]
    assert world.get(f"/item/{pinned['id']}")["reminder"] is True
    # 对 on a reminder hides it; the item stays, still pinned.
    world.post("/item/confirm", {"id": pinned["id"]})
    assert all(e["kind"] != "suggest_stale" for e in world.overview()["new"]["entries"])
    assert world.items()["城市: Victoria (BC)"]["pinned"] is True


def _consolidation_input(world: World, model: _Consolidator, day: str) -> str:
    _nightly(world, model)
    return model.inputs[day]


def _changes_of_last(world: World) -> str:
    with closing(open_memory_db(world.path)) as conn:
        return str(
            conn.execute(
                "SELECT changes FROM core_memory WHERE origin = 'nightly' "
                "ORDER BY rowid DESC LIMIT 1",
            ).fetchone()[0],
        )


def test_keeping_a_stale_item_puts_it_back_pinned_and_a_stale_confirm_drops_the_entry(
    world: World,
) -> None:
    """留着 restores what the night removed, pinned; 对 on it just hides the entry."""
    stale = next(e for e in world.overview()["new"]["entries"] if e["kind"] == "stale")
    version = world.overview()["new"]["version"]
    assert (
        world.client.post(
            "/inherent/memory/item/keep",
            json={"version": "core:nope", "id": stale["id"]},
        ).status_code
        == 404
    )
    world.post("/item/keep", {"version": version, "id": stale["id"]})
    back = world.items()["饮食: 每天喝咖啡"]
    assert (back["pinned"], back["section"], back["id"]) == (True, "偏好", stale["id"])
    assert all(e["kind"] != "stale" for e in world.overview()["new"]["entries"])
    assert world.post("/item/keep", {"version": version, "id": stale["id"]}, status=400)


def test_undo_takes_back_what_a_version_changed_as_a_new_version(world: World) -> None:
    """A delete comes back in place, a rewrite returns to its old words, nights stay done."""
    upto = current_core_memory(world.path).upto_day
    target = world.items()["城市: Victoria"]
    world.post("/item/delete", {"id": target["id"]})
    deleted = world.get("/versions")["versions"][0]
    assert (deleted["kind"], deleted["counts"]["old"], deleted["undoable"]) == ("user", 1, True)
    count = len(world.rows())
    world.post("/undo", {"version": deleted["id"]})
    assert len(world.rows()) == count + 1
    assert world.rows()[-1][1] == "user"
    again = world.items()["城市: Victoria"]
    assert again["id"] == target["id"]
    assert (
        json.loads(world.rows()[-1][3])["sections"]["关于你"][0]["text"] == "城市: Victoria"
    )  # its old index
    top = world.get("/versions")["versions"][0]
    assert (top["kind"], top["current"]) == ("undo", True)
    assert current_core_memory(world.path).upto_day == upto

    # The night's version: the rewrite goes back, the add goes, the stale item returns.
    nightly = next(
        v
        for v in world.get("/versions")["versions"]
        if v["kind"] == "nightly" and v["counts"]["old"]
    )
    world.post("/undo", {"version": nightly["id"]})
    items = world.items()
    assert "周五交稿" not in items
    assert "写论文: 第二章" not in items
    assert items["项目: 写论文"]["section"] == "正在做的事"
    assert items["饮食: 每天喝咖啡"]["section"] == "偏好"
    assert current_core_memory(world.path).upto_day == upto  # undoing a night does not rerun it


def test_undo_refuses_when_a_later_version_touched_the_same_item_and_for_the_first_version(
    world: World,
) -> None:
    """409 names the item; the first version and a version that changed nothing are 400."""
    versions = world.get("/versions")
    nightly = next(v for v in versions["versions"] if v["kind"] == "nightly" and v["counts"]["add"])
    target = world.items()["周五交稿"]
    world.post("/item/edit", {"id": target["id"], "text": "周五交稿 (第一版)"})
    rows_before = world.rows()
    refused = world.client.post("/inherent/memory/undo", json={"version": nightly["id"]})
    assert refused.status_code == 409
    assert "周五交稿" in refused.json()["detail"]
    assert world.rows() == rows_before  # nothing was written
    first = versions["versions"][-1]
    assert first["kind"] == "migration"
    assert world.post("/undo", {"version": first["id"]}, status=400)
    _insert(world.path, [(_rid(1, 1), _stamp(1, 9), "allen", "今天没什么事")])
    _nightly(world, _Consolidator())  # an empty answer: a version that only advances upto_day
    empty = next(v for v in world.get("/versions")["versions"] if v["kind"] == "nightly")
    assert empty["undoable"] is False
    assert world.post("/undo", {"version": empty["id"]}, status=400)
    assert world.post("/undo", {"version": "core:nope"}, status=404)
    # Taking back the later version first clears the block.
    top = next(
        v for v in world.get("/versions")["versions"] if v["kind"] == "user" and v["counts"]["chg"]
    )
    world.post("/undo", {"version": top["id"]})
    world.post("/undo", {"version": nightly["id"]})
    assert "周五交稿" not in world.items()


def test_the_versions_list_names_each_kind_and_marks_the_current_one(world: World) -> None:
    """Newest first: migration, remember and nightly rows with their 新/改/过时 lines."""
    versions = world.get("/versions")
    assert versions["total"] == len(world.rows())
    kinds = [v["kind"] for v in versions["versions"]]
    assert (kinds[0], kinds[-1]) == ("nightly", "migration")
    assert kinds.count("remember") == 3
    assert [v["current"] for v in versions["versions"]] == [True] + [False] * (len(kinds) - 1)
    latest = versions["versions"][0]
    assert latest["day"] == _day(2).isoformat() or latest["day"] == _day(3).isoformat()
    assert latest["counts"] == {"add": 1, "chg": 1, "old": 1}
    assert {line["tag"] for line in latest["lines"]} == {"add", "chg", "old"}
    assert latest["undoable"] is True
    assert versions["versions"][-1]["undoable"] is False


def test_the_cap_is_saved_in_settings_validated_and_obeyed_by_the_next_write(world: World) -> None:
    """1000 to 20000; the page reads it back with the booted value; a longer note is refused."""
    assert world.post("/cap", {"max_chars": 999}, status=400)
    assert world.post("/cap", {"max_chars": 20001}, status=400)
    saved = world.post("/cap", {"max_chars": 1200})
    assert saved == {"max_chars": 1200, "booted_max_chars": 4000}
    page = world.overview()
    assert (page["max_chars"], page["booted_max_chars"]) == (1200, 4000)
    saved_file = json.loads((world.tmp / "settings.json").read_text(encoding="utf-8"))
    assert saved_file["core_memory_max_chars"] == 1200
    target = world.items()["写论文: 第二章"]
    refused = world.client.post(
        "/inherent/memory/item/edit",
        json={"id": target["id"], "text": "长" * 400},
    )
    assert refused.status_code == 200  # 400 chars fits under 1200 with the rest
    over = world.client.post(
        "/inherent/memory/item/edit",
        json={"id": world.items()["周五交稿"]["id"], "text": "长" * 400},
    )
    assert over.status_code == 200
    third = world.client.post(
        "/inherent/memory/item/edit",
        json={"id": world.items()["城市: Victoria"]["id"], "text": "长" * 400},
    )
    assert third.status_code == 400
    assert "cap" in third.json()["detail"]
    # Shrinking is always allowed, even when the note is over the cap.
    assert world.post("/item/edit", {"id": target["id"], "text": "短"})["ok"] is True


def _search_world(tmp_path: Path, since: str = "") -> World:
    path = tmp_path / "memory.db"
    _insert(
        path,
        [
            (_rid(5, 1), _stamp(5, 8), "allen", "你从一数到五十, 慢一点"),
            (_rid(5, 2), _stamp(5, 8, 1), "jarvis", "好.一, 二, 三……一直数到五十."),
            (_rid(2, 1), _stamp(2, 17, 16), "allen", "我 9 月 28 号让你从一数到几."),
            (_rid(2, 2), _stamp(2, 17, 17), "jarvis_live", "你让我从 1 数到 50."),
            (_rid(2, 3), _stamp(2, 17, 18), "allen", "今天天气怎么样"),
        ],
    )
    return World(tmp_path, since=since)


def test_search_groups_by_day_newest_first_with_both_speakers_and_filters(tmp_path: Path) -> None:
    """Any of the words, a window of text around the first match, the two speakers' filters."""
    world = _search_world(tmp_path)
    found = world.get("/search", q="数到", who="all")
    assert [d["day"] for d in found["days"]] == [_day(2).isoformat(), _day(5).isoformat()]
    assert (found["total"], found["words"], found["truncated"]) == (4, ["数到"], False)
    first = found["days"][0]["hits"]
    assert [(h["who"], h["text"]) for h in first] == [
        ("user", "我 9 月 28 号让你从一数到几."),
        ("jarvis", "你让我从 1 数到 50."),
    ]
    users = world.get("/search", q="数到", who="user")
    assert {h["who"] for d in users["days"] for h in d["hits"]} == {"user"}
    hers = world.get("/search", q="数到", who="jarvis")
    assert {h["who"] for d in hers["days"] for h in d["hits"]} == {"jarvis"}
    assert world.get("/search", q="天气 西雅图")["total"] == 1  # any of the words
    assert world.get("/search", q="   ")["days"] == []
    assert world.get("/search", q="100%")["total"] == 0  # LIKE wildcards are plain text
    hit = found["days"][0]["hits"][0]
    assert hit["ts"].startswith(_day(2).isoformat())


def test_search_stops_at_the_history_boundary_and_says_where_it_starts(tmp_path: Path) -> None:
    """Records before ``history_since`` are not results; the answer names the boundary."""
    since = _stamp(3, 0)
    world = _search_world(tmp_path, since=since)
    found = world.get("/search", q="数到")
    assert [d["day"] for d in found["days"]] == [_day(2).isoformat()]
    assert found["since"] == since


def test_a_day_view_pages_the_conversation_and_can_start_at_a_record(tmp_path: Path) -> None:
    """Oldest first, one local day, ``around`` opens near a record, a bad day is 400."""
    path = tmp_path / "memory.db"
    _insert(
        path,
        [
            (_rid(2, n), _stamp(2, 9, n % 60), "allen" if n % 2 else "jarvis", f"第{n}句")
            for n in range(1, 31)
        ]
        + [(_rid(3, 1), _stamp(3, 9), "allen", "前一天")],
    )
    world = World(tmp_path)
    day = _day(2).isoformat()
    first = world.get(f"/day/{day}/records", limit=10)
    assert (first["total"], first["offset"], len(first["records"])) == (30, 0, 10)
    assert [r["text"] for r in first["records"][:2]] == ["第1句", "第2句"]
    assert [r["who"] for r in first["records"][:2]] == ["user", "jarvis"]
    near = world.get(f"/day/{day}/records", around=_rid(2, 25), limit=10)
    assert near["offset"] == 16
    assert "第25句" in [r["text"] for r in near["records"]]
    assert world.client.get("/inherent/memory/day/not-a-day/records").status_code == 400


def _summarise(world: World, replies: dict[str, str]) -> _Summariser:
    """Run the real day-summary job; ``replies`` maps a day to the model's answer."""
    summariser = _Summariser()
    summariser.replies = replies
    schedule = _schedule(world.tmp, _Consolidator())
    schedule._client = summariser  # type: ignore[assignment]  # noqa: SLF001 — the provider seam
    schedule.write(TODAY)
    return summariser


GENERATED = (
    "## {day}\n### Topics\n- 回顾昨天的工作: 实时语音和字幕 [record_id={a}]\n"
    "- 问了周末天气 [record_id={b}]\n### Decisions and facts the user stated\n"
    "- demo 定在 10 月 9 日 [record_id={a}]\n### Unfinished\nnone"
)


def test_a_day_summary_edit_wins_over_the_generated_one_and_is_never_regenerated(
    tmp_path: Path,
) -> None:
    """The edit is a new row that recall and the cards read; the night never writes that day again.

    A line the user left as it was keeps its record id.
    """
    path = tmp_path / "memory.db"
    day = _day(1).isoformat()
    _insert(
        path,
        [
            (_rid(1, 1), _stamp(1, 9), "allen", "谈一下实时语音"),
            (_rid(1, 2), _stamp(1, 9, 5), "allen", "周末天气"),
        ],
        pad=True,
    )
    world = World(tmp_path)
    old = GENERATED.format(day=day, a=_rid(1, 1), b=_rid(1, 2))
    summariser = _summarise(world, {day: old})
    assert summariser.asked == [day]
    cards = world.get("/days")["days"]
    assert [(c["day"], c["kind"], c["records"]) for c in cards] == [(day, "model", 2)]
    assert cards[0]["lines"] == [
        "回顾昨天的工作: 实时语音和字幕",
        "问了周末天气",
    ]  # markers never reach the page
    shown = world.get(f"/day/{day}")
    assert shown["sections"] == {
        "topics": ["回顾昨天的工作: 实时语音和字幕", "问了周末天气"],
        "decisions": ["demo 定在 10 月 9 日"],
        "unfinished": [],
    }
    edited = world.post(
        "/day/edit",
        {
            "day": day,
            "sections": {
                "topics": ["回顾昨天的工作: 实时语音和字幕", "改过的一行"],
                "decisions": ["demo 定在 10 月 9 日"],
                "unfinished": ["字幕还没修好"],
            },
        },
    )
    assert edited == {"ok": True}
    with closing(open_memory_db(path)) as conn:
        rows = conn.execute(
            "SELECT summary, model FROM day_summaries WHERE day = ? ORDER BY rowid", (day,)
        ).fetchall()
    assert [m for _s, m in rows] == [rows[0][1], "user"]
    assert rows[0][0] == old  # the generated row is still there, as history
    assert rows[1][0] == (
        f"## {day}\n### Topics\n"
        f"- 回顾昨天的工作: 实时语音和字幕 [record_id={_rid(1, 1)}]\n- 改过的一行\n"
        "### Decisions and facts the user stated\n"
        f"- demo 定在 10 月 9 日 [record_id={_rid(1, 1)}]\n"
        "### Unfinished\n- 字幕还没修好"
    )
    assert world.get("/days")["days"][0]["kind"] == "user"
    assert world.get(f"/day/{day}")["sections"]["unfinished"] == ["字幕还没修好"]
    # recall (the model's tool) reads the newest row.
    from jarvis.state.daily_records import recall  # noqa: PLC0415 — one reader, one use

    start = _stamp(2, 0)
    spoken = recall(path, {"from": start, "to": _stamp(0, 0)})
    assert [s["summary"] for s in spoken["summaries"]] == [rows[1][0]]
    # The night never writes a day that has a row: a second run asks the model nothing.
    again = _summarise(world, {day: "## x\n### Topics\nnone"})
    assert again.asked == []
    # Emptying a section writes "none", as the generator does; a verbatim day has nothing to edit.
    world.post(
        "/day/edit", {"day": day, "sections": {"topics": ["a"], "decisions": [], "unfinished": []}}
    )
    assert world.get(f"/day/{day}")["sections"] == {
        "topics": ["a"],
        "decisions": [],
        "unfinished": [],
    }
    other = _day(4).isoformat()
    append_day_summary(
        path,
        day=other,
        summary=f"## {other} (the whole day, word for word)\n09:00 user: 好",
        model="verbatim",
        record_count=1,
        input_chars=1,
        output_chars=3,
    )
    assert world.get("/days")["days"][-1] == {
        "day": other,
        "ts": world.get("/days")["days"][-1]["ts"],
        "kind": "verbatim",
        "records": 1,
        "lines": ["好"],
    }
    assert world.get(f"/day/{other}")["lines"] == ["09:00 user: 好"]
    assert world.post("/day/edit", {"day": other, "sections": {}}, status=400)
    assert world.client.get("/inherent/memory/day/2001-01-01").status_code == 404
    assert world.post("/day/edit", {"day": "2001-01-01", "sections": {}}, status=404)


def test_the_routes_are_absent_without_the_page(tmp_path: Path) -> None:
    """No store wired, no routes: every memory path is a plain 404."""
    client = TestClient(
        create_app(
            InherentDeps(submit_callable=lambda _t: None, broadcaster=InherentBroadcaster())
        ),
    )
    assert client.get("/inherent/memory").status_code == 404
    assert client.post("/inherent/memory/undo", json={"version": "x"}).status_code == 404
    del tmp_path


def test_a_fresh_store_migrates_on_the_first_read_and_has_nothing_new(tmp_path: Path) -> None:
    """An empty store answers with six empty sections, no new entries and one version."""
    world = World(tmp_path)
    open_memory_db(world.path).close()
    page = world.overview()
    assert page["items"] == 0
    assert page["new"] == {"day": None, "ts": None, "version": None, "entries": []}
    assert [v["kind"] for v in world.get("/versions")["versions"]] == ["migration"]
