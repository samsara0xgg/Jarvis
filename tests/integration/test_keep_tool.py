"""ADR 0245: what he asks her to keep is one write, ``keep``; what she kept is one read, ``kept``.

The real tools run through ``ToolRegistry.dispatch`` onto a real event log and a real memory.db
under a fixed clock (a Sunday evening in Victoria). Each check asserts the result the model
reads and the rows the stores hold; the last ones drive the real ``decide()`` loop with a
scripted model, so no provider is called.
"""

# ruff: noqa: RUF001 - the notes under test are Chinese, with fullwidth punctuation
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from jarvis.decision.tier0 import load_tier0_table, match_tier0, render_tier0_response
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.keep_tool import build_keep_tools
from jarvis.execution.tools import ActionLifecycle, ToolRegistry, build_default_registry
from jarvis.shared import ActionRequest, CallerPrincipal, lang
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.state.memory_db import current_core_memory, open_memory_db
from scripts import tool_exam
from tests.integration.test_turn_guards import _batch, _count, _guards, _ScriptedModel, _tool_texts

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

    def call(
        self, name: str, args: Mapping[str, Any],
        who: CallerPrincipal = CallerPrincipal.JARVIS_LLM,
    ) -> dict[str, Any]:
        self.n += 1
        action_id = f"A{self.n}"
        request = ActionRequest(
            action_id=action_id, tool_name=name, target_entity_ref=None, caller_principal=who,
            risk_level="L1", arguments=args, authorization_lease=None, run_id=None, turn_id=None,
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

    def count(self, event_type: str) -> int:
        return len(list(iter_events_of_types(self.conn, (event_type,))))


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    """A fresh world whose log stamps events with its clock; the language is Chinese, as run."""
    before = lang.language()
    lang.set_language("zh")
    made = _World(tmp_path)
    monkeypatch.setattr(
        "jarvis.state.event_log._now_epoch_ms", lambda: int(made.now.timestamp() * 1000),
    )
    yield made
    made.conn.close()
    lang.set_language(before)


# --- the two entries ---------------------------------------------------------------------------


def test_the_menu_entries_are_two_small_tools_with_the_owner_s_words() -> None:
    """One write and one read; each brief is short and says when to use it and what it returns."""
    keep, kept = build_keep_tools(None, ZONE)
    assert (keep.name, keep.read_only, keep.risk_level) == ("keep", False, "L1")
    assert (kept.name, kept.read_only, kept.risk_level) == ("kept", True, "L0")
    for one in (keep, kept):
        assert one.allowed_callers == {CallerPrincipal.JARVIS_LLM, CallerPrincipal.REGEX_ROUTER}
        assert len(one.description) <= 600
        assert not one.deferred
        assert one.input_schema["additionalProperties"] is False
    assert set(keep.input_schema["properties"]) == {"text", "kind", "at", "topic", "cancel"}
    assert set(kept.input_schema["properties"]) == {"kind", "query", "page"}
    for phrase in ("记一下", "记得", "回家修", "提醒我", "别忘了", "saved only when this returns"):
        assert phrase in keep.description
    assert "kept" in keep.description
    assert "keep(cancel=)" in kept.description


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
    assert sizes["keep"] < 1400
    assert sizes["kept"] < 700


# --- a memo ------------------------------------------------------------------------------------


def test_a_memo_is_saved_once_and_the_result_says_so(world: _World) -> None:
    """"Saved" the first time; the same text again, spaced or cased otherwise, writes nothing."""
    first = world.keep("回家修 emoji 功能")
    assert first == {
        "summary": 'Saved to memos: "回家修 emoji 功能".',
        "kind": "memo",
        "text": "回家修 emoji 功能",
    }
    for again in (
        world.keep("回家修 emoji 功能"),
        world.keep(" 回家修   Emoji 功能 ", kind="memo"),
    ):
        assert again["summary"] == 'Already saved to memos, nothing new: "回家修 emoji 功能".'
    assert world.count("memo.captured") == 1
    (event,) = iter_events_of_types(world.conn, ("memo.captured",))
    assert event.payload["text"] == "回家修 emoji 功能"


def test_the_same_memo_is_new_again_after_the_window_and_another_memo_is_new(
    world: _World,
) -> None:
    """Ten minutes make a second "buy milk" a second note; other words are another note at once."""
    world.keep("buy milk")
    world.keep("call the bank")
    assert world.count("memo.captured") == 2
    world.now += timedelta(minutes=11)
    assert world.keep("buy milk")["summary"].startswith("Saved to memos")
    assert world.count("memo.captured") == 3


def test_a_long_memo_is_cut_at_two_thousand_characters_in_the_store(world: _World) -> None:
    """The stored text is capped as before; the result shows one line of it."""
    done = world.keep("x" * 2500)
    assert len(done["text"]) == 2000
    assert len(done["summary"]) < 200


# --- a reminder --------------------------------------------------------------------------------


def test_a_reminder_takes_local_time_says_when_and_returns_its_id(world: _World) -> None:
    """``at`` is local ("2026-10-12 09:00"); the result names the time as he says it."""
    made = world.keep("call mum", kind="reminder", at="2026-10-12 09:00")
    assert made["summary"] == (
        'Reminder set for tomorrow (Mon) 9:00 am: "call mum". It rings then; nothing else to do.'
    )
    assert made["when"] == "tomorrow (Mon) 9:00 am"
    assert made["at"] == "2026-10-12T09:00:00-07:00"
    assert made["id"].startswith("reminder-")
    (event,) = iter_events_of_types(world.conn, ("reminder.scheduled",))
    assert event.payload["due_at_local"] == "2026-10-12T09:00:00-07:00"
    assert event.payload["text"] == "call mum"
    assert event.payload["reminder_id"] == made["id"]


def test_at_alone_means_a_reminder_and_an_offset_form_is_still_accepted(world: _World) -> None:
    """No kind but an ``at``: a reminder. A time with its own offset is read as that instant."""
    bare = world.keep("dentist", at="2026-10-13T15:30")
    assert bare["when"] == "Tue Oct 13, 3:30 pm"
    offset = world.keep("standup", kind="reminder", at="2026-10-12T17:00:00+00:00")
    assert offset["at"] == "2026-10-12T10:00:00-07:00"
    assert world.count("reminder.scheduled") == 2


@pytest.mark.parametrize(
    ("again", "stored"),
    [
        ({"text": "call mum", "at": "2026-10-12 09:00"}, 1),
        ({"text": "  Call   MUM ", "kind": "reminder", "at": "2026-10-12T09:00:30"}, 1),
        ({"text": "call mum", "kind": "reminder", "at": "2026-10-12T16:00:00+00:00"}, 1),
        ({"text": "call mum", "at": "2026-10-12 09:01"}, 2),
        ({"text": "call dad", "at": "2026-10-12 09:00"}, 2),
    ],
)
def test_the_same_reminder_text_and_minute_is_set_once(
    world: _World, again: dict[str, Any], stored: int,
) -> None:
    """Text and minute decide: the repeat returns the first id; a changed one is a new reminder."""
    first = world.keep("call mum", kind="reminder", at="2026-10-12 09:00")
    repeat = world.call("keep", again)
    assert (repeat["id"] == first["id"]) is (stored == 1)
    assert repeat["summary"].startswith("Already set for ") is (stored == 1)
    assert world.count("reminder.scheduled") == stored


@pytest.mark.parametrize(
    ("args", "code", "words"),
    [
        ({"kind": "reminder", "at": "soon"}, "invalid_time", "work the date out"),
        ({"kind": "reminder", "at": "tomorrow 9am"}, "invalid_time", "2026-10-12 09:00"),
        ({"kind": "reminder", "at": "2026-10-12"}, "invalid_time", "local date and time"),
        ({"kind": "reminder"}, "invalid_time", "needs at"),
        (
            {"at": "2026-10-11 18:00"}, "time_in_past",
            "is in the past (it is today (Sun) 7:55 pm now)",
        ),
        ({"at": "2028-01-01 09:00"}, "too_far_ahead", "more than a year"),
        ({"kind": "memo", "at": "2026-10-12 09:00"}, "invalid_arguments", "goes only with kind=r"),
        ({"kind": "fact", "at": "2026-10-12 09:00"}, "invalid_arguments", "goes only with kind=r"),
        ({"kind": "memo", "topic": "x"}, "invalid_arguments", "goes only with kind=fact"),
        ({"kind": "note"}, "invalid_arguments", "memo, reminder or fact"),
    ],
)
def test_a_bad_reminder_time_or_pair_is_refused_with_the_fix(
    world: _World, args: dict[str, Any], code: str, words: str,
) -> None:
    """Nothing is written; the error says what was wrong and what to pass instead."""
    refused = world.call("keep", {"text": "x", **args})
    assert refused["code"] == code
    assert words in refused["error"]
    assert world.count("reminder.scheduled") == world.count("memo.captured") == 0


def test_no_text_is_refused(world: _World) -> None:
    """An empty call names the argument and gives an example."""
    refused = world.call("keep", {"text": "  "})
    assert refused["code"] == "empty_text"
    assert "keep(text=" in refused["error"]
    assert world.call("keep", {})["code"] == "empty_text"


# --- cancel ------------------------------------------------------------------------------------


def test_cancel_by_id_says_what_was_cancelled_and_a_second_cancel_changes_nothing(
    world: _World,
) -> None:
    """Cancelled once; again it is "already cancelled"; an unknown id is the only error."""
    made = world.keep("call mum", at="2026-10-12 09:00")
    cancelled = world.call("keep", {"cancel": made["id"]})
    assert cancelled["summary"] == 'Cancelled the reminder for tomorrow (Mon) 9:00 am: "call mum".'
    assert (cancelled["kind"], cancelled["id"]) == ("reminder", made["id"])
    again = world.call("keep", {"cancel": made["id"]})
    assert again["summary"].startswith("Already cancelled, nothing changed: ")
    assert world.count("reminder.cancelled") == 1
    assert world.kept(kind="reminder")["items"] == []

    unknown = world.call("keep", {"cancel": "reminder-zzz"})
    assert unknown["code"] == "not_found"
    assert 'no reminder has the id "reminder-zzz"' in unknown["error"]
    assert 'kept(kind="reminder")' in unknown["error"]
    assert world.count("reminder.cancelled") == 1


def test_cancel_stands_alone(world: _World) -> None:
    """A cancel with new text would do two things at once: refused, nothing written."""
    made = world.keep("call mum", at="2026-10-12 09:00")
    for extra in ({"text": "x"}, {"at": "2026-10-12 10:00"}, {"topic": "t"}):
        refused = world.call("keep", {"cancel": made["id"], **extra})
        assert refused["code"] == "invalid_arguments"
    assert world.count("reminder.cancelled") == 0


def test_a_cancelled_reminder_can_be_set_again(world: _World) -> None:
    """The idempotency looks at waiting reminders: after a cancel the same one is new."""
    made = world.keep("call mum", at="2026-10-12 09:00")
    world.call("keep", {"cancel": made["id"]})
    again = world.keep("call mum", at="2026-10-12 09:00")
    assert again["summary"].startswith("Reminder set for ")
    assert again["id"] != made["id"]


# --- a fact ------------------------------------------------------------------------------------


def test_a_fact_goes_to_core_memory_once_and_a_topic_replaces_its_item(world: _World) -> None:
    """Saved, then "already"; the same topic rewrites its line; the origin is ``remember``."""
    assert world.memory is not None
    done = world.keep("10月12日", kind="fact", topic="生日")
    assert done["summary"] == 'Saved to long-term memory: "生日: 10月12日".'
    again = world.keep("10月12日", kind="fact", topic="生日")
    assert again["summary"] == 'Already in long-term memory, nothing new: "生日: 10月12日".'
    changed = world.keep("10月13日", kind="fact", topic="生日")
    assert changed["summary"].startswith("Saved to long-term memory")
    plain = world.keep("他不吃香菜", kind="fact")
    assert plain["text"] == "他不吃香菜"
    lines = [item["text"] for section in current_core_memory(world.memory).doc.values()
             for item in section]
    assert lines == ["生日: 10月13日", "他不吃香菜"]
    with closing(open_memory_db(world.memory)) as conn:
        origins = [row[0] for row in conn.execute("SELECT origin FROM core_memory ORDER BY rowid")]
    assert origins == ["migration", "remember", "remember", "remember"]


def test_a_fact_that_is_not_one_short_line_or_has_no_memory_is_refused(
    world: _World, tmp_path: Path,
) -> None:
    """Too long: use a memo. No memory.db on this host: the error says so."""
    too_long = world.keep("x" * 241, kind="fact")
    assert too_long["code"] == "too_long"
    assert world.keep("x", kind="fact", topic="t" * 41)["code"] == "too_long"
    bare = _World(tmp_path / "bare", memory=False)
    try:
        refused = bare.keep("他不吃香菜", kind="fact")
        assert refused["code"] == "unavailable"
        assert "kind=memo" in refused["error"]
        assert bare.keep("他不吃香菜")["summary"].startswith("Saved to memos")
    finally:
        bare.conn.close()


# --- kept --------------------------------------------------------------------------------------


def _seed(world: _World) -> None:
    for n in range(10):
        world.keep(f"memo {n}")
    world.keep("call mum", at="2026-10-12 09:00")
    world.keep("pay rent", at="2026-10-11 21:00")
    world.keep("10月12日", kind="fact", topic="生日")
    world.keep("他不吃香菜", kind="fact")


def test_kept_lists_reminders_then_memos_then_facts_eight_to_a_page(world: _World) -> None:
    """Soonest reminder first, memos newest first, facts newest first; page 2 continues."""
    _seed(world)
    one = world.kept()
    assert [(r["kind"], r["text"]) for r in one["items"]] == [
        ("reminder", "pay rent"), ("reminder", "call mum"),
        *[("memo", f"memo {n}") for n in range(9, 3, -1)],
    ]
    assert one["more"] is True
    assert one["items"][0]["when"] == "today (Sun) 9:00 pm"
    assert one["items"][1]["when"] == "tomorrow (Mon) 9:00 am"
    assert all(r["id"].startswith("reminder-") for r in one["items"][:2])
    assert all("id" not in r for r in one["items"][2:])
    assert one["summary"].startswith(
        "2 waiting reminders, 10 memos, 2 lasting facts (showing 1-8): "
        '"pay rent" (rings today (Sun) 9:00 pm); "call mum" (rings tomorrow (Mon) 9:00 am); '
        '"memo 9" (',
    )
    two = world.kept(page=2)
    assert [r["text"] for r in two["items"]] == [
        "memo 3", "memo 2", "memo 1", "memo 0", "他不吃香菜", "生日: 10月12日",
    ]
    assert two["more"] is False
    assert "(showing 9-14)" in two["summary"]
    # His facts are named in the summary, which a Tier 0 reply speaks, only when he asks for them.
    assert "他不吃香菜" not in two["summary"]
    assert "(saved Oct 11)" not in two["summary"]
    assert "他不吃香菜" in world.kept(kind="fact")["summary"]
    assert world.kept(page=3)["summary"] == "There is no page 3; what was kept ends at page 2."


def test_kept_by_kind_and_query(world: _World) -> None:
    """A kind narrows the list; a query keeps the rows whose text holds the words."""
    _seed(world)
    assert [r["text"] for r in world.kept(kind="reminder")["items"]] == ["pay rent", "call mum"]
    assert world.kept(kind="memo")["summary"].startswith("10 memos (showing 1-8): ")
    assert [r["text"] for r in world.kept(kind="fact")["items"]] == ["他不吃香菜", "生日: 10月12日"]
    found = world.kept(query="MUM")
    assert [r["text"] for r in found["items"]] == ["call mum"]
    assert found["summary"] == (
        '1 waiting reminder matching "mum": "call mum" (rings tomorrow (Mon) 9:00 am).'
    )
    assert world.kept(query="nothing like this")["summary"] == (
        'Nothing kept matches "nothing like this".'
    )


def test_kept_says_when_nothing_is_kept(world: _World) -> None:
    """Every empty list is a sentence."""
    assert world.kept()["summary"] == "Nothing kept yet."
    assert world.kept(kind="memo")["summary"] == "No memos kept."
    assert world.kept(kind="reminder")["summary"] == "No reminders waiting."
    assert world.kept(kind="fact")["summary"] == "No lasting facts kept."


def test_kept_shows_memo_times_in_his_zone(world: _World) -> None:
    """A memo saved at 02:55 UTC reads as 7:55 pm in Victoria, the day before, a day later."""
    world.now = datetime(2026, 10, 12, 2, 55, tzinfo=UTC)
    world.keep("buy milk")
    world.now += timedelta(days=1, hours=1)
    (row,) = world.kept()["items"]
    assert row["when"] == "yesterday (Sun) 7:55 pm"


def test_kept_stays_under_four_thousand_characters_with_long_texts(world: _World) -> None:
    """Eight rows of the longest memos still fit; each row is cut and the cut is an ellipsis."""
    for n in range(10):
        world.keep(f"{n} " + "long note " * 200)
    out = world.kept()
    assert len(json.dumps(out, ensure_ascii=False)) < 4000
    assert out["items"][0]["text"].endswith("…")


@pytest.mark.parametrize(
    ("args", "code"),
    [({"kind": "everything"}, "invalid_arguments"), ({"page": 0}, "invalid_arguments"),
     ({"page": "2"}, "invalid_arguments")],
)
def test_kept_refuses_arguments_that_do_not_fit(
    world: _World, args: dict[str, Any], code: str,
) -> None:
    """The error says what to pass."""
    refused = world.call("kept", args)
    assert refused["code"] == code
    assert "kept:" in refused["error"]


# --- Tier 0 ------------------------------------------------------------------------------------


def test_note_and_notelist_run_the_two_tools(world: _World) -> None:
    """`/note` keeps a memo and `/notelist` reads back what was kept, both without a model."""
    table = load_tier0_table(REPO / "config" / "tier0_patterns.yaml")
    router = CallerPrincipal.REGEX_ROUTER
    note = match_tier0("/note 买牛奶", table)
    assert note is not None
    assert (note.tool_name, note.tool_args) == ("keep", {"text": "买牛奶"})
    saved = world.call(note.tool_name, note.tool_args, router)
    assert saved["summary"] == 'Saved to memos: "买牛奶".'
    listing = match_tier0("/notelist", table)
    assert listing is not None
    assert (listing.tool_name, listing.tool_args) == ("kept", {})
    payload = world.call(listing.tool_name, listing.tool_args, router)
    spoken = render_tier0_response(listing, payload, "en")
    assert spoken == '1 memo: "买牛奶" (today (Sun) 7:55 pm).'


# --- decide() with a scripted model ------------------------------------------------------------


def test_the_model_saves_once_and_answers(tmp_path: Path) -> None:
    """One keep, one answer: the second request carries the sentence that says it was saved."""
    model = _ScriptedModel(
        ("keep", {"text": "回家修 emoji 功能"}), "好，记下了：回家修 emoji 功能。",
    )
    text, conn = _batch(tmp_path, model)
    try:
        assert text == "好，记下了：回家修 emoji 功能。"
        assert len(model.requests) == 2
        assert _count(conn, "memo.captured") == 1
        (result,) = _tool_texts(model.requests[-1][0])
        assert 'Saved to memos: \\"回家修 emoji 功能\\".' in result
        assert _guards(conn) == []
    finally:
        conn.close()


def test_a_model_that_saves_again_with_other_arguments_still_writes_one_row(
    tmp_path: Path,
) -> None:
    """The exam's twelve saves: each variation after the first is answered "already saved"."""
    text = "回家给你加个 emoji 功能"
    model = _ScriptedModel(
        ("keep", {"text": text}),
        ("keep", {"text": text, "kind": "memo"}),
        ("keep", {"text": f"{text} "}),
        ("keep", {"text": text.upper(), "kind": "memo"}),
        "好，记在备忘里了。",
    )
    answer, conn = _batch(tmp_path, model)
    try:
        assert answer == "好，记在备忘里了。"
        assert _count(conn, "memo.captured") == 1
        results = _tool_texts(model.requests[-1][0])
        assert "Saved to memos" in results[0]
        assert all("Already saved to memos, nothing new" in one for one in results[1:])
    finally:
        conn.close()


def test_an_answer_that_claims_a_save_before_any_call_is_sent_back_to_keep(tmp_path: Path) -> None:
    """ADR 0240 on the new tool: 「记下了」 with nothing kept goes back once, then keep runs."""
    model = _ScriptedModel("记下了。", ("keep", {"text": "明天是我生日"}), "好。")
    _, conn = _batch(tmp_path, model)
    try:
        assert [g["guard"] for g in _guards(conn)] == ["unbacked"]
        assert len(model.requests) == 3
    finally:
        conn.close()
