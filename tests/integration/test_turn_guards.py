"""ADR 0240: a call the model repeats is not run again; an answer no call backs goes back once.

Two acceptance checks. The first reads the guards' pure functions over a table of (answer,
calls) pairs, Chinese and English, the evidence phrases and the plain talk that must not trip
them. The second drives the real ``decide()`` loops, dispatcher and event log with a scripted
model (no provider is called): the batch loop, then the spoken route with and without the
structured reply (ADR 0114).
"""

# ruff: noqa: RUF001 - the replies under test are Chinese, with fullwidth punctuation
from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import (
    decide,
)
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.llm_stream import LLMTextDelta, LLMToolCallCompleted
from jarvis.decision.packet import assemble_packet
from jarvis.decision.pre_route import RoutineStreamRoute, spoken_risk_context
from jarvis.decision.response_run import start_response_run
from jarvis.decision.stream_gate import spoken_stream_policy
from jarvis.decision.turn_guards import (
    REPEATED_CALL,
    REPEATED_FAILED,
    UNBACKED_NOTE,
    TurnCall,
    calendar_in_view,
    repeated_call,
    ride_stop_in_view,
    unbacked_claim,
    unbacked_said,
)
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import append_record
from jarvis.surface.stream_emission import emit_permitted_segment
from tests.support.model import ScriptedModel, tool_call
from tests.support.phone import StepsHost, answer_with_record, history_of, logged_tool_call
from tests.support.turn_guards import guard_batch, guard_context, logged_guards, tool_texts

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from jarvis.decision.llm_stream import LLMStreamEvent

WROTE = TurnCall("keep", read_only=False, ok=True)
WRITE_FAILED = TurnCall("keep", read_only=False, ok=False)
READ = TurnCall("recall", read_only=True, ok=True)
DID, CHECK, LOOKED = "did it", "will check", "looked it up"


@pytest.mark.parametrize(
    ("text", "calls", "expected"),
    [
        # What the replay exam caught (Haiku 16 turns, Luna 9, Sonnet 4): claimed, never done.
        ("记下了", [], DID),
        ("我记住啦", [], DID),
        ("已记下：10月3日…", [READ], DID),
        ("好的，已经设好了，会提醒你。", [], DID),
        ("发出去了。", [], DID),
        ("已经放到屏幕上了。", [], DID),
        ("I've put the findings on screen", [], DID),
        ("Saved. Want me to add a reminder?", [], DID),
        ("Noted.", [], DID),
        ("I'll remind you at 3.", [], DID),
        ("The reminder is set.", [], DID),
        ("我会记住你说的。", [], DID),  # an emotional reply still promises a save: call remember
        ("记下了", [WRITE_FAILED], DID),  # the write failed, so it did not happen
        ('{"spoken": "记下了。", "written": ""}', [], DID),  # the structured reply (ADR 0114)
        # Announced, never looked up.
        ("我查一下10月1日的记录。", [], CHECK),
        ("我去看看", [], CHECK),
        ("让我看看", [], CHECK),
        ("稍等", [], CHECK),
        ("I need to check tomorrow's forecast", [], CHECK),
        ("Let me check that for you.", [], CHECK),
        # Backed: a write succeeded, or the lookup was a call.
        ("记下了", [WROTE], None),
        ("记下了", [READ, WROTE], None),
        ("我查一下", [READ], None),
        ("I'll check", [READ], None),
        # Plain talk, questions and reports of what exists.
        ("你记得吗", [], None),
        ("do you remember", [], None),
        ("Do you remember?", [], None),
        ("你记住了吗？", [], None),
        ("要我记下来吗？", [], None),
        ("Want me to save it?", [], None),
        ("我会一直在", [], None),
        ("提醒你，下午三点有会", [], None),
        ("备忘录里有3条", [READ], None),
        ("已经发现了问题", [], None),
        # Only what he hears counts: the screen part may list what already exists.
        ("<voice>面试是周二下午一点。</voice><document>- 已设置两个提醒</document>", [], None),
        ('{"spoken":"面试是周二下午一点。","written":"提醒：已设置两个提醒"}', [], None),
        ("<voice>好，记下了。</voice><document>- 回家修手机发烫</document>", [], DID),
        ('{"spoken":"已记下：周六吃饭。","written":""}', [], DID),
        # "On screen" is true when the answer carries its written part.
        ('{"spoken":"时间我写在屏幕上了。","written":"- 10:00 产品会"}', [], None),
        ("<voice>I've put the list on screen.</voice><document>- one</document>", [], None),
        ("I've put the findings on screen.", [], DID),
        ("我看你是累了", [], None),
        ("我找到了", [], None),
        ("", [], None),
        # Said to have been looked up, in a turn with no call (replay exam 2026-10-12, Haiku).
        ("这周的话，我查了你的默认日历，目前没有安排。", [], LOOKED),
        ("Your calendar is clear tomorrow.", [], LOOKED),
        ("我查过了，明天没有会。", [], LOOKED),
        ("已经查过了，没有新邮件。", [], LOOKED),
        ("看了一下你的日历，下午三点有牙医。", [], LOOKED),
        ("你的日历明天是空的。", [], LOOKED),
        ("日历上没有安排。", [], LOOKED),
        ("I checked your calendar and you are free.", [], LOOKED),
        ("I've just checked, nothing at 3.", [], LOOKED),
        ("I looked through your inbox, nothing new.", [], LOOKED),
        ("According to your calendar, you have a meeting at 3.", [], LOOKED),
        ("You have nothing on your calendar tomorrow.", [], LOOKED),
        ('{"spoken":"我查了一下，周五没有安排。","written":""}', [], LOOKED),
        ("<voice>I checked your reminders.</voice><document>- one</document>", [], LOOKED),
        ("Done. Your calendar is clear.", [WRITE_FAILED], None),  # a call was made: not this guard
        # A lookup that was a call, or a refused one, is not a claim of one without a call.
        ("我查了你的日历，明天没有安排。", [READ], None),
        ("Your calendar is clear tomorrow.", [READ], None),
        ("I checked your calendar and you are free.", [WRITE_FAILED], None),
        # Plain talk: 看了一下 at something that is not his data, and other things looked at.
        ("我看了一下你说的这个想法，挺不错的。", [], None),
        ("我看了一下这张图，挺好看的。", [], None),
        ("I looked at your essay and it reads well.", [], None),
        ("I looked it over and it is fine.", [], None),
        ("I checked out the new cafe yesterday.", [], None),
        ("你上次说过日历要同步。", [], None),
        ("Your calendar app is open on the phone.", [], None),
        ("你的日历有点乱。", [], None),
        # Questions, denials and offers are not claims.
        ("要我查了你的日历再告诉你吗？", [], None),
        ("Should I say I checked your calendar?", [], None),
        ("你的日历同步好了吗", [], None),
        ("我还没查过你的日历。", [], None),
        ("我没有查过。", [], None),
        ("I haven't checked your calendar yet.", [], None),
        ("我可以帮你查看过去的记录。", [], None),
        # The answer cites the program's state: not a claim of a lookup.
        ("The state line says your calendar is clear until 3.", [], None),
        ("状态行里写着你的日历下午是空的。", [], None),
        ("Per the program, the next item is the dentist at 3:30.", [], None),
        # An answer from what the block holds needs no call: mail, reminders, the day's state.
        ("邮箱里没有新邮件。", [], None),
        ("Your pending reminders: the dentist call at 9 tomorrow.", [], None),
        ("你半小时前还在写 my_day 的测试。", [], None),
        ("Your next stop is Shelbourne, about 12 minutes from Hillside.", [], None),
    ],
)
def test_the_answer_table(text: str, calls: list[TurnCall], expected: str | None) -> None:
    """Each (answer, calls) reads as the guard should: a claim, a promise, or plain talk."""
    assert unbacked_said(text, calls) == expected
    note = unbacked_claim(text, calls)
    assert note == (None if expected is None else UNBACKED_NOTE.format(said=expected))


def test_a_long_answer_that_mentions_a_lookup_is_an_answer() -> None:
    """Only an answer that just announces the lookup is a promise."""
    text = "你上次问过这个：" + "会议定在周五下午，地点在三楼，" * 6 + "我再查一下细节。"
    assert unbacked_said(text, []) is None


CAL_LINE = 'Next on his calendar: 3:30 pm "Dentist".'


@pytest.mark.parametrize(
    ("text", "seen", "expected"),
    [
        # What his calendar holds is backed when the block carries the line, not otherwise.
        ("Your calendar is clear tomorrow.", True, None),
        ("Your calendar is clear tomorrow.", False, LOOKED),
        ("你的日历明天是空的。", True, None),
        ("你的日历明天是空的。", False, LOOKED),
        ("根据你的日历，下午三点半有牙医。", True, None),
        ("根据你的日历，下午三点半有牙医。", False, LOOKED),
        ("You have nothing on your calendar tomorrow.", True, None),
        ("You have nothing on your calendar tomorrow.", False, LOOKED),
        ("According to your calendar, the dentist is at 3:30.", True, None),
        ("According to your calendar, the dentist is at 3:30.", False, LOOKED),
        ("日历上没有安排。", True, None),
        ("日历上没有安排。", False, LOOKED),
        # The block is not a lookup: saying one was made still needs a call.
        ("我查了日历，明天没有安排。", True, LOOKED),
        ("我查了日历，明天没有安排。", False, LOOKED),
        ("看了一下你的日历，下午三点半有牙医。", True, LOOKED),
        ("I checked your calendar and you are free.", True, LOOKED),
        ("I looked through your inbox, nothing new.", True, LOOKED),
        ("已经查过了，没有新邮件。", True, LOOKED),
        # Plain talk and the other two cases are unchanged by the line.
        ("我看了一下你说的这个想法，挺不错的。", True, None),
        ("记下了", True, DID),
        ("我查一下", True, CHECK),
    ],
)
def test_the_calendar_line_backs_what_his_calendar_holds_and_nothing_else(
    text: str, seen: bool, expected: str | None,  # noqa: FBT001 - a parametrized table cell
) -> None:
    """Calendar contents pass with the line in view; an explicit lookup claim still bounces."""
    assert unbacked_said(text, [], calendar_seen=seen) == expected
    note = unbacked_claim(text, [], calendar_seen=seen)
    assert note == (None if expected is None else UNBACKED_NOTE.format(said=expected))


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (CAL_LINE, True),
        ('Next on his calendar (2 not confirmed): 3:30 pm "Dentist".', True),
        ('Next on his calendar (read 4 minutes ago): 3:30 pm "Dentist".', True),  # an older block
        (f"Time: 2026-10-12T09:00-07:00 Monday\nDoing now (as of 5 min ago): X.\n{CAL_LINE}", True),
        ("Doing now (as of 5 minutes ago, at 9:00 am): Writing.", False),
        ("", False),
        # Only the line itself: his calendar named in another line is not it.
        ('Dashboard: he has "Next on his calendar (x)" open.', False),
        ("Pending reminders (1): call about Next on his calendar (later)", False),
        ("Next on his calendar", False),  # no colon: not the producer's line
        ("Next on his calendar (later) he has a dentist", False),
    ],
)
def test_the_calendar_line_is_found_only_as_a_line_of_its_own(state: str, expected: bool) -> None:  # noqa: FBT001
    """The state block's own line starts with the heading, its bracket if any, and a colon."""
    assert calendar_in_view(state) is expected


@pytest.mark.parametrize(
    ("text", "seen", "expected"),
    [
        # The ride line shows his stop set and the warning with it (ADR 0263): saying so is backed.
        ("好，下车站已经设成 Gum St，快到站前一站我会提醒你。提醒已经设好了。", True, None),
        ("下车站设好了，到站前一站会提醒你。", True, None),
        ("Your stop is set, and I'll remind you one stop before it.", True, None),
        ("提醒是之前设好的，下车前一站我会叫你。", True, None),
        # Without the line, or about something else, a claim of this kind is still unbacked.
        ("好，下车站已经设成 Gum St，快到站前一站我会提醒你。提醒已经设好了。", False, DID),
        ("提醒已经设好了。", True, DID),  # nothing says it is about his stop
        ("好，明天九点的会提醒你，已设好了。", True, DID),
        ("下车站设好了，也记下了你的新提醒。", True, DID),  # 记下 is not what the line shows
        ("下车站设好了，我也发出去了。", True, DID),
        ("I've sent it. Your stop is set.", True, DID),
        # The line backs claims only; a lookup is still an act.
        ("我查了，下车站是 Gum St。", True, LOOKED),
    ],
)
def test_the_ride_line_backs_the_stop_and_the_warning_and_nothing_else(
    text: str, seen: bool, expected: str | None,  # noqa: FBT001 - a parametrized table cell
) -> None:
    """A set-claim about his get-off stop passes with the ride line in view; others bounce."""
    assert unbacked_said(text, [], ride_stop_seen=seen) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (RIDE_LINE_SET := "Bus ride: he is on route 26. His stop: Gum St, 3 stops left.", True),
        ("Time: 9:00\n" + RIDE_LINE_SET, True),
        ("Bus ride: on route 26. His stop is unknown: ask him once where he gets off.", False),
        ("Bus ride: he is on route 26. His stop is still unknown (you already asked).", False),
        ("Pending reminders (1): note Bus ride: His stop: Gum St", False),
        ("", False),
    ],
)
def test_the_ride_line_is_found_only_as_a_line_of_its_own_with_a_stop(
    state: str, expected: bool,  # noqa: FBT001
) -> None:
    """Only the producer's line, and only once it carries a stop, shows the stop set."""
    assert ride_stop_in_view(state) is expected


RECALL = TurnCall("recall", read_only=True, ok=True, key="a")
RECALL_FAILED = TurnCall("recall", read_only=True, ok=False, key="a")
LISTED = TurnCall("kept", read_only=True, ok=True)
MEMO_A = TurnCall("keep", read_only=False, ok=True, key="a")
MEMO_X = TurnCall("keep", read_only=False, ok=True, key="x")
MEMO_X_FAILED = TurnCall("keep", read_only=False, ok=False, key="x")


@pytest.mark.parametrize(
    ("calls", "name", "key", "expected"),
    [
        ([], "recall", "a", None),
        ([RECALL], "recall", "a", REPEATED_CALL),
        ([RECALL], "recall", "b", None),  # other arguments
        ([RECALL], "get_briefing", "a", None),  # other tool
        ([RECALL_FAILED], "recall", "a", REPEATED_FAILED),  # it failed: change something
        ([RECALL, MEMO_X], "recall", "a", None),  # a write since: the repeat may differ
        ([RECALL, MEMO_X_FAILED], "recall", "a", REPEATED_CALL),  # it failed, nothing changed
        ([RECALL, LISTED], "recall", "a", REPEATED_CALL),  # a read changes nothing
        ([MEMO_A, RECALL], "recall", "a", REPEATED_CALL),  # the write came before the read
        ([MEMO_A], "keep", "a", REPEATED_CALL),  # the same memo again
    ],
)
def test_the_repeat_table(
    calls: list[TurnCall], name: str, key: str, expected: str | None,
) -> None:
    """A call identical to an earlier one is refused unless a write succeeded in between."""
    assert repeated_call(calls, name, key) == expected


# --- decide() with a scripted model ------------------------------------------------------


def _count(conn: sqlite3.Connection, event_type: str) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM events WHERE type=?", (event_type,)).fetchone()[0]
    )


def _notes(messages: Sequence[dict[str, Any]], said: str) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("content") == UNBACKED_NOTE.format(said=said)]


MEMO = ("schedule", {"action": "add", "what": "明天开会"})


def test_an_identical_repeat_is_not_run_and_gets_the_note(tmp_path: Path) -> None:
    """(a) The same read twice in a turn: one run, and the second call's result is the note."""
    model = ScriptedModel(tool_call("kept"), tool_call("kept"), "没有备忘。")
    text, conn = guard_batch(tmp_path, model)
    try:
        assert text == "没有备忘。"
        assert _count(conn, "action.result_observed") == 1
        assert tool_texts(model.requests[-1].messages)[1] == REPEATED_CALL
        assert logged_guards(conn) == [{"turn_id": "T_guard", "guard": "repeat", "tool": "kept"}]
    finally:
        conn.close()


def test_a_repeat_of_a_call_that_failed_names_the_fix(tmp_path: Path) -> None:
    """The same failing call again is refused with the note that says to change what failed."""
    empty = ("keep", {"text": ""})
    model = ScriptedModel(tool_call(*empty), tool_call(*empty), "没记成。")
    _, conn = guard_batch(tmp_path, model)
    try:
        assert _count(conn, "action.result_observed") == 1
        failed, repeat = tool_texts(model.requests[-1].messages)
        assert "empty_text" in failed
        assert repeat == REPEATED_FAILED
    finally:
        conn.close()


def test_the_same_memo_twice_is_saved_once(tmp_path: Path) -> None:
    """A write repeated unchanged (schedule, the same words) is refused too."""
    model = ScriptedModel(tool_call(*MEMO), tool_call(*MEMO), "记好了。")
    _, conn = guard_batch(tmp_path, model)
    try:
        assert _count(conn, "thing.added") == 1
        assert tool_texts(model.requests[-1].messages)[1] == REPEATED_CALL
        assert _count(conn, "turn.guard_fired") == 1
    finally:
        conn.close()


def test_a_repeat_after_a_write_runs(tmp_path: Path) -> None:
    """(b) list, write, list: the second list may differ, so it runs."""
    model = ScriptedModel(tool_call("kept"), tool_call(*MEMO), tool_call("kept"), "有一条。")
    _, conn = guard_batch(tmp_path, model)
    try:
        assert _count(conn, "action.result_observed") == 3
        assert REPEATED_CALL not in tool_texts(model.requests[-1].messages)
        assert _count(conn, "turn.guard_fired") == 0
    finally:
        conn.close()


def test_a_claim_with_no_call_goes_back_once_and_the_call_follows(tmp_path: Path) -> None:
    """(c) 「记下了」 with nothing saved: one note, then the model saves, then the answer stands."""
    model = ScriptedModel("记下了。", tool_call(*MEMO), "好了，明天开会记在备忘录里了。")
    text, conn = guard_batch(tmp_path, model)
    try:
        assert text == "好了，明天开会记在备忘录里了。"
        assert len(model.requests) == 3
        second = model.requests[1].messages
        (note,) = _notes(second, DID)
        assert note["role"] == "user"
        assert second[second.index(note) - 1] == {"role": "assistant", "content": "记下了。"}
        assert _count(conn, "thing.added") == 1
        assert logged_guards(conn) == [{"turn_id": "T_guard", "guard": "unbacked", "said": DID}]
    finally:
        conn.close()


def test_a_did_line_in_the_history_is_no_backing_for_a_claim_in_this_turn(tmp_path: Path) -> None:
    """ADR 0267: an earlier turn's [You did: ...] is not a call; 「记下了」 now still goes back."""
    host = StepsHost(tmp_path)
    logged_tool_call(host, "T_old", "keep", {"text": "上周的事"}, take=0)
    append_record(host.memory, record_id="u0", source="allen", text="把上周的事记一下")
    answer_with_record(host, "T_old", "好。")
    history = history_of(host)
    assert history[-1]["content"].endswith("好。\n[You did: saved a note]")
    model = ScriptedModel("记下了。", tool_call(*MEMO), "好了，明天开会记在备忘录里了。")
    text, conn = guard_batch(tmp_path, model, history=history)
    try:
        assert text == "好了，明天开会记在备忘录里了。"
        assert history[-1] in model.requests[0].messages
        assert len(_notes(model.requests[1].messages, DID)) == 1
        assert logged_guards(conn) == [{"turn_id": "T_guard", "guard": "unbacked", "said": DID}]
        assert _count(conn, "thing.added") == 1
    finally:
        conn.close()


def test_the_bounce_happens_once_a_turn(tmp_path: Path) -> None:
    """(d) The answer after the note stands, claim or not."""
    model = ScriptedModel("记下了。", "记下了。")
    text, conn = guard_batch(tmp_path, model)
    try:
        assert text == "记下了。"
        assert len(model.requests) == 2
        assert _count(conn, "turn.guard_fired") == 1
        assert _count(conn, "turn.ended") == 1
    finally:
        conn.close()


def test_an_announced_lookup_goes_back_and_the_lookup_follows(tmp_path: Path) -> None:
    """「我查一下」 and no call: the note says it will check; the model then looks."""
    model = ScriptedModel("我查一下。", tool_call("kept"), "还没有备忘。")
    text, conn = guard_batch(tmp_path, model)
    try:
        assert text == "还没有备忘。"
        assert _notes(model.requests[1].messages, CHECK)
        assert _count(conn, "action.result_observed") == 1
    finally:
        conn.close()


def test_a_claimed_lookup_goes_back_and_the_lookup_follows(tmp_path: Path) -> None:
    """「我查了你的日历」 and no call: the note says it looked it up; the model then looks."""
    model = ScriptedModel("我查了你的日历，没有安排。", tool_call("kept"), "还没有备忘。")
    text, conn = guard_batch(tmp_path, model)
    try:
        assert text == "还没有备忘。"
        assert _notes(model.requests[1].messages, LOOKED)
        assert _count(conn, "action.result_observed") == 1
        assert logged_guards(conn) == [{"turn_id": "T_guard", "guard": "unbacked", "said": LOOKED}]
    finally:
        conn.close()


def test_a_lookup_claimed_after_a_real_call_is_not_sent_back(tmp_path: Path) -> None:
    """The call made earlier this turn backs it; the answer stands with no note."""
    model = ScriptedModel(tool_call("kept"), "我查了，还没有备忘。")
    text, conn = guard_batch(tmp_path, model)
    try:
        assert text == "我查了，还没有备忘。"
        assert len(model.requests) == 2
        assert _count(conn, "turn.guard_fired") == 0
    finally:
        conn.close()


def test_a_calendar_answer_the_block_backs_is_not_sent_back(tmp_path: Path) -> None:
    """With the calendar line in this turn's state, 「你的日历下午是空的」 stands with no call."""
    model = ScriptedModel("你的日历明天是空的。")
    text, conn = guard_batch(tmp_path, model, live_context=(CAL_LINE,))
    try:
        assert text == "你的日历明天是空的。"
        assert len(model.requests) == 1
        assert _count(conn, "turn.guard_fired") == 0
    finally:
        conn.close()


def test_the_same_answer_without_the_line_goes_back(tmp_path: Path) -> None:
    """No calendar line in the state: the claim is unbacked and the lookup follows the note."""
    model = ScriptedModel("你的日历明天是空的。", tool_call("kept"), "没有。")
    _, conn = guard_batch(tmp_path, model)
    try:
        assert _notes(model.requests[1].messages, LOOKED)
        assert _count(conn, "action.result_observed") == 1
    finally:
        conn.close()


def test_saying_the_calendar_was_checked_goes_back_even_with_the_line(tmp_path: Path) -> None:
    """The block is not a lookup: 「我查了日历」 with no call is sent back whatever is in view."""
    model = ScriptedModel("我查了日历，三点半有牙医。", tool_call("kept"), "好。")
    _, conn = guard_batch(tmp_path, model, live_context=(CAL_LINE,))
    try:
        assert _notes(model.requests[1].messages, LOOKED)
        assert logged_guards(conn) == [{"turn_id": "T_guard", "guard": "unbacked", "said": LOOKED}]
    finally:
        conn.close()


def test_no_bounce_when_the_budget_wraps_the_turn_up(tmp_path: Path) -> None:
    """(e) The budget's own no-tool answer is final, claim or not."""
    wrapped = ScriptedModel(tool_call("kept", {"n": 1}), tool_call("kept", {"n": 2}), "记下了。")
    text, conn = guard_batch(tmp_path, wrapped, budget=2)
    try:
        assert text == "记下了。"
        assert len(wrapped.requests) == 3
        assert not wrapped.requests[2].tools  # the wrap-up offers no tools
        assert _count(conn, "turn.guard_fired") == 0
    finally:
        conn.close()


def test_no_bounce_on_the_last_request_the_budget_allows(tmp_path: Path) -> None:
    """There is no request left to run after a note, so the answer stands."""
    last = ScriptedModel(tool_call("kept"), "记下了。")
    text, conn = guard_batch(tmp_path, last, budget=2)
    try:
        assert text == "记下了。"
        assert len(last.requests) == 2
        assert _count(conn, "turn.guard_fired") == 0
    finally:
        conn.close()


# --- the spoken route -----------------------------------------------------------------------


class _Stream:
    """One scripted request's typed events, the way the runtime's stream hands them over."""

    def __init__(self, events: list[LLMStreamEvent]) -> None:
        self._events = events

    def __iter__(self) -> Iterator[LLMStreamEvent]:
        return iter(self._events)

    def cancel(self, reason: str) -> None:
        """Never cancelled here."""

    def interrupt_when(self, predicate: Callable[[], bool]) -> None:
        """Nothing here waits on Jev."""

    def close(self) -> None:
        """Nothing to release."""

    @property
    def disposition(self) -> None:
        return None


class _SpokenModel:
    """The client the spoken route opens streams with; ``script`` holds each request's events."""
    native_tool_search = False  # Jarvis's own tool_search (ADR 0287)

    provider = "openai"
    model = "stub-model"

    def __init__(self, *requests: list[LLMStreamEvent]) -> None:
        self.script = list(requests)
        self.sent: list[list[dict[str, Any]]] = []

    def request_tier(self, tier: str) -> str:
        return tier

    def stream_events(self, **request: Any) -> object:  # noqa: ANN401 - the client's keyword surface
        self.sent.append([dict(m) for m in request["messages"]])
        return object()


def _say(text: str, *, structured: bool) -> list[LLMStreamEvent]:
    body = json.dumps({"spoken": text, "written": ""}, ensure_ascii=False) if structured else text
    return [
        LLMTextDelta(
            llm_request_id="r", provider_response_id=None, text=body, content_block_index=0,
            phase="final_answer",
        )
    ]


def _propose(name: str, args: dict[str, Any]) -> list[LLMStreamEvent]:
    return [
        LLMToolCallCompleted(
            llm_request_id="r", provider_response_id=None, call_index=0, call_id="c1",
            name=name, arguments_json=json.dumps(args),
        )
    ]


def _deltas(text: str, *, structured: bool, size: int = 4) -> list[LLMStreamEvent]:
    """The same answer as ``_say`` but as several deltas, the way a provider streams it."""
    body = json.dumps({"spoken": text, "written": ""}, ensure_ascii=False) if structured else text
    return [
        LLMTextDelta(
            llm_request_id="r", provider_response_id=None, text=body[n : n + size],
            content_block_index=0, phase="final_answer",
        )
        for n in range(0, len(body), size)
    ]


def _spoken_turn(
    tmp_path: Path, model: _SpokenModel, *, structured: bool = False,
    live_context: tuple[str, ...] = (),
) -> tuple[Any, sqlite3.Connection]:
    """Run one spoken turn through ``decide()`` on the scripted model; the caller closes it."""
    conn = open_event_log(tmp_path / "events.db")
    trigger = emit_event(
        conn,
        type="utterance.received",
        payload={"transcript": "把明天开会记一下", "turn_id": "T_say", "channel": "speech"},
        correlation={"turn_id": "T_say"},
    )
    context = spoken_risk_context(
        assemble_packet(trigger, conn), response_id="R_say", turn_id="T_say",
    )
    policy = spoken_stream_policy(context, preset_snapshot_hash="b" * 64)
    factory = LLMSessionFactory(
        {"provider": "openai", "model": "synthetic", "base_url": "https://example.invalid"}
    )
    run = start_response_run(
        conn,
        turn_id="T_say",
        trigger_event_uid=trigger.event_uid,
        request_client=factory.create(factory.snapshot(), response_id="R_say"),
        policy=policy,
        response_id="R_say",
    )
    route = RoutineStreamRoute(
        policy=policy,
        context=context,
        open_stream=lambda _handle: _Stream(model.script.pop(0)),
        emit_segment=lambda permit, text: emit_permitted_segment(
            conn, permit, text, query=context.user_request,
            attention_channel=context.attention_channel,
        ).event,
        segment_guard=run.admission_guard,
        structured=structured,
    )
    ctx = guard_context(conn, tmp_path, model, 8, live_context)
    return decide(trigger, replace(ctx, routine_stream=route)), conn


def _segments(conn: sqlite3.Connection) -> str:
    """Everything the turn put out as spoken segments, in order."""
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE type LIKE '%segment%' OR type LIKE '%chunk%'"
    ).fetchall()
    return "".join(str(json.loads(r[0]).get("text", "")) for r in rows)


@pytest.mark.parametrize("structured", [False, True])
def test_the_spoken_route_never_speaks_a_bounced_claim(
    tmp_path: Path, *, structured: bool,
) -> None:
    """ADR 0263: a claim no call backs is held, judged and dropped, never spoken."""
    model = _SpokenModel(
        _deltas("记下了。", structured=structured),
        _propose(*MEMO),
        _deltas("好了，明天开会记在备忘录里了。", structured=structured),
    )
    result, conn = _spoken_turn(tmp_path, model, structured=structured)
    try:
        assert result.response_plan is not None
        spoken = result.response_plan.text
        assert spoken == "好了，明天开会记在备忘录里了。"
        assert _segments(conn) == spoken  # one answer went out, in the chunks and in the plan
        assert len(model.sent) == 3
        assert _notes(model.sent[1], DID)
        assert _count(conn, "thing.added") == 1
        assert logged_guards(conn) == [{"turn_id": "T_say", "guard": "unbacked", "said": DID}]
    finally:
        conn.close()


def test_a_sentence_before_the_claim_is_spoken_at_once_and_not_repeated(tmp_path: Path) -> None:
    """The text ahead of a claim goes out as it completes; the answer after the note adds to it."""
    model = _SpokenModel(
        _deltas("好的，我来办。记下了。", structured=False),
        _propose(*MEMO),
        _deltas("明天开会已经记在备忘录里。", structured=False),
    )
    result, conn = _spoken_turn(tmp_path, model)
    try:
        assert result.response_plan is not None
        assert result.response_plan.text == "好的，我来办。\n\n明天开会已经记在备忘录里。"
        assert "记下了" not in _segments(conn)
    finally:
        conn.close()


RIDE_STATE = (
    "Bus ride: he is on route 26, boarded at Alder St at 9:10 am. Next stop: Cedar Ave."
    " His stop: Gum St, 3 stops left, arrives about 9:43 am (in 12 min). Times are from BC"
    " Transit's live bus position.",
)
STOP_SET = "好，下车站是 Gum St，大约 9:43 到。快到站前一站我会提醒你。提醒已经设好了。"


def test_the_ride_line_backs_the_stop_being_set_so_nothing_goes_back(tmp_path: Path) -> None:
    """The stop was set turns ago and the state says so: saying it is set needs no new write."""
    model = _SpokenModel(_deltas(STOP_SET, structured=False))
    result, conn = _spoken_turn(tmp_path, model, live_context=RIDE_STATE)
    try:
        assert result.response_plan is not None
        assert result.response_plan.text == STOP_SET
        assert len(model.sent) == 1
        assert _count(conn, "turn.guard_fired") == 0
    finally:
        conn.close()


def test_the_same_claim_without_the_ride_line_still_goes_back(tmp_path: Path) -> None:
    """With no ride line to show it, the claim is unbacked: held, dropped, and the call follows."""
    model = _SpokenModel(
        _deltas(STOP_SET, structured=False),
        _propose(*MEMO),
        _deltas("好了，记下了下车站。", structured=False),
    )
    result, conn = _spoken_turn(tmp_path, model)
    try:
        assert result.response_plan is not None
        assert "提醒已经设好了" not in result.response_plan.text
        assert "提醒已经设好了" not in _segments(conn)
        assert logged_guards(conn) == [{"turn_id": "T_say", "guard": "unbacked", "said": DID}]
    finally:
        conn.close()
