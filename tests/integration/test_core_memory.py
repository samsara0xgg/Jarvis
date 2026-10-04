"""Core memory (ADR 0146): migration, rendering, ``remember`` versions and the nightly pass.

Real memory.db, real ``DaySummarySchedule`` and the real ``remember`` tool through the
default registry; only the consolidator's provider call is faked, by a client that
answers per day from a script and records what it was asked.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from contextlib import closing
from datetime import datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.core_memory import date_stale
from jarvis.decision.llm import ChatResult
from jarvis.decision.surrogate_route import KEY_ENV, JevLog, SurrogateRoute
from jarvis.runtime.core_memory import CoreMemorySettings
from jarvis.runtime.day_summary import DaySummarySchedule, DaySummarySettings
from jarvis.state import core_memory
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    brief_note,
    core_memory_pending_days,
    current_core_memory,
    open_memory_db,
    remember_fact,
    render_context,
    set_user_name,
    user_name,
)
from tests.integration.test_daily_tools import DailyHarness
from tests.integration.test_day_summaries import TODAY, _day, _insert, _rid, _stamp, _Summariser

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

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
    rendered = (
        "### 关于你\n- The user's name is Ada.\n- Prefers short answers.\n- 送餐地址: 1 Test St"
    )
    assert render_context(path, exclude_id="").profile == f"[About the user]\n{rendered}"
    assert f"[About the user]\n{rendered}\n" in brief_note(path, max_chars=10_000)

    version = current_core_memory(path)
    assert [(i["topic"], i["since"], i["sources"]) for i in version.doc["关于你"]] == [
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
    remember_fact(path, "晨跑", "每天 6 点", "承诺和待办")
    full = brief_note(path, max_chars=10_000)
    tight = brief_note(path, max_chars=len(full) - 1).splitlines()
    assert "### 承诺和待办" not in tight
    assert "- 送餐地址: 1 Test St" in tight


def test_an_empty_store_renders_no_block_and_a_rendered_item_has_no_ids(tmp_path: Path) -> None:
    """No items is no block; the numbered form is for the consolidator only."""
    path = tmp_path / "memory.db"
    open_memory_db(path).close()
    assert render_context(path, exclude_id="").profile == ""
    remember_fact(path, "a", "1")
    remember_fact(path, "b", "2", "偏好")
    doc = current_core_memory(path).doc
    assert core_memory.render(doc) == "### 关于你\n- a: 1\n### 偏好\n- b: 2"
    assert core_memory.numbered(doc) == "### 关于你\n[1] a: 1\n### 偏好\n[2] b: 2"


def test_remember_replaces_the_same_topic_adds_a_new_one_and_honours_the_section(
    tmp_path: Path,
) -> None:
    """Each write is one version; the name line is topic ``name`` under 关于你."""
    path = tmp_path / "memory.db"
    set_user_name(path, "Ada")
    remember_fact(path, "送餐地址", "1 Test St")
    remember_fact(path, "饮食偏好", "不吃香菜", "偏好")
    remember_fact(path, "送餐地址", "2 Sample Rd")  # replaced where it was
    assert _items(path) == {
        "关于你": ["The user's name is Ada.", "送餐地址: 2 Sample Rd"],
        "偏好": ["饮食偏好: 不吃香菜"],
    }
    remember_fact(path, "饮食偏好", "不吃香菜和辣", "定下来的规矩")  # explicit section moves it
    remember_fact(path, "送餐地址", "3 Elm Ave")  # no section: stays where it is
    assert _items(path) == {
        "关于你": ["The user's name is Ada.", "送餐地址: 3 Elm Ave"],
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
    assert [i["since"] for i in version.doc["关于你"]] == [
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


def test_the_remember_tool_takes_an_optional_section(daily: DailyHarness) -> None:
    """The schema offers the six sections; a bad one is refused and writes nothing."""
    tool = next(t for t in daily.tools if t.name == "remember")
    schema = tool.input_schema
    assert schema["properties"]["section"]["enum"] == list(core_memory.SECTIONS)
    assert schema["required"] == ["topic", "fact"]
    assert all(section in tool.description for section in core_memory.SECTIONS)

    assert daily.call("remember", {"topic": "老板", "fact": "Sam", "section": "常提到的人"}) == {
        "remembered": "老板: Sam",
    }
    daily.call("remember", {"topic": "口味", "fact": "辣"})
    assert _items(daily.memory) == {"关于你": ["口味: 辣"], "常提到的人": ["老板: Sam"]}
    refused = daily.call("remember", {"topic": "x", "fact": "y", "section": "nowhere"})
    assert refused["code"] == "invalid_argument"
    assert len(_versions(daily.memory)) == 3  # migration + two writes


# --- the nightly pass ---------------------------------------------------------


class _Consolidator:
    """Stands in for the core_memory preset's client; ``script`` maps a day to its answers."""

    provider = "openai"
    model = "fake-consolidator"

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.inputs: dict[str, str] = {}
        self.script: dict[str, list[str | Exception]] = {}

    def chat(self, *, messages: list[dict[str, Any]], **_kwargs: object) -> ChatResult:
        text = str(messages[0]["content"])
        day = re.search(r"\[Records of (\S+),", text).group(1)  # type: ignore[union-attr]
        self.asked.append(day)
        self.inputs[day] = text
        answers = self.script.get(day) or ['{"changes": []}']
        answer = answers[min(self.asked.count(day) - 1, len(answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return ChatResult(
            text=answer,
            tool_calls=(),
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
            raw={},
            model_used=self.model,
        )


def _changes(*ops: dict[str, Any]) -> str:
    return json.dumps({"changes": list(ops)}, ensure_ascii=False)


def _schedule(tmp_path: Path, model: _Consolidator, **core: int) -> DaySummarySchedule:
    log = tmp_path / "events.db"
    open_event_log(log).close()
    schedule = DaySummarySchedule(
        memory=MemorySettings(db_path=tmp_path / "memory.db", audio_dir=tmp_path / "audio"),
        settings=DaySummarySettings(at=time(5, 0), preset="p", prompt="summarise", max_chars=500),
        llm_config={},
        event_log_path=log,
        pricing_table={},
        core_memory=CoreMemorySettings(preset="c", prompt="consolidate", **core),
    )
    schedule._client = _Summariser()  # type: ignore[assignment]  # noqa: SLF001 — the provider seam
    schedule._core_client = model  # type: ignore[assignment]  # noqa: SLF001 — the provider seam
    return schedule


def _days(path: Path, *ago: int) -> None:
    """One short user record on each of the given days (kept word for word: no summary call)."""
    _insert(
        path,
        [(_rid(n, 1), _stamp(n, 9), "allen", f"第{n}天说的话") for n in ago],
    )


def test_the_schedule_consolidates_each_day_in_order_after_the_summaries(tmp_path: Path) -> None:
    """add, rewrite (with a move) and stale apply; an empty list advances ``upto_day``."""
    path = tmp_path / "memory.db"
    _days(path, 4, 3, 2, 0)
    remember_fact(path, "城市", "Victoria")
    d4, d3, d2, d0 = (_day(n).isoformat() for n in (4, 3, 2, 0))
    model = _Consolidator()
    model.script[d4] = [
        _changes(
            {"op": "add", "section": "偏好", "text": "喜欢咖啡", "sources": ["r1"]},
            {"op": "add", "section": "正在做的事", "text": "写论文", "sources": ["r1"]},
        ),
    ]
    model.script[d3] = [
        _changes(
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
    schedule = _schedule(tmp_path, model)

    schedule.write(TODAY)

    assert model.asked == [d4, d3, d2]  # oldest first; day 1 has no summary; today is not past
    assert "[1] 城市: Victoria" in model.inputs[d4]
    assert "### 偏好\n[2] 喜欢咖啡\n### 正在做的事\n[3] 写论文" in model.inputs[d3]
    assert f"[Summary of {d3}]\n## {d3} (the whole day, word for word)" in model.inputs[d3]
    assert f"r1 | {_stamp(3, 9)} | allen | 第3天说的话" in model.inputs[d3]
    assert _items(path) == {"关于你": ["城市: Victoria"], "定下来的规矩": ["更喜欢 茶"]}
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
        [_rid(3, 1)],
        None,
    )

    # The next run starts after upto_day: only the day that has a summary since.
    model.asked.clear()
    schedule.write(TODAY + timedelta(days=1))
    assert model.asked == [d0]
    assert current_core_memory(path).upto_day == d0


def _bad_answers() -> dict[str, tuple[str, dict[str, int]]]:
    rid = "r1"
    stale = {"op": "stale", "item": 1, "sources": [rid]}
    return {
        "not json": ("sure, here you go", {}),
        "no changes list": ('{"edits": []}', {}),
        "unknown item": (
            _changes({"op": "stale", "item": 9, "sources": [rid]}),
            {},
        ),
        "foreign record id": (
            _changes({"op": "add", "section": "偏好", "text": "x", "sources": ["r2"]}),
            {},
        ),
        "a real record id instead of a label": (
            _changes({"op": "add", "section": "偏好", "text": "x", "sources": [_rid(3, 1)]}),
            {},
        ),
        "no sources": (_changes({"op": "add", "section": "偏好", "text": "x", "sources": []}), {}),
        "bad section": (
            _changes({"op": "add", "section": "其他", "text": "x", "sources": [rid]}),
            {},
        ),
        "item targeted twice": (
            _changes(stale, {"op": "rewrite", "item": 1, "text": "y", "sources": [rid]}),
            {},
        ),
        "too many stale": (
            _changes(stale, {"op": "stale", "item": 2, "sources": [rid]}),
            {"max_stale": 1},
        ),
        "over max_chars": (
            _changes({"op": "add", "section": "偏好", "text": "长" * 100, "sources": [rid]}),
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
    _days(path, 3, 2)
    remember_fact(path, "城市", "Victoria")
    remember_fact(path, "口味", "辣")
    answer, limits = _bad_answers()[case]
    d3, d2 = _day(3).isoformat(), _day(2).isoformat()
    model = _Consolidator()
    model.script[d3] = [answer]
    before = _versions(path)

    _schedule(tmp_path, model, **limits).write(TODAY)

    assert model.asked == [d3, d3]  # asked once more, then the chain stops before day 2
    assert _versions(path) == before
    assert current_core_memory(path).upto_day is None
    assert core_memory_pending_days(path, TODAY) == [d3, d2]


def test_sources_are_short_labels_in_time_order_and_stored_as_real_ids(tmp_path: Path) -> None:
    """The model sees r1, r2, ... and never the 32-hex ids; the store keeps the real ones."""
    path = tmp_path / "memory.db"
    _insert(
        path,
        [
            (_rid(3, 2), _stamp(3, 9, 1), "jarvis", "好的"),  # inserted first, later in time
            (_rid(3, 1), _stamp(3, 9), "allen", "我喜欢茶"),
        ],
    )
    d3 = _day(3).isoformat()
    model = _Consolidator()
    model.script[d3] = [
        _changes({"op": "add", "section": "偏好", "text": "喜欢茶", "sources": ["r1", "r2"]}),
    ]
    _schedule(tmp_path, model).write(TODAY)
    assert (
        f"r1 | {_stamp(3, 9)} | allen | 我喜欢茶\nr2 | {_stamp(3, 9, 1)} | jarvis | 好的"
        in (model.inputs[d3])
    )
    assert _rid(3, 1) not in model.inputs[d3]
    assert current_core_memory(path).doc["偏好"][0]["sources"] == [_rid(3, 1), _rid(3, 2)]
    nightly = _versions(path)[-1]
    assert nightly[2][0]["sources"] == [_rid(3, 1), _rid(3, 2)]


def test_a_second_attempt_that_passes_lands(tmp_path: Path) -> None:
    """The retry reads the same input and may fix the answer."""
    path = tmp_path / "memory.db"
    _days(path, 3)
    d3 = _day(3).isoformat()
    model = _Consolidator()
    model.script[d3] = [
        "nope",
        "```json\n"
        + _changes(
            {"op": "add", "section": "承诺和待办", "text": "周五交稿", "sources": ["r1"]},
        )
        + "\n```",
    ]
    _schedule(tmp_path, model).write(TODAY)
    assert model.asked == [d3, d3]
    assert _items(path) == {"承诺和待办": ["周五交稿"]}


def test_a_failing_day_stops_the_chain_and_the_next_run_resumes_from_it(tmp_path: Path) -> None:
    """Later days build on earlier ones, so none runs past a failure."""
    path = tmp_path / "memory.db"
    _days(path, 4, 3, 2)
    d4, d3, d2 = (_day(n).isoformat() for n in (4, 3, 2))
    model = _Consolidator()
    model.script[d4] = [
        _changes({"op": "add", "section": "偏好", "text": "喜欢咖啡", "sources": ["r1"]}),
    ]
    model.script[d3] = [TimeoutError("provider unreachable")]
    schedule = _schedule(tmp_path, model)

    schedule.write(TODAY)
    assert model.asked == [d4, d3]  # d2 is not asked
    assert current_core_memory(path).upto_day == d4
    assert core_memory_pending_days(path, TODAY) == [d3, d2]

    model.script[d3] = [
        _changes({"op": "stale", "item": 1, "sources": ["r1"]}),
    ]
    model.asked.clear()
    schedule.write(TODAY + timedelta(days=1))
    assert model.asked == [d3, d2]
    assert _items(path) == {}
    assert current_core_memory(path).upto_day == d2


def test_a_remember_during_the_call_makes_the_answer_stale(tmp_path: Path) -> None:
    """Item numbers belong to the version the model saw; a newer version drops the answer."""
    path = tmp_path / "memory.db"
    _days(path, 3)
    remember_fact(path, "城市", "Victoria")
    d3 = _day(3).isoformat()

    class _Racing(_Consolidator):
        def chat(self, *, messages: list[dict[str, Any]], **kwargs: object) -> ChatResult:
            if not self.asked:
                remember_fact(path, "口味", "辣")  # lands between the read and the write
            return super().chat(messages=messages, **kwargs)

    model = _Racing()
    model.script[d3] = [
        _changes({"op": "stale", "item": 1, "sources": ["r1"]}),
        '{"changes": []}',
    ]
    _schedule(tmp_path, model).write(TODAY)
    assert model.asked == [d3, d3]  # the first answer was refused; the retry saw both items
    assert _items(path) == {"关于你": ["城市: Victoria", "口味: 辣"]}
    assert current_core_memory(path).upto_day == d3


def test_consolidation_is_off_without_a_core_memory_block() -> None:
    """Absent block (or no preset/prompt) is no consolidation; the defaults are 4000 and 5."""
    assert CoreMemorySettings.from_config(None) is None
    assert CoreMemorySettings.from_config({"preset": "p"}) is None
    on = CoreMemorySettings.from_config({"preset": "p", "prompt": " x "})
    assert on is not None
    assert (on.prompt, on.max_chars, on.max_stale) == ("x", 4000, 5)


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

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

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
        fake.server.shutdown()


def _reviewed(
    tmp_path: Path,
    model: _Consolidator,
    route: SurrogateRoute | None,
    review_min_confidence: float | None = None,
    *,
    review_drop: bool = False,
) -> DaySummarySchedule:
    schedule = _schedule(tmp_path, model)
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
    _days(path, 3)
    remember_fact(path, "城市", "Victoria")
    d3 = _day(3).isoformat()
    kept = {"op": "add", "section": "偏好", "text": "HIGH 喜欢咖啡", "sources": ["r1"]}
    unsure = {"op": "add", "section": "偏好", "text": "LOW 喜欢茶", "sources": ["r1"]}
    joke = {"op": "rewrite", "item": 1, "text": "JOKE 住在火星", "sources": ["r1"]}
    stale = {"op": "stale", "item": 1, "sources": ["r1"]}
    model = _Consolidator()
    model.script[d3] = [_changes(kept, unsure, joke)]
    jev_log = tmp_path / "jev.jsonl"

    _reviewed(
        tmp_path,
        model,
        jev.route(JevLog(jev_log)),
        review_min_confidence=0.9,
        review_drop=True,
    ).write(TODAY)

    assert _items(path) == {"关于你": ["城市: Victoria"], "偏好": ["HIGH 喜欢咖啡"]}
    logged = _versions(path)[-1][2]
    assert [c["op"] for c in logged] == ["add", "dropped", "dropped"]
    assert logged[1] == {
        "op": "dropped",
        "change": {**unsure, "sources": [_rid(3, 1)]},
        "choice": "keep",
        "confidence": 0.6,
    }
    assert (logged[2]["choice"], logged[2]["confidence"]) == ("one_off", 0.99)
    assert current_core_memory(path).upto_day == d3
    # The question carries the item, its section (a rewrite's from the document) and the line.
    states = sorted(r["state"] for r in jev.requests)
    assert len(states) == 3
    joke_state = (
        f"Section: 关于你\nItem: JOKE 住在火星\nCited lines:\n{_stamp(3, 9)} | allen | 第3天说的话"
    )
    assert joke_state in states
    assert _rid(3, 1) not in "".join(states)
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
    _days(path, 2)
    d2 = _day(2).isoformat()
    model.script[d2] = [_changes({**stale, "item": 1})]
    _reviewed(
        tmp_path,
        model,
        jev.route(),
        review_min_confidence=0.9,
        review_drop=True,
    ).write(TODAY)
    assert jev.requests == []
    assert _items(path) == {"偏好": ["HIGH 喜欢咖啡"]}


@pytest.mark.parametrize("failure", ["http", "no_key"])
def test_a_jev_failure_rejects_the_day_like_a_gate_and_stops_the_chain(
    tmp_path: Path,
    jev: _Jev,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Nothing is stored, the day is asked once more, and the next day is not reached."""
    path = tmp_path / "memory.db"
    _days(path, 3, 2)
    d3, d2 = _day(3).isoformat(), _day(2).isoformat()
    model = _Consolidator()
    model.script[d3] = [
        _changes({"op": "add", "section": "偏好", "text": "HIGH 喜欢咖啡", "sources": ["r1"]}),
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
    assert _items(path) == {"偏好": ["HIGH 喜欢咖啡"]}


def test_log_only_review_lands_every_change_and_stores_each_verdict(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """Without review_drop nothing is dropped; each add and rewrite gets a reviewed entry."""
    path = tmp_path / "memory.db"
    _days(path, 3)
    remember_fact(path, "城市", "Victoria")
    d3 = _day(3).isoformat()
    kept = {"op": "add", "section": "偏好", "text": "HIGH 喜欢咖啡", "sources": ["r1"]}
    joke = {"op": "rewrite", "item": 1, "text": "JOKE 住在火星", "sources": ["r1"]}
    model = _Consolidator()
    model.script[d3] = [_changes(kept, joke)]
    jev_log = tmp_path / "jev.jsonl"

    _reviewed(tmp_path, model, jev.route(JevLog(jev_log)), review_min_confidence=0.9).write(TODAY)

    assert _items(path) == {"关于你": ["JOKE 住在火星"], "偏好": ["HIGH 喜欢咖啡"]}
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
    _days(path, 3)
    d3 = _day(3).isoformat()
    model = _Consolidator()
    model.script[d3] = [
        _changes({"op": "add", "section": "偏好", "text": "HIGH 喜欢咖啡", "sources": ["r1"]}),
    ]
    if failure == "http":
        jev.status = 500
    else:
        monkeypatch.delenv(KEY_ENV)

    _reviewed(tmp_path, model, jev.route(), review_min_confidence=0.9).write(TODAY)

    assert model.asked == [d3]  # no retry: the day landed
    assert _items(path) == {"偏好": ["HIGH 喜欢咖啡"]}
    assert [c["op"] for c in _versions(path)[-1][2]] == ["add"]
    assert current_core_memory(path).upto_day == d3


def test_review_is_off_without_review_min_confidence(tmp_path: Path, jev: _Jev) -> None:
    """No key in the block, no Jev call; the block parses the bar, model and timeout."""
    path = tmp_path / "memory.db"
    _days(path, 3)
    d3 = _day(3).isoformat()
    model = _Consolidator()
    model.script[d3] = [
        _changes({"op": "add", "section": "偏好", "text": "JOKE 住在火星", "sources": ["r1"]}),
    ]
    _reviewed(tmp_path, model, jev.route()).write(TODAY)
    assert jev.requests == []
    assert _items(path) == {"偏好": ["JOKE 住在火星"]}

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


def _doc_with(*texts: str, pinned: tuple[str, ...] = ()) -> core_memory.Doc:
    doc = core_memory.empty_doc()
    doc["承诺和待办"] = [{"text": t, "topic": None, "sources": [], "since": "x"} for t in texts]
    for item in doc["承诺和待办"]:
        item["pinned"] = item["text"] in pinned
    return doc


@pytest.mark.parametrize(
    ("text", "day", "stale_items"),
    [
        ("计划于 2026-10-03 和朋友吃饭,2026-10-04 看电影。", "2026-10-03", []),
        ("计划于 2026-10-03 和朋友吃饭,2026-10-04 看电影。", "2026-10-04", [1]),
        ("Reliable Controls 线上面试 2026-10-08 13:00–14:00", "2026-10-07", []),  # noqa: RUF001
        ("Reliable Controls 线上面试 2026-10-08 13:00–14:00", "2026-10-08", [1]),  # noqa: RUF001
        ("2026年10月8日交表", "2026-10-07", []),
        ("2026年10月8日交表", "2026-10-08", [1]),
        ("10月8日开会", "2026-10-08", [1]),
        ("10月8号开会", "2026-10-09", [1]),
        ("10月8日开会", "2026-10-07", []),
        ("10/8 取快递", "2026-10-08", [1]),
        ("10/8 取快递", "2026-10-07", []),
        ("2026/10/8 取快递", "2026-10-07", []),  # a slashed ISO date is not M/D
        ("完成 3/4 的进度", "2026-03-07", [1]),  # any M/D reads as a date, a ratio too
        ("3/4 取快递", "2026-10-07", []),  # >6 months past, nearest year is 2027: upcoming
        ("记得买牛奶", "2026-10-08", []),  # no date: never touched
        ("2026-13-45 不是日期", "2026-10-08", []),
        ("1月5日交表", "2026-12-30", []),  # year-less: the year nearest the day, so Jan 2027
        ("12月30日交表", "2027-01-02", [1]),  # ... and Dec 2026
        ("2月29日交表", "2028-03-01", [1]),  # leap day found in 2028
    ],
)
def test_a_to_do_goes_stale_by_code_when_its_latest_date_is_not_after_the_day(
    text: str, day: str, stale_items: list[int]
) -> None:
    """Each date form, the latest-date rule, the year rule and an item with no date."""
    changes = date_stale(_doc_with(text), day, None)
    assert [c["item"] for c in changes] == stale_items


def test_date_stale_touches_only_the_to_do_section_and_pins_remind_once() -> None:
    """Section 正在做的事 is skipped; a pinned item is reminded only on the night it passes."""
    doc = _doc_with("交表 2026-10-03", "交稿 2026-10-03", pinned=("交稿 2026-10-03",))
    doc["正在做的事"] = [{"text": "写论文,截至 2026-10-01", "topic": None, "sources": []}]
    assert [c["item"] for c in date_stale(doc, "2026-10-03", None)] == [2, 3]  # [1] is 正在做的事
    assert [c["item"] for c in date_stale(doc, "2026-10-03", "2026-10-02")] == [2, 3]
    assert [c["item"] for c in date_stale(doc, "2026-10-04", "2026-10-03")] == [2]  # 3: no reminder
    assert date_stale(doc, "2026-10-03", None)[0]["reason"] == "date passed (2026-10-03)"


def test_the_night_marks_past_dated_to_dos_stale_with_no_model_call_and_never_twice(
    tmp_path: Path,
) -> None:
    """One nightly version per day: stale ops for unpinned, a reminder for pinned, nothing else."""
    path = tmp_path / "memory.db"
    d1, d0 = _day(1), _day(0)
    when = f"{d1.month}月{d1.day}日"
    for topic, fact in {
        "过去": f"{d1 - timedelta(days=1)} 吃饭,{d1} 看电影",
        "之后": f"{d1} 吃饭,{d0} 看电影",
        "中文": f"{d1.year}年{when}交稿",
        "斜杠": f"{d1.month}/{d1.day} 取快递",
        "无日期": "买牛奶",
        "已钉": f"{d1} 交表",
    }.items():
        remember_fact(path, topic, fact, "承诺和待办")
    remember_fact(path, "论文", f"截至 {d1 - timedelta(days=1)} 写完", "正在做的事")
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        base = core_memory.current(conn, "2026-01-01T00:00:00-07:00")
        pin = core_memory.copy.deepcopy(base.doc)
        next(i for i in pin["承诺和待办"] if i["topic"] == "已钉")["pinned"] = True
        core_memory.append_version(
            conn, base=base, doc=pin, origin="user", upto_day=None, changes=[], now=LEGACY_TS
        )
    _days(path, 1, 0, -1)
    model = _Consolidator()  # answers "no changes" every day: every stale below is code's
    schedule = _schedule(tmp_path, model)

    schedule.write(TODAY)  # day 1 lands
    assert _items(path)["承诺和待办"] == [
        "之后: " + f"{d1} 吃饭,{d0} 看电影",
        "无日期: 买牛奶",
        "已钉: " + f"{d1} 交表",
    ]
    assert _items(path)["正在做的事"] == [f"论文: 截至 {d1 - timedelta(days=1)} 写完"]
    nightly = [v for v in _versions(path) if v[0] == "nightly"]
    assert [v[1] for v in nightly] == [d1.isoformat()]  # one version, upto_day intact
    ops = [(c["op"], c.get("reason")) for c in nightly[0][2]]
    assert ops == [("stale", f"date passed ({d1})")] * 3 + [("suggest_stale", None)]

    schedule.write(
        TODAY + timedelta(days=1)
    )  # day 0: the 之后 item's date arrives; 已钉 is not re-reminded
    nightly = [v for v in _versions(path) if v[0] == "nightly"]
    assert [c["op"] for c in nightly[-1][2]] == ["stale"]
    assert "之后" not in str(_items(path))

    schedule.write(TODAY + timedelta(days=2))  # nothing due: the day's version has no changes
    nightly = [v for v in _versions(path) if v[0] == "nightly"]
    assert nightly[-1][2] == []
    assert len(model.asked) == 3  # the model was asked for each day, never for a stale
