"""Daily-loop acceptance against the real registry, SQLite log and dispatcher."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.execution.daily_tools import build_daily_tools
from jarvis.execution.tools import (
    ReadOnlyToolRegistry,
    Tool,
    ToolContext,
    ToolError,
    build_default_registry,
)
from jarvis.state.event_log import emit_event, open_runtime_event_log
from jarvis.state.memory_db import append_record, open_memory_db
from tests.integration.test_wave4b_action_runner import _Fixture, _request

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class DailyHarness:
    """Use a real default registry for both runner and inline tool calls."""

    def __init__(
        self,
        root: Path,
        *,
        runner: bool = False,
        timesink_path: Path | None = None,
    ) -> None:
        """Create an isolated default-registry fixture."""
        self.memory = root / "memory.db"
        with closing(open_memory_db(self.memory)):
            pass
        self.tools = build_default_registry(
            memory_db_path=self.memory, timesink_db_path=timesink_path
        ).get_definitions()
        self.fx = _Fixture(root, tools=self.tools, with_runner=runner)
        self.sequence = 0

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch and decode the actual persisted tool output."""
        self.sequence += 1
        result = self.fx.dispatch(_request(name, f"daily{self.sequence}", arguments=args))
        assert result.slots[0].tool_output is not None
        output = json.loads(result.slots[0].tool_output)
        assert output.get("truncated") is not True
        assert len(result.slots[0].tool_output) <= 16384
        return dict(output)

    def record(self, identity: str, text: str = "Use local todos") -> None:
        """Append real conversation material without a model."""
        append_record(self.memory, record_id=identity, source="allen", text=text)


@pytest.fixture
def daily(tmp_path: Path) -> Iterator[DailyHarness]:
    """One isolated persisted runtime per check."""
    harness = DailyHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.fx.close()


@pytest.mark.parametrize("runner", [False, True])
def test_todos_dispatch_retries_revisions_and_restart(tmp_path: Path, *, runner: bool) -> None:
    """Completion and reopening survive restart; retries never create duplicate events."""
    h = DailyHarness(tmp_path, runner=runner)
    try:
        args = {"title": "Connect activity query", "request_id": "create-one"}
        first = h.call("create_todo", args)
        assert first == h.call("create_todo", args)
        assert h.call("create_todo", {**args, "title": "different"})["code"] == "request_conflict"
        identity = first["todo_id"]
        change = {
            "todo_id": identity,
            "expected_version": 1,
            "patch": {"status": "done"},
            "request_id": "finish",
        }
        assert h.call("update_todo", change)["version"] == 2
        assert h.call("update_todo", change)["version"] == 2
        assert h.call("list_todos", {})["items"] == []
        assert (
            h.call("update_todo", {**change, "request_id": "stale"})["code"] == "version_conflict"
        )
        assert (
            h.call(
                "update_todo",
                {
                    **change,
                    "expected_version": 2,
                    "patch": {"status": "open", "due_at": "2026-09-22T09:00:00-07:00"},
                    "request_id": "reopen",
                },
            )["version"]
            == 3
        )
        with closing(open_runtime_event_log(h.fx.paths.event_log)) as fresh:
            count = fresh.execute(
                "SELECT count(*) FROM events WHERE type='todo.revised'"
            ).fetchone()[0]
            assert count == 3
            tool = next(t for t in h.tools if t.name == "list_todos")
            assert isinstance(tool, Tool)

            assert (
                tool.handler({}, ToolContext(fresh, h.fx.paths, "read"))["items"][0]["version"] == 3
            )
    finally:
        h.fx.close()


def test_record_pages_are_complete_and_snapshot_stable(daily: DailyHarness) -> None:
    """Read more than the former 20-record limit, with inserts between pages."""
    for i in range(35):
        daily.record(f"r{i}")
    first = daily.call("search_records", {"limit": 7})
    assert len(first["records"]) == 7
    daily.record("late", "arrived while paging")
    ids = [r["id"] for r in first["records"]]
    page = first
    while page["next_cursor"]:
        page = daily.call("search_records", {"limit": 7, "cursor": page["next_cursor"]})
        ids.extend(r["id"] for r in page["records"])
    assert len(ids) == len(set(ids)) == 35
    assert "late" not in ids
    assert daily.call("search_records", {})["records"][0]["id"] == "late"
    assert (
        daily.call("search_records", {"keyword": "changed", "cursor": first["next_cursor"]})["code"]
        == "invalid_cursor"
    )


def test_long_original_is_losslessly_retrievable(daily: DailyHarness) -> None:
    """Escaped JSON/control characters do not trigger the dispatcher's lossy cap."""
    original = 'hello\n\x00"\\世界' * 2500
    daily.record("long", original)
    daily.record("other", "end")
    args = {"record_ids": ["missing", "long", "other"]}
    recovered = {"long": "", "other": ""}
    while True:
        page = daily.call("read_records", args)
        assert page["missing_ids"] == ["missing"]
        for record in page["records"]:
            assert len(recovered[record["id"]]) == record["offset"]
            recovered[record["id"]] += record["text"]
        if page["next_cursor"] is None:
            break
        args["cursor"] = page["next_cursor"]
    assert recovered == {"long": original, "other": "end"}


def test_knowledge_sources_corrections_and_deprecation(daily: DailyHarness) -> None:
    """Missing provenance cannot be saved; corrections preserve original events."""
    args = {
        "statement": "Keep todos locally",
        "kind": "decision",
        "basis": "user_statement",
        "source_refs": ["record:source"],
        "request_id": "knowledge-one",
    }
    assert daily.call("save_knowledge", args)["code"] == "invalid_source"
    daily.record("source")
    first = daily.call("save_knowledge", args)
    assert first == daily.call("save_knowledge", args)
    revised = daily.call(
        "save_knowledge",
        {
            **args,
            "knowledge_id": first["knowledge_id"],
            "expected_version": 1,
            "statement": "Use Microsoft later",
            "request_id": "knowledge-two",
        },
    )
    assert revised["version"] == 2
    items = daily.call("search_knowledge", {})["items"]
    assert items[0]["supersedes"] == first["source_ref"]
    assert items[0]["statement"] == "Use Microsoft later"
    daily.call(
        "save_knowledge",
        {
            **args,
            "knowledge_id": first["knowledge_id"],
            "expected_version": 2,
            "status": "deprecated",
            "request_id": "knowledge-three",
        },
    )
    assert daily.call("search_knowledge", {})["items"] == []
    assert len(daily.call("search_knowledge", {"status": "all"})["items"]) == 1


def test_briefing_versions_chunks_and_no_delivery(daily: DailyHarness) -> None:
    """A saved briefing is not delivered; continuation reads its original version."""
    content = 'Today: "work"\n' * 900
    args = {
        "local_date": "2026-09-20",
        "timezone": "America/Vancouver",
        "content": content,
        "source_refs": [],
        "coverage": {"git": "unknown"},
        "request_id": "brief-one",
    }
    first = daily.call("save_briefing", args)
    assert first == daily.call("save_briefing", args)
    query = {"local_date": args["local_date"], "timezone": args["timezone"]}
    page = daily.call("get_briefing", query)
    assert page["coverage"]["screen"] == "unknown"
    assert page["delivery_status"] == "not_tracked"
    assert (
        daily.call("save_briefing", {**args, "request_id": "stale"})["code"] == "version_conflict"
    )
    assert (
        daily.call(
            "save_briefing",
            {**args, "content": "new", "expected_version": 1, "request_id": "brief-two"},
        )["version"]
        == 2
    )
    recovered = page["content"]
    while page["next_cursor"]:
        page = daily.call("get_briefing", {**query, "cursor": page["next_cursor"]})
        assert page["version"] == 1
        recovered += page["content"]
    assert recovered == content
    assert daily.call("get_briefing", query)["content"] == "new"
    assert (
        daily.fx.conn.execute("SELECT count(*) FROM events WHERE type LIKE 'surface.%'").fetchone()[
            0
        ]
        == 0
    )


def test_activity_coverage_time_basis_and_details(daily: DailyHarness) -> None:
    """Historical Git records never imply coverage of screen or current repository state."""
    ev = emit_event(
        daily.fx.conn,
        type="project.commit_seen",
        ts_epoch_ms=10000,
        payload={
            "repo_path": "/repos/jarvis",
            "commit_sha": "abc",
            "subject": "Add tools",
            "committed_at_ms": 1000,
            "actor": "observer",
            "truncated": True,
            "skipped_count": 3,
        },
    )
    args = {"from": "1970-01-01T00:00:09Z", "to": "1970-01-01T00:00:11Z"}
    result = daily.call("query_activity", args)
    assert result["time_basis"] == "observed_at"
    assert result["coverage"]["git"]["status"] == "unknown"
    assert result["coverage"]["git"]["configured_now"] is False
    assert result["coverage"]["git"]["skipped_count"] == 3
    assert result["coverage"]["screen"]["status"] == "not_implemented"
    activity = result["items"][0]
    assert activity["occurred_at"] == "1970-01-01T00:00:01.000+00:00"
    assert activity["source_refs"] == [f"event:{ev.event_uid}"]
    detail = daily.call("read_activity", {"activity_id": activity["id"]})
    assert json.loads(detail["content"])["commit_sha"] == "abc"
    assert daily.call("query_activity", {**args, "sources": ["screen"]})["items"] == []
    assert daily.call("query_activity", {**args, "project": "/different"})["items"] == []


def test_list_snapshot_preserves_pre_update_state(daily: DailyHarness) -> None:
    """Filter after folding revisions, while retaining the first page's watermark."""
    for i in range(3):
        daily.call("create_todo", {"title": str(i), "request_id": f"t{i}"})
    first = daily.call("list_todos", {"limit": 1})
    targets = daily.call("list_todos", {})["items"]
    for item in targets:
        daily.call(
            "update_todo",
            {
                "todo_id": item["id"],
                "expected_version": 1,
                "patch": {"status": "done"},
                "request_id": item["id"],
            },
        )
    assert daily.call("list_todos", {})["items"] == []
    second = daily.call("list_todos", {"limit": 1, "cursor": first["next_cursor"]})
    assert second["items"][0]["status"] == "open"


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("create_todo", {"title": "x", "request_id": "a", "extra": True}),
        ("create_todo", {"title": "x", "request_id": "a", "due_at": "2026-09-20"}),
        ("list_todos", {"limit": True}),
        ("search_records", {"from": "2026-09-21T00:00:00Z", "to": "2026-09-20T00:00:00Z"}),
        ("query_activity", {"from": "yesterday", "to": "today"}),
        ("read_records", {"record_ids": ["a", "a"]}),
        (
            "save_briefing",
            {
                "local_date": "2026-02-30",
                "timezone": "Mars",
                "content": "x",
                "source_refs": [],
                "coverage": {},
                "request_id": "a",
            },
        ),
        ("update_todo", {"todo_id": "x", "expected_version": 1, "patch": {}, "request_id": "a"}),
    ],
)
def test_invalid_inputs_are_terminal_tool_errors(
    daily: DailyHarness, name: str, args: dict[str, Any]
) -> None:
    """Bad input never strands an action or writes a revision."""
    assert daily.call(name, args)["code"] == "invalid_argument"
    assert (
        daily.fx.conn.execute("SELECT count(*) FROM events WHERE type LIKE '%.revised'").fetchone()[
            0
        ]
        == 0
    )


def test_concurrent_revision_only_one_wins(daily: DailyHarness) -> None:
    """Two SQLite writers cannot both update the same version."""
    created = daily.call("create_todo", {"title": "x", "request_id": "create"})
    tool = next(t for t in daily.tools if t.name == "update_todo")
    assert isinstance(tool, Tool)

    def update(request_id: str) -> str:
        with closing(open_runtime_event_log(daily.fx.paths.event_log)) as conn:
            try:
                tool.handler(
                    {
                        "todo_id": created["todo_id"],
                        "expected_version": 1,
                        "patch": {"status": "done"},
                        "request_id": request_id,
                    },
                    ToolContext(conn, daily.fx.paths, request_id),
                )
            except ToolError as exc:
                return exc.code
            else:
                return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ["one", "two"])) == ["ok", "version_conflict"]


def test_live_registry_still_refuses_writes(daily: DailyHarness) -> None:
    """Adding local state must not silently widen GPT Live's read-only permission."""
    view = ReadOnlyToolRegistry(daily.fx.registry)
    names = {t.name for t in view.get_definitions()}
    assert {"query_activity", "search_knowledge", "list_todos", "get_briefing"} <= names
    assert not names & {"create_todo", "update_todo", "save_knowledge", "save_briefing"}


def test_failed_reference_write_rolls_back_and_can_retry(daily: DailyHarness) -> None:
    """A failed write leaves no receipt that could hide a later successful retry."""
    args = {"title": "x", "request_id": "rollback", "source_refs": ["record:later"]}
    assert daily.call("create_todo", args)["code"] == "invalid_source"
    assert not daily.fx.conn.in_transaction
    daily.record("later")
    assert daily.call("create_todo", args)["version"] == 1
    assert len(daily.call("list_todos", {})["items"]) == 1


def test_half_open_record_window_and_due_date_clearing(daily: DailyHarness) -> None:
    """Offset-aware filters include the start but exclude the end, even across DST."""
    with closing(open_memory_db(daily.memory)) as conn, conn:
        conn.executemany(
            "INSERT INTO records(id,ts,source,text) VALUES(?,?,?,?)",
            [
                ("start", "2026-11-01T01:30:00-07:00", "allen", "start"),
                ("end", "2026-11-01T01:30:00-08:00", "allen", "end"),
            ],
        )
    page = daily.call(
        "search_records", {"from": "2026-11-01T08:30:00Z", "to": "2026-11-01T09:30:00Z"}
    )
    assert [row["id"] for row in page["records"]] == ["start"]
    todo = daily.call(
        "create_todo", {"title": "x", "due_at": "2026-11-01T01:30:00-07:00", "request_id": "due"}
    )
    assert not daily.call("list_todos", {"due_before": "2026-11-01T08:30:00Z"})["items"]
    daily.call(
        "update_todo",
        {
            "todo_id": todo["todo_id"],
            "expected_version": 1,
            "patch": {"due_at": None},
            "request_id": "clear",
        },
    )
    assert daily.call("list_todos", {})["items"][0]["due_at"] is None


def test_read_records_missing_set_is_stable(daily: DailyHarness) -> None:
    """A record added during chunking is available next query, not silently spliced in."""
    daily.record("long", "a" * 5000)
    args = {"record_ids": ["long", "late"]}
    first = daily.call("read_records", args)
    daily.record("late")
    second = daily.call("read_records", {**args, "cursor": first["next_cursor"]})
    assert second["missing_ids"] == ["late"]
    assert second["next_cursor"] is None
    assert daily.call("read_records", {"record_ids": ["late"]})["missing_ids"] == []


def test_oversize_knowledge_never_becomes_unreadable(daily: DailyHarness) -> None:
    """Reject an oversized revision rather than committing unpageable or clipped knowledge."""
    daily.record("source")
    result = daily.call(
        "save_knowledge",
        {
            "statement": "\x00" * 4000,
            "kind": "fact",
            "basis": "inference",
            "source_refs": ["record:source"],
            "request_id": "oversize",
        },
    )
    assert result["code"] == "result_too_large"
    assert daily.call("search_knowledge", {})["items"] == []


def test_request_id_namespace_crosses_local_domains(daily: DailyHarness) -> None:
    """A request ID cannot accidentally become both a todo and a briefing."""
    daily.call("create_todo", {"title": "x", "request_id": "shared"})
    result = daily.call(
        "save_briefing",
        {
            "local_date": "2026-09-20",
            "timezone": "UTC",
            "content": "x",
            "source_refs": [],
            "coverage": {},
            "request_id": "shared",
        },
    )
    assert result["code"] == "request_conflict"


def test_no_memory_file_is_created_by_history_lookup(tmp_path: Path) -> None:
    """Unavailable history is reported, not fabricated as an empty initialized store."""
    missing = tmp_path / "missing.db"
    tools = build_daily_tools(missing)
    fx = _Fixture(tmp_path, tools=tools, with_runner=False)
    try:
        result = fx.dispatch(_request("search_records", "missing", arguments={}))
        assert result.slots[0].error == "source_unavailable"
        assert not missing.exists()
    finally:
        fx.close()
