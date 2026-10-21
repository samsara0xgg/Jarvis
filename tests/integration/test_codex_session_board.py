"""Hook-to-dashboard wire scenarios; no LLM or external Codex instance needed."""
from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from jarvis.shared.lang import t
from jarvis.surface import codex_sessions
from jarvis.surface.codex_sessions import settle_codex_sessions
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _noop(text: str) -> None:
    del text


@pytest.fixture
def sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Codex's sessions directory, where the daemon accepts a hook's rollout from."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "codex" / "sessions").mkdir(parents=True)
    return tmp_path / "codex" / "sessions"


def test_activity_retention_and_turn_identity_over_http() -> None:
    """New work never evicts running work; archive tokens change only per turn."""
    client = TestClient(create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(),
    )))
    with patch("jarvis.surface.inherent_server.agents.time.time", return_value=1000):
        for index in range(12):
            assert client.post("/inherent/codex-hook", json={
                "session_id": str(index), "hook_event_name": "UserPromptSubmit",
                "prompt": "scenario", "cwd": "/test",
            }).status_code == 200
        rows = client.get("/inherent/codex-sessions").json()["sessions"]
        assert len(rows) == 12
        original = next(r for r in rows if r["session_id"] == "0")["turn_started_ms"]
    with patch("jarvis.surface.inherent_server.agents.time.time", return_value=1001):
        for event in ["PermissionRequest", "PostToolUse", "Stop", "SessionEnd"]:
            client.post("/inherent/codex-hook", json={
                "session_id": "0", "hook_event_name": event,
                "last_assistant_message": "done", "tool_name": "shell",
            })
        row = next(r for r in client.get("/inherent/codex-sessions").json()["sessions"]
                   if r["session_id"] == "0")
        assert row["state"] == "finished"
        assert row["last_message"] == "done"
        assert row["turn_started_ms"] == original
    with patch("jarvis.surface.inherent_server.agents.time.time", return_value=1002):
        client.post("/inherent/codex-hook", json={
            "session_id": "0", "hook_event_name": "UserPromptSubmit", "prompt": "scenario",
        })
        row = next(r for r in client.get("/inherent/codex-sessions").json()["sessions"]
                   if r["session_id"] == "0")
        assert row["turn_started_ms"] > original
        client.post("/inherent/codex-hook", json={
            "session_id": "0", "hook_event_name": "Stop",
        })
    with patch("jarvis.surface.inherent_server.agents.time.time", return_value=1002 + 86400):
        rows = client.get("/inherent/codex-sessions").json()["sessions"]
        assert len(rows) == 11
        assert all(r["state"] == "running" for r in rows)
        client.post("/inherent/codex-hook", json={
            "session_id": "unexpected", "hook_event_name": "Unknown",
        })
        assert len(client.get("/inherent/codex-sessions").json()["sessions"]) == 11


def _rollout_line(
    path: Path, at_s: float, kind: str, *, entry_type: str = "event_msg", **payload: object,
) -> None:
    """Append one rollout line the way Codex writes it (compact JSON, UTC ISO time)."""
    stamp = datetime.fromtimestamp(at_s, UTC).isoformat(timespec="milliseconds")
    entry: dict[str, object] = {
        "timestamp": stamp.replace("+00:00", "Z"), "type": entry_type,
        "payload": {"type": kind, **payload},
    }
    if kind == "turn_context":
        entry.update(type="turn_context", payload=payload)
    with path.open("a") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def test_rollout_settles_what_hooks_miss(sessions: Path) -> None:
    """Auto-reviewed approvals never ask Allen; a turn ended without a Stop leaves running."""
    client = TestClient(create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(),
    )))
    auto, mine = sessions / "auto.jsonl", sessions / "mine.jsonl"
    for path, reviewer in ((auto, "auto_review"), (mine, "user")):
        _rollout_line(path, 999, "turn_context", turn_id="t1", approvals_reviewer=reviewer)
        _rollout_line(path, 999, "task_started", turn_id="t1")

    def hook(sid: str, path: Path, event: str, **extra: object) -> None:
        client.post("/inherent/codex-hook", json={
            "session_id": sid, "hook_event_name": event, "transcript_path": str(path),
            "turn_id": "t1", "tool_name": "Bash", "tool_input": {"command": "git push"}, **extra,
        })

    def row(sid: str) -> dict[str, object]:
        rows = client.get("/inherent/codex-sessions").json()["sessions"]
        return next(r for r in rows if r["session_id"] == sid)

    with patch("jarvis.surface.inherent_server.agents.time.time", return_value=1000):
        hook("auto", auto, "PermissionRequest")
        hook("mine", mine, "PermissionRequest")
        assert row("auto")["state"] == "running"
        assert row("mine")["state"] == "needs_input"
    # No Stop arrives: an interrupt, or a Stop handler Codex skips.
    _rollout_line(auto, 1001, "task_complete", turn_id="t1", last_agent_message="Pushed.")
    _rollout_line(mine, 1001, "turn_aborted", turn_id="t1", reason="interrupted")
    with patch("jarvis.surface.inherent_server.agents.time.time", return_value=1002):
        assert (row("auto")["state"], row("auto")["last_message"]) == ("finished", "Pushed.")
        assert (row("mine")["state"], row("mine")["detail"]) == ("idle", t("codex.turn_stopped"))
        # A new prompt is not closed by the end of the turn before it.
        hook("auto", auto, "UserPromptSubmit", prompt="next")
        assert row("auto")["state"] == "running"


def test_codex_question_waits_on_allen_until_he_writes(sessions: Path) -> None:
    """Codex asks and works on: the row asks until Allen sends anything, past the turn's end."""
    client = TestClient(create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(),
    )))
    path = sessions / "rollout.jsonl"
    _rollout_line(path, 999, "task_started", turn_id="t1")
    asked = t("codex.asks", question="Web prototype or SwiftUI?")

    def hook(event: str, **extra: object) -> None:
        client.post("/inherent/codex-hook", json={
            "session_id": "s", "hook_event_name": event, "transcript_path": str(path),
            "turn_id": "t1", "tool_name": "Bash", **extra,
        })

    def row() -> dict[str, object]:
        return dict(client.get("/inherent/codex-sessions").json()["sessions"][0])

    with patch("jarvis.surface.inherent_server.agents.time.time", return_value=1000):
        hook("UserPromptSubmit", prompt="polish the app")
    _rollout_line(
        path, 1001, "function_call", entry_type="response_item", call_id="c1",
        name="request_user_input_async",
        arguments=json.dumps({"questions": [{"title": "Web prototype or SwiftUI?"}]}),
    )
    with patch("jarvis.surface.inherent_server.agents.time.time", return_value=1002):
        assert (row()["state"], row()["detail"]) == ("needs_input", asked)
        # Codex keeps working; its tool calls neither answer nor hide the question,
        # not even once the call has scrolled out of the rollout's tail.
        hook("PostToolUse")
        _rollout_line(path, 1002, "function_call_output", entry_type="response_item",
                      output="x" * 300_000)
        assert (row()["state"], row()["detail"]) == ("needs_input", asked)
    _rollout_line(path, 1003, "task_complete", turn_id="t1", last_agent_message="Went with web.")
    with patch("jarvis.surface.inherent_server.agents.time.time", return_value=1004):
        assert (row()["state"], row()["last_message"]) == ("needs_input", "Went with web.")
    # His answer, as Codex records any message he sends in the thread.
    _rollout_line(path, 1005, "task_started", turn_id="t2")
    _rollout_line(path, 1005, "item_completed", item={"type": "UserMessage"})
    with patch("jarvis.surface.inherent_server.agents.time.time", return_value=1006):
        assert (row()["state"], row()["detail"]) == ("running", "")


def _within(seconds: float, call: Callable[[], object]) -> None:
    """Run ``call`` and fail, rather than hang the suite, when it has not returned in time."""
    done = threading.Event()

    def run() -> None:
        call()
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(seconds), "the call is stuck on a file it should have refused"


def _permission_request(client: TestClient, path: str, session: str = "s") -> dict[str, object]:
    """Post a PermissionRequest naming ``path`` as the rollout; the board row it leaves."""
    reply = client.post("/inherent/codex-hook", json={
        "session_id": session, "hook_event_name": "PermissionRequest", "transcript_path": path,
        "turn_id": "t1", "tool_name": "Bash", "tool_input": {"command": "git push"},
    })
    assert reply.status_code == 200
    rows = client.get("/inherent/codex-sessions").json()["sessions"]
    return dict(next(row for row in rows if row["session_id"] == session))


def _reviewer_line(reviewer: str) -> str:
    line = {"type": "turn_context", "payload": {"approvals_reviewer": reviewer}}
    return json.dumps(line, separators=(",", ":")) + "\n"  # compact, as Codex writes it


def test_a_hook_cannot_make_the_daemon_read_what_is_not_a_rollout(
    sessions: Path, tmp_path: Path,
) -> None:
    """A device, a FIFO, a link out of the directory and a file elsewhere are not read."""
    client = TestClient(create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(),
    )))
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text(_reviewer_line("auto_review"))
    fifo = sessions / "fifo.jsonl"
    os.mkfifo(fifo)
    link = sessions / "link.jsonl"
    link.symlink_to(elsewhere)

    for refused in ("/dev/zero", str(fifo), str(link), str(elsewhere)):
        def post(path: str = refused) -> None:
            row = _permission_request(client, path, session=path)
            # Read as a rollout, "elsewhere" would have said Codex's reviewer answers.
            assert row["state"] == "needs_input"
            assert "transcript_path" not in row

        _within(10, post)


def test_an_oversized_rollout_is_read_only_as_far_as_the_cap(sessions: Path) -> None:
    """The reviewer line is found within the cap, not by reading the whole file."""
    client = TestClient(create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(),
    )))
    near, far = sessions / "near.jsonl", sessions / "far.jsonl"
    for path, behind in ((near, 1024 * 1024), (far, 20 * 1024 * 1024)):
        with path.open("wb") as fh:
            fh.write(_reviewer_line("auto_review").encode())
            fh.truncate(fh.tell() + behind)  # a hole: megabytes of zeros after the line

    assert _permission_request(client, str(near), "near")["state"] == "running"
    assert _permission_request(client, str(far), "far")["state"] == "needs_input"


def test_a_row_naming_a_device_or_a_fifo_is_settled_without_reading_it(tmp_path: Path) -> None:
    """The reader itself refuses what is not a regular file, whatever put the path on the row."""
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    board = {
        name: {"session_id": name, "state": "running", "since_ms": 1, "transcript_path": path}
        for name, path in (("zero", "/dev/zero"), ("fifo", str(fifo)))
    }

    _within(10, lambda: settle_codex_sessions(board))

    assert [row["state"] for row in board.values()] == ["running", "running"]


def test_reading_a_rollout_does_not_hold_the_event_loop(
    sessions: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a hook's rollout is being read, the daemon still answers its other requests."""
    rollout = sessions / "rollout.jsonl"
    rollout.write_text(_reviewer_line("user"))
    reading, loop_free = threading.Event(), threading.Event()
    held_the_loop: list[bool] = []
    real = codex_sessions._rollout  # noqa: SLF001

    def slow(path: str, *, tail: int) -> str:
        reading.set()
        held_the_loop.append(not loop_free.wait(10))
        return real(path, tail=tail)

    monkeypatch.setattr(codex_sessions, "_rollout", slow)
    with TestClient(create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(),
    ))) as client:
        hook = threading.Thread(target=_permission_request, args=(client, str(rollout)))
        hook.start()
        assert reading.wait(10)
        assert client.get("/api/health").status_code == 200
        loop_free.set()
        hook.join(10)

    assert held_the_loop
    assert not any(held_the_loop)
