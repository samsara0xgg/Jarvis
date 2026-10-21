"""Core memory (ADR 0146): migration, rendering, ``remember`` versions and the nightly pass.

Real memory.db, real ``DaySummarySchedule`` and the real ``remember`` tool through the
default registry; only the consolidator's provider call is faked, by a client that
answers per day from a script and records what it was asked.
"""

from __future__ import annotations

import contextlib
import json
from contextlib import closing
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.surrogate_route import KEY_ENV, JevLog, SurrogateRoute
from jarvis.runtime.core_memory import CoreMemorySettings
from jarvis.state import core_memory
from jarvis.state.memory_db import (
    brief_note,
    core_memory_pending_days,
    current_core_memory,
    due_items,
    open_memory_db,
    remember_fact,
    render_context,
    set_user_name,
    user_name,
)
from tests.support.core_memory import Consolidator, consolidator_changes, core_schedule, seed_days
from tests.support.daily_tools import DailyHarness
from tests.support.day_summaries import (
    TODAY,
    day_ago,
    insert_records,
    record_id,
    stamp_ago,
)
from tests.support.net import start_http_server, stop_http_server

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.decision.llm import ChatResult
    from jarvis.runtime.day_summary import DaySummarySchedule

LEGACY_TS = "2026-09-01T09:00:00-07:00"


def _versions(path: Path) -> list[tuple[str, str | None, list[dict[str, Any]]]]:
    """``(origin, upto_day, changes)`` of every version, oldest first."""
    with closing(open_memory_db(path)) as conn:
        rows = conn.execute("SELECT origin, upto_day, changes FROM core_memory ORDER BY rowid")
        return [(origin, upto, json.loads(changes)) for origin, upto, changes in rows]


def _items(path: Path) -> dict[str, list[str]]:
    doc = current_core_memory(path).doc
    return {section: [item["text"] for item in doc[section]] for section in doc if doc[section]}


def test_the_first_read_migrates_profile_and_prompts_render_core_memory(tmp_path: Path) -> None:
    """Every profile row becomes a 关于你 item; profile stays but is no longer read."""
    path = tmp_path / "memory.db"
    with closing(open_memory_db(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO profile (id, ts, text) VALUES (?, ?, ?)",
            [
                ("profile-name", LEGACY_TS, "The user's name is Ada."),
                ("seed-1", LEGACY_TS, "Prefers short answers."),
                ("fact:送餐地址", LEGACY_TS, "送餐地址: 1 Test St"),
            ],
        )
    # ADR 0201: each line carries its first-seen date and kind.
    rendered = (
        "### 经历\n- (2026-09-01, set) The user's name is Ada."
        "\n- (2026-09-01, set) Prefers short answers.\n- (2026-09-01, set) 送餐地址: 1 Test St"
    )
    assert render_context(path, exclude_id="").profile == f"[About the user]\n{rendered}"
    assert f"[About the user]\n{rendered}\n" in brief_note(path, max_chars=10_000)

    version = current_core_memory(path)
    assert [(i["topic"], i["since"], i["sources"]) for i in version.doc["经历"]] == [
        ("name", "2026-09-01", []),
        (None, "2026-09-01", []),
        ("送餐地址", "2026-09-01", []),
    ]
    assert user_name(path) == "Ada"
    # One migration version, however many reads; profile is untouched and no longer read.
    assert [origin for origin, _, _ in _versions(path)] == ["migration"]
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute("UPDATE profile SET text = 'changed'")
        assert conn.execute("SELECT count(*) FROM profile").fetchone() == (3,)
    assert render_context(path, exclude_id="").profile == f"[About the user]\n{rendered}"

    # A tight Live brief drops whole items, never leaving a heading with nothing under it.
    remember_fact(path, "晨跑", "每天 6 点", "生活里的几件事")
    full = brief_note(path, max_chars=10_000)
    tight = brief_note(path, max_chars=len(full) - 1).splitlines()
    assert "### 生活里的几件事" not in tight
    assert "- (2026-09-01, set) 送餐地址: 1 Test St" in tight


def test_an_empty_store_renders_no_block_and_a_rendered_item_has_no_ids(tmp_path: Path) -> None:
    """No items is no block; the numbered form is for the consolidator only."""
    path = tmp_path / "memory.db"
    open_memory_db(path).close()
    assert render_context(path, exclude_id="").profile == ""
    remember_fact(path, "a", "1")
    remember_fact(path, "b", "2", "兴趣和品味")
    doc = current_core_memory(path).doc
    assert core_memory.render(doc) == "### 经历\n- a: 1\n### 兴趣和品味\n- b: 2"
    assert core_memory.numbered(doc) == (
        "### 经历 (6/4000)\n[1] a: 1\n### 兴趣和品味 (6/2000)\n[2] b: 2"
    )


def test_remember_replaces_the_same_topic_adds_a_new_one_and_honours_the_section(
    tmp_path: Path,
) -> None:
    """Each write is one version; the name line is topic ``name`` under 关于你."""
    path = tmp_path / "memory.db"
    set_user_name(path, "Ada")
    remember_fact(path, "送餐地址", "1 Test St")
    remember_fact(path, "饮食偏好", "不吃香菜", "兴趣和品味")
    remember_fact(path, "送餐地址", "2 Sample Rd")  # replaced where it was
    assert _items(path) == {
        "经历": ["The user's name is Ada.", "送餐地址: 2 Sample Rd"],
        "兴趣和品味": ["饮食偏好: 不吃香菜"],
    }
    remember_fact(path, "饮食偏好", "不吃香菜和辣", "定下来的规矩")  # explicit section moves it
    remember_fact(path, "送餐地址", "3 Elm Ave")  # no section: stays where it is
    assert _items(path) == {
        "经历": ["The user's name is Ada.", "送餐地址: 3 Elm Ave"],
        "定下来的规矩": ["饮食偏好: 不吃香菜和辣"],
    }
    set_user_name(path, "Bea")
    assert user_name(path) == "Bea"
    assert [origin for origin, _, _ in _versions(path)] == [
        "migration",
        "setup",
        "remember",
        "remember",
        "remember",
        "remember",
        "remember",
        "setup",
    ]
    last = _versions(path)[-1]
    assert last[2] == [
        {
            "op": "rewrite",
            "topic": "name",
            "text": "The user's name is Bea.",
            "sources": [],
        },
    ]
    version = current_core_memory(path)
    assert [i["since"] for i in version.doc["经历"]] == [
        datetime.now().astimezone().date().isoformat()
    ] * 2


@pytest.fixture
def daily(tmp_path: Path) -> Iterator[DailyHarness]:
    """One isolated persisted runtime per check."""
    harness = DailyHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.fx.close()


def test_keep_writes_a_fact_and_says_so(daily: DailyHarness) -> None:
    """``keep`` files a line in core memory; a topic replaces its item, the same text nothing."""
    tool = next(t for t in daily.tools if t.name == "keep")
    assert set(tool.input_schema["properties"]) == {"text", "topic", "section"}

    saved = daily.call("keep", {"topic": "老板", "text": "Sam"})
    assert saved["summary"] == 'Saved to long-term memory: "老板: Sam".'
    daily.call("keep", {"text": "口味 辣"})  # no topic: the text alone
    assert _items(daily.memory) == {"经历": ["老板: Sam", "口味 辣"]}

    again = daily.call("keep", {"topic": "老板", "text": "Sam"})
    assert again["summary"] == 'Already in long-term memory, nothing new: "老板: Sam".'
    assert len(_versions(daily.memory)) == 3  # migration + two writes

    daily.call("keep", {"topic": "老板", "text": "Tom"})  # the topic is replaced
    assert _items(daily.memory) == {"经历": ["老板: Tom", "口味 辣"]}
    too_long = daily.call("keep", {"text": "x" * 300})
    assert too_long["code"] == "too_long"
    assert len(_versions(daily.memory)) == 4


# --- the nightly pass ---------------------------------------------------------


def test_the_schedule_consolidates_each_day_in_order_after_the_summaries(tmp_path: Path) -> None:
    """add, rewrite (with a move) and stale apply; an empty list advances ``upto_day``."""
    path = tmp_path / "memory.db"
    seed_days(path, 4, 3, 2, 0)
    remember_fact(path, "城市", "Victoria")
    d4, d3, d2, d0 = (day_ago(n).isoformat() for n in (4, 3, 2, 0))
    model = Consolidator()
    model.script[d4] = [
        consolidator_changes(
            {"op": "add", "section": "兴趣和品味", "text": "喜欢咖啡", "sources": ["r1"]},
            {"op": "add", "section": "生活里的几件事", "text": "写论文", "sources": ["r1"]},
        ),
    ]
    model.script[d3] = [
        consolidator_changes(
            {
                "op": "rewrite",
                "item": 2,
                "text": "  更喜欢\n茶  ",
                "sources": ["r1"],
                "section": "定下来的规矩",
            },
            {"op": "stale", "item": 3, "sources": ["r1"]},
        ),
    ]
    schedule = core_schedule(tmp_path, model)

    schedule.write(TODAY)

    assert model.asked == [d4, d3, d2]  # oldest first; day 1 has no summary; today is not past
    assert "[1] 城市: Victoria" in model.inputs[d4]
    expected = "### 兴趣和品味 (6/2000)\n[2] 喜欢咖啡\n### 生活里的几件事 (5/10000)\n[3] 写论文"
    assert expected in model.inputs[d3]
    assert f"[Summary of {d3}]\n## {d3} (the whole day, word for word)" in model.inputs[d3]
    assert f"r1 | {stamp_ago(3, 9)} | allen | 第3天说的话" in model.inputs[d3]
    assert _items(path) == {"经历": ["城市: Victoria"], "定下来的规矩": ["更喜欢 茶"]}
    after = [v[:2] for v in _versions(path)]
    assert after == [
        ("migration", None),
        ("remember", None),
        ("nightly", d4),
        ("nightly", d3),
        ("nightly", d2),
    ]
    # The empty answer appended the same document, with no changes, to advance upto_day.
    assert _versions(path)[-1][2] == []
    assert current_core_memory(path).upto_day == d2
    rewritten = current_core_memory(path).doc["定下来的规矩"][0]
    assert (rewritten["since"], rewritten["sources"], rewritten["topic"]) == (
        d3,
        [record_id(3, 1)],
        None,
    )

    # The next run starts after upto_day: only the day that has a summary since.
    model.asked.clear()
    schedule.write(TODAY + timedelta(days=1))
    assert model.asked == [d0]
    assert current_core_memory(path).upto_day == d0


def _bad_answers() -> dict[str, tuple[str, dict[str, Any]]]:
    rid = "r1"
    stale = {"op": "stale", "item": 1, "sources": [rid]}
    return {
        "not json": ("sure, here you go", {}),
        "no changes list": ('{"edits": []}', {}),
        "unknown item": (
            consolidator_changes({"op": "stale", "item": 9, "sources": [rid]}),
            {},
        ),
        "foreign record id": (
            consolidator_changes(
                {"op": "add", "section": "兴趣和品味", "text": "x", "sources": ["r2"]}
            ),
            {},
        ),
        "a real record id instead of a label": (
            consolidator_changes(
                {"op": "add", "section": "兴趣和品味", "text": "x", "sources": [record_id(3, 1)]}
            ),
            {},
        ),
        "no sources": (
            consolidator_changes(
                {"op": "add", "section": "兴趣和品味", "text": "x", "sources": []}
            ),
            {},
        ),
        "bad section": (
            consolidator_changes({"op": "add", "section": "其他", "text": "x", "sources": [rid]}),
            {},
        ),
        "item targeted twice": (
            consolidator_changes(
                stale, {"op": "rewrite", "item": 1, "text": "y", "sources": [rid]}
            ),
            {},
        ),
        "too many stale": (
            consolidator_changes(stale, {"op": "stale", "item": 2, "sources": [rid]}),
            {"max_stale": 1},
        ),
        "over its section cap": (
            consolidator_changes(
                {"op": "add", "section": "身体", "text": "长" * 100, "sources": [rid]}
            ),
            {"section_caps": {**core_memory.SECTION_CAPS, "身体": 99}},
        ),
        "an ID number in 证件和硬期限": (
            consolidator_changes(
                {"op": "add", "section": "证件和硬期限", "text": "护照 E1234567", "sources": [rid]},
            ),
            {},
        ),
        "over max_chars": (
            consolidator_changes(
                {"op": "add", "section": "兴趣和品味", "text": "长" * 100, "sources": [rid]}
            ),
            {"max_chars": 60},
        ),
    }


@pytest.mark.parametrize("case", list(_bad_answers()))
def test_a_rejected_answer_stores_nothing_and_is_asked_once_more(
    tmp_path: Path,
    case: str,
) -> None:
    """The whole list is dropped on any gate; the day stays open for the next run."""
    path = tmp_path / "memory.db"
    seed_days(path, 3, 2)
    remember_fact(path, "城市", "Victoria")
    remember_fact(path, "口味", "辣")
    answer, limits = _bad_answers()[case]
    d3, d2 = day_ago(3).isoformat(), day_ago(2).isoformat()
    model = Consolidator()
    model.script[d3] = [answer]
    before = _versions(path)

    core_schedule(tmp_path, model, **limits).write(TODAY)

    assert model.asked == [d3, d3]  # asked once more, then the chain stops before day 2
    assert _versions(path) == before
    assert current_core_memory(path).upto_day is None
    assert core_memory_pending_days(path, TODAY) == [d3, d2]


def test_sources_are_short_labels_in_time_order_and_stored_as_real_ids(tmp_path: Path) -> None:
    """The model sees r1, r2, ... and never the 32-hex ids; the store keeps the real ones."""
    path = tmp_path / "memory.db"
    insert_records(
        path,
        [
            (
                record_id(3, 2),
                stamp_ago(3, 9, 1),
                "jarvis",
                "好的",
            ),  # inserted first, later in time
            (record_id(3, 1), stamp_ago(3, 9), "allen", "我喜欢茶"),
        ],
    )
    d3 = day_ago(3).isoformat()
    model = Consolidator()
    model.script[d3] = [
        consolidator_changes(
            {"op": "add", "section": "兴趣和品味", "text": "喜欢茶", "sources": ["r1", "r2"]}
        ),
    ]
    core_schedule(tmp_path, model).write(TODAY)
    assert (
        f"r1 | {stamp_ago(3, 9)} | allen | 我喜欢茶\nr2 | {stamp_ago(3, 9, 1)} | jarvis | 好的"
        in (model.inputs[d3])
    )
    assert record_id(3, 1) not in model.inputs[d3]
    assert current_core_memory(path).doc["兴趣和品味"][0]["sources"] == [
        record_id(3, 1),
        record_id(3, 2),
    ]
    nightly = _versions(path)[-1]
    assert nightly[2][0]["sources"] == [record_id(3, 1), record_id(3, 2)]


def test_a_second_attempt_that_passes_lands(tmp_path: Path) -> None:
    """The retry reads the same input and may fix the answer."""
    path = tmp_path / "memory.db"
    seed_days(path, 3)
    d3 = day_ago(3).isoformat()
    model = Consolidator()
    model.script[d3] = [
        "nope",
        "```json\n"
        + consolidator_changes(
            {"op": "add", "section": "生活里的几件事", "text": "写完论文", "sources": ["r1"]},
        )
        + "\n```",
    ]
    core_schedule(tmp_path, model).write(TODAY)
    assert model.asked == [d3, d3]
    assert _items(path) == {"生活里的几件事": ["写完论文"]}


def test_a_failing_day_stops_the_chain_and_the_next_run_resumes_from_it(tmp_path: Path) -> None:
    """Later days build on earlier ones, so none runs past a failure."""
    path = tmp_path / "memory.db"
    seed_days(path, 4, 3, 2)
    d4, d3, d2 = (day_ago(n).isoformat() for n in (4, 3, 2))
    model = Consolidator()
    model.script[d4] = [
        consolidator_changes(
            {"op": "add", "section": "兴趣和品味", "text": "喜欢咖啡", "sources": ["r1"]}
        ),
    ]
    model.script[d3] = [TimeoutError("provider unreachable")]
    schedule = core_schedule(tmp_path, model)

    schedule.write(TODAY)
    assert model.asked == [d4, d3]  # d2 is not asked
    assert current_core_memory(path).upto_day == d4
    assert core_memory_pending_days(path, TODAY) == [d3, d2]

    model.script[d3] = [
        consolidator_changes({"op": "stale", "item": 1, "sources": ["r1"]}),
    ]
    model.asked.clear()
    schedule.write(TODAY + timedelta(days=1))
    assert model.asked == [d3, d2]
    assert _items(path) == {}
    assert current_core_memory(path).upto_day == d2


def test_a_remember_during_the_call_makes_the_answer_stale(tmp_path: Path) -> None:
    """Item numbers belong to the version the model saw; a newer version drops the answer."""
    path = tmp_path / "memory.db"
    seed_days(path, 3)
    remember_fact(path, "城市", "Victoria")
    d3 = day_ago(3).isoformat()

    class _Racing(Consolidator):
        def chat(self, *, messages: list[dict[str, Any]], **kwargs: object) -> ChatResult:
            if not self.asked:
                remember_fact(path, "口味", "辣")  # lands between the read and the write
            return super().chat(messages=messages, **kwargs)

    model = _Racing()
    model.script[d3] = [
        consolidator_changes({"op": "stale", "item": 1, "sources": ["r1"]}),
        '{"changes": []}',
    ]
    core_schedule(tmp_path, model).write(TODAY)
    assert model.asked == [d3, d3]  # the first answer was refused; the retry saw both items
    assert _items(path) == {"经历": ["城市: Victoria", "口味: 辣"]}
    assert current_core_memory(path).upto_day == d3


def test_consolidation_is_off_without_a_core_memory_block() -> None:
    """Absent block (or no preset/prompt) is no consolidation; the defaults are 4000 and 5."""
    assert CoreMemorySettings.from_config(None) is None
    assert CoreMemorySettings.from_config({"preset": "p"}) is None
    on = CoreMemorySettings.from_config({"preset": "p", "prompt": " x "})
    assert on is not None
    assert (on.prompt, on.max_chars, on.max_stale) == ("x", 40000, 5)


# --- the Jev review gate --------------------------------------------------------


class _Jev:
    """A fake decisions endpoint: records each request and answers by a marker in the item."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(request)
                text = request["state"]
                choice, confidence = (
                    ("keep", 0.95)
                    if "HIGH" in text
                    else ("keep", 0.6)
                    if "LOW" in text
                    else ("one_off", 0.99)
                )
                body = json.dumps(
                    {
                        "answers": {"review": {"choice": choice, "confidence": confidence}},
                        "usage": {"cost": 0.00003},
                    },
                ).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError):
                    self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.server = start_http_server(Handler)

    def route(self, log: JevLog | None = None) -> SurrogateRoute:
        return SurrogateRoute(
            model="m",
            min_confidence=1.0,
            timeout_ms=2000,
            url=f"http://127.0.0.1:{self.server.server_port}/",
            log=log,
        )


@pytest.fixture
def jev(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Jev]:
    """A fake Jev endpoint and a key in the environment."""
    monkeypatch.setenv(KEY_ENV, "test-key-not-a-secret")
    fake = _Jev()
    try:
        yield fake
    finally:
        stop_http_server(fake.server)


def _reviewed(
    tmp_path: Path,
    model: Consolidator,
    route: SurrogateRoute | None,
    review_min_confidence: float | None = None,
    *,
    review_drop: bool = False,
) -> DaySummarySchedule:
    schedule = core_schedule(tmp_path, model)
    schedule._core_memory = CoreMemorySettings(  # noqa: SLF001 — the review knobs under test
        preset="c",
        prompt="consolidate",
        review_min_confidence=review_min_confidence,
        review_drop=review_drop,
    )
    schedule._review = route  # noqa: SLF001 — the transport seam
    return schedule


def test_jev_keeps_a_confident_keep_and_drops_only_the_rest_with_a_log(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """A keep at the bar lands; a lower keep or another choice drops just that change, logged."""
    path = tmp_path / "memory.db"
    seed_days(path, 3)
    remember_fact(path, "城市", "Victoria")
    d3 = day_ago(3).isoformat()
    kept = {"op": "add", "section": "兴趣和品味", "text": "HIGH 喜欢咖啡", "sources": ["r1"]}
    unsure = {"op": "add", "section": "兴趣和品味", "text": "LOW 喜欢茶", "sources": ["r1"]}
    joke = {"op": "rewrite", "item": 1, "text": "JOKE 住在火星", "sources": ["r1"]}
    stale = {"op": "stale", "item": 1, "sources": ["r1"]}
    model = Consolidator()
    model.script[d3] = [consolidator_changes(kept, unsure, joke)]
    jev_log = tmp_path / "jev.jsonl"

    _reviewed(
        tmp_path,
        model,
        jev.route(JevLog(jev_log)),
        review_min_confidence=0.9,
        review_drop=True,
    ).write(TODAY)

    assert _items(path) == {"经历": ["城市: Victoria"], "兴趣和品味": ["HIGH 喜欢咖啡"]}
    logged = _versions(path)[-1][2]
    assert [c["op"] for c in logged] == ["add", "dropped", "dropped"]
    assert logged[1] == {
        "op": "dropped",
        "change": {**unsure, "sources": [record_id(3, 1)]},
        "choice": "keep",
        "confidence": 0.6,
    }
    assert (logged[2]["choice"], logged[2]["confidence"]) == ("one_off", 0.99)
    assert current_core_memory(path).upto_day == d3
    # The question carries the item, its section (a rewrite's from the document) and the line.
    states = sorted(r["state"] for r in jev.requests)
    assert len(states) == 3
    joke_state = (
        f"Section: 经历\nItem: JOKE 住在火星\nCited lines:\n{stamp_ago(3, 9)} | allen | 第3天说的话"
    )
    assert joke_state in states
    assert record_id(3, 1) not in "".join(states)
    assert set(jev.requests[0]["questions"]["review"]["criteria"]) == {
        "keep",
        "garbled",
        "one_off",
        "not_users",
    }
    # Every call is in the Jev dataset, input and output.
    lines = [json.loads(line) for line in jev_log.read_text().splitlines()]
    assert [(line["use"], line["kind"]) for line in lines] == [("core_memory", "call")] * 3
    assert {line["answers"]["review"]["choice"] for line in lines} == {"keep", "one_off"}

    # A stale change is never reviewed.
    jev.requests.clear()
    seed_days(path, 2)
    d2 = day_ago(2).isoformat()
    model.script[d2] = [consolidator_changes({**stale, "item": 1})]
    _reviewed(
        tmp_path,
        model,
        jev.route(),
        review_min_confidence=0.9,
        review_drop=True,
    ).write(TODAY)
    assert jev.requests == []
    assert _items(path) == {"兴趣和品味": ["HIGH 喜欢咖啡"]}


@pytest.mark.parametrize("failure", ["http", "no_key"])
def test_a_jev_failure_rejects_the_day_like_a_gate_and_stops_the_chain(
    tmp_path: Path,
    jev: _Jev,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Nothing is stored, the day is asked once more, and the next day is not reached."""
    path = tmp_path / "memory.db"
    seed_days(path, 3, 2)
    d3, d2 = day_ago(3).isoformat(), day_ago(2).isoformat()
    model = Consolidator()
    model.script[d3] = [
        consolidator_changes(
            {"op": "add", "section": "兴趣和品味", "text": "HIGH 喜欢咖啡", "sources": ["r1"]},
        ),
    ]
    if failure == "http":
        jev.status = 500
    else:
        monkeypatch.delenv(KEY_ENV)
    current_core_memory(path)  # the first read migrates; that version is not the night's
    before = _versions(path)

    schedule = _reviewed(tmp_path, model, jev.route(), review_min_confidence=0.9, review_drop=True)
    schedule.write(TODAY)

    assert model.asked == [d3, d3]
    assert _versions(path) == before
    assert core_memory_pending_days(path, TODAY) == [d3, d2]

    # Jev back: the next run lands the day.
    jev.status = 200
    monkeypatch.setenv(KEY_ENV, "test-key-not-a-secret")
    model.asked.clear()
    schedule.write(TODAY)
    assert model.asked == [d3, d2]
    assert _items(path) == {"兴趣和品味": ["HIGH 喜欢咖啡"]}


def test_log_only_review_lands_every_change_and_stores_each_verdict(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """Without review_drop nothing is dropped; each add and rewrite gets a reviewed entry."""
    path = tmp_path / "memory.db"
    seed_days(path, 3)
    remember_fact(path, "城市", "Victoria")
    d3 = day_ago(3).isoformat()
    kept = {"op": "add", "section": "兴趣和品味", "text": "HIGH 喜欢咖啡", "sources": ["r1"]}
    joke = {"op": "rewrite", "item": 1, "text": "JOKE 住在火星", "sources": ["r1"]}
    model = Consolidator()
    model.script[d3] = [consolidator_changes(kept, joke)]
    jev_log = tmp_path / "jev.jsonl"

    _reviewed(tmp_path, model, jev.route(JevLog(jev_log)), review_min_confidence=0.9).write(TODAY)

    assert _items(path) == {"经历": ["JOKE 住在火星"], "兴趣和品味": ["HIGH 喜欢咖啡"]}
    logged = _versions(path)[-1][2]
    assert [c["op"] for c in logged] == ["add", "rewrite", "reviewed", "reviewed"]
    assert logged[2:] == [
        {"op": "reviewed", "target": 0, "choice": "keep", "confidence": 0.95},
        {"op": "reviewed", "target": 1, "choice": "one_off", "confidence": 0.99},
    ]
    assert len(jev.requests) == 2
    assert len(jev_log.read_text().splitlines()) == 2  # both calls are in the Jev dataset


@pytest.mark.parametrize("failure", ["http", "no_key"])
def test_log_only_review_failure_lands_the_day_unreviewed(
    tmp_path: Path,
    jev: _Jev,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A Jev failure is a warning: the changes land with no reviewed entries."""
    path = tmp_path / "memory.db"
    seed_days(path, 3)
    d3 = day_ago(3).isoformat()
    model = Consolidator()
    model.script[d3] = [
        consolidator_changes(
            {"op": "add", "section": "兴趣和品味", "text": "HIGH 喜欢咖啡", "sources": ["r1"]},
        ),
    ]
    if failure == "http":
        jev.status = 500
    else:
        monkeypatch.delenv(KEY_ENV)

    _reviewed(tmp_path, model, jev.route(), review_min_confidence=0.9).write(TODAY)

    assert model.asked == [d3]  # no retry: the day landed
    assert _items(path) == {"兴趣和品味": ["HIGH 喜欢咖啡"]}
    assert [c["op"] for c in _versions(path)[-1][2]] == ["add"]
    assert current_core_memory(path).upto_day == d3


def test_review_is_off_without_review_min_confidence(tmp_path: Path, jev: _Jev) -> None:
    """No key in the block, no Jev call; the block parses the bar, model and timeout."""
    path = tmp_path / "memory.db"
    seed_days(path, 3)
    d3 = day_ago(3).isoformat()
    model = Consolidator()
    model.script[d3] = [
        consolidator_changes(
            {"op": "add", "section": "兴趣和品味", "text": "JOKE 住在火星", "sources": ["r1"]},
        ),
    ]
    _reviewed(tmp_path, model, jev.route()).write(TODAY)
    assert jev.requests == []
    assert _items(path) == {"兴趣和品味": ["JOKE 住在火星"]}

    off = CoreMemorySettings.from_config({"preset": "p", "prompt": "x"})
    on = CoreMemorySettings.from_config(
        {"preset": "p", "prompt": "x", "review_min_confidence": 0.8},
    )
    assert off is not None
    assert not off.review_drop
    assert (off.review_min_confidence, off.review_model, off.review_timeout_ms) == (
        None,
        "typesafe/jev-1.13",
        10_000,
    )
    assert on is not None
    assert on.review_min_confidence == 0.8


def test_a_version_with_the_old_six_names_loads_into_the_ten_without_losing_an_item(
    tmp_path: Path,
) -> None:
    """ADR 0258, 0299: four sections are renamed, 定下来的规矩 stays, 承诺和待办 loads as none.

    The retired section's item is still readable raw, for the carry-in.
    """
    path = tmp_path / "memory.db"
    old = {
        "关于你": ["a"],
        "偏好": ["b"],
        "常提到的人": ["c"],
        "正在做的事": ["d"],
        "定下来的规矩": ["e"],
        "承诺和待办": ["f"],
    }
    raw: dict[str, Any] = {
        "sections": {
            s: [{"text": t, "topic": None, "sources": [], "since": "x"} for t in ts]
            for s, ts in old.items()
        },
    }
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute(
            "INSERT INTO core_memory (id, ts, base_id, origin, upto_day, doc, changes, chars) "
            "VALUES ('core:old', ?, NULL, 'nightly', NULL, ?, '[]', 0)",
            (LEGACY_TS, json.dumps(raw, ensure_ascii=False)),
        )
    assert _items(path) == {
        "经历": ["a"],
        "兴趣和品味": ["b"],
        "身边的人": ["c"],
        "生活里的几件事": ["d"],
        "定下来的规矩": ["e"],
    }
    assert [item["text"] for item in due_items(path)] == ["f"]


def test_a_section_lists_pinned_items_first_then_the_newest_and_numbers_follow() -> None:
    """Rank: pinned, then most recent ``since``; ``numbered`` and ``listing`` agree."""
    doc = core_memory.empty_doc()
    doc["身边的人"] = [
        {"text": "old", "topic": None, "sources": [], "since": "2026-01-01"},
        {"text": "new", "topic": None, "sources": [], "since": "2026-03-01"},
        {"text": "pin", "topic": None, "sources": [], "since": "2026-02-01", "pinned": True},
    ]
    ranked = core_memory.apply_changes(doc, [], "2026-04-01")
    assert [i["text"] for i in ranked["身边的人"]] == ["pin", "new", "old"]
    assert core_memory.numbered(ranked) == (
        "### 身边的人 (17/10000)\n[1] (pinned) pin\n[2] new\n[3] old"
    )
    assert [item["text"] for _s, item, _i in core_memory.listing(ranked)] == ["pin", "new", "old"]


def test_a_rewrite_keeps_the_old_wording_with_the_day_it_held(tmp_path: Path) -> None:
    """Nightly rewrite and a same-topic ``remember`` both leave ``history`` on the item."""
    path = tmp_path / "memory.db"
    remember_fact(path, "城市", "Victoria")
    first = current_core_memory(path).doc["经历"][0]
    remember_fact(path, "城市", "Seattle")
    item = current_core_memory(path).doc["经历"][0]
    assert item["text"] == "城市: Seattle"
    assert item["history"] == [{"text": "城市: Victoria", "since": first["since"]}]

    doc = core_memory.apply_changes(
        current_core_memory(path).doc,
        [{"op": "rewrite", "item": 1, "text": "城市: Tacoma", "sources": ["x"]}],
        "2026-10-20",
    )
    rewritten = doc["经历"][0]
    assert [h["text"] for h in rewritten["history"]] == ["城市: Victoria", "城市: Seattle"]
    assert rewritten["history"][1]["since"] == item["since"]
    assert "Victoria" not in core_memory.render(doc)  # the prompt shows the current wording only


def test_a_date_is_not_an_id_number_but_a_long_run_of_digits_is() -> None:
    """证件和硬期限 refuses 6 or more digits in a row, dates apart; other sections do not."""
    core_memory.check_item("证件和硬期限", "护照 2026-10-15 到期")
    core_memory.check_item("证件和硬期限", "签证 2026/10/15 到期, 申请号 12345")
    core_memory.check_item("钱", "卡号 1234567890")
    with pytest.raises(ValueError, match="dates only"):
        core_memory.check_item("证件和硬期限", "护照 123456")
