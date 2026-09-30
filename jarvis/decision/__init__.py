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
import re
import time
import unicodedata
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from jarvis.decision.confirm_grammar import ConfirmGrammarHit, match_confirm_grammar
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
from jarvis.decision.llm_stream import LLMResponseFailed, LLMTextDelta, LLMToolCallCompleted
from jarvis.decision.packet import (
    SituationPacket,
    assemble_packet,
    format_pending_clarification_note,
    format_pending_confirmation_note,
)
from jarvis.decision.policy import EffectivePolicy, effective_policy, surface_for
from jarvis.decision.pre_route import SPOKEN_CHANNELS
from jarvis.decision.response_run import ResponseCancelledError
from jarvis.decision.stream_envelope import (
    StreamEnvelopeSplitter,
    compose_envelope,
    envelope_only,
    split_envelope,
)
from jarvis.decision.stream_finalize import StreamFinalizationFailure, finalize_stream
from jarvis.decision.stream_gate import stream_emission_gate
from jarvis.decision.stream_risk import SPOKEN_RULE_VERSION, SegmentRiskClassifier
from jarvis.decision.stream_sentences import SemanticAssembler
from jarvis.decision.tier0 import render_tier0_response
from jarvis.shared import (
    ActionRequest,
    CallerPrincipal,
    Event,
    RawResult,
    RawResultBundle,
)
from jarvis.shared.lang import action, letter_to, t
from jarvis.shared.pricing import compute_cost_usd, load_pricing_table
from jarvis.shared.realtime import AlreadyConsumed, Wave1FeatureFlags, stable_authorization_identity
from jarvis.shared.realtime_trace import realtime_trace_context, record_realtime_trace
from jarvis.shared.text import is_english, truncate_utf8
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
from jarvis.state.turn_overlap import TurnInFlight, turns_in_flight, words_since

if TYPE_CHECKING:
    import sqlite3

    from jarvis.decision.confirm_grammar import ConfirmGrammarTable
    from jarvis.decision.llm import ChatResult, LLMClient
    from jarvis.decision.llm_stream import LLMStreamHandle
    from jarvis.decision.pre_route import RoutineStreamRoute, StreamCorrection
    from jarvis.decision.stream_envelope import EnvelopeTail
    from jarvis.decision.stream_sentences import SemanticCandidate
    from jarvis.decision.tier0 import Tier0Hit, Tier0Table
    from jarvis.shared import AuthorizationLease, RiskLevel
    from jarvis.state.committed_event_bus import CommittedEventBus
    from jarvis.state.conversation import PresentationRecord
    from jarvis.state.projections import PendingConfirmationSlot

LOGGER = logging.getLogger(__name__)

# Hard ceiling on the tool-use loop in `decide()`. Defends against an LLM
# that keeps proposing tool calls without converging; `llm.max_tool_iterations`
# replaces it (ADR 0060).
DEFAULT_MAX_TOOL_ITERATIONS = 5

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
        if ctx.wave1_features.exactly_once_cost_accounting
        else None
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
            ctx.conn,
            run_id=run_id,
            payload=payload,
            correlation=correlation or None,
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

    @property
    def deferred(self) -> bool:
        """ADR 0034: off the model's tool list until a search loads it."""
        ...

    @property
    def requires_confirmation(self) -> bool:
        """ADR 0062: a call puts up a card and waits for Allen's button."""
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
    max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS
    tier0_table: Tier0Table | None = None
    entity_resolver: EntityResolverLike | None = None
    write_entity_resolver: EntityResolverLike | None = None
    confirmation_ttl_ms: int = _DEFAULT_CONFIRMATION_TTL_MS
    confirm_grammar_table: ConfirmGrammarTable = ()
    wave1_features: Wave1FeatureFlags = field(default_factory=Wave1FeatureFlags)
    cancellation_checkpoint: Callable[[str], None] | None = None
    request_admission: Callable[[str], None] | None = None
    # The history (current summary, then one message per record, by role)
    # and the per-turn time line, rendered by the composition root from
    # memory.db. The history goes first in the prompt and only grows at
    # its end between compactions; the time line goes after it.
    history: Sequence[Mapping[str, str]] = ()
    time_note: str | None = None
    # The plugins connected at this turn, one line. History replays older
    # answers that said an app was not connected; this line is the current
    # fact beside them.
    connected_apps: str | None = None
    # ADR-0008 Step 8. ``routine_stream`` is the pre-routed streaming seam the
    # runtime bound for this run (None on every other turn, so decide() keeps
    # the batch tool loop). ``stream_correction`` marks a full-text run that
    # continues the exposed prefix of a failed stream.
    routine_stream: RoutineStreamRoute | None = None
    stream_correction: StreamCorrection | None = None
    # docs/plans/slow-results-proposal.md (``realtime.response.slow_results``):
    # a turn is told which earlier ones are still being answered, and a turn
    # Allen spoke past opens its answer by pointing back at his question.
    slow_results: bool = False


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
    # ADR 0034: deferred tools a `loaded_tools` result put on this turn's menu.
    loaded_tools: set[str] = field(default_factory=set)


_STATUS_HEADER: Final[str] = "[Current state | from the program, not the user's words]"

# docs/plans/speak-as-written-proposal.md: on the spoken route each sentence is
# spoken as the model writes it, so the spoken reply comes first and is the
# answer itself. The length default and the explicit-request override are the
# ones the spoken-form rewrite prompt carried (ADR 0099); the line before a
# tool call is spoken when that call is dispatched. It names no tool and gives
# no example of one, so it cannot invite a call.
_SPOKEN_REPLY_NOTE: Final[str] = (
    "Your reply is spoken aloud as you write it. Start with the answer itself, in plain "
    "spoken sentences in the language of the user's words: by default at most about 60 "
    "Chinese characters or 40 English words, with no lists, headings, links, code or other "
    "markup. When the user explicitly asks you to count, read aloud, repeat something "
    "verbatim, go into detail or speak at a given length, say all of it. When the answer has "
    "more that belongs on screen (a list, a table, code, links, figures to read), write the "
    "spoken reply inside <voice></voice> and then the full written answer inside "
    "<document></document>. When you need a tool for what the user asked, first say one "
    "short line in the language of the user's words saying what you are about to do, then "
    "call the tool in the same response; never end your turn on that line."
)


def _interaction_line(packet: SituationPacket, ctx: DecideContext) -> str | None:
    """How this turn reached Jarvis, or None when the trigger carries no channel."""
    channel = packet.trigger_event.payload.get("channel")
    if not isinstance(channel, str) or not channel:
        return None
    if channel == "gpt_live":
        return "Channel: voice (relayed by Live; the answer will be read aloud)"
    if channel not in SPOKEN_CHANNELS:
        return "Channel: text"
    route = ctx.routine_stream
    if route is not None and route.context.route == "spoken":
        return f"Channel: voice\n{_SPOKEN_REPLY_NOTE}"
    return "Channel: voice"


_HEARD_QUOTE_MAX_CHARS: Final[int] = 40
_UNSPOKEN_LINE: Final[str] = (
    "Previous answer: never spoken aloud; it was stopped before it began to play "
    "and was only shown on screen"
)

# Allen did not catch what she said: the whole utterance only asks for it
# again. Matched on the text with spaces and closing punctuation removed.
# 「什么」 needs no question mark: final ASR ended it with 「。」 in the
# 2026-09-29 live test.
_REPEAT_REQUEST_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:你|你刚才|刚才)?说?(?:的是)?(?:什么|啥)[?？]?|[啊嗯哈蛤][?？]"  # noqa: RUF001 — Allen's fullwidth question mark.
    r"|(?:请|麻烦)?你?再说一[遍次]吧?|我?没听清楚?"
    r"|(?:sorry|pardon|what|huh)\??|comeagain\??|(?:can|could)?yousay(?:that|it)again(?:please)?\??"
    r"|say(?:that|it)again(?:please)?\??|i?didn'?t(?:catch|hear)(?:that|you|it)\??",
)


def _unspaced(text: str) -> str:
    return "".join(text.split())


def _repeat_of_last_answer(packet: SituationPacket) -> str | None:
    """The last spoken answer's voice text when Allen only asks to hear it again."""
    transcript = packet.trigger_event.payload.get("transcript")
    if (
        not isinstance(transcript, str)
        or packet.trigger_event.payload.get("channel") == "gpt_live"
        or _REPEAT_REQUEST_RE.fullmatch(re.sub(r"[\s,，。.!！]+", "", transcript.lower())) is None  # noqa: RUF001 — the fullwidth marks are Allen's own punctuation.
    ):
        return None
    history = packet.conversation_history
    turns = history.turns if history is not None else ()
    index = next((i for i, t in enumerate(turns) if t.turn_id == packet.current_turn_id), 0)
    for turn in reversed(turns[:index]):
        for response in reversed(turn.responses):
            voice = split_envelope(response.panel_available)[0].strip()
            if response.phase == "final" and voice:
                return voice
    return None


def _previous_answer_line(
    packet: SituationPacket, in_flight: frozenset[str] = frozenset(),
) -> str | None:
    """Where the last spoken answer stopped, or None when it was heard whole.

    Read from the playback evidence the conversation projection already
    validates. Turns that got no answer, such as a half-sentence dropped
    for the one after it, are looked past: the cut answer before them is
    still the last thing Allen heard. So are turns another run is still
    answering (``in_flight``). Only the facts go in; whether to continue or
    take up the new words is the model's call.
    """
    history = packet.conversation_history
    turns = history.turns if history is not None else ()
    index = next((i for i, t in enumerate(turns) if t.turn_id == packet.current_turn_id), 0)
    lines: list[str] = []
    for turn in reversed(turns[:index]):
        if turn.turn_id in in_flight:
            continue
        finals = [r for r in turn.responses if r.phase == "final"]
        spoken = [r for r in finals if r.panel_available.strip()]
        if spoken and spoken[-1].unspoken:
            lines.append(_UNSPOKEN_LINE)
            continue  # Never played: the last audible answer is further back.
        if spoken:
            cut = _answer_cut_line(spoken[-1])
            if cut is not None:
                lines.append(cut)
            prefix = spoken[-1].spoken_heard
            if prefix is not None and not prefix.submitted_samples and not prefix.complete:
                continue  # A later answer that never started cannot hide the last audible one.
            break
        if finals and not lines:
            lines.append("Previous turn: interrupted before it was answered")
    return "\n".join(lines) or None


def _answer_cut_line(answer: PresentationRecord) -> str | None:
    voice = split_envelope(answer.panel_available)[0].strip()
    prefix = answer.spoken_heard
    # Speech is the voice text split, stripped and cleaned, so the heard
    # prefix of a whole answer differs from it in spacing and markup.
    if prefix is None or prefix.complete or not voice or _unspaced(prefix.text) == _unspaced(voice):
        return None
    heard = prefix.text.strip()
    if prefix.ended == "completed":
        return (
            "Previous answer: playback completed, but some audio was quiet or its audibility "
            "was uncertain; do not describe it as interrupted."
        )
    if not heard:
        if prefix.submitted_samples:
            return (
                "Previous answer: playback started, but the exact stop position is unknown; "
                "do not infer it from the full written answer."
            )
        return "Previous answer: interrupted before any of it was spoken"
    if len(heard) > _HEARD_QUOTE_MAX_CHARS:
        heard = "…" + heard[-_HEARD_QUOTE_MAX_CHARS:]
    return f'Previous answer: interrupted after "{heard}"; the rest was not spoken'


# A turn stuck longer than this is the supervisor's, not the model's to hear about.
_IN_FLIGHT_WINDOW_MS: Final[int] = 5 * 60 * 1000


def _earlier_turns_in_flight(
    packet: SituationPacket, ctx: DecideContext,
) -> tuple[TurnInFlight, ...]:
    """The user's earlier turns another run is still answering (the switch on)."""
    if not ctx.slow_results:
        return ()
    return turns_in_flight(
        ctx.conn,
        trigger_event_uid=packet.trigger_event.event_uid,
        since_ms=_now_epoch_ms() - _IN_FLIGHT_WINDOW_MS,
    )


def _turns_in_flight_line(earlier: tuple[TurnInFlight, ...]) -> str | None:
    """Name the user's earlier questions another turn is still answering."""
    if not earlier:
        return None
    now_ms = _now_epoch_ms()
    asked = "; ".join(
        f'"{turn.words}" (asked {max(0, now_ms - turn.asked_ms) // 1000} s ago)'
        for turn in earlier
    )
    return (
        f"Still being answered in another turn: {asked}. That answer will be spoken "
        "when it is ready. Answer only what the user just said; do not answer or look "
        "up the earlier question again."
    )


def _late_answer_note(packet: SituationPacket, ctx: DecideContext) -> str | None:
    """After a slow tool: what the user said meanwhile, and how to open this answer."""
    if not ctx.slow_results:
        return None
    said = words_since(ctx.conn, trigger_event_uid=packet.trigger_event.event_uid)
    if not said:
        return None
    quoted = "; ".join(f'"{words}"' for words in said)
    return (
        "[Runtime note, not the user's words] While this was being looked up, the user "
        f"went on to say: {quoted}. That is answered separately; do not answer it here. "
        "Open this answer with a few words pointing back to what they asked here, as a "
        "person would when coming back to an earlier question, then give the answer. "
        "Answer in the language the user spoke."
    )


def _add_late_answer_note(
    messages: list[dict[str, Any]], packet: SituationPacket, ctx: DecideContext,
) -> bool:
    """Append the late-answer note once the user has spoken past this turn."""
    note = _late_answer_note(packet, ctx)
    if note is None:
        return False
    messages.append({"role": "user", "content": note})
    return True


def _current_status_block(packet: SituationPacket, ctx: DecideContext) -> str | None:
    """This turn's state under one header, or None when there is nothing to say."""
    earlier = _earlier_turns_in_flight(packet, ctx)
    lines = [
        line
        for line in (
            ctx.time_note,
            _interaction_line(packet, ctx),
            ctx.connected_apps,
            _previous_answer_line(packet, frozenset(turn.turn_id for turn in earlier)),
            _turns_in_flight_line(earlier),
            format_pending_confirmation_note(packet),
            format_pending_clarification_note(packet),
            _format_open_actions_note(packet),
        )
        if line
    ]
    if not lines:
        return None
    return "\n".join((_STATUS_HEADER, *lines))


def _insert_system_notes(
    messages: list[dict[str, Any]],
    packet: SituationPacket,
    ctx: DecideContext,
) -> None:
    """History ahead of the conversation; this turn's state on the user message.

    The history messages are byte-identical between turns except at their
    end, so they sit at the front for the provider's prefix cache (spec
    §10.5). Everything that changes per turn — the time line, the
    interaction mode, a pending confirmation ask, actions still running —
    rides at the head of this turn's own user message under a header that
    says it is not the user's words. A history ending on an unanswered
    user row folds that row in ahead of the header, so a request never
    carries two user messages in a row.
    """
    status = _current_status_block(packet, ctx)
    if status is not None:
        for message in reversed(messages):
            if message.get("role") == "user":
                message["content"] = f"{status}\n\n{message['content']}"
                break
    head = [dict(turn) for turn in ctx.history]
    if head and head[-1]["role"] == "user" and messages and messages[0].get("role") == "user":
        messages[0]["content"] = f"{head.pop()['content']}\n\n{messages[0]['content']}"
    messages[0:0] = head


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
    #   - The grammar is active ONLY for the first utterance after the
    #     ask, within its TTL (`PendingConfirmationSlot.answers_by_words`,
    #     ADR 0062) — later words fall through to ordinary Tier 0 / Tier 2
    #     handling while the card keeps waiting for its button. Its worst
    #     case (a paraphrase like "行吧那就写进去吧", or 「发吧」 ten minutes
    #     on) is the LLM proposing the action again via the ordinary
    #     tool-call path, which produces a FRESH `confirm_required` -> a
    #     fresh `confirmation.requested` that re-asks — never a dispatch.
    #   - A card button (`confirmation_decision` on the intent, ADR 0062)
    #     names the exact confirmation it answers and is Allen's own act on
    #     his own surface, so it needs no grammar and no timing.
    pending_slot = packet.pending_confirmation.slot
    card_decision = trigger.payload.get("confirmation_decision")
    if isinstance(card_decision, Mapping):
        return _handle_card_decision(card_decision, pending_slot, packet, policy, ctx, scratch)
    if pending_slot is not None and pending_slot.answers_by_words(_now_epoch_ms()):
        transcript_raw = trigger.payload.get("transcript", "")
        transcript = transcript_raw if isinstance(transcript_raw, str) else ""
        grammar_hit = match_confirm_grammar(transcript, ctx.confirm_grammar_table)
        if grammar_hit is not None:
            if grammar_hit.decision == "yes":
                return _handle_confirmation_accepted(
                    pending_slot,
                    grammar_hit,
                    transcript,
                    packet,
                    policy,
                    ctx,
                    scratch,
                )
            return _handle_confirmation_rejected(
                pending_slot,
                grammar_hit,
                transcript,
                packet,
                ctx,
                scratch,
            )

    # 「什么?」/「再说一遍」: say the last spoken answer again, word for word,
    # through the Pre-emit Gate and without a model request (spec §17).
    repeated = _repeat_of_last_answer(packet)
    if repeated is not None:
        return _finalize_response(repeated, packet, ctx, scratch)

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
        if ctx.routine_stream.context.route == "spoken":
            return _run_spoken_stream(packet, policy, ctx, ctx.routine_stream, scratch)
        return _run_routine_stream(packet, ctx, ctx.routine_stream, scratch)

    messages = _loop_messages(packet, ctx)
    llm_surface = surface_for(policy, ctx.tool_registry, CallerPrincipal.JARVIS_LLM)
    late_noted = False

    iteration = 0
    while iteration < ctx.max_tool_iterations:
        iteration += 1
        # ADR 0034: the menu is rebuilt per call, so a tool a search loaded is
        # callable from the next call on; only this caller's surface can load.
        tools = _tool_menu(llm_surface, scratch.loaded_tools)
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
                ctx,
                chat_result,
                kind="decision",
                turn_id=scratch.turn_id,
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
            late_noted = late_noted or _add_late_answer_note(messages, packet, ctx)
            continue

        # LLM returned text -> finalize via Pre-emit Gate.
        return _finalize_response(
            chat_result.text or "",
            packet,
            ctx,
            scratch,
            model_answer=True,
        )

    # Tool budget spent. The answer gets its own request: one more call with no
    # tools, asked to sum up what the tool results already show (ADR 0030).
    LOGGER.warning("decide(): tool-use loop hit max_iterations=%d", ctx.max_tool_iterations)
    answer = _answer_after_tool_budget(ctx, messages, scratch)
    return _finalize_response(
        answer, packet, ctx, scratch, model_answer=answer != t("tool_budget.exhausted"),
    )


# ADR 0030: the tool budget and the answer are separate requests. When the loop
# has spent its tool iterations, the model gets one last request without tools
# to answer from what it already holds; this is the note that request carries.
_TOOL_BUDGET_ANSWER_PROMPT: Final[str] = (
    "[Runtime note, not the user's words] This turn has used all its tool calls; no more "
    "tools can be called. Answer now from the tool results above only: first the facts "
    "you established (with their time and source), then say plainly which parts you "
    "could not find or confirm. Make up nothing without evidence. Answer in the "
    "language the user wrote in."
)
# Spoken when that last request fails or returns nothing (``tool_budget.exhausted``
# in the language table): no internal vocabulary, no completion words.


def _answer_after_tool_budget(
    ctx: DecideContext,
    messages: list[dict[str, Any]],
    scratch: _Scratch,
) -> str:
    """One no-tool request for the answer once the tool iterations are spent.

    The request rides the same cost guard, admission and cancellation checks
    as the loop's calls; a provider failure or an empty reply falls back to a
    fixed Chinese limitation instead of an internal English error. Cancellation
    is never swallowed.
    """
    messages.append({"role": "user", "content": _TOOL_BUDGET_ANSWER_PROMPT})
    iteration = ctx.max_tool_iterations + 1
    try:
        with realtime_trace_context(
            turn_id=scratch.turn_id, request_kind="decision", iteration=iteration
        ):
            chat_result = _run_llm_chat_with_cost_guard(
                ctx,
                messages=messages,
                system=ctx.system_prompt,
                tools=None,
                tool_choice=None,
                kind="decision",
                turn_id=scratch.turn_id,
            )
    except ResponseCancelledError:
        raise
    except Exception:
        LOGGER.exception("decide(): answer request after the tool budget failed")
        return t("tool_budget.exhausted")
    scratch.events.append(
        _emit_cost_recorded(ctx, chat_result, kind="decision", turn_id=scratch.turn_id),
    )
    _check_response_cancelled(ctx, "after provider response")
    return (chat_result.text or "").strip() or t("tool_budget.exhausted")


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


# ``tier0.tool_error``: fixed, scrub-safe text for an erroring Tier 0 tool. MUST stay free of
# completion-class keywords (完成 / 已完成 / done / verified): a Tier 0
# draft that trips the Pre-emit Gate's downgrade path would re-prompt
# the LLM, which is exactly what this path exists to avoid. The
# handler's own error tag is a developer string and goes to the log,
# never to the voice surface.

# ADR-0012 §3 D5 — synthetic tool result injected on the FIRST
# `confirm_required` this turn (the ask). Never sent to the LLM within
# THIS `decide()` call (the tool loop ends right after), but kept
# JSON-shaped for consistency with every other synthetic tool result
# in this module.
_CONFIRM_REQUIRED_TOOL_RESULT_TEXT: Final[str] = (
    "Waiting for the user's confirmation; it cannot run this turn"
)

# ADR-0012 §3 D5 — the exact rendered action line (``confirm.ask_write``), appended by the
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
# ADR 0033 / 0062: every other tool at the threshold gets one spoken line,
# rendered from the frozen arguments only; the card shows the arguments.


def _ask_line(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """``confirm.ask_letter`` for a letter (to, subject, body), else ``confirm.ask_tool``."""
    to = letter_to(dict(arguments))
    if to is not None:
        return t("confirm.ask_letter", to=to, subject=arguments["subject"])
    return t("confirm.ask_tool", action=action(tool_name)[0])


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
            value_bytes[:max_bytes],
            len(value_bytes),
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
        return _finalize_response(t("tier0.gate_refused"), packet, ctx, scratch)

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
        action_request,
        ctx.conn,
        ctx.runtime_paths,
        ctx.lifecycle,
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
        draft = t("tier0.tool_error")
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


def _dispatch_one_tool_call(  # noqa: PLR0913, PLR0915 — single-pass orchestration of resolver + gate + dispatch; splitting muddles the audit trace.
    *,
    tool_call: object,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
    messages: list[dict[str, Any]],
    lead_in: str | None = None,
) -> _DispatchOutcome:
    """Resolve, gate, and dispatch one LLM-proposed tool call.

    ``lead_in`` is the line the model wrote before this call on a spoken turn;
    it rides ``action.proposed`` for the acknowledge to speak at dispatch.

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
            **({"lead_in": lead_in} if lead_in else {}),
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
        ctx,
        primary_slot,
        turn_id=scratch.turn_id,
    )
    if cost_event is not None:
        scratch.events.append(cost_event)

    # ADR 0034: a result naming `loaded_tools` puts them on this turn's menu.
    loaded = primary_slot.payload.get("loaded_tools") if primary_slot.error is None else None
    if isinstance(loaded, list):
        scratch.loaded_tools.update(str(n) for n in loaded)

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
            json.loads(slot.tool_output) if slot.tool_output else dict(slot.payload)
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

# Canonical user-facing limitation phrasings (language-table keys) for the
# action terminal-failure paths (B-0003c / ADR-0002 Negative-path appendix).
# ADR-0008 D9 (Step 4): a cancelled run is a limitation with a different
# cause: nothing broke, somebody stopped it.
_ACTION_TERMINAL_LIMITATION_KEY: Final[Mapping[str, str]] = {
    "action.timeout_assumed": "limitation.timeout",
    "action.failed": "limitation.failed",
    "action.cancelled": "limitation.cancelled",
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

    canonical_text = t(_ACTION_TERMINAL_LIMITATION_KEY.get(trigger.type, "limitation.failed"))

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


# OpenAI's citation markup around a tool result the model cites (U+E200, the
# cited turns, U+E201): no surface renders it, and TTS read it aloud as "cite
# turn0search0" (2026-09-30 live run).
_CITATION_RE: Final[re.Pattern[str]] = re.compile("\ue200[^\ue200\ue201]*\ue201")


class _SegmentSpeaker:
    """One run's voice text through the envelope, assembler and stream gate.

    Every delta passes the envelope splitter first, so the assembler and the
    durable chunks only ever see tag-free voice text. A denied candidate seals
    the run (D2 rule 2): ``prefix`` is what was exposed, ``voice`` all the
    voice text written so far. ``gate_segments=False`` exposes nothing.
    """

    def __init__(
        self,
        ctx: DecideContext,
        route: RoutineStreamRoute,
        scratch: _Scratch,
        *,
        classifier: SegmentRiskClassifier,
        gate_segments: bool = True,
    ) -> None:
        """Start with nothing exposed on ``route``'s run."""
        self._ctx = ctx
        self._route = route
        self._scratch = scratch
        self._classifier = classifier
        self._splitter = StreamEnvelopeSplitter()
        self._assembler = SemanticAssembler(first_clause_chars=route.first_clause_chars)
        self._citation = ""  # an open citation, held until it closes
        self.prefix = ""
        self.voice = ""
        self.emitted = 0
        self.sealed = not gate_segments
        self.last_gate: str | None = None

    def feed(self, text: str) -> None:
        """Take one delta; expose every sentence it completes until sealed."""
        text = _CITATION_RE.sub("", self._citation + text)
        cut = text.find("\ue200")
        self._citation, text = (text[cut:], text[:cut]) if cut >= 0 else ("", text)
        safe = self._splitter.feed(text)
        self.voice += safe
        self._assemble(safe)

    def finish(self) -> EnvelopeTail:
        """Flush the held tail and the last fragment; return the envelope's rest."""
        if self._citation:
            # A citation that never closed was the model's own text after all.
            self.feed(self._citation.replace("\ue200", ""))
        tail = self._splitter.finish()
        self.voice += tail.voice_tail
        self._assemble(tail.voice_tail)
        self._assemble("", final=True)
        return tail

    def _assemble(self, text: str, *, final: bool = False) -> None:
        if self.sealed or self._assembler.blocked_reason is not None:
            return
        candidates = self._assembler.finish() if final else self._assembler.feed(text)
        for candidate in candidates:
            if not self._admit(candidate):
                return

    def _admit(self, candidate: SemanticCandidate) -> bool:
        route = self._route
        with route.segment_guard():
            outcome = stream_emission_gate(
                self._ctx.conn,
                policy=route.policy,
                context=route.context,
                segment=candidate,
                sequence=self.emitted,
                phase="final",
                channel="both",
                classifier=self._classifier,
                committed_event_bus=route.committed_event_bus,
            )
            self._scratch.events.append(outcome.event)
            self.last_gate = outcome.event.event_uid
            if outcome.permit is None:
                self.sealed = True
                return False
            self._scratch.events.append(route.emit_segment(outcome.permit, candidate.text))
        self.prefix += candidate.text
        self.emitted += 1
        return True


def _stream_routine_text(
    ctx: DecideContext,
    route: RoutineStreamRoute,
    messages: list[dict[str, Any]],
    scratch: _Scratch,
    *,
    gate_segments: bool,
) -> _StreamedText:
    """Stream one no-tool answer, permitting and exposing sentences until sealed.

    Everything after the exposed prefix is the suffix the finalizer judges.
    ``gate_segments=False`` regenerates a suffix only.
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
    speaker = _SegmentSpeaker(
        ctx, route, scratch, classifier=SegmentRiskClassifier(), gate_segments=gate_segments,
    )
    failed: LLMResponseFailed | None = None
    try:
        for event in stream:
            _check_response_cancelled(ctx, "while streaming")
            if isinstance(event, LLMTextDelta):
                speaker.feed(event.text)
            elif isinstance(event, LLMResponseFailed):
                failed = event
    finally:
        stream.close()
    _check_response_cancelled(ctx, "after provider stream")
    if failed is not None:
        message = f"routine stream failed before completion ({failed.error_code})"
        raise RuntimeError(message)
    tail = speaker.finish()
    return _StreamedText(
        prefix=speaker.prefix,
        suffix=speaker.voice[len(speaker.prefix) :],
        document=tail.document,
        enveloped=tail.enveloped,
        emitted_segments=speaker.emitted,
        last_gate_event_uid=speaker.last_gate,
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
        return _finalize_response(draft, packet, ctx, scratch, model_answer=True)
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
            plan,
            text=text,
            response_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
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


# --- Prefix warm ------------------------------------------------------------

# Allen's next turn sends this history, then his words. OpenAI's cache
# answered each turn's first request only up to the system prompt and tools
# (~6k of ~45k tokens, 2026-09-30): a cached prompt is found where an earlier
# request ended, and no earlier request ended where the history does (each
# turn's own words carried its state lines, and its tool calls came after).
# One request that ends exactly there, capped at the provider's minimum output,
# is that earlier request.
PREFIX_WARM_MAX_OUTPUT_TOKENS: Final[int] = 16


def open_prefix_warm(  # noqa: PLR0913 — the next request's whole prefix, plus its accounting.
    conn: sqlite3.Connection,
    *,
    llm_client: LLMClient,
    system_prompt: str,
    history: Sequence[Mapping[str, str]],
    tool_registry: ToolRegistryLike,
    responses: bool,
    committed_event_bus: CommittedEventBus | None = None,
) -> LLMStreamHandle | None:
    """The next turn's first request up to its new message, as a request of its own.

    Same system prompt, same tools and the same history messages as
    :func:`_loop_messages` puts ahead of the next user message. A history that
    ends on Allen's unanswered words is sent without them: the next turn folds
    them into its own message. ``None`` when there is no history to send.
    """
    messages = [dict(turn) for turn in history]
    if messages and messages[-1]["role"] == "user":
        messages.pop()
    if not messages:
        return None
    policy = effective_policy(_allowed_tool_surface(tool_registry))
    tools = _tool_menu(surface_for(policy, tool_registry, CallerPrincipal.JARVIS_LLM), ())
    cost_recorder = CostRecorder(
        conn, pricing_table=_pricing_table(), committed_event_bus=committed_event_bus,
    )
    return cost_recorder.stream_events(
        llm_client,
        messages=messages,
        system=system_prompt,
        tools=tools,
        kind="prefix_warm",
        turn_id=None,
        responses=responses,
        max_output_tokens=PREFIX_WARM_MAX_OUTPUT_TOKENS,
    )


# --- The spoken route (docs/plans/speak-as-written-proposal.md) -------------


@dataclass
class _SpokenReply:
    """One streamed request of a spoken turn, by phase, and its calls.

    ``text`` is the ``commentary`` line (``_assistant_message_for`` echoes it);
    ``answer`` the rest, which went to the speaker as it came.
    """

    text: str = ""
    answer: str = ""
    tool_calls: list[LLMToolCallCompleted] = field(default_factory=list)


def _stream_spoken_request(  # noqa: C901, PLR0913 - one request, the turn's seams, one event switch
    ctx: DecideContext,
    route: RoutineStreamRoute,
    speaker: _SegmentSpeaker,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    scratch: _Scratch,
) -> _SpokenReply:
    """Stream one request; answer text reaches the speaker as it arrives."""
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
            tools=tools,
            kind="decision",
            turn_id=scratch.turn_id,
            responses=True,
        ),
    )
    reply = _SpokenReply()
    failed: LLMResponseFailed | None = None
    try:
        for event in stream:
            _check_response_cancelled(ctx, "while streaming")
            if isinstance(event, LLMTextDelta) and event.phase == "commentary":
                if not reply.text:
                    record_realtime_trace("spoken_line_first_text", turn_id=scratch.turn_id)
                reply.text += event.text
            elif isinstance(event, LLMTextDelta):
                if not reply.answer:
                    record_realtime_trace("spoken_answer_first_text", turn_id=scratch.turn_id)
                reply.answer += event.text
                speaker.feed(event.text)
            elif isinstance(event, LLMToolCallCompleted):
                if not reply.tool_calls:
                    record_realtime_trace(
                        "spoken_call_written", turn_id=scratch.turn_id, tool_name=event.name,
                    )
                reply.tool_calls.append(event)
            elif isinstance(event, LLMResponseFailed):
                failed = event
    finally:
        stream.close()
    _check_response_cancelled(ctx, "after provider stream")
    if failed is not None:
        message = f"spoken stream failed before completion ({failed.error_code})"
        raise RuntimeError(message)
    return reply


def _run_spoken_stream(  # noqa: C901 - one request loop: calls, one continuation, the budget
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    route: RoutineStreamRoute,
    scratch: _Scratch,
) -> DecideResult:
    """A turn Allen spoke: every request streams and the answer is spoken as written.

    Answer text goes through the envelope, assembler and stream gate under the
    spoken rule version. The ``commentary`` line before a call rides that
    call's ``action.proposed`` for the acknowledge to speak at dispatch. A
    response that ends on such a line with no call gets one more request.
    """
    messages = _loop_messages(packet, ctx)
    llm_surface = surface_for(policy, ctx.tool_registry, CallerPrincipal.JARVIS_LLM)
    speaker = _SegmentSpeaker(
        ctx, route, scratch, classifier=SegmentRiskClassifier(rule_version=SPOKEN_RULE_VERSION),
    )
    line: str | None = None
    continued = late_noted = False
    for iteration in range(1, ctx.max_tool_iterations + 1):
        tools = _tool_menu(llm_surface, scratch.loaded_tools)
        with realtime_trace_context(
            turn_id=scratch.turn_id, request_kind="decision", iteration=iteration,
        ):
            reply = _stream_spoken_request(ctx, route, speaker, messages, tools, scratch)
        line = reply.text.strip() or line
        if reply.tool_calls:
            message = _assistant_message_for(reply)
            if reply.text.strip():
                message["phase"] = "commentary"
            messages.append(message)
            for tool_call in reply.tool_calls:
                _check_response_cancelled(ctx, "before tool proposal")
                _dispatch_one_tool_call(
                    tool_call=tool_call,
                    policy=policy,
                    ctx=ctx,
                    scratch=scratch,
                    messages=messages,
                    lead_in=line,
                )
            line = None
            ending = _turn_ending_draft(scratch, reply.text)
            if ending is not None:
                return _finish_spoken(packet, ctx, route, speaker, scratch, draft=ending)
            packet = assemble_packet(packet.trigger_event, ctx.conn)
            late_noted = late_noted or _add_late_answer_note(messages, packet, ctx)
            continue
        if reply.text.strip() and not reply.answer.strip() and not continued:
            # It said what it would do and stopped: one more request to do it.
            messages.append({"role": "assistant", "content": reply.text, "phase": "commentary"})
            continued = True
            continue
        if not reply.answer.strip():
            speaker.feed(reply.text)  # its line was all it said, so that is the answer
        return _finish_spoken(packet, ctx, route, speaker, scratch)

    # Tool budget spent (ADR 0030): one more request, without tools, for the answer.
    LOGGER.warning("decide(): spoken loop hit max_iterations=%d", ctx.max_tool_iterations)
    messages.append({"role": "user", "content": _TOOL_BUDGET_ANSWER_PROMPT})
    try:
        reply = _stream_spoken_request(ctx, route, speaker, messages, None, scratch)
    except ResponseCancelledError:
        raise
    except Exception:
        LOGGER.exception("decide(): answer request after the tool budget failed")
        reply = _SpokenReply()
    if not (reply.answer + reply.text).strip():
        speaker.feed(t("tool_budget.exhausted"))
    elif not reply.answer.strip():
        speaker.feed(reply.text)
    return _finish_spoken(packet, ctx, route, speaker, scratch)


def _finish_spoken(  # noqa: PLR0913 - the turn's handles plus the ask that may end it
    packet: SituationPacket,
    ctx: DecideContext,
    route: RoutineStreamRoute,
    speaker: _SegmentSpeaker,
    scratch: _Scratch,
    *,
    draft: str | None = None,
) -> DecideResult:
    """Close a spoken turn: full text when nothing was exposed, else the stream's plan.

    ``draft`` is a confirmation ask that ended the turn. Nothing is rewritten:
    with no sentence exposed the text becomes an ordinary full-text answer on
    this run; otherwise the unexposed rest is judged as the stream's suffix.
    """
    tail = speaker.finish()
    suffix = speaker.voice[len(speaker.prefix) :]
    if speaker.emitted == 0:
        if draft is None:
            draft = compose_envelope(suffix, tail.document) if tail.enveloped else suffix
        return _finalize_response(draft, packet, ctx, scratch)
    if draft is not None:
        suffix = f"{suffix}\n\n{draft}" if suffix.strip() else f"\n\n{draft}"
    attention = attention_policy(packet)
    response_id = route.context.response_id
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
            committed_prefix=speaker.prefix,
            uncommitted_suffix=suffix,
            policy=route.policy,
            context=route.context,
        )
    if isinstance(outcome, StreamFinalizationFailure):
        return DecideResult(
            response_plan=None,
            events_emitted=tuple(scratch.events),
            turn_id=scratch.turn_id,
            attention_channel=attention,
            route="spoken",
            emitted_segments=speaker.emitted,
            last_gate_event_uid=speaker.last_gate,
            stream_failure=outcome,
        )
    plan = outcome
    if tail.enveloped:
        text = compose_envelope(plan.text, tail.document)
        plan = replace(
            plan,
            text=text,
            response_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    return DecideResult(
        response_plan=plan,
        events_emitted=tuple(scratch.events),
        turn_id=scratch.turn_id,
        attention_channel=attention,
        route="spoken",
        emitted_segments=speaker.emitted,
        last_gate_event_uid=speaker.last_gate,
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


# ADR 0045: a spoken answer is a short spoken form, not the written answer read
# aloud. Plain text that already fits the spoken form's own limit (60 Chinese
# characters, about 40 English words) is spoken as written; anything longer, or
# with list / heading / quote / table / code / bold / bracket markup, gets one
# no-tool request for a spoken form. A 38-character self-introduction sent to
# the rewrite on 2026-09-25 came back missing its first sentence.
_SPOKEN_FORM_MAX_PLAIN_CHARS_ZH: Final[int] = 60
_SPOKEN_FORM_MAX_PLAIN_CHARS_EN: Final[int] = 240
_WRITTEN_MARKUP_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:[-*•+]\s|\d+[.)、]|#{1,6}\s|>|\|)|```|\*\*|[(（]",  # noqa: RUF001 — the fullwidth bracket is the Chinese aside being matched.
    re.MULTILINE,
)
# One prompt per language, picked from the script of the user's own words: a
# prompt asked to "keep the original language" still answered an English
# answer in Chinese (smoke run 2026-09-24). ~60 Chinese characters and ~40
# English words are both about 13 s of speech. Neither names Allen: with his
# name in the prompt every spoken form opened with his name. The question goes
# along so the rewrite knows which sentence answers it.
_SPOKEN_FORM_PROMPT_ZH: Final[str] = (
    "Rewrite the answer the user gives you as a spoken reply in Mandarin Chinese, "
    "to be read aloud as is. By default, use at most three sentences and 60 Chinese characters, "
    "only "
    "the conclusion that answers the question and the one or two numbers that matter "
    "most; no lists, headings, brackets, links, code or any markup; add nothing that "
    "is not in the answer. If the question explicitly asks for counting, reading aloud, "
    "verbatim repetition, a detailed explanation, or a specific length, fulfill that request "
    "instead of these summary and length limits: preserve the requested content in order, "
    "including every number or item, removing only visual markup. Do not replace the "
    "requested speech with an acknowledgement or a description of what you will say. "
    "Output only the spoken reply, in Chinese."
)
_SPOKEN_FORM_PROMPT_EN: Final[str] = (
    "Rewrite the answer the user gives you as a spoken English reply: "
    "by default, use at most three sentences and 40 words, only the conclusion that answers the "
    "question and the one or two numbers that matter; no lists, headings, brackets, "
    "links, code or markup; add nothing that is not in the answer. If the question explicitly "
    "asks for counting, reading aloud, verbatim repetition, a detailed explanation, or a "
    "specific length, fulfill that request instead of these summary and length limits: "
    "preserve the requested content in order, including every number or item, removing only "
    "visual markup. Do not replace the requested speech with an acknowledgement or a "
    "description of what you will say. Output only the "
    "spoken reply."
)


def _needs_spoken_form(text: str) -> bool:
    """True for an answer too long or too written to be read aloud as is."""
    limit = _SPOKEN_FORM_MAX_PLAIN_CHARS_EN if is_english(text) else _SPOKEN_FORM_MAX_PLAIN_CHARS_ZH
    return len(text) > limit or _WRITTEN_MARKUP_RE.search(text) is not None


def _spoken_form_request(question: object, answer: str) -> str:
    """The rewrite's one user message: the question it answers, then the answer."""
    if not isinstance(question, str) or not question.strip():
        return answer
    return f"Question: {question.strip()}\n\nAnswer:\n{answer}"


def _with_spoken_form(
    plan: ResponsePlan,
    packet: SituationPacket,
    ctx: DecideContext,
    scratch: _Scratch,
) -> ResponsePlan:
    """ADR 0045: speak a short spoken form, in the language Allen used.

    Asked for when the answer is long or written, or in another language than
    Allen's words this turn. The whole answer moves unchanged to the document
    channel, so the screen, memory.db and the backend history keep it while
    TTS speaks only the voice span. Returned as is: a correction run (its
    prefix is already spoken), a ``gpt_live`` turn (Live paraphrases the
    result itself), an answer the model enveloped on its own, and a short
    plain answer in his language. A failed or empty request keeps the old
    behaviour of speaking the whole answer.
    """
    text = plan.text.strip()
    # Spoken in the language Allen used this turn: the answer may not be
    # (2026-09-24, "What time is it?" copied get_current_time's Chinese
    # spoken_time into a Chinese answer).
    heard = packet.trigger_event.payload.get("transcript")
    english = is_english(heard if isinstance(heard, str) and heard else text)
    if (
        ctx.stream_correction is not None
        or packet.trigger_event.payload.get("channel") == "gpt_live"
        or split_envelope(text)[2]
        or not text
        or (is_english(text) == english and not _needs_spoken_form(text))
    ):
        return plan
    try:
        with realtime_trace_context(turn_id=scratch.turn_id, request_kind="spoken_form"):
            chat_result = _run_llm_chat_with_cost_guard(
                ctx,
                messages=[
                    {"role": "user", "content": _spoken_form_request(heard, text)},
                ],
                system=_SPOKEN_FORM_PROMPT_EN if english else _SPOKEN_FORM_PROMPT_ZH,
                tools=None,
                tool_choice=None,
                kind="decision",
                turn_id=scratch.turn_id,
            )
    except ResponseCancelledError:
        raise
    except Exception:
        LOGGER.exception("decide(): spoken-form request failed; the whole answer is spoken")
        return plan
    scratch.events.append(
        _emit_cost_recorded(ctx, chat_result, kind="decision", turn_id=scratch.turn_id),
    )
    _check_response_cancelled(ctx, "after provider response")
    spoken = (chat_result.text or "").strip()
    if not spoken:
        return plan
    enveloped = compose_envelope(spoken, text)
    return replace(
        plan,
        text=enveloped,
        response_hash=hashlib.sha256(enveloped.encode("utf-8")).hexdigest(),
    )


def _finalize_response(  # noqa: PLR0913 — draft + the three decide() handles + three keyword routing hints.
    draft_text: str,
    packet: SituationPacket,
    ctx: DecideContext,
    scratch: _Scratch,
    *,
    gate_text: str | None = None,
    document_form: bool = False,
    model_answer: bool = False,
) -> DecideResult:
    """Apply the Pre-emit Gate to a draft, emit gate + turn.ended, return.

    ``model_answer`` marks a draft the model wrote as its answer. Only that
    draft may get a spoken form (ADR 0040): fixed L3 text is already written
    to be spoken, and a confirmation ask must be heard word for word.

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
    draft_text = envelope_only(_CITATION_RE.sub("", draft_text))

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

    # ADR 0040: only the model's own answer that will be spoken gets a spoken
    # form, never a Tier 0 read-back (quoted tool output), fixed L3 text or a
    # confirmation ask.
    if attention == "voice_notify" and model_answer:
        plan = _with_spoken_form(plan, packet, ctx, scratch)

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
    """One line for the actions still running, or None when nothing is."""
    open_actions = packet.status_board.open_actions
    if not open_actions:
        return None
    now_ms = int(time.time() * 1000)
    oldest_s = max(0, (now_ms - min(action.dispatched_ts_ms for action in open_actions)) // 1000)
    return (
        f"Running in the background: {len(open_actions)} actions have not reported back"
        f" (the oldest was sent {oldest_s} s ago)"
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
    expires_at_ms = _now_epoch_ms() + ctx.confirmation_ttl_ms

    if action_request.tool_name != "write_file":
        # ADR 0033: any other tool at the threshold (an MCP tool that needs
        # approval) freezes its arguments as proposed; nothing is staged.
        generic_snapshot: dict[str, Any] = {
            "tool_name": action_request.tool_name,
            "caller": action_request.caller_principal.value,
            "canonical_target": canonical_target,
            "target_entity_ref": action_request.target_entity_ref,
            "risk_level": tool_def.risk_level,
            "args_meta": dict(arguments),
        }
        return emit_event(
            ctx.conn,
            type="confirmation.requested",
            payload={
                "confirmation_id": confirmation_id,
                "action_snapshot": generic_snapshot,
                "template_line": _ask_line(action_request.tool_name, arguments),
                "expires_at_ms": expires_at_ms,
            },
            source_event_id=source_event_id,
            correlation=_action_correlation(action_request),
        )

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

    template_line = t(
        "confirm.ask_write",
        tool_name=action_request.tool_name,
        canonical_target=canonical_target,
        mode=mode_str,
        content_bytes=content_byte_count,
        risk_level=tool_def.risk_level,
    )

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
# constructs an `AuthorizationLease`. It is reachable from two call sites in
# `_handle_utterance`: the grammar hook, gated on a pending slot the utterance
# may still answer and an exact-sentence grammar hit, and a card's button
# (ADR 0062), gated on the intent naming the pending slot's own id. No LLM code
# path touches any of those preconditions. That is the load-bearing invariant
# (ADR §3 D6) made structural rather than merely documented.

_LEASE_TTL_MS: Final[int] = 60_000
"""ADR-0012 D2/D6: the lease need only outlive gate + dispatch (seconds, not
the confirmation ask's own minutes-scale TTL)."""

# The confirmation replies live in the language table under ``confirm.*``.
# ``confirm.content_mismatch`` — ADR-0012 §4 failure-mode table: "content
# artifact missing/hash mismatch at accept -> abort re-proposal, fixed error
# line". Scrub-safe (no completion words) by construction, same discipline as
# every other fixed line in this module.
#
# ``confirm.tool_gone`` — defensive-only: the tool named in a frozen snapshot is no longer
# registered (e.g. the daemon restarted with the tool removed between ask
# and answer). Unreachable in the Day-1 scenario (write_file is the only L3
# tool and registries don't shrink mid-process), kept for the same reason
# `_check_entity_trusted`'s `tool_def is None` arm is kept — a handler must
# be safe standing alone, not merely behind preconditions that happen to
# always hold in production.
#
# ``confirm.reproposal_refused`` — ADR-0012 §4: "gate refuses the re-proposal
# (policy/entity drift since ask) -> fixed line reporting the refusal reason". Interpolates only the
# GateOutcome literal ("refuse" / "confirm_required") — never raw
# `gate.reasons` strings, which are developer-facing audit text not vetted
# against the Pre-emit Gate's completion-keyword scrub.
#
# ``confirm.dispatch_error`` — ADR-0012 §4: "write_file handler I/O error -> error observation ->
# Limitation routing (existing machinery)" — `result_interpreter` below
# already emits the Limitation Claim; this is only the direct-reply text
# for the turn that was the user's own "yes".
#
# ``confirm.write_ran`` — ADR-0012 §3 D6 exact wording: "backed by ack semantics; the wording
# deliberately stops at 已执行 and must not be strengthened" (no completion or
# verification words; "ran" is exactly what the ack proves). ``confirm.tool_ran``
# (ADR 0062) is the tool's own ack-only line from the language table ("已发送"
# for a send), never the model's words.


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
            ctx.conn,
            confirmation_id=slot.confirmation_id,
            accepted=grammar_hit.decision == "yes",
            utterance_raw=transcript,
            grammar_rule_id=grammar_hit.rule_id,
            correlation=correlation,
        )
    return emit_event(
        ctx.conn,
        type="confirmation.accepted" if grammar_hit.decision == "yes" else "confirmation.rejected",
        payload={
            "confirmation_id": slot.confirmation_id,
            "utterance_raw": transcript,
            "grammar_rule_id": grammar_hit.rule_id,
        },
        source_event_id=_latest_event_uid_of_type(ctx.conn, event_type="confirmation.requested"),
        correlation=correlation,
    )


# ADR 0062: a card's button answers like a grammar hit; the rule id says which.
_CARD_SEND: Final[ConfirmGrammarHit] = ConfirmGrammarHit(rule_id="card_button", decision="yes")
_CARD_DISMISS: Final[ConfirmGrammarHit] = ConfirmGrammarHit(rule_id="card_dismiss", decision="no")


def _handle_card_decision(  # noqa: PLR0913 — the answer path's inputs plus the button's payload.
    decision: Mapping[str, Any],
    slot: PendingConfirmationSlot | None,
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
) -> DecideResult:
    """ADR 0062: a card's button, bound to the exact confirmation it shows.

    A card that is no longer the pending one (answered, or replaced by a
    newer ask) runs nothing. Accept with edits first freezes the edited
    arguments as a new ask for the same tool and target, then accepts that:
    the dispatched arguments are always exactly a recorded snapshot's.
    """
    if slot is None or not slot.is_live(_now_epoch_ms()) or (
        decision.get("confirmation_id") != slot.confirmation_id
    ):
        scratch.confirmation_answered_this_turn = True
        return _finalize_response(t("confirm.stale"), packet, ctx, scratch)
    if decision.get("decision") != "accept":
        return _handle_confirmation_rejected(slot, _CARD_DISMISS, "", packet, ctx, scratch)
    edits = decision.get("edits")
    if isinstance(edits, Mapping) and edits:
        slot = _revise_confirmation(slot, edits, ctx, scratch)
    return _handle_confirmation_accepted(slot, _CARD_SEND, "", packet, policy, ctx, scratch)


def _revise_confirmation(
    slot: PendingConfirmationSlot,
    edits: Mapping[str, Any],
    ctx: DecideContext,
    scratch: _Scratch,
) -> PendingConfirmationSlot:
    """Freeze Allen's edits as a new ask for the same tool and target (ADR 0062).

    Only a string argument the ask already had can be replaced, and only by a
    string; staged content (``write_file``) is never edited here. Returns the
    new pending slot, or ``slot`` unchanged when nothing differs.
    """
    args_meta_raw = slot.snapshot.get("args_meta")
    args_meta = dict(args_meta_raw) if isinstance(args_meta_raw, Mapping) else {}
    if "content_artifact" in args_meta:
        return slot
    changed = {
        key: value
        for key, value in edits.items()
        if isinstance(value, str)
        and isinstance(args_meta.get(key), str)
        and args_meta[key] != value
    }
    if not changed:
        return slot
    arguments = {**args_meta, **changed}
    tool_name = str(slot.snapshot.get("tool_name", ""))
    correlation = {"turn_id": scratch.turn_id} if scratch.turn_id else None
    emit_event(
        ctx.conn,
        type="confirmation.requested",
        payload={
            "confirmation_id": _new_confirmation_id(),
            "action_snapshot": {**slot.snapshot, "args_meta": arguments},
            "template_line": _ask_line(tool_name, arguments),
            "expires_at_ms": _now_epoch_ms() + ctx.confirmation_ttl_ms,
        },
        source_event_id=_latest_event_uid_of_type(ctx.conn, event_type="confirmation.requested"),
        correlation=correlation,
    )
    revised = make_snapshot(ctx.conn).pending_confirmations.slot
    return revised if revised is not None else slot


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
        return _finalize_response(t("confirm.stale"), packet, ctx, scratch)
    scratch.events.append(rejected_event)
    scratch.confirmation_answered_this_turn = True

    draft = t("confirm.rejected", action=action(str(slot.snapshot.get("tool_name", "")))[0])
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
        return _finalize_response(t("confirm.stale"), packet, ctx, scratch)
    scratch.events.append(accepted_event)
    scratch.confirmation_answered_this_turn = True

    # --- 2. Re-read + verify the staged content artifact -------------------
    snapshot = slot.snapshot
    args_meta_raw = snapshot.get("args_meta")
    args_meta: Mapping[str, Any] = args_meta_raw if isinstance(args_meta_raw, Mapping) else {}
    content_artifact_raw = args_meta.get("content_artifact")
    expected_sha256_raw = args_meta.get("content_sha256")

    tool_name_raw = snapshot.get("tool_name")
    tool_name = tool_name_raw if isinstance(tool_name_raw, str) else ""
    # ADR 0033: only write_file stages content; any other tool re-proposes its
    # frozen arguments unchanged.
    staged = tool_name == "write_file"

    content_text: str | None = None
    if staged and isinstance(content_artifact_raw, str) and isinstance(expected_sha256_raw, str):
        artifact_path = Path(content_artifact_raw)
        if artifact_path.is_file():
            content_bytes_data = artifact_path.read_bytes()
            if hashlib.sha256(content_bytes_data).hexdigest() == expected_sha256_raw:
                content_text = content_bytes_data.decode("utf-8")

    if staged and content_text is None:
        return _finalize_response(t("confirm.content_mismatch"), packet, ctx, scratch)
    tool_def = _find_tool_def(ctx.tool_registry, tool_name)
    if tool_def is None:
        return _finalize_response(t("confirm.tool_gone"), packet, ctx, scratch)

    target_entity_ref_raw = snapshot.get("target_entity_ref")
    target_entity_ref = target_entity_ref_raw if isinstance(target_entity_ref_raw, str) else None

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
    if staged:
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
                ctx.conn,
                source_confirmation_event_id=accepted_event.event_uid,
                action_request=action_request,
                lease=lease,
                gate_payload=gate_payload,
                correlation=_action_correlation(action_request),
            )
        except ConfirmationRevalidationError:
            return _finalize_response(t("confirm.stale"), packet, ctx, scratch)
        if isinstance(authorization, AlreadyConsumed):
            return _finalize_response(t("confirm.accepted"), packet, ctx, scratch)
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
        draft = t("confirm.reproposal_refused", outcome=gate.outcome)
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
            action_request,
            ctx.conn,
            ctx.runtime_paths,
            ctx.lifecycle,
        )
    except AuthorizedDispatchAlreadyStarted:
        return _finalize_response(t("confirm.accepted"), packet, ctx, scratch)
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
        draft = t("confirm.dispatch_error", error=primary_result_slot.error)
        return _finalize_response(draft, packet, ctx, scratch)

    if not staged:
        draft = t("confirm.tool_ran", done=action(tool_name)[1])
        return _finalize_response(draft, packet, ctx, scratch)
    path_written = primary_result_slot.payload.get("path", "?")
    bytes_written = primary_result_slot.payload.get("bytes_written", "?")
    draft = t(
        "confirm.write_ran",
        path=path_written,
        bytes_written=bytes_written,
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


_CARD_NOTE: Final = (
    " Calling this does not run it: the user gets a card with these arguments"
    " and runs or discards it."
)


def _tool_menu(
    llm_surface: Sequence[ToolDefinitionLike], loaded: Collection[str],
) -> list[dict[str, Any]]:
    """One request's tools: the surface without the deferred ones not yet loaded."""
    return tool_definitions_for_llm(
        [_tool_to_dict(t) for t in llm_surface if not t.deferred or t.name in loaded],
    )


def _tool_to_dict(tool_def: ToolDefinitionLike) -> dict[str, Any]:
    """Project a ToolDefinitionLike into a dict for ``intent.build_messages``.

    A tool that asks first says so (ADR 0062), or the model, believing the
    call acts at once, asks in its own words before the card asks again.
    """
    card = _CARD_NOTE if tool_def.requires_confirmation else ""
    return {
        "name": tool_def.name,
        "description": tool_def.description + card,
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
    "DEFAULT_MAX_TOOL_ITERATIONS",
    "PREFIX_WARM_MAX_OUTPUT_TOKENS",
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
    "open_prefix_warm",
    "pre_action_gate",
    "pre_emit_gate",
]
