"""ADR 0390: the topics night, one call per past day after the day summary.

Acceptance checks:

- the gate, as a table of answers to the reason each is refused: one malformed part refuses the
  whole answer, except a new topic below the bar (two days, or six lines of the day), which is
  left out and named; a name on a kept topic is ignored, since only he renames;
- the input: kept topics with where each stands (a done one without its recap), the earlier talk
  no topic holds under short labels, and the day's lines with the topics they were said in;
- the schedule: after the summaries, every pending day oldest first; a day below the bar opens
  nothing and the next day can open it with the earlier line; a day without messages is recorded
  without a call; a day refused twice stops the chain and the next run resumes there; a change he
  makes during the call voids that answer; each call is recorded under its own kind.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import date, time
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.topics_night import (
    DayLine,
    KeptTopic,
    Mention,
    NightAnswer,
    build_night_messages,
    check_night,
    labels,
    summary_mentions,
)
from jarvis.runtime.day_summary import DaySummarySchedule, DaySummarySettings
from jarvis.runtime.topics_night import KIND, TopicsSettings
from jarvis.state import topics
from jarvis.state.cost_accounting import call_recorded_since
from jarvis.state.event_log import open_event_log, open_runtime_event_log
from jarvis.state.memory_db import MemorySettings
from tests.support.clock import FakeClock
from tests.support.day_summaries import (
    LOCAL_ZONE,
    TODAY,
    asked_days,
    day_ago,
    day_of,
    day_summariser,
    insert_records,
    record_id,
    stamp_ago,
)
from tests.support.model import Failure, Reply, ScriptedModel
from tests.support.topics import night, opened

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.model import Request

DAY = "2026-10-14"
EARLIER = (Mention("2026-10-11", "陶艺课报名", "a1"),)
LINES = tuple(
    DayLine(f"c{n}", f"2026-10-14T08:0{n}:00-07:00", "allen", f"陶艺课的第{n}句", ())
    for n in range(1, 8)
)
KNOWN = labels(DAY, EARLIER, LINES)


def _topic(topic_id: str, name: str, state: topics.State = "open") -> topics.Topic:
    return topics.Topic(topic_id, name, state, "还在进行", f"{name}的前情", None, f"v-{topic_id}")


KEPT = (
    KeptTopic(_topic("t1", "陶艺课"), "2026-10-13"),
    KeptTopic(_topic("t2", "搬家", "done"), "2026-10-13"),
    KeptTopic(_topic("t3", "拉坯工具"), "2026-10-13"),
)


def _entry(topic: str, *lines: str, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    return {"topic": topic, "line": "还在进行", "recap": "前情", "lines": list(lines), **extra}


def _check(answer: object, finish: str = "stop") -> NightAnswer | str:
    text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    return check_night(text, finish, day=DAY, kept=KEPT, known=KNOWN)


# --- the gate ----------------------------------------------------------------------------------


def test_a_good_answer_files_lines_opens_a_topic_on_two_days_and_merges() -> None:
    """Labels come back as record ids; a kept topic keeps its name; the recap keeps its breaks."""
    answer = _check({
        "topics": [
            _entry("t1", "r1", "r2", "r1", name="别的名字", recap="第一行\n  第二行  "),
            _entry("new", "e1", "r3", name="陶艺工具"),
            _entry("t2", "r4", state="done"),
        ],
        "merges": [{"from": "t3", "into": "t1"}],
    })
    assert isinstance(answer, NightAnswer), answer
    plan = answer.plan
    assert plan.merges == (("t3", "t1"),)
    assert [(one.topic_id, one.name, one.state, one.record_ids) for one in plan.topics] == [
        ("t1", "陶艺课", "open", ("c1", "c2")),
        (None, "陶艺工具", "open", ("a1", "c3")),
        ("t2", "搬家", "done", ("c4",)),
    ]
    assert plan.topics[0].recap == "第一行\n第二行"
    assert answer.dropped == ()


def test_a_new_topic_below_the_bar_is_left_out_and_the_rest_lands() -> None:
    """One day and five lines is below; six lines of the day, or two days, is enough."""
    answer = _check({
        "topics": [
            _entry("new", "r1", "r2", "r3", "r4", "r5", name="太早"),
            _entry("new", "r1", "r2", "r3", "r4", "r5", "r6", name="六行"),
            _entry("t1", "r7"),
        ],
    })
    assert isinstance(answer, NightAnswer), answer
    assert [one.name for one in answer.plan.topics] == ["六行", "陶艺课"]
    assert answer.dropped == ("太早",)
    fenced = _check(f"```json\n{json.dumps({'topics': []})}\n```")
    assert isinstance(fenced, NightAnswer), "a fenced answer is the one JSON object"


REFUSED: dict[str, tuple[object, str]] = {
    "not JSON": ("今天没有话题", "not valid JSON"),
    "no topics list": ({"merges": []}, 'no "topics" list'),
    "merges not a list": ({"topics": [], "merges": {}}, '"merges" is not a list'),
    "an entry that is not an object": ({"topics": ["t1"]}, "entry is not an object"),
    "a topic id never given": ({"topics": [_entry("t9", "r1")]}, "not a listed topic or new"),
    "a label never given": ({"topics": [_entry("t1", "r99")]}, "labels that are not given: r99"),
    "lines as text": ({"topics": [{**_entry("t1"), "lines": "r1"}]}, "not a list of labels"),
    "a state outside open and done": (
        {"topics": [_entry("t1", "r1", state="merged")]}, "state is not open or done",
    ),
    "a new topic without a name": ({"topics": [_entry("new", "r1", "e1")]}, "name is not 1 to 24"),
    "a name of 25 characters": (
        {"topics": [_entry("new", "r1", "e1", name="长" * 25)]}, "name is not 1 to 24",
    ),
    "a line of 81 characters": (
        {"topics": [_entry("t1", "r1", line="长" * 81)]}, "line is not 1 to 80",
    ),
    "a recap of 801 characters": (
        {"topics": [_entry("t1", "r1", recap="长" * 801)]}, "recap is not 1 to 800",
    ),
    "an empty recap": ({"topics": [_entry("t1", "r1", recap=" \n ")]}, "recap is not 1 to 800"),
    "two entries for one topic": (
        {"topics": [_entry("t1", "r1"), _entry("t1", "r2")]}, "a topic has two entries",
    ),
    "a kept topic with no line": ({"topics": [_entry("t1")]}, "cites no line"),
    "a new topic with a kept name": (
        {"topics": [_entry("new", "r1", "e1", name="搬家")]}, "name of a topic already kept",
    ),
    "a merge into itself": (
        {"topics": [_entry("t1", "r1")], "merges": [{"from": "t1", "into": "t1"}]},
        "does not name two listed topics",
    ),
    "a merge of a topic never given": (
        {"topics": [_entry("t1", "r1")], "merges": [{"from": "t9", "into": "t1"}]},
        "does not name two listed topics",
    ),
    "a merge that is not an object": ({"topics": [], "merges": ["t3"]}, "merge is not an object"),
    "a topic merged twice": (
        {
            "topics": [_entry("t1", "r1"), _entry("t2", "r2")],
            "merges": [{"from": "t3", "into": "t1"}, {"from": "t3", "into": "t2"}],
        },
        "merged twice",
    ),
    "a chain of merges": (
        {
            "topics": [_entry("t3", "r1")],
            "merges": [{"from": "t2", "into": "t1"}, {"from": "t1", "into": "t3"}],
        },
        "merged into one that is merged away",
    ),
    "a merged-away topic listed too": (
        {
            "topics": [_entry("t1", "r1"), _entry("t3", "r2")],
            "merges": [{"from": "t3", "into": "t1"}],
        },
        "merged away and also listed",
    ),
    "a merge target without its new recap": (
        {"topics": [], "merges": [{"from": "t3", "into": "t1"}]}, "has no entry with its new recap",
    ),
}


@pytest.mark.parametrize(("answer", "why"), REFUSED.values(), ids=REFUSED.keys())
def test_a_malformed_answer_is_refused_whole_with_its_reason(answer: object, why: str) -> None:
    """Nothing of a refused answer is stored; the reason is what the log says."""
    refused = _check(answer)
    assert isinstance(refused, str)
    assert why in refused


def test_an_answer_cut_off_by_the_output_limit_is_refused() -> None:
    """A cut answer may have lost entries, so it is not read at all."""
    assert _check({"topics": []}, finish="length") == "cut off by the output limit"


# --- the input ---------------------------------------------------------------------------------


def test_the_input_holds_the_topics_the_earlier_talk_and_the_day_under_short_labels() -> None:
    """A done topic shows no recap; his words are cut at 2000 characters, hers at 600."""
    lines = (
        DayLine("x1", "2026-10-14T08:00:00-07:00", "allen", "长" * 2100, ("t1",)),
        DayLine("x2", "2026-10-14T08:01:00-07:00", "jarvis", "好" * 700, ()),
    )
    (message,) = build_night_messages(day=DAY, kept=KEPT, earlier=EARLIER, lines=lines)
    text = message["content"]
    assert "t1 | open | 陶艺课 | 2026-10-13 | 还在进行 | 陶艺课的前情" in text
    assert "t2 | done | 搬家 | 2026-10-13 | 还在进行\n" in text
    assert "e1 | 2026-10-11 | 陶艺课报名" in text
    assert f"r1 | 08:00 | allen | {'长' * 2000}… (100 more characters) | t1" in text
    assert f"r2 | 08:01 | jarvis | {'好' * 600}… (100 more characters) | -" in text
    assert text.index("[Topics") < text.index("[Earlier talk") < text.index("[Records of")
    empty = build_night_messages(day=DAY, kept=(), earlier=(), lines=lines)[0]["content"]
    assert "(none yet)" in empty
    assert "(none)" in empty


def test_earlier_talk_comes_from_the_topics_heading_of_a_day_summary() -> None:
    """Only bullets under ### Topics that name their record; "none" is not talk."""
    summary = (
        "## 2026-10-11\n### Topics\n- 陶艺课报名 [record_id=a1]\n- 没有出处\n"
        "- none [record_id=a2]\n"
        "### Decisions and facts the user stated\n- 周六十点 [record_id=a3]"
    )
    assert summary_mentions("2026-10-11", summary) == [Mention("2026-10-11", "陶艺课报名", "a1")]


# --- the schedule ------------------------------------------------------------------------------


def _schedule(tmp_path: Path, model: ScriptedModel, *, since: str = "") -> DaySummarySchedule:
    log = tmp_path / "events.db"
    open_event_log(log).close()
    return DaySummarySchedule(
        memory=MemorySettings(db_path=tmp_path / "memory.db", audio_dir=tmp_path / "audio"),
        settings=DaySummarySettings(at=time(5, 0), preset="p", prompt="summarise", max_chars=500),
        llm_config={},
        event_log_path=log,
        pricing_table={},
        clock=FakeClock(zone=LOCAL_ZONE),
        topics=TopicsSettings(preset="t", prompt="sort", since=since),
        client=day_summariser().as_llm_client(),
        topics_client=model.as_llm_client(),
    )


def _seed(db: Path) -> None:
    insert_records(db, [
        (record_id(3, 1), stamp_ago(3, 9), "allen", "周六的陶艺课我报名了"),
        (record_id(3, 2), stamp_ago(3, 9, 1), "jarvis", "好的"),
        (record_id(2, 1), stamp_ago(2, 19), "allen", "陶艺课要带围裙吗"),
        (record_id(2, 2), stamp_ago(2, 19, 1), "jarvis", "要"),
        (record_id(1, 1), stamp_ago(1, 12), "picture_note", "[Picture note]\n一碗面"),
    ])


def _answer(**by_day: str | Exception) -> ScriptedModel:
    """The topics preset's model: each day's answer, or no change."""

    def respond(request: Request) -> Reply | Failure:
        ago = (TODAY - date.fromisoformat(day_of(request))).days
        answer = by_day.get(f"d{ago}", '{"topics": []}')
        return Failure(answer) if isinstance(answer, Exception) else Reply(answer)

    return ScriptedModel(otherwise=respond, model="fake-topics")


CLASS = json.dumps(
    {"topics": [_entry("new", "e1", "r1", "r2", name="陶艺课", recap="他报了陶艺课 要带围裙")]},
    ensure_ascii=False,
)
TOO_EARLY = json.dumps({"topics": [_entry("new", "r1", "r2", name="陶艺课")]}, ensure_ascii=False)


def test_the_night_sorts_each_past_day_after_its_summary_and_opens_on_the_second_day(
    tmp_path: Path,
) -> None:
    """d3 is below the bar; d2 opens it with d3's line; d1 has no message and costs no call."""
    model = _answer(d3=TOO_EARLY, d2=CLASS)
    db = tmp_path / "memory.db"
    _seed(db)
    schedule = _schedule(tmp_path, model)

    schedule.write(TODAY)

    d3, d2, d1 = (day_ago(n).isoformat() for n in (3, 2, 1))
    assert asked_days(model) == [d3, d2]
    second = str(model.requests[1].messages[0]["content"])
    assert f"e1 | {d3} | 周六的陶艺课我报名了" in second, "the word-for-word day's line of his"
    assert "r2 | 19:01 | jarvis | 要 | -" in second
    (class_topic,) = topics.listed(db, LOCAL_ZONE)
    assert (class_topic.topic.id, class_topic.topic.name) == ("t1", "陶艺课")
    assert [
        (place.day.isoformat(), [line.record_id for line in place.lines], place.by)
        for place in class_topic.places
    ] == [(d3, [record_id(3, 1)], "night"), (d2, [record_id(2, 1), record_id(2, 2)], "night")]
    assert topics.nights_done(db) == {d3, d2, d1}
    with closing(open_runtime_event_log(tmp_path / "events.db")) as conn:
        assert call_recorded_since(conn, kind=KIND, since_ms=0)

    schedule.write(TODAY)
    assert len(model.requests) == 2, "every day is sorted; nothing is asked again"


def test_a_day_refused_twice_stops_the_chain_and_the_next_run_resumes_there(
    tmp_path: Path,
) -> None:
    """Two bad answers for d3, then a failed call: nothing stored, d2 never asked; then it lands."""
    answers: dict[str, str | Exception] = {"d3": "不是 JSON", "d2": CLASS}
    model = _answer(**answers)
    db = tmp_path / "memory.db"
    _seed(db)
    _schedule(tmp_path, model).write(TODAY)
    d3 = day_ago(3).isoformat()
    assert asked_days(model) == [d3, d3]
    assert topics.nights_done(db) == set()

    failing = _answer(d3=RuntimeError("provider down"))
    _schedule(tmp_path, failing).write(TODAY)
    assert asked_days(failing) == [d3], "a failed call stops the chain at once"
    assert topics.nights_done(db) == set()

    fixed = _answer(d2=CLASS)
    _schedule(tmp_path, fixed).write(TODAY)
    assert asked_days(fixed) == [d3, day_ago(2).isoformat()]
    assert [one.topic.name for one in topics.listed(db, LOCAL_ZONE)] == ["陶艺课"]


def test_a_change_he_makes_during_the_call_voids_that_answer(tmp_path: Path) -> None:
    """The answer was made from topics that are gone; the second attempt reads the new ones."""
    db = tmp_path / "memory.db"
    _seed(db)
    night(db, "2000-01-01", opened("搬家", record_id(3, 1)))
    seen: list[str] = []

    def respond(request: Request) -> Reply:
        seen.append(str(request.messages[0]["content"]))
        if len(seen) == 1:
            assert topics.change(db, "t1", "rename", name="搬新家") is not None
        return Reply(json.dumps({"topics": [_entry("t1", "r1")]}))

    _schedule(tmp_path, ScriptedModel(otherwise=respond)).write(TODAY)

    assert "t1 | open | 搬家 |" in seen[0]
    assert "t1 | open | 搬新家 |" in seen[1]
    live = topics.live_topic(db, "t1")
    assert live is not None
    assert live.name == "搬新家", "the night never renames"


def test_since_skips_the_days_before_it(tmp_path: Path) -> None:
    """The first run starts at ``topics.since``."""
    model = _answer(d2=CLASS)
    db = tmp_path / "memory.db"
    _seed(db)
    _schedule(tmp_path, model, since=day_ago(2).isoformat()).write(TODAY)
    assert asked_days(model) == [day_ago(2).isoformat()]


def test_the_settings_need_a_preset_and_a_prompt() -> None:
    """No block, or one without preset or prompt, is no night; a bad since is every day."""
    assert TopicsSettings.from_config(None) is None
    assert TopicsSettings.from_config({"preset": "p"}) is None
    assert TopicsSettings.from_config({"prompt": "sort"}) is None
    assert TopicsSettings.from_config({"preset": "p", "prompt": " sort ", "since": "soon"}) == (
        TopicsSettings(preset="p", prompt="sort", since="")
    )
    assert TopicsSettings.from_config(
        {"preset": "p", "prompt": "sort", "since": "2026-09-01"},
    ) == TopicsSettings(preset="p", prompt="sort", since="2026-09-01")
