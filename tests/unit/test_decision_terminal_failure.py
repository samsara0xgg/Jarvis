"""Unit tests for the L3 terminal-failure dispatch branch (B-0003c).

Covers :func:`jarvis.decision._handle_action_terminal_failure` —
the new branch that ``decide()`` enters when L4's spawn_worker
Timer emits ``action.timeout_assumed`` or ``action.failed``.

Per B-0003c / ADR-0002 Negative-path appendix (lines 1593-1600):

- The handler must emit ``claim.created(type=Limitation)`` +
  ``evidence.attached(relation=limits, level=reported)`` via the
  Result Interpreter's ``_SEMANTICS_TO_CLAIM["error"]`` row.
- The handler must produce a :class:`ResponsePlan` carrying user-
  facing limitation language so the surface never silently swallows
  the failure (the live B-0003 bug).
- When the spawning ``action.proposed`` event is missing
  (e.g. action_id from a different process), the handler must
  degrade gracefully — default ``tool_name="spawn_worker"``, still
  emit the Limitation Claim, still return a non-None response_plan.

The tests build a minimal :class:`jarvis.decision.DecideContext`
with a stub LLM client (no network) and exercise ``decide()``
end-to-end on a tmp_path-backed Event Log.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import (
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
    decide,
)
from jarvis.decision.llm import ChatResult, LLMClient
from jarvis.decision.pre_emit_phrases import LIMITATION_REGEXES
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.shared import Event


# --- stubs ----------------------------------------------------------------


@dataclass(frozen=True)
class _StubRuntimePaths:
    """RuntimePathsLike-shaped stub — the terminal-failure branch never reads it."""

    event_log: Path
    artifacts_root: Path

    def artifact_dir_for_run(self, run_id: str) -> Path:
        """Return a per-run dir under ``artifacts_root`` (created on demand)."""
        out = self.artifacts_root / run_id
        out.mkdir(parents=True, exist_ok=True)
        return out


class _StubLLMClient:
    """Stand-in for :class:`jarvis.decision.llm.LLMClient`.

    ``chat()`` returns the configured text with no tool_calls so the
    decide() retry path (if reached) finalizes immediately with the
    canned text.
    """

    def __init__(self, *, response_text: str = "") -> None:
        """Configure the canned response text returned by every chat call."""
        self._response_text = response_text
        self.chat_calls = 0
        self.model = "stub-model"
        self._last_input_tokens: int | None = 0
        self._last_output_tokens: int | None = 0
        self._last_finish_reason: str | None = "stop"

    @property
    def last_input_tokens(self) -> int | None:
        """Mirror :attr:`LLMClient.last_input_tokens` — populated lazily."""
        return self._last_input_tokens

    @property
    def last_output_tokens(self) -> int | None:
        """Mirror :attr:`LLMClient.last_output_tokens` — populated lazily."""
        return self._last_output_tokens

    @property
    def last_finish_reason(self) -> str | None:
        """Mirror :attr:`LLMClient.last_finish_reason` — populated lazily."""
        return self._last_finish_reason

    @contextmanager
    def fresh_context(self) -> Iterator[_StubLLMClient]:
        """Mirror :meth:`LLMClient.fresh_context` — no-op for the stub."""
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],  # noqa: ARG002 — stub ignores args.
        system: str,  # noqa: ARG002 — stub ignores args.
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002 — stub ignores args.
        tool_choice: str | None = "auto",  # noqa: ARG002 — stub ignores args.
    ) -> ChatResult:
        """Return the canned text; record call count for assertions."""
        self.chat_calls += 1
        return ChatResult(
            text=self._response_text,
            tool_calls=(),
            finish_reason="stop",
            input_tokens=0,
            output_tokens=0,
            raw={},
            model_used="stub-model",
            tokens_in=0,
            tokens_out=0,
        )


def _build_ctx(tmp_path: Path) -> tuple[DecideContext, sqlite3.Connection]:
    """Build a DecideContext + return the open Event Log conn.

    The conn is returned separately so the caller can close it after
    the test (``ctx`` is frozen and the conn is one of its fields).
    """
    conn = open_event_log(tmp_path / "events.db")
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db",
        artifacts_root=tmp_path / "artifacts",
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    registry = build_default_registry()
    lifecycle = ActionLifecycle()
    # Stub LLM is configured to RAISE on call: the terminal-failure
    # handler must finalize with the canonical limitation text at the
    # Pre-emit Gate's attempt-0 (no retry). Any chat() invocation here
    # signals a regression in the gate's negative-lookbehind for ``完成``.
    llm = _StubLLMClient(response_text="UNREACHED — terminal failure should not retry")
    # The stubs are structural fits for the runtime / LLM Protocols
    # the DecideContext consumes; the cast() calls quiet mypy's nominal
    # check without weakening the runtime contract — decide() never
    # touches the network because the stub LLM short-circuits ``chat``.
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", lifecycle),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt",
    )
    return ctx, conn


def _seed_utterance_and_proposed(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: str,
    action_id: str,
    turn_id: str,
) -> None:
    """Seed surface.user_intent + task.created + action.proposed.

    The proposed event must reference an existing task row so the Task
    Ledger projection records the task and the Pre-emit Gate can find
    the active subject. ``action.proposed`` is the row
    :func:`_handle_action_terminal_failure` looks up to recover the
    spawning ``tool_name`` (B-0003c degradation path key).
    """
    emit_event(
        conn,
        type="task.created",
        payload={"task_id": task_id, "goal": "make Codex do thing", "source": "manual"},
    )
    emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "go do the thing", "turn_id": turn_id},
        correlation={"turn_id": turn_id},
    )
    emit_event(
        conn,
        type="action.proposed",
        payload={
            "action_id": action_id,
            "tool_name": "spawn_worker",
            "caller_principal": "jarvis_llm",
            "risk_level": "L2",
            "target_entity_ref": task_id,
            "turn_id": turn_id,
            "run_id": run_id,
        },
        correlation={
            "action_id": action_id,
            "run_id": run_id,
            "turn_id": turn_id,
            "task_id": task_id,
        },
    )


def _emit_timeout(  # noqa: PLR0913 — every correlation id is load-bearing; the helper is local to this test module.
    conn: sqlite3.Connection,
    *,
    action_id: str,
    run_id: str,
    task_id: str,
    turn_id: str,
    error: str = "codex_turn_timeout",
    reason: str = "codex turn timed out after 600s",
) -> Event:
    """Emit an ``action.timeout_assumed`` row and return the Event."""
    return emit_event(
        conn,
        type="action.timeout_assumed",
        payload={"action_id": action_id, "error": error, "reason": reason},
        correlation={
            "action_id": action_id,
            "run_id": run_id,
            "turn_id": turn_id,
            "task_id": task_id,
        },
    )


def _emit_failed(  # noqa: PLR0913 — every correlation id is load-bearing; the helper is local to this test module.
    conn: sqlite3.Connection,
    *,
    action_id: str,
    run_id: str,
    task_id: str,
    turn_id: str,
    error: str = "codex_subprocess_crashed",
    reason: str = "codex subprocess exited non-zero",
) -> Event:
    """Emit an ``action.failed`` row and return the Event."""
    return emit_event(
        conn,
        type="action.failed",
        payload={"action_id": action_id, "error": error, "reason": reason},
        correlation={
            "action_id": action_id,
            "run_id": run_id,
            "turn_id": turn_id,
            "task_id": task_id,
        },
    )


def _events_of_type(events: tuple[Event, ...], event_type: str) -> list[Event]:
    """Return the subset of ``events`` matching ``event_type``."""
    return [e for e in events if e.type == event_type]


# --- tests ----------------------------------------------------------------


def test_action_timeout_assumed_emits_limitation_claim(tmp_path: Path) -> None:
    """Timeout trigger -> Limitation Claim + reported Evidence + response_plan."""
    ctx, conn = _build_ctx(tmp_path)
    try:
        _seed_utterance_and_proposed(
            conn,
            task_id="task_X",
            run_id="R1",
            action_id="A1",
            turn_id="T1",
        )
        trigger = _emit_timeout(
            conn,
            action_id="A1",
            run_id="R1",
            task_id="task_X",
            turn_id="T1",
        )

        result = decide(trigger, ctx)

        claim_events = _events_of_type(result.events_emitted, "claim.created")
        limitation_claims = [c for c in claim_events if c.payload.get("type") == "Limitation"]
        assert limitation_claims, (
            "expected at least one claim.created(type=Limitation) emitted by "
            f"the terminal-failure handler; got {claim_events!r}"
        )

        evidence_events = _events_of_type(result.events_emitted, "evidence.attached")
        limits_reported = [
            e
            for e in evidence_events
            if e.payload.get("relation") == "limits"
            and e.payload.get("level") == "reported"
        ]
        assert limits_reported, (
            "expected at least one evidence.attached(relation=limits, "
            f"level=reported); got {evidence_events!r}"
        )

        assert result.response_plan is not None
        # Canonical text passes the Pre-emit Gate at attempt 0 thanks to
        # the ``(?<![未没不])完成`` negative lookbehind on the gate's
        # completion detector. No LLM retry should fire on this path.
        assert "超时" in result.response_plan.text, (
            f"response_plan.text={result.response_plan.text!r} does not contain "
            f"the canonical '超时' phrasing; B-0003c requires the timeout path "
            "to surface the ADR Negative-path appendix string."
        )
        assert cast("_StubLLMClient", ctx.llm_client).chat_calls == 0, (
            "terminal-failure handler unexpectedly invoked the LLM — gate "
            "regression: canonical limitation text should pass attempt 0."
        )
    finally:
        conn.close()


def test_action_failed_emits_limitation_claim_with_crash_text(tmp_path: Path) -> None:
    """action.failed trigger -> Limitation Claim + crash-flavored response_plan."""
    ctx, conn = _build_ctx(tmp_path)
    try:
        _seed_utterance_and_proposed(
            conn,
            task_id="task_X",
            run_id="R2",
            action_id="A2",
            turn_id="T2",
        )
        trigger = _emit_failed(
            conn,
            action_id="A2",
            run_id="R2",
            task_id="task_X",
            turn_id="T2",
        )

        result = decide(trigger, ctx)

        limitation_claims = [
            c
            for c in _events_of_type(result.events_emitted, "claim.created")
            if c.payload.get("type") == "Limitation"
        ]
        assert limitation_claims, (
            "expected at least one Limitation Claim on the action.failed path"
        )

        limits_reported = [
            e
            for e in _events_of_type(result.events_emitted, "evidence.attached")
            if e.payload.get("relation") == "limits"
            and e.payload.get("level") == "reported"
        ]
        assert limits_reported, (
            "expected at least one evidence.attached(relation=limits, level=reported)"
        )

        assert result.response_plan is not None
        # Canonical crash text passes the Pre-emit Gate at attempt 0.
        assert "跑挂" in result.response_plan.text, (
            f"response_plan.text={result.response_plan.text!r} does not contain "
            f"the canonical '跑挂' phrasing; B-0003c requires the action.failed "
            "path to surface the ADR Negative-path appendix string."
        )
        assert cast("_StubLLMClient", ctx.llm_client).chat_calls == 0, (
            "terminal-failure handler unexpectedly invoked the LLM on the "
            "crash path — gate regression."
        )
    finally:
        conn.close()


def test_terminal_failure_handler_no_double_emission_on_unknown_action_id(
    tmp_path: Path,
) -> None:
    """Unknown action_id -> degraded path: still emits Limitation, returns plan, no raise."""
    ctx, conn = _build_ctx(tmp_path)
    try:
        # Seed an utterance + task but NO action.proposed for the action_id
        # we are about to time out — exercises the degradation path that
        # defaults ``tool_name="spawn_worker"``.
        emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_X", "goal": "g", "source": "manual"},
        )
        emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "go", "turn_id": "T3"},
            correlation={"turn_id": "T3"},
        )
        trigger = _emit_timeout(
            conn,
            action_id="A_orphan",
            run_id="R3",
            task_id="task_X",
            turn_id="T3",
        )

        result = decide(trigger, ctx)

        limitation_claims = [
            c
            for c in _events_of_type(result.events_emitted, "claim.created")
            if c.payload.get("type") == "Limitation"
        ]
        assert limitation_claims, (
            "expected one Limitation Claim even on the orphan-action_id path"
        )
        # Statement is rendered from the synthetic ActionRequest's
        # tool_name; B-0003c spec requires the default to be
        # "spawn_worker" when no prior action.proposed is found.
        statements = [c.payload.get("statement", "") for c in limitation_claims]
        assert any("spawn_worker" in s for s in statements), (
            f"Limitation claim statement(s) did not mention the default "
            f"tool_name='spawn_worker'; got {statements!r}"
        )

        assert result.response_plan is not None
    finally:
        conn.close()


def test_limitation_regex_matches_canonical_timeout_and_crash_strings() -> None:
    """Direct regex unit test against the canonical phrasings (B-0003c).

    ADR-0002 Negative-path appendix lines 1593-1600 specify the
    canonical user-facing limitation phrasings on the spawn_worker
    terminal-failure paths. The matching regexes must live in
    :data:`jarvis.decision.pre_emit_phrases.LIMITATION_REGEXES` per
    ``test_canary_regex_constants_single_source`` (single source of
    truth).
    """
    timeout_text = "Codex 超时，未完成"  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.
    crash_text = "Codex 跑挂了，没新 diff"  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.

    assert any(pat.search(timeout_text) for pat in LIMITATION_REGEXES), (
        f"no LIMITATION_REGEXES pattern matched the canonical timeout "
        f"phrasing {timeout_text!r}; expected the B-0003c addition "
        f"r'超时.{{0,4}}未完成' to match."
    )
    assert any(pat.search(crash_text) for pat in LIMITATION_REGEXES), (
        f"no LIMITATION_REGEXES pattern matched the canonical crash "
        f"phrasing {crash_text!r}; expected the B-0003c addition "
        f"r'跑挂' to match."
    )
