"""Real lock and cancellation barriers for the independent review counterexamples."""

# ruff: noqa: ANN401, SLF001 - inspect actual runtime/provider boundaries

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis import decision
from jarvis.decision.llm import ChatResult
from jarvis.decision.llm_session import LLMRequestClient
from jarvis.decision.response_run import ResponseCancelledError
from jarvis.runtime import make_response_cancel_callable
from jarvis.shared.realtime import AuthorizedDispatch
from jarvis.state import authorized_dispatch_outbox as outbox
from jarvis.state.event_log import open_event_log
from tests.integration.test_wave1_concurrency_safety import (
    _confirmation_candidate,
    _seed_accepted_confirmation,
)
from tests.integration.test_wave4a_response_run import (
    _drive_turn_on_own_connection,
    _emit_intent,
    _event_count,
    _make_runtime,
    _payloads,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("stage", ["authorize", "admit"])
def test_authorization_ttl_is_checked_after_sqlite_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    """A real competing writer holds admission across the lease's expiry."""
    path = tmp_path / "events.db"
    setup = open_event_log(path)
    accepted, snapshot = _seed_accepted_confirmation(setup, suffix=stage)
    request, lease = _confirmation_candidate(accepted.event_uid, snapshot, random_suffix=stage)
    outbox.ensure_authorized_dispatch_schema(setup)
    clock = [2_049_000]
    monkeypatch.setattr(outbox, "time", SimpleNamespace(time=lambda: clock[0] / 1000))
    gate = {"gate": "pre_action", "outcome": "pass", "reasons": []}
    if stage == "admit":
        dispatch = outbox.authorize_confirmation_dispatch(
            setup, source_confirmation_event_id=accepted.event_uid,
            action_request=request, lease=lease, gate_payload=gate,
        )
        assert isinstance(dispatch, AuthorizedDispatch)
        stable_lease = lease.copy()
        stable_lease["lease_id"] = dispatch.identity.lease_id
        request = replace(request, action_id=dispatch.identity.action_id,
                          authorization_lease=stable_lease)
    waiting = threading.Event()
    setup.execute("BEGIN IMMEDIATE")

    def worker() -> None:
        conn = sqlite3.connect(path, timeout=5)
        conn.set_trace_callback(lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None)
        try:
            if stage == "authorize":
                outbox.authorize_confirmation_dispatch(
                    conn, source_confirmation_event_id=accepted.event_uid,
                    action_request=request, lease=lease, gate_payload=gate,
                )
            else:
                outbox.admit_authorized_dispatch(
                    conn, request,
                    payload={"action_id": request.action_id, "tool_name": "write_file"},
                )
        finally:
            conn.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                assert waiting.wait(5)
                clock[0] = 2_051_000
            finally:
                setup.commit()
            with pytest.raises(outbox.ConfirmationRevalidationError, match="expired"):
                future.result(timeout=5)
        states = setup.execute("SELECT state FROM authorized_dispatch_outbox").fetchall()
        assert states == ([("pending",)] if stage == "admit" else [])
        assert _event_count(setup, "action.dispatched") == 0
    finally:
        setup.close()


def test_cancel_during_pricing_prevents_provider_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real drive_turn must not enter chat after cancellation won during setup."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    runtime = replace(runtime, wave1_features=replace(
        runtime.wave1_features, exactly_once_cost_accounting=True,
    ))
    intent = _emit_intent(runtime.conn, "pricing-cancel", "现在时间")
    entered, release = threading.Event(), threading.Event()
    original_pricing = decision._pricing_table
    calls: list[int] = []

    def pricing() -> Any:
        entered.set()
        assert release.wait(5)
        return original_pricing()

    def chat(_self: LLMRequestClient, **_kwargs: Any) -> ChatResult:
        calls.append(1)
        return _chat_result()

    monkeypatch.setattr(decision, "_pricing_table", pricing)
    monkeypatch.setattr(LLMRequestClient, "chat", chat)
    cancel = make_response_cancel_callable(runtime)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_drive_turn_on_own_connection, runtime,
                                 user_intent_event=intent, available_surfaces=frozenset())
            try:
                assert entered.wait(5)
                response_id = _payloads(runtime.conn, "response.started")[0]["response_id"]
                assert cancel(response_id, "generation", "user_stop") == "cancelled"
            finally:
                release.set()
            with pytest.raises(ResponseCancelledError):
                future.result(timeout=5)
        assert calls == []
        assert _event_count(runtime.conn, "response.request_admitted") == 0
        assert _event_count(runtime.conn, "cost.recorded") == 0
        assert _event_count(runtime.conn, "surface.response_emitted") == 0
    finally:
        runtime.conn.close()


def _chat_result() -> ChatResult:
    return ChatResult(
        text='{"verdict":"ok","reasons":[]}', tool_calls=(), finish_reason="stop",
        input_tokens=10, output_tokens=5, raw={}, tokens_in=10, tokens_out=5,
        model_used="gpt-fast",
    )
