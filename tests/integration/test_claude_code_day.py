"""ADR 0299: the day report of his Claude Code work, from material to the day line.

Invented sessions and reports only. The material command runs over synthetic session logs of two
accounts with the brain's sleep faked (no network, no token): the day is cut by his sleep, and
sidechains, tool calls and results, thinking, reminders, command lines and the report task's own
sessions never reach it. The Mac shipper sends a changed report once, the brain's file intake
takes Markdown for its one source only, and the intake's reader turns a shipped report into
entries, a day-line item and an archive answer, a rewrite standing for the day.
"""

# ruff: noqa: E501, PT018 - invented dialogue lines; asserts compare tuples
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
import yaml

from jarvis.execution.archive_tool import build_archive_tool
from jarvis.runtime.claude_daily import ClaudeDailyIntake
from jarvis.state import imports
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import open_memory_db
from jarvis.state.shipments import CODE_DAY_SOURCE, MAX_REPORT_BYTES, Shipments, ShipRefused
from jarvis.surface import claude_code_day
from jarvis.surface.mac_ship_core import Report, State
from jarvis.surface.mac_ship_days import QUIET_MS, ship

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.execution.tools import Tool, ToolContext

VANCOUVER = ZoneInfo("America/Vancouver")
MARKER = claude_code_day.MARKER


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _user(at: datetime, content: object, **extra: object) -> dict[str, Any]:
    return {"type": "user", "timestamp": _iso(at), "cwd": "/work/alpha", "message": {"role": "user", "content": content}, **extra}


def _claude(at: datetime, *blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "timestamp": _iso(at), "message": {"role": "assistant", "model": "claude-x", "content": list(blocks)}}


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _write(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e, separators=(",", ":")) for e in entries) + "\n", encoding="utf-8")


# --- the material --------------------------------------------------------------------------


def test_the_material_is_cut_by_his_sleep_and_holds_only_what_was_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Window by sleep start; sidechain, tool, thinking, reminder, command and own-run text out."""
    today = datetime.now(VANCOUVER).date()
    day = today - timedelta(days=3)

    def at(offset: int, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day + timedelta(days=offset), datetime.min.time(), VANCOUVER).replace(hour=hour, minute=minute)

    # he fell asleep at 02:00 after day D; a short nap earlier (day D+1 at 13:00) is not the cut
    sleeps = {_ms(at(1, 2)): _ms(at(1, 9)), _ms(at(1, 13)): _ms(at(1, 13, 40))}
    monkeypatch.setattr(claude_code_day, "fetch_sleeps", lambda *_a: (sleeps, "America/Vancouver"))

    projects = tmp_path / "a" / "projects"
    other = tmp_path / "b" / "projects"
    _write(projects / "-work-alpha" / "sess-1.jsonl", [
        {"type": "ai-title", "aiTitle": "Alpha parser", "sessionId": "sess-1"},
        _user(at(0, 9, 5), "fix the parser"),
        _claude(at(0, 9, 6), {"type": "thinking", "thinking": "SECRET-THOUGHT"}, _text("I will look at it."), {"type": "tool_use", "name": "Bash", "input": {"command": "TOOL-INPUT"}}),
        _user(at(0, 9, 7), [{"type": "tool_result", "tool_use_id": "t", "content": "TOOL-OUTPUT"}]),
        _user(at(0, 9, 8), "<system-reminder>REMINDER-ONLY</system-reminder>"),
        _user(at(0, 9, 9), "<command-name>/clear</command-name>"),
        _user(at(0, 9, 10), "<system-reminder>REMINDER-INSIDE</system-reminder>keep going please"),
        _user(at(0, 9, 11), "meta line", isMeta=True),
        _claude(at(0, 9, 12), _text("Done: parser fixed.")),
        {**_claude(at(0, 9, 13), _text("SIDECHAIN-TEXT")), "isSidechain": True},
        {"type": "attachment", "timestamp": _iso(at(0, 9, 14)), "attachment": {"type": "queued_command", "origin": {"kind": "human"}, "prompt": "and one more thing"}},
        {"type": "attachment", "timestamp": _iso(at(0, 9, 15)), "attachment": {"type": "queued_command", "origin": {"kind": "task-notification"}, "prompt": "NOTIFICATION"}},
        _user(at(0, 23, 30), "late evening question"),
        _claude(at(1, 1, 30), _text("answered after midnight, before sleep")),
        _user(at(1, 3, 0), "after the sleep began"),
    ])
    _write(other / "-work-beta" / "sess-2.jsonl", [
        _user(at(0, 14), "beta question", cwd="/work/beta"),
        _claude(at(0, 14, 1), _text("beta answer")),
    ])
    _write(other / "-work-beta" / "own-run.jsonl", [
        _user(at(0, 22), f"run the report {MARKER} now"),
        _claude(at(0, 22, 1), _text("OWN-RUN-REPLY")),
    ])
    _write(projects / "-work-alpha" / "quiet.jsonl", [_user(at(-5, 9), "a week ago")])
    _write(projects / "-work-alpha" / "sess-1" / "subagents" / "agent-x.jsonl", [_user(at(0, 10), "SUBAGENT-FILE")])

    config = tmp_path / "jarvis.yaml"
    config.write_text(yaml.safe_dump({"mac_ship": {"claude_code": {"roots": [
        {"account": "a", "dir": str(projects)}, {"account": "b", "dir": str(other)},
    ], "desktop_titles": str(tmp_path / "none")}}}), encoding="utf-8")
    out = tmp_path / "out"
    assert claude_code_day.main(["material", "--days", "6", "--out", str(out), "--config", str(config), "--runtime-root", str(tmp_path / "rt")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["dates"][day.isoformat()] == {"sessions": 2, "messages": 9}

    folder = out / "material" / day.isoformat()
    assert sorted(p.name for p in folder.iterdir()) == ["a-sess-1.md", "b-sess-2.md", "index.md"]
    alpha = (folder / "a-sess-1.md").read_text(encoding="utf-8")
    assert "- title: Alpha parser" in alpha and "- project folder: /work/alpha" in alpha
    assert "- first-last message: 09:05-01:30" in alpha
    for kept in (
        "[09:05] User: fix the parser", "[09:06] Claude: I will look at it.", "[09:10] User: keep going please",
        "[09:12] Claude: Done: parser fixed.", "[09:14] User: and one more thing", "[23:30] User: late evening question",
        "[01:30] Claude: answered after midnight, before sleep",
    ):
        assert kept in alpha
    for left_out in (
        "SECRET-THOUGHT", "TOOL-INPUT", "TOOL-OUTPUT", "REMINDER", "<command-", "meta line", "SIDECHAIN-TEXT",
        "NOTIFICATION", "after the sleep began", "SUBAGENT-FILE", "OWN-RUN-REPLY",
    ):
        assert left_out not in alpha
    index = (folder / "index.md").read_text(encoding="utf-8")
    assert "Sessions: 2" in index and "a-sess-1.md | Alpha parser | /work/alpha | account a" in index
    assert "b-sess-2.md | - | /work/beta | account b | 14:00-14:01 | 2 messages" in index
    assert "OWN-RUN" not in index and "own-run" not in index and "quiet" not in index

    # the 03:00 message is the next day's (not in this one), and a day with a report is left alone
    (out / f"{day.isoformat()}.md").write_text("done", encoding="utf-8")
    capsys.readouterr()
    assert claude_code_day.main(["material", "--days", "6", "--out", str(out), "--config", str(config), "--runtime-root", str(tmp_path / "rt")]) == 0
    assert day.isoformat() not in json.loads(capsys.readouterr().out)["dates"]


def test_without_a_sleep_a_day_ends_at_five_and_the_longest_sleep_wins() -> None:
    """The rule of the cut: 05:00 when no sleep begins in [18:00, next 14:00); else the longest."""
    day = date(2026, 10, 18)
    ms = _ms
    five = ms(datetime(2026, 10, 19, 5, tzinfo=VANCOUVER))
    assert claude_code_day.day_end(day, [], VANCOUVER, 0) == five
    nap, night = ms(datetime(2026, 10, 19, 13, tzinfo=VANCOUVER)), ms(datetime(2026, 10, 19, 2, 40, tzinfo=VANCOUVER))
    sleeps = [(nap, nap + 40 * 60_000), (night, night + 7 * 3_600_000), (ms(datetime(2026, 10, 18, 17, tzinfo=VANCOUVER)), None)]
    assert claude_code_day.day_end(day, sleeps, VANCOUVER, 0) == night
    asleep_now = ms(datetime(2026, 10, 19, 1, tzinfo=VANCOUVER))
    assert claude_code_day.day_end(day, [(asleep_now, None)], VANCOUVER, asleep_now + 3_600_000) == asleep_now


def test_an_unreachable_brain_cuts_at_five_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A token file that cannot be used, or a brain that does not answer, falls back."""
    config = tmp_path / "jarvis.yaml"
    config.write_text(yaml.safe_dump({"mac_ship": {"claude_code": {"roots": []}}}), encoding="utf-8")
    args = ["material", "--out", str(tmp_path / "o"), "--config", str(config), "--runtime-root", str(tmp_path)]
    assert claude_code_day.main(args) == 0
    assert "cut at 05:00" in capsys.readouterr().err  # no brain-token file under the runtime root


# --- the Mac ships it, the brain takes it --------------------------------------------------


class _Brain:
    """In-process stand-in for the PUT route: a ``Sink`` over the real file intake."""

    def __init__(self, directory: Path) -> None:
        self.intake = Shipments(directory, free_bytes=lambda _p: 10**12)
        self.sent: list[str] = []

    def send_entries(self, source: str, body: bytes) -> None:
        raise AssertionError((source, body))

    def send_file(self, source: str, relpath: str, data: bytes) -> str:
        self.sent.append(relpath)
        return self.intake.receive_file(source, relpath, data).sha256


REPORT = """jarvis-claude-code-daily: 1
date: 2026-10-18
timezone: America/Vancouver

## Jarvis day report
- sessions: 3
- time: 09:05-23:30
- summary: Built the material command.
- decisions: cut at sleep
- open: live test
- learned: prefers one logical commit
- mentions: ADR 0299

## Yana
- sessions: 1
- time: 14:00-14:40
- summary: Looked at the build script.
"""


def test_the_shipper_sends_a_changed_report_once_and_leaves_material_at_home(tmp_path: Path) -> None:
    """Top-level .md only, after it has been quiet; the same bytes are not sent twice."""
    folder = tmp_path / "claude-code-days"
    (folder / "material" / "2026-10-18").mkdir(parents=True)
    (folder / "material" / "2026-10-18" / "index.md").write_text("m", encoding="utf-8")
    (folder / "2026-10-18.md").write_text(REPORT, encoding="utf-8")
    (folder / "notes.txt").write_text("x", encoding="utf-8")
    brain, state, report = _Brain(tmp_path / "imports"), State(tmp_path / "state.db"), Report()
    modified = (folder / "2026-10-18.md").stat().st_mtime_ns // 1_000_000

    ship(folder, state, brain, report, modified + QUIET_MS - 1)  # still being written
    assert brain.sent == []
    ship(folder, state, brain, report, modified + QUIET_MS)
    ship(folder, state, brain, report, modified + 2 * QUIET_MS)
    assert brain.sent == ["2026-10-18.md"] and report.files == 1
    (folder / "2026-10-18.md").write_text(REPORT + "- extra: 1\n", encoding="utf-8")
    ship(folder, state, brain, report, modified + 10 * QUIET_MS)
    assert brain.sent == ["2026-10-18.md"] * 2
    kept = tmp_path / "imports" / CODE_DAY_SOURCE / "files"
    assert [p.name for p in kept.iterdir()] == ["2026-10-18.md"]
    assert (kept / "2026-10-18.md").read_text(encoding="utf-8").endswith("- extra: 1\n")
    state.close()


def test_the_file_intake_takes_markdown_for_its_one_source_only(tmp_path: Path) -> None:
    """.md, UTF-8, 256 KiB at most, for claude-code-daily; pictures still only for timesink."""
    intake = Shipments(tmp_path, free_bytes=lambda _p: 10**12)
    data = REPORT.encode()
    first = intake.receive_file(CODE_DAY_SOURCE, "2026-10-18.md", data)
    assert first.created and first.size == len(data)
    assert not intake.receive_file(CODE_DAY_SOURCE, "2026-10-18.md", data).created
    assert intake.receive_file(CODE_DAY_SOURCE, "2026-10-18.md", data + b"x").created  # a rewrite stands

    def refused(source: str, relpath: str, body: bytes) -> int:
        with pytest.raises(ShipRefused) as caught:
            intake.receive_file(source, relpath, body)
        return caught.value.status

    assert refused("timesink", "2026-10-18.md", data) == 422  # a picture source takes no Markdown
    assert refused("claude-code", "2026-10-18.md", data) == 404
    assert refused(CODE_DAY_SOURCE, "2026-10-18.txt", data) == 422
    assert refused(CODE_DAY_SOURCE, "sub/2026-10-18.md", data) == 422
    assert refused(CODE_DAY_SOURCE, "2026-10-19.md", b"") == 400
    assert refused(CODE_DAY_SOURCE, "2026-10-19.md", b"\xff\xfe\x00bad") == 415
    assert refused(CODE_DAY_SOURCE, "2026-10-19.md", b"x" * (MAX_REPORT_BYTES + 1)) == 413
    assert not (tmp_path / CODE_DAY_SOURCE / "files" / "2026-10-19.md").exists()


class _NoGmail:
    """A connection set that would fail if Gmail were touched."""

    def client_for(self, _name: str) -> object:
        message = "Gmail must not be touched"
        raise AssertionError(message)


def test_the_brain_reads_a_shipped_report_into_entries_and_the_newest_stands(tmp_path: Path) -> None:
    """Without Gmail: entries per project, one day item, a rewrite is a new revision."""
    imports_dir, db = tmp_path / imports.IMPORTS_DIRNAME, tmp_path / "memory.db"
    open_memory_db(db).close()
    brain = _Brain(imports_dir)
    brain.intake.receive_file(CODE_DAY_SOURCE, "2026-10-18.md", REPORT.encode())
    intake = ClaudeDailyIntake.code_reports_only(_NoGmail(), db, imports_dir)  # type: ignore[arg-type]
    assert intake.read_code_reports() == 1 and intake.read_code_reports() == 0

    def entries(kind: str = "") -> list[imports.Entry]:
        with closing(open_memory_db(db)) as conn:
            return imports.entries(conn, source="claude_code_daily", kinds=(kind,) if kind else ())

    assert sorted(e.fields["title"] for e in entries("daily_chat")) == ["Jarvis day report", "Yana"]
    (day,) = entries("daily")
    assert (day.fields["date"], day.fields["sections"], day.zone) == ("2026-10-18", 2, "America/Vancouver")
    with closing(open_memory_db(db)) as conn:
        assert imports.entries(conn, source="claude_daily") == []  # the chats letter's source is untouched
    raws = sorted(p.name.partition("-")[2] for p in (imports_dir / "claude_code_daily").iterdir())
    assert raws == ["2026-10-18.md"]

    def item() -> dict[str, Any]:
        conn = open_event_log(tmp_path / "events.db")
        first, last = day_window(date(2026, 10, 18), VANCOUVER)
        line = fold_day(conn, first, last, _ms(datetime(2026, 10, 19, 20, tzinfo=UTC)), DaySources(VANCOUVER, memory_db=db))
        conn.close()
        (found,) = [i for i in line.items if i["kind"] == "claude_day"]
        return found

    first_item = item()
    assert first_item["id"] == "claude-code-day-2026-10-18" and first_item["lane"] == "you"
    assert (first_item["source"], first_item["projects"], first_item["state"]) == ("claude_code_daily", 2, "done")
    assert first_item["text"] == "Jarvis day report; Yana" and "chats" not in first_item
    assert first_item["start_ms"] == _ms(datetime(2026, 10, 18, 9, 5, tzinfo=VANCOUVER))

    brain.intake.receive_file(CODE_DAY_SOURCE, "2026-10-18.md", REPORT.replace("## Yana", "## Yana build").encode())
    assert intake.read_code_reports() == 1
    assert item()["text"] == "Jarvis day report; Yana build"  # the rewrite stands for the day
    assert len(entries("daily")) == 2

    tool: Tool = build_archive_tool(db, VANCOUVER, lambda: datetime(2026, 10, 20, 9, 0, tzinfo=UTC))[0]
    found = cast("dict[str, Any]", tool.handler({"source": "claude_code_daily", "query": ["material command"]}, cast("ToolContext", None)))
    assert found["entries"] and all(e["source"] == "claude_code_daily" for e in found["entries"])
