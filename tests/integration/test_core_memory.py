"""Core memory (ADR 0145): migration, rendering, ``remember`` versions and the nightly pass.

Real memory.db, real ``DaySummarySchedule`` and the real ``remember`` tool through the
default registry; only the consolidator's provider call is faked, by a client that
answers per day from a script and records what it was asked.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.llm import ChatResult
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
            {"op": "add", "section": "偏好", "text": "喜欢咖啡", "sources": [_rid(4, 1)]},
            {"op": "add", "section": "正在做的事", "text": "写论文", "sources": [_rid(4, 1)]},
        ),
    ]
    model.script[d3] = [
        _changes(
            {
                "op": "rewrite",
                "item": 2,
                "text": "  更喜欢\n茶  ",
                "sources": [_rid(3, 1)],
                "section": "定下来的规矩",
            },
            {"op": "stale", "item": 3, "sources": [_rid(3, 1)]},
        ),
    ]
    schedule = _schedule(tmp_path, model)

    schedule.write(TODAY)

    assert model.asked == [d4, d3, d2]  # oldest first; day 1 has no summary; today is not past
    assert "[1] 城市: Victoria" in model.inputs[d4]
    assert "### 偏好\n[2] 喜欢咖啡\n### 正在做的事\n[3] 写论文" in model.inputs[d3]
    assert f"[Summary of {d3}]\n## {d3} (the whole day, word for word)" in model.inputs[d3]
    assert f"{_rid(3, 1)} | {_stamp(3, 9)} | allen | 第3天说的话" in model.inputs[d3]
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
    rid = _rid(3, 1)
    stale = {"op": "stale", "item": 1, "sources": [rid]}
    return {
        "not json": ("sure, here you go", {}),
        "no changes list": ('{"edits": []}', {}),
        "unknown item": (
            _changes({"op": "stale", "item": 9, "sources": [rid]}),
            {},
        ),
        "foreign record id": (
            _changes({"op": "add", "section": "偏好", "text": "x", "sources": [_rid(2, 1)]}),
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
            {"op": "add", "section": "承诺和待办", "text": "周五交稿", "sources": [_rid(3, 1)]},
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
        _changes({"op": "add", "section": "偏好", "text": "喜欢咖啡", "sources": [_rid(4, 1)]}),
    ]
    model.script[d3] = [TimeoutError("provider unreachable")]
    schedule = _schedule(tmp_path, model)

    schedule.write(TODAY)
    assert model.asked == [d4, d3]  # d2 is not asked
    assert current_core_memory(path).upto_day == d4
    assert core_memory_pending_days(path, TODAY) == [d3, d2]

    model.script[d3] = [
        _changes({"op": "stale", "item": 1, "sources": [_rid(3, 1)]}),
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
        _changes({"op": "stale", "item": 1, "sources": [_rid(3, 1)]}),
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
