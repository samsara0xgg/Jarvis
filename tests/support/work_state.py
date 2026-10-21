"""The work-state service behind the real registry and log, with one canned analyst."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

from jarvis.decision.llm import ChatResult, ToolCall
from jarvis.decision.work_state import REPORT_TOOL_NAME
from jarvis.execution.tools import build_default_registry
from jarvis.runtime.work_state import WorkStateService
from jarvis.state.event_log import iter_events_of_types
from jarvis.state.memory_db import open_memory_db
from jarvis.state.work_state import gather_evidence
from tests.support.tool_world import ToolWorld, tool_request

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

REPORT: dict[str, Any] = {
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
        self.report = report or REPORT
        self.materials: list[str] = []
        self.tiers: list[str | None] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    @property
    def last_material(self) -> str:
        return self.materials[-1] if self.materials else ""

    def analyze(
        self,
        conn: sqlite3.Connection,
        *,
        system: str,
        messages: list[dict[str, Any]],
        service_tier: str | None = None,
    ) -> ChatResult:
        """Mimic a forced tool call."""
        del conn, system
        self.calls += 1
        self.tiers.append(service_tier)
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


class WorkStateRig:
    """The real registry + log + HTTP app around one service and one canned analyst."""

    def __init__(self, root: Path, *, timesink: Path | None) -> None:
        """Wire the stores exactly as the composition root does, minus the model."""
        self.memory = root / "memory.db"
        self.timesink = timesink
        with open_memory_db(self.memory):
            pass
        self.analyst = CannedAnalyst()
        self.fx = ToolWorld(root, tools=())
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
        result = self.fx.dispatch(tool_request(name, f"ws{self.sequence}", arguments=args))
        assert result.tool_output is not None
        return dict(json.loads(result.tool_output))

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
