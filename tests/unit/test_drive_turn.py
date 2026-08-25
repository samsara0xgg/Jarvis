"""Unit tests for :func:`jarvis.runtime.drive_turn` (ADR-0003 Step 3).

``drive_turn`` is the extract from the original ``run_turn`` body that
takes a pre-emitted ``surface.user_intent`` :class:`Event` and drives
the L3 decide loop + finalize + render. It exists so non-CLI surfaces
(the daemon HTTP handler in Step 7 + the daemon watcher in Step 8) can
drive a turn from an event that the HTTP handler already wrote to the
log — Step 8 must NOT re-emit ``surface.user_intent`` or the watcher
would loop infinitely.

This module covers the LLM-free invariants of the extract:

- ``drive_turn`` reads ``turn_id`` from
  ``user_intent_event.payload["turn_id"]`` and threads it through the
  :class:`RunTurnResult`.
- ``drive_turn`` does NOT re-emit ``surface.user_intent`` — the row
  count for that event type stays at exactly one across the call.
- ``drive_turn`` emits exactly one ``surface.response_emitted`` row
  whose ``turn_id`` payload matches the input event's ``turn_id``.
- The basic shape of :class:`RunTurnResult` is preserved
  (``iterations >= 1``, ``response_plan`` non-None, ``response_text``
  populated, ``events_emitted`` includes the render event).
- The new thin ``run_turn`` wrapper composes emit + delegate in the
  right order: ``surface.user_intent`` precedes
  ``surface.response_emitted`` in the log.

ADR-0009 Step 5 adds the live-action-set lifecycle:

- ``drive_turn`` releases every action its dispatches registered, on the
  success path and on the crash path alike. D4: "a crashed turn must not
  pin its actions 'active' forever" — a pinned action_id is one the
  supervisor sweep will refuse to close for the life of the process.

All tests are Tier 1 (LLM-free): the L3 ``decide`` call is replaced via
``monkeypatch`` with a deterministic stub that returns a final
:class:`ResponsePlan` on the first invocation, so ``drive_turn``
finalizes in one iteration without touching the network.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from jarvis.decision import DecideResult
from jarvis.decision.gates import ResponsePlan
from jarvis.execution.tools import (
    live_action_ids,
    register_live_action,
    release_turn_actions,
)
from jarvis.runtime import (
    JarvisRuntime,
    RunTurnResult,
    bootstrap_runtime_app,
    drive_turn,
    run_turn,
)
from jarvis.state.event_log import emit_event
from jarvis.surface.cli import emit_surface_user_intent

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jarvis.shared import Event


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "jarvis.yaml"
_PROMPT_PATH = _REPO_ROOT / "prompts" / "jarvis_v1.md"

# The deterministic response text the stubbed ``decide`` returns. Kept
# scrub-safe by construction (no completion-class keywords) so the
# Pre-emit Gate token check in render_response is the only enforcement
# we need to satisfy — drive_turn always primes the token with the
# plan's response_hash before calling render_response.
_STUB_RESPONSE_TEXT: str = "drive_turn unit-test response — status pending."


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[JarvisRuntime]:
    """Bootstrap a real :class:`JarvisRuntime` against ``tmp_path``.

    No LLM is invoked during bootstrap — the :class:`LLMClient` SDK is
    constructed lazily on the first ``chat`` call, which the stubbed
    ``decide`` short-circuits.
    """
    rt = bootstrap_runtime_app(
        config_path=_CONFIG_PATH,
        prompt_path=_PROMPT_PATH,
        runtime_root=tmp_path,
    )
    yield rt
    rt.conn.close()


def _stub_response_plan() -> ResponsePlan:
    """Build a deterministic :class:`ResponsePlan` for the stubbed decide."""
    text = _STUB_RESPONSE_TEXT
    return ResponsePlan(
        text=text,
        permission="force_limitation_language",
        downgrade_required=False,
        active_claim_levels=(),
        response_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        output_risk_class="routine",
        required_gate_mode="sentence",
    )


@pytest.fixture
def stub_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``jarvis.runtime.decide`` with a one-shot final-plan stub.

    The stub returns a :class:`DecideResult` whose ``response_plan`` is
    populated on the first invocation, so :func:`drive_turn` finalizes
    after one iteration with no need to wait for an async trigger.
    """

    def _fake_decide(trigger: Event, _ctx: object) -> DecideResult:
        # Echo the trigger's turn_id back so RunTurnResult.turn_id is
        # exactly the value the runtime threaded in.
        turn_id = trigger.payload.get("turn_id") if trigger.type == "surface.user_intent" else None
        return DecideResult(
            response_plan=_stub_response_plan(),
            events_emitted=(),
            turn_id=turn_id if isinstance(turn_id, str) else None,
            attention_channel="voice_notify",
        )

    monkeypatch.setattr("jarvis.runtime.decide", _fake_decide)


# --- Helpers --------------------------------------------------------------


def _count_event_rows(runtime: JarvisRuntime, event_type: str) -> int:
    """Return the number of rows in the event log with ``type == event_type``."""
    cursor = runtime.conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = ?",
        (event_type,),
    )
    row = cursor.fetchone()
    return int(row[0])


def _select_event_rows(
    runtime: JarvisRuntime,
    event_type: str,
) -> list[tuple[int, str]]:
    """Return ``(id, payload_json)`` pairs for every row matching ``event_type``."""
    cursor = runtime.conn.execute(
        "SELECT id, payload_json FROM events WHERE type = ? ORDER BY id ASC",
        (event_type,),
    )
    return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


# --- Tests ----------------------------------------------------------------


def test_drive_turn_uses_turn_id_from_intent_event_payload(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001 — fixture installs the monkeypatch
) -> None:
    """``RunTurnResult.turn_id`` MUST come from ``user_intent_event.payload``."""
    explicit_turn_id = "explicit-test-turn-id"
    user_intent_event = emit_surface_user_intent(
        runtime.conn,
        transcript="hello",
        turn_id=explicit_turn_id,
    )
    result = drive_turn(runtime, user_intent_event=user_intent_event)
    assert isinstance(result, RunTurnResult)
    assert result.turn_id == explicit_turn_id


def test_drive_turn_does_not_re_emit_surface_user_intent(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001
) -> None:
    """``drive_turn`` MUST NOT emit a second ``surface.user_intent`` row.

    The Step-8 watcher contract: the HTTP handler already wrote the
    intent event when the operator POSTed text; if ``drive_turn``
    re-emitted, the watcher would observe the new row and recurse.
    """
    user_intent_event = emit_surface_user_intent(
        runtime.conn,
        transcript="ping",
        turn_id="T_noemit",
    )
    assert _count_event_rows(runtime, "surface.user_intent") == 1
    drive_turn(runtime, user_intent_event=user_intent_event)
    assert _count_event_rows(runtime, "surface.user_intent") == 1, (
        "drive_turn re-emitted surface.user_intent; the daemon watcher would "
        "loop infinitely if this invariant breaks."
    )


def test_drive_turn_emits_surface_response_emitted(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001
) -> None:
    """A ``surface.response_emitted`` row MUST land tagged with the same ``turn_id``."""
    user_intent_event = emit_surface_user_intent(
        runtime.conn,
        transcript="hello",
        turn_id="T_response",
    )
    drive_turn(runtime, user_intent_event=user_intent_event)

    response_rows = _select_event_rows(runtime, "surface.response_emitted")
    assert len(response_rows) == 1, (
        f"expected exactly one surface.response_emitted row, got "
        f"{len(response_rows)}: {response_rows!r}"
    )
    _, payload_json = response_rows[0]
    payload = json.loads(payload_json)
    assert payload["turn_id"] == "T_response", (
        f"surface.response_emitted payload missing matching turn_id: {payload!r}"
    )


def test_drive_turn_returns_run_turn_result_with_iterations_at_least_one(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001
) -> None:
    """Basic :class:`RunTurnResult` shape check."""
    user_intent_event = emit_surface_user_intent(
        runtime.conn,
        transcript="hello",
        turn_id="T_shape",
    )
    result = drive_turn(runtime, user_intent_event=user_intent_event)
    assert result.iterations >= 1
    assert result.response_plan is not None
    assert result.response_plan.text == _STUB_RESPONSE_TEXT
    assert _STUB_RESPONSE_TEXT in result.response_text
    # The render event is appended onto events_emitted at the end of
    # drive_turn; the stub decide returned an empty events tuple, so
    # collected_events is exactly ``(render_event,)``.
    assert len(result.events_emitted) == 1
    assert result.events_emitted[0].type == "surface.response_emitted"


def test_run_turn_thin_wrapper_emits_then_delegates(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001
) -> None:
    """``run_turn`` MUST emit ``surface.user_intent`` BEFORE ``surface.response_emitted``.

    Proves the composition: mint -> emit -> delegate -> render. If the
    wrapper accidentally swapped order or skipped the emit, the log
    would not show both rows in the expected sequence.
    """
    result = run_turn(runtime, utterance="hello world", turn_id="T_wrapper")

    intent_rows = _select_event_rows(runtime, "surface.user_intent")
    response_rows = _select_event_rows(runtime, "surface.response_emitted")
    assert len(intent_rows) == 1
    assert len(response_rows) == 1
    # SQLite id is monotonic per the event log; intent MUST precede response.
    intent_id, _ = intent_rows[0]
    response_id, _ = response_rows[0]
    assert intent_id < response_id, (
        f"run_turn wrapper out of order: intent id {intent_id} should "
        f"precede response id {response_id}"
    )
    assert result.turn_id == "T_wrapper"


def test_drive_turn_threads_minted_turn_id_when_wrapper_mints(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001
) -> None:
    """``run_turn`` minted ``turn_id`` MUST flow through drive_turn end-to-end."""
    result = run_turn(runtime, utterance="hello world")
    assert result.turn_id, "wrapper-minted turn_id must be non-empty"
    # The minted id must also appear on the surface.user_intent row.
    intent_rows = _select_event_rows(runtime, "surface.user_intent")
    assert len(intent_rows) == 1
    _, payload_json = intent_rows[0]
    assert json.loads(payload_json)["turn_id"] == result.turn_id


def test_drive_turn_does_not_emit_extra_user_intent_when_log_preseeded(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001
) -> None:
    """If the log already has unrelated rows, drive_turn still adds exactly one intent row.

    Guards against a regression where drive_turn might re-emit the
    incoming event under the misapprehension that the log is empty.
    The pre-seeded event is unrelated (``task.created``) and does not
    affect the surface.user_intent count.
    """
    # Pre-seed an unrelated event so the log is non-empty when
    # drive_turn runs.
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_pre",
            "goal": "pre-seed",
            "source": "manual",
        },
    )
    user_intent_event = emit_surface_user_intent(
        runtime.conn,
        transcript="hello",
        turn_id="T_preseed",
    )
    drive_turn(runtime, user_intent_event=user_intent_event)
    assert _count_event_rows(runtime, "surface.user_intent") == 1


# --- ADR-0009 Step 5: live action-id set lifecycle -------------------------


def test_drive_turn_releases_live_actions_on_the_success_path(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001
) -> None:
    """Actions registered during a turn MUST NOT outlive it."""
    user_intent_event = emit_surface_user_intent(
        runtime.conn,
        transcript="hello",
        turn_id="T_live_ok",
    )
    register_live_action(turn_id="T_live_ok", action_id="A_live_ok")
    assert "A_live_ok" in live_action_ids()

    drive_turn(runtime, user_intent_event=user_intent_event)

    assert "A_live_ok" not in live_action_ids(), (
        "ADR-0009 D4: drive_turn owns the release side of the live action set."
    )


def test_drive_turn_releases_live_actions_when_the_turn_crashes(
    runtime: JarvisRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising ``decide`` MUST still release the turn's actions.

    D4 pins this explicitly: "a crashed turn must not pin its actions
    'active' forever". A leaked action_id makes the supervisor sweep
    skip that action for the life of the daemon, so the very orphan the
    crash created can never be closed.
    """

    class _TurnExplodedError(RuntimeError):
        """Raised by the stub decide to simulate a mid-turn crash."""

    def _exploding_decide(trigger: Event, _ctx: object) -> DecideResult:
        turn_id = trigger.payload["turn_id"]
        register_live_action(turn_id=str(turn_id), action_id="A_live_crash")
        raise _TurnExplodedError

    monkeypatch.setattr("jarvis.runtime.decide", _exploding_decide)

    user_intent_event = emit_surface_user_intent(
        runtime.conn,
        transcript="hello",
        turn_id="T_live_crash",
    )
    with pytest.raises(_TurnExplodedError):
        drive_turn(runtime, user_intent_event=user_intent_event)

    assert "A_live_crash" not in live_action_ids(), (
        "drive_turn must release its live action_ids from a finally that runs "
        "on the exception path too."
    )


def test_drive_turn_releases_only_its_own_turns_actions(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001
) -> None:
    """A concurrent turn's actions MUST survive this turn's release.

    The daemon drives turns on worker threads; a release that dropped
    the whole set would unprotect every other in-flight turn.
    """
    register_live_action(turn_id="T_other", action_id="A_other")
    user_intent_event = emit_surface_user_intent(
        runtime.conn,
        transcript="hello",
        turn_id="T_live_scope",
    )
    try:
        drive_turn(runtime, user_intent_event=user_intent_event)
        assert "A_other" in live_action_ids()
    finally:
        release_turn_actions("T_other")
