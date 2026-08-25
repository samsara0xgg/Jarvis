"""decide()-level tests for the Tier 0 fast path (spec §17).

Proves: a whitelist hit dispatches with caller_principal=regex_router
through the FULL gate/audit chain and never calls the LLM; a miss falls
through to the Tier 2 loop unchanged; a table entry pointing at a tool
regex_router may not call is refused by the Pre-action Gate (defense in
depth — bootstrap validation normally rejects such a table).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import (
    _TIER0_TOOL_ERROR_TEXT,
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
    decide,
)
from jarvis.decision.llm import ChatResult, LLMClient
from jarvis.decision.pre_emit_phrases import COMPLETION_REGEXES
from jarvis.decision.tier0 import load_tier0_table, validate_tier0_table
from jarvis.execution.tools import (
    ActionLifecycle,
    ToolDefinition,
    ToolRegistry,
    build_default_registry,
    get_current_time_handler,
)
from jarvis.shared import CallerPrincipal, RawResult
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.decision.tier0 import Tier0Table
    from jarvis.shared import ActionRequest, Event


@dataclass(frozen=True)
class _StubRuntimePaths:
    """RuntimePathsLike-shaped stub — the Tier 0 tools never read it."""

    event_log: Path
    artifacts_root: Path

    def artifact_dir_for_run(self, run_id: str) -> Path:
        """Return a per-run dir under ``artifacts_root`` (created on demand)."""
        out = self.artifacts_root / run_id
        out.mkdir(parents=True, exist_ok=True)
        return out


class _StubLLMClient:
    """Stand-in :class:`LLMClient` that returns canned limitation-language text.

    The canned text passes the Pre-emit Gate at attempt 0 (no completion
    keywords), so ``decide()`` finalizes in one chat call without a retry
    or network access.
    """

    _CANNED_TEXT = "agent reported, status unverified — awaiting your reply."

    def __init__(self) -> None:
        self.chat_calls = 0
        self.model = "stub-model"
        self._last_input_tokens: int | None = 0
        self._last_output_tokens: int | None = 0
        self._last_finish_reason: str | None = "stop"

    @property
    def last_input_tokens(self) -> int | None:
        return self._last_input_tokens

    @property
    def last_output_tokens(self) -> int | None:
        return self._last_output_tokens

    @property
    def last_finish_reason(self) -> str | None:
        return self._last_finish_reason

    @contextmanager
    def fresh_context(self) -> Iterator[_StubLLMClient]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],  # noqa: ARG002 — stub ignores args.
        system: str,  # noqa: ARG002 — stub ignores args.
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002 — stub ignores args.
        tool_choice: str | None = "auto",  # noqa: ARG002 — stub ignores args.
    ) -> ChatResult:
        self.chat_calls += 1
        return ChatResult(
            text=self._CANNED_TEXT,
            tool_calls=(),
            finish_reason="stop",
            input_tokens=0,
            output_tokens=0,
            raw={},
            model_used="stub-model",
            tokens_in=0,
            tokens_out=0,
        )


class _ExplodingLLMClient(_StubLLMClient):
    """Chat must never be reached on a Tier 0 hit."""

    def chat(self, **kwargs: object) -> ChatResult:  # noqa: ARG002 — stub ignores args.
        msg = "Tier 0 hit must not call the LLM"
        raise AssertionError(msg)


_TABLE_YAML = """\
- id: time_now
  pattern: "^现在几点了?[?？。.!！]?$"
  tool: get_current_time
  template: "现在是{spoken_time}。"
"""  # noqa: RUF001 — fullwidth CJK punctuation is the routing data under test.


def _table(tmp_path: Path) -> Tier0Table:
    """Write + load a one-entry whitelist targeting ``get_current_time``."""
    p = tmp_path / "tier0_patterns.yaml"
    p.write_text(_TABLE_YAML, encoding="utf-8")
    return load_tier0_table(p)


def _build_ctx(
    tmp_path: Path,
    *,
    llm: _StubLLMClient | None = None,
    tier0_table: Tier0Table | None = None,
    registry: ToolRegistry | None = None,
) -> tuple[DecideContext, sqlite3.Connection]:
    """Build a DecideContext + return the open Event Log conn."""
    conn = open_event_log(tmp_path / "events.db")
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db",
        artifacts_root=tmp_path / "artifacts",
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    if registry is None:
        registry = build_default_registry()
    lifecycle = ActionLifecycle()
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", lifecycle),
        llm_client=cast("LLMClient", llm if llm is not None else _StubLLMClient()),
        system_prompt="stub system prompt",
        tier0_table=tier0_table,
    )
    return ctx, conn


_REGEX_ONLY_TOOL = "regex_only_probe"

_REGEX_ONLY_YAML = (
    "- id: probe\n"
    '  pattern: "^现在几点$"\n'
    f"  tool: {_REGEX_ONLY_TOOL}\n"
    '  template: "探针 {spoken_time}"\n'
)


def _registry_with_regex_only_tool() -> ToolRegistry:
    """Default registry plus a tool ONLY ``regex_router`` may call.

    No shipped tool is regex-router-only today, which is the sole reason
    the surface-mismatch this pins is dormant. The handler is reused from
    ``get_current_time`` so the dispatch/lifecycle/event behavior is the
    real thing and only the caller scoping differs.
    """
    registry = build_default_registry()
    registry.register(
        ToolDefinition(
            name=_REGEX_ONLY_TOOL,
            description="clock probe visible only on the regex_router surface",
            allowed_callers=frozenset({CallerPrincipal.REGEX_ROUTER}),
            risk_level="L0",
            result_semantics="observation",
            is_async=False,
            input_schema={"type": "object", "properties": {}},
            handler=get_current_time_handler,
        ),
    )
    return registry


_ERROR_SLOT_TOOL = "error_slot_probe"

_ERROR_SLOT_YAML = (
    "- id: error_probe\n"
    '  pattern: "^现在几点$"\n'
    f"  tool: {_ERROR_SLOT_TOOL}\n"
    '  template: "探针 {spoken_time}"\n'
)

# A handler error tag that is itself completion-class (``\bdone\b``).
# Handler error tags are developer strings, not curated user copy, so
# nothing stops one from reading like a completion claim.
_COMPLETION_FLAVORED_ERROR = "task done but verification failed"


def _error_slot_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,  # noqa: ARG001 — signature uniformity; no events needed.
    runtime_paths: object,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """Fail the action and return an error slot carrying a completion keyword."""
    lifecycle.transition(action_request.action_id, "failed")
    return RawResult(
        action_id=action_request.action_id,
        semantics="error",
        payload={},
        tool_output=None,
        error=_COMPLETION_FLAVORED_ERROR,
    )


def _registry_with_error_slot_tool() -> ToolRegistry:
    """Default registry plus a regex_router tool that always errors."""
    registry = build_default_registry()
    registry.register(
        ToolDefinition(
            name=_ERROR_SLOT_TOOL,
            description="probe that always returns an error slot",
            allowed_callers=frozenset({CallerPrincipal.REGEX_ROUTER}),
            risk_level="L0",
            result_semantics="error",
            is_async=False,
            input_schema={"type": "object", "properties": {}},
            handler=_error_slot_handler,
        ),
    )
    return registry


def _emit_intent(conn: sqlite3.Connection, transcript: str) -> Event:
    """Emit the ``surface.user_intent`` trigger that drives ``decide()``."""
    return emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": transcript, "turn_id": "T_t0", "channel": "cli_stdin"},
        correlation={"turn_id": "T_t0"},
    )


def test_tier0_hit_bypasses_llm_and_runs_full_audit_chain(tmp_path: Path) -> None:
    """A whitelist hit dispatches through the full chain with zero LLM calls.

    Spec §17 + §3.5.2: the low-latency path still passes the Pre-action
    Gate and lands the same proposed/authorized/result_observed audit
    rows the Tier 2 loop would, only with ``caller_principal`` =
    ``regex_router`` plus the ``routed_by`` / ``pattern_id`` provenance
    keys. The exploding LLM stub is the bypass proof.
    """
    ctx, conn = _build_ctx(tmp_path, llm=_ExplodingLLMClient(), tier0_table=_table(tmp_path))
    try:
        result = decide(_emit_intent(conn, "现在几点"), ctx)
        assert result.response_plan is not None
        assert result.response_plan.text.startswith("现在是")
        types = [e.type for e in result.events_emitted]
        assert "turn.started" in types
        assert "action.proposed" in types
        assert "action.authorized" in types
        assert "turn.ended" in types
        proposed = next(e for e in result.events_emitted if e.type == "action.proposed")
        assert proposed.payload["caller_principal"] == "regex_router"
        assert proposed.payload["routed_by"] == "tier_0"
        assert proposed.payload["pattern_id"] == "time_now"
        gate_rows = [
            e for e in result.events_emitted
            if e.type == "gate.evaluated" and e.payload.get("gate") == "pre_action"
        ]
        assert gate_rows
        assert gate_rows[0].payload["outcome"] == "pass"
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='action.result_observed'",
        ).fetchone()
        assert int(row[0]) == 1
        assert result.attention_channel == "queue_review"
    finally:
        conn.close()


def test_tier0_resolves_regex_router_only_tool(tmp_path: Path) -> None:
    """A tool on the regex_router surface must resolve in the Tier 0 path.

    ``validate_tier0_table`` certifies whitelist entries against
    ``for_caller(REGEX_ROUTER)``, so the dispatcher's name resolution has
    to see at least that surface. When it scanned the ``JARVIS_LLM``
    surface instead, a bootstrap-certified entry hit ``tool_def is None``
    and silently degraded to the LLM — a config the daemon had just
    declared valid, dropped at runtime behind one log line.

    The table below is asserted bootstrap-legal first, so the pin cannot
    rot into "this config was never valid anyway".
    """
    p = tmp_path / "regex_only.yaml"
    p.write_text(_REGEX_ONLY_YAML, encoding="utf-8")
    table = load_tier0_table(p)
    registry = _registry_with_regex_only_tool()
    validate_tier0_table(
        table,
        allowed_tool_names=frozenset(
            t.name for t in registry.for_caller(CallerPrincipal.REGEX_ROUTER)
        ),
        async_tool_names=frozenset(t.name for t in registry.get_definitions() if t.is_async),
    )

    ctx, conn = _build_ctx(
        tmp_path, llm=_ExplodingLLMClient(), tier0_table=table, registry=registry,
    )
    try:
        result = decide(_emit_intent(conn, "现在几点"), ctx)
        assert result.response_plan is not None
        assert result.response_plan.text.startswith("探针 ")
        proposed = next(e for e in result.events_emitted if e.type == "action.proposed")
        assert proposed.payload["tool_name"] == _REGEX_ONLY_TOOL
        assert proposed.payload["caller_principal"] == "regex_router"
        gate_rows = [
            e for e in result.events_emitted
            if e.type == "gate.evaluated" and e.payload.get("gate") == "pre_action"
        ]
        assert gate_rows
        assert gate_rows[0].payload["outcome"] == "pass"
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='action.result_observed'",
        ).fetchone()
        assert int(row[0]) == 1
    finally:
        conn.close()


def test_tier0_tool_error_never_reaches_the_llm_or_the_surface(tmp_path: Path) -> None:
    """An erroring Tier 0 tool must answer with fixed text, not the handler's tag.

    Interpolating ``primary_slot.error`` into the draft made the Tier 0
    reply a function of developer error strings. With an open task in
    scope (so ``_finalize_response`` picks a real active subject with no
    verified Postcondition), an error tag containing a completion
    keyword drove the Pre-emit Gate to ``downgrade_required`` — and the
    downgrade path re-prompts ``ctx.llm_client.chat``. That breaks the
    one invariant Tier 0 exists for: no LLM on this path. The exploding
    stub below is the proof; the seeded open task is what makes the
    subject non-None.
    """
    p = tmp_path / "error_probe.yaml"
    p.write_text(_ERROR_SLOT_YAML, encoding="utf-8")
    registry = _registry_with_error_slot_tool()
    ctx, conn = _build_ctx(
        tmp_path,
        llm=_ExplodingLLMClient(),
        tier0_table=load_tier0_table(p),
        registry=registry,
    )
    try:
        emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_open", "goal": "unverified work", "source": "manual"},
        )
        result = decide(_emit_intent(conn, "现在几点"), ctx)

        assert result.response_plan is not None
        assert result.response_plan.text == _TIER0_TOOL_ERROR_TEXT
        assert _COMPLETION_FLAVORED_ERROR not in result.response_plan.text
        for regex in COMPLETION_REGEXES:
            assert regex.search(result.response_plan.text) is None

        # Exactly one Pre-emit verdict (attempt 0) — attempts 1/2 exist
        # only on the downgrade path, which is where the LLM re-prompt
        # lives.
        pre_emit_rows = [
            e for e in result.events_emitted
            if e.type == "gate.evaluated" and e.payload.get("gate") == "pre_emit"
        ]
        assert [e.payload["attempt"] for e in pre_emit_rows] == [0]
        # An open task WAS in scope — otherwise the gate short-circuits
        # and this test would pass for the wrong reason.
        assert pre_emit_rows[0].payload["outcome"] == "force_limitation_language"
    finally:
        conn.close()


def test_tier0_miss_falls_through_to_llm(tmp_path: Path) -> None:
    """A transcript outside the whitelist takes the unchanged Tier 2 loop.

    The transcript deliberately avoids a demonstrative + task-noun pair
    so ``_no_task_to_refer_to``'s F1 short-circuit does not preempt the
    LLM call this test is measuring.
    """
    stub = _StubLLMClient()
    ctx, conn = _build_ctx(tmp_path, llm=stub, tier0_table=_table(tmp_path))
    try:
        result = decide(_emit_intent(conn, "帮我看看现在的进度"), ctx)
        assert result.response_plan is not None
        assert stub.chat_calls == 1
    finally:
        conn.close()


def test_tier0_none_table_behaves_like_day1(tmp_path: Path) -> None:
    """No table loaded → Tier 0 is disabled and every turn reaches the LLM."""
    stub = _StubLLMClient()
    ctx, conn = _build_ctx(tmp_path, llm=stub, tier0_table=None)
    try:
        decide(_emit_intent(conn, "现在几点"), ctx)
        assert stub.chat_calls == 1  # scaffold behavior preserved
    finally:
        conn.close()


def test_tier0_gate_refuses_disallowed_tool(tmp_path: Path) -> None:
    """A table naming a tool regex_router may not call is refused, not run.

    Hand-built to bypass ``validate_tier0_table``: ``list_tasks`` is a
    sync tool whose ``allowed_callers`` is ``{jarvis_llm}``, so the
    Pre-action Gate's ``caller_allowed`` check must refuse it and the
    turn must end on fixed text without ever dispatching or reaching the
    LLM (defense in depth behind bootstrap validation).
    """
    bad_yaml = (
        '- id: bad\n  pattern: "^现在几点$"\n  tool: list_tasks\n  template: "x"\n'
    )
    p = tmp_path / "bad.yaml"
    p.write_text(bad_yaml, encoding="utf-8")
    ctx, conn = _build_ctx(
        tmp_path, llm=_ExplodingLLMClient(), tier0_table=load_tier0_table(p),
    )
    try:
        result = decide(_emit_intent(conn, "现在几点"), ctx)
        assert result.response_plan is not None
        assert "未执行" in result.response_plan.text
        gate_rows = [
            e for e in result.events_emitted
            if e.type == "gate.evaluated" and e.payload.get("gate") == "pre_action"
        ]
        assert gate_rows
        assert gate_rows[0].payload["outcome"] == "refuse"
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='action.result_observed'",
        ).fetchone()
        assert int(row[0]) == 0  # never dispatched
    finally:
        conn.close()
