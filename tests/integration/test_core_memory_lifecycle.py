"""Core memory's two lifecycles and the monthly rebuild (ADR 0389).

Real memory.db, the real ``DaySummarySchedule`` and the real page functions; only the
consolidator's and the rebuilder's provider calls are faked, by the scripted model.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.core_memory import date_close
from jarvis.decision.core_memory_rebuild import check_rebuild
from jarvis.decision.cost_guard import CostRecorder
from jarvis.runtime.core_memory import CoreMemorySettings, RebuildSettings, checkin_line
from jarvis.runtime.core_memory_rebuild import (
    Proposal,
    land,
    main,
    month_days,
    propose_rebuild,
    rebuild_due,
    run_rebuild,
)
from jarvis.runtime.turn.context import live_lines
from jarvis.state import core_memory, memory_page
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    append_day_summary,
    current_core_memory,
    keep_fact,
    last_rebuild_month,
    open_memory_db,
    remember_fact,
    render_context,
)
from tests.support.core_memory import (
    consolidation_input,
    consolidator,
    consolidator_changes,
    core_schedule,
)
from tests.support.day_summaries import (
    LOCAL_ZONE,
    TODAY,
    asked_days,
    day_ago,
    insert_records,
    record_id,
    stamp_ago,
)
from tests.support.model import Reply, ScriptedModel

if TYPE_CHECKING:
    from pathlib import Path

LIMITS: dict[str, Any] = {"max_chars": 40000, "section_caps": core_memory.SECTION_CAPS}
REBUILD = RebuildSettings(preset="r", prompt="rebuild")


def _prompt(path: Path) -> str:
    return render_context(path, exclude_id="").profile


def _stored(path: Path, text: str) -> core_memory.Item:
    """The item with ``text`` in the current version, ended or not."""
    doc = current_core_memory(path).doc
    return next(item for items in doc.values() for item in items if item["text"] == text)


def _versions(path: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    with closing(open_memory_db(path)) as conn:
        rows = conn.execute("SELECT origin, changes FROM core_memory ORDER BY rowid")
        return [(origin, json.loads(changes)) for origin, changes in rows]


def test_a_thread_closes_once_every_date_it_waited_for_has_passed(tmp_path: Path) -> None:
    """A date ahead of the item's wording closes it once passed; history dates and months do not.

    The closed item leaves the prompt, stays in the store with its reason, and comes back pinned.
    """
    path = tmp_path / "memory.db"
    insert_records(path, [(record_id(n, 1), stamp_ago(n, 9), "allen", f"第{n}天") for n in (3, 2)])
    d3, d2 = day_ago(3).isoformat(), day_ago(2).isoformat()
    later = TODAY + timedelta(days=70)
    texts = {
        "test": f"路考 {d2}",
        "visa": f"学签 {d2} 到期",
        "interview": f"{day_ago(9).isoformat()} 面了 Amazon, 等结果",
        "car": f"新车 {day_ago(9).isoformat()} 下单, 交付 {later.year}-{later.month:02d}",
        "past": f"{day_ago(9).isoformat()} 搬到 Victoria",
    }
    model = consolidator(
        {
            d3: [
                consolidator_changes(
                    *(
                        {"op": "add", "section": section, "text": texts[key], "sources": ["r1"]}
                        for key, section in (
                            ("test", "生活里的几件事"),
                            ("visa", "证件和硬期限"),
                            ("interview", "生活里的几件事"),
                            ("car", "生活里的几件事"),
                            ("past", "经历"),
                        )
                    ),
                ),
            ],
        },
    )

    core_schedule(tmp_path, model).write(TODAY - timedelta(days=1))

    assert asked_days(model) == [d3, d2]
    prompt = _prompt(path)
    for kept in ("interview", "car", "past"):
        assert texts[kept] in prompt
    for closed in ("test", "visa"):
        assert texts[closed] not in prompt
        item = _stored(path, texts[closed])
        assert (item["state"], item["ended_on"], item["why"]) == (
            "closed", d2, f"date passed ({d2})",
        )
    night = _versions(path)[-1][1]
    assert [c.get("reason") for c in night if c["op"] == "stale"] == [f"date passed ({d2})"] * 2

    # The page lists it under its section as ended, and restoring it puts it back, pinned.
    page = memory_page.overview(path, max_chars=40000, booted_max_chars=40000)
    goals = next(s for s in page["sections"] if s["name"] == "生活里的几件事")
    ended = next(e for e in goals["ended"] if e["text"] == texts["test"])
    assert (ended["state"], ended["why"]) == ("closed", f"date passed ({d2})")
    assert texts["test"] not in {i["text"] for i in goals["items"]}
    memory_page.restore_item(path, ended["id"], **LIMITS)
    back = _stored(path, texts["test"])
    assert (back["state"], back["pinned"], "why" in back) == ("open", True, False)
    assert back["evidence"] == TODAY.isoformat()  # his keeping it is the evidence
    assert texts["test"] in _prompt(path)


def test_a_pinned_thread_past_its_date_is_only_a_reminder_and_only_once(tmp_path: Path) -> None:
    """The night that first passes the date notes suggest_stale; the item stays; then quiet."""
    path = tmp_path / "memory.db"
    d4, d2 = day_ago(4).isoformat(), day_ago(2).isoformat()
    insert_records(path, [(record_id(n, 1), stamp_ago(n, 9), "allen", "话") for n in (3, 2, 1)])
    text = f"路考 {d2} 下午"
    _seed(path, {"生活里的几件事": [_item("test", text, d4, pinned=True, state="open")]}, upto=d4)

    core_schedule(tmp_path, consolidator()).write(TODAY)

    notes = [c for _o, changes in _versions(path) for c in changes if c["op"] == "suggest_stale"]
    assert [(n["id"], n["text"]) for n in notes] == [("test", text)]
    assert _stored(path, text)["state"] == "open"
    assert text in _prompt(path)


def test_changes_cite_records_of_any_day_and_ongoing_renews_a_thread(tmp_path: Path) -> None:
    """An item's earlier records are offered as e-labels and stored as real ids.

    ``ongoing`` moves only the evidence day.
    """
    path = tmp_path / "memory.db"
    insert_records(
        path, [(record_id(n, 1), stamp_ago(n, 9), "allen", f"找工作第{n}天") for n in (3, 2, 1)],
    )
    d3, d2, d1 = (day_ago(n).isoformat() for n in (3, 2, 1))
    model = consolidator(
        {
            d3: [consolidator_changes(
                {"op": "add", "section": "生活里的几件事", "text": "找工作", "sources": ["r1"]},
            )],
            d2: [consolidator_changes(
                {"op": "rewrite", "item": 1, "text": "找工作: 二面", "sources": ["e1", "r1"]},
            )],
            d1: [consolidator_changes({"op": "ongoing", "item": 1, "sources": ["r1"]})],
        },
    )

    core_schedule(tmp_path, model).write(TODAY)

    assert f"[1] 找工作 (evidence {d3}) [cites e1]" in consolidation_input(model, d2)
    item = _stored(path, "找工作: 二面")
    assert item["sources"] == [record_id(3, 1), record_id(2, 1)]
    assert (item["evidence"], item["since"], item["state"]) == (d1, d2, "open")
    # ongoing changes no wording: the 改动 screen shows nothing for that night.
    top = memory_page.versions(path)["versions"][0]
    assert (top["day"], top["lines"]) == (d1, [])


@pytest.mark.parametrize(
    ("change", "section"),
    [
        ({"op": "ongoing", "item": 1, "sources": ["r1"]}, "兴趣和品味"),  # no thread
        ({"op": "ongoing", "item": 1, "sources": ["e1"]}, "生活里的几件事"),  # no record of the day
        ({"op": "rewrite", "item": 1, "text": "x", "sources": ["e9"]}, "生活里的几件事"),
    ],
)
def test_an_ongoing_or_citation_the_gate_refuses_stores_nothing(
    tmp_path: Path, change: dict[str, Any], section: str,
) -> None:
    """An ongoing on a standing item or without a record of the day, an unknown label: refused."""
    path = tmp_path / "memory.db"
    insert_records(path, [(record_id(n, 1), stamp_ago(n, 9), "allen", "话") for n in (3, 2)])
    d3, d2 = day_ago(3).isoformat(), day_ago(2).isoformat()
    model = consolidator(
        {
            d3: [consolidator_changes(
                {"op": "add", "section": section, "text": "一条", "sources": ["r1"]},
            )],
            d2: [consolidator_changes(change)],
        },
    )

    core_schedule(tmp_path, model).write(TODAY - timedelta(days=1))

    assert asked_days(model) == [d3, d2, d2]
    assert current_core_memory(path).upto_day == d3


def test_a_stale_standing_item_leaves_the_prompt_and_stays_in_the_store(tmp_path: Path) -> None:
    """A standing item marked stale on evidence keeps its place in the store, marked stale."""
    path = tmp_path / "memory.db"
    insert_records(path, [(record_id(2, 1), stamp_ago(2, 9), "allen", "我不喝咖啡了")])
    remember_fact(path, "饮食", "每天喝咖啡", "兴趣和品味")
    model = consolidator(
        {
            day_ago(2).isoformat(): [
                consolidator_changes({"op": "stale", "item": 1, "sources": ["r1"]}),
            ],
        },
    )

    core_schedule(tmp_path, model).write(TODAY)

    assert "每天喝咖啡" not in _prompt(path)
    item = _stored(path, "饮食: 每天喝咖啡")
    assert (item["state"], item["ended_on"]) == ("stale", day_ago(2).isoformat())
    assert current_core_memory(path).doc["兴趣和品味"] == [item]
    # The 改动 screen shows the night ending it.
    assert memory_page.versions(path)["versions"][0]["lines"] == [
        {"tag": "old", "text": "饮食: 每天喝咖啡"},
    ]


def test_an_item_with_no_words_of_his_behind_it_renders_as_inferred(tmp_path: Path) -> None:
    """Cited only on her words: inferred; cited on his: said."""
    path = tmp_path / "memory.db"
    insert_records(
        path,
        [
            (record_id(2, 1), stamp_ago(2, 9), "jarvis", "看起来你喜欢爵士"),
            (record_id(2, 2), stamp_ago(2, 10), "allen", "我在学吉他"),
        ],
    )
    d2 = day_ago(2).isoformat()
    model = consolidator(
        {
            d2: [consolidator_changes(
                {"op": "add", "section": "兴趣和品味", "text": "喜欢爵士", "sources": ["r1"]},
                {"op": "add", "section": "兴趣和品味", "text": "在学吉他", "sources": ["r2"]},
            )],
        },
    )

    core_schedule(tmp_path, model).write(TODAY)

    prompt = _prompt(path)
    assert f"- ({d2}, inferred) 喜欢爵士" in prompt
    assert f"- ({d2}, said) 在学吉他" in prompt


def test_a_quiet_thread_is_offered_once_a_day_quietest_first(tmp_path: Path) -> None:
    """21 days without evidence: one line a day; a thread again only after new evidence."""
    path = tmp_path / "memory.db"
    noon = datetime.combine(TODAY, time(12), LOCAL_ZONE)
    for days, text in ((30, "买车"), (22, "找房子"), (5, "找工作")):
        keep_fact(path, text, section="生活里的几件事", now=noon - timedelta(days=days))
    keep_fact(path, "养狗", section="身边的人", now=noon - timedelta(days=90))  # standing

    def line(days: int) -> str | None:
        return checkin_line(path, LOCAL_ZONE, lambda: noon + timedelta(days=days))

    first = line(0)
    assert first is not None
    assert "“买车”" in first
    assert "30 days" in first
    assert line(0) is None  # one a day
    second = line(1)
    assert second is not None
    assert "“找房子”" in second
    assert line(2) is None  # 找工作 is not quiet yet; the two above were shown for this evidence


@pytest.mark.parametrize(
    ("text", "worded", "night", "closes"),
    [
        ("路考 2026-11-12", "2026-11-01", "2026-11-12", True),
        ("路考 2026-11-12", "2026-11-01", "2026-11-11", False),
        ("截至 2026-10-01 投了 50 份", "2026-10-01", "2026-12-01", False),
        ("车 4月30日 订的, 12月20日 交付", "2026-10-01", "2026-12-20", True),  # 4月30日 is history
        ("车 4月30日 订的, 12月20日 交付", "2026-10-01", "2026-12-19", False),
        ("交付 2026-12 到 2027-01", "2026-10-03", "2027-01-30", False),
        ("交付 2026-12 到 2027-01", "2026-10-03", "2027-01-31", True),
        ("书读了 1/2", "2026-10-22", "2027-01-03", False),  # a ratio, not a date
    ],
)
def test_which_thread_dates_close_it(text: str, worded: str, night: str, closes: bool) -> None:  # noqa: FBT001
    """Only dates ahead of the wording count.

    A month is its last day; a year-less date takes the year nearest the wording day.
    """
    doc = core_memory.empty_doc()
    doc["生活里的几件事"] = [_item("t", text, worded, state="open")]
    assert bool(date_close(doc, night, None)) is closes


def test_the_check_in_reaches_her_whole(tmp_path: Path) -> None:
    """The live context keeps the whole note, the instruction and a long item."""
    path = tmp_path / "memory.db"
    noon = datetime.combine(TODAY, time(12), LOCAL_ZONE)
    text = "找工作: 投了 Shopify 和 Stripe, 等 Stripe 二面结果, " + "备注" * 100
    keep_fact(path, text, section="生活里的几件事", now=noon - timedelta(days=30))

    (line,) = live_lines((lambda: checkin_line(path, LOCAL_ZONE, lambda: noon),))

    assert "ask him once, in passing, whether it is still going" in line
    assert line.endswith(f"“{text}”")


def _seed(path: Path, sections: dict[str, list[core_memory.Item]], upto: str | None = None) -> None:
    """A version holding exactly these items, as an import or a hand fix would write it."""
    doc = core_memory.empty_doc()
    doc.update(sections)
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        base = core_memory.current(conn, "2026-09-01T00:00:00+00:00")
        core_memory.append_version(
            conn, base=base, doc=doc, origin="test", upto_day=upto, changes=[],
            now="2026-09-01T00:00:00+00:00",
        )


def _item(item_id: str, text: str, since: str, **more: Any) -> core_memory.Item:  # noqa: ANN401
    return {"id": item_id, "text": text, "topic": None, "sources": [], "since": since, **more}


MONTH = day_ago(3).strftime("%Y-%m")


@pytest.fixture
def note(tmp_path: Path) -> Path:
    """[1] pinned birthday, [2] an inferred taste, [3] guitar (set), [4] a bug as a thread.

    The month has one summary day citing two lines of his and one of hers.
    """
    path = tmp_path / "memory.db"
    _seed(
        path,
        {
            "经历": [_item("born", "生日 3月5日", "2026-09-01", pinned=True)],
            "兴趣和品味": [
                _item("guess", "可能喜欢爵士", "2026-09-02", sources=[record_id(3, 3)]),
                _item("guitar", "吉他: 在学", "2026-09-01", topic="吉他"),
            ],
            "生活里的几件事": [_item("bug", "唤醒词 bug 2026-09-13", "2026-09-13", state="open")],
        },
    )
    d3 = day_ago(3).isoformat()
    insert_records(
        path,
        [
            (record_id(3, 1), stamp_ago(3, 9), "allen", "我喜欢爵士钢琴"),
            (record_id(3, 2), stamp_ago(3, 10), "allen", "吉他每周练三次"),
            (record_id(3, 3), stamp_ago(3, 11), "jarvis", "你大概喜欢爵士"),
        ],
    )
    append_day_summary(
        path,
        day=d3,
        summary=(
            f"## {d3}\n### Topics\n- 喜欢爵士钢琴 [record_id={record_id(3, 1)}]\n"
            f"- 吉他每周练三次 [record_id={record_id(3, 2)}]\n"
            f"- 她猜他喜欢爵士 [record_id={record_id(3, 3)}]\n"
            "### Decisions and facts the user stated\nnone\n### Unfinished\nnone"
        ),
        model="m", record_count=3, input_chars=1, output_chars=1,
    )
    return path


def _propose(path: Path, answer: str, **core: Any) -> Proposal:  # noqa: ANN401
    log = path.parent / "events.db"
    model = ScriptedModel(Reply(answer), model="fake-rebuilder")
    with closing(open_event_log(log)) as conn:
        return propose_rebuild(
            MONTH,
            memory=MemorySettings(db_path=path, audio_dir=path.parent / "audio"),
            settings=CoreMemorySettings(preset="c", prompt="c", rebuild=REBUILD, **core),
            client=model.as_llm_client(),
            cost_recorder=CostRecorder(conn, pricing_table={}),
            day=TODAY.isoformat(),
        )


def _answer(items: list[dict[str, Any]], dropped: list[dict[str, Any]]) -> str:
    return json.dumps({"items": items, "dropped": dropped}, ensure_ascii=False)


BORN = {"item": 1, "section": "经历", "text": "生日 3月5日"}
GUITAR = {"item": 3, "section": "兴趣和品味", "text": "吉他: 在学, 每周练三次", "sources": ["s2"]}
JAZZ = {"section": "兴趣和品味", "text": "喜欢爵士钢琴", "sources": ["s1"]}
DROPS = [{"item": 2, "reason": "只是她的猜测"}, {"item": 4, "reason": "一次性的 bug"}]


def test_a_rebuild_that_passes_lands_as_one_version_shown_first_and_undoable(note: Path) -> None:
    """Kept, reworded, added and dropped items land as one rebuild version; undo restores all."""
    path = note
    before = _prompt(path)
    proposal = _propose(path, _answer([BORN, GUITAR, JAZZ], DROPS))
    assert proposal.why is None

    assert land(MemorySettings(db_path=path, audio_dir=path.parent / "audio"), proposal)

    origin, changes = _versions(path)[-1]
    assert origin == "rebuild"
    assert changes[-1] == {"op": "rebuild", "month": MONTH}
    assert last_rebuild_month(path) == MONTH
    prompt = _prompt(path)
    for text in ("生日 3月5日", "吉他: 在学, 每周练三次", "喜欢爵士钢琴"):
        assert text in prompt
    for text in ("可能喜欢爵士", "唤醒词 bug"):
        assert text not in prompt
    bug = _stored(path, "唤醒词 bug 2026-09-13")
    assert (bug["state"], bug["why"]) == ("closed", "一次性的 bug")
    assert _stored(path, "可能喜欢爵士")["state"] == "stale"
    assert _stored(path, "喜欢爵士钢琴")["sources"] == [record_id(3, 1)]

    # The page's "new" shows the rebuild first, with each drop's reason.
    new = memory_page.overview(path, max_chars=40000, booted_max_chars=40000)["new"]
    assert new["origin"] == "rebuild"
    whys = {e["text"]: e.get("why") for e in new["entries"] if e["kind"] == "stale"}
    assert whys == {"可能喜欢爵士": "只是她的猜测", "唤醒词 bug 2026-09-13": "一次性的 bug"}
    top = memory_page.versions(path)["versions"][0]
    assert (top["kind"], top["counts"]) == ("rebuild", {"add": 1, "chg": 1, "old": 2})

    memory_page.undo(path, top["id"], **LIMITS)
    assert _prompt(path) == before


def _bad_rebuilds() -> dict[str, tuple[str, dict[str, Any]]]:
    keep = {"item": 2, "section": "兴趣和品味", "text": "可能喜欢爵士"}
    return {
        "not JSON": ("here is the note", {}),
        "pinned item reworded": (
            _answer([{**BORN, "text": "生日是 3月5日"}, GUITAR], DROPS), {},
        ),
        "pinned item moved": (_answer([{**BORN, "section": "身体"}, GUITAR], DROPS), {}),
        "pinned item dropped": (
            _answer([GUITAR], [*DROPS, {"item": 1, "reason": "旧了"}]), {},
        ),
        "an item neither kept nor dropped": (_answer([BORN], DROPS), {}),
        "an item twice": (_answer([BORN, GUITAR, {**GUITAR, "text": "又一次"}], DROPS), {}),
        "a drop without a reason": (
            _answer([BORN, GUITAR], [DROPS[0], {"item": 4, "reason": " "}]), {},
        ),
        "inferred kept with no record": (_answer([BORN, GUITAR, keep], DROPS[1:]), {}),
        "inferred kept on her words only": (
            _answer([BORN, GUITAR, {**keep, "sources": ["s3"]}], DROPS[1:]), {},
        ),
        "a new item citing nothing": (
            _answer([BORN, GUITAR, {**JAZZ, "sources": []}], DROPS), {},
        ),
        "an unknown label": (_answer([BORN, GUITAR, {**JAZZ, "sources": ["s9"]}], DROPS), {}),
        "an ID number in 证件和硬期限": (
            _answer(
                [
                    BORN, GUITAR,
                    {"section": "证件和硬期限", "text": "护照 E1234567", "sources": ["s1"]},
                ],
                DROPS,
            ),
            {},
        ),
        "over a section cap": (
            _answer([BORN, GUITAR, JAZZ], DROPS),
            {"section_caps": {**core_memory.SECTION_CAPS, "兴趣和品味": 10}},
        ),
        "over the whole cap": (_answer([BORN, GUITAR, JAZZ], DROPS), {"max_chars": 30}),
    }


@pytest.mark.parametrize("case", list(_bad_rebuilds()))
def test_a_rebuild_that_fails_a_gate_lands_nothing(note: Path, case: str) -> None:
    """Any failed gate: no changes, a reason, and no version."""
    answer, core = _bad_rebuilds()[case]
    rows = len(_versions(note))

    proposal = _propose(note, answer, **core)

    assert proposal.changes is None
    assert proposal.why
    assert not land(MemorySettings(db_path=note, audio_dir=note.parent / "audio"), proposal)
    assert len(_versions(note)) == rows


def test_a_rebuild_that_cites_a_kept_item_keeps_its_records_and_its_day(note: Path) -> None:
    """A pinned item kept word for word with a new citation adds the record; its day stays."""
    born = {**BORN, "sources": ["s1"]}
    proposal = _propose(note, _answer([born, GUITAR, JAZZ], DROPS))
    assert proposal.why is None
    assert land(MemorySettings(db_path=note, audio_dir=note.parent / "audio"), proposal)

    item = _stored(note, "生日 3月5日")
    assert item["sources"] == [record_id(3, 1)]
    assert (item["since"], item["pinned"]) == ("2026-09-01", True)
    entries = memory_page.overview(note, max_chars=40000, booted_max_chars=40000)["new"]["entries"]
    assert "生日 3月5日" not in {e["text"] for e in entries}  # nothing to show for it


def test_a_rejected_monthly_rebuild_is_tried_once_that_month(tmp_path: Path) -> None:
    """Two calls on the first night, none on the next; the latest month counts, not the last row."""
    path = tmp_path / "memory.db"
    log = tmp_path / "events.db"
    open_event_log(log).close()
    last_month = (TODAY.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    older = (TODAY.replace(day=1) - timedelta(days=70)).strftime("%Y-%m")
    month_end = month_days(last_month)[1]
    city = _item("city", "住在 Victoria", "2026-09-01", pinned=True)
    _seed(path, {"经历": [city]}, upto=month_end)
    memory = MemorySettings(db_path=path, audio_dir=tmp_path / "audio")
    newer = (TODAY.replace(day=1) - timedelta(days=40)).strftime("%Y-%m")
    for month in (older, newer, older):
        assert land(memory, Proposal(current_core_memory(path).id, month, month_end, [], None))
    assert last_rebuild_month(path) == newer
    rejecting = ScriptedModel(otherwise=Reply(_answer([], [])), model="fake-rebuilder")
    settings = CoreMemorySettings(preset="c", prompt="c", rebuild=REBUILD)

    def run(day: int) -> str:
        return run_rebuild(
            memory, settings, rejecting.as_llm_client(), event_log_path=log, pricing_table={},
            today=TODAY + timedelta(days=day),
        )

    assert run(0).startswith("rejected")
    assert "tried this month" in run(0)
    assert len(rejecting.requests) == 2


def test_a_rebuild_cut_off_by_the_output_limit_is_rejected(note: Path) -> None:
    """A whole note cut off at the output limit is never applied, however good the start."""
    doc = current_core_memory(note).doc
    answer = _answer([BORN, GUITAR, JAZZ], DROPS)
    assert check_rebuild(
        answer, "max_tokens", doc=doc, labels={"s1": "x", "s2": "y"}, label_days={},
        his_labels=set(), kinds={}, max_chars=40000, day=TODAY.isoformat(),
    ) == (None, "cut off by the output limit")


def test_the_month_rebuilds_itself_only_after_a_first_rebuild_and_once(tmp_path: Path) -> None:
    """Due on the first run after the month's last day is consolidated; never before a hand run."""
    path = tmp_path / "memory.db"
    last_month = (TODAY.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    month_end = month_days(last_month)[1]
    city = _item("city", "住在 Victoria", "2026-09-01", topic="城市")
    _seed(path, {"经历": [city]}, upto=month_end)
    memory = MemorySettings(db_path=path, audio_dir=tmp_path / "audio")
    assert rebuild_due(memory, TODAY) is None  # no first rebuild yet

    before = (TODAY.replace(day=1) - timedelta(days=40)).strftime("%Y-%m")
    base = current_core_memory(path)
    assert land(memory, Proposal(base.id, before, month_end, [], None))
    rebuilder = ScriptedModel(
        otherwise=Reply(_answer([{"item": 1, "section": "经历", "text": "住在 Victoria"}], [])),
        model="fake-rebuilder",
    )
    schedule = core_schedule(tmp_path, consolidator(), rebuild_model=rebuilder, rebuild=REBUILD)

    schedule.write(TODAY)
    schedule.write(TODAY + timedelta(days=1))

    assert len(rebuilder.requests) == 1
    assert f"[Day summaries of {last_month}" in str(rebuilder.requests[0].messages[0]["content"])
    assert last_rebuild_month(path) == last_month
    assert [origin for origin, _c in _versions(path)][-2:] == ["rebuild", "rebuild"]


def test_the_hand_run_lands_exactly_the_saved_changes_once(note: Path) -> None:
    """``--apply`` lands a saved proposal; the same file again finds the note changed."""
    proposal = _propose(note, _answer([BORN, GUITAR, JAZZ], DROPS))
    saved = note.parent / "rebuild.json"
    saved.write_text(json.dumps(proposal._asdict(), ensure_ascii=False))
    root = ["--runtime-root", str(note.parent), "--apply", str(saved)]

    assert main(root) == 0
    assert _versions(note)[-1][0] == "rebuild"
    assert main(root) == 1
