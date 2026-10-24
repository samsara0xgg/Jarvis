"""ADR 0390: topics are an index she keeps over the one conversation.

Acceptance checks over a real memory.db written by the store's own functions:

- the store: a topic lists its lines one place per day, ``by`` him when he said any of it inside
  the topic; a merged topic's id names the one it went into; only messages are filed; a row of
  the conversation is tagged only with a topic he said it in;
- his changes (rename, merge, done, reopen, drop) are versions, and a reopened merged topic gets
  its lines back; the night refuses a plan made from topics that changed meanwhile, and goes day
  by day, never past a day still waiting for its summary;
- the routes: ``GET /inherent/topics``, ``GET /inherent/conversation?around=``, and ``POST
  /inherent/submit`` with ``topic`` (a merged id resolves, an unknown one is a 422 that writes
  nothing); the phone's ``say`` keeps its topic on the row;
- a turn sent from a topic: her request names the topic and its recap, and his words and her
  answer are filed under it at once;
- her ``topics`` tool reads and changes them, with receipts and the call that puts a change back.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.execution.tools import Receipted, ToolError
from jarvis.runtime.daemon import submissions
from jarvis.runtime.turn import start as start_module
from jarvis.state import topics
from jarvis.state.device_tokens import device_name_for_token
from jarvis.state.event_log import open_event_log, open_runtime_event_log
from jarvis.state.memory_db import MemorySettings, conversation_around, open_memory_db
from jarvis.surface.cli import emit_surface_user_intent
from jarvis.surface.terminal_events import BrainEvents
from tests.support.clock import FakeClock
from tests.support.routine import drive_streaming_turn
from tests.support.spoken import SpokenPeer, spoken_runtime
from tests.support.timeline import peers_with
from tests.support.tool_context import tool_context
from tests.support.topics import CLASS_RECAP, ZONE, base, night, opened, pottery_world

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.execution.tools import Tool


def _world(tmp_path: Path) -> Path:
    db = tmp_path / "memory.db"
    pottery_world(db)
    return db


def _places(db: Path, topic_id: str) -> list[tuple[str, str, list[str]]]:
    """``(day, by, record ids)`` of each place the listed topic was talked about."""
    found = {one.topic.id: one for one in topics.listed(db, ZONE)}
    return [
        (place.day.isoformat(), place.by, [line.record_id for line in place.lines])
        for place in found[topic_id].places
    ]


# --- the store ---------------------------------------------------------------------------------


def test_a_topic_lists_its_places_by_day_and_a_merged_id_names_where_it_went(
    tmp_path: Path,
) -> None:
    """Open first, newest talk first; one place per day; ``him`` wins a day he talked inside it."""
    db = _world(tmp_path)

    listed = topics.listed(db, ZONE)
    assert [(one.topic.id, one.topic.state) for one in listed] == [("t1", "open"), ("t2", "done")]
    assert _places(db, "t1") == [
        ("2026-10-11", "night", ["a1", "a2"]),
        ("2026-10-13", "night", ["b1", "b2"]),
        ("2026-10-14", "him", ["c1", "c2", "c3"]),  # c3 came with the merged t3
    ]
    assert listed[0].topic.recap == CLASS_RECAP
    live = topics.live_topic(db, "t3")
    assert live is not None
    assert live.id == "t1"
    assert topics.live_topic(db, "t9") is None
    assert topics.live_topic(db, "陶艺课") is None, "a name is not an id"


def test_only_messages_are_filed_and_only_his_filing_tags_a_row(tmp_path: Path) -> None:
    """A picture note is not a line of talk; a line the night filed was not said in the topic."""
    db = _world(tmp_path)

    assert topics.file_his(db, "t1", ["n1", "a3"]) == "t1"
    assert "n1" not in [line.record_id for line in topics.topic_lines(db, "t1")]
    rows: list[dict[str, object]] = [{"id": rid} for rid in ("a1", "a3", "c1", "c3")]
    topics.tag_rows(db, rows)
    assert [row.get("topic") for row in rows] == [
        None, {"id": "t1", "name": "陶艺课"}, {"id": "t1", "name": "陶艺课"}, None,
    ]
    assert topics.file_his(db, "t9", ["a3"]) is None


def test_his_changes_are_versions_and_a_reopened_merge_gets_its_lines_back(tmp_path: Path) -> None:
    """rename, done, drop and reopen; a refused change writes nothing."""
    db = _world(tmp_path)

    renamed = topics.change(db, "t1", "rename", name="  陶艺 课  ")
    assert renamed is not None
    assert (renamed.before.name, renamed.topic.name) == ("陶艺课", "陶艺 课")
    assert topics.change(db, "t1", "rename", name="陶艺 课") is None, "already so"
    with pytest.raises(ValueError, match="1 to 24"):
        topics.change(db, "t1", "rename", name="长" * 25)
    with pytest.raises(ValueError, match="another topic"):
        topics.change(db, "t1", "merge", into="t3")  # t3 went into t1 itself
    with pytest.raises(ValueError, match="reopen it first"):
        topics.change(db, "t3", "done")

    assert topics.change(db, "t3", "reopen") is not None
    assert _places(db, "t3") == [("2026-10-14", "night", ["c3"])]
    assert "c3" not in [line.record_id for line in topics.topic_lines(db, "t1")]
    assert topics.change(db, "t3", "drop") is not None
    assert [one.topic.id for one in topics.listed(db, ZONE)] == ["t1", "t2"]
    assert topics.live_topic(db, "t3") is None, "a dropped topic takes no lines"
    with closing(open_memory_db(db)) as conn:
        origins = conn.execute(
            "SELECT origin, state FROM topic_versions WHERE topic_id = 't3' ORDER BY rowid",
        ).fetchall()
    assert origins == [
        ("night", "open"), ("him", "merged"), ("him", "open"), ("him", "dropped"),
    ]


def test_the_night_refuses_a_plan_from_topics_that_changed_and_goes_day_by_day(
    tmp_path: Path,
) -> None:
    """A rename during the call voids the plan; days wait for the first unsummarised one."""
    db = _world(tmp_path)
    stale = base(db)
    assert topics.change(db, "t1", "rename", name="陶艺") is not None
    plan = topics.NightPlan("2026-10-15", (opened("新的", "a3"),), ())
    assert topics.apply_night(db, plan, base=stale, model="test") is None
    assert topics.live_topic(db, "t4") is None, "nothing of the plan was written"

    with closing(open_memory_db(db)) as conn, conn:
        conn.executemany(
            "INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)",
            [
                ("d1", "2026-10-16T09:00:00-07:00", "allen", "早"),
                ("e1", "2026-10-17T09:00:00-07:00", "allen", "早"),
                ("f1", "2026-10-18T09:00:00-07:00", "allen", "早"),
            ],
        )
        conn.executemany(
            "INSERT INTO day_summaries (id, day, ts, model, summary, record_count, input_chars,"
            " output_chars) VALUES (?, ?, '2026-10-19T05:00:00-07:00', 'm', 's', 1, 1, 1)",
            [(f"s{day}", day) for day in ("2026-10-11", "2026-10-13", "2026-10-14",
                                           "2026-10-16", "2026-10-18")],
        )
    today = date.fromisoformat("2026-10-19")
    # the latest night is 10-14; 10-17 has records and no summary yet, so 10-18 waits for it
    assert topics.pending_days(db, today) == ["2026-10-16"]
    assert topics.pending_days(db, today, since="2026-10-17") == []


# --- the routes --------------------------------------------------------------------------------


def _route_peers(tmp_path: Path, db: Path, **deps: Any) -> Any:  # noqa: ANN401 - the app's peers
    def around(record_id: str, limit: int) -> dict[str, Any] | None:
        found = conversation_around(db, record_id, limit=limit)
        if found is None:
            return None
        rows, older, newer = found
        topics.tag_rows(db, rows)
        return {"since": "", "rows": rows, "has_more": older, "has_newer": newer}

    keys = tmp_path / "keys"
    keys.mkdir()
    return peers_with(
        keys,
        conversation_read=lambda *_args: {"since": "", "rows": [], "has_more": False},
        conversation_around=around,
        topics_read=lambda: topics.listed(db, ZONE),
        topic_resolve=lambda tid: submissions.resolve_topic(db, tid),
        device_name=lambda token: device_name_for_token(keys, token),
        **deps,
    )


def test_the_topics_route_lists_each_topic_with_one_position_per_day(tmp_path: Path) -> None:
    """The first line of each day, its seq and time, who filed it, and the lines to light up."""
    db = _world(tmp_path)
    peers = _route_peers(tmp_path, db)

    body = peers.paired.get("/inherent/topics").json()

    assert [(one["id"], one["name"], one["state"], one["count"]) for one in body["topics"]] == [
        ("t1", "陶艺课", "open", 3), ("t2", "搬家", "done", 1),
    ]
    first = body["topics"][0]
    assert first["recap"] == CLASS_RECAP
    assert [(p["record_id"], p["by"], p["lines"]) for p in first["positions"]] == [
        ("a1", "night", ["a1", "a2"]),
        ("b1", "night", ["b1", "b2"]),
        ("c1", "him", ["c1", "c2", "c3"]),
    ]
    assert [p["seq"] for p in first["positions"]] == [1, 5, 8]
    assert first["last_ms"] == first["positions"][-1]["ts_ms"] + 10 * 60 * 1000  # c3, 08:10
    assert peers.bare_remote.get("/inherent/topics").status_code == 401


def test_the_conversation_opens_around_a_message_and_tags_what_was_said_in_a_topic(
    tmp_path: Path,
) -> None:
    """The message in the middle, both directions flagged; a note's id or a cursor is refused."""
    db = _world(tmp_path)
    peers = _route_peers(tmp_path, db)

    page = peers.paired.get("/inherent/conversation", params={"around": "b1", "limit": 4}).json()
    assert [row["id"] for row in page["rows"]] == ["a3", "b1", "b2", "b3"], "n1 is no message"
    assert (page["has_more"], page["has_newer"]) == (True, True)
    whole = peers.paired.get("/inherent/conversation", params={"around": "c1"}).json()
    assert [row["id"] for row in whole["rows"]][-3:] == ["c1", "c2", "c3"]
    assert "n1" not in [row["id"] for row in whole["rows"]], "a picture note is not talk"
    tags = {row["id"]: row.get("topic") for row in whole["rows"]}
    assert tags["c1"] == {"id": "t1", "name": "陶艺课"}
    assert tags["a1"] is None
    assert not whole["has_newer"]

    assert peers.paired.get("/inherent/conversation", params={"around": "n1"}).status_code == 404
    refused = peers.paired.get("/inherent/conversation", params={"around": "b1", "after": 2})
    assert refused.status_code == 400


def test_submit_from_a_topic_resolves_a_merged_id_and_refuses_an_unknown_one(
    tmp_path: Path,
) -> None:
    """The turn goes with the topic it names now; a 422 starts nothing."""
    db = _world(tmp_path)
    sent: list[tuple[Any, ...]] = []

    def in_topic(*args: Any) -> str:  # noqa: ANN401
        sent.append(args)
        return "T-topic"

    peers = _route_peers(tmp_path, db, submit_in_topic=in_topic)

    ok = peers.paired.post("/inherent/submit", json={"text": "那周日几点", "topic": "t3"})
    assert ok.json() == {"status": "accepted", "turn_id": "T-topic"}
    assert sent == [("phone", "那周日几点", (), None, None, "t1")]
    local = peers.local.post("/inherent/submit", json={"text": "还有吗", "topic": "t1"})
    assert local.status_code == 200
    assert sent[-1][0] is None, "the local key is no device"

    for topic, why in (("t9", "no topic t9"), ("陶艺课", "string_pattern_mismatch")):
        refused = peers.paired.post("/inherent/submit", json={"text": "那周日几点", "topic": topic})
        assert refused.status_code == 422, topic
        assert why in refused.text
    assert len(sent) == 2

    keys = tmp_path / "bare"
    keys.mkdir()
    bare = peers_with(keys).paired.post("/inherent/submit", json={"text": "x", "topic": "t1"})
    assert (bare.status_code, bare.json()["detail"]) == (422, "this host keeps no topics")


def test_the_topic_rides_on_the_turns_opening_row_from_either_route(tmp_path: Path) -> None:
    """``submit_in_topic`` (HTTP) and a phone ``say`` both keep ``topic`` on the row."""
    log = tmp_path / "events.db"
    open_event_log(log).close()

    submissions.submit_in_topic(log, None, "那周日几点", (), None, None, "t1")
    submissions.submit_in_topic(log, "iphone", "带什么", (), None, "u-1", "t1")
    with closing(open_runtime_event_log(log)) as conn:
        BrainEvents(conn).record_phone_say("iphone", "u-2", "还有吗", spoken=True, topic="t1")
        rows = conn.execute(
            "SELECT type, ingestion_node, payload_json FROM events "
            "WHERE type IN ('surface.user_intent', 'utterance.received') ORDER BY id",
        ).fetchall()
    kept = [(kind, node, json.loads(payload).get("topic")) for kind, node, payload in rows]
    assert ("surface.user_intent", "mac", "t1") in kept
    assert sum(1 for _, node, topic in kept if node == "iphone" and topic == "t1") >= 2


# --- a turn in a topic -------------------------------------------------------------------------


@pytest.fixture
def _spoken_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spoken route on the test peer, as ``test_phone_about`` opens it."""
    monkeypatch.setenv("ROUTINE_STREAM_FIXTURE_KEY", "synthetic")
    monkeypatch.setattr(start_module, "labels_phases", lambda _base_url: True)


def _sent(peer: SpokenPeer, index: int = 0) -> str:
    return "\n␟\n".join(str(item["content"]) for item in peer.requests[index][1]["input"])


@pytest.mark.usefixtures("_spoken_route")
def test_a_turn_in_a_topic_names_it_with_its_recap_and_files_both_sides_at_once(
    tmp_path: Path,
) -> None:
    """His words and her answer are under the topic before the night; without one, no line."""
    db = tmp_path / "memory.db"
    with SpokenPeer([[("final_answer", "周日上午十点。")], [("final_answer", "好。")]]) as peer:
        runtime = replace(
            spoken_runtime(tmp_path, peer.url),
            memory=MemorySettings(db_path=db, audio_dir=tmp_path / "audio"),
        )
        pottery_world(db)
        drive_streaming_turn(runtime, emit_surface_user_intent(
            runtime.conn, transcript="那周日几点", turn_id="T-in", channel="inherent_ptt",
            ingestion_node="iphone", topic="t3",
        ))
        drive_streaming_turn(runtime, emit_surface_user_intent(
            runtime.conn, transcript="今天天气", turn_id="T-out", channel="inherent_ptt",
        ))

    first = _sent(peer)
    assert 'He is talking in one of the topics you keep: "陶艺课" (topic t1).' in first
    assert CLASS_RECAP in first
    assert 'topics(action="read", topic="t1")' in first
    assert "He is talking in one of the topics" not in peer.requests[1][1]["input"][-1]["content"]
    with closing(sqlite3.connect(db)) as conn:
        newest = [str(rid) for (rid,) in conn.execute(
            "SELECT id FROM records WHERE source IN ('allen', 'jarvis') ORDER BY rowid",
        )]
    ours = newest[-4:-2]  # his words and her answer of the first turn
    filed = {line.record_id: line.by for line in topics.topic_lines(db, "t1")}
    assert [filed.get(rid) for rid in ours] == ["him", "him"]
    assert not set(newest[-2:]) & set(filed), "the second turn was in no topic"


# --- her tool ----------------------------------------------------------------------------------


def _tool(db: Path) -> Tool:
    from jarvis.execution.topics_tool import build_topics_tool  # noqa: PLC0415 - the unit here

    (tool,) = build_topics_tool(db)
    return tool


def _call(tool: Tool, **args: Any) -> Receipted:  # noqa: ANN401
    return cast("Receipted", tool.handler(args, tool_context(clock=FakeClock())))


def test_the_tool_lists_and_reads_with_receipts_in_his_local_days(tmp_path: Path) -> None:
    """List names each topic and where it stands; read gives its recap and days, newest first."""
    tool = _tool(_world(tmp_path))

    listed = _call(tool)
    assert listed["summary"] == "2 topics, 1 open."
    assert [(one["id"], one["last_talked"], one["days"]) for one in listed["topics"]] == [
        ("t1", "today (Wed)", 3), ("t2", "yesterday (Tue)", 1),
    ]
    assert listed.receipt.reads_only
    read = _call(tool, action="read", topic="陶艺")  # part of a name will do
    assert read["recap"] == CLASS_RECAP
    assert [(one["day"], one["said_in_it"], one["open"]) for one in read["talked_on"]] == [
        ("today (Wed)", True, "c1"),
        ("yesterday (Tue)", False, "b1"),
        ("Sun Oct 11", False, "a1"),
    ]
    assert _call(tool, action="read", topic="t3")["id"] == "t1", "a merged id reads where it went"
    assert tool.reads is not None
    assert tool.reads({"action": "read"})
    assert not tool.reads({"action": "rename"})


def test_the_tool_changes_a_topic_at_his_word_and_says_how_to_put_it_back(
    tmp_path: Path,
) -> None:
    """rename, done, merge and reopen, each with its undo; a refusal says what to do instead."""
    db = _world(tmp_path)
    tool = _tool(db)

    renamed = _call(tool, action="rename", topic="t1", name="周末陶艺")
    assert renamed["summary"] == "Renamed '陶艺课' to '周末陶艺'."
    assert renamed["undo"] == {"action": "rename", "topic": "t1", "name": "陶艺课"}
    assert renamed.receipt.object == "topic:t1"
    done = _call(tool, action="done", topic="周末陶艺")
    assert done["undo"] == {"action": "reopen", "topic": "t1"}
    assert _call(tool, action="done", topic="t1")["summary"].endswith("nothing changed.")
    reopened = _call(tool, action="reopen", topic="t3")
    assert reopened["undo"] == {"action": "merge", "topic": "t3", "into": "t1"}
    assert topics.live_topic(db, "t3") is not None
    merged = _call(tool, action="merge", topic="拉坯工具", into="t1")
    assert merged["summary"] == "Merged 拉坯工具 into 周末陶艺."

    for args, why in (
        ({"action": "rename", "topic": "t2", "name": "周末陶艺"}, "merge t2 into it"),
        ({"action": "rename", "topic": "t2"}, "1 to 24"),
        ({"action": "read"}, "by its id"),
        ({"action": "read", "topic": "旅行"}, "no topic"),
        ({"action": "list", "topic": "t1"}, "list takes page"),
        ({"action": "close", "topic": "t1"}, "action is one of"),
    ):
        with pytest.raises(ToolError, match=why):
            _call(tool, **args)


def test_an_ambiguous_name_asks_him_which(tmp_path: Path) -> None:
    """Two topics share a word: the error names both with their ids, for him to choose."""
    db = _world(tmp_path)
    night(db, "2026-10-15", opened("陶艺工具", "a3"), opened("陶艺作品", "c3"))
    with pytest.raises(ToolError, match="Ask him which one") as refused:
        _call(_tool(db), action="read", topic="陶艺")
    assert refused.value.kind == "needs_him"
    assert all(f"({one})" in str(refused.value) for one in ("t1", "t4", "t5"))
