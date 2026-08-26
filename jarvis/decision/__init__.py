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
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from jarvis.decision.confirm_grammar import match_confirm_grammar
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
from jarvis.decision.packet import (
    DEFAULT_OBSERVER_POLL_INTERVAL_S,
    EVIDENCE_NOTE_PREFIX,
    SituationPacket,
    assemble_packet,
    format_evidence_context_note,
    format_pending_confirmation_note,
    format_status_board_note,
)
from jarvis.decision.policy import EffectivePolicy, effective_policy, surface_for
from jarvis.decision.pre_emit_phrases import COMPLETION_REGEXES
from jarvis.decision.resolver import (
    ResolverConfidence,
    ResolverResult,
    resolve_task_ref,
    resolve_task_ref_by_window,
)
from jarvis.decision.result_interpreter import (
    VerifyVerdict,
    emit_worker_report_extras,
    interpret_verify_diff_bundle,
    result_interpreter,
)
from jarvis.decision.reviewer import ReviewerVerdict, review_diff
from jarvis.decision.tier0 import render_tier0_response
from jarvis.shared import (
    ActionRequest,
    CallerPrincipal,
    Event,
    RawResult,
    RawResultBundle,
)
from jarvis.shared.pricing import compute_cost_usd, load_pricing_table
from jarvis.shared.text import truncate_utf8
from jarvis.state.event_log import emit_event
from jarvis.state.projections import make_snapshot

if TYPE_CHECKING:
    import sqlite3

    from jarvis.decision.confirm_grammar import ConfirmGrammarHit, ConfirmGrammarTable
    from jarvis.decision.llm import ChatResult, LLMClient
    from jarvis.decision.tier0 import Tier0Hit, Tier0Table
    from jarvis.shared import AuthorizationLease, EvidenceLevel, RiskLevel
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

# `_dispatch_one_tool_call`'s three-way signal to `_run_tool_use_loop`
# (ADR-0012 D5 widens the prior bool: True=continue / False=pause):
# "continue" — sync tool dispatched, loop to the next iteration;
# "async_pause" — spawn_worker paused, `worker.reported` re-enters
# later; "confirm_required" — the FIRST confirm_required this turn was
# frozen into an ask, the tool loop ends here (no more LLM calls).
_DispatchOutcome = Literal["continue", "async_pause", "confirm_required"]

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


# Demonstrative + task-noun reference detector. When the user's utterance
# clearly refers to a specific existing task ("昨天那个 task", "刚才的任务")
# but the Resolver finds no matching subject AND the ledger has no open
# tasks, the canonical surface response IS the F1 branch-1 hard refusal
# text ("找不到对应的 task"), not an LLM-generated paraphrase via the
# list_tasks tool. _no_task_to_refer_to() drives the deterministic
# short-circuit in :func:`_run_tool_use_loop`. The pattern is intentionally
# permissive — false positives degrade to the same correct text, false
# negatives just fall through to the normal LLM path.
_DEMONSTRATIVE_TASK_RE: Final[re.Pattern[str]] = re.compile(
    r"(那个|那些|这个|这些|刚才|上次|昨天|今天|明天).{0,30}(task|任务|项目)",
)


def _no_task_to_refer_to(packet: SituationPacket) -> bool:
    """True iff the user demonstratively referenced a task that doesn't exist.

    Conditions (all must hold):
        - ``packet.open_tasks`` is empty (no resolvable target).
        - ``packet.trigger_event.payload['transcript']`` matches
          :data:`_DEMONSTRATIVE_TASK_RE` (demonstrative pronoun /
          temporal anchor + task noun).

    Used by :func:`_run_tool_use_loop` to short-circuit to the F1
    branch-1 hard refusal before the LLM round-trip -- guarantees the
    canonical "找不到对应的 task (未验证 / unverified)" surface on the
    "user refers to a task that does not exist" path, irrespective of
    whether the LLM would otherwise dispatch ``list_tasks`` and produce
    a paraphrase.
    """
    if packet.open_tasks:
        return False
    transcript = packet.trigger_event.payload.get("transcript", "") or ""
    if not isinstance(transcript, str):
        return False
    return _DEMONSTRATIVE_TASK_RE.search(transcript) is not None


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
        entity_bookmarks: ``(alias, absolute-path)`` seed pairs for the
            EntityRegistry projection's config route (ADR-0011 D4),
            threaded down to every ``assemble_packet`` call the same
            way ``tier0_table`` is threaded. The runtime composition
            root loads ``config/file_targets.yaml``; L3 cannot import
            the loader itself. Default ``()`` — no seed.
        entity_resolver: Injected resolve-on-propose callable (ADR-0011
            D4), or ``None``. ``_dispatch_one_tool_call`` calls it for
            a ``requires_entity=True`` tool whose ``target_entity_ref``
            is still unset after the task-ref resolver runs. ``None``
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
    entity_bookmarks: Sequence[tuple[str, str]] = ()
    entity_resolver: EntityResolverLike | None = None
    write_entity_resolver: EntityResolverLike | None = None
    confirmation_ttl_ms: int = _DEFAULT_CONFIRMATION_TTL_MS
    confirm_grammar_table: ConfirmGrammarTable = ()


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


def _active_subject_or_default(
    scratch: _Scratch,
    packet: SituationPacket,
) -> str | None:
    """Resolved subject, else the first open task, else None.

    The single home of a fallback previously duplicated across
    `_finalize_response` and the prompt-note builders — the note and
    the gate must brief/judge the SAME subject, or the LLM gets briefed
    on task A and gated on task B.
    """
    if scratch.active_subject_ref is not None:
        return scratch.active_subject_ref
    if packet.open_tasks:
        return packet.open_tasks[0].task_id
    return None


def _insert_system_notes(
    messages: list[dict[str, Any]],
    packet: SituationPacket,
    scratch: _Scratch,
    ctx: DecideContext,
) -> None:
    """Insert the four prompt-head system notes into ``messages``.

    Each note is added via ``messages.insert(0, ...)``, and each such
    call pushes every note already inserted further from index 0 — so
    the call order below is bottom-to-top: the FIRST call ends up
    nearest the live conversation (later inserts push it toward the
    tail, where the user message sits), the LAST call ends up at index
    0, farthest from it. Final stacking order (top → bottom):
    Status Board (ambient background, called last), open tasks (Task
    Ledger snapshot for reference resolution), evidence context, pending
    confirmation (nearest the conversation, called first — the most
    immediately turn-critical: it governs what THIS draft may claim
    about THIS turn's outstanding ask; spec §3.4.4, Phase 0 batch 4;
    ADR-0012 §3 D4).

    All four share the §10.5 deviation: dynamic context sits at the
    head of the prompt, not the tail — flagged, not fixed, here.
    """
    # ADR-0012 §3 D4: id-free note naming an outstanding confirmation
    # ask, if one is live. Lets an unrelated turn's LLM know an ask is
    # outstanding (C4) and a paraphrased-consent turn's LLM talk about
    # it (C6) without being able to act on it — the note carries no
    # confirmation_id. Called FIRST so it ends up nearest the
    # conversation: it is the most immediately turn-critical of the
    # four (governs what THIS draft may claim about THIS turn's
    # outstanding ask).
    pending_confirmation_note = format_pending_confirmation_note(packet)
    if pending_confirmation_note is not None:
        messages.insert(0, {"role": "user", "content": pending_confirmation_note})
    evidence_note = format_evidence_context_note(
        packet, subject_ref=_active_subject_or_default(scratch, packet),
    )
    if evidence_note is not None:
        messages.insert(0, {"role": "user", "content": evidence_note})
    # Task Ledger snapshot so the LLM can resolve natural references
    # like "昨天那个 task" to the canonical task_id (Step 12 follow-up).
    open_tasks_note = _format_open_tasks_note(packet)
    if open_tasks_note is not None:
        messages.insert(0, {"role": "user", "content": open_tasks_note})
    # ADR-0009 D6 (render half of Step 11): folded Status Board with its
    # §3.6.9 freshness wording, so "repo X 现在什么状态" is answered from
    # observer-folded state instead of the LLM reaching for git (M6).
    status_board_note = format_status_board_note(
        packet, poll_interval_s=ctx.observer_poll_interval_s,
    )
    if status_board_note is not None:
        messages.insert(0, {"role": "user", "content": status_board_note})


def _refresh_evidence_note(
    messages: list[dict[str, Any]],
    packet: SituationPacket,
    scratch: _Scratch,
) -> None:
    """Replace (or insert) the evidence-context note after a packet refresh.

    The resolver may have set the subject since the first render, and
    sync tool dispatches change the claim/evidence state mid-loop.
    """
    refreshed_note = format_evidence_context_note(
        packet, subject_ref=_active_subject_or_default(scratch, packet),
    )
    if refreshed_note is None:
        return
    for note_index, message in enumerate(messages):
        content = message.get("content")
        if isinstance(content, str) and content.startswith(EVIDENCE_NOTE_PREFIX):
            messages[note_index] = {"role": "user", "content": refreshed_note}
            return
    messages.insert(0, {"role": "user", "content": refreshed_note})


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
    packet = assemble_packet(trigger, ctx.conn, entity_bookmarks=ctx.entity_bookmarks)
    policy = effective_policy(_allowed_tool_surface(ctx.tool_registry))

    # ``utterance.received`` is the voice-surface twin of
    # ``surface.user_intent``: ADR-0005 §5.1 — the ASR pipeline owns the
    # audit / normalize step and emits ``utterance.received`` (carrying
    # the same ``turn_id`` + ``transcript`` payload contract), so the
    # voice path must take the same handler. Without this widening,
    # every voice turn no-ops here and the watcher times out 5 s later.
    if trigger.type in ("surface.user_intent", "utterance.received"):
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
    # F1 deterministic short-circuit: when the user demonstratively
    # references a task that doesn't exist in the ledger, emit the
    # canonical "找不到对应的 task (未验证 / unverified)" hard refusal
    # directly. Without this the LLM may dispatch ``list_tasks`` and
    # paraphrase ("open tasks 为空"); the F1 branch-1 text carries the
    # bilingual unverified marker the surface gate guarantees on the
    # "user referenced a nonexistent task" path. Skipping the LLM
    # round-trip also collapses turn latency for this dead-end case.
    if _no_task_to_refer_to(packet):
        plan = _hard_refusal_plan(
            active_subject="unknown_subject",
            active_claim_levels=(),
        )
        # Emit entity.resolved with outcome="not_found" so the audit
        # chain records the resolution attempt — without this, the
        # F1 short-circuit returns a hard refusal without any trace of
        # which natural ref we tried to resolve. The synthetic
        # ResolverResult mirrors what resolve_task_ref would have
        # returned against an empty ledger.
        transcript_raw = packet.trigger_event.payload.get("transcript", "")
        natural_ref = transcript_raw if isinstance(transcript_raw, str) else ""
        synthetic_result = ResolverResult(
            resolved_to=None,
            confidence="none",
            candidates=(),
            match_basis="no task to refer to (F1 short-circuit)",
        )
        scratch.events.append(
            _emit_entity_resolved(
                ctx,
                natural_ref=natural_ref,
                result=synthetic_result,
                turn_id=scratch.turn_id,
                source_event_id=packet.trigger_event.event_uid,
            )
        )
        if scratch.turn_id is not None:
            ended_event = emit_event(
                ctx.conn,
                type="turn.ended",
                payload={
                    "turn_id": scratch.turn_id,
                    "final_response_hash": plan.response_hash,
                },
                source_event_id=packet.trigger_event.event_uid,
                correlation={"turn_id": scratch.turn_id},
            )
            scratch.events.append(ended_event)
        return DecideResult(
            response_plan=plan,
            events_emitted=tuple(scratch.events),
            turn_id=scratch.turn_id,
            attention_channel="queue_review",
        )

    messages = build_llm_messages(packet)
    _insert_system_notes(messages, packet, scratch, ctx)
    llm_surface = surface_for(policy, ctx.tool_registry, CallerPrincipal.JARVIS_LLM)
    tools = tool_definitions_for_llm([_tool_to_dict(t) for t in llm_surface])

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

            dispatch_outcome: _DispatchOutcome = "continue"
            for tool_call in chat_result.tool_calls:
                dispatch_outcome = _dispatch_one_tool_call(
                    tool_call=tool_call,
                    packet=packet,
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
                # synthetic refuse result"). Only "async_pause" breaks
                # immediately — spawn_worker pausing mid-batch has
                # always short-circuited the remaining tool_calls.
                if dispatch_outcome == "async_pause":
                    break

            if _confirmation_already_requested_this_turn(scratch):
                # ADR-0012 D5: the ask ends the tool loop here — no more
                # LLM calls this turn, regardless of which tool_call in
                # the batch (or which loop iteration) triggered it. The
                # draft is THIS response's own LLM text (optional 铺垫,
                # if any — goes through the normal Pre-emit Gate scrub
                # below like any other draft) plus the runtime-rendered
                # template line frozen at staging time
                # (`_stage_and_request_confirmation`), never composed by
                # the LLM.
                llm_preamble = (chat_result.text or "").strip()
                template_line = scratch.pending_confirmation_template_line or ""
                draft = f"{llm_preamble}\n\n{template_line}" if llm_preamble else template_line
                return _finalize_response(draft, packet, ctx, scratch)

            if dispatch_outcome == "async_pause":
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
            # the next LLM call sees freshly-emitted claims/evidence,
            # and re-render the evidence note against it — sync tools
            # (verify_diff) mint claims mid-loop, which is exactly when
            # the note must not be stale.
            packet = assemble_packet(
                packet.trigger_event, ctx.conn, entity_bookmarks=ctx.entity_bookmarks,
            )
            _refresh_evidence_note(messages, packet, scratch)
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


def _spoken_preview_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
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
            value_bytes[:_TIER0_SPOKEN_PREVIEW_MAX_BYTES], len(value_bytes),
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

    Mirrors :func:`_dispatch_one_tool_call` steps 2-7b for a sync,
    entity-free tool with ``caller_principal=REGEX_ROUTER``: proposed →
    Pre-action Gate → authorized → L4 dispatch → Result Interpreter →
    deterministic template text → :func:`_finalize_response` (Pre-emit
    Gate + attention). Spec §3.5.2: "低延迟路径，但仍要过 entity /
    policy / risk gate".

    An entry naming an unknown or async tool is a table
    misconfiguration that ``validate_tier0_table`` normally rejects at
    bootstrap; reaching it here degrades to the Tier 2 loop rather than
    failing the turn.
    """  # noqa: RUF002 — fullwidth punctuation is verbatim spec §3.5.2 Chinese quotation.
    tool_def = _find_registered_tool_def(ctx.tool_registry, hit.tool_name)
    if tool_def is None or tool_def.is_async:
        LOGGER.warning(
            "tier0: pattern %r targets unusable tool %r — falling back to LLM",
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

    gate = pre_action_gate(
        action_request,
        policy,
        packet.task_ledger_snapshot,
        tool_def=tool_def,
        entity_registry=packet.entity_registry,
    )
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

    bundle = ctx.tool_registry.dispatch(
        action_request, ctx.conn, ctx.runtime_paths, ctx.lifecycle,
    )
    result_observed_uid = _latest_event_uid_of_type(
        ctx.conn, event_type="action.result_observed",
    )
    source_event_for_interpreter = result_observed_uid or proposed_event.event_uid
    for slot in bundle.slots:
        interpreted_events = result_interpreter(
            slot,
            source_event_id=source_event_for_interpreter,
            action_request=action_request,
            conn=ctx.conn,
            subject_ref_override=None,
        )
        scratch.events.extend(interpreted_events)

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
    draft = render_tier0_response(hit, _spoken_preview_payload(primary_slot.payload))
    return _finalize_response(
        draft, packet, ctx, scratch, gate_text=hit.response_template,
    )


def _dispatch_one_tool_call(  # noqa: C901, PLR0912, PLR0913, PLR0915 — single-pass orchestration of resolver (Day-1 + Day-2 time-window) + gate + dispatch + interpreter; splitting muddles the audit trace.
    *,
    tool_call: object,
    packet: SituationPacket,
    policy: EffectivePolicy,
    ctx: DecideContext,
    scratch: _Scratch,
    messages: list[dict[str, Any]],
) -> _DispatchOutcome:
    """Resolve, gate, and dispatch one LLM-proposed tool call.

    Returns:
        ``"continue"`` if the tool was sync (the loop should continue
        with the next iteration). ``"async_pause"`` if the tool was
        async (the caller should pause and return a partial
        DecideResult). ``"confirm_required"`` if this call's Pre-action
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

    # 1. Tool definition lookup (ADR-0011 §12.2 MUST-FIX 2). Moved ahead
    #    of the task-ref resolver below: an unknown tool must not run
    #    the resolver at all (it would emit a task-flavored
    #    ``entity.resolved`` for a name that doesn't even exist — a
    #    deliberate, minor behavior change from the previous ordering),
    #    and a ``requires_entity=True`` tool must never run it either —
    #    see the guard on that block.
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

    # 2. Task-ref resolver. Tools that accept a ``task_id`` go through
    #    the resolver; LLM-supplied ``task_id`` is treated as a natural
    #    ref (per spec §3.3.7: LLM must not invent entity IDs). When the
    #    LLM additionally emits a structured ``{since_ts, until_ts}``
    #    pair on the action arguments (ADR-0002 Step 5: L3 LLM
    #    translates natural-language time windows like "昨天" into
    #    epoch-ms bounds), we route through ``resolve_task_ref_by_window``
    #    so the resolver queries the projection via
    #    :meth:`TaskLedgerSnapshot.tasks_in_window` — no direct SQL from
    #    L3 per spec §3.4.3.
    #
    #    ADR-0011 §12.2 MUST-FIX 2: gated on ``not tool_def.requires_entity``
    #    — a ``requires_entity=True`` tool (e.g. ``read_file``) must not
    #    have ``target_entity_ref`` filled from a ``task_id`` /
    #    ``natural_ref`` argument that raw, unvalidated LLM JSON happens
    #    to carry alongside an unrelated ``target``. Without this guard
    #    that fill runs BEFORE the resolve-on-propose block below ever
    #    sees ``target_entity_ref`` (its precondition is
    #    ``target_entity_ref is None``), so the file target is neither
    #    resolved nor refused — it is silently skipped.
    target_entity_ref: str | None = None
    if not tool_def.requires_entity:
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

    # ADR-0011 D4 (resolve-on-propose): a tool that declares
    # `requires_entity=True` (e.g. `read_file`) but has no
    # `target_entity_ref` yet gets one shot at the injected entity
    # resolver here. Nothing upstream can have filled the ref first: the
    # task-ref resolver block above is skipped entirely for a
    # `requires_entity=True` tool (its `not tool_def.requires_entity`
    # guard, §12.2 MUST-FIX 2 above), and the active-subject inheritance
    # below carries its own `not tool_def.requires_entity` guard — so
    # this is the only block that can set `target_entity_ref` for such a
    # tool. `ctx.entity_resolver` / `ctx.write_entity_resolver` (ADR-0012
    # D1, see the tool-name branch below) are `None` in any context that
    # hasn't wired them; the feature is then inert and Step 3's
    # `requires_entity` gate arm refuses, which is correct fail-closed
    # behavior, not a bug. `entity.resolved` is emitted on BOTH outcomes
    # (ADR §4) — the `not_found` emission is what E2 depends on.
    #
    # ADR-0011 §12.2 MUST-FIX 1: `gate_entity_registry` starts as the
    # packet's registry (assembled before this call, so it can be one
    # `entity.resolved` event stale) and is overlaid with the
    # just-emitted event on a hit, via the exact same fold logic route 3
    # of `_fold_entity_registry` uses (`EntityRegistry.with_resolved_event`).
    # The event is already durable in the log; this only catches the
    # gate's view up to it — it is not a widening of trust.
    # ADR-0012 D1: `write_file`'s write-target resolution differs from
    # every other `requires_entity=True` tool's (a non-existent target
    # can still resolve, iff its parent directory is in scope), so it
    # gets its own injected resolver rather than sharing
    # `ctx.entity_resolver`. Selecting by bare tool name mirrors the
    # existing `name == "verify_diff"` precedent just above this block.
    file_entity_resolver = (
        ctx.write_entity_resolver if name == "write_file" else ctx.entity_resolver
    )
    gate_entity_registry = packet.entity_registry
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
    if (
        tool_def.requires_entity
        and target_entity_ref is None
        and file_entity_resolver is not None
    ):
        raw_target = arguments.get("target")
        raw_query = raw_target if isinstance(raw_target, str) else ""
        resolved = file_entity_resolver(raw_query)
        entity_event = _emit_file_entity_resolved(
            ctx,
            natural_ref=raw_query,
            resolved=resolved,
            turn_id=scratch.turn_id,
            source_event_id=packet.trigger_event.event_uid,
        )
        scratch.events.append(entity_event)
        if resolved is not None:
            target_entity_ref = resolved.entity_id
            write_target_canonical = resolved.canonical
            gate_entity_registry = gate_entity_registry.with_resolved_event(entity_event)

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
    #
    # ADR-0011 D3/D4 guard: a `requires_entity=True` tool must NEVER
    # inherit the active *task* subject as its `target_entity_ref` —
    # doing so would hand the gate a task id for a tool whose contract
    # is a non-task entity (e.g. a file), and the ledger arm would pass
    # it, silently defeating both D3 (an entity-required tool needs a
    # REAL resolved target) and D4/E2 (an unresolved target must
    # refuse, not fall back to whatever task happens to be active).
    if (
        target_entity_ref is None
        and scratch.active_subject_ref is not None
        and not tool_def.requires_entity
    ):
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

    # Idempotence guard (spec §3.4.3/§3.4.4 decide-from-state + §3.4.6
    # untrusted LLM): a verify_diff for a task already verify-proposed this
    # turn is redundant — re-verifying the same artifact yields no new
    # evidence and risks a duplicate task.verified. Computed BEFORE this
    # action.proposed is emitted so the query sees only prior proposals;
    # enforced as a Pre-action Gate refusal below.
    redundant_verify_diff = name == "verify_diff" and _verify_diff_already_proposed_this_turn(
        ctx.conn, turn_id=scratch.turn_id, task_id=target_entity_ref,
    )

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

    # 4. Pre-action Gate. Uses `gate_entity_registry`, not
    #    `packet.entity_registry` — see the MUST-FIX 1 comment above the
    #    resolve-on-propose block: the two are identical except when
    #    this call just resolved a file target, in which case the
    #    former also carries that event.
    gate = pre_action_gate(
        action_request,
        policy,
        packet.task_ledger_snapshot,
        tool_def=tool_def,
        entity_registry=gate_entity_registry,
    )
    gate_outcome = gate.outcome
    gate_reasons = list(gate.reasons)
    if redundant_verify_diff and gate_outcome == "pass":
        # Refuse the redundant verify_diff (spec §3.4.3/§3.4.4 + §3.4.6) so
        # the existing refuse path injects a tool-result and the LLM settles
        # into a response instead of re-verifying an already-settled task.
        gate_outcome = "refuse"
        gate_reasons = ["redundant_verify_diff_this_turn"]
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
        return "async_pause"

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

    last_interpreted_evidence: Event | None
    verify_verdict: VerifyVerdict
    if name == "verify_diff":
        last_interpreted_evidence, verify_verdict = _route_verify_diff_bundle(
            bundle=bundle,
            ctx=ctx,
            scratch=scratch,
            action_request=action_request,
            target_entity_ref=target_entity_ref,
            fallback_source_event_id=source_event_for_interpreter,
        )
    else:
        last_interpreted_evidence = None
        verify_verdict = "neither"
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
                verify_verdict = "verified"

    # Trailing completion event per the F2 ladder + Fix 2 Option A
    # amendment. Three branches keyed on the Result Interpreter's
    # verdict:
    #
    # - verdict="verified" → task.verified per ADR § Canonical event
    #   trace evt 22. Canonical happy path: diff_nonempty AND
    #   verify_command exit 0. Cause-chain sources the last evidence
    #   event the interpreter emitted.
    # - verdict="no_op" → task.no_op (Fix 2 Option A). Empty diff +
    #   verify_command exit 0: the verify_command alone cannot promote
    #   to level=verified absent an artifact-change signal (spec §8.9).
    #   No evidence event to chain off of, so source the cause-chain
    #   off the action.proposed row.
    # - verdict="neither" → emit nothing (verify failed, no verify
    #   slot, or no canonical subject).
    if verify_verdict == "verified" and last_interpreted_evidence is not None:
        task_verified_event = emit_event(
            ctx.conn,
            type="task.verified",
            payload={"task_id": target_entity_ref, "by": "jarvis"},
            source_event_id=last_interpreted_evidence.event_uid,
            correlation=_action_correlation(action_request),
        )
        scratch.events.append(task_verified_event)
        scratch.active_subject_ref = target_entity_ref
    elif verify_verdict == "no_op" and target_entity_ref is not None:
        no_op_payload: dict[str, Any] = {"task_id": target_entity_ref}
        if action_payload is not None:
            verify_command = action_payload.get("verify_command")
            if isinstance(verify_command, str) and verify_command:
                no_op_payload["verify_command"] = verify_command
        no_op_payload["reason"] = (
            "diff_nonempty=False + verify_command exit 0; "
            "no artifact-change signal to support task.verified (spec §8.9)"
        )
        task_no_op_event = emit_event(
            ctx.conn,
            type="task.no_op",
            payload=no_op_payload,
            source_event_id=proposed_event.event_uid,
            correlation=_action_correlation(action_request),
        )
        scratch.events.append(task_no_op_event)
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
    return "continue"


def _route_verify_diff_bundle(  # noqa: PLR0913 - F2 ladder hand-off inputs are all load-bearing per ADR-0002 § Verify_command plumbing.
    *,
    bundle: RawResultBundle,
    ctx: DecideContext,
    scratch: _Scratch,
    action_request: ActionRequest,
    target_entity_ref: str | None,
    fallback_source_event_id: str,
) -> tuple[Event | None, VerifyVerdict]:
    """Drive the F2 ladder for a ``verify_diff`` :class:`RawResultBundle`.

    Per ADR-0002 Step 12 (§ Evidence ladder lines 250-339), amended by
    Fix 2 Option A for the empty-diff paradox row:

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

    Returns ``(last_evidence_event, verdict)`` where ``verdict`` is
    one of :data:`VerifyVerdict`: ``"verified"`` → caller emits
    ``task.verified``; ``"no_op"`` → caller emits ``task.no_op``;
    ``"neither"`` → caller emits nothing.
    """
    if target_entity_ref is None:
        # No canonical subject → fall back to the Day-1 single-slot
        # path; ladder rows need a subject_ref to be useful.
        last_evidence: Event | None = None
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
        return last_evidence, "neither"

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
    # ADR-0002 § Reviewer contract: the reviewer sees the FULL diff.
    # ``diff_text_preview`` stays as the fallback for slots minted
    # before the full-text field existed.
    diff_text = observation_slot.payload.get("diff_text")
    if not isinstance(diff_text, str) or not diff_text:
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

    emitted, verdict = interpret_verify_diff_bundle(
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
    return last_evidence, verdict


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

    # 3b. Phase 0 batch 5: fold the optional WorkerReport fields
    # (remaining_risks / tests_run / commands_run) into claims.
    if scratch.active_subject_ref is not None:
        report_extra_events = emit_worker_report_extras(
            ctx.conn,
            report_payload=trigger.payload,
            subject_ref=scratch.active_subject_ref,
            source_event_id=trigger.event_uid,
            correlation={
                "action_id": action_id,
                **({"run_id": run_id} if isinstance(run_id, str) else {}),
                **({"turn_id": scratch.turn_id} if scratch.turn_id else {}),
            },
        )
        scratch.events.extend(report_extra_events)

    # 4 + 5. Re-call the LLM to plan verification + continue loop.
    return _run_tool_use_loop(
        assemble_packet(trigger, ctx.conn, entity_bookmarks=ctx.entity_bookmarks),
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
        assemble_packet(trigger, ctx.conn, entity_bookmarks=ctx.entity_bookmarks),
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
    *,
    gate_text: str | None = None,
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
    words, so it keeps gating itself exactly as before. Retry attempts
    1/2 (unreachable today — no shipped Tier 0 template contains
    completion language) still gate `draft_text` if a future
    misconfigured template ever gets this far; that is the intended
    defense-in-depth, not an oversight.

    ``turn.ended.source_event_id`` references the LAST gate event in
    the chain regardless of which branch was taken.
    """
    hard_refusal_used = False
    active_subject = _active_subject_or_default(scratch, packet)
    if active_subject is None:
        # Spec §3.4.12 v0 + §3.4.4 LLMSituationPacket: no subject in
        # scope is a first-class case, not a failure mode. Pass None to
        # the gate; it short-circuits to a routine pass-through (see
        # pre_emit_gate's None branch). The retry chain (attempts 1
        # and 2) and _hard_refusal_plan remain reachable only when a
        # real subject is in scope and downgrade_required fires.
        LOGGER.debug(
            "_finalize_response: no active_subject_ref and no open tasks "
            "(turn_id=%r); passing None to pre_emit_gate for §3.4.12 v0 "
            "pass-through (no consequential claim to gate).",
            scratch.turn_id,
        )

    # Always refresh the projection so the gate sees the latest
    # claim/evidence rows.
    projections = make_snapshot(ctx.conn)

    # Attempt 0 — initial verdict on the raw LLM draft (or, on the
    # Tier 0 path, on the closed template literal — see `gate_text` in
    # the docstring above).
    text_to_gate = draft_text if gate_text is None else gate_text
    plan = pre_emit_gate(text_to_gate, projections.claim_evidence, active_subject)
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

    # The ``active_subject is not None`` clause is redundant at runtime —
    # pre_emit_gate's None branch returns downgrade_required=False
    # unconditionally (spec §3.4.12 v0), so plan.downgrade_required
    # already implies active_subject is not None. It is present to
    # narrow the type for _hard_refusal_plan below (attempt 2).
    if plan.downgrade_required and active_subject is not None:
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

    # ADR-0012 D5 MUST-FIX (post-review): the template line must
    # survive the retry chain above UNCONDITIONALLY. The retry chain
    # re-prompts the LLM with only `draft_text`/`retry_text` in
    # history — the LLM never sees `scratch.pending_confirmation_
    # template_line` as something to preserve, so a downgrade_required
    # verdict on attempt 0 can silently drop the line from `plan.text`
    # (attempt 1's retry_plan, or attempt 2's forced/hard-refusal
    # plan, all overwrite `plan` with fresh text that was never built
    # from the frozen snapshot). That line is Allen's only chance to
    # catch a fuzzy-resolved write target before bytes are written
    # (Step 4 erratum) — losing it would let a "是" bind to a
    # confirmation whose rendered question Allen never actually saw.
    # Idempotent post-condition, not a rewrite of the function above:
    # if this turn froze a template_line and the text about to ship
    # doesn't already end with it, append it and re-hash — same
    # re-hash discipline as the `gate_text` override earlier in this
    # function, so `turn.ended` below and the returned plan agree.
    # Unreachable-in-practice on the acceptance scenario (write_file
    # proposals carry no active_subject_ref, so pre_emit_gate never
    # even reaches the retry chain) but not a structural guarantee —
    # this guard is what makes it one.
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

    # B-0005/B-0006: a Limitation Claim emitted THIS turn must reach the
    # operator (ADR K5 row) — scan the turn's own events, not the folded
    # projection, so historical Limitations never re-trigger voice.
    limitation_emitted = any(
        ev.type == "claim.created" and ev.payload.get("type") == "Limitation"
        for ev in scratch.events
    )
    # Phase 0 batch 5: the worker's explicit review request promotes the
    # worker.reported silent_log fallthrough to queue_review (never a
    # voice demotion — see attention_policy's branch placement).
    needs_human_review = (
        packet.trigger_event.type == "worker.reported"
        and packet.trigger_event.payload.get("needs_human_review") is True
    )
    attention = attention_policy(
        packet,
        projections.claim_evidence,
        limitation_emitted=limitation_emitted,
        needs_human_review=needs_human_review,
    )
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


def _emit_file_entity_resolved(
    ctx: DecideContext,
    *,
    natural_ref: str,
    resolved: ResolvedEntityLike | None,
    turn_id: str | None,
    source_event_id: str,
) -> Event:
    """Emit the file-flavored ``entity.resolved`` event (ADR-0011 D4).

    A separate function rather than a branch inside
    :func:`_emit_entity_resolved`: that function's shape is load-bearing
    for the flagship canary's task-resolution path, and a resolver
    outcome ladder do not apply the same way here — the injected
    resolver either fully resolves or misses outright (no ambiguous
    multi-candidate case), so this emitter always writes
    ``candidates=[]`` and one of exactly two outcomes. Emitted on BOTH
    outcomes per ADR §4 — the ``not_found`` emission is what E2
    (garbage target -> gate refuse) depends on for its audit trail.
    """
    if resolved is not None:
        payload: dict[str, Any] = {
            "entity_type": "file",
            "natural_ref": natural_ref,
            "resolved_to": resolved.entity_id,
            "confidence": resolved.confidence,
            "candidates": [],
            "match_basis": resolved.match_basis,
            "outcome": "resolved",
        }
    else:
        payload = {
            "entity_type": "file",
            "natural_ref": natural_ref,
            "resolved_to": None,
            "confidence": "none",
            "candidates": [],
            "match_basis": "none",
            "outcome": "not_found",
        }
    return emit_event(
        ctx.conn,
        type="entity.resolved",
        payload=payload,
        source_event_id=source_event_id,
        correlation={"turn_id": turn_id} if turn_id else None,
    )


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
    requested_event_uid = _latest_event_uid_of_type(
        ctx.conn, event_type="confirmation.requested",
    )
    rejected_event = emit_event(
        ctx.conn,
        type="confirmation.rejected",
        payload={
            "confirmation_id": slot.confirmation_id,
            "utterance_raw": transcript,
            "grammar_rule_id": grammar_hit.rule_id,
        },
        source_event_id=requested_event_uid,
        correlation={"turn_id": scratch.turn_id} if scratch.turn_id else None,
    )
    scratch.events.append(rejected_event)
    scratch.confirmation_answered_this_turn = True

    draft = _CONFIRMATION_REJECTED_TEMPLATE.format(template_line=slot.template_line)
    return _finalize_response(draft, packet, ctx, scratch)


def _handle_confirmation_accepted(  # noqa: PLR0913, PLR0915 — one keyword per D6 answer-path input (each load-bearing); single-pass mint+re-propose+gate+dispatch+interpret mirrors `_dispatch_one_tool_call`'s own noqa'd shape — splitting would only scatter the audit trace.
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
       dispatch via the L4 registry, Result Interpreter on the
       returned slot, fixed broadcast.
    """
    requested_event_uid = _latest_event_uid_of_type(
        ctx.conn, event_type="confirmation.requested",
    )
    accepted_event = emit_event(
        ctx.conn,
        type="confirmation.accepted",
        payload={
            "confirmation_id": slot.confirmation_id,
            "utterance_raw": transcript,
            "grammar_rule_id": grammar_hit.rule_id,
        },
        source_event_id=requested_event_uid,
        correlation={"turn_id": scratch.turn_id} if scratch.turn_id else None,
    )
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

    lease: AuthorizationLease = {
        "lease_id": _new_lease_id(),
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
    action_id = _new_action_id()
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
    # THIS turn's acceptance. Re-fold fresh off the log, mirroring how
    # `_dispatch_one_tool_call` overlays a just-emitted `entity.resolved`
    # onto `gate_entity_registry` rather than trusting the packet's copy
    # (ADR-0011 §12.2 MUST-FIX 1) — same "the event is already durable;
    # only the caller's cached VIEW of it needs a refresh" shape.
    pending_confirmations = make_snapshot(ctx.conn).pending_confirmations
    gate = pre_action_gate(
        action_request,
        policy,
        packet.task_ledger_snapshot,
        tool_def=tool_def,
        entity_registry=packet.entity_registry,
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

    bundle = ctx.tool_registry.dispatch(
        action_request, ctx.conn, ctx.runtime_paths, ctx.lifecycle,
    )
    primary_result_slot = bundle.slots[0]

    result_observed_uid = _latest_event_uid_of_type(
        ctx.conn, event_type="action.result_observed",
    )
    source_event_for_interpreter = result_observed_uid or proposed_event.event_uid
    interpreted_events = result_interpreter(
        primary_result_slot,
        source_event_id=source_event_for_interpreter,
        action_request=action_request,
        conn=ctx.conn,
        subject_ref_override=target_entity_ref,
    )
    scratch.events.extend(interpreted_events)

    if primary_result_slot.error is not None:
        draft = _CONFIRMATION_DISPATCH_ERROR_TEMPLATE.format(error=primary_result_slot.error)
        return _finalize_response(draft, packet, ctx, scratch)

    path_written = primary_result_slot.payload.get("path", "?")
    bytes_written = primary_result_slot.payload.get("bytes_written", "?")
    draft = _CONFIRMED_WRITE_SUCCESS_TEMPLATE.format(
        path=path_written, bytes_written=bytes_written,
    )
    return _finalize_response(draft, packet, ctx, scratch)


def _resolver_outcome(result: ResolverResult) -> str:
    """Map ResolverResult.confidence -> entity.resolved.outcome.

    Per ADR § Resolver contract table. The ladder is
    ``{"resolved", "ambiguous", "not_found"}`` — ``"not_found"`` is the
    canonical 0-candidate outcome (formerly ``"failed"``).
    """
    if result.confidence in ("exact", "high"):
        return "resolved"
    if result.confidence == "fuzzy":
        if result.resolved_to is not None:
            return "resolved"
        return "ambiguous"
    return "not_found"


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


def _verify_diff_already_proposed_this_turn(
    conn: sqlite3.Connection,
    *,
    turn_id: str | None,
    task_id: str | None,
) -> bool:
    """True if a ``verify_diff`` was already proposed for ``task_id`` this turn.

    Deterministic idempotence signal for the decision loop. The untrusted
    Tier-2 LLM (spec §3.4.6) may re-propose ``verify_diff`` for a task whose
    verification was already settled this turn — re-verifying the same
    artifact yields no new evidence and risks a duplicate ``task.verified``.
    Spec §3.4.3/§3.4.4 put ``open_actions`` in the Situation Packet precisely
    so the decision engine picks the next action from current state rather
    than redundantly re-acting; this query is the deterministic backstop the
    Pre-action Gate consults to refuse the redundant proposal.

    Turn- and task-scoped: a verify_diff in another turn, or for another task,
    does not count. ``None`` turn/task fails open (treated as not-redundant),
    so the first proposal of a turn always proceeds.
    """
    if turn_id is None or task_id is None:
        return False
    cursor = conn.execute(
        "SELECT 1 FROM events "
        "WHERE type = 'action.proposed' "
        "AND json_extract(payload_json, '$.tool_name') = 'verify_diff' "
        "AND json_extract(payload_json, '$.target_entity_ref') = ? "
        "AND json_extract(payload_json, '$.turn_id') = ? "
        "LIMIT 1",
        (task_id, turn_id),
    )
    return cursor.fetchone() is not None


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
