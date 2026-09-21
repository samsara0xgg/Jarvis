"""L3 Runtime Decision — ``decide()`` entry point + supporting pipeline.

Per ADR 0001 § Stub strategy L3 rows, § Resolver contract, § Gate
contracts, § Canonical event trace evt 03..24.

This package exports the public surface of L3:

- :class:`SituationPacket` (from :mod:`jarvis.decision.packet`).
- :class:`EffectivePolicy` (from :mod:`jarvis.decision.policy`).
- :class:`GateResult` / :class:`ResponsePlan` (from
  :mod:`jarvis.decision.gates`).
- :func:`decide` — the single function the composition root
  (Step 10) wires into the runtime loop.
- :class:`DecideContext` / :class:`DecideResult` — call / return
  shapes for :func:`decide`.

The full pipeline orchestration lives here. Submodules implement the
individual stages:

- ``packet.py`` — Situation Packet assembler.
- ``policy.py`` — Effective Policy Resolver.
- ``intent.py`` — Tier 0 scaffold + Tier 2 LLM bridge.
- ``gates.py`` — Pre-action / Pre-emit gates + Attention Policy.
- ``llm.py`` — multi-provider LLM client (Step 8).

``decide()`` handles **one** trigger per call and returns. Multi-trigger
orchestration lives in ``jarvis/runtime``.

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
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from jarvis.decision.confirm_grammar import match_confirm_grammar
from jarvis.decision.cost_guard import CostRecorder
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
from jarvis.decision.llm_stream import LLMResponseFailed, LLMTextDelta
from jarvis.decision.packet import (
    DEFAULT_OBSERVER_POLL_INTERVAL_S,
    SituationPacket,
    assemble_packet,
    format_pending_confirmation_note,
    format_status_board_note,
)
from jarvis.decision.policy import EffectivePolicy, effective_policy, surface_for
from jarvis.decision.stream_envelope import (
    StreamEnvelopeSplitter,
    compose_envelope,
    envelope_only,
    split_envelope,
)
from jarvis.decision.stream_finalize import StreamFinalizationFailure, finalize_stream
from jarvis.decision.stream_gate import stream_emission_gate
from jarvis.decision.stream_risk import SegmentRiskClassifier
from jarvis.decision.stream_sentences import SemanticAssembler
from jarvis.decision.tier0 import render_tier0_response
from jarvis.shared import (
    ActionRequest,
    CallerPrincipal,
    Event,
    RawResult,
    RawResultBundle,
)
from jarvis.shared.pricing import compute_cost_usd, load_pricing_table
from jarvis.shared.realtime import AlreadyConsumed, Wave1FeatureFlags, stable_authorization_identity
from jarvis.shared.realtime_trace import realtime_trace_context, record_realtime_trace
from jarvis.shared.text import truncate_utf8
from jarvis.state.authorized_dispatch_outbox import (
    AuthorizedDispatchAlreadyStarted,
    ConfirmationRevalidationError,
    answer_confirmation_once,
    authorize_confirmation_dispatch,
)
from jarvis.state.cost_accounting import record_run_cost_once
from jarvis.state.event_log import emit_event, iter_events_of_types
from jarvis.state.projections import make_snapshot
from jarvis.state.stream_emission import committed_text_prefix

if TYPE_CHECKING:
    import sqlite3

    from jarvis.decision.confirm_grammar import ConfirmGrammarHit, ConfirmGrammarTable
    from jarvis.decision.llm import ChatResult, LLMClient
    from jarvis.decision.pre_route import RoutineStreamRoute, StreamCorrection
    from jarvis.decision.stream_sentences import SemanticCandidate
    from jarvis.decision.tier0 import Tier0Hit, Tier0Table
    from jarvis.shared import AuthorizationLease, RiskLevel
    from jarvis.state.projections import PendingConfirmationSlot

LOGGER = logging.getLogger(__name__)

# Hard ceiling on the tool-use loop in `decide()`. Defends against an LLM
# that keeps proposing tool calls without converging.
_DEFAULT_MAX_TOOL_ITERATIONS = 5

# ADR-0012 §3 D4/V2 — confirmation TTL default (10 minutes). Config-
# overridable via `config/jarvis.yaml`'s `confirmation.ttl_ms`
# (composition root: `jarvis.runtime._confirmation_ttl_ms`) so the
# live burn can use a short value without touching code.
_DEFAULT_CONFIRMATION_TTL_MS: Final[int] = 600_000

# `_dispatch_one_tool_call`'s signal to `_run_tool_use_loop`:
# "continue" — tool dispatched, loop to the next iteration;
# "confirm_required" — the FIRST confirm_required this turn was
# frozen into an ask, the tool loop ends here (no more LLM calls).
_DispatchOutcome = Literal["continue", "confirm_required"]

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
    if ctx.wave1_features.exactly_once_cost_accounting:
        outcome = CostRecorder(
            ctx.conn,
            pricing_table=_pricing_table(),
        ).record_chat_result(
            chat_result,
            client=ctx.llm_client,
            kind=kind,
            turn_id=turn_id,
            run_id=run_id,
        )
        return outcome.event

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


def _check_response_cancelled(ctx: DecideContext, where: str) -> None:
    """Stop new response work while preserving already accepted actions."""
    if ctx.cancellation_checkpoint is not None:
        ctx.cancellation_checkpoint(where)


def _run_llm_chat_with_cost_guard(  # noqa: PLR0913 - mirrors the provider call plus audit keys
    ctx: DecideContext,
    *,
    messages: list[dict[str, Any]],
    system: str,
    tools: list[dict[str, Any]] | None,
    kind: str,
    turn_id: str | None,
    tool_choice: str | None = "auto",
) -> ChatResult:
    """Use the exactly-once guard only when its Wave 1 flag is enabled."""
    _check_response_cancelled(ctx, "before provider request")
    cost_recorder = (
        CostRecorder(ctx.conn, pricing_table=_pricing_table())
        if ctx.wave1_features.exactly_once_cost_accounting else None
    )
    if ctx.request_admission is not None:
        ctx.request_admission(kind)
    if cost_recorder is None:
        return ctx.llm_client.chat(
            messages=messages,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
        )
    return cost_recorder.chat(
        ctx.llm_client,
        messages=messages,
        system=system,
        tools=tools,
        tool_choice=tool_choice,
        kind=kind,
        turn_id=turn_id,
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
    return _append_cost_recorded(ctx, cost, turn_id=turn_id)


def _append_cost_recorded(
    ctx: DecideContext,
    cost: Mapping[str, Any],
    *,
    turn_id: str | None,
) -> Event | None:
    """Price one cost mapping and append the single ``cost.recorded`` row."""
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
    if run_id is not None:
        return record_run_cost_once(
            ctx.conn, run_id=run_id, payload=payload, correlation=correlation or None,
        )
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

    def pending_write_path(self, confirmation_id: str) -> Path:
        """Return the staging path for a pending write's content.

        ADR-0012 §3 D3/D5: on ``confirm_required``, ``write_file``'s
        ``content`` argument is staged here — never on the event
        payload (§3.3.9 bounded payloads) — before
        ``confirmation.requested`` is emitted.
        """
        ...


class ToolDefinitionLike(Protocol):
    """Structural view of L4 ``ToolDefinition`` records.

    Used by ``decide()`` when assembling the LLM tool list.
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
    def risk_level(self) -> RiskLevel:
        """Risk level for the Pre-action Gate."""
        ...

    @property
    def input_schema(self) -> Mapping[str, Any]:
        """JSON-schema for the tool's arguments."""
        ...

    @property
    def requires_entity(self) -> bool:
        """Whether the Pre-action Gate must see a resolved target_entity_ref.

        ADR-0011 D3: consumed by ``pre_action_gate``'s entity arm. Both
        call sites pass the ``tool_def`` this Protocol types straight
        through to the gate.
        """
        ...


class ResolvedEntityLike(Protocol):
    """One resolved non-task entity handed back by the injected resolver.

    ADR-0011 D4 resolve-on-propose: L3 cannot import
    ``jarvis.execution.path_resolver`` (layer boundary), so the runtime
    composition root injects a small callable (``EntityResolverLike``)
    that returns an object satisfying this Protocol on a hit.
    """

    @property
    def entity_id(self) -> str:
        """Deterministic natural key, e.g. ``"file:<abs-path>"``."""
        ...

    @property
    def canonical(self) -> str:
        """The resolved absolute path (or other canonical form)."""
        ...

    @property
    def confidence(self) -> str:
        """``"exact"`` | ``"fuzzy"`` | ``"bookmark"`` | ``"config"``."""
        ...

    @property
    def match_basis(self) -> str:
        """Where the match came from, e.g. ``"bookmark"`` / ``"search"``."""
        ...


class EntityResolverLike(Protocol):
    """Injected resolve-on-propose callable (ADR-0011 D4).

    Never raises — a resolution miss is a normal ``None`` return, not
    an exception; the caller (``_dispatch_one_tool_call``) treats
    ``None`` as ``outcome="not_found"``.
    """

    def __call__(self, query: str, /) -> ResolvedEntityLike | None:
        """Resolve ``query`` (a raw ``target`` argument) or return None."""
        ...


class ToolRegistryLike(Protocol):
    """Structural view of L4 ``ToolRegistry``.

    L3 calls ``for_caller`` (for tool list assembly) and ``dispatch``
    (for executing a gated ActionRequest). The composition root binds
    the real registry; tests build minimal duck-typed substitutes.
    """

    def get_definitions(self) -> tuple[ToolDefinitionLike, ...]:
        """Return every registered tool, unfiltered by caller.

        Used for caller-blind NAME resolution (Tier 0), where deciding
        whether the principal may actually call the tool is the
        Pre-action Gate's job, not the lookup's.
        """
        ...

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
        tier0_table: Compiled Tier 0 regex whitelist (spec §17) the
            composition root loaded from ``config/tier0_patterns.yaml``.
            ``None`` (or an empty table) disables Tier 0 — every turn
            falls through to the Tier 2 LLM loop.
        observer_poll_interval_s: The repo observer's configured poll
            cadence (``observer.poll_interval_s``), in seconds, supplied
            by the composition root the same way ``tier0_table`` is.
            Only the Status Board note reads it, to derive its stale
            threshold (3x the interval, ADR-0009 D6 v0). The default is
            the shipped cadence, so a hand-assembled context still calls
            staleness the way the daemon does.
        entity_resolver: Injected resolve-on-propose callable (ADR-0011
            D4), or ``None``. ``_dispatch_one_tool_call`` calls it for
            a ``requires_entity=True`` tool whose ``target_entity_ref``
            is still unset. ``None``
            (the default) makes the feature inert: every
            ``requires_entity=True`` tool then refuses via Step 3's
            gate arm — correct fail-closed behavior, not a bug, for
            any context that hasn't wired a resolver (e.g. a context
            with no file-entity tools registered).
        write_entity_resolver: Injected resolve-on-propose callable for
            ``write_file`` (ADR-0012 D1 — extends ADR-0011 D4).
            ``_dispatch_one_tool_call`` uses THIS resolver instead of
            ``entity_resolver`` when the tool being resolved is
            ``write_file``, because a write target's resolution rule
            differs from a read target's: a non-existent path resolves
            iff its parent directory is in scope, which
            ``entity_resolver`` (backed by ``path_resolver.resolve``,
            existing-file-only) cannot do. ``None`` (the default) makes
            ``write_file`` resolution inert the same way ``None`` on
            ``entity_resolver`` does for ``read_file`` — fail-closed,
            not a bug.
        confirmation_ttl_ms: How long a ``confirmation.requested`` ask
            stays live before the PendingConfirmations projection
            judges it expired (ADR-0012 §3 D4/V2), in milliseconds.
            Default 10 minutes; the composition root reads
            ``confirmation.ttl_ms`` from ``config/jarvis.yaml``
            (`jarvis.runtime._confirmation_ttl_ms`) so the live burn
            can use a short value without touching code.
        confirm_grammar_table: Compiled exact-sentence yes/no grammar
            (ADR-0012 §3 D6) the composition root loaded from
            ``config/confirm_grammar.yaml``. ``()`` (the default)
            disables the answer-path grammar hook entirely — every
            utterance falls through to Tier 0 / Tier 2 exactly as if
            no confirmation flow existed, same "off means inert" shape
            as ``tier0_table=None``.
    """

    conn: sqlite3.Connection
    runtime_paths: RuntimePathsLike
    tool_registry: ToolRegistryLike
    lifecycle: LifecycleLike
    llm_client: LLMClient
    system_prompt: str
    max_tool_iterations: int = _DEFAULT_MAX_TOOL_ITERATIONS
    tier0_table: Tier0Table | None = None
    observer_poll_interval_s: int = DEFAULT_OBSERVER_POLL_INTERVAL_S
    entity_resolver: EntityResolverLike | None = None
    write_entity_resolver: EntityResolverLike | None = None
    confirmation_ttl_ms: int = _DEFAULT_CONFIRMATION_TTL_MS
    confirm_grammar_table: ConfirmGrammarTable = ()
    wave1_features: Wave1FeatureFlags = field(default_factory=Wave1FeatureFlags)
    cancellation_checkpoint: Callable[[str], None] | None = None
    request_admission: Callable[[str], None] | None = None
    # The history block (profile, current summary, verbatim records) and
    # the per-turn time line, rendered by the composition root from
    # memory.db. The history goes first in the prompt and only grows at
    # its end between compactions; the time line goes after it.
    memory_note: str | None = None
    time_note: str | None = None
    # ADR-0008 Step 8. ``routine_stream`` is the pre-routed streaming seam the
    # runtime bound for this run (None on every other turn, so decide() keeps
    # the batch tool loop). ``stream_correction`` marks a full-text run that
    # continues the exposed prefix of a failed stream.
    routine_stream: RoutineStreamRoute | None = None
    stream_correction: StreamCorrection | None = None


@dataclass(frozen=True)
class DecideResult:
    """Output of one :func:`decide` call.

    Attributes:
        response_plan: Final ResponsePlan when this invocation
            produced user-facing text; ``None`` for invocations that
            only emitted internal events.
        events_emitted: Frozen tuple of every Event ``decide()``
            emitted during this invocation (turn / action / gate /
            confirmation). Surface adapters and tests inspect this.
        turn_id: Turn correlation for this invocation (when
            applicable).
        attention_channel: Output of
            :func:`jarvis.decision.gates.attention_policy` for the
            packet that drove this invocation.
        route: ADR-0008 Step 8 — ``"casual_or_explanatory"`` when this
            invocation streamed through the routine route; ``None`` on
            the batch path.
        emitted_segments: Permitted segments already exposed as durable
            ``surface.response_chunk`` rows before the plan was final.
        last_gate_event_uid: The last ``gate.evaluated(stream_emit)``
            this invocation committed; the runtime's ``turn.ended``
            source on the routine route.
        stream_failure: A typed finalization refusal; the runtime fails
            the run with its prefix hash and opens a correction run.
    """

    response_plan: ResponsePlan | None
    events_emitted: tuple[Event, ...]
    turn_id: str | None
    attention_channel: AttentionChannel = "queue_review"
    route: str | None = None
    emitted_segments: int = 0
    last_gate_event_uid: str | None = None
    stream_failure: StreamFinalizationFailure | None = None


# --- Internal scratch state for one decide() invocation --------------------


@dataclass
class _Scratch:
    """Per-call mutable accumulator used to keep ``decide()`` readable.

    Not exported. Each ``decide()`` invocation builds a fresh one and
    returns its contents wrapped in a frozen :class:`DecideResult`.
    """

    events: list[Event] = field(default_factory=list)
    turn_id: str | None = None
    # ADR-0012 D5: the exact rendered template_line for the FIRST
    # `confirm_required` this turn froze — stamped by
    # `_stage_and_request_confirmation` via `_dispatch_one_tool_call`,
    # read back by `_run_tool_use_loop` to build the final draft. Lives
    # on scratch (not a local in either function) because the value is
    # produced deep in the tool-dispatch loop and consumed one frame up.
    pending_confirmation_template_line: str | None = None
    # ADR-0012 D6: True once `_handle_confirmation_accepted` /
    # `_handle_confirmation_rejected` has run this turn (any outcome —
    # success, hash-mismatch abort, gate-refuse abort, dispatch error,
    # or a plain rejection). `_finalize_response` reads this to force
    # `voice_notify`: every one of those branches is a direct,
    # synchronous reply to a question Allen just asked, never a
    # silent_log/queue_review candidate. Same flag-to-finalize idiom as
    # `pending_confirmation_template_line` above.
    confirmation_answered_this_turn: bool = False


def _insert_system_notes(
    messages: list[dict[str, Any]],
    packet: SituationPacket,
    ctx: DecideContext,
) -> None:
    """Insert the prompt-head system notes into ``messages``.

    Each note is added via ``messages.insert(0, ...)``, and each such
    call pushes every note already inserted further from index 0 — so
    the call order below is bottom-to-top: the FIRST call ends up
    nearest the live conversation (later inserts push it toward the
    tail, where the user message sits), the LAST call ends up at index
    0, farthest from it. Final stacking order (top → bottom):
    Status Board (ambient background, called last), open actions,
    pending confirmation (nearest the conversation, called first — the
    most immediately turn-critical: it governs what THIS draft may claim
    about THIS turn's outstanding ask; spec §3.4.4, Phase 0 batch 4;
    ADR-0012 §3 D4).

    The memory history block is inserted LAST so it sits at index 0,
    ahead of every per-turn note: it is the one block that stays
    byte-identical between turns, so the provider's prefix cache covers
    it, and everything that changes per turn (the time line, the folded
    state) follows it (spec §10.5).
    """
    # ADR-0012 §3 D4: id-free note naming an outstanding confirmation
    # ask, if one is live. Lets an unrelated turn's LLM know an ask is
    # outstanding (C4) and a paraphrased-consent turn's LLM talk about
    # it (C6) without being able to act on it — the note carries no
    # confirmation_id. Called FIRST so it ends up nearest the
    # conversation: it is the most immediately turn-critical of the
    # notes (governs what THIS draft may claim about THIS turn's
    # outstanding ask).
    pending_confirmation_note = format_pending_confirmation_note(packet)
    if pending_confirmation_note is not None:
        messages.insert(0, {"role": "user", "content": pending_confirmation_note})
    # The non-terminal actions, so the LLM knows what is still running.
    open_actions_note = _format_open_actions_note(packet)
    if open_actions_note is not None:
        messages.insert(0, {"role": "user", "content": open_actions_note})
    # ADR-0009 D6 (render half of Step 11): folded Status Board with its
    # §3.6.9 freshness wording, so "repo X 现在什么状态" is answered from
    # observer-folded state instead of the LLM reaching for git (M6).
    status_board_note = format_status_board_note(
        packet, poll_interval_s=ctx.observer_poll_interval_s,
    )
    if status_board_note is not None:
        messages.insert(0, {"role": "user", "content": status_board_note})
    if ctx.time_note:
        messages.insert(0, {"role": "user", "content": ctx.time_note})
    if ctx.memory_note:
        messages.insert(0, {"role": "user", "content": ctx.memory_note})


# --- decide() entry point ---------------------------------------------------


def decide(trigger: Event, ctx: DecideContext) -> DecideResult:
    """Drive one L3 invocation in response to ``trigger``.

    See the module docstring for the canonical event trace. The
    branches handled Day-1:

    - **``surface.user_intent``**: emit ``turn.started``, run Tier 0,
      then drive the Tier 2 LLM tool-use loop. Each tool call goes
      through the Pre-action Gate and dispatch. When the LLM emits
      text, run the Pre-emit Gate and finalize with ``turn.ended``.
    - **``action.result_observed``**: ask the LLM for a final
      response, then Pre-emit Gate, then ``turn.ended``.

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
    policy = effective_policy(_allowed_tool_surface(ctx.tool_registry))

    # ``utterance.received`` is the voice-surface twin of
    # ``surface.user_intent``: ADR-0005 §5.1 — the ASR pipeline owns the
    # audit / normalize step and emits ``utterance.received`` (carrying
    # the same ``turn_id`` + ``transcript`` payload contract), so the
    # voice path must take the same handler. Without this widening,
    # every voice turn no-ops here and the watcher times out 5 s later.
    if trigger.type in ("surface.user_intent", "utterance.received"):
        return _handle_utterance(packet, policy, ctx, scratch)
    if trigger.type == "action.result_observed":
        return _handle_result_observed(packet, policy, ctx, scratch)
    if trigger.type in ("action.timeout_assumed", "action.failed", "action.cancelled"):
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


def _claimed_turn_started(conn: sqlite3.Connection, trigger_event_uid: str) -> Event | None:
    """Return the durable turn claim for this trigger, if the pump made one.

    ADR-0008 D8's ``claim_input_once`` keys the claim by the trigger's
    ``event_uid``, which is exactly what ``source_event_id`` holds, so the
    lookup needs no new index or payload field.
    """
    return next(
        (
            event
            for event in iter_events_of_types(conn, ("turn.started",))
            if event.source_event_id == trigger_event_uid
        ),
        None,
    )


def _handle_utterance(
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """Process a ``surface.user_intent`` trigger end-to-end (Day-1)."""
    trigger = packet.trigger_event
    # ADR-0008 D8: when the runtime's intent pump durably claimed this
    # trigger, the claim IS this turn's `turn.started` and its turn_id is
    # authoritative. Hydrating it here rather than emitting a second one is
    # what keeps one utterance to one turn row; with the pump off there is
    # never a claim and this is the unchanged Day-1 path.
    claimed = _claimed_turn_started(ctx.conn, trigger.event_uid)
    turn_id = (
        str(claimed.payload["turn_id"])
        if claimed is not None
        else (packet.current_turn_id or _new_turn_id())
    )
    scratch.turn_id = turn_id

    if claimed is None:
        scratch.events.append(
            emit_event(
                ctx.conn,
                type="turn.started",
                payload={"turn_id": turn_id, "trigger": trigger.event_uid},
                source_event_id=trigger.event_uid,
                correlation={"turn_id": turn_id},
            ),
        )

    # ADR-0012 §3 D6 — the answer-path grammar hook, BEFORE
    # `tier_0_match`. THE LOAD-BEARING INVARIANT this ADR builds
    # toward: no LLM output, under any phrasing, can cause an L3
    # dispatch — only a hit against `ctx.confirm_grammar_table` can,
    # and only by reaching `_handle_confirmation_accepted`, the ONE
    # function in this module that mints an AuthorizationLease, and
    # even then only through the FULL `pre_action_gate`. This is
    # structural, not a policy the LLM is asked to respect:
    #   - The check below runs and returns BEFORE `tier_0_match` and
    #     (further down) before `_run_tool_use_loop` ever calls the
    #     LLM this turn. On a grammar hit, the LLM is never invoked at
    #     all for this trigger.
    #   - The grammar is active ONLY while the PendingConfirmations
    #     projection holds a live (`state == "pending"`, unexpired)
    #     slot (`PendingConfirmationSlot.is_live`) — an expired or
    #     already-answered slot makes this whole block inert and the
    #     turn falls through to ordinary Tier 0 / Tier 2 handling
    #     exactly as if no confirmation existed (D6 "expired pending ->
    #     ordinary turn"; C3).
    #   - The LLM's only touchpoint with a pending ask is
    #     `format_pending_confirmation_note` — an id-free system note
    #     it can talk ABOUT (C4/C6) but that carries no
    #     `confirmation_id`, no tool, and no argument it could use to
    #     act on the ask. Its worst case (a paraphrase like "行吧那就写
    #     进去吧", ADR's own C6 example) is proposing the action again
    #     via the ordinary tool-call path, which produces a FRESH
    #     `confirm_required` -> a fresh `confirmation.requested` that
    #     supersedes this slot and re-asks — never a dispatch.
    pending_slot = packet.pending_confirmation.slot
    if pending_slot is not None and pending_slot.is_live(_now_epoch_ms()):
        transcript_raw = trigger.payload.get("transcript", "")
        transcript = transcript_raw if isinstance(transcript_raw, str) else ""
        grammar_hit = match_confirm_grammar(transcript, ctx.confirm_grammar_table)
        if grammar_hit is not None:
            if grammar_hit.decision == "yes":
                return _handle_confirmation_accepted(
                    pending_slot, grammar_hit, transcript, packet, policy, ctx, scratch,
                )
            return _handle_confirmation_rejected(
                pending_slot, grammar_hit, transcript, packet, ctx, scratch,
            )

    # Tier 0 deterministic shortcut (spec §17): hit → dispatch through
    # the full gate/audit chain with caller_principal=regex_router,
    # LLM never invoked. Miss / no table → Tier 2 loop.
    hit = tier_0_match(packet, ctx.tier0_table)
    if hit is not None:
        return _run_tier0_path(hit, packet, policy, ctx, scratch)

    # Tier 2 tool-use loop. Each iteration calls the LLM, dispatches any
    # tool_calls (through the Pre-action Gate), and either continues (if more tool calls) or breaks
    # (if the LLM returned text — which goes to Pre-emit Gate).
    return _run_tool_use_loop(packet, policy, ctx, scratch)


def _run_tool_use_loop(
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """Drive the Tier 2 LLM tool-use loop until text or limit."""
    if ctx.routine_stream is not None:
        return _run_routine_stream(packet, ctx, ctx.routine_stream, scratch)

    messages = _loop_messages(packet, ctx)
    llm_surface = surface_for(policy, ctx.tool_registry, CallerPrincipal.JARVIS_LLM)
    tools = tool_definitions_for_llm([_tool_to_dict(t) for t in llm_surface])

    iteration = 0
    while iteration < ctx.max_tool_iterations:
        iteration += 1
        record_realtime_trace(
            "llm_chat_call_started_upper_bound",
            turn_id=scratch.turn_id,
            request_kind="decision",
            iteration=iteration,
            measurement_semantics="before_llm_client_call_not_transport_send",
        )
        with realtime_trace_context(
            turn_id=scratch.turn_id,
            request_kind="decision",
            iteration=iteration,
        ):
            chat_result = _run_llm_chat_with_cost_guard(
                ctx,
                messages=messages,
                system=ctx.system_prompt,
                tools=tools,
                kind="decision",
                turn_id=scratch.turn_id,
            )
        record_realtime_trace(
            "llm_batch_response_completed",
            turn_id=scratch.turn_id,
            request_kind="decision",
            iteration=iteration,
            candidate_kind="tool" if chat_result.tool_calls else "text",
            text_characters=len(chat_result.text or ""),
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

        _check_response_cancelled(ctx, "after provider response")
        if chat_result.tool_calls:
            # Append the assistant turn (with tool_calls) so the next
            # iteration sees the LLM's tool requests in history.
            messages.append(_assistant_message_for(chat_result))

            for tool_call in chat_result.tool_calls:
                _check_response_cancelled(ctx, "before tool proposal")
                _dispatch_one_tool_call(
                    tool_call=tool_call,
                    policy=policy,
                    ctx=ctx,
                    scratch=scratch,
                    messages=messages,
                )
                # ADR-0012 D4: a "confirm_required" outcome does NOT
                # break this inner loop — a batch of several tool_calls
                # in the SAME LLM response must still see every later
                # one (the first froze the ask; each later one falls
                # through to the generic gate-refuse handling inside
                # `_dispatch_one_tool_call` once
                # `_confirmation_already_requested_this_turn` is True,
                # per D4 "further L3 proposals in the same turn get the
                # synthetic refuse result").

            turn_ending_draft = _turn_ending_draft(scratch, chat_result.text)
            if turn_ending_draft is not None:
                return _finalize_response(turn_ending_draft, packet, ctx, scratch)

            # Refresh the packet so the next LLM call sees the log as the
            # tool dispatches left it.
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


def _turn_ending_draft(scratch: _Scratch, llm_text: str | None) -> str | None:
    """The draft that ends the tool loop this turn, or None to keep looping.

    ADR-0012 D5: the first ``confirm_required`` ends the tool loop here —
    no more LLM calls this turn, regardless of which tool_call in the
    batch (or which loop iteration) triggered it. The draft is THIS
    response's own LLM text (optional 铺垫, if any — goes through the
    normal Pre-emit Gate scrub like any other draft) plus the
    runtime-rendered template line frozen at staging time
    (`_stage_and_request_confirmation`), never composed by the LLM.
    """
    if _confirmation_already_requested_this_turn(scratch):
        llm_preamble = (llm_text or "").strip()
        template_line = scratch.pending_confirmation_template_line or ""
        return f"{llm_preamble}\n\n{template_line}" if llm_preamble else template_line
    return None


_TIER0_GATE_REFUSED_TEXT: Final[str] = "这条指令被 Pre-action Gate 拦下，未执行。"  # noqa: RUF001 — fullwidth comma/period are intentional Chinese punctuation.

# Fixed, scrub-safe text for an erroring Tier 0 tool. MUST stay free of
# completion-class keywords (完成 / 已完成 / done / verified): a Tier 0
# draft that trips the Pre-emit Gate's downgrade path would re-prompt
# the LLM, which is exactly what this path exists to avoid. The
# handler's own error tag is a developer string and goes to the log,
# never to the voice surface.
_TIER0_TOOL_ERROR_TEXT: Final[str] = "这条指令执行出错，未产生结果。"  # noqa: RUF001 — fullwidth comma/period are intentional Chinese punctuation.

# ADR-0012 §3 D5 — synthetic tool result injected on the FIRST
# `confirm_required` this turn (the ask). Never sent to the LLM within
# THIS `decide()` call (the tool loop ends right after), but kept
# JSON-shaped for consistency with every other synthetic tool result
# in this module.
_CONFIRM_REQUIRED_TOOL_RESULT_TEXT: Final[str] = "等待 Allen 确认，本回合不可执行"  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.

# ADR-0012 §3 D5 — the exact rendered action line, appended by the
# RUNTIME to the draft, never composed by the LLM (§13.4; consent
# binds machine truth). Rendered ONLY from the frozen action_snapshot
# (`_stage_and_request_confirmation`) — `canonical_target` is the
# resolved absolute path, the one thing standing between Allen and a
# fuzzy-resolved write landing somewhere unintended. Fixed vocabulary,
# scrub-safe (no 完成/已完成/done/verified), same discipline as the
# Tier 0 texts above. `{tool_name}`/`{risk_level}` are interpolated
# rather than hardcoded to "write_file"/"L3" even though that is the
# only L3 tool today (D1) — byte-identical output for the one case
# that exists, forward-compatible if a second L3 tool ever lands.
_CONFIRMATION_TEMPLATE_LINE: Final[str] = (
    "待确认：{tool_name} → `{canonical_target}`"  # noqa: RUF001 — fullwidth colon is intentional Chinese punctuation.
    "（{mode}，{content_bytes} 字节，风险 {risk_level}）。"  # noqa: RUF001 — fullwidth parens/comma/period are intentional Chinese punctuation.
    "回复「可以」执行，「不要」取消。"  # noqa: RUF001 — fullwidth comma/period are intentional Chinese punctuation.
)

_TIER0_SPOKEN_PREVIEW_MAX_BYTES: Final[int] = 200
"""MUST-FIX 2b (ADR-0011 §12): a Tier 0 template's tool-payload values
go straight to TTS unbounded otherwise — `read_file`/`read_clipboard`'s
own 8 KiB cap is a text-safety limit, not a speech-safety one (2761
measured chars for a truncated CJK clipboard). Every string value
interpolated into a Tier 0 template is capped to this short spoken
preview before rendering; the FULL value already reached
``action.result_observed`` via the L4 handler's own emission before
:func:`_run_tier0_path` ever calls :func:`render_tier0_response`, so
ADR §8 row T6 ("pbpaste content in observation") stays satisfied
regardless of what gets spoken."""


def _spoken_preview_payload(
    payload: Mapping[str, Any],
    max_bytes: int = _TIER0_SPOKEN_PREVIEW_MAX_BYTES,
) -> dict[str, Any]:
    """Cap every string value in ``payload`` to a short spoken preview.

    Non-string values (e.g. ``truncated: bool``, ``total_bytes: int``)
    pass through unchanged — :func:`render_tier0_response` only ever
    interpolates them via ``str()`` and they're already short scalars.
    """
    preview: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(value, str):
            preview[key] = value
            continue
        value_bytes = value.encode("utf-8")
        text, undelivered, _lossy = truncate_utf8(
            value_bytes[:max_bytes], len(value_bytes),
        )
        preview[key] = f"{text}…[truncated {undelivered} bytes]" if undelivered > 0 else text
    return preview


def _run_tier0_path(
    hit: Tier0Hit,
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """Dispatch one Tier 0 whitelist hit (spec §17) without the LLM.

    Mirrors :func:`_dispatch_one_tool_call` for a sync, entity-free tool
    with ``caller_principal=REGEX_ROUTER``: proposed → Pre-action Gate →
    authorized → L4 dispatch → deterministic template text →
    :func:`_finalize_response` (Pre-emit Gate + attention). Spec §3.5.2:
    "低延迟路径，但仍要过 entity / policy / risk gate".

    An entry naming an unknown tool is a table misconfiguration that
    ``validate_tier0_table`` normally rejects at bootstrap; reaching it
    here degrades to the Tier 2 loop rather than failing the turn.
    """  # noqa: RUF002 — fullwidth punctuation is verbatim spec §3.5.2 Chinese quotation.
    tool_def = _find_registered_tool_def(ctx.tool_registry, hit.tool_name)
    if tool_def is None:
        LOGGER.warning(
            "tier0: pattern %r targets unknown tool %r — falling back to LLM",
            hit.pattern_id,
            hit.tool_name,
        )
        return _run_tool_use_loop(packet, policy, ctx, scratch)

    action_id = _new_action_id()
    action_request = ActionRequest(
        action_id=action_id,
        tool_name=hit.tool_name,
        target_entity_ref=None,
        caller_principal=CallerPrincipal.REGEX_ROUTER,
        risk_level=tool_def.risk_level,
        arguments=dict(hit.tool_args),
        authorization_lease=None,
        run_id=None,
        turn_id=scratch.turn_id,
    )
    proposed_event = emit_event(
        ctx.conn,
        type="action.proposed",
        payload={
            "action_id": action_id,
            "tool_name": hit.tool_name,
            "caller_principal": CallerPrincipal.REGEX_ROUTER.value,
            "risk_level": tool_def.risk_level,
            "target_entity_ref": None,
            "turn_id": scratch.turn_id,
            "arguments": dict(hit.tool_args),
            "routed_by": "tier_0",
            "pattern_id": hit.pattern_id,
        },
        correlation=_action_correlation(action_request),
    )
    scratch.events.append(proposed_event)

    gate = pre_action_gate(action_request, policy, tool_def=tool_def)
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
        # No LLM to adapt on this path (that is the point of Tier 0), so
        # the refusal itself is the user-facing text. This also covers
        # `confirm_required`: boot validation (`validate_tier0_table`'s
        # `requires_confirmation_tool_names` arm, ADR-0012 §3 D5)
        # forbids any Tier 0 row from targeting a `requires_confirmation`
        # tool, so a real `confirm_required` should never reach here.
        # This branch is the RUNTIME enforcement of that boot rule
        # (defense in depth) — declared, not dead code: Tier 0 has no
        # LLM to ask/answer a confirmation, so if a misconfiguration
        # ever slipped past the boot check, refusing here (rather than
        # e.g. crashing) is still the correct, safe behavior.
        return _finalize_response(_TIER0_GATE_REFUSED_TEXT, packet, ctx, scratch)

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

    record_realtime_trace(
        "action_dispatch_started",
        turn_id=scratch.turn_id,
        action_id=action_id,
        tool_name=hit.tool_name,
    )
    _check_response_cancelled(ctx, "before tool dispatch")
    bundle = ctx.tool_registry.dispatch(
        action_request, ctx.conn, ctx.runtime_paths, ctx.lifecycle,
    )
    record_realtime_trace(
        "action_dispatch_returned",
        turn_id=scratch.turn_id,
        action_id=action_id,
        tool_name=hit.tool_name,
        result_slots=len(bundle.slots),
    )
    record_realtime_trace(
        "action_result_available",
        turn_id=scratch.turn_id,
        action_id=action_id,
        tool_name=hit.tool_name,
        result_source="synchronous_dispatch_return",
    )

    primary_slot = bundle.slots[0]
    if primary_slot.error is not None:
        # The error tag is a developer string: it can carry anything,
        # including completion-class wording that would push
        # _finalize_response onto the LLM-retry path. Keep it in the log
        # and answer with fixed, scrub-safe text.
        LOGGER.warning(
            "tier0: pattern %r tool %r returned error slot %r — answering with fixed text",
            hit.pattern_id,
            hit.tool_name,
            primary_slot.error,
        )
        draft = _TIER0_TOOL_ERROR_TEXT
        return _finalize_response(draft, packet, ctx, scratch)

    # MUST-FIX 2 (ADR-0011 §12): `primary_slot.payload` may carry
    # user-controlled tool output (e.g. clipboard content) — cap it to
    # a short spoken preview before it goes to TTS (2b), and gate on
    # `hit.response_template` (the closed, unformatted template
    # literal) rather than on the rendered `draft` below (2a) — see
    # `_finalize_response`'s `gate_text` parameter for the full
    # rationale. The FULL, uncapped payload already reached
    # `action.result_observed` via the L4 handler above; only what
    # gets SPOKEN and what gets GATED change here.
    draft = render_tier0_response(
        hit,
        _spoken_preview_payload(
            primary_slot.payload,
            hit.max_spoken_bytes or _TIER0_SPOKEN_PREVIEW_MAX_BYTES,
        ),
    )
    # Form, not length, picks the channel (spec §18.3 voice = conclusion,
    # panel = evidence): a multi-line render is material for the card.
    # ponytail: line-count heuristic; upgrade to a per-tool output-form
    # declaration if a one-line tool result ever needs the panel.
    return _finalize_response(
        draft,
        packet,
        ctx,
        scratch,
        gate_text=hit.response_template,
        document_form="\n" in draft.strip(),
    )


def _dispatch_one_tool_call(  # noqa: PLR0915 — single-pass orchestration of resolver + gate + dispatch; splitting muddles the audit trace.
    *,
    tool_call: object,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
    messages: list[dict[str, Any]],
) -> _DispatchOutcome:
    """Resolve, gate, and dispatch one LLM-proposed tool call.

    Returns:
        ``"continue"`` if the tool was dispatched or refused (the loop
        should continue with the next iteration). ``"confirm_required"``
        if this call's Pre-action
        Gate outcome was ``confirm_required`` AND it is the first such
        outcome this turn (ADR-0012 D5) — the caller must end the tool
        loop and finalize with the frozen ask's template line. A
        second-or-later ``confirm_required`` in the same turn (D4)
        instead falls through to the generic ``gate_outcome != "pass"``
        refuse handling below and returns ``"continue"``.
    """
    name = getattr(tool_call, "name", "")
    call_id = getattr(tool_call, "call_id", "")
    arguments_json = getattr(tool_call, "arguments_json", "{}")
    try:
        arguments: dict[str, Any] = json.loads(arguments_json or "{}")
    except (TypeError, ValueError):
        arguments = {}

    # 1. Tool definition lookup.
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
        return "continue"

    # 2. ADR-0011 D4 (resolve-on-propose): a tool that declares
    # `requires_entity=True` (e.g. `read_file`) gets one shot at the
    # injected entity resolver here. `ctx.entity_resolver` /
    # `ctx.write_entity_resolver` (ADR-0012 D1, see the tool-name branch
    # below) are `None` in any context that hasn't wired them; the
    # feature is then inert and the `requires_entity` gate arm refuses,
    # which is correct fail-closed behavior, not a bug.
    # ADR-0012 D1: `write_file`'s write-target resolution differs from
    # every other `requires_entity=True` tool's (a non-existent target
    # can still resolve, iff its parent directory is in scope), so it
    # gets its own injected resolver rather than sharing
    # `ctx.entity_resolver`.
    file_entity_resolver = (
        ctx.write_entity_resolver if name == "write_file" else ctx.entity_resolver
    )
    target_entity_ref: str | None = None
    # ADR-0012 §3 D3: the confirmation snapshot's human-readable
    # `canonical_target` is the resolver's `.canonical` (bare absolute
    # path), captured here at the source of truth rather than
    # re-derived later by stripping `target_entity_ref`'s `"file:"`
    # prefix — that prefix convention belongs to `execution/tools.py`
    # (L4), which L3 must not import. Stays `None` for any tool other
    # than `write_file` (or a miss); `confirm_required` is only
    # reachable once `target_entity_ref` is set, which for `write_file`
    # only ever happens via this block, so the two are always set
    # together on the path that reaches confirm_required.
    write_target_canonical: str | None = None
    if tool_def.requires_entity and file_entity_resolver is not None:
        raw_target = arguments.get("target")
        raw_query = raw_target if isinstance(raw_target, str) else ""
        resolved = file_entity_resolver(raw_query)
        if resolved is not None:
            target_entity_ref = resolved.entity_id
            write_target_canonical = resolved.canonical

    action_id = _new_action_id()
    action_request = ActionRequest(
        action_id=action_id,
        tool_name=name,
        target_entity_ref=target_entity_ref,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level=tool_def.risk_level,
        arguments=arguments,
        authorization_lease=None,
        run_id=None,
        turn_id=scratch.turn_id,
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

    # 4. Pre-action Gate.
    gate = pre_action_gate(action_request, policy, tool_def=tool_def)
    gate_outcome = gate.outcome
    gate_reasons = list(gate.reasons)
    gate_event = emit_event(
        ctx.conn,
        type="gate.evaluated",
        payload={
            "gate": "pre_action",
            "outcome": gate_outcome,
            "reasons": gate_reasons,
            "check_results": dict(gate.check_results),
            "action_id": action_id,
        },
        source_event_id=proposed_event.event_uid,
        correlation=_action_correlation(action_request),
    )
    scratch.events.append(gate_event)

    # ADR-0012 D5/D4: the FIRST `confirm_required` this turn freezes an
    # ask and ends the tool loop. Checked before the generic
    # `!= "pass"` handling below, which a second-or-later
    # `confirm_required` in the same turn falls through to (D4: "只有
    # 第一个 confirm_required 变成 ask, 之后的 L3 proposal 得到 synthetic
    # refuse result") — that generic refuse is exactly what already
    # happens for `gate_outcome == "confirm_required"` there, so no
    # separate refuse text is needed for the second+ case.
    if gate_outcome == "confirm_required" and not _confirmation_already_requested_this_turn(
        scratch,
    ):
        confirmation_event = _stage_and_request_confirmation(
            ctx,
            action_request=action_request,
            arguments=arguments,
            canonical_target=write_target_canonical or "",
            tool_def=tool_def,
            source_event_id=gate_event.event_uid,
        )
        scratch.events.append(confirmation_event)
        scratch.pending_confirmation_template_line = str(
            confirmation_event.payload["template_line"],
        )
        messages.append(
            _tool_result_message(
                call_id=call_id,
                content=json.dumps(
                    {
                        "status": "confirmation_requested",
                        "message": _CONFIRM_REQUIRED_TOOL_RESULT_TEXT,
                    }
                ),
            )
        )
        return "confirm_required"

    if gate_outcome != "pass":
        # Refuse / confirm — inject a tool result explaining the refusal
        # and let the LLM adapt. Day-1 scenario should not hit this.
        messages.append(
            _tool_result_message(
                call_id=call_id,
                content=json.dumps(
                    {
                        "error": "gate refused",
                        "outcome": gate_outcome,
                        "reasons": gate_reasons,
                    }
                ),
            )
        )
        return "continue"

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
    #    action.running + action.result_observed).
    record_realtime_trace(
        "action_dispatch_started",
        turn_id=scratch.turn_id,
        action_id=action_id,
        tool_name=name,
    )
    _check_response_cancelled(ctx, "before tool dispatch")
    bundle = ctx.tool_registry.dispatch(
        action_request,
        ctx.conn,
        ctx.runtime_paths,
        ctx.lifecycle,
    )
    record_realtime_trace(
        "action_dispatch_returned",
        turn_id=scratch.turn_id,
        action_id=action_id,
        tool_name=name,
        result_slots=len(bundle.slots),
    )
    record_realtime_trace(
        "action_result_available",
        turn_id=scratch.turn_id,
        action_id=action_id,
        tool_name=name,
        result_source="synchronous_dispatch_return",
    )
    primary_slot = bundle.slots[0]

    # 6b. ADR-0002 Step 3: when L4 returns RawResult.metadata["cost"],
    #     emit cost.recorded from L3 — the sole emit-site per spec §5.4.1.
    cost_event = _emit_cost_recorded_from_metadata(
        ctx, primary_slot, turn_id=scratch.turn_id,
    )
    if cost_event is not None:
        scratch.events.append(cost_event)

    # 7. Append the tool result back into the messages list so the LLM
    #    can see it on the next iteration.
    messages.append(
        _tool_result_message(call_id=call_id, content=_render_bundle_for_llm(bundle)),
    )
    return "continue"


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
    of the tool-use loop.
    """
    trigger = packet.trigger_event
    action_id = trigger.payload.get("action_id")
    if not isinstance(action_id, str):
        LOGGER.warning("action.result_observed missing action_id — no-op")
        return DecideResult(
            response_plan=None,
            events_emitted=tuple(scratch.events),
            turn_id=scratch.turn_id,
            attention_channel="silent_log",
        )
    # Ask the LLM to compose a final response now that the result is on
    # the trace.
    return _run_tool_use_loop(assemble_packet(trigger, ctx.conn), policy, ctx, scratch)


# --- action.timeout_assumed / action.failed branch -------------------------

# Canonical user-facing limitation phrasings for the action
# terminal-failure paths (B-0003c / ADR-0002 Negative-path appendix).
_TIMEOUT_LIMITATION_TEXT: Final[str] = "Codex 超时，未完成"  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.
_FAILED_LIMITATION_TEXT: Final[str] = "Codex 跑挂了，没新 diff"  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.
# ADR-0008 D9 (Step 4). A cancelled run is a limitation with a different
# cause: nothing broke, somebody stopped it. Matched by the ``r"已停止"``
# pattern added alongside the two above.
_CANCELLED_LIMITATION_TEXT: Final[str] = "任务已停止，未完成"  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.

_ACTION_TERMINAL_LIMITATION_TEXT: Final[Mapping[str, str]] = {
    "action.timeout_assumed": _TIMEOUT_LIMITATION_TEXT,
    "action.failed": _FAILED_LIMITATION_TEXT,
    "action.cancelled": _CANCELLED_LIMITATION_TEXT,
}


def _handle_action_terminal_failure(
    packet: SituationPacket,
    policy: EffectivePolicy,  # noqa: ARG001 — kept for branch-signature uniformity with the other _handle_* dispatchers.
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """Process an ``action.timeout_assumed`` / ``failed`` / ``cancelled`` re-entry.

    Recover the ``turn_id`` from the trigger correlation, pick the
    canonical user-facing limitation text for the trigger type and feed
    it to :func:`_finalize_response`, which runs the Pre-emit Gate and
    emits ``turn.ended``.
    """
    trigger = packet.trigger_event
    correlation = trigger.correlation or {}
    turn_id_corr = correlation.get("turn_id") or packet.current_turn_id
    scratch.turn_id = turn_id_corr if isinstance(turn_id_corr, str) else None

    canonical_text = _ACTION_TERMINAL_LIMITATION_TEXT.get(
        trigger.type,
        _FAILED_LIMITATION_TEXT,
    )

    return _finalize_response(canonical_text, packet, ctx, scratch)


def _loop_messages(packet: SituationPacket, ctx: DecideContext) -> list[dict[str, Any]]:
    """Build the tool loop's messages: history, system notes, correction prefix."""
    messages = build_llm_messages(packet)
    _insert_system_notes(messages, packet, ctx)
    if ctx.stream_correction is not None:
        # The failed stream's exposed prefix is the model's own prior text;
        # the correction continues it and never rewrites it (ADR-0008 D3).
        messages.append(_assistant_text_message(ctx.stream_correction.committed_prefix))
    return messages


def _with_correction_prefix(plan: ResponsePlan, ctx: DecideContext) -> ResponsePlan:
    """Prepend a failed stream's exposed prefix to a correction run's plan.

    Same re-hash discipline as the template-line guard in
    :func:`_finalize_response`; a plan that already starts with the prefix
    is returned as is.
    """
    correction = ctx.stream_correction
    if correction is None:
        return plan
    voice, document, enveloped = split_envelope(plan.text)
    if voice.startswith(correction.committed_prefix):
        return plan
    joined = correction.committed_prefix + voice
    corrected = compose_envelope(joined, document) if enveloped else joined
    return replace(
        plan,
        text=corrected,
        response_hash=hashlib.sha256(corrected.encode("utf-8")).hexdigest(),
    )


# --- ADR-0008 Step 8: routine streaming route -------------------------------


@dataclass(frozen=True)
class _StreamedText:
    """One provider stream's outcome: exposed prefix, remaining tail, envelope."""

    prefix: str
    suffix: str
    document: str
    enveloped: bool
    emitted_segments: int
    last_gate_event_uid: str | None


def _stream_routine_text(  # noqa: C901 - one provider stream feeding one gate loop
    ctx: DecideContext,
    route: RoutineStreamRoute,
    messages: list[dict[str, Any]],
    scratch: _Scratch,
    *,
    gate_segments: bool,
) -> _StreamedText:
    """Stream one no-tool answer, permitting and exposing sentences until sealed.

    Every delta passes the envelope splitter first, so the assembler and the
    durable chunks only ever see tag-free voice text. A denied candidate seals
    the run (D2 rule 2); everything after the exposed prefix is the suffix the
    finalizer judges. ``gate_segments=False`` regenerates a suffix only.
    """
    _check_response_cancelled(ctx, "before provider request")
    if ctx.request_admission is not None:
        ctx.request_admission("decision")
    cost_recorder = CostRecorder(
        ctx.conn,
        pricing_table=_pricing_table(),
        committed_event_bus=route.committed_event_bus,
    )
    stream = route.open_stream(
        cost_recorder.stream_events(
            ctx.llm_client,
            messages=messages,
            system=ctx.system_prompt,
            tools=None,
            kind="decision",
            turn_id=scratch.turn_id,
        ),
    )
    splitter = StreamEnvelopeSplitter()
    assembler = SemanticAssembler()
    classifier = SegmentRiskClassifier()
    prefix = ""
    voice = ""
    emitted = 0
    sealed = not gate_segments
    last_gate: str | None = None
    failed: LLMResponseFailed | None = None

    def admit(candidate: SemanticCandidate) -> bool:
        nonlocal emitted, last_gate, prefix, sealed
        with route.segment_guard():
            outcome = stream_emission_gate(
                ctx.conn,
                policy=route.policy,
                context=route.context,
                segment=candidate,
                sequence=emitted,
                phase="final",
                channel="both",
                classifier=classifier,
                committed_event_bus=route.committed_event_bus,
            )
            scratch.events.append(outcome.event)
            last_gate = outcome.event.event_uid
            if outcome.permit is None:
                sealed = True
                return False
            scratch.events.append(route.emit_segment(outcome.permit, candidate.text))
        prefix += candidate.text
        emitted += 1
        return True

    def assemble(text: str, *, final: bool = False) -> None:
        if sealed or assembler.blocked_reason is not None:
            return
        candidates = assembler.finish() if final else assembler.feed(text)
        for candidate in candidates:
            if not admit(candidate):
                return

    try:
        for event in stream:
            _check_response_cancelled(ctx, "while streaming")
            if isinstance(event, LLMTextDelta):
                safe = splitter.feed(event.text)
                voice += safe
                assemble(safe)
            elif isinstance(event, LLMResponseFailed):
                failed = event
    finally:
        stream.close()
    _check_response_cancelled(ctx, "after provider stream")
    if failed is not None:
        message = f"routine stream failed before completion ({failed.error_code})"
        raise RuntimeError(message)
    tail = splitter.finish()
    voice += tail.voice_tail
    assemble(tail.voice_tail)
    assemble("", final=True)
    return _StreamedText(
        prefix=prefix,
        suffix=voice[len(prefix) :],
        document=tail.document,
        enveloped=tail.enveloped,
        emitted_segments=emitted,
        last_gate_event_uid=last_gate,
    )


def _comparable(text: str) -> str:
    """The text's identity for prefix comparison: no case folding, no punctuation."""
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", text)
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def _without_repeated_prefix(prefix: str, suffix: str) -> str:
    """Drop a regenerated tail's restatement of the prefix already committed.

    A3(b): the one ``gate_segments=False`` regeneration is prompted with the
    exposed prefix as the model's own prior turn, and a model that repeats it
    would make ``ResponsePlan.text`` say the same sentence twice. One pass,
    and a repeat only counts when it ends where the text does: a regeneration
    that opens with the prefix and then runs straight on into a longer word is
    not a restatement, and cutting inside that word would corrupt what the
    model actually wrote.
    """
    target = _comparable(prefix)
    if not target:
        return suffix
    matched = 0
    for index, char in enumerate(suffix):
        piece = _comparable(char)
        if not piece:
            continue
        if target[matched : matched + len(piece)] != piece:
            return suffix
        matched += len(piece)
        if matched != len(target):
            continue
        cut = index + 1
        if cut < len(suffix) and _comparable(suffix[cut]):
            return suffix
        while cut < len(suffix) and not _comparable(suffix[cut]):
            cut += 1
        return suffix[cut:]
    return suffix


def _run_routine_stream(
    packet: SituationPacket,
    ctx: DecideContext,
    route: RoutineStreamRoute,
    scratch: _Scratch,
) -> DecideResult:
    """ADR-0008 Step 8: stream a pre-routed casual answer, then finalize it.

    The finalizer writes nothing; a typed failure goes back to the runtime,
    which fails the run with the durable prefix hash and opens a correction
    run. ``suffix_rejected`` earns exactly one suffix regeneration first.
    A stream sealed before its first permit never gets that far: it degrades
    to the ordinary full-text path in place, on the text it already has.
    """
    messages = build_llm_messages(packet)
    _insert_system_notes(messages, packet, ctx)
    streamed = _stream_routine_text(ctx, route, messages, scratch, gate_segments=True)
    if streamed.emitted_segments == 0:
        # D2 rules 1 and 4: a seal before the first permit exposed nothing, so
        # there is no prefix for D3's correction machinery to protect. The text
        # already generated becomes an ordinary full-text candidate on this same
        # run — one generation, judged by the Pre-emit Gate like any answer.
        draft = (
            compose_envelope(streamed.suffix, streamed.document)
            if streamed.enveloped
            else streamed.suffix
        )
        record_realtime_trace(
            "routine_stream_degraded_to_full_text",
            turn_id=scratch.turn_id,
            response_id=route.context.response_id,
            text_characters=len(draft),
        )
        return _finalize_response(draft, packet, ctx, scratch)
    attention = attention_policy(packet)
    response_id = route.context.response_id
    document = streamed.document
    outcome: ResponsePlan | StreamFinalizationFailure
    if attention != route.context.attention_channel:
        outcome = StreamFinalizationFailure(
            response_id,
            "policy_mismatch",
            committed_text_prefix(ctx.conn, response_id).prefix_hash,
            ("attention_channel_differs_from_pinned_policy",),
        )
    else:
        outcome = finalize_stream(
            ctx.conn,
            committed_prefix=streamed.prefix,
            uncommitted_suffix=streamed.suffix,
            policy=route.policy,
            context=route.context,
        )
        if isinstance(outcome, StreamFinalizationFailure) and outcome.reason == "suffix_rejected":
            again = _stream_routine_text(
                ctx,
                route,
                [*messages, _assistant_text_message(streamed.prefix)],
                scratch,
                gate_segments=False,
            )
            document = again.document or document
            outcome = finalize_stream(
                ctx.conn,
                committed_prefix=streamed.prefix,
                uncommitted_suffix=_without_repeated_prefix(streamed.prefix, again.suffix),
                policy=route.policy,
                context=route.context,
            )
    if isinstance(outcome, StreamFinalizationFailure):
        return DecideResult(
            response_plan=None,
            events_emitted=tuple(scratch.events),
            turn_id=scratch.turn_id,
            attention_channel=attention,
            route="casual_or_explanatory",
            emitted_segments=streamed.emitted_segments,
            last_gate_event_uid=streamed.last_gate_event_uid,
            stream_failure=outcome,
        )
    plan = outcome
    if streamed.enveloped:
        text = compose_envelope(plan.text, document)
        plan = replace(
            plan, text=text, response_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    return DecideResult(
        response_plan=plan,
        events_emitted=tuple(scratch.events),
        turn_id=scratch.turn_id,
        attention_channel=attention,
        route="casual_or_explanatory",
        emitted_segments=streamed.emitted_segments,
        last_gate_event_uid=streamed.last_gate_event_uid,
    )


# --- Finalization (Pre-emit Gate + turn.ended) -----------------------------


def emit_turn_ended(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    final_response_hash: str,
    consumed_trigger_event_uid: str,
    source_event_id: str,
) -> Event:
    """Append the turn's closing row; L3 owns its shape wherever it is written.

    The routine streaming route writes it from the runtime after the
    response terminal, since the finalizer itself writes no events.
    """
    return emit_event(
        conn,
        type="turn.ended",
        payload={
            "turn_id": turn_id,
            "final_response_hash": final_response_hash,
            "consumed_trigger_event_uid": consumed_trigger_event_uid,
        },
        source_event_id=source_event_id,
        correlation={"turn_id": turn_id},
    )


def _emit_pre_emit_gate_event(
    ctx: DecideContext,
    scratch: _Scratch,
    *,
    plan: ResponsePlan,
    attempt: int,
) -> Event:
    """Emit one ``gate.evaluated(pre_emit)`` event for a Pre-emit verdict.

    ``attempt`` is always 0 since ADR 0019 removed the retry chain; the
    field stays on the payload so the audit row keeps its shape.
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
    permitted = not plan.downgrade_required
    record_realtime_trace(
        "response_candidate_gate_evaluated",
        turn_id=scratch.turn_id,
        gate_attempt=attempt,
        permitted=permitted,
        permission=plan.permission,
        measurement_semantics="completed_batch_candidate_not_stream_delta",
    )
    if permitted:
        record_realtime_trace(
            "response_candidate_permitted",
            turn_id=scratch.turn_id,
            gate_attempt=attempt,
            permission=plan.permission,
            measurement_semantics="first_permitted_completed_candidate_not_stream_delta",
        )
    return gate_event


def _finalize_response(  # noqa: PLR0913 — draft + the three decide() handles + two keyword routing hints.
    draft_text: str,
    packet: SituationPacket,
    ctx: DecideContext,
    scratch: _Scratch,
    *,
    gate_text: str | None = None,
    document_form: bool = False,
) -> DecideResult:
    """Apply the Pre-emit Gate to a draft, emit gate + turn.ended, return.

    ``gate_text`` (ADR-0011 §12, MUST-FIX 2a): the Tier 0 path
    (`_run_tier0_path`) renders `draft_text` by interpolating TOOL
    OUTPUT — possibly user-controlled, e.g. clipboard content — into
    an Allen-authored template. That interpolated text is quoted data
    Jarvis is reading back on Allen's behalf, not a claim Jarvis is
    making, so a clipboard that happens to contain "已完成" must not
    read as Jarvis inflating completion. When the caller supplies
    ``gate_text`` (the closed, unformatted template literal —
    `Tier0Hit.response_template`), attempt 0 gates THAT instead of
    `draft_text`; `draft_text` — the fully rendered response, content
    and all — still ships to the user unchanged below. The LLM path
    never sets `gate_text`: there the whole draft IS Jarvis's own
    words.

    ``turn.ended.source_event_id`` references the gate event.
    """
    # A draft that thinks aloud before its envelope would carry that reasoning
    # into ``ResponsePlan.text`` (2026-09-12, turn T7d3d8e48: "I have enough to
    # answer. The user originally asked..." reached memory.db while voice_text
    # was clean). Text outside the envelope has no channel; drop it before the
    # gate hashes the draft.
    draft_text = envelope_only(draft_text)

    # Attempt 0 — the verdict on the raw LLM draft (or, on the Tier 0
    # path, on the closed template literal — see `gate_text` in the
    # docstring above).
    text_to_gate = draft_text if gate_text is None else gate_text
    plan = pre_emit_gate(text_to_gate)
    if gate_text is not None:
        # `pre_emit_gate` echoes back whatever text it gated. Ship the
        # fully rendered `draft_text` instead of the template literal,
        # and re-hash so `response_hash` matches what the surface
        # actually renders — the gate reasoned about `gate_text`, but
        # `draft_text` is what's on record from here on (including on
        # the `gate.evaluated` event emitted just below).
        plan = replace(
            plan,
            text=draft_text,
            response_hash=hashlib.sha256(draft_text.encode("utf-8")).hexdigest(),
        )
    last_gate_event = _emit_pre_emit_gate_event(ctx, scratch, plan=plan, attempt=0)

    # ADR-0012 D5: the template line is Allen's only chance to catch a
    # fuzzy-resolved write target before bytes are written (Step 4
    # erratum). Idempotent post-condition: if this turn froze a
    # template_line and the text about to ship doesn't already end with
    # it, append it and re-hash — same re-hash discipline as the
    # `gate_text` override above, so `turn.ended` and the returned plan
    # agree.
    pending_template_line = scratch.pending_confirmation_template_line
    if pending_template_line and not plan.text.endswith(pending_template_line):
        guarded_text = (
            f"{plan.text}\n\n{pending_template_line}"
            if plan.text.strip()
            else pending_template_line
        )
        plan = replace(
            plan,
            text=guarded_text,
            response_hash=hashlib.sha256(guarded_text.encode("utf-8")).hexdigest(),
        )

    # ADR-0008 D3: a correction run delivers the failed stream's exposed
    # prefix unchanged, then its own continuation.
    plan = _with_correction_prefix(plan, ctx)

    # turn.ended. ``source_event_id`` references the gate verdict.
    if scratch.turn_id is not None:
        scratch.events.append(
            emit_turn_ended(
                ctx.conn,
                turn_id=scratch.turn_id,
                final_response_hash=plan.response_hash,
                consumed_trigger_event_uid=packet.trigger_event.event_uid,
                source_event_id=last_gate_event.event_uid,
            ),
        )

    attention = attention_policy(packet, document_form=document_form)

    # ADR-0012 D5: a `confirmation.requested` emitted THIS turn always
    # routes to `ask_confirm` — the same finalize-scan-override pattern
    # as `limitation_emitted` above (a scan of `scratch.events`, not a
    # new `attention_policy()` branch). Placed last so it wins over
    # every other override: the turn's entire content IS the ask, and
    # `ask_confirm` deliberately sits outside the ADR-0009 D4 TTS
    # suppression set (`jarvis.runtime.inherent_loop._TTS_SILENT_CHANNELS`)
    # — the question must be spoken, not swallowed.
    attention = _confirmation_attention_override(scratch, attention)

    return DecideResult(
        response_plan=plan,
        events_emitted=tuple(scratch.events),
        turn_id=scratch.turn_id,
        attention_channel=attention,
    )


def _confirmation_attention_override(
    scratch: _Scratch,
    attention: AttentionChannel,
) -> AttentionChannel:
    """Apply the two ADR-0012 finalize-scan attention overrides, in order.

    Split out of :func:`_finalize_response` purely to keep that
    function's branch count under ruff's C901 threshold — the logic
    itself is unchanged from the inline version.

    1. ``ask_confirm`` — this turn emitted a ``confirmation.requested``
       (D5): the turn's entire content IS the ask, and ``ask_confirm``
       deliberately sits outside the ADR-0009 D4 TTS suppression set —
       the question must be spoken, not swallowed.
    2. ``voice_notify`` — this turn ran
       ``_handle_confirmation_accepted`` / ``_handle_confirmation_rejected``
       (D6): a direct, synchronous reply to a question Allen just
       asked, on every outcome (success, aborted accept, or plain
       rejection) — never silent_log/queue_review.

    Mutually exclusive in practice — one ``decide()`` call either
    STAGES an ask or ANSWERS one, never both — so the order between
    the two checks does not matter.
    """
    if _confirmation_already_requested_this_turn(scratch):
        attention = "ask_confirm"
    if scratch.confirmation_answered_this_turn:
        attention = "voice_notify"
    return attention


def _pre_emit_reasons(plan: ResponsePlan) -> tuple[str, ...]:
    """Render Pre-emit Gate plan into ``reasons`` strings for audit."""
    return (
        f"permission={plan.permission}",
        f"downgrade_required={plan.downgrade_required}",
        f"active_claim_levels={list(plan.active_claim_levels)}",
    )


# --- Helpers ----------------------------------------------------------------


def _format_open_actions_note(packet: SituationPacket) -> str | None:
    """Render the Status Board's open actions as a system note, or None."""
    if not packet.status_board.open_actions:
        return None
    now_ms = int(time.time() * 1000)
    bullets = "\n".join(
        f"- action_id={action.action_id!r}, dispatched "
        f"{max(0, (now_ms - action.dispatched_ts_ms) // 1000)} s ago, no terminal yet"
        for action in packet.status_board.open_actions
    )
    return (
        "[system context] Open actions (Status Board snapshot — dispatched, "
        "not finished):\n"
        f"{bullets}\n"
        "Do not invent action_ids."
    )


def _new_turn_id() -> str:
    """Fresh turn_id (T + 8-hex)."""
    return "T" + uuid.uuid4().hex[:8]


def _new_action_id() -> str:
    """Fresh action_id (A + 8-hex)."""
    return "A" + uuid.uuid4().hex[:8]


def _new_confirmation_id() -> str:
    """Fresh confirmation_id (C + 8-hex) — mirrors T/A above (ADR-0012 D5)."""
    return "C" + uuid.uuid4().hex[:8]


def _now_epoch_ms() -> int:
    """Current epoch time in milliseconds (ADR-0012 D5 ``expires_at_ms``).

    Private copy of the same one-liner in ``jarvis.decision.gates``
    (not exported there either) — trivial enough that importing across
    for it would cost more than it saves.
    """
    return int(time.time() * 1000)


def _action_correlation(action_request: ActionRequest) -> Mapping[str, str]:
    """Build the canonical ``{action_id, run_id?, turn_id?}`` correlation."""
    out: dict[str, str] = {"action_id": action_request.action_id}
    if action_request.run_id is not None:
        out["run_id"] = action_request.run_id
    if action_request.turn_id is not None:
        out["turn_id"] = action_request.turn_id
    return out


def _confirmation_already_requested_this_turn(scratch: _Scratch) -> bool:
    """True iff a ``confirmation.requested`` already rides ``scratch.events``.

    ADR-0012 D4: only the FIRST ``confirm_required`` this turn becomes
    an ask; every later one falls through to the generic gate-refuse
    handling instead. Mirrors ``_finalize_response``'s
    ``limitation_emitted`` scan — a turn-scoped event-log scan, not a
    persisted flag, so it is naturally correct across the tool-use
    loop's iterations without any extra bookkeeping.
    """
    return any(ev.type == "confirmation.requested" for ev in scratch.events)


def _stage_and_request_confirmation(  # noqa: PLR0913 — one keyword per D3 snapshot input; each is load-bearing (ADR-0012 §3 D3), splitting would only relocate the arg list.
    ctx: DecideContext,
    *,
    action_request: ActionRequest,
    arguments: Mapping[str, Any],
    canonical_target: str,
    tool_def: ToolDefinitionLike,
    source_event_id: str,
) -> Event:
    """Freeze the action snapshot, stage its content, emit the ask.

    ADR-0012 §3 D3/D5. Three things happen, in order:

    1. ``content`` (never any other argument) is staged to
       ``ctx.runtime_paths.pending_write_path(confirmation_id)`` and
       hashed — the content itself never rides the event payload
       (§3.3.9 bounded payloads); only ``content_sha256`` /
       ``content_bytes`` / ``content_artifact`` do, folded into
       ``args_meta`` alongside the tool's other (non-content)
       arguments.
    2. The six-key ``action_snapshot`` is assembled: ``tool_name``,
       ``caller``, ``canonical_target`` (bare path, for the template
       line), ``target_entity_ref`` (the ``file:<abs-path>`` form —
       the exact field Step 6 will copy into a minted lease's
       ``allowed_targets``; see ADR-0012's Step 1 erratum),
       ``risk_level``, ``args_meta``.
    3. ``confirmation.requested`` is emitted, carrying the snapshot
       plus ``template_line`` (rendered from the snapshot, never from
       LLM text) and ``expires_at_ms`` (``ctx.confirmation_ttl_ms``
       from now).

    Args:
        ctx: The current DecideContext (supplies ``runtime_paths`` for
            staging and ``confirmation_ttl_ms`` for the TTL).
        action_request: The proposed, not-yet-authorized ActionRequest
            (already carries ``target_entity_ref`` and ``tool_name``).
        arguments: The tool call's raw arguments (parsed LLM JSON) —
            NOT ``action_request.arguments`` specifically so the
            caller can pass the exact dict it already resolved/parsed.
        canonical_target: The resolved bare absolute path (empty
            string if resolution somehow left it unset — defensive
            only; unreachable in practice, see the call site comment).
        tool_def: The tool's definition (for ``risk_level`` — Day-1
            always ``"L3"``, but read off the def rather than
            hardcoded).
        source_event_id: The ``gate.evaluated`` event that produced
            ``confirm_required`` — mirrors every other downstream event
            in this function chaining off the gate verdict that caused
            it (e.g. ``action.authorized``).

    Returns:
        The emitted ``confirmation.requested`` :class:`Event`.
    """
    confirmation_id = _new_confirmation_id()

    content_raw = arguments.get("content")
    content_str = content_raw if isinstance(content_raw, str) else ""
    content_bytes_data = content_str.encode("utf-8")
    content_sha256 = hashlib.sha256(content_bytes_data).hexdigest()
    content_byte_count = len(content_bytes_data)
    artifact_path = ctx.runtime_paths.pending_write_path(confirmation_id)
    artifact_path.write_text(content_str, encoding="utf-8")

    mode_raw = arguments.get("mode")
    mode_str = mode_raw if isinstance(mode_raw, str) else str(mode_raw)

    args_meta: dict[str, Any] = {k: v for k, v in arguments.items() if k != "content"}
    args_meta["content_sha256"] = content_sha256
    args_meta["content_bytes"] = content_byte_count
    args_meta["content_artifact"] = str(artifact_path)

    action_snapshot: dict[str, Any] = {
        "tool_name": action_request.tool_name,
        "caller": action_request.caller_principal.value,
        "canonical_target": canonical_target,
        "target_entity_ref": action_request.target_entity_ref,
        "risk_level": tool_def.risk_level,
        "args_meta": args_meta,
    }

    template_line = _CONFIRMATION_TEMPLATE_LINE.format(
        tool_name=action_request.tool_name,
        canonical_target=canonical_target,
        mode=mode_str,
        content_bytes=content_byte_count,
        risk_level=tool_def.risk_level,
    )
    expires_at_ms = _now_epoch_ms() + ctx.confirmation_ttl_ms

    return emit_event(
        ctx.conn,
        type="confirmation.requested",
        payload={
            "confirmation_id": confirmation_id,
            "action_snapshot": action_snapshot,
            "template_line": template_line,
            "expires_at_ms": expires_at_ms,
        },
        source_event_id=source_event_id,
        correlation=_action_correlation(action_request),
    )


# --- ADR-0012 D6 — the answer path ------------------------------------------
#
# `_handle_confirmation_accepted` is the ONLY function in this module (and,
# by the module-boundary rules at the top of this file, in all of L3) that
# constructs an `AuthorizationLease`. It is reachable from exactly one call
# site: the grammar hook in `_handle_utterance`, itself gated on a live
# PendingConfirmations slot and an exact-sentence grammar hit. No LLM code
# path touches either precondition. That is the load-bearing invariant
# (ADR §3 D6) made structural rather than merely documented.

_LEASE_TTL_MS: Final[int] = 60_000
"""ADR-0012 D2/D6: the lease need only outlive gate + dispatch (seconds, not
the confirmation ask's own minutes-scale TTL)."""

_CONFIRMATION_REJECTED_TEMPLATE: Final[str] = "好，已取消：{template_line}"  # noqa: RUF001 — fullwidth comma/colon are intentional Chinese punctuation.

# ADR-0012 §4 failure-mode table: "content artifact missing/hash mismatch at
# accept -> abort re-proposal, fixed error line". Scrub-safe (no
# 完成/已完成/done/verified) by construction, same discipline as every other
# fixed line in this module.
_CONFIRMATION_CONTENT_MISMATCH_TEXT: Final[str] = "暂存内容校验失败，写入未执行。"  # noqa: RUF001 — fullwidth comma/period are intentional Chinese punctuation.

# Defensive-only: the tool named in a frozen snapshot is no longer
# registered (e.g. the daemon restarted with the tool removed between ask
# and answer). Unreachable in the Day-1 scenario (write_file is the only L3
# tool and registries don't shrink mid-process), kept for the same reason
# `_check_entity_trusted`'s `tool_def is None` arm is kept — a handler must
# be safe standing alone, not merely behind preconditions that happen to
# always hold in production.
_CONFIRMATION_TOOL_GONE_TEXT: Final[str] = "无法执行：工具已不可用，写入未执行。"  # noqa: RUF001 — fullwidth colon/comma/period are intentional Chinese punctuation.

# ADR-0012 §4: "gate refuses the re-proposal (policy/entity drift since ask)
# -> fixed line reporting the refusal reason". Interpolates only the
# GateOutcome literal ("refuse" / "confirm_required") — never raw
# `gate.reasons` strings, which are developer-facing audit text not vetted
# against the Pre-emit Gate's completion-keyword scrub.
_CONFIRMATION_REPROPOSAL_REFUSED_TEMPLATE: Final[str] = (
    "已取消：重新检查未通过（{outcome}），写入未执行。"  # noqa: RUF001 — fullwidth parens/comma/period are intentional Chinese punctuation.
)

# ADR-0012 §4: "write_file handler I/O error -> error observation ->
# Limitation routing (existing machinery)" — `result_interpreter` below
# already emits the Limitation Claim; this is only the direct-reply text
# for the turn that was Allen's own "可以".
_CONFIRMATION_DISPATCH_ERROR_TEMPLATE: Final[str] = "写入执行出错，未写入：{error}"  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.

# ADR-0012 §3 D6 exact wording — "backed by ack semantics; the wording
# deliberately stops at 已执行 and must not be strengthened" (no 完成/
# 已完成/verified/done — see `_COMPLETION_KEYWORDS` in `jarvis.decision.gates`;
# "执行"/"已执行" do not match any of those patterns).
_CONFIRMED_WRITE_SUCCESS_TEMPLATE: Final[str] = (
    "write_file 已执行：`{path}`（{bytes_written} 字节）"  # noqa: RUF001 — fullwidth colon/parens/comma are intentional Chinese punctuation.
)


def _new_lease_id() -> str:
    """Fresh lease_id (L + 8-hex) — mirrors T/A/C above (ADR-0012 D6)."""
    return "L" + uuid.uuid4().hex[:8]


def _record_confirmation_answer(
    slot: PendingConfirmationSlot,
    grammar_hit: ConfirmGrammarHit,
    transcript: str,
    ctx: DecideContext,
    scratch: _Scratch,
) -> Event:
    correlation = {"turn_id": scratch.turn_id} if scratch.turn_id else None
    if ctx.wave1_features.confirmation_dispatch_outbox:
        return answer_confirmation_once(
            ctx.conn, confirmation_id=slot.confirmation_id,
            accepted=grammar_hit.decision == "yes", utterance_raw=transcript,
            grammar_rule_id=grammar_hit.rule_id, correlation=correlation,
        )
    return emit_event(
        ctx.conn,
        type="confirmation.accepted" if grammar_hit.decision == "yes" else "confirmation.rejected",
        payload={"confirmation_id": slot.confirmation_id, "utterance_raw": transcript,
                 "grammar_rule_id": grammar_hit.rule_id},
        source_event_id=_latest_event_uid_of_type(ctx.conn, event_type="confirmation.requested"),
        correlation=correlation,
    )


def _handle_confirmation_rejected(  # noqa: PLR0913 — one keyword per D6 answer-path input; each is load-bearing, splitting would only relocate the arg list.
    slot: PendingConfirmationSlot,
    grammar_hit: ConfirmGrammarHit,
    transcript: str,
    packet: SituationPacket,
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """ADR-0012 D6 no-branch: emit ``confirmation.rejected``, fixed cancel line.

    No lease, no re-proposal, no gate, no dispatch — the entire branch
    is two events (``confirmation.rejected`` here, ``turn.ended`` via
    :func:`_finalize_response`) and a fixed line. The
    PendingConfirmations fold moves the slot to ``state="rejected"``
    on this event (matched by ``confirmation_id``), which is terminal
    — :meth:`PendingConfirmationSlot.is_live` is false for any
    non-``"pending"`` state, so this exact slot can never be answered
    again (a later 「可以」 either hits a NEWER slot or, with none
    pending, is an ordinary utterance).
    """
    try:
        rejected_event = _record_confirmation_answer(slot, grammar_hit, transcript, ctx, scratch)
    except ConfirmationRevalidationError:
        scratch.confirmation_answered_this_turn = True
        return _finalize_response("确认已被处理或失效。未接纳新的写入。", packet, ctx, scratch)
    scratch.events.append(rejected_event)
    scratch.confirmation_answered_this_turn = True

    draft = _CONFIRMATION_REJECTED_TEMPLATE.format(template_line=slot.template_line)
    return _finalize_response(draft, packet, ctx, scratch)


def _handle_confirmation_accepted(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915 — one audited accept/gate/dispatch trace with explicit fail-closed exits.
    slot: PendingConfirmationSlot,
    grammar_hit: ConfirmGrammarHit,
    transcript: str,
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """ADR-0012 D6 yes-branch: accept -> mint lease -> re-propose -> gate -> dispatch.

    This is the ONE function in L3 that constructs an
    ``AuthorizationLease`` (see the section banner above this
    function). Steps, each mirroring the equivalent stage of the
    ordinary LLM-driven dispatch in ``_dispatch_one_tool_call`` (steps
    3-7b) so the audit chain reads the same way regardless of which
    path produced it:

    1. Emit ``confirmation.accepted`` (durable proof of Allen's exact
       words + which grammar rule matched).
    2. Re-read the staged content artifact and verify it against the
       frozen ``content_sha256`` — a mismatch aborts here, before any
       lease is minted or any gate runs (§4 failure mode).
    3. Mint the lease per D2's nine fields, scoped to exactly this
       tool + this ``target_entity_ref`` (the entity-REF form — cross-
       step contract #1: ``allowed_targets`` must NOT hold the bare
       ``canonical_target`` path, or every re-proposal would refuse as
       wrong-target).
    4. Rebuild the ActionRequest from the FROZEN snapshot with a NEW
       ``action_id`` and the lease attached, emit ``action.proposed``.
    5. Run the FULL ``pre_action_gate`` — no shortcuts. The
       ``gate.evaluated`` payload always carries ``lease_id`` when a
       lease is attached (cross-step contract #3), regardless of
       outcome, so the audit trail shows what was attempted even on a
       refusal.
    6. On ``pass``: ``action.authorized`` + lifecycle transition,
       dispatch via the L4 registry, fixed broadcast.
    """
    try:
        accepted_event = _record_confirmation_answer(slot, grammar_hit, transcript, ctx, scratch)
    except ConfirmationRevalidationError:
        scratch.confirmation_answered_this_turn = True
        return _finalize_response("确认已被处理或失效。未接纳新的写入。", packet, ctx, scratch)
    scratch.events.append(accepted_event)
    scratch.confirmation_answered_this_turn = True

    # --- 2. Re-read + verify the staged content artifact -------------------
    snapshot = slot.snapshot
    args_meta_raw = snapshot.get("args_meta")
    args_meta: Mapping[str, Any] = args_meta_raw if isinstance(args_meta_raw, Mapping) else {}
    content_artifact_raw = args_meta.get("content_artifact")
    expected_sha256_raw = args_meta.get("content_sha256")

    content_text: str | None = None
    if isinstance(content_artifact_raw, str) and isinstance(expected_sha256_raw, str):
        artifact_path = Path(content_artifact_raw)
        if artifact_path.is_file():
            content_bytes_data = artifact_path.read_bytes()
            if hashlib.sha256(content_bytes_data).hexdigest() == expected_sha256_raw:
                content_text = content_bytes_data.decode("utf-8")

    if content_text is None:
        return _finalize_response(_CONFIRMATION_CONTENT_MISMATCH_TEXT, packet, ctx, scratch)

    tool_name_raw = snapshot.get("tool_name")
    tool_name = tool_name_raw if isinstance(tool_name_raw, str) else ""
    tool_def = _find_tool_def(ctx.tool_registry, tool_name)
    if tool_def is None:
        return _finalize_response(_CONFIRMATION_TOOL_GONE_TEXT, packet, ctx, scratch)

    target_entity_ref_raw = snapshot.get("target_entity_ref")
    target_entity_ref = (
        target_entity_ref_raw if isinstance(target_entity_ref_raw, str) else None
    )

    # --- 3. Mint the lease (D2 nine fields) ---------------------------------
    # Cross-step contract #1 (Step 1 erratum): `allowed_targets` holds the
    # entity-ref form (`target_entity_ref`, e.g. "file:/abs/path") — the
    # exact field `_lease_scope_permits` matches byte-equal against
    # `ActionRequest.target_entity_ref`. NOT `canonical_target` (the bare
    # path) — that field exists only for the human-readable template line.
    reproposal_arguments: dict[str, Any] = {
        k: v
        for k, v in args_meta.items()
        if k not in ("content_sha256", "content_bytes", "content_artifact")
    }
    reproposal_arguments["content"] = content_text

    identity = stable_authorization_identity(accepted_event.event_uid)
    atomic_dispatch = ctx.wave1_features.confirmation_dispatch_outbox
    lease: AuthorizationLease = {
        "lease_id": identity.lease_id if atomic_dispatch else _new_lease_id(),
        "granted_by": "allen",
        "granted_to": CallerPrincipal.JARVIS_LLM,
        "allowed_tools": frozenset({tool_name}),
        "allowed_targets": (
            frozenset({target_entity_ref}) if target_entity_ref is not None else frozenset()
        ),
        "expires_at_ms": _now_epoch_ms() + _LEASE_TTL_MS,
        "max_uses": 1,
        "reason": slot.template_line,
        "source_confirmation_event_id": accepted_event.event_uid,
    }

    # --- 4. Deterministic re-proposal ---------------------------------------
    action_id = identity.action_id if atomic_dispatch else _new_action_id()
    action_request = ActionRequest(
        action_id=action_id,
        tool_name=tool_name,
        target_entity_ref=target_entity_ref,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level=tool_def.risk_level,
        arguments=reproposal_arguments,
        authorization_lease=lease,
        run_id=None,
        turn_id=scratch.turn_id,
    )
    proposed_event = emit_event(
        ctx.conn,
        type="action.proposed",
        payload={
            "action_id": action_id,
            "tool_name": tool_name,
            "caller_principal": CallerPrincipal.JARVIS_LLM.value,
            "risk_level": tool_def.risk_level,
            "target_entity_ref": target_entity_ref,
            "turn_id": scratch.turn_id,
            "arguments": dict(reproposal_arguments),
        },
        source_event_id=accepted_event.event_uid,
        correlation=_action_correlation(action_request),
    )
    scratch.events.append(proposed_event)

    # --- 5. FULL Pre-action Gate — no shortcuts -----------------------------
    # `packet.pending_confirmation` is STALE here: it was folded at the top
    # of `_handle_utterance`, BEFORE `confirmation.accepted` (step 1 above)
    # was durably appended — its slot's `accepted_event_uid` is still None.
    # D2.4's single-use check joins on exactly that field (cross-step
    # contract #2), so the gate needs a projection that has already seen
    # THIS turn's acceptance. Re-fold fresh off the log — the event is
    # already durable; only the caller's cached VIEW of it needs a refresh.
    pending_confirmations = make_snapshot(ctx.conn).pending_confirmations
    gate = pre_action_gate(
        action_request,
        policy,
        tool_def=tool_def,
        pending_confirmations=pending_confirmations,
    )
    gate_payload: dict[str, Any] = {
        "gate": "pre_action",
        "outcome": gate.outcome,
        "reasons": list(gate.reasons),
        "check_results": dict(gate.check_results),
        "action_id": action_id,
        # Cross-step contract #3: MUST be present whenever a lease is
        # attached, on every outcome — the D2.4 consumption fold
        # (`jarvis.state.projections._fold_pending_confirmations`) only
        # ever closes on a `pass` carrying this key, and C5's replay
        # scenario needs it on a `refuse` too for the audit trail.
        "lease_id": lease["lease_id"],
    }
    if atomic_dispatch and gate.outcome == "pass":
        try:
            authorization = authorize_confirmation_dispatch(
                ctx.conn, source_confirmation_event_id=accepted_event.event_uid,
                action_request=action_request, lease=lease, gate_payload=gate_payload,
                correlation=_action_correlation(action_request),
            )
        except ConfirmationRevalidationError:
            return _finalize_response("确认已被处理或失效。未接纳新的写入。", packet, ctx, scratch)
        if isinstance(authorization, AlreadyConsumed):
            return _finalize_response("该确认已接纳。执行状态请以结果为准。", packet, ctx, scratch)
        gate_event = authorization.gate_event
    else:
        gate_event = emit_event(
        ctx.conn,
        type="gate.evaluated",
        payload=gate_payload,
        source_event_id=proposed_event.event_uid,
        correlation=_action_correlation(action_request),
    )
    scratch.events.append(gate_event)

    if gate.outcome != "pass":
        draft = _CONFIRMATION_REPROPOSAL_REFUSED_TEMPLATE.format(outcome=gate.outcome)
        return _finalize_response(draft, packet, ctx, scratch)

    # --- 6. action.authorized + lifecycle, dispatch, interpret -------------
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

    record_realtime_trace(
        "action_dispatch_started",
        turn_id=scratch.turn_id,
        action_id=action_id,
        tool_name=tool_name,
    )
    _check_response_cancelled(ctx, "before tool dispatch")
    try:
        bundle = ctx.tool_registry.dispatch(
            action_request, ctx.conn, ctx.runtime_paths, ctx.lifecycle,
        )
    except AuthorizedDispatchAlreadyStarted:
        return _finalize_response("该确认已接纳。执行状态请以结果为准。", packet, ctx, scratch)
    record_realtime_trace(
        "action_dispatch_returned",
        turn_id=scratch.turn_id,
        action_id=action_id,
        tool_name=tool_name,
        result_slots=len(bundle.slots),
    )
    record_realtime_trace(
        "action_result_available",
        turn_id=scratch.turn_id,
        action_id=action_id,
        tool_name=tool_name,
        result_source="synchronous_confirmation_dispatch_return",
    )
    primary_result_slot = bundle.slots[0]

    if primary_result_slot.error is not None:
        draft = _CONFIRMATION_DISPATCH_ERROR_TEMPLATE.format(error=primary_result_slot.error)
        return _finalize_response(draft, packet, ctx, scratch)

    path_written = primary_result_slot.payload.get("path", "?")
    bytes_written = primary_result_slot.payload.get("bytes_written", "?")
    draft = _CONFIRMED_WRITE_SUCCESS_TEMPLATE.format(
        path=path_written, bytes_written=bytes_written,
    )
    return _finalize_response(draft, packet, ctx, scratch)


def _find_tool_def(
    registry: ToolRegistryLike,
    name: str,
) -> ToolDefinitionLike | None:
    """Locate a tool definition by name (JARVIS_LLM-scoped surface)."""
    for tool_def in registry.for_caller(CallerPrincipal.JARVIS_LLM):
        if tool_def.name == name:
            return tool_def
    return None


def _find_registered_tool_def(
    registry: ToolRegistryLike,
    name: str,
) -> ToolDefinitionLike | None:
    """Locate a tool definition by name across EVERY registered tool.

    Tier 0's lookup must cover at least what ``validate_tier0_table``
    certifies at bootstrap — the ``regex_router`` surface — which
    :func:`_find_tool_def`'s ``JARVIS_LLM``-scoped scan does not: a
    whitelist entry naming a regex-router-only tool would pass bootstrap
    validation and then vanish at dispatch time, silently degrading to
    the LLM.

    Resolution here is deliberately caller-blind. Whether the principal
    may actually call the tool is the Pre-action Gate's decision, so a
    registered-but-disallowed tool yields an auditable ``refuse`` on
    ``gate.evaluated`` instead of a fall-through with no gate row.
    """
    for tool_def in registry.get_definitions():
        if tool_def.name == name:
            return tool_def
    return None


def _allowed_tool_surface(
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
    "EntityResolverLike",
    "GateOutcome",
    "GateResult",
    "LifecycleLike",
    "PreEmitPermission",
    "ResolvedEntityLike",
    "ResponsePlan",
    "RuntimePathsLike",
    "SituationPacket",
    "ToolDefinitionLike",
    "ToolRegistryLike",
    "attention_policy",
    "decide",
    "effective_policy",
    "emit_turn_ended",
    "pre_action_gate",
    "pre_emit_gate",
]
