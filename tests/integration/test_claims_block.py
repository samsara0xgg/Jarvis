"""ADR 0320: her answer opens with a claims block, and code checks it against receipts first.

The first part reads the block's filter and checker over tables. The rest drives the real
``decide()`` loops, dispatcher and event log with the one scripted model (no provider is called):
the batch loop, then the spoken route plain and structured (ADR 0114). Each test asserts what
someone can see: the event rows, the request the model was sent, what was spoken, the plan.
"""

# ruff: noqa: RUF001 - the replies under test are Chinese, with fullwidth punctuation
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import claims as claims_check
from jarvis.decision.reaction_markup import ReactionFilter, split_claims
from jarvis.shared.receipts import Receipt, receipt_id
from jarvis.state.turn_steps import turn_receipts
from tests.support.model import Reply, Request, ScriptedModel, tool_call
from tests.support.phone import StepsHost, answer_with_record, history_of, logged_tool_call
from tests.support.turn_guards import claims_rows, guard_batch, segments, spoken_turn, tool_texts

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence
    from pathlib import Path

MEMO = ("schedule", {"action": "add", "what": "明天开会"})
FAILING = ("keep", {"text": ""})  # a change that fails: empty_text
ID = re.compile(r'"receipt": "(r[0-9a-f]{6})"')


# --- the block's filter ------------------------------------------------------------------------


def _filtered(pieces: Sequence[str]) -> tuple[str, str | None]:
    """What a reply fed in these pieces shows, and the block it declared."""
    parsed = ReactionFilter()
    shown = "".join(parsed.feed(piece) for piece in pieces) + parsed.finish()
    return shown, parsed.claims


@pytest.mark.parametrize(
    ("pieces", "shown", "block"),
    [
        (["<claims></claims>好的。"], "好的。", ""),
        (["<claims>\ndid r3f9a1c: memo\n</claims>记好了。"], "记好了。", "did r3f9a1c: memo"),
        (list("<claims>looked r1b2c3d: x</claims>在这。"), "在这。", "looked r1b2c3d: x"),
        (["<react>👍</react><claims></claims>好。"], "好。", ""),
        (["<claims></claims><react>👍</react>好。"], "好。", ""),
        (["<CLAIMS>offer kept: lookup</CLAIMS>好。"], "好。", "offer kept: lookup"),
        # A block left open is cut to the end of the reply, and the reply has no block.
        (["<claims>did r3f9a1c: memo\n记好了。"], "", None),
        (["好。<claims>did r3f9a1c: memo"], "好。", None),
        # No block, or a start that is not one, is text.
        (["好，记好了。"], "好，记好了。", None),
        (["a <cl", "aim> b"], "a <claim> b", None),
        (["1 <"], "1 <", None),
        # Only the first closed block counts; a later one is cut too.
        (["<claims>a</claims>好<claims>b</claims>。"], "好。", "a"),
    ],
)
def test_the_filter_cuts_the_block_and_keeps_the_first_closed_one(
    pieces: list[str], shown: str, block: str | None,
) -> None:
    """The block never shows, however the reply is cut into pieces; unclosed means no block."""
    assert _filtered(pieces) == (shown, block)
    assert split_claims("".join(pieces))[0::2] == (shown, block)


# --- the checker ---------------------------------------------------------------------------------

DONE_CHANGE = Receipt("done", reads_only=False, line="Kept it", object="thing:aaaaaa")
DONE_READ = Receipt("done", reads_only=True, line="read kept()")
FAILED_CHANGE = Receipt("failed", reads_only=False, line="keep() gave error", failure="dont_retry")
SHOWN = {"r000001": DONE_CHANGE, "r000002": DONE_READ, "r000003": FAILED_CHANGE}


@pytest.mark.parametrize(
    ("block", "why"),
    [
        ("", []),
        ("did r000001: set it", [""]),
        ("did r000001, r000001: set it twice", [""]),
        ("looked r000002: read it", [""]),
        ("did r000002: set it", ["receipt r000002 is a read, not a change"]),
        ("looked r000001: read it", ["receipt r000001 is a change, not a read"]),
        ("did r000003: set it", ["receipt r000003 is failed, not done"]),
        ("did r0000ff: set it", ["no such receipt r0000ff"]),
        ("did r000001, r0000ff: both", ["no such receipt r0000ff"]),
        ("did : nothing cited", ["it cites no receipt"]),
        ("offer keep: save it", [""]),
        ("offer kept: list them", ["kept only reads and is on the menu"]),
        ("offer nowhere: x", [""]),
        ("did r000001: a\nsome stray line\nlooked r000002: b", ["", ""]),
        # A list mark, no space before the colon, or a full-width colon is still an entry.
        ("- did r000001: set it\n* looked r000002: read it", ["", ""]),
        ("1. did r000002: set it", ["receipt r000002 is a read, not a change"]),
        ("did r000001：set it", [""]),
        ("did: nothing cited", ["it cites no receipt"]),
        ("didn't: a word, not a kind", []),
    ],
)
def test_each_entry_is_judged_against_the_receipt_it_cites(block: str, why: list[str]) -> None:
    """A change backs ``did``, a read backs ``looked``; a read-only tool on the menu is no offer."""
    verdicts = claims_check.check(block, shown=SHOWN, offerable=lambda tool: tool == "kept")
    assert [v.why for v in verdicts] == why


@pytest.mark.parametrize(
    ("block", "unread"),
    [
        (None, 0),
        ("", 0),
        ("\n  \n", 0),
        ("- did r000001: set it", 0),
        ("none", 1),
        ("did r000001: a\nsome stray line\nlooked r000002: b", 1),
    ],
)
def test_lines_with_words_but_no_entry_are_counted(block: str | None, unread: int) -> None:
    """An empty block counts none; a line she wrote that reads as no entry counts one."""
    assert claims_check.unread(block) == unread


# --- the loops -----------------------------------------------------------------------------------


def _ids(request: Request) -> list[str]:
    """The receipt ids in the tool results the request carries, oldest first."""
    return ID.findall("\n".join(tool_texts(request.messages)))


def _then(*lines: str, say: str = "好了。") -> Callable[[Request], Reply]:
    """A reply whose block cites the turn's receipts in call order: ``{0}`` is the first."""

    def reply(request: Request) -> Reply:
        found = _ids(request)
        cited = "\n".join(line.format(*found) for line in lines)
        return Reply(f"<claims>\n{cited}\n</claims>{say}", chunk=4)

    return reply


def _structured(text: str) -> str:
    """A reply as the structured route's schema has it: the block's lines become ``claims``."""
    block, _, rest = text.partition("</claims>")
    lines = block.removeprefix("<claims>").strip()
    return json.dumps({"claims": lines, "spoken": rest, "written": ""}, ensure_ascii=False)


class Route:
    """One way a turn runs: the batch loop, or the spoken route plain or structured."""

    def __init__(self, name: str, tmp_path: Path) -> None:
        """Name the route and where its files go."""
        self.name = name
        self.tmp_path = tmp_path

    def run(self, model: ScriptedModel, **extra: Any) -> tuple[str, sqlite3.Connection]:  # noqa: ANN401
        """The answer the turn ended with, and its log (the caller closes it)."""
        if self.name == "text":
            return guard_batch(self.tmp_path, model, **extra)
        result, conn = spoken_turn(
            self.tmp_path, model, structured=self.name == "structured", **extra,
        )
        assert result.response_plan is not None
        return result.response_plan.text, conn

    def say(self, reply: Callable[[Request], Reply] | str) -> Callable[[Request], Reply]:
        """``reply`` as this route's model writes it."""

        def written(request: Request) -> Reply:
            one = reply if isinstance(reply, str) else reply(request)
            if isinstance(one, str):
                one = Reply(one, chunk=4)
            if self.name == "structured" and one.text is not None and one.text.startswith("<"):
                return Reply(_structured(one.text), calls=one.calls, chunk=4)
            return one

        return written


@pytest.fixture(params=["text", "spoken", "structured"])
def route(request: pytest.FixtureRequest, tmp_path: Path) -> Route:
    """The three routes that speak a model's answer on a turn with tools."""
    return Route(request.param, tmp_path)


def test_a_tool_result_carries_its_receipt_id_and_the_next_turns_history_shows_it(
    tmp_path: Path, route: Route,
) -> None:
    """The id is on every result she reads; the history line names the same one."""
    model = ScriptedModel(tool_call(*MEMO), route.say(_then("did {0}: meeting")))
    _, conn = route.run(model)
    try:
        (seen,) = _ids(model.requests[1])
        ((rid, receipt),) = turn_receipts(conn, ["T_guard" if route.name == "text" else "T_say"])[
            "T_guard" if route.name == "text" else "T_say"
        ]
        assert seen == rid == receipt_id(rid_action(conn))
        assert receipt.ok
    finally:
        conn.close()
    host = _host(tmp_path)
    logged_tool_call(host, "T_old", "keep", {"text": "上周的事"}, take=0)
    answer_with_record(host, "T_old", "好。")
    line = history_of(host)[-1]["content"]
    assert re.search(r"\[Receipts: ran keep\(text=上周的事\) \(r[0-9a-f]{6}, done\)\]$", line)


def _host(tmp_path: Path) -> StepsHost:
    """A log and memory.db in their own folder, so a turn's own log is not shared."""
    (tmp_path / "h").mkdir()
    return StepsHost(tmp_path / "h")


def rid_action(conn: sqlite3.Connection) -> str:
    """The action id of the turn's one call."""
    return str(conn.execute(
        "SELECT json_extract(payload_json, '$.action_id') FROM events "
        "WHERE type = 'action.proposed'",
    ).fetchone()[0])


def test_a_did_citing_a_done_change_passes_and_speech_starts_before_the_reply_ends(
    tmp_path: Path,
) -> None:
    """The block passes and the answer is spoken as written, its first sentence at once."""
    model = ScriptedModel(
        tool_call(*MEMO), _then("did {0}: meeting", say="记好了。明天开会已经在日程里了。"),
    )
    result, conn = spoken_turn(tmp_path, model)
    try:
        assert result.response_plan is not None
        assert result.response_plan.text == "记好了。明天开会已经在日程里了。"
        assert segments(conn) == result.response_plan.text
        (row,) = claims_rows(conn)
        assert row["sent_back"] is False
        assert [e["ok"] for e in row["entries"]] == [True]
        ids = {
            ev_type: rid for rid, ev_type in conn.execute("SELECT id, type FROM events")
        }
        first_sentence = conn.execute(
            "SELECT MIN(id) FROM events WHERE type LIKE '%chunk%'",
        ).fetchone()[0]
        assert first_sentence < ids["answer.claims_checked"]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("calls", "block", "reason"),
    [
        ([MEMO, ("kept", {})], "did {1}: it is saved", "is a read, not a change"),
        ([FAILING], "did {0}: it is saved", "is failed, not done"),
        ([], "did r00dead: it is saved", "no such receipt r00dead"),
    ],
)
def test_a_did_that_cites_a_read_a_failure_or_nothing_is_sent_back_once(
    route: Route, calls: list[tuple[str, dict[str, Any]]], block: str, reason: str,
) -> None:
    """The note names the entry and lists the receipts; the rewrite is checked again."""
    steps: list[Any] = [tool_call(*call) for call in calls]
    steps += [route.say(_then(block, say="存好了。")), route.say(_then(say="没存成。"))]
    model = ScriptedModel(*steps)
    text, conn = route.run(model)
    try:
        assert text == "没存成。"
        assert len(model.requests) == len(steps)
        note = model.requests[-1].messages[-2]["content"]
        assert note.startswith(claims_check.NOTE_HEAD)
        assert f"{reason}" in note
        assert "did " in note
        assert note.rstrip().endswith(claims_check.NOTE_TAIL)
        for rid in _ids(model.requests[-1]):
            assert rid in note
        first, second = claims_rows(conn)
        assert (first["sent_back"], second["sent_back"]) == (True, False)
        assert [e["ok"] for e in first["entries"]] == [False]
        assert second["entries"] == []
    finally:
        conn.close()


def test_a_second_failure_is_spoken_as_written_and_recorded_with_no_third_request(
    route: Route,
) -> None:
    """There is never a second note: the rewrite that fails again stands."""
    bad = route.say(_then("did r00dead: saved", say="存好了。"))
    model = ScriptedModel(bad, bad)
    text, conn = route.run(model)
    try:
        assert text == "存好了。"
        assert len(model.requests) == 2
        first, second = claims_rows(conn)
        assert (first["sent_back"], second["sent_back"]) == (True, False)
        assert second["spoken_as_written"] is True
        assert [e["why"] for e in second["entries"]] == ["no such receipt r00dead"]
    finally:
        conn.close()


def test_a_bounced_answer_is_never_spoken(tmp_path: Path) -> None:
    """On the spoken route the first answer fails its block, so none of it reaches the speaker."""
    model = ScriptedModel(
        _then("did r00dead: saved", say="存好了。"), _then(say="还没存。"),
    )
    result, conn = spoken_turn(tmp_path, model)
    try:
        assert result.response_plan is not None
        assert result.response_plan.text == "还没存。"
        assert segments(conn) == "还没存。"
    finally:
        conn.close()


def test_a_looked_citing_an_earlier_turns_read_from_the_history_passes(tmp_path: Path) -> None:
    """A receipt in a history line is one she was shown: the read stands behind ``looked``."""
    host = StepsHost(tmp_path)  # her turn runs on the same log the history was read from
    read = Receipt("done", reads_only=True, line="read kept()").to_json()
    logged_tool_call(host, "T_old", "kept", {}, take=0, receipt=read)
    answer_with_record(host, "T_old", "还没有备忘。")
    history = history_of(host)
    (old,) = re.findall(r"\((r[0-9a-f]{6}), done\)", history[-1]["content"])
    model = ScriptedModel(f"<claims>\nlooked {old}: the memo list\n</claims>还是没有备忘。")
    text, conn = guard_batch(tmp_path, model, history=history)
    try:
        assert text == "还是没有备忘。"
        assert len(model.requests) == 1
        (row,) = claims_rows(conn)
        assert [e["ok"] for e in row["entries"]] == [True]
    finally:
        conn.close()


def test_an_offer_of_a_lookup_on_the_menu_is_sent_back_and_an_offer_of_a_change_passes(
    route: Route,
) -> None:
    """She does the lookup instead of offering it; offering to change something stays an offer."""
    model = ScriptedModel(
        route.say(_then("offer kept: list your memos", say="要我查查备忘吗？")),
        route.say(_then("offer keep: save it for you", say="没有备忘，要我记一条吗？")),
    )
    text, conn = route.run(model)
    try:
        assert text == "没有备忘，要我记一条吗？"
        assert len(model.requests) == 2
        note = model.requests[1].messages[-2]["content"]
        assert "kept only reads and is on the menu" in note
        assert note.rstrip().endswith(
            f"{claims_check.NOTE_TAIL} {claims_check.NOTE_OFFER}",
        )
        first, second = claims_rows(conn)
        assert (first["sent_back"], second["sent_back"]) == (True, False)
        assert [e["ok"] for e in second["entries"]] == [True]
    finally:
        conn.close()


def test_a_reminder_set_and_a_light_that_failed_cannot_share_one_claim(route: Route) -> None:
    """The reminder-set-and-light-off claim with one done and one failed receipt is sent back."""
    model = ScriptedModel(
        tool_call(*MEMO),
        tool_call(*FAILING),
        route.say(_then("did {0}: reminder set\ndid {1}: light off", say="提醒设好了，灯也关了。")),
        route.say(_then("did {0}: reminder set", say="提醒设好了，灯没关成。")),
    )
    text, conn = route.run(model)
    try:
        assert text == "提醒设好了，灯没关成。"
        note = model.requests[-1].messages[-2]["content"]
        assert "did " in note
        assert "is failed, not done" in note
        assert "reminder set" not in note.split("This turn's receipts")[0]
        first, _second = claims_rows(conn)
        assert [e["ok"] for e in first["entries"]] == [True, False]
    finally:
        conn.close()


def test_text_before_a_tool_call_is_neither_spoken_nor_in_the_answer(tmp_path: Path) -> None:
    """With no passed block, what she writes ahead of a call is dropped, not joined."""
    model = ScriptedModel(
        Reply("我先查一下备忘。", calls=tool_call("kept").calls, chunk=4),
        _then(say="还没有备忘。"),
    )
    result, conn = spoken_turn(tmp_path, model)
    try:
        assert result.response_plan is not None
        assert result.response_plan.text == "还没有备忘。"
        assert segments(conn) == "还没有备忘。"
        assert "我先查一下备忘" not in json.dumps(model.requests[1].messages, ensure_ascii=False)
        assert [r["had_block"] for r in claims_rows(conn)] == [True]
    finally:
        conn.close()


def test_speech_stops_at_a_call_after_a_passed_block_and_what_was_said_stays(
    tmp_path: Path,
) -> None:
    """The block passes, her first sentence goes out, the call runs, the reply starts over."""
    model = ScriptedModel(
        Reply("<claims></claims>好的，我看看。", calls=tool_call("kept").calls, chunk=4),
        _then(say="没有备忘。"),
    )
    result, conn = spoken_turn(tmp_path, model)
    try:
        assert result.response_plan is not None
        assert "好的，我看看。" in segments(conn)
        assert result.response_plan.text.endswith("没有备忘。")
        first, second = claims_rows(conn)
        assert first["ended_in_call"] is True
        assert first["stopped_at_call"] is True
        assert second["ended_in_call"] is False
    finally:
        conn.close()


def test_a_reply_with_no_block_is_held_to_its_end_and_recorded_as_having_none(
    route: Route,
) -> None:
    """The text is spoken only at the end, checked as an empty block."""
    model = ScriptedModel(route.say("你好，今天天气不错。"))
    text, conn = route.run(model)
    try:
        assert text == "你好，今天天气不错。"
        (row,) = claims_rows(conn)
        assert (row["had_block"], row["entries"], row["sent_back"]) == (False, [], False)
        assert row["unread"] == 0
    finally:
        conn.close()


def test_a_block_with_words_but_no_entry_is_recorded_apart_from_an_empty_one(
    route: Route,
) -> None:
    """Neither is sent back; the row counts the lines that read as no entry."""
    model = ScriptedModel(route.say("<claims>none</claims>你好。"))
    text, conn = route.run(model)
    try:
        assert text == "你好。"
        (row,) = claims_rows(conn)
        assert (row["had_block"], row["entries"], row["unread"]) == (True, [], 1)
        assert row["sent_back"] is False
    finally:
        conn.close()


def test_the_block_never_reaches_speech_the_screen_or_the_record(route: Route) -> None:
    """Its words are in the check's own row and the request sent back, and nowhere she is heard."""
    marker = "marker-word"
    model = ScriptedModel(
        tool_call(*MEMO), route.say(_then("did {0}: " + marker, say="记好了，明天开会。")),
    )
    text, conn = route.run(model)
    try:
        assert text == "记好了，明天开会。"
        assert marker not in segments(conn)
        others = conn.execute(
            "SELECT payload_json FROM events WHERE type != 'answer.claims_checked'",
        ).fetchall()
        assert not [row for row in others if marker in row[0] or "<claims>" in row[0]]
    finally:
        conn.close()
