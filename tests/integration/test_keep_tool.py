"""ADR 0245, 0299: a lasting fact about him is one write, ``keep``; the facts kept are one read.

The real tools run through ``ToolRegistry.dispatch`` onto a real event log and a real memory.db
under a fixed clock (a Sunday evening in Victoria). Each check asserts the result the model
reads and the core memory it leaves; the last ones drive the real ``decide()`` loop with a
scripted model, so no provider is called. What he has to go to or do is ``schedule``
(``test_schedule_tool.py``).
"""

# ruff: noqa: RUF001 - the facts under test are Chinese, with fullwidth punctuation
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.keep_tool import build_keep_tools
from jarvis.execution.tools import ActionLifecycle, ToolRegistry, build_default_registry
from jarvis.shared import ActionRequest, CallerPrincipal, lang
from jarvis.state import core_memory
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import current_core_memory, open_memory_db
from scripts import tool_exam
from tests.support.model import ScriptedModel, tool_call
from tests.support.turn_guards import guard_batch, logged_guards, tool_texts

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

REPO = Path(__file__).resolve().parents[2]
ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 11, 19, 55, tzinfo=ZONE)  # Sunday
OLD = {"create_memo", "list_memos", "set_reminder", "list_reminders", "cancel_reminder", "remember"}


class _World:
    """One runtime root with ``keep`` and ``kept`` over a real log, memory.db and a moving clock."""

    def __init__(self, root: Path, *, memory: bool = True) -> None:
        self.paths = bootstrap_runtime(root)
        self.conn = open_event_log(self.paths.event_log)
        self.memory = root / "memory.db" if memory else None
        if self.memory is not None:
            open_memory_db(self.memory).close()
        self.now = NOW
        self.registry = ToolRegistry()
        for tool in build_keep_tools(self.memory, ZONE, lambda: self.now):
            self.registry.register(tool)
        self.lifecycle = ActionLifecycle()
        self.n = 0

    def call(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        self.n += 1
        action_id = f"A{self.n}"
        request = ActionRequest(
            action_id=action_id, tool_name=name, target_entity_ref=None,
            caller_principal=CallerPrincipal.JARVIS_LLM, risk_level="L1", arguments=args,
            authorization_lease=None, run_id=None, turn_id=None,
        )
        self.lifecycle.register(action_id)
        self.lifecycle.transition(action_id, "authorized")
        bundle = self.registry.dispatch(request, self.conn, self.paths, self.lifecycle)
        slot = bundle.slots[0]
        assert slot.tool_output is not None
        assert len(slot.tool_output) <= 4000
        out = json.loads(slot.tool_output)
        return out if isinstance(out, dict) else {"value": out}

    def keep(self, text: str, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the model's arguments.
        return self.call("keep", {"text": text, **args})

    def kept(self, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the model's arguments.
        return self.call("kept", args)

    def lines(self) -> list[str]:
        assert self.memory is not None
        return [
            item["text"] for section in current_core_memory(self.memory).doc.values()
            for item in section
        ]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    """A fresh world whose memory stamps items with its clock; the language is Chinese, as run."""
    before = lang.language()
    lang.set_language("zh")
    made = _World(tmp_path)
    monkeypatch.setattr("jarvis.state.memory_db.local_now", lambda: made.now)
    yield made
    made.conn.close()
    lang.set_language(before)


# --- the two entries ---------------------------------------------------------------------------


def test_the_menu_entries_are_two_small_tools_with_the_owner_s_words() -> None:
    """One write and one read; each brief is short, says when to use it and what it returns."""
    keep, kept = build_keep_tools(None, ZONE)
    assert (keep.name, keep.read_only, keep.risk_level) == ("keep", False, "L1")
    assert (kept.name, kept.read_only, kept.risk_level) == ("kept", True, "L0")
    for one in (keep, kept):
        assert one.allowed_callers == {CallerPrincipal.JARVIS_LLM, CallerPrincipal.REGEX_ROUTER}
        assert len(one.description) <= 600
        assert one.description.isascii()
        assert one.description.startswith("Use ")
        assert not one.deferred
        assert one.input_schema["additionalProperties"] is False
        assert all(p["description"] for p in one.input_schema["properties"].values())
    assert set(keep.input_schema["properties"]) == {"text", "topic", "section"}
    assert set(kept.input_schema["properties"]) == {"query", "page"}
    assert keep.input_schema["properties"]["section"]["enum"] == list(core_memory.SECTIONS)
    assert "(schedule)" in keep.description
    assert "(schedule)" in kept.description
    assert keep.supersedes == {"remember": "keep(text=...)"}


def test_the_default_registry_has_the_two_and_none_of_the_six() -> None:
    """Menu and router reach ``keep`` and ``kept``; the tools they replace are not registered."""
    registry = build_default_registry()
    seen = {t.name for t in registry.get_definitions()}
    assert {"keep", "kept"} <= seen
    assert not seen & OLD


def test_the_offline_menu_offers_keep_and_kept_in_place_of_the_six(tmp_path: Path) -> None:
    """The shipped config through the tool exam's own registry, with the tools' sizes."""
    recorded = tmp_path / "mcp-tools.json"
    recorded.write_text("{}", encoding="utf-8")
    config = yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    menu = tool_exam.offered_tools(config, recorded)
    names = {one["name"] for one in menu}
    assert {"keep", "kept"} <= names
    assert not names & OLD
    sizes = {
        one["name"]: len(one["description"]) + len(json.dumps(one["input_schema"]))
        for one in menu if one["name"] in {"keep", "kept"}
    }
    assert sizes["keep"] < 1200  # the section enum names the ten sections of core memory
    assert sizes["kept"] < 600


def test_core_memory_has_ten_sections_and_keep_offers_each() -> None:
    """The enum is the sections of core memory, and the retired to-do section is not one."""
    (keep, _) = build_keep_tools(None, ZONE)
    offered = keep.input_schema["properties"]["section"]["enum"]
    assert len(offered) == len(core_memory.SECTIONS) == 10
    assert core_memory.DUE_SECTION not in offered


# --- a fact ------------------------------------------------------------------------------------


def test_a_fact_goes_to_core_memory_once_and_a_topic_replaces_its_item(world: _World) -> None:
    """Saved, then "already"; the same topic rewrites its line; the origin is ``remember``."""
    assert world.memory is not None
    done = world.keep("10月12日", topic="生日")
    assert done == {
        "summary": 'Saved to long-term memory: "生日: 10月12日".', "text": "生日: 10月12日",
    }
    again = world.keep("10月12日", topic="生日")
    assert again["summary"] == 'Already in long-term memory, nothing new: "生日: 10月12日".'
    changed = world.keep("10月13日", topic="生日")
    assert changed["summary"].startswith("Saved to long-term memory")
    plain = world.keep("他不吃香菜")
    assert plain["text"] == "他不吃香菜"
    assert world.lines() == ["生日: 10月13日", "他不吃香菜"]
    with closing(open_memory_db(world.memory)) as conn:
        origins = [row[0] for row in conn.execute("SELECT origin FROM core_memory ORDER BY rowid")]
    assert origins == ["migration", "remember", "remember", "remember"]


def test_a_fact_goes_to_the_section_the_model_names_and_a_bad_one_is_refused(
    world: _World,
) -> None:
    """``section`` files the fact; none means 经历; an ID number in 证件和硬期限 is refused."""
    assert world.memory is not None
    world.keep("他不吃辣", section="身体")
    world.keep("他爱喝茶")
    world.keep("护照 2031-05-01 到期", section="证件和硬期限")
    doc = current_core_memory(world.memory).doc
    assert [i["text"] for i in doc["身体"]] == ["他不吃辣"]
    assert [i["text"] for i in doc["经历"]] == ["他爱喝茶"]
    assert [i["text"] for i in doc["证件和硬期限"]] == ["护照 2031-05-01 到期"]
    assert world.keep("护照 E12345678", section="证件和硬期限")["code"] == "invalid_arguments"
    assert world.keep("x", section="其他")["code"] == "invalid_arguments"
    assert world.keep("x", section=core_memory.DUE_SECTION)["code"] == "invalid_arguments"
    assert "护照 E12345678" not in str(current_core_memory(world.memory).doc)


def test_a_fact_that_is_not_one_short_line_or_has_no_memory_is_refused(
    world: _World, tmp_path: Path,
) -> None:
    """Too long: a longer note is the ledger's. No memory.db on this host: the error says so."""
    too_long = world.keep("x" * 241)
    assert too_long["code"] == "too_long"
    assert "schedule" in too_long["error"]
    assert world.keep("x", topic="t" * 41)["code"] == "too_long"
    bare = _World(tmp_path / "bare", memory=False)
    try:
        refused = bare.keep("他不吃香菜")
        assert refused["code"] == "unavailable"
        assert "nothing was saved" in refused["error"]
        assert bare.kept()["summary"] == "No lasting facts kept."
    finally:
        bare.conn.close()


def test_no_text_is_refused(world: _World) -> None:
    """An empty call names the argument and gives an example."""
    refused = world.call("keep", {"text": "  "})
    assert refused["code"] == "empty_text"
    assert "keep(text=" in refused["error"]
    assert world.call("keep", {})["code"] == "empty_text"
    assert world.lines() == []


@pytest.mark.parametrize(
    "retired", ["kind", "at", "until", "place", "in_minutes", "cancel", "confirm", "whole_sender"],
)
def test_what_keep_took_before_the_ledger_is_sent_to_schedule(
    world: _World, retired: str,
) -> None:
    """A model that still passes a memo's or an appointment's argument hears where to go."""
    refused = world.call("keep", {"text": "call mum", retired: "x"})
    assert refused["code"] == "invalid_arguments"
    assert 'schedule(action="add", what=..., at=...)' in refused["error"]
    assert world.lines() == []


def test_an_argument_keep_never_had_is_named(world: _World) -> None:
    """The error lists the three it takes."""
    refused = world.call("keep", {"text": "x", "bogus": 1})
    assert refused["error"] == "keep: no argument 'bogus'; use text, topic or section"


# --- kept --------------------------------------------------------------------------------------


def _seed(world: _World) -> None:
    """Eleven facts over three days: newest day first when read back."""
    world.keep("他对花生过敏")
    world.now += timedelta(days=1)
    for n in range(8):
        world.keep(f"fact {n}")
    world.now += timedelta(days=1)
    world.keep("10月12日", topic="生日")
    world.keep("他不吃香菜")


def test_kept_lists_facts_newest_first_eight_to_a_page_and_names_them(world: _World) -> None:
    """Newest day first, the later write first within a day; page 2 continues."""
    _seed(world)
    one = world.kept()
    assert [r["text"] for r in one["items"]] == [
        "他不吃香菜", "生日: 10月12日", "fact 7", "fact 6", "fact 5", "fact 4", "fact 3", "fact 2",
    ]
    assert [r["when"] for r in one["items"]][:2] == ["saved Oct 13", "saved Oct 13"]
    assert one["items"][2]["when"] == "saved Oct 12"
    assert one["more"] is True
    assert one["summary"].startswith(
        '11 lasting facts (showing 1-8): "他不吃香菜" (saved Oct 13);'
        ' "生日: 10月12日" (saved Oct 13); "fact 7" (saved Oct 12);',
    )
    two = world.kept(page=2)
    assert [r["text"] for r in two["items"]] == ["fact 1", "fact 0", "他对花生过敏"]
    assert two["items"][-1]["when"] == "saved Oct 11"
    assert two["more"] is False
    assert "(showing 9-11)" in two["summary"]
    assert world.kept(page=3)["summary"] == "There is no page 3; what was kept ends at page 2."


def test_kept_by_query(world: _World) -> None:
    """A query keeps the facts whose words hold it, whatever the case."""
    _seed(world)
    found = world.kept(query="FACT 3")
    assert [r["text"] for r in found["items"]] == ["fact 3"]
    assert found["summary"] == '1 lasting fact matching "fact 3": "fact 3" (saved Oct 12).'
    assert [r["text"] for r in world.kept(query="生日")["items"]] == ["生日: 10月12日"]
    assert world.kept(query="nothing like this")["summary"] == (
        'No lasting facts kept match "nothing like this".'
    )


def test_kept_says_when_nothing_is_kept(world: _World) -> None:
    """The empty list is a sentence."""
    assert world.kept()["summary"] == "No lasting facts kept."
    assert world.kept()["items"] == []


def test_kept_dates_a_fact_in_the_year_it_was_saved_when_that_is_not_this_one(
    world: _World,
) -> None:
    """``saved Oct 11`` this year, ``saved Oct 11 2025`` last year."""
    world.now = datetime(2025, 10, 11, 9, 0, tzinfo=ZONE)
    world.keep("他爱喝茶")
    world.now = NOW
    assert world.kept()["items"][0]["when"] == "saved Oct 11 2025"


def test_kept_stays_under_four_thousand_characters_with_the_longest_facts(world: _World) -> None:
    """Eight rows of the longest facts (240 characters) and the summary naming them still fit."""
    for n in range(10):
        world.keep(f"{n}" + "x" * 239)
    out = world.kept()
    assert len(out["items"]) == 8
    assert len(json.dumps(out, ensure_ascii=False)) < 4000


@pytest.mark.parametrize(
    ("args", "code"),
    [({"page": 0}, "invalid_arguments"), ({"page": "2"}, "invalid_arguments")],
)
def test_kept_refuses_arguments_that_do_not_fit(
    world: _World, args: dict[str, Any], code: str,
) -> None:
    """The error says what to pass."""
    refused = world.call("kept", args)
    assert refused["code"] == code
    assert "kept:" in refused["error"]


def test_kept_shows_a_fact_in_his_zone_the_day_it_was_saved(world: _World) -> None:
    """A fact saved at 02:55 UTC on the 12th is the 11th in Victoria."""
    world.now = datetime(2026, 10, 12, 2, 55, tzinfo=UTC)
    world.keep("他爱喝茶")
    assert world.kept()["items"][0]["when"] == "saved Oct 12"


# --- decide() with a scripted model ------------------------------------------------------------


def _lines(memory: Path) -> list[str]:
    return [i["text"] for section in current_core_memory(memory).doc.values() for i in section]


@pytest.fixture
def memory_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The turn-guard harness's registry, with a memory.db behind ``keep``."""
    memory = tmp_path / "memory.db"
    open_memory_db(memory).close()
    monkeypatch.setattr(
        "tests.support.turn_guards.build_default_registry",
        lambda: build_default_registry(memory_db_path=memory),
    )
    return memory


def test_the_model_saves_once_and_answers(tmp_path: Path, memory_registry: Path) -> None:
    """One keep, one answer: the second request carries the sentence that says it was saved."""
    model = ScriptedModel(tool_call("keep", {"text": "他不吃香菜"}), "好，记下了：他不吃香菜。")
    text, conn = guard_batch(tmp_path, model)
    try:
        assert text == "好，记下了：他不吃香菜。"
        assert len(model.requests) == 2
        assert _lines(memory_registry) == [
            "他不吃香菜",
        ]
        (result,) = tool_texts(model.requests[-1].messages)
        assert 'Saved to long-term memory: \\"他不吃香菜\\".' in result
        assert logged_guards(conn) == []
    finally:
        conn.close()


def test_a_model_that_saves_again_with_other_arguments_still_writes_one_item(
    tmp_path: Path, memory_registry: Path,
) -> None:
    """Each variation after the first is answered "already", and the memory holds one line."""
    fact = "allergic to peanuts"
    model = ScriptedModel(
        tool_call("keep", {"text": fact}),
        tool_call("keep", {"text": f"{fact} "}),
        tool_call("keep", {"text": f" {fact}"}),
        "好，记住了。",
    )
    answer, conn = guard_batch(tmp_path, model)
    try:
        assert answer == "好，记住了。"
        assert _lines(memory_registry) == [
            fact,
        ]
        results = tool_texts(model.requests[-1].messages)
        assert "Saved to long-term memory" in results[0]
        assert all("Already in long-term memory, nothing new" in one for one in results[1:])
    finally:
        conn.close()


def test_an_answer_that_claims_a_save_before_any_call_is_sent_back_to_keep(
    tmp_path: Path, memory_registry: Path,
) -> None:
    """ADR 0240 on the tool: 「记下了」 with nothing kept goes back once, then keep runs."""
    model = ScriptedModel("记下了。", tool_call("keep", {"text": "明天是我生日"}), "好。")
    _, conn = guard_batch(tmp_path, model)
    try:
        assert [g["guard"] for g in logged_guards(conn)] == ["unbacked"]
        assert len(model.requests) == 3
        assert _lines(memory_registry) == [
            "明天是我生日",
        ]
    finally:
        conn.close()
