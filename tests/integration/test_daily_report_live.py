"""Opt-in acceptance: real TimeSink/Git/conversation data + DeepSeek, isolated Jarvis state.

--live-llm reads a read-only snapshot of this machine's TimeSink database, a
copy of memory.db and the real repo-observer events, lets the configured preset
choose the tool from the real registry, and saves the report into a temporary
event log. No production write, screenshot or daemon restart occurs.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from jarvis.decision.llm import LLMClient
from jarvis.deployment.launchd import default_runtime_root
from jarvis.execution.tools import build_default_registry
from jarvis.runtime.daily_report import DailyReportService
from jarvis.runtime.work_state import build_analyst
from jarvis.state.daily_report import resolve_day, resolve_zone
from jarvis.state.event_log import emit_event
from tests.integration.test_flat_tool_dispatch import _Fixture, _request

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.live_llm

_ACTIVITY_TYPES = ("repo.state_observed", "project.commit_seen")
_IMPORT_DAYS = 3
_SYSTEM = (
    "你是 Jarvis。用工具回答 Allen 的请求。只在工具返回成功后才说事情已完成，"
    "不要编造内容。最后用中文简短回复。"
)
_ALLOWED = {"daily_work_report", "get_briefing", "query_activity", "search_records"}


def _copy(source: Path, target: Path) -> None:
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(target)) as dst,
    ):
        src.backup(dst)


def _import_git_events(live_log: Path, conn: sqlite3.Connection, since_ms: int) -> int:
    """Copy the real observer's Git evidence into the isolated log, timestamps intact."""
    if not live_log.exists():
        return 0
    with closing(sqlite3.connect(live_log.as_uri() + "?mode=ro", uri=True)) as src:
        rows = src.execute(
            "SELECT type,ts_epoch_ms,payload_json FROM events WHERE type IN (?,?) "
            "AND ts_epoch_ms>=? ORDER BY id",
            (*_ACTIVITY_TYPES, since_ms),
        ).fetchall()
    for kind, ts_ms, payload in rows:
        emit_event(conn, type=str(kind), ts_epoch_ms=int(ts_ms), payload=json.loads(payload))
    return len(rows)


@dataclass
class LiveRig:
    """The real registry, dispatcher and service over frozen copies of the real stores."""

    fx: Any
    service: DailyReportService
    tools: tuple[Any, ...]
    config: dict[str, Any]
    zone_name: str
    today: str
    yesterday: str
    git_events: int
    out: Path
    sequence: int = 0

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch through the real dispatcher and decode the persisted output."""
        self.sequence += 1
        result = self.fx.dispatch(_request(name, f"live-dr{self.sequence}", arguments=arguments))
        assert result.slots[0].tool_output is not None
        assert len(result.slots[0].tool_output) <= 16384
        return dict(json.loads(result.slots[0].tool_output))

    def read_all(self, day: str) -> str:
        """The whole saved report, paged back through get_briefing."""
        page = self.call("get_briefing", {"local_date": day, "timezone": self.zone_name})
        content = str(page["content"])
        while page["next_cursor"] is not None:
            page = self.call(
                "get_briefing",
                {
                    "local_date": day,
                    "timezone": self.zone_name,
                    "cursor": page["next_cursor"],
                },
            )
            content += str(page["content"])
        assert page["complete"] is True
        return content

    def save(self, name: str, text: str) -> None:
        """Keep the generated evidence next to the test's temporary stores."""
        (self.out / name).write_text(text, encoding="utf-8")


@pytest.fixture
def live(tmp_path: Path) -> Iterator[LiveRig]:
    """Freeze this machine's real evidence into an isolated runtime."""
    config = yaml.safe_load(Path("config/jarvis.yaml").read_text(encoding="utf-8"))
    timesink_source = Path(config["observer"]["timesink"]["db_path"]).expanduser()
    if not timesink_source.exists():
        pytest.skip("Real TimeSink database is not available on this host")
    frozen = tmp_path / "timesink.sqlite"
    _copy(timesink_source, frozen)
    memory = tmp_path / "memory.db"
    live_memory = default_runtime_root() / "memory.db"
    if live_memory.exists():
        _copy(live_memory, memory)
    else:  # pragma: no cover — a host with no conversation history yet.
        with closing(sqlite3.connect(memory)) as conn:
            conn.execute(
                "CREATE TABLE records (id TEXT PRIMARY KEY, ts TEXT NOT NULL, "
                "source TEXT NOT NULL, text TEXT NOT NULL, audio_path TEXT)"
            )
    repos = tuple(
        str(Path(item).expanduser())
        for item in config["observer"]["repos"]
        if isinstance(item, str)
    )
    reporter = build_analyst(
        config,
        config.get("daily_report", {}).get("preset", config["work_state"]["preset"]),
        pricing_path=None,
        account_cost=False,
        kind="daily_report",
        timeout_s=240.0,
    )
    assert reporter is not None
    zone_name, zone = resolve_zone(config["work_state"].get("timezone"), None)
    now = datetime.now(UTC)
    fx = _Fixture(tmp_path, tools=())
    imported = _import_git_events(
        default_runtime_root() / "mac_events.db",
        fx.conn,
        int((now - timedelta(days=_IMPORT_DAYS)).timestamp() * 1000),
    )
    sessions = Path.home() / ".codex" / "sessions"
    service = DailyReportService(
        memory_path=memory,
        timesink_path=frozen,
        repos=repos,
        reporter=reporter,
        model=config["work_state"]["preset"],
        tz=zone,
        codex_sessions_path=sessions if sessions.is_dir() else None,
    )
    tools = build_default_registry(
        memory_db_path=memory,
        timesink_db_path=frozen,
        observed_repos=repos,
        daily_report_run=lambda args, ctx: service.run(
            ctx.conn,
            local_date=args.get("local_date"),
            timezone=args.get("timezone"),
            regenerate=bool(args.get("regenerate", False)),
            action_id=ctx.action_id,
        ),
    ).get_definitions()
    for tool in tools:
        fx.registry.register(tool)
    rig = LiveRig(
        fx=fx,
        service=service,
        tools=tuple(tools),
        config=config,
        zone_name=zone_name,
        today=now.astimezone(zone).date().isoformat(),
        yesterday=resolve_day(None, zone, now).isoformat(),
        git_events=imported,
        out=tmp_path,
    )
    try:
        yield rig
    finally:
        fx.close()


def test_live_daily_report_generates_reuses_and_regenerates(live: LiveRig) -> None:
    """A real model writes yesterday's report from real data, then reuses and redoes it."""
    client = LLMClient(live.config["llm"])
    catalog = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in live.tools
        if t.name in _ALLOWED
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": "帮我写一份昨天的工作报告。"}]
    invoked: list[str] = []
    outcome: dict[str, Any] = {}
    for _ in range(6):
        answer = client.chat(system=_SYSTEM, messages=messages, tools=catalog)
        calls = answer.tool_calls
        if not calls:
            assert answer.text
            break
        messages.append(
            {
                "role": "assistant",
                "content": answer.text or "",
                "tool_calls": [
                    {
                        "id": c.call_id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.arguments_json},
                    }
                    for c in calls
                ],
            }
        )
        for one in calls:
            assert one.name in _ALLOWED
            invoked.append(one.name)
            result = live.call(one.name, json.loads(one.arguments_json))
            assert "code" not in result, result
            if one.name == "daily_work_report":
                assert result["outcome"] == "generated", result.get("error")
                outcome = result
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": one.call_id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
    else:  # pragma: no cover — the model kept calling tools.
        pytest.fail("Conversation did not finish within six model rounds")

    assert "daily_work_report" in invoked, "the model must reach the skill, not answer from memory"
    assert outcome["local_date"] == live.yesterday
    assert outcome["timezone"] == live.zone_name
    assert outcome["version"] == 1

    content = live.read_all(live.yesterday)
    assert content.startswith(f"# 工作日报 {live.yesterday}（{live.zone_name}）")
    for heading in ("## 核心摘要", "## 工作事项", "## 数据覆盖与不确定性", "## 证据引用"):
        assert heading in content

    # Whatever the day held must be cited from the store it came from.
    counts = outcome["evidence_counts"]
    refs = live.call(
        "get_briefing", {"local_date": live.yesterday, "timezone": live.zone_name}
    )["source_refs"]
    kinds = {ref.split(":")[0] for ref in refs}
    if counts["screen"]:
        assert outcome["coverage"]["screen"] == "available"
        assert "timesink-capture" in kinds, "a day with OCR text must cite a capture"
    if counts["git"]:
        assert kinds & {"event", "git"}, "a day with commits must cite a commit"
    if counts["records"]:
        assert outcome["coverage"]["records"] == "available"
    assert "这一天尚未结束" not in content, "a finished day is not reported as partial"

    again = live.call(
        "daily_work_report", {"local_date": live.yesterday, "timezone": live.zone_name}
    )
    assert again["outcome"] == "reused"
    assert again["version"] == 1
    redone = live.call(
        "daily_work_report",
        {"local_date": live.yesterday, "timezone": live.zone_name, "regenerate": True},
    )
    assert redone["outcome"] == "generated"
    assert redone["version"] == 2

    live.save("report-yesterday.md", content)
    live.save(
        "acceptance-yesterday.json",
        json.dumps(
            {
                "local_date": live.yesterday,
                "timezone": live.zone_name,
                "conversation_tools": invoked,
                "outcomes": [outcome["outcome"], again["outcome"], redone["outcome"]],
                "versions": [outcome["version"], again["version"], redone["version"]],
                "coverage": outcome["coverage"],
                "evidence_counts": outcome["evidence_counts"],
                "limits": outcome["limits"],
                "source_refs_saved": outcome["source_refs_saved"],
                "total_chars": outcome["total_chars"],
                "model_calls": outcome["model_calls"],
                "git_events_imported": live.git_events,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


def test_live_report_for_a_day_still_running(live: LiveRig) -> None:
    """The current local day is cut at now: a report says so, and an empty morning saves nothing."""
    result = live.call(
        "daily_work_report", {"local_date": live.today, "timezone": live.zone_name}
    )
    assert result["outcome"] in {"generated", "no_evidence"}, result.get("error")
    evidence: dict[str, Any] = {
        "local_date": live.today,
        "timezone": live.zone_name,
        "outcome": result["outcome"],
        "coverage": result["coverage"],
        "limits": result.get("limits"),
        "git_events_imported": live.git_events,
    }
    if result["outcome"] == "no_evidence":
        assert result["version"] == 0
        live.save("acceptance-today.json", json.dumps(evidence, ensure_ascii=False, indent=2))
        return

    content = live.read_all(live.today)
    assert "这一天尚未结束" in content
    counts = result["evidence_counts"]
    refs = live.call("get_briefing", {"local_date": live.today, "timezone": live.zone_name})[
        "source_refs"
    ]
    kinds = {ref.split(":")[0] for ref in refs}
    if counts["screen"]:
        assert "timesink-capture" in kinds, "a day with OCR text must cite a capture"
    live.save("report-today.md", content)
    evidence.update(
        evidence_counts=counts,
        source_ref_kinds=sorted(kinds),
        source_refs_saved=result["source_refs_saved"],
        total_chars=result["total_chars"],
        model_calls=result["model_calls"],
    )
    live.save("acceptance-today.json", json.dumps(evidence, ensure_ascii=False, indent=2))
