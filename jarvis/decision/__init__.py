"""L3 Runtime Decision — ``decide()`` entry point + supporting pipeline.

Per ADR 0001 § Stub strategy L3 rows, § Resolver contract, § Gate
contracts, § Canonical event trace evt 03..24.

This package exports the public surface of L3:

- :class:`SituationPacket` (from :mod:`jarvis.decision.packet`).
- :class:`EffectivePolicy` (from :mod:`jarvis.decision.policy`).
- :class:`GateResult` / :class:`ResponsePlan` (from
  :mod:`jarvis.decision.gates`).
- :class:`ResolverResult` (from :mod:`jarvis.decision.resolver`).
- :func:`decide` — the single function the composition root
  (Step 10) wires into the runtime loop.
- :class:`DecideContext` / :class:`DecideResult` — call / return
  shapes for :func:`decide`.

The full pipeline orchestration lives here. Submodules implement the
individual stages:

- ``packet.py`` — Situation Packet assembler.
- ``policy.py`` — Effective Policy Resolver.
- ``resolver.py`` — pure, LLM-free entity-ref resolver
  (canary H10 asserts no ``jarvis.decision.llm`` import).
- ``intent.py`` — Tier 0 scaffold + Tier 2 LLM bridge.
- ``gates.py`` — Pre-action / Pre-emit gates + Attention Policy.
- ``result_interpreter.py`` — Post-action gate / claim+evidence
  emission per the Day-1 semantics->level table.
- ``llm.py`` — multi-provider LLM client (Step 8).

``decide()`` handles **one** trigger per call and returns. Multi-trigger
orchestration (utterance -> worker.reported -> verify -> turn.ended)
lives in Step 10's ``jarvis/runtime``. Step 12's scenario test exercises
the full loop through ``decide()`` re-entry.

Layer rules: stdlib + ``jarvis.shared`` + ``jarvis.state`` +
``jarvis.constitution`` (optional). MUST NOT import
``jarvis.execution``, ``jarvis.surface``, ``jarvis.deployment``,
``jarvis.runtime``, ``jarvis.cli``. L4 / L6 surfaces are reached via
``RuntimePathsLike`` / ``ToolRegistryLike`` / ``LifecycleLike``
Protocols that the runtime composition root satisfies structurally.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from jarvis.decision.gates import (
    AttentionChannel,
    GateOutcome,
    GateResult,
    PreEmitPermission,
    ResponsePlan,
    attention_policy,
    pre_action_gate,
    pre_emit_gate,
)
from jarvis.decision.intent import (
    build_llm_messages,
    tier_0_match,
    tool_definitions_for_llm,
)
from jarvis.decision.packet import SituationPacket, assemble_packet
from jarvis.decision.policy import EffectivePolicy, effective_policy
from jarvis.decision.pre_emit_phrases import COMPLETION_REGEXES
from jarvis.decision.resolver import (
    ResolverConfidence,
    ResolverResult,
    resolve_task_ref,
    resolve_task_ref_by_window,
)
from jarvis.decision.result_interpreter import (
    interpret_verify_diff_bundle,
    result_interpreter,
)
from jarvis.decision.reviewer import ReviewerVerdict, review_diff
from jarvis.shared import (
    ActionRequest,
    CallerPrincipal,
    Event,
    RawResult,
    RawResultBundle,
)
from jarvis.shared.pricing import compute_cost_usd, load_pricing_table
from jarvis.state.event_log import emit_event
from jarvis.state.projections import make_snapshot

if TYPE_CHECKING:
    import sqlite3

    from jarvis.decision.llm import ChatResult, LLMClient
    from jarvis.shared import EvidenceLevel, RiskLevel

LOGGER = logging.getLogger(__name__)

# Hard ceiling on the tool-use loop in `decide()`. Defends against an LLM
# that keeps proposing tool calls without converging.
_DEFAULT_MAX_TOOL_ITERATIONS = 5

# Limitation-language template used when Pre-emit Gate forces a downgrade
# after the LLM's second attempt still claims completion. Day-1 keeps it
# Chinese-first per the prompt asset's Chinese-default rule. Acceptance F4
# regex includes "未验证" so this template satisfies it on the negative path.
_FORCED_LIMITATION_TEMPLATE = (
    "tool result: {draft}\n— Pre-emit Gate forced limitation framing (unverified / 未验证)."
)

# Completion-keyword scrub set. Mirrors the gate's _COMPLETION_KEYWORDS
# (in `jarvis.decision.gates`) in spirit; the gate decides whether to
# FORCE downgrade, this scrub decides what text to render once the
# forced template fires. Kept separate from the gate's regex set so spec
# changes can evolve independently (the gate adds/removes detection
# patterns; the scrub adds/removes redaction patterns). The
# coverage drift guard
# `tests/unit/test_pre_emit_forced_template.py::test_completion_scrub_covers_every_gate_keyword`
# locks in the mapping — every new gate keyword must declare an
# explicit scrub counterpart there.
#
# ADR-0002 Step 13: the canonical completion regex patterns live in
# :mod:`jarvis.decision.pre_emit_phrases` (``COMPLETION_REGEXES``). The
# scrub consumes the canonical source-text via ``.pattern`` (so
# ``re.sub(..., flags=re.IGNORECASE)`` is applied uniformly here) and
# appends two scrub-only synonyms the gate doesn't detect today —
# ``completed`` / ``finished`` — so the forced template doesn't leak
# them. Bare `完成` is canonical-anchored with `^` rather than scrubbed
# mid-text because CJK has no `\b` word boundary and unrooted `完成`
# mid-string false-matches phrases like `完成度` / `完成情况`. If the
# gate trips on mid-text `完成`, the forced template still trips and
# `_hard_refusal_plan` is the final defense.
_COMPLETION_SCRUB_PATTERNS: Final[tuple[str, ...]] = (
    *tuple(pat.pattern for pat in COMPLETION_REGEXES),
    r"\bcompleted\b",       # scrub-only synonym (gate doesn't detect)
    r"\bfinished\b",        # scrub-only synonym (gate doesn't detect)
)

_COMPLETION_REDACTION_MARKER: Final[str] = "[redacted-completion-claim]"


def _scrub_completion_keywords(text: str) -> str:
    """Replace completion-class keywords with a redaction marker.

    Used by the forced limitation template when the LLM's retry still
    claims completion — the embedded draft must not carry bare
    completion words to the surface. Case-insensitive by default.
    """
    out = text
    for pat in _COMPLETION_SCRUB_PATTERNS:
        out = re.sub(pat, _COMPLETION_REDACTION_MARKER, out, flags=re.IGNORECASE)
    return out


def _hard_refusal_plan(
    active_subject: str,
    *,
    active_claim_levels: tuple[EvidenceLevel, ...] = (),
) -> ResponsePlan:
    r"""Build a fixed limitation ResponsePlan with no LLM-supplied text.

    Used as the last line of defense when both the LLM retry and the
    scrubbed forced template still trip the Pre-emit Gate. The text
    uses ``未验证 / unverified`` (F4 limitation regex hit via ``未验证``
    in ``_LIMITATION_PATTERNS`` and the ``\bunverified\b`` negation
    marker, NOT bare ``\bverified\b``) and avoids every completion
    keyword the gate detects, so it is scrub-safe by construction.

    ``active_claim_levels`` is carried through from the last Pre-emit
    Gate verdict (typically ``forced_plan.active_claim_levels``) so the
    final ResponsePlan still records what evidence levels the subject
    actually held. Defaults to ``()`` only for direct unit-test calls
    that don't have a gate verdict to thread through.

    F1: branch the user-facing text on what evidence the subject
    actually holds, so Allen sees "找不到 task" / "verify 没过" /
    "没有 diff" instead of operator-facing gate jargon. Branch-4 (the
    catch-all) keeps the original wording. All branches must contain
    both "未验证" and "unverified" and must NOT match any pattern in
    :data:`jarvis.decision.gates._COMPLETION_KEYWORDS` (scrub-safe by
    construction — see ``test_hard_refusal_plan_text_variants``).
    """
    if not active_claim_levels:
        text = "找不到对应的 task（未验证 / unverified）。"  # noqa: RUF001 — intentional Chinese punctuation.
    elif "executed" in active_claim_levels:
        text = (
            "Codex 跑了但 verify 没过（未验证 / unverified），"  # noqa: RUF001 — intentional Chinese punctuation.
            "verify_command 返回非 0。"
        )
    elif tuple(active_claim_levels) == ("reported",):
        text = "Codex 报告了但没产生可验证的 diff（未验证 / unverified）。"  # noqa: RUF001 — intentional Chinese punctuation.
    else:
        text = (
            f"agent reported, status unverified (未验证) — Pre-emit Gate "
            f"refused completion language for subject {active_subject} "
            f"(no Postcondition evidence)."
        )
    # Hard refusal is always routine — by construction it carries no
    # completion claim, so spec §3.4.13's risk class is the floor.
    return ResponsePlan(
        text=text,
        permission="force_limitation_language",
        downgrade_required=False,  # this text is scrub-safe by construction
        active_claim_levels=active_claim_levels,
        response_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        output_risk_class="routine",
        required_gate_mode="sentence",
    )


# --- Pricing table loader (ADR-0002 Step 3 § cost.recorded plumbing) ------


def _repo_data_pricing_json() -> Path:
    """Return the absolute path to ``<repo>/data/pricing.json``.

    The shared pricing module defaults to a cwd-relative path
    (``Path("data/pricing.json")``), which breaks when ``decide()`` runs
    from a daemon cwd. Decision computes the path off ``__file__`` so the
    same pricing table is consulted regardless of process cwd.
    """
    return Path(__file__).resolve().parents[2] / "data" / "pricing.json"


@functools.lru_cache(maxsize=1)
def _pricing_table() -> Mapping[str, Mapping[str, float]]:
    """Return the flattened pricing table, loaded at most once per process.

    Cached because every ``cost.recorded`` emit reads it and the table
    contents are static for a process lifetime (refreshed by
    ``scripts/refresh_pricing.py`` between runs). Missing / malformed
    JSON falls through to an empty dict — the per-model lookup then
    returns ``None`` from :func:`compute_cost_usd` and the event still
    emits, just with ``cost_usd=None`` (honest unknown vs. raising).
    """
    return load_pricing_table(_repo_data_pricing_json())


def _emit_cost_recorded(
    ctx: DecideContext,
    chat_result: ChatResult,
    *,
    kind: str,
    turn_id: str | None,
    run_id: str | None = None,
) -> Event:
    """Emit one ``cost.recorded`` event for an L3 LLM turn.

    L3 is the SOLE emit-site per spec §5.4.1 (owner_layer=L3 on the
    ``cost.recorded`` registry entry). Called after every
    ``ctx.llm_client.chat(...)`` site in :mod:`jarvis.decision` so the
    audit trail can sum per-turn spend without trawling provider logs.

    Args:
        ctx: Live :class:`DecideContext` (for the open SQLite conn).
        chat_result: The ChatResult just returned by ``llm_client.chat``.
        kind: ``"decision"`` for the L3 decide() / finalize loop;
            ``"reviewer"`` later (Step 9) for the reviewer LLM; ``"codex"``
            when consuming RawResult.metadata["cost"] from L4.
        turn_id: The active turn correlation (when within a turn).
        run_id: Worker run id (only set when the turn is part of a
            worker run; ``cost.recorded`` carries it as an optional
            correlation field so per-run cost rollups are possible).
    """
    cost_usd = compute_cost_usd(
        chat_result.model_used or None,
        chat_result.tokens_in,
        chat_result.tokens_out,
        chat_result.cache_read_in,
        chat_result.cache_write_in,
        dict(_pricing_table()),
    )
    payload: dict[str, Any] = {
        "kind": kind,
        "model": chat_result.model_used,
        "tokens_in": chat_result.tokens_in,
        "tokens_out": chat_result.tokens_out,
        "cache_read_in": chat_result.cache_read_in,
        "cache_write_in": chat_result.cache_write_in,
        "cost_usd": cost_usd,
    }
    if run_id is not None:
        payload["run_id"] = run_id
    correlation: dict[str, str] = {}
    if turn_id is not None:
        correlation["turn_id"] = turn_id
    if run_id is not None:
        correlation["run_id"] = run_id
    return emit_event(
        ctx.conn,
        type="cost.recorded",
        payload=payload,
        correlation=correlation or None,
    )


def _emit_cost_recorded_from_metadata(
    ctx: DecideContext,
    raw_result: RawResult,
    *,
    turn_id: str | None,
) -> Event | None:
    """Emit ``cost.recorded`` from L4-returned RawResult.metadata["cost"].

    Per ADR-0002 § RawResult.metadata extension: L4's spawn_worker
    handler populates ``raw_result.metadata["cost"]`` from Codex's
    ``turn/completed`` payload. L3 is the sole emit-site, so this
    function reads the side-channel and emits the event from L3.
    Returns the emitted Event, or ``None`` when no cost metadata is
    present (the common case until Step 10 wires spawn_worker for real).
    """
    metadata = raw_result.metadata
    if metadata is None:
        return None
    cost = metadata.get("cost")
    if not isinstance(cost, Mapping):
        return None
    model = cost.get("model")
    if not isinstance(model, str) or not model:
        # No model → cost.recorded would fail the required-field check.
        return None
    tokens_in = int(cost.get("tokens_in", 0) or 0)
    tokens_out = int(cost.get("tokens_out", 0) or 0)
    cache_read_in = int(cost.get("cache_read_in", 0) or 0)
    cache_write_in = int(cost.get("cache_write_in", 0) or 0)
    cost_usd = compute_cost_usd(
        model,
        tokens_in,
        tokens_out,
        cache_read_in,
        cache_write_in,
        dict(_pricing_table()),
    )
    kind_raw = cost.get("kind", "codex")
    kind = kind_raw if isinstance(kind_raw, str) and kind_raw else "codex"
    run_id_raw = cost.get("run_id")
    run_id = run_id_raw if isinstance(run_id_raw, str) else None
    payload: dict[str, Any] = {
        "kind": kind,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cache_read_in": cache_read_in,
        "cache_write_in": cache_write_in,
        "cost_usd": cost_usd,
    }
    if run_id is not None:
        payload["run_id"] = run_id
    correlation: dict[str, str] = {}
    if turn_id is not None:
        correlation["turn_id"] = turn_id
    if run_id is not None:
        correlation["run_id"] = run_id
    return emit_event(
        ctx.conn,
        type="cost.recorded",
        payload=payload,
        correlation=correlation or None,
    )


# --- Public Protocols (avoid sibling-layer imports) ------------------------


class RuntimePathsLike(Protocol):
    """Structural view of ``jarvis.deployment.RuntimePaths`` (Step 6 mirror).

    L3 must not import L6. The composition root (Step 10) passes a
    real ``RuntimePaths`` that satisfies this Protocol structurally.
    """

    @property
    def event_log(self) -> Path:
        """Path to the SQLite Event Log file."""
        ...

    def artifact_dir_for_run(self, run_id: str) -> Path:
        """Return (and create) the per-run artifact directory."""
        ...


class ToolDefinitionLike(Protocol):
    """Structural view of L4 ``ToolDefinition`` records.

    Used by ``decide()`` when assembling the LLM tool list and when
    deciding whether a tool is async (no immediate Result Interpreter
    invocation).
    """

    @property
    def name(self) -> str:
        """Tool name (matches ``ActionRequest.tool_name``)."""
        ...

    @property
    def description(self) -> str:
        """Tool description (LLM-facing)."""
        ...

    @property
    def is_async(self) -> bool:
        """True if the handler schedules a follow-up event."""
        ...

    @property
    def risk_level(self) -> RiskLevel:
        """Risk level for the Pre-action Gate."""
        ...

    @property
    def input_schema(self) -> Mapping[str, Any]:
        """JSON-schema for the tool's arguments."""
        ...


class ToolRegistryLike(Protocol):
    """Structural view of L4 ``ToolRegistry``.

    L3 calls ``for_caller`` (for tool list assembly) and ``dispatch``
    (for executing a gated ActionRequest). The composition root binds
    the real registry; tests build minimal duck-typed substitutes.
    """

    def for_caller(self, caller_principal: CallerPrincipal) -> tuple[ToolDefinitionLike, ...]:
        """Return tools this caller may dispatch."""
        ...

    def dispatch(
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        lifecycle: LifecycleLike,
    ) -> RawResultBundle:
        """Dispatch one ActionRequest and return its RawResultBundle.

        Day-2 § RawResultBundle contract: the L4 dispatcher uniformly
        returns ``RawResultBundle``. Bare ``RawResult`` returns from
        single-slot handlers are wrapped at the L4 boundary; multi-slot
        tools (``verify_diff``) return a bundle directly.
        """
        ...


class LifecycleLike(Protocol):
    """Structural view of L4 ``ActionLifecycle``.

    ``decide()`` registers + transitions per ADR § Canonical event
    trace. The composition root binds the real lifecycle FSM.
    """

    def register(self, action_id: str) -> None:
        """Register a new action at initial state ``proposed``."""
        ...

    def transition(self, action_id: str, new_state: str) -> None:
        """Move ``action_id`` to ``new_state``."""
        ...

    def state_of(self, action_id: str) -> str | None:
        """Return the current state for ``action_id``."""
        ...

    def is_terminal(self, action_id: str) -> bool:
        """True iff the action is in one of the terminal states."""
        ...


# --- Public dataclasses (frozen) -------------------------------------------


@dataclass(frozen=True)
class DecideContext:
    """Inputs to :func:`decide`.

    Frozen so ``decide()`` cannot accidentally mutate context state.

    Attributes:
        conn: Open Event Log connection.
        runtime_paths: Provisioned runtime layout (passed to L4
            handlers).
        tool_registry: L4 ToolRegistry.
        lifecycle: L4 ActionLifecycle (per-process FSM).
        llm_client: L3 LLMClient (Step 8).
        system_prompt: Rendered system prompt string (loaded from
            ``prompts/jarvis_v1.md`` by the caller).
        max_tool_iterations: Safety bound on the tool-use loop.
    """

    conn: sqlite3.Connection
    runtime_paths: RuntimePathsLike
    tool_registry: ToolRegistryLike
    lifecycle: LifecycleLike
    llm_client: LLMClient
    system_prompt: str
    max_tool_iterations: int = _DEFAULT_MAX_TOOL_ITERATIONS


@dataclass(frozen=True)
class DecideResult:
    """Output of one :func:`decide` call.

    Attributes:
        response_plan: Final ResponsePlan when this invocation
            produced user-facing text (utterance / sync-result
            branches); ``None`` for invocations that only emitted
            internal events (e.g. the spawn_worker dispatch leg of
            the utterance branch).
        events_emitted: Frozen tuple of every Event ``decide()``
            emitted during this invocation (turn / entity / action /
            gate / claim / evidence / task). Surface adapters and
            tests inspect this.
        turn_id: Turn correlation for this invocation (when
            applicable).
        attention_channel: Output of
            :func:`jarvis.decision.gates.attention_policy` for the
            packet that drove this invocation.
    """

    response_plan: ResponsePlan | None
    events_emitted: tuple[Event, ...]
    turn_id: str | None
    attention_channel: AttentionChannel = "queue_review"


# --- Internal scratch state for one decide() invocation --------------------


@dataclass
class _Scratch:
    """Per-call mutable accumulator used to keep ``decide()`` readable.

    Not exported. Each ``decide()`` invocation builds a fresh one and
    returns its contents wrapped in a frozen :class:`DecideResult`.
    """

    events: list[Event] = field(default_factory=list)
    turn_id: str | None = None
    # active_subject_ref tracks the canonical task_id (or other entity
    # id) currently in play. Set by the Resolver after entity.resolved.
    active_subject_ref: str | None = None
    # active_action_request lets the worker.reported branch fabricate a
    # synthetic ActionRequest if necessary (the spawn_worker action.id
    # is the link). Day-1 we rely on it being available in the trace.
    last_run_id: str | None = None
    # action_id of the currently in-flight async action (spawn_worker)
    # — used by the worker.reported branch to thread Result Interpreter.
    in_flight_action_id: str | None = None
    in_flight_target_ref: str | None = None
    in_flight_turn_id: str | None = None


# --- decide() entry point ---------------------------------------------------


def decide(trigger: Event, ctx: DecideContext) -> DecideResult:
    """Drive one L3 invocation in response to ``trigger``.

    See the module docstring for the canonical event trace. The
    branches handled Day-1:

    - **``surface.user_intent``**: emit ``turn.started``, run Tier 0
      (returns None Day-1), then drive the Tier 2 LLM tool-use loop.
      Each tool call goes through the Resolver (if it has a task_id
      argument), the Pre-action Gate, dispatch, and either the
      Result Interpreter (sync) or "return None" (async — the Timer
      will re-enter via ``worker.reported``). When the LLM emits
      text, run the Pre-emit Gate; possibly re-prompt once; finalize
      with ``turn.ended``.
    - **``worker.reported``**: emit ``action.result_observed
      (semantics=report)`` referencing the worker.reported event,
      transition the lifecycle ``running -> result_observed``, run
      Result Interpreter to emit a Report Claim + reported Evidence,
      then re-call the LLM so it can plan verification.
    - **``action.result_observed``**: run Result Interpreter on the
      observed semantics, then ask the LLM for a final response,
      then Pre-emit Gate, then ``turn.ended``.

    The orchestration of MULTI-trigger across a single conversation
    turn is owned by Step 10 ``jarvis/runtime``. ``decide()`` is
    one trigger in, one DecideResult out.

    Args:
        trigger: The event that re-entered L3 (already in the log).
        ctx: Frozen :class:`DecideContext`.

    Returns:
        Frozen :class:`DecideResult`.
    """
    scratch = _Scratch()
    packet = assemble_packet(trigger, ctx.conn)
    policy = effective_policy(_allowed_tools_per_caller(ctx.tool_registry))

    if trigger.type == "surface.user_intent":
        return _handle_utterance(packet, policy, ctx, scratch)
    if trigger.type == "worker.reported":
        return _handle_worker_reported(packet, policy, ctx, scratch)
    if trigger.type == "action.result_observed":
        return _handle_result_observed(packet, policy, ctx, scratch)
    if trigger.type in ("action.timeout_assumed", "action.failed"):
        return _handle_action_terminal_failure(packet, policy, ctx, scratch)

    # Unknown trigger: emit nothing, return an empty plan. Stage 2 may
    # widen; Day-1 every trigger we care about is one of the three.
    LOGGER.warning("decide(): unknown trigger type %r — no-op", trigger.type)
    return DecideResult(
        response_plan=None,
        events_emitted=(),
        turn_id=scratch.turn_id,
        attention_channel="silent_log",
    )


# --- surface.user_intent branch --------------------------------------------


def _handle_utterance(
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """Process a ``surface.user_intent`` trigger end-to-end (Day-1)."""
    trigger = packet.trigger_event
    turn_id = packet.current_turn_id or _new_turn_id()
    scratch.turn_id = turn_id

    started_event = emit_event(
        ctx.conn,
        type="turn.started",
        payload={"turn_id": turn_id, "trigger": trigger.event_uid},
        source_event_id=trigger.event_uid,
        correlation={"turn_id": turn_id},
    )
    scratch.events.append(started_event)

    # Tier 0 deterministic shortcut. Day-1 always None.
    tier_0_match(packet)

    # Tier 2 tool-use loop. Each iteration calls the LLM, dispatches any
    # tool_calls (with full Resolver + Pre-action Gate + Result
    # Interpreter), and either continues (if more tool calls) or breaks
    # (if the LLM returned text — which goes to Pre-emit Gate).
    return _run_tool_use_loop(packet, policy, ctx, scratch)


def _run_tool_use_loop(
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """Drive the Tier 2 LLM tool-use loop until text or limit."""
    messages = build_llm_messages(packet)
    # Surface the Task Ledger snapshot as an explicit system note so
    # the LLM can resolve natural references like "昨天那个 task" to
    # the canonical task_id. Without this hint the LLM has no
    # visibility into open tasks and may stall asking Allen for an ID
    # the runtime already owns. Day-1 ADR § Situation Packet expects
    # L3 to render this context for the LLM; this is the minimum
    # surgical surface that delivers it. (Step 12 follow-up.)
    open_tasks_note = _format_open_tasks_note(packet)
    if open_tasks_note is not None:
        messages.insert(0, {"role": "user", "content": open_tasks_note})
    tools = tool_definitions_for_llm(
        [_tool_to_dict(t) for t in ctx.tool_registry.for_caller(CallerPrincipal.JARVIS_LLM)],
    )

    iteration = 0
    while iteration < ctx.max_tool_iterations:
        iteration += 1
        chat_result = ctx.llm_client.chat(
            messages=messages, system=ctx.system_prompt, tools=tools,
        )
        # ADR-0002 Step 3: emit cost.recorded for the L3 decision turn.
        # L3 is the sole emit-site per spec §5.4.1; this is one of two
        # ctx.llm_client.chat(...) sites in this module — see the
        # cost.recorded-per-llm-call canary for the static guard.
        scratch.events.append(
            _emit_cost_recorded(
                ctx, chat_result, kind="decision", turn_id=scratch.turn_id,
            ),
        )

        if chat_result.tool_calls:
            # Append the assistant turn (with tool_calls) so the next
            # iteration sees the LLM's tool requests in history.
            messages.append(_assistant_message_for(chat_result))

            async_pause = False
            for tool_call in chat_result.tool_calls:
                if not _dispatch_one_tool_call(
                    tool_call=tool_call,
                    packet=packet,
                    policy=policy,
                    ctx=ctx,
                    scratch=scratch,
                    messages=messages,
                ):
                    async_pause = True
                    break

            if async_pause:
                # spawn_worker is async; lifecycle stays at running and
                # the Timer will re-enter via worker.reported. Return
                # a "partial" DecideResult — no final response yet.
                attention = attention_policy(packet, packet.task_ledger_snapshot.claim_evidence)
                return DecideResult(
                    response_plan=None,
                    events_emitted=tuple(scratch.events),
                    turn_id=scratch.turn_id,
                    attention_channel=attention,
                )

            # Otherwise (only sync tool results) refresh the packet so
            # the next LLM call sees freshly-emitted claims/evidence.
            packet = assemble_packet(packet.trigger_event, ctx.conn)
            continue

        # LLM returned text -> finalize via Pre-emit Gate.
        return _finalize_response(
            chat_result.text or "",
            packet,
            ctx,
            scratch,
        )

    # Loop bound hit without resolution — finalize with a safe limitation
    # so the surface never sees an unbounded loop in production.
    LOGGER.warning("decide(): tool-use loop hit max_iterations=%d", ctx.max_tool_iterations)
    fallback = "tool-use loop exhausted; turn incomplete."
    return _finalize_response(fallback, packet, ctx, scratch)


def _dispatch_one_tool_call(  # noqa: C901, PLR0912, PLR0913, PLR0915 — single-pass orchestration of resolver (Day-1 + Day-2 time-window) + gate + dispatch + interpreter; splitting muddles the audit trace.
    *,
    tool_call: object,
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
    messages: list[dict[str, Any]],
) -> bool:
    """Resolve, gate, and dispatch one LLM-proposed tool call.

    Returns:
        ``True`` if the tool was sync (the loop should continue with
        the next iteration). ``False`` if the tool was async (the
        caller should pause and return a partial DecideResult).
    """
    name = getattr(tool_call, "name", "")
    call_id = getattr(tool_call, "call_id", "")
    arguments_json = getattr(tool_call, "arguments_json", "{}")
    try:
        arguments: dict[str, Any] = json.loads(arguments_json or "{}")
    except (TypeError, ValueError):
        arguments = {}

    # 1. Resolver. Tools that accept a ``task_id`` go through the
    #    resolver; LLM-supplied ``task_id`` is treated as a natural ref
    #    (per spec §3.3.7: LLM must not invent entity IDs). When the LLM
    #    additionally emits a structured ``{since_ts, until_ts}`` pair
    #    on the action arguments (ADR-0002 Step 5: L3 LLM translates
    #    natural-language time windows like "昨天" into epoch-ms bounds),
    #    we route through ``resolve_task_ref_by_window`` so the resolver
    #    queries the projection via :meth:`TaskLedgerSnapshot.tasks_in_window`
    #    — no direct SQL from L3 per spec §3.4.3.
    target_entity_ref: str | None = None
    natural_ref_raw = arguments.get("task_id") or arguments.get("natural_ref") or ""
    natural_ref = natural_ref_raw if isinstance(natural_ref_raw, str) else ""
    since_ts = _coerce_epoch_ms(arguments.get("since_ts"))
    until_ts = _coerce_epoch_ms(arguments.get("until_ts"))
    resolver_result: ResolverResult | None = None
    if since_ts is not None and until_ts is not None:
        resolver_result = resolve_task_ref_by_window(
            natural_ref,
            packet.task_ledger_snapshot,
            since_ts=since_ts,
            until_ts=until_ts,
        )
        # Window-resolution args are consumed here; do not leak into the
        # L4 tool call (L4 tools don't understand them).
        arguments.pop("since_ts", None)
        arguments.pop("until_ts", None)
    elif natural_ref:
        resolver_result = resolve_task_ref(natural_ref, packet.task_ledger_snapshot)
    if resolver_result is not None:
        scratch.events.append(
            _emit_entity_resolved(
                ctx,
                natural_ref=natural_ref,
                result=resolver_result,
                turn_id=scratch.turn_id,
                source_event_id=packet.trigger_event.event_uid,
            )
        )
        if resolver_result.resolved_to is not None:
            target_entity_ref = resolver_result.resolved_to
            scratch.active_subject_ref = resolver_result.resolved_to
            # Rewrite arguments.task_id to the canonical id so L4 sees
            # the real id, not the natural ref. ``run_id`` arguments
            # are left untouched.
            if "task_id" in arguments:
                arguments["task_id"] = resolver_result.resolved_to

    # 2. Build the ActionRequest. Risk + caller default to
    #    JARVIS_LLM/L2 — the Pre-action Gate verifies via policy.
    tool_def = _find_tool_def(ctx.tool_registry, name)
    if tool_def is None:
        # Unknown tool from LLM. Inject a tool-result message saying so
        # and let the loop continue (the LLM should adapt).
        messages.append(
            _tool_result_message(
                call_id=call_id,
                content=json.dumps({"error": f"unknown tool: {name}"}),
            )
        )
        return True

    # When the tool accepts run_id (verify_diff), prefer the most recent
    # run we spawned this turn. The action.proposed correlation carries
    # run_id forward.
    if "run_id" in arguments and scratch.last_run_id is not None:
        arguments["run_id"] = arguments.get("run_id") or scratch.last_run_id

    # If the tool did not carry a task_id argument (e.g. verify_diff
    # is keyed on run_id), inherit the active subject from scratch so
    # the resulting Postcondition Claim's subject_ref is the canonical
    # task_id rather than the synthetic action_id. Without this, the
    # Pre-emit Gate cannot find the verified evidence for the active
    # subject and task.verified is never emitted. (Step 12 follow-up.)
    if target_entity_ref is None and scratch.active_subject_ref is not None:
        target_entity_ref = scratch.active_subject_ref

    # ADR-0002 Step 12 § Verify_command plumbing (lines 875-913):
    # when L3 proposes the ``verify_diff`` action, lift the task's
    # ``verify_command`` and ``repo_path`` off the Task Ledger
    # projection and stash them on ``ActionRequest.payload`` using the
    # EXACT literal keys ``"verify_command"`` / ``"repo_path"`` so the
    # L4 ``verify_diff_handler`` reads them via
    # ``action_request.payload.get("verify_command")``. The values are
    # passed through verbatim — no transformation. Canary
    # ``test_canary_verify_command_plumbed_to_action_request`` AST-checks
    # both ends.
    action_payload: Mapping[str, Any] | None = None
    if name == "verify_diff" and target_entity_ref is not None:
        task_record = packet.task_ledger_snapshot.get(target_entity_ref)
        if task_record is not None:
            payload_dict: dict[str, Any] = {
                "verify_command": task_record.verify_command,
            }
            if task_record.repo_path is not None:
                payload_dict["repo_path"] = task_record.repo_path
            action_payload = payload_dict

    action_id = _new_action_id()
    action_request = ActionRequest(
        action_id=action_id,
        tool_name=name,
        target_entity_ref=target_entity_ref,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level=tool_def.risk_level,
        arguments=arguments,
        authorization_lease=None,
        run_id=scratch.last_run_id if "run_id" in arguments else None,
        turn_id=scratch.turn_id,
        payload=action_payload,
    )

    # 3. action.proposed
    proposed_event = emit_event(
        ctx.conn,
        type="action.proposed",
        payload={
            "action_id": action_id,
            "tool_name": name,
            "caller_principal": CallerPrincipal.JARVIS_LLM.value,
            "risk_level": tool_def.risk_level,
            "target_entity_ref": target_entity_ref,
            "turn_id": scratch.turn_id,
            "arguments": dict(arguments),
        },
        correlation=_action_correlation(action_request),
    )
    scratch.events.append(proposed_event)

    # 4. Pre-action Gate
    gate = pre_action_gate(action_request, policy, packet.task_ledger_snapshot)
    gate_event = emit_event(
        ctx.conn,
        type="gate.evaluated",
        payload={
            "gate": "pre_action",
            "outcome": gate.outcome,
            "reasons": list(gate.reasons),
            "check_results": dict(gate.check_results),
            "action_id": action_id,
        },
        source_event_id=proposed_event.event_uid,
        correlation=_action_correlation(action_request),
    )
    scratch.events.append(gate_event)

    if gate.outcome != "pass":
        # Refuse / confirm — inject a tool result explaining the refusal
        # and let the LLM adapt. Day-1 scenario should not hit this.
        messages.append(
            _tool_result_message(
                call_id=call_id,
                content=json.dumps(
                    {
                        "error": "gate refused",
                        "outcome": gate.outcome,
                        "reasons": list(gate.reasons),
                    }
                ),
            )
        )
        return True

    # 5. action.authorized + lifecycle register/transition
    authorized_event = emit_event(
        ctx.conn,
        type="action.authorized",
        payload={"action_id": action_id},
        source_event_id=gate_event.event_uid,
        correlation=_action_correlation(action_request),
    )
    scratch.events.append(authorized_event)
    ctx.lifecycle.register(action_id)
    ctx.lifecycle.transition(action_id, "authorized")

    # 6. Dispatch via L4 ToolRegistry (emits action.dispatched +
    #    action.running internally; handler emits run.started for
    #    spawn_worker and action.result_observed for sync tools).
    #    Day-2 § RawResultBundle contract: dispatcher returns a bundle
    #    uniformly; single-slot tools are wrapped at the L4 boundary.
    bundle = ctx.tool_registry.dispatch(
        action_request,
        ctx.conn,
        ctx.runtime_paths,
        ctx.lifecycle,
    )
    primary_slot = bundle.slots[0]

    # 6b. ADR-0002 Step 3: when L4 returns RawResult.metadata["cost"]
    #     (populated by spawn_worker from Codex's turn/completed once
    #     Step 10 wires it), emit cost.recorded(kind="codex") from L3.
    #     L3 is the sole emit-site per spec §5.4.1; L4 only places the
    #     payload on metadata. Cost lives on slot 1 (spawn_worker single
    #     slot or verify_diff observation slot) — multi-slot tools do
    #     not populate cost on the chained verification slot.
    cost_event = _emit_cost_recorded_from_metadata(
        ctx, primary_slot, turn_id=scratch.turn_id,
    )
    if cost_event is not None:
        scratch.events.append(cost_event)

    # 7a. Async tools (spawn_worker): leave lifecycle at running and
    #     pause. ``worker.reported`` will re-enter decide() later.
    if tool_def.is_async:
        scratch.in_flight_action_id = action_id
        scratch.in_flight_target_ref = target_entity_ref
        scratch.in_flight_turn_id = scratch.turn_id
        # Capture the run_id so verify_diff can refer to it.
        run_id_from_raw = primary_slot.payload.get("run_id")
        if isinstance(run_id_from_raw, str):
            scratch.last_run_id = run_id_from_raw
        return False

    # 7b. Sync tools (verify_diff Day-2 dual-slot, create_task single
    #     slot): the handler emitted ``action.result_observed`` itself.
    #     Find that event in the freshly-folded log so we have a
    #     source_event_id for the claim+evidence pair(s) the Result
    #     Interpreter is about to emit. For verify_diff, route through
    #     the Day-2 F2 ladder (Step 12 § Evidence ladder) which calls
    #     :func:`review_diff` once on the captured diff text and emits
    #     per-slot, per-source Claim/Evidence rows per the ADR table.
    #     Other tools continue to use the Day-1 single-slot path.
    result_observed_uid = _latest_event_uid_of_type(
        ctx.conn,
        event_type="action.result_observed",
    )
    source_event_for_interpreter = result_observed_uid or proposed_event.event_uid

    if name == "verify_diff":
        last_interpreted_evidence, saw_verification_for_target = (
            _route_verify_diff_bundle(
                bundle=bundle,
                ctx=ctx,
                scratch=scratch,
                action_request=action_request,
                target_entity_ref=target_entity_ref,
                fallback_source_event_id=source_event_for_interpreter,
            )
        )
    else:
        last_interpreted_evidence = None
        saw_verification_for_target = False
        for slot in bundle.slots:
            interpreted_events = result_interpreter(
                slot,
                source_event_id=source_event_for_interpreter,
                action_request=action_request,
                conn=ctx.conn,
                subject_ref_override=target_entity_ref,
            )
            scratch.events.extend(interpreted_events)
            last_interpreted_evidence = interpreted_events[1]
            if slot.semantics == "verification" and target_entity_ref is not None:
                saw_verification_for_target = True

    # If any slot produced a verified Postcondition for the active task,
    # emit task.verified per ADR § Canonical event trace evt 22. Source
    # the cause-chain off the last evidence event the interpreter wrote.
    # Spec hard rule (ADR-0002 § Evidence ladder): task.verified fires
    # ONLY from the verification slot of verify_diff — every other path
    # leaves the bit cleared.
    if saw_verification_for_target and last_interpreted_evidence is not None:
        task_verified_event = emit_event(
            ctx.conn,
            type="task.verified",
            payload={"task_id": target_entity_ref, "by": "jarvis"},
            source_event_id=last_interpreted_evidence.event_uid,
            correlation=_action_correlation(action_request),
        )
        scratch.events.append(task_verified_event)
        scratch.active_subject_ref = target_entity_ref

    # 8. Append the tool result back into the messages list so the LLM
    #    can see it on the next iteration. Multi-slot returns: render
    #    the bundle as a JSON object so the LLM sees both outputs.
    messages.append(
        _tool_result_message(
            call_id=call_id,
            content=_render_bundle_for_llm(bundle),
        )
    )
    return True


def _route_verify_diff_bundle(  # noqa: PLR0913 - F2 ladder hand-off inputs are all load-bearing per ADR-0002 § Verify_command plumbing.
    *,
    bundle: RawResultBundle,
    ctx: DecideContext,
    scratch: _Scratch,
    action_request: ActionRequest,
    target_entity_ref: str | None,
    fallback_source_event_id: str,
) -> tuple[Event | None, bool]:
    """Drive the F2 ladder for a ``verify_diff`` :class:`RawResultBundle`.

    Per ADR-0002 Step 12 (§ Evidence ladder lines 250-339):

    1. Looks up the ``action.result_observed`` event_uid per slot from
       the freshly-folded log so the F2 ladder rows hang off the right
       cause-chain row.
    2. Calls :func:`review_diff` once on the captured diff text (slot
       1) — wrapped in :meth:`LLMClient.fresh_context` per
       § Reviewer contract (line 738). Skipped when slot 1 has
       ``diff_nonempty == False``.
    3. Emits ``cost.recorded(kind="reviewer", ...)`` from the same
       function body so the per-LLM-call cost canary is satisfied
       (the reviewer module itself does not emit; Step 12 owns the
       caller-side emit per § Reviewer contract line 743+).
    4. Delegates to :func:`interpret_verify_diff_bundle` to emit the
       Claim + Evidence rows per the ladder.

    Returns ``(last_evidence_event, did_verify)`` so the caller emits
    ``task.verified`` only when the verification slot fired.
    """
    if target_entity_ref is None:
        # No canonical subject → fall back to the Day-1 single-slot
        # path; ladder rows need a subject_ref to be useful.
        last_evidence: Event | None = None
        verified = False
        for slot in bundle.slots:
            interpreted_events = result_interpreter(
                slot,
                source_event_id=fallback_source_event_id,
                action_request=action_request,
                conn=ctx.conn,
                subject_ref_override=target_entity_ref,
            )
            scratch.events.extend(interpreted_events)
            last_evidence = interpreted_events[1]
        return last_evidence, verified

    # Per spec §5.4.2 + ADR-0002 § Verify_command plumbing line 905,
    # L3 emits one ``action.result_observed`` per slot. Step 11's
    # handler emits the slot-1 (observation) event itself for backward
    # compat; the slot-2 (verification | error) event is L3's job and
    # lands here. We then collect the freshly-folded event_uids so the
    # F2 ladder rows hang off the right cause-chain row.
    if len(bundle.slots) > 1:
        slot2 = bundle.slots[1]
        scratch.events.append(
            emit_event(
                ctx.conn,
                type="action.result_observed",
                payload={
                    "action_id": action_request.action_id,
                    "semantics": slot2.semantics,
                    "tool_output": slot2.tool_output,
                    "error": slot2.error,
                    "run_id": action_request.run_id,
                },
                source_event_id=fallback_source_event_id,
                correlation=_action_correlation(action_request),
            ),
        )

    source_event_ids_by_semantics = _collect_recent_result_observed_uids(
        ctx.conn,
        action_id=action_request.action_id,
        fallback=fallback_source_event_id,
    )

    observation_slot = bundle.slots[0]
    diff_nonempty = bool(observation_slot.payload.get("diff_nonempty", False))
    diff_text = observation_slot.payload.get("diff_text_preview")
    task_goal = _task_goal_for_subject(ctx, target_entity_ref)

    reviewer_verdict: ReviewerVerdict | None = None
    if diff_nonempty and isinstance(diff_text, str) and diff_text:
        # Step 9 + Step 12: call the reviewer LLM in fresh-context.
        # ``review_diff`` itself wraps the ``.chat()`` call inside the
        # context manager (canary
        # ``test_canary_reviewer_fresh_context`` enforces).
        reviewer_verdict = review_diff(
            task_goal=task_goal,
            diff_text=diff_text,
            llm_client=ctx.llm_client,
        )
        # ADR-0002 § Reviewer contract line 743: the reviewer module
        # returns token counts on :class:`ReviewerVerdict`; this
        # function — the L3 caller — emits the matching
        # ``cost.recorded(kind="reviewer", ...)`` so the
        # per-LLM-call canary is satisfied without putting the emit
        # inside ``reviewer.py``.
        scratch.events.append(
            _emit_cost_recorded_from_verdict(
                ctx,
                verdict=reviewer_verdict,
                turn_id=scratch.turn_id,
                action_id=action_request.action_id,
            ),
        )

    emitted, did_verify = interpret_verify_diff_bundle(
        bundle,
        source_event_ids_by_semantics=source_event_ids_by_semantics,
        action_request=action_request,
        conn=ctx.conn,
        subject_ref=target_entity_ref,
        task_goal=task_goal,
        reviewer_verdict=reviewer_verdict,
    )
    scratch.events.extend(emitted)

    # Find the last evidence.attached event so the caller can hang
    # task.verified off it (cause-chain — Acceptance H10).
    last_evidence = next(
        (e for e in reversed(emitted) if e.type == "evidence.attached"),
        None,
    )
    return last_evidence, did_verify


def _collect_recent_result_observed_uids(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    fallback: str,
) -> dict[str, str]:
    """Return the most recent ``action.result_observed.event_uid`` per slot semantics.

    Step 11's ``verify_diff_handler`` emits one ``action.result_observed``
    per slot (observation + verification | error). Step 12 hangs the
    F2 ladder rows off the right cause-chain row by keying on the
    payload's ``semantics`` field. When no row matches a semantics, the
    caller's ``fallback`` value is returned for that key so the
    interpreter still emits.
    """
    cursor = conn.execute(
        "SELECT event_uid, payload_json "
        "FROM events "
        "WHERE type = 'action.result_observed' "
        "ORDER BY id DESC LIMIT 4",
    )
    by_semantics: dict[str, str] = {}
    for event_uid, payload_json in cursor.fetchall():
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
        if payload.get("action_id") != action_id:
            continue
        semantics = payload.get("semantics")
        if isinstance(semantics, str) and semantics not in by_semantics:
            by_semantics[semantics] = event_uid
    if not by_semantics:
        by_semantics["observation"] = fallback
    return by_semantics


def _task_goal_for_subject(
    ctx: DecideContext,
    subject_ref: str,
) -> str:
    """Return the goal text for ``subject_ref`` from the live Task Ledger."""
    snapshot = make_snapshot(ctx.conn)
    record = snapshot.task_ledger.get(subject_ref)
    if record is not None:
        return record.goal
    # Conservative fallback: the LLM may still produce a usable review
    # against an empty goal (it will just see no goal context).
    return ""


def _emit_cost_recorded_from_verdict(
    ctx: DecideContext,
    *,
    verdict: ReviewerVerdict,
    turn_id: str | None,
    action_id: str,
) -> Event:
    """Emit ``cost.recorded(kind="reviewer", ...)`` from a :class:`ReviewerVerdict`.

    Mirror of :func:`_emit_cost_recorded` but reads the token counts
    off the reviewer verdict (the reviewer module returns these so
    the caller can emit without re-reading the LLM client's last
    ChatResult, which would race with another call).
    """
    cost_usd = compute_cost_usd(
        verdict.model,
        verdict.tokens_in,
        verdict.tokens_out,
        0,
        0,
        dict(_pricing_table()),
    )
    payload: dict[str, Any] = {
        "kind": "reviewer",
        "model": verdict.model,
        "tokens_in": verdict.tokens_in,
        "tokens_out": verdict.tokens_out,
        "cache_read_in": 0,
        "cache_write_in": 0,
        "cost_usd": cost_usd,
    }
    correlation: dict[str, str] = {"action_id": action_id}
    if turn_id is not None:
        correlation["turn_id"] = turn_id
    return emit_event(
        ctx.conn,
        type="cost.recorded",
        payload=payload,
        correlation=correlation,
    )


def _render_bundle_for_llm(bundle: RawResultBundle) -> str:
    """Serialize a :class:`RawResultBundle` for the LLM tool-result message.

    Single-slot bundles render exactly as the Day-1 ``raw_result.tool_output``
    (or a JSON projection of ``raw_result.payload`` when ``tool_output``
    is None) so existing prompt expectations stay stable. Multi-slot
    bundles (Day-2 ``verify_diff`` with a chained ``verify_command``)
    render as a JSON object keyed by slot semantics — the LLM sees both
    the observation diff preview AND the verify_command exit code in
    one tool-result, which Step 12's prompt asset will explicitly call
    out.
    """
    if len(bundle.slots) == 1:
        slot = bundle.slots[0]
        return slot.tool_output or json.dumps(dict(slot.payload))
    rendered: dict[str, Any] = {}
    for slot in bundle.slots:
        rendered[slot.semantics] = (
            json.loads(slot.tool_output)
            if slot.tool_output
            else dict(slot.payload)
        )
    return json.dumps(rendered, ensure_ascii=False)


# --- worker.reported branch ------------------------------------------------


def _handle_worker_reported(
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """Process a ``worker.reported`` re-entry (async lifecycle).

    Per ADR § Gate contracts (Result Interpreter):

    1. Emit ``action.result_observed(semantics=report)`` referencing
       the worker.reported event_uid (since L4's async spawn_worker
       did not — the canonical trace evt 11 is L3's responsibility).
    2. Transition the lifecycle ``running -> result_observed``.
    3. Result Interpreter: emit Report Claim + reported Evidence.
    4. Call the LLM with an "[system trigger] worker.reported" prompt
       so it can plan verification.
    5. Continue via the standard tool-use loop.
    """
    trigger = packet.trigger_event
    action_id = trigger.payload.get("action_id")
    run_id = trigger.payload.get("run_id")
    turn_id_from_corr = (
        packet.current_turn_id
        or (trigger.correlation.get("turn_id") if trigger.correlation is not None else None)
    )
    scratch.turn_id = turn_id_from_corr if isinstance(turn_id_from_corr, str) else None
    if isinstance(run_id, str):
        scratch.last_run_id = run_id

    if not isinstance(action_id, str):
        LOGGER.warning("worker.reported missing action_id payload — no-op")
        return DecideResult(
            response_plan=None,
            events_emitted=tuple(scratch.events),
            turn_id=scratch.turn_id,
            attention_channel="silent_log",
        )

    # Active subject = the task_id the trigger correlation carries.
    task_id_corr = None
    if trigger.correlation is not None:
        task_id_corr = trigger.correlation.get("task_id")
    if isinstance(task_id_corr, str):
        scratch.active_subject_ref = task_id_corr

    # 1. action.result_observed referencing the worker.reported.
    result_observed_event = emit_event(
        ctx.conn,
        type="action.result_observed",
        payload={
            "action_id": action_id,
            "semantics": "report",
            "tool_output": trigger.payload.get("summary"),
            "run_id": run_id,
        },
        source_event_id=trigger.event_uid,
        correlation={
            "action_id": action_id,
            **({"run_id": run_id} if isinstance(run_id, str) else {}),
            **({"turn_id": scratch.turn_id} if scratch.turn_id else {}),
        },
    )
    scratch.events.append(result_observed_event)

    # 2. Lifecycle running -> result_observed.
    current = ctx.lifecycle.state_of(action_id)
    if current == "running":
        ctx.lifecycle.transition(action_id, "result_observed")

    # 3. Result Interpreter — Report claim + reported evidence.
    synthetic_request = ActionRequest(
        action_id=action_id,
        tool_name="spawn_worker",
        target_entity_ref=scratch.active_subject_ref,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L2",
        arguments={},
        authorization_lease=None,
        run_id=run_id if isinstance(run_id, str) else None,
        turn_id=scratch.turn_id,
    )
    synthetic_raw = _synthesize_raw_for_worker_report(
        action_id=action_id,
        artifact_path=trigger.payload.get("artifact_path"),
        summary=trigger.payload.get("summary"),
    )
    interpreted = result_interpreter(
        synthetic_raw,
        source_event_id=result_observed_event.event_uid,
        action_request=synthetic_request,
        conn=ctx.conn,
        subject_ref_override=scratch.active_subject_ref,
    )
    scratch.events.extend(interpreted)

    # 4 + 5. Re-call the LLM to plan verification + continue loop.
    return _run_tool_use_loop(
        assemble_packet(trigger, ctx.conn),
        policy,
        ctx,
        scratch,
    )


def _synthesize_raw_for_worker_report(
    *,
    action_id: str,
    artifact_path: object,
    summary: object,
) -> RawResult:
    """Build a synthetic RawResult for the worker.reported trigger.

    The worker.reported event is not a tool's ``RawResult`` — it is an
    asynchronous report from L4's Timer thread. L3 fabricates a real
    ``RawResult`` so the Result Interpreter sees a uniform shape. Step 0b
    of ADR-0002 collapsed the prior ``_SyntheticRawResult`` helper into a
    direct ``RawResult`` construction now that the type lives in
    ``jarvis.shared``.
    """
    payload: dict[str, Any] = {}
    if isinstance(artifact_path, str):
        payload["artifact_path"] = artifact_path
    return RawResult(
        action_id=action_id,
        semantics="report",
        payload=payload,
        tool_output=str(summary) if summary is not None else None,
        error=None,
    )


# --- action.result_observed branch -----------------------------------------


def _handle_result_observed(
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """Process a synchronous ``action.result_observed`` re-entry.

    Day-1 this branch is only used when the runtime feeds a freshly
    appended ``action.result_observed`` back into ``decide()`` outside
    of the tool-use loop. The happy path produces verified evidence
    via the inline loop in ``surface.user_intent``; this branch covers
    Stage 2 scenarios where the surface drives multi-step planning
    asynchronously.
    """
    trigger = packet.trigger_event
    semantics = trigger.payload.get("semantics", "ack")
    action_id = trigger.payload.get("action_id")
    if not isinstance(action_id, str):
        LOGGER.warning("action.result_observed missing action_id — no-op")
        return DecideResult(
            response_plan=None,
            events_emitted=tuple(scratch.events),
            turn_id=scratch.turn_id,
            attention_channel="silent_log",
        )

    # Fabricate a synthetic ActionRequest + RawResult so the Result
    # Interpreter can fold this into the claim/evidence stream.
    payload_run_id = trigger.payload.get("run_id")
    synthetic_request = ActionRequest(
        action_id=action_id,
        tool_name=str(trigger.payload.get("tool_name", "unknown")),
        target_entity_ref=scratch.active_subject_ref,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L0",
        arguments={},
        authorization_lease=None,
        run_id=payload_run_id if isinstance(payload_run_id, str) else None,
        turn_id=scratch.turn_id,
    )
    synthetic_raw = RawResult(
        action_id=action_id,
        semantics=semantics,
        payload=dict(trigger.payload),
        tool_output=trigger.payload.get("tool_output"),
        error=trigger.payload.get("error"),
    )

    interpreted = result_interpreter(
        synthetic_raw,
        source_event_id=trigger.event_uid,
        action_request=synthetic_request,
        conn=ctx.conn,
        subject_ref_override=scratch.active_subject_ref,
    )
    scratch.events.extend(interpreted)

    # Ask the LLM to compose a final response now that fresh evidence
    # is on the trace.
    return _run_tool_use_loop(
        assemble_packet(trigger, ctx.conn),
        policy,
        ctx,
        scratch,
    )


# --- action.timeout_assumed / action.failed branch -------------------------

# Canonical user-facing limitation phrasings for the spawn_worker
# terminal-failure paths (B-0003c / ADR-0002 Negative-path appendix
# lines 1593-1600). Both strings are matched by their respective
# patterns in ``jarvis.decision.pre_emit_phrases.LIMITATION_REGEXES``
# (``r"超时.{0,4}未完成"`` / ``r"跑挂"``); the ``未完成`` substring is
# allowed past ``_COMPLETION_KEYWORDS`` by the ``(?<![未没不])``
# negative lookbehind, so the gate's attempt-0 verdict is
# ``force_limitation_language`` with ``downgrade_required=False`` —
# no LLM retry round-trip.
_TIMEOUT_LIMITATION_TEXT: Final[str] = "Codex 超时，未完成"  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.
_FAILED_LIMITATION_TEXT: Final[str] = "Codex 跑挂了，没新 diff"  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.


def _handle_action_terminal_failure(
    packet: SituationPacket,
    policy: EffectivePolicy,  # noqa: ARG001 — kept for branch-signature uniformity with the other _handle_* dispatchers.
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """Process an ``action.timeout_assumed`` / ``action.failed`` re-entry.

    Per B-0003c + ADR-0002 Negative-path appendix:

    1. Recover correlation (``turn_id``, ``run_id``, ``task_id``,
       ``action_id``) from the trigger correlation/payload. Look up
       the spawning ``action.proposed`` event to recover the original
       ``tool_name``; default to ``"spawn_worker"`` when missing.
    2. Synthesize a :class:`RawResult` with ``semantics="error"`` and
       a :class:`ActionRequest` whose ``tool_name`` matches the
       proposed event. Feed both to
       :func:`jarvis.decision.result_interpreter.result_interpreter`,
       which emits ``claim.created(type=Limitation)`` +
       ``evidence.attached(relation=limits, level=reported)`` per the
       ``_SEMANTICS_TO_CLAIM["error"]`` row.
    3. Pick the canonical user-facing limitation text for the trigger
       type and feed it to :func:`_finalize_response`, which runs the
       Pre-emit Gate and emits ``turn.ended``.

    The canonical limitation strings (``"Codex 超时,未完成"`` /
    ``"Codex 跑挂了,没新 diff"``) are matched by the corresponding
    patterns in :mod:`jarvis.decision.pre_emit_phrases.LIMITATION_REGEXES`
    so the gate's attempt-0 verdict is force_limitation_language with
    ``downgrade_required=False`` — no LLM retry round-trip.
    """
    trigger = packet.trigger_event
    action_id = trigger.payload.get("action_id")
    error = trigger.payload.get("error")
    reason = trigger.payload.get("reason")

    correlation = trigger.correlation or {}
    turn_id_corr = correlation.get("turn_id") or packet.current_turn_id
    run_id_corr = correlation.get("run_id")
    task_id_corr = correlation.get("task_id")

    scratch.turn_id = turn_id_corr if isinstance(turn_id_corr, str) else None
    if isinstance(run_id_corr, str):
        scratch.last_run_id = run_id_corr
    if isinstance(task_id_corr, str):
        scratch.active_subject_ref = task_id_corr

    # Look up the original spawning action.proposed event so the
    # synthetic ActionRequest carries the same tool_name (typically
    # "spawn_worker"). Falling back to "spawn_worker" keeps the
    # handler degradation-safe when the proposed event was emitted in
    # a previous process or the action_id is otherwise unrecoverable.
    tool_name = _tool_name_for_action_id(
        ctx.conn,
        action_id if isinstance(action_id, str) else None,
    )

    if not isinstance(action_id, str):
        LOGGER.warning(
            "%s missing action_id payload — emitting limitation without "
            "claim/evidence",
            trigger.type,
        )
    else:
        synthetic_request = ActionRequest(
            action_id=action_id,
            tool_name=tool_name,
            target_entity_ref=scratch.active_subject_ref,
            caller_principal=CallerPrincipal.JARVIS_LLM,
            risk_level="L2",
            arguments={},
            authorization_lease=None,
            run_id=run_id_corr if isinstance(run_id_corr, str) else None,
            turn_id=scratch.turn_id,
        )
        synthetic_raw = RawResult(
            action_id=action_id,
            semantics="error",
            payload={
                "action_id": action_id,
                "error": error,
                "reason": reason,
            },
            tool_output=None,
            error=error if isinstance(error, str) else None,
        )
        interpreted = result_interpreter(
            synthetic_raw,
            source_event_id=trigger.event_uid,
            action_request=synthetic_request,
            conn=ctx.conn,
            subject_ref_override=scratch.active_subject_ref,
        )
        scratch.events.extend(interpreted)

    canonical_text = (
        _TIMEOUT_LIMITATION_TEXT
        if trigger.type == "action.timeout_assumed"
        else _FAILED_LIMITATION_TEXT
    )

    return _finalize_response(canonical_text, packet, ctx, scratch)


def _tool_name_for_action_id(
    conn: sqlite3.Connection,
    action_id: str | None,
) -> str:
    """Look up the ``tool_name`` from the spawning ``action.proposed`` row.

    Returns the tool name on the action.proposed event keyed by
    ``payload.action_id == action_id``. Falls back to
    ``"spawn_worker"`` when no proposed event is found — the
    handler's degradation path per B-0003c.
    """
    if action_id is None:
        return "spawn_worker"
    cursor = conn.execute(
        "SELECT payload_json FROM events WHERE type = ? "
        "ORDER BY id DESC",
        ("action.proposed",),
    )
    for row in cursor:
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            continue
        if payload.get("action_id") == action_id:
            tool = payload.get("tool_name")
            if isinstance(tool, str) and tool:
                return tool
            break
    return "spawn_worker"


# --- Finalization (Pre-emit Gate + turn.ended) -----------------------------


def _emit_pre_emit_gate_event(
    ctx: DecideContext,
    scratch: _Scratch,
    *,
    plan: ResponsePlan,
    attempt: int,
) -> Event:
    """Emit one ``gate.evaluated(pre_emit)`` event for a Pre-emit verdict.

    ``attempt`` is the retry index — 0 for the initial draft, 1 for the
    LLM retry, 2 for the forced template. Every Pre-emit Gate verdict
    along the retry chain gets its own event so the audit trail can
    reconstruct the full path, not just the final ResponsePlan
    (spec § Invariant 1: state flows through events).
    """
    gate_event = emit_event(
        ctx.conn,
        type="gate.evaluated",
        payload={
            "gate": "pre_emit",
            "outcome": plan.permission,
            "reasons": list(_pre_emit_reasons(plan)),
            "response_hash": plan.response_hash,
            "claim_levels": list(plan.active_claim_levels),
            "attempt": attempt,
        },
        correlation={"turn_id": scratch.turn_id} if scratch.turn_id else None,
    )
    scratch.events.append(gate_event)
    return gate_event


def _finalize_response(
    draft_text: str,
    packet: SituationPacket,
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """Apply the Pre-emit Gate to a draft, emit gate + turn.ended, return.

    Implements the one-retry rule per ADR § Gate contracts. Each
    ``pre_emit_gate(...)`` call emits its own ``gate.evaluated`` event
    with a distinct ``attempt`` index (0 / 1 / 2) so the audit trail
    captures every verdict, not just the final one:

    - attempt 0 — initial verdict on the LLM draft. If
      ``downgrade_required`` is False, ship that plan.
    - attempt 1 — re-prompt the LLM once with a limitation-language
      system note; gate the retry text.
    - attempt 2 — if the retry still trips the gate, rewrite the draft
      via ``_FORCED_LIMITATION_TEMPLATE`` (with completion-keyword
      scrub on the embedded text) and gate that. If even this trips,
      fall back to a fixed :func:`_hard_refusal_plan` that is
      scrub-safe by construction (no extra gate event — it is a
      deterministic bailout, not a gate verdict).

    ``turn.ended.source_event_id`` references the LAST gate event in
    the chain regardless of which branch was taken.
    """
    hard_refusal_used = False
    active_subject = scratch.active_subject_ref
    if active_subject is None and packet.open_tasks:
        active_subject = packet.open_tasks[0].task_id
    if active_subject is None:
        # No scratch.active_subject_ref AND no open tasks — the gate
        # will see an empty claim set and force_limitation_language
        # by construction. Demoted to debug: the hard-refusal text now
        # carries a user-facing "找不到对应的 task" branch (F1), so the
        # failure mode reaches the operator via the surface rather than
        # via stderr noise.
        LOGGER.debug(
            "_finalize_response: no active_subject_ref and no open tasks; "
            "falling back to 'unknown_subject' (turn_id=%r). The Pre-emit "
            "Gate will force limitation framing.",
            scratch.turn_id,
        )
        active_subject = "unknown_subject"

    # Always refresh the projection so the gate sees the latest
    # claim/evidence rows.
    projections = make_snapshot(ctx.conn)

    # Attempt 0 — initial verdict on the raw LLM draft.
    plan = pre_emit_gate(draft_text, projections.claim_evidence, active_subject)
    last_gate_event = _emit_pre_emit_gate_event(ctx, scratch, plan=plan, attempt=0)

    if plan.downgrade_required:
        # Attempt 1 — single LLM retry. Append a system-style
        # instruction and re-run the LLM ONCE; do not pull tools this
        # time — we want text.
        retry_messages: list[dict[str, Any]] = [
            {"role": "user", "content": packet.trigger_event.payload.get("transcript", "")},
            _assistant_text_message(draft_text),
            {
                "role": "user",
                "content": (
                    "[system note] Pre-emit Gate refused: the active task has no "
                    "verified Postcondition evidence. Rewrite your response using "
                    "limitation language (e.g. 'agent reported, not verified' / "
                    "'未验证')."
                ),
            },
        ]
        retry_result = ctx.llm_client.chat(
            messages=retry_messages,
            system=ctx.system_prompt,
            tools=None,
        )
        # ADR-0002 Step 3: emit cost.recorded for the Pre-emit retry
        # LLM turn (the second of two L3 chat() sites in this module).
        scratch.events.append(
            _emit_cost_recorded(
                ctx, retry_result, kind="decision", turn_id=scratch.turn_id,
            ),
        )
        retry_text = retry_result.text or ""
        retry_plan = pre_emit_gate(retry_text, projections.claim_evidence, active_subject)
        last_gate_event = _emit_pre_emit_gate_event(
            ctx, scratch, plan=retry_plan, attempt=1,
        )
        if not retry_plan.downgrade_required:
            plan = retry_plan
        else:
            # Attempt 2 — forced template. Defense in depth:
            #   (a) scrub completion keywords from the LLM draft so the
            #       embedded text can't carry bare completion claims to
            #       the surface.
            #   (b) if the scrubbed-and-templated text STILL trips the
            #       gate (e.g. a completion synonym the scrub regex
            #       doesn't cover), fall back to a fixed
            #       _hard_refusal_plan that is scrub-safe by
            #       construction.
            forced = _FORCED_LIMITATION_TEMPLATE.format(
                draft=_scrub_completion_keywords(retry_text or draft_text),
            )
            forced_plan = pre_emit_gate(forced, projections.claim_evidence, active_subject)
            last_gate_event = _emit_pre_emit_gate_event(
                ctx, scratch, plan=forced_plan, attempt=2,
            )
            if forced_plan.downgrade_required:
                # Thread the gate's last computed claim_levels through
                # so the hard-refusal plan still reports the subject's
                # actual evidence state (typically empty / reported
                # only — that's WHY the gate kept refusing).
                plan = _hard_refusal_plan(
                    active_subject,
                    active_claim_levels=forced_plan.active_claim_levels,
                )
                hard_refusal_used = True
            else:
                plan = forced_plan

    # turn.ended. ``source_event_id`` references the last gate verdict
    # on the chain (attempt 0 / 1 / 2 depending on how far retry went).
    if scratch.turn_id is not None:
        ended_event = emit_event(
            ctx.conn,
            type="turn.ended",
            payload={"turn_id": scratch.turn_id, "final_response_hash": plan.response_hash},
            source_event_id=last_gate_event.event_uid,
            correlation={"turn_id": scratch.turn_id},
        )
        scratch.events.append(ended_event)

    attention = attention_policy(packet, projections.claim_evidence)
    # The attention_policy verdict reflects evidence state at the trigger
    # event (worker.reported + no verified Postcondition → silent_log per
    # ``test_attention_silent_log_on_worker_reported_without_verified``).
    # When the Pre-emit Gate retry chain exhausted to _hard_refusal_plan,
    # the fixed limitation text IS the user-facing surface — swallowing it
    # to silent_log strands the operator after a long wait. Promote to
    # queue_review so cli_stdout fires; the message itself still uses
    # limitation language so the "审核了再告诉我" spirit holds.
    if hard_refusal_used and attention == "silent_log":
        attention = "queue_review"

    return DecideResult(
        response_plan=plan,
        events_emitted=tuple(scratch.events),
        turn_id=scratch.turn_id,
        attention_channel=attention,
    )


def _pre_emit_reasons(plan: ResponsePlan) -> tuple[str, ...]:
    """Render Pre-emit Gate plan into ``reasons`` strings for audit."""
    return (
        f"permission={plan.permission}",
        f"downgrade_required={plan.downgrade_required}",
        f"active_claim_levels={list(plan.active_claim_levels)}",
    )


# --- Helpers ----------------------------------------------------------------


def _coerce_epoch_ms(value: object) -> int | None:
    """Return ``value`` as int when it looks like an epoch-ms; else None.

    The LLM emits ``since_ts`` / ``until_ts`` as part of the tool-call
    arguments JSON (ADR-0002 Step 5 § Time-window resolver). JSON has no
    integer/float distinction, so accept both and coerce. Anything that
    is not a finite numeric value is treated as missing (the resolver
    then falls back to the natural-ref path).
    """
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly.
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def _format_open_tasks_note(packet: SituationPacket) -> str | None:
    """Render the open-task snapshot as a system note string, or None.

    Returns None when there are no open tasks (no signal to give the
    LLM). Otherwise a short bullet list of ``task_id: goal`` entries
    plus a directive instructing the LLM to use the matching task_id
    when the user references a task by natural language. This is the
    Day-1 minimum that lets the LLM consume the Task Ledger snapshot
    without requiring a full Situation Packet rendering.
    """
    if not packet.open_tasks:
        return None
    bullets = "\n".join(
        f"- task_id={record.task_id!r}, goal={record.goal!r}"
        for record in packet.open_tasks
    )
    return (
        "[system context] Current open tasks (Task Ledger snapshot):\n"
        f"{bullets}\n"
        "When the user references a task by natural language (e.g. "
        "'昨天那个 task'), pass the matching `task_id` from this list "
        "to any tool that needs one. Do not invent task_ids. When the "
        "user references a task by a time window (e.g. 'yesterday'), you "
        "may instead pass `since_ts` and `until_ts` (epoch milliseconds) "
        "as extra arguments — the resolver runs a time-window query "
        "against the Task Ledger. Yesterday = [now - 86400000, now]."
    )


def _new_turn_id() -> str:
    """Fresh turn_id (T + 8-hex)."""
    return "T" + uuid.uuid4().hex[:8]


def _new_action_id() -> str:
    """Fresh action_id (A + 8-hex)."""
    return "A" + uuid.uuid4().hex[:8]


def _action_correlation(action_request: ActionRequest) -> Mapping[str, str]:
    """Build the canonical ``{action_id, run_id?, turn_id?}`` correlation."""
    out: dict[str, str] = {"action_id": action_request.action_id}
    if action_request.run_id is not None:
        out["run_id"] = action_request.run_id
    if action_request.turn_id is not None:
        out["turn_id"] = action_request.turn_id
    return out


def _emit_entity_resolved(
    ctx: DecideContext,
    *,
    natural_ref: str,
    result: ResolverResult,
    turn_id: str | None,
    source_event_id: str,
) -> Event:
    """Emit the ``entity.resolved`` event per ADR § Resolver contract."""
    outcome = _resolver_outcome(result)
    payload: dict[str, Any] = {
        "entity_type": "task",
        "natural_ref": natural_ref,
        "resolved_to": result.resolved_to,
        "confidence": result.confidence,
        "candidates": list(result.candidates),
        "match_basis": result.match_basis,
        "outcome": outcome,
    }
    if result.confidence == "fuzzy":
        payload["resolver_warning"] = True
    return emit_event(
        ctx.conn,
        type="entity.resolved",
        payload=payload,
        source_event_id=source_event_id,
        correlation={"turn_id": turn_id} if turn_id else None,
    )


def _resolver_outcome(result: ResolverResult) -> str:
    """Map ResolverResult.confidence -> entity.resolved.outcome.

    Per ADR § Resolver contract table.
    """
    if result.confidence in ("exact", "high"):
        return "resolved"
    if result.confidence == "fuzzy":
        if result.resolved_to is not None:
            return "resolved"
        return "ambiguous"
    return "failed"


def _find_tool_def(
    registry: ToolRegistryLike,
    name: str,
) -> ToolDefinitionLike | None:
    """Locate a tool definition by name (JARVIS_LLM-scoped surface)."""
    for tool_def in registry.for_caller(CallerPrincipal.JARVIS_LLM):
        if tool_def.name == name:
            return tool_def
    return None


def _allowed_tools_per_caller(
    registry: ToolRegistryLike,
) -> Mapping[CallerPrincipal, frozenset[str]]:
    """Derive the policy's tool-surface from the registry, per caller."""
    surface: dict[CallerPrincipal, frozenset[str]] = {}
    for principal in CallerPrincipal:
        surface[principal] = frozenset(t.name for t in registry.for_caller(principal))
    return surface


def _tool_to_dict(tool_def: ToolDefinitionLike) -> dict[str, Any]:
    """Project a ToolDefinitionLike into a dict for ``intent.build_messages``."""
    return {
        "name": tool_def.name,
        "description": tool_def.description,
        "input_schema": dict(tool_def.input_schema),
    }


def _assistant_message_for(chat_result: object) -> dict[str, Any]:
    """Build an assistant-role message echoing the LLM's tool_calls.

    OpenAI's API requires the assistant turn that emitted tool_calls to
    appear in history before the matching tool results. We synthesize
    a minimal message that captures the tool_call IDs / names /
    arguments; the LLM only reads its own ``tool_call`` shape, so the
    fidelity needed is just the call_id binding.
    """
    tool_calls = getattr(chat_result, "tool_calls", ())
    return {
        "role": "assistant",
        "content": getattr(chat_result, "text", None) or "",
        "tool_calls": [
            {
                "id": getattr(tc, "call_id", ""),
                "type": "function",
                "function": {
                    "name": getattr(tc, "name", ""),
                    "arguments": getattr(tc, "arguments_json", "{}"),
                },
            }
            for tc in tool_calls
        ],
    }


def _assistant_text_message(text: str) -> dict[str, Any]:
    """Build an assistant text turn for retry context."""
    return {"role": "assistant", "content": text}


def _tool_result_message(*, call_id: str, content: str) -> dict[str, Any]:
    """Build a tool-result message (OpenAI ``role=tool``)."""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
    }


def _latest_event_uid_of_type(
    conn: sqlite3.Connection,
    *,
    event_type: str,
) -> str | None:
    """Return the most recent ``event_uid`` of ``event_type``, or None."""
    cursor = conn.execute(
        "SELECT event_uid FROM events WHERE type = ? ORDER BY id DESC LIMIT 1",
        (event_type,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return str(row[0])


# --- Public re-exports ------------------------------------------------------


__all__ = [
    "AttentionChannel",
    "DecideContext",
    "DecideResult",
    "EffectivePolicy",
    "GateOutcome",
    "GateResult",
    "LifecycleLike",
    "PreEmitPermission",
    "ResolverConfidence",
    "ResolverResult",
    "ResponsePlan",
    "RuntimePathsLike",
    "SituationPacket",
    "ToolDefinitionLike",
    "ToolRegistryLike",
    "attention_policy",
    "decide",
    "effective_policy",
    "pre_action_gate",
    "pre_emit_gate",
    "resolve_task_ref",
    "resolve_task_ref_by_window",
    "result_interpreter",
]
