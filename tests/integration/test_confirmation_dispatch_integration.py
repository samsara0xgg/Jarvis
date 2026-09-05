"""Real L3/L4 confirmation races, durable admission and crash boundaries."""

# ruff: noqa: ANN401, SLF001 - barriers instrument the real decision boundary

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis import decision
from jarvis.decision import DecideContext, decide
from jarvis.decision.confirm_grammar import ConfirmGrammarRule
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, ToolRegistry, build_default_registry
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.state.authorized_dispatch_outbox import AuthorizedDispatchAlreadyStarted
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.shared import ActionRequest


def test_two_real_deciders_accept_one_pending_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both cached pending slots and both pure gates race; append only once."""
    path = tmp_path / "events.db"
    target = tmp_path / "target.txt"
    target.write_text("", encoding="utf-8")
    artifact = tmp_path / "pending.txt"
    artifact.write_text("once", encoding="utf-8")
    setup = open_event_log(path)
    emit_event(
        setup,
        type="confirmation.requested",
        payload={
            "confirmation_id": "CONF-race",
            "template_line": "approved append",
            "expires_at_ms": int(time.time() * 1000) + 60_000,
            "action_snapshot": {
                "tool_name": "write_file",
                "caller": "jarvis_llm",
                "risk_level": "L3",
                "canonical_target": str(target),
                "target_entity_ref": f"file:{target}",
                "args_meta": {
                    "target": str(target),
                    "mode": "append",
                    "content_sha256": hashlib.sha256(b"once").hexdigest(),
                    "content_bytes": 4,
                    "content_artifact": str(artifact),
                },
            },
        },
    )
    triggers = [
        emit_event(
            setup,
            type="surface.user_intent",
            payload={
                "transcript": "可以",
                "turn_id": f"T-{i}",
            },
        )
        for i in range(2)
    ]
    setup.close()
    acceptance_barrier = threading.Barrier(2)
    gate_barrier = threading.Barrier(2)
    original_accept = decision._handle_confirmation_accepted
    original_gate = decision.pre_action_gate

    def accept(*args: Any, **kwargs: Any) -> Any:
        acceptance_barrier.wait(timeout=5)
        return original_accept(*args, **kwargs)

    def gate(*args: Any, **kwargs: Any) -> Any:
        result = original_gate(*args, **kwargs)
        gate_barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(decision, "_handle_confirmation_accepted", accept)
    monkeypatch.setattr(decision, "pre_action_gate", gate)

    def run(index: int) -> None:
        conn = open_event_log(path)
        try:
            registry = build_default_registry(confirmation_dispatch_outbox=True)
            ctx = DecideContext(
                conn=conn,
                runtime_paths=cast(
                    "Any",
                    SimpleNamespace(
                        event_log=path,
                        artifacts_root=tmp_path,
                    ),
                ),
                tool_registry=cast("Any", registry),
                lifecycle=cast("Any", ActionLifecycle()),
                llm_client=cast("Any", SimpleNamespace()),
                system_prompt="integration",
                entity_bookmarks=(("target", str(target)),),
                confirm_grammar_table=(ConfirmGrammarRule("yes", re.compile("可以"), "yes"),),
                wave1_features=Wave1FeatureFlags(confirmation_dispatch_outbox=True),
            )
            decide(triggers[index], ctx)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))
    check = open_event_log(path)
    counts = {
        kind: check.execute("SELECT COUNT(*) FROM events WHERE type=?", (kind,)).fetchone()[0]
        for kind in ("confirmation.accepted", "action.dispatched", "action.running")
    }
    assert counts == {"confirmation.accepted": 1, "action.dispatched": 1, "action.running": 1}
    assert target.read_text(encoding="utf-8") == "once"
    assert check.execute("SELECT COUNT(*) FROM confirmation_consumption_claims").fetchone()[0] == 1
    assert check.execute("SELECT state FROM authorized_dispatch_outbox").fetchall() == [
        ("dispatched",)
    ]
    check.close()


@pytest.mark.parametrize("failure", ["before_admission_commit", "after_admission_commit"])
def test_real_registry_outbox_retry_respects_crash_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Rollback is retryable; a committed admission is ambiguous and cannot repeat."""
    paths = bootstrap_runtime(tmp_path / "runtime")
    conn = open_event_log(paths.event_log)
    target = tmp_path / "target.txt"
    target.write_text("")
    artifact = tmp_path / "pending.txt"
    artifact.write_text("once")
    emit_event(
        conn,
        type="confirmation.requested",
        payload={
            "confirmation_id": "CONF-crash",
            "template_line": "approved append",
            "expires_at_ms": int(time.time() * 1000) + 60_000,
            "action_snapshot": {
                "tool_name": "write_file",
                "caller": "jarvis_llm",
                "risk_level": "L3",
                "canonical_target": str(target),
                "target_entity_ref": f"file:{target}",
                "args_meta": {
                    "target": str(target),
                    "mode": "append",
                    "content_sha256": hashlib.sha256(b"once").hexdigest(),
                    "content_bytes": 4,
                    "content_artifact": str(artifact),
                },
            },
        },
    )
    trigger = emit_event(
        conn, type="surface.user_intent", payload={"transcript": "可以", "turn_id": "crash-turn"}
    )
    requests: list[ActionRequest] = []
    original_dispatch = ToolRegistry.dispatch

    def capture(self: ToolRegistry, request: ActionRequest, *args: Any) -> Any:
        requests.append(request)
        return original_dispatch(self, request, *args)

    monkeypatch.setattr(ToolRegistry, "dispatch", capture)
    if failure == "before_admission_commit":
        conn.execute(
            "CREATE TRIGGER fail_dispatch BEFORE INSERT ON events "
            "WHEN NEW.type = 'action.dispatched' BEGIN "
            "SELECT RAISE(ABORT, 'injected dispatch failure'); END"
        )
    else:

        def fail_handler(*_args: Any, **_kwargs: Any) -> Any:
            message = "injected dispatch failure"
            raise sqlite3.IntegrityError(message)

        monkeypatch.setattr(ToolRegistry, "_run_inline", fail_handler)
    registry = build_default_registry(confirmation_dispatch_outbox=True)
    ctx = DecideContext(
        conn=conn,
        runtime_paths=paths,
        tool_registry=cast("Any", registry),
        lifecycle=cast("Any", ActionLifecycle()),
        llm_client=cast("Any", SimpleNamespace()),
        system_prompt="fixture",
        entity_bookmarks=(("target", str(target)),),
        confirm_grammar_table=(ConfirmGrammarRule("yes", re.compile("可以"), "yes"),),
        wave1_features=Wave1FeatureFlags(confirmation_dispatch_outbox=True),
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected dispatch failure"):
            decide(trigger, ctx)
        assert target.read_text() == ""
        expected = "pending" if failure == "before_admission_commit" else "dispatched"
        assert conn.execute("SELECT state FROM authorized_dispatch_outbox").fetchall() == [
            (expected,)
        ]
        if failure == "before_admission_commit":
            conn.execute("DROP TRIGGER fail_dispatch")
        monkeypatch.undo()
        conn.close()
        conn = open_event_log(paths.event_log)
        lifecycle = ActionLifecycle()
        lifecycle.register(requests[0].action_id)
        lifecycle.transition(requests[0].action_id, "authorized")
        registry = build_default_registry(confirmation_dispatch_outbox=True)
        if failure == "before_admission_commit":
            registry.dispatch(requests[0], conn, paths, lifecycle)
            assert target.read_text() == "once"
        else:
            with pytest.raises(AuthorizedDispatchAlreadyStarted):
                registry.dispatch(requests[0], conn, paths, lifecycle)
            assert target.read_text() == ""
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE type='action.dispatched'").fetchone()[0]
            == 1
        )
    finally:
        conn.close()
