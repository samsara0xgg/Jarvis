"""ADR 0023 acceptance: one refresh workflow, one record, no model call without new input.

Data-driven: a real TimeSink-shaped store, the real event log, the real
dispatcher and HTTP app; only the model is a canned analyst so the checks
assert on what is persisted and served, not on prose.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jarvis.decision.llm import ChatResult, ToolCall
from jarvis.decision.work_state import REPORT_TOOL_NAME, build_request, parse_report
from jarvis.execution.tools import build_default_registry
from jarvis.runtime.inherent_loop import _poll_timesink_once
from jarvis.runtime.work_state import WorkStateService
from jarvis.state.daily_contract import DailyError
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.state.memory_db import append_record, open_memory_db
from jarvis.state.work_state import (
    compose_state,
    current_state,
    gather_evidence,
    question_terms,
    save_state,
)
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from jarvis.surface.timesink_observer import TimesinkObserver, latest_observation
from tests.integration.test_flat_tool_dispatch import _Fixture, _request
from tests.integration.test_timesink_activity import add_capture, add_span, source

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

__all__ = ["source"]

_REPORT: dict[str, Any] = {
    "now": {"text": "在 Chrome 里看 cc rules 文档", "basis": "observed", "refs": ["s1", "zz9"]},
    "activities": [
        {"text": "整理 Jarvis 的规则文件", "basis": "observed", "progress": None, "refs": ["a1"]},
        {"text": "说过要把待办留在本地", "basis": "stated", "progress": "已决定", "refs": ["r1"]},
        {"text": "可能在准备发布", "basis": "inferred", "progress": None, "refs": []},
    ],
    "links": [
        {"kind": "todo", "key": "t1", "note": "今天的改动都围绕它", "basis": "inferred"},
        {"kind": "todo", "key": "t99", "note": "不存在的键", "basis": "inferred"},
    ],
    "uncertainties": ["没有 Git 数据"],
}


class CannedAnalyst:
    """Return one fixed report and count calls; ``fail`` raises like a provider outage."""

    def __init__(self, report: dict[str, Any] | None = None) -> None:
        self.calls = 0
        self.fail = False
        self.report = report or _REPORT
        self.materials: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    @property
    def last_material(self) -> str:
        return self.materials[-1] if self.materials else ""

    def analyze(
        self, conn: sqlite3.Connection, *, system: str, messages: list[dict[str, Any]]
    ) -> ChatResult:
        """Mimic a forced tool call."""
        del conn, system
        self.calls += 1
        self.materials.append(str(messages[0]["content"]))
        self.started.set()
        self.release.wait(timeout=5)
        if self.fail:
            msg = "provider down"
            raise ConnectionError(msg)
        return ChatResult(
            text=None,
            tool_calls=(ToolCall("c1", REPORT_TOOL_NAME, json.dumps(self.report)),),
            finish_reason="tool_calls",
            input_tokens=10,
            output_tokens=10,
            raw={},
        )


class Rig:
    """The real registry + log + HTTP app around one service and one canned analyst."""

    def __init__(self, root: Path, *, timesink: Path | None) -> None:
        """Wire the stores exactly as the composition root does, minus the model."""
        self.memory = root / "memory.db"
        self.timesink = timesink
        with open_memory_db(self.memory):
            pass
        self.analyst = CannedAnalyst()
        self.fx = _Fixture(root, tools=())
        self.service = WorkStateService(
            event_log_path=self.fx.paths.event_log,
            memory_path=self.memory,
            timesink_path=timesink,
            repos=(),
            analyst=self.analyst,
            model="canned",
        )
        self.tools = build_default_registry(
            memory_db_path=self.memory,
            timesink_db_path=timesink,
            work_state_refresh=lambda args, ctx: self.service.refresh(
                ctx.conn,
                question=args.get("question"),
                note=args.get("note"),
                force=bool(args.get("force", False)),
                trigger="conversation",
                action_id=ctx.action_id,
            ),
        ).get_definitions()
        for tool in self.tools:
            self.fx.registry.register(tool)
        self.sequence = 0

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch through the real dispatcher and decode the persisted output."""
        self.sequence += 1
        result = self.fx.dispatch(_request(name, f"ws{self.sequence}", arguments=args))
        assert result.slots[0].tool_output is not None
        return dict(json.loads(result.slots[0].tool_output))

    def events(self, kind: str) -> list[dict[str, Any]]:
        """Every persisted event of one type, oldest first."""
        return [dict(e.payload) for e in iter_events_of_types(self.fx.conn, (kind,))]

    def evidence(self, **kwargs: Any) -> Any:  # noqa: ANN401 — Evidence, forwarded kwargs.
        """Gather exactly what a refresh would gather."""
        return gather_evidence(
            self.fx.conn,
            memory_path=self.memory,
            timesink_path=self.timesink,
            repos=(),
            **kwargs,
        )


@pytest.fixture
def rig(tmp_path: Path, source: sqlite3.Connection) -> Iterator[Rig]:
    """A store with today's data: one span, one capture, one record, one todo."""
    add_span(source, _stamp(-90), _stamp(-60), title="cc | rules")
    add_capture(source, _stamp(-30), _stamp(-5), text="rules for jarvis\nkeep todos local")
    built = Rig(tmp_path, timesink=tmp_path / "timesink.sqlite")
    append_record(built.memory, record_id="rec-1", source="allen", text="待办就留在本地")
    built.call("create_todo", {"title": "把 TimeSink 修复合进 main", "request_id": "todo-1"})
    yield built
    built.fx.close()


def _stamp(minutes_from_now: int) -> str:
    at = datetime.now(UTC) + timedelta(minutes=minutes_from_now)
    return at.isoformat(sep=" ", timespec="milliseconds").removesuffix("+00:00")


def _old_record(rig: Rig, identity: str, days_ago: int, text: str) -> None:
    at = (datetime.now(UTC) - timedelta(days=days_ago)).astimezone().isoformat(timespec="seconds")
    with open_memory_db(rig.memory) as memory:
        memory.execute(
            "INSERT INTO records(id, ts, source, text) VALUES(?,?,?,?)",
            (identity, at, "allen", text),
        )
        memory.commit()


def test_refresh_persists_a_sourced_versioned_record(rig: Rig) -> None:
    """The record carries basis per claim, only real refs, and survives a fresh connection."""
    view = rig.call("refresh_work_state", {"question": "我现在在做什么"})
    assert view["outcome"] == "analyzed"
    state = view["state"]
    assert state["version"] == 1
    assert state["now"]["basis"] == "observed"
    assert state["now"]["as_of"] == state["observed_until"]
    assert state["now"]["refs"]
    assert all(r.startswith("timesink-capture:") for r in state["now"]["refs"])
    assert [a["basis"] for a in state["activities"]] == ["observed", "stated", "inferred"]
    assert state["activities"][0]["refs"][0].startswith("timesink:")
    assert state["activities"][1]["refs"] == ["record:rec-1"]
    assert [link["title"] for link in state["links"]] == ["把 TimeSink 修复合进 main"]
    assert state["question"] == "我现在在做什么"
    assert state["observed_until"] is not None
    assert state["evidence"]["coverage"] == {
        "app": "partial",
        "screen": "partial",
        "state": "partial",
        "records": "available",
        "todos": "available",
        "knowledge": "available",
        "git": "unavailable",
    }
    assert any("Git" in limit for limit in state["evidence"]["limits"])
    assert any(u.startswith("材料范围：") for u in state["uncertainties"])
    assert "keep todos local" in rig.analyst.last_material
    assert "[t1]" in rig.analyst.last_material
    assert "## 材料范围说明" in rig.analyst.last_material
    # Persisted, not cached: a brand-new connection sees the same record.
    fresh = sqlite3.connect(rig.fx.paths.event_log)
    reread = current_state(fresh)
    fresh.close()
    assert reread is not None
    assert reread["version"] == 1
    saved = rig.events("work_state.revised")
    assert saved[-1]["trigger"] == "conversation"
    assert saved[-1]["action_id"] == "ws2"
    assert view["freshness"]["analyzed_at"] == state["analyzed_at"]
    assert view["freshness"]["checked_at_ms"] is not None
    assert view["freshness"]["latest_observed_at"] is not None


def test_reuse_needs_same_input_and_same_evidence(rig: Rig, source: sqlite3.Connection) -> None:
    """Repeated identical asks are free; a new question, note, row or force is a new analysis."""
    rig.call("refresh_work_state", {"question": "今天做了什么"})
    assert rig.analyst.calls == 1
    again = rig.call("refresh_work_state", {"question": "今天做了什么"})
    assert again["outcome"] == "reused"
    assert again["state"]["version"] == 1
    assert rig.analyst.calls == 1
    # An extending row changes the cited revision and observed interval.
    source.execute("UPDATE capture SET lastSeenAt=? WHERE id=1", (_stamp(-1),))
    source.commit()
    assert rig.call("refresh_work_state", {"question": "今天做了什么"})["outcome"] == "analyzed"
    # The same screen with a different question is not the same input.
    other = rig.call("refresh_work_state", {"question": "之前说的 TimeSink 事情进展如何"})
    assert other["outcome"] == "analyzed"
    assert other["state"]["version"] == 3
    assert other["state"]["evidence"]["terms"] == ["TimeSink"]
    noted = rig.call("refresh_work_state", {"note": "我在准备发布"})
    assert noted["outcome"] == "analyzed"
    assert noted["state"]["note"] == "我在准备发布"
    assert "[u1] 我在准备发布" in rig.analyst.last_material
    add_capture(source, _stamp(-3), _stamp(-1), text="new window content")
    assert rig.call("refresh_work_state", {"note": "我在准备发布"})["outcome"] == "analyzed"
    assert rig.call("refresh_work_state", {"note": "我在准备发布"})["outcome"] == "reused"
    forced = rig.call("refresh_work_state", {"note": "我在准备发布", "force": True})
    assert forced["outcome"] == "analyzed"
    assert rig.analyst.calls == 6


def test_question_directs_retrieval_beyond_the_default_window(
    rig: Rig, source: sqlite3.Connection
) -> None:
    """Older records and captures come in only when the question names them; keys stay valid."""
    _old_record(rig, "rec-old", 10, "Typlus 的 notarize 下周再弄")
    _old_record(rig, "rec-noise", 10, "晚饭吃什么")
    add_capture(source, _stamp(-5 * 24 * 60), _stamp(-5 * 24 * 60 + 3), text="Typlus release notes")
    plain = rig.evidence()
    assert plain.sections["related_records"] == []
    assert plain.sections["related_screen"] == []
    directed = rig.evidence(question="之前说的 Typlus 进展如何")
    assert directed.terms == ["Typlus"]
    related = directed.sections["related_records"]
    assert [r["text"] for r in related] == ["Typlus 的 notarize 下周再弄"]
    assert directed.refs[directed.sections["related_records"][0]["key"]] == "record:rec-old"
    assert [r["text"] for r in directed.sections["related_screen"]] == ["Typlus release notes"]
    assert directed.fingerprint != plain.fingerprint
    assert question_terms("我们之前讨论的工作状态记录怎么样了") == [
        "讨论",
        "工作状态记录",
        "工作",
        "作状",
        "状态",
        "态记",
        "记录",
    ]


def test_material_limits_are_explicit(rig: Rig, source: sqlite3.Connection) -> None:
    """Truncation is stated in the material and the record instead of reading as inactivity."""
    for index in range(35):
        add_capture(source, _stamp(-100 + index * 2), _stamp(-99 + index * 2), text=f"page {index}")
    evidence = rig.evidence()
    assert evidence.counts["recent"] == 30
    assert any("36 条屏幕内容" in limit for limit in evidence.limits)
    view = rig.call("refresh_work_state", {})
    assert any("36 条屏幕内容" in u for u in view["state"]["uncertainties"])
    assert "只保留最新的 30 条" in rig.analyst.last_material


def test_basis_rules_are_enforced_programmatically(rig: Rig, source: sqlite3.Connection) -> None:
    """Stated needs a record/note, observed needs TimeSink data, inferred forces an uncertainty."""
    rig.analyst.report = {
        "now": {"text": "在写代码", "basis": "observed", "refs": ["s1"]},
        "activities": [
            {"text": "他说要发布", "basis": "stated", "progress": None, "refs": ["s1"]},
            {"text": "看了文档", "basis": "observed", "progress": None, "refs": ["r1"]},
            {"text": "确定的事", "basis": "stated", "progress": None, "refs": ["r1"]},
        ],
        "links": [],
        "uncertainties": [],
    }
    view = rig.call("refresh_work_state", {})
    state = view["state"]
    assert [a["basis"] for a in state["activities"]] == ["inferred", "inferred", "stated"]
    assert any("2 条结论缺少明确依据" in u for u in state["uncertainties"])
    # A note is a valid stated source; the now claim disappears without recent observations.
    rig.analyst.report["activities"][0]["refs"] = ["u1"]
    source.execute("DELETE FROM capture")
    source.commit()
    noted = rig.call("refresh_work_state", {"note": "我要发布"})
    assert noted["state"]["activities"][0]["basis"] == "stated"
    assert noted["state"]["activities"][0]["refs"] == ["note"]
    assert noted["state"]["now"] is None


def test_failure_keeps_previous_record(rig: Rig, source: sqlite3.Connection) -> None:
    """A provider error, an unusable reply or a stale writer never replaces the saved state."""
    rig.call("refresh_work_state", {})
    add_capture(source, _stamp(-3), _stamp(-1), text="new window content")
    rig.analyst.fail = True
    failed = rig.call("refresh_work_state", {})
    assert failed["outcome"] == "failed"
    assert "provider down" in failed["error"]
    assert failed["state"]["version"] == 1
    rig.analyst.fail = False
    rig.analyst.report = {"activities": "not a list"}
    garbage = rig.call("refresh_work_state", {})
    assert garbage["outcome"] == "failed"
    assert garbage["state"]["version"] == 1
    assert garbage["state"]["now"] is not None
    with pytest.raises(DailyError, match="Version conflict"):
        save_state(
            rig.fx.conn, {"now": None}, expected_version=0, trigger="dashboard", action_id=None
        )
    latest = current_state(rig.fx.conn)
    assert latest is not None
    assert latest["version"] == 1


def test_no_data_is_unknown_not_invented(tmp_path: Path) -> None:
    """Without TimeSink, records or notes the model is never asked and nothing is written."""
    built = Rig(tmp_path, timesink=tmp_path / "missing.sqlite")
    try:
        view = built.call("refresh_work_state", {"question": "我在做什么"})
        assert view["outcome"] == "no_evidence"
        assert view["state"] is None
        assert built.analyst.calls == 0
        evidence = gather_evidence(
            built.fx.conn, memory_path=built.memory, timesink_path=None, repos=()
        )
        assert evidence.empty
        assert evidence.coverage["app"] == "unavailable"
        assert evidence.coverage["screen"] == "unavailable"
        assert any("TimeSink 不可读" in limit for limit in evidence.limits)
    finally:
        built.fx.close()


def test_windows_follow_the_configured_zone(rig: Rig) -> None:
    """Today starts at local midnight in the configured zone, not the process zone."""
    zone = ZoneInfo("Asia/Shanghai")
    evidence = rig.evidence(tz=zone, now=datetime(2026, 9, 21, 3, 30, tzinfo=UTC))
    assert evidence.window["from"] == "2026-09-21T00:00:00+08:00"
    assert evidence.window["recent_from"] == "2026-09-21T09:30:00+08:00"


def _concurrent(rig: Rig, first: dict[str, Any], second: dict[str, Any]) -> list[dict[str, Any]]:
    class WaitingCondition(threading.Condition):
        def wait(self, timeout: float | None = None) -> bool:
            waiting.set()
            return super().wait(timeout)

    waiting = threading.Event()
    rig.service._cond = WaitingCondition()  # noqa: SLF001 — deterministic contention barrier.
    rig.analyst.release.clear()
    rig.analyst.started.clear()
    results: list[dict[str, Any]] = []

    def run(args: dict[str, Any]) -> Callable[[], None]:
        def target() -> None:
            results.append(rig.service.refresh_in_own_connection(**args))

        return target

    one = threading.Thread(target=run(first))
    one.start()
    assert rig.analyst.started.wait(timeout=5)
    two = threading.Thread(target=run(second))
    two.start()
    assert waiting.wait(timeout=5)
    rig.analyst.release.set()
    one.join(timeout=10)
    two.join(timeout=10)
    return results


def test_same_input_joins_the_running_analysis(rig: Rig) -> None:
    """Two identical triggers at once (dashboard + dashboard) run one analysis, see one version."""
    results = _concurrent(rig, {"trigger": "dashboard"}, {"trigger": "dashboard"})
    assert rig.analyst.calls == 1
    assert [r["state"]["version"] for r in results] == [1, 1]
    assert any(r.get("joined") for r in results)
    assert len(rig.events("work_state.revised")) == 1


def test_different_question_during_analysis_is_answered_separately(rig: Rig) -> None:
    """A new question or fact arriving mid-analysis waits, then gets its own analysis."""
    results = _concurrent(
        rig,
        {"trigger": "dashboard"},
        {"trigger": "conversation", "question": "之前说的 TimeSink 进展如何"},
    )
    assert rig.analyst.calls == 2
    assert sorted(r["state"]["version"] for r in results) == [1, 2]
    assert not any(r.get("joined") for r in results)
    assert "Allen 现在问的是：之前说的 TimeSink 进展如何" in rig.analyst.materials[1]
    assert current_state(rig.fx.conn)["question"] == "之前说的 TimeSink 进展如何"  # type: ignore[index]


def test_http_routes_share_the_service(rig: Rig) -> None:
    """GET never analyses; POST answers the same shape with an outcome."""

    async def refresh() -> dict[str, Any]:
        return await asyncio.to_thread(rig.service.refresh_in_own_connection, trigger="dashboard")

    def read() -> dict[str, Any]:
        # TestClient serves from another thread; production reads on the loop thread's conn.
        conn = open_event_log(rig.fx.paths.event_log)
        try:
            return rig.service.read(conn)
        finally:
            conn.close()

    deps = InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        work_state_read=read,
        work_state_refresh=refresh,
    )
    with TestClient(create_app(deps)) as client:
        empty = client.get("/inherent/work-state").json()
        assert empty["state"] is None
        assert empty["data"] is None
        assert empty["freshness"] == {
            "checked_at_ms": None,
            "latest_observed_at": None,
            "analyzed_at": None,
            "analysis_observed_until": None,
        }
        assert empty["outcome"] is None
        assert empty["refreshing"] is False
        assert rig.analyst.calls == 0
        refreshed = client.post("/inherent/work-state/refresh").json()
        assert refreshed["outcome"] == "analyzed"
        assert refreshed["state"]["version"] == 1
        assert client.get("/inherent/work-state").json()["state"]["version"] == 1
        assert rig.analyst.calls == 1
    assert rig.events("work_state.revised")[-1]["trigger"] == "dashboard"


def test_head_poll_emits_only_on_change(tmp_path: Path, source: sqlite3.Connection) -> None:
    """The 5-minute sync records collector state once per change, including extending rows."""
    fx = _Fixture(tmp_path, tools=())
    try:
        observer = TimesinkObserver(fx.conn, tmp_path / "timesink.sqlite")
        assert observer.recover_baseline() is None
        assert observer.emit(observer.collect()) is not None
        assert observer.emit(observer.collect()) is None
        add_capture(source, _stamp(-3), _stamp(-1), text="x")
        assert observer.emit(observer.collect()) is not None
        # An extending row moves the head without a new id.
        source.execute("UPDATE capture SET lastSeenAt=? WHERE id=1", (_stamp(0),))
        source.commit()
        assert observer.emit(observer.collect()) is not None
        add_span(source, _stamp(-10), _stamp(-5))
        assert observer.emit(observer.collect()) is not None
        source.execute("UPDATE span SET end=? WHERE id=1", (_stamp(-2),))
        source.commit()
        assert observer.emit(observer.collect()) is not None
        # A restart recovers the baseline from the log: no duplicate row for the same head.
        restarted = TimesinkObserver(fx.conn, tmp_path / "timesink.sqlite")
        restarted.recover_baseline()
        assert restarted.emit(restarted.collect()) is None
        head = latest_observation(fx.conn)
        assert head is not None
        assert head["status"] == "ok"
        assert head["capture_high"] == 1
        service = WorkStateService(
            event_log_path=fx.paths.event_log,
            memory_path=None,
            timesink_path=None,
            repos=(),
            analyst=None,
            model="none",
        )
        fresh = service.read(fx.conn)["freshness"]
        assert fresh["checked_at_ms"] == head["observed_at_ms"]
        newest = max(head["capture_latest_seen"], head["span_latest_end"])
        assert fresh["latest_observed_at"] == newest
        gone = TimesinkObserver(fx.conn, tmp_path / "missing.sqlite")
        gone.recover_baseline()
        assert gone.emit(gone.collect()) is not None
        assert gone.emit(gone.collect()) is None
        last = latest_observation(fx.conn)
        assert last is not None
        assert last["status"] == "unavailable"
        rows = list(iter_events_of_types(fx.conn, ("timesink.state_observed",)))
        assert len(rows) == 6
    finally:
        fx.close()


def test_request_and_parse_round_trip(rig: Rig) -> None:
    """The material is keyed and bounded; the reply parser validates shapes before saving."""
    evidence = rig.evidence(question="进展如何")
    system, messages = build_request(evidence, question="进展如何", previous=None)
    assert "report_work_state" in system
    assert "Allen 现在问的是：进展如何" in messages[0]["content"]
    assert len(messages[0]["content"]) < 20000
    text_only = ChatResult(
        text='```json\n{"now": null, "activities": [], "links": [], "uncertainties": ["无"]}\n```',
        tool_calls=(),
        finish_reason="stop",
        input_tokens=1,
        output_tokens=1,
        raw={},
    )
    assert parse_report(text_only)["uncertainties"] == ["无"]


def test_lock_and_updated_rows_invalidate_reuse(rig: Rig, source: sqlite3.Connection) -> None:
    """A refresh after lock or a row revision must not claim its old evidence is current."""
    first = rig.call("refresh_work_state", {})
    old_ref = first["state"]["now"]["refs"][0]
    source.execute("UPDATE capture SET lastSeenAt=? WHERE id=1", (_stamp(-2),))
    source.execute("INSERT INTO stateEvent(at,kind) VALUES(?,?)", (_stamp(-1), "lock"))
    source.commit()
    updated = rig.call("refresh_work_state", {})
    assert updated["outcome"] == "analyzed"
    assert rig.analyst.calls == 2
    assert "lock" in rig.analyst.last_material
    assert updated["state"]["now"]["refs"] != [old_ref]
    new_ref = updated["state"]["now"]["refs"][0]
    assert "code" not in rig.call("read_activity", {"activity_id": new_ref})
    # State changes matter even without changing any content row.
    source.execute("INSERT INTO stateEvent(at,kind) VALUES(?,?)", (_stamp(0), "unlock"))
    source.commit()
    assert rig.call("refresh_work_state", {})["outcome"] == "analyzed"


def test_assistant_records_are_not_allen_statements(rig: Rig) -> None:
    """A valid record reference alone does not establish that Allen said something."""
    append_record(rig.memory, record_id="advice", source="jarvis", text="建议下周发布")
    evidence = rig.evidence()
    key = next(row["key"] for row in evidence.sections["records"] if row["who"] == "jarvis")
    rig.analyst.report = {
        "now": None,
        "activities": [
            {"text": "Allen 决定下周发布", "basis": "stated", "refs": [key], "progress": None}
        ],
        "links": [{"kind": "discussion", "key": key, "note": "发布承诺", "basis": "stated"}],
        "uncertainties": [],
    }
    state = rig.call("refresh_work_state", {})["state"]
    assert state["activities"][0]["basis"] == "inferred"
    assert state["links"][0]["basis"] == "inferred"


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"now": None, "activities": "bad", "links": [], "uncertainties": []},
        {
            "now": None,
            "activities": [{"text": "x", "basis": "observed", "refs": []}],
            "links": [],
            "uncertainties": [],
        },
        {"now": None, "activities": [], "links": [], "uncertainties": [123]},
    ],
)
def test_malformed_report_preserves_the_last_state(rig: Rig, bad: dict[str, Any]) -> None:
    """Malformed reports are failures, not successful empty replacement documents."""
    saved = rig.call("refresh_work_state", {})["state"]
    rig.analyst.report = bad
    failed = rig.call("refresh_work_state", {"force": True})
    assert failed["outcome"] == "failed"
    assert failed["state"] == saved
    assert len(rig.events("work_state.revised")) == 1


def test_finished_request_returns_its_own_result(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """B can finish before A sends its response, but A still receives A's saved analysis."""
    original = rig.service._view  # noqa: SLF001 — pause only response delivery, not analysis.
    a_ready, b_done = threading.Event(), threading.Event()
    results: dict[str, Any] = {}

    def delayed(
        conn: sqlite3.Connection, result: dict[str, Any], *, joined: bool = False
    ) -> dict[str, Any]:
        if threading.current_thread().name == "request-A":
            a_ready.set()
            assert b_done.wait(5)
        return original(conn, result, joined=joined)

    monkeypatch.setattr(rig.service, "_view", delayed)

    def request_a() -> None:
        results["a"] = rig.service.refresh_in_own_connection(question="问题 A")

    def request_b() -> None:
        try:
            results["b"] = rig.service.refresh_in_own_connection(question="问题 B")
        finally:
            b_done.set()

    a = threading.Thread(target=request_a, name="request-A")
    a.start()
    assert a_ready.wait(5)
    b = threading.Thread(target=request_b)
    b.start()
    a.join(10)
    b.join(10)
    assert not a.is_alive()
    assert not b.is_alive()
    assert results["a"]["state"]["question"] == "问题 A"
    assert results["b"]["state"]["question"] == "问题 B"
    assert results["a"]["state"]["version"] == 1
    assert results["b"]["state"]["version"] == 2


def test_unchanged_poll_updates_check_clock_without_model(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polling unchanged data advances checked_at without events or analysis calls."""
    observer = TimesinkObserver(rig.fx.conn, rig.timesink)
    monkeypatch.setattr("jarvis.runtime.work_state.time.time", lambda: 1000.0)
    asyncio.run(_poll_timesink_once(observer, rig.service.note_checked))
    first = rig.service.read(rig.fx.conn)
    monkeypatch.setattr("jarvis.runtime.work_state.time.time", lambda: 1300.0)
    asyncio.run(_poll_timesink_once(observer, rig.service.note_checked))
    second = rig.service.read(rig.fx.conn)
    assert second["freshness"]["checked_at_ms"] - first["freshness"]["checked_at_ms"] == 300000
    assert second["freshness"]["latest_observed_at"] == first["freshness"]["latest_observed_at"]
    assert len(rig.events("timesink.state_observed")) == 1
    assert rig.analyst.calls == 0


def test_now_uses_the_cited_capture_time(rig: Rig, source: sqlite3.Connection) -> None:
    """A newer unrelated app span cannot make an older screen claim appear current."""
    add_span(source, _stamp(-2), _stamp(-1), title="different app activity")
    state = rig.call("refresh_work_state", {})["state"]
    assert datetime.fromisoformat(state["now"]["as_of"]) < datetime.fromisoformat(
        state["observed_until"]
    )
    evidence = rig.evidence()
    report = {**_REPORT, "now": {"text": "未知来源", "basis": "observed", "refs": ["a1"]}}
    assert compose_state(report, evidence, question=None, model="test")["now"] is None
