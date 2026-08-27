"""Composition root — the only module allowed to wire across middle layers.

Per ADR 0001 § Six-layer boundary contract and ``.importlinter`` layer
rules: ``jarvis.runtime`` is the SINGLE module in the codebase that may
import ``jarvis.decision``, ``jarvis.execution``, ``jarvis.surface``,
``jarvis.deployment`` simultaneously. Every other module respects the
sibling-isolation contract via Protocols.

Responsibilities (Day-1):

1. :func:`bootstrap_runtime_app` — load YAML config, render the system
   prompt, bootstrap L6 paths, open the L2 event log, build the L4
   default registry + lifecycle, instantiate the L3 LLM client, and
   return a frozen :class:`JarvisRuntime`. No LLM call happens during
   bootstrap.
2. :func:`run_turn` — drive ONE conversation turn end-to-end:
   emit ``surface.user_intent`` (L5), call :func:`jarvis.decision.decide`
   (re-entering as more triggers arrive), record the Pre-emit token,
   and render the final ``ResponsePlan`` to stdout (L5).
3. :func:`_wait_for_next_trigger` — poll the event log for the next
   L3 trigger event (``worker.reported``, ``action.result_observed``,
   ``action.timeout_assumed``, or ``action.failed``) produced by L4's
   ``threading.Timer`` worker thread. ``time.sleep`` is intentional
   here per spec §3.4.1: the composition root polls across thread
   boundaries; the "no time.sleep" rule applies only to L3 / L4 gate
   machinery.

Layer rules: ``jarvis.runtime`` may import everything below it. It is
imported by ``jarvis.cli`` only.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Mapping  # runtime use: isinstance in the config readers.
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import yaml

from jarvis.decision import (
    DecideContext,
    EntityResolverLike,
    LifecycleLike,
    ResolvedEntityLike,
    ToolRegistryLike,
    decide,
)
from jarvis.decision.confirm_grammar import ConfirmGrammarConfigError, load_confirm_grammar
from jarvis.decision.llm import LLMClient, load_llm_config
from jarvis.decision.policy import (
    PolicyConsistencyError,
    effective_policy,
    validate_requires_confirmation,
)
from jarvis.decision.result_interpreter import emit_stash_conflict_surfacing
from jarvis.decision.tier0 import Tier0ConfigError, load_tier0_table, validate_tier0_table
from jarvis.deployment import RuntimePaths, bootstrap_runtime, load_env_file
from jarvis.execution.diff_capture import StashError, restore_pretask_changes
from jarvis.execution.path_resolver import (
    FileTargetsConfigError,
    load_file_targets_config,
    resolve_write_target,
)
from jarvis.execution.path_resolver import resolve as resolve_file_entity
from jarvis.execution.tools import (
    DEFAULT_OBSIDIAN_VAULT_ROOT,
    DEFAULT_SCREEN_MAX_WIDTH_PX,
    DEFAULT_WEB_FETCH_MAX_BYTES,
    DEFAULT_WEB_FETCH_MAX_TEXT_BYTES,
    DEFAULT_WEB_SEARCH_MAX_RESULTS,
    DEFAULT_WEB_SEARCH_PROVIDER,
    DEFAULT_WEB_TIMEOUT_S,
    ActionLifecycle,
    ToolRegistry,
    VisionClient,
    build_default_registry,
    release_turn_actions,
    turn_action_ids,
)
from jarvis.shared import CallerPrincipal, Event
from jarvis.state.event_log import iter_events, open_event_log
from jarvis.surface.cli import (
    PreEmitTokenError,
    SurfaceState,
    emit_surface_user_intent,
    parse_response_channels,
    record_pre_emit_token,
)
from jarvis.surface.cli_render import render_response

if TYPE_CHECKING:
    import sqlite3

    from jarvis.decision import ResponsePlan
    from jarvis.decision.confirm_grammar import ConfirmGrammarTable
    from jarvis.decision.tier0 import Tier0Table


LOGGER = logging.getLogger("jarvis.runtime")


# --- Defaults ---------------------------------------------------------------

# Default config / prompt asset paths relative to the repo root. The
# composition root resolves the repo root by walking up from this module
# until a ``config/jarvis.yaml`` exists; tests / users may override.
_DEFAULT_CONFIG_FILENAME = Path("config") / "jarvis.yaml"
_DEFAULT_PROMPT_FILENAME = Path("prompts") / "jarvis_v1.md"

# Trigger event types the runtime loop expects from L4 async paths.
# ``worker.reported`` is the spawn_worker happy-path Timer event.
# ``action.result_observed`` is defensive — sync tools emit it inline
# so decide() consumes it within one invocation, but we accept it here
# so a late-firing scheduled re-entry does not deadlock the poll loop.
# ``action.timeout_assumed`` and ``action.failed`` are the spawn_worker
# terminal failure events (B-0003b): the Codex turn timed out or the
# subprocess crashed; the runtime must wake decide() so L3 can fold a
# Limitation Claim onto the trace and emit a canonical limitation
# response. Without these in the trigger set the runtime waiter would
# deadlock and the user would see silence after a 10-min Codex hang.
_RUNTIME_TRIGGER_TYPES: tuple[str, ...] = (
    "worker.reported",
    "action.result_observed",
    "action.timeout_assumed",
    "action.failed",
)

# Default polling cadence for ``_wait_for_next_trigger``. 10 ms balances
# CPU usage with first-byte latency once the Timer fires.
_DEFAULT_POLL_INTERVAL_S: float = 0.01

# Default per-turn iteration ceiling. Protects ``run_turn`` from a stuck
# trigger chain. ADR § Acceptance F2 puts the Day-1 happy path at ~3
# iterations (utterance -> worker.reported -> verify-result composition),
# so 50 is ample headroom.
_DEFAULT_MAX_ITERATIONS: int = 50

# Default per-trigger wait. The spawn_worker Timer fires at ~10 ms in
# Day-1; ``verify_diff`` re-entry happens inline. 5 s is generous.
_DEFAULT_TRIGGER_TIMEOUT_S: float = 5.0

# Fallback for a runtime whose config carries no ``observer:`` block
# (hand-assembled test runtimes). ``config/jarvis.yaml`` is the real
# source; mirroring the shipped default here means a missing block reads
# the same cadence the daemon would actually poll at.
_FALLBACK_OBSERVER_POLL_INTERVAL_S: float = 60.0

# Fallback for a runtime whose config carries no ``confirmation:`` block.
# Mirrors ``jarvis.decision._DEFAULT_CONFIRMATION_TTL_MS`` (private in
# that module — not imported here, same as
# ``_FALLBACK_OBSERVER_POLL_INTERVAL_S`` above keeps its own mirror
# rather than importing ``packet.DEFAULT_OBSERVER_POLL_INTERVAL_S``).
_FALLBACK_CONFIRMATION_TTL_MS: int = 600_000

# Default `tools.screen.vision_preset` (ADR-0011 D7) — the `llm.presets.*`
# key `screen_look` reads for its one vision call when the config's
# `tools.screen` block doesn't override it.
_DEFAULT_VISION_PRESET_NAME: str = "vision"

# System prompt for the injected vision client (`_LLMVisionClient` below).
# Directed at the vision model, not Allen, so it stays English; asking for
# a Chinese answer means `screen_look`'s text observation slots straight
# into the rest of Jarvis's Chinese-speaking pipeline without a translation
# hop.
_VISION_SYSTEM_PROMPT: str = (
    "You are a screen-reading assistant. Describe what is currently "
    "visible in the screenshot factually and concisely. Respond in "
    "Chinese (中文)."
)

# Fallback ``tools.obsidian.vault_root`` (ADR-0011 D7) for a runtime
# whose config carries no ``tools:`` block. NIT-FIX 8 (ADR-0011 §12):
# unlike ``_FALLBACK_OBSERVER_POLL_INTERVAL_S`` above, this one does
# NOT hold its own copy of the literal — two copies of the same vault
# path in two layers can silently drift. `jarvis.execution.tools`
# (L4, the layer this fallback exists for) owns
# ``DEFAULT_OBSIDIAN_VAULT_ROOT``; `jarvis.runtime` just imports it —
# a higher-layer-imports-lower-layer edge `.importlinter` allows.


# --- Exceptions -------------------------------------------------------------


class RuntimeBootstrapError(RuntimeError):
    """Raised when :func:`bootstrap_runtime_app` cannot assemble the runtime."""


class TriggerWaitTimeout(RuntimeError):  # noqa: N818 — Day-1 vocabulary keeps `Timeout` suffix per ADR § Multi-trigger loop.
    """Raised by :func:`_wait_for_next_trigger` when no trigger arrives in time."""


# --- ADR-0011 D4 — resolve-on-propose wiring --------------------------------


@dataclass(frozen=True)
class _ResolvedFileEntity:
    """Adapts `path_resolver.ResolvedTarget` to L3's `ResolvedEntityLike`.

    `jarvis.runtime` is the only module allowed to know both the L3
    Protocol shape and the L4 `ResolvedTarget` dataclass it adapts —
    that is the whole point of the resolve-on-propose seam (ADR-0011
    D4): L3 never imports `jarvis.execution.path_resolver` directly.
    """

    entity_id: str
    canonical: str
    confidence: str
    match_basis: str


def _make_entity_resolver(conn: sqlite3.Connection) -> EntityResolverLike:
    """Build the resolve-on-propose callable (ADR-0011 D4), closing over `conn`.

    Wired into `DecideContext.entity_resolver`; `_dispatch_one_tool_call`
    calls it for a `requires_entity=True` tool whose `target_entity_ref`
    is still unset. `path_resolver.resolve` never raises for a bad or
    unresolvable query (its own docstring's contract) — a miss is the
    normal `None` return, not an exception, so this wrapper adds no
    try/except of its own; a real bug inside `resolve` should surface,
    not be swallowed here.
    """

    def _resolve(query: str) -> ResolvedEntityLike | None:
        target = resolve_file_entity(query, "file", conn)
        if target is None:
            return None
        return _ResolvedFileEntity(
            entity_id=f"file:{target.path}",
            canonical=str(target.path),
            confidence="bookmark" if target.source == "bookmark" else "fuzzy",
            match_basis=target.source,
        )

    return _resolve


def _make_write_entity_resolver(conn: sqlite3.Connection) -> EntityResolverLike:
    """Build the `write_file` resolve-on-propose callable (ADR-0012 D1).

    Mirrors :func:`_make_entity_resolver` but calls
    `path_resolver.resolve_write_target` instead of `path_resolver.resolve`,
    so a non-existent target whose parent directory is in scope also
    resolves (D1's extension of ADR-0011 D4 for write targets). Wired
    into `DecideContext.write_entity_resolver`;
    `_dispatch_one_tool_call` picks this resolver instead of
    `entity_resolver` when the tool being resolved is `write_file`.
    """

    def _resolve(query: str) -> ResolvedEntityLike | None:
        target = resolve_write_target(query, conn)
        if target is None:
            return None
        return _ResolvedFileEntity(
            entity_id=f"file:{target.path}",
            canonical=str(target.path),
            confidence="bookmark" if target.source == "bookmark" else "fuzzy",
            match_basis=target.source,
        )

    return _resolve


def _entity_bookmarks() -> tuple[tuple[str, str], ...]:
    """Load `config/file_targets.yaml` bookmarks as `(alias, abs-path)` pairs.

    Seeds the EntityRegistry projection's config route (ADR-0011 D4).
    `jarvis.state` may not import `jarvis.execution.path_resolver`, so
    the composition root loads the config here and threads the pairs
    down as plain data — the same reason `tier0_table` is loaded here
    and threaded rather than re-parsed inside `jarvis.decision`.
    """
    return tuple(
        (alias, str(path))
        for alias, path in load_file_targets_config().bookmarks.items()
    )


# --- Public dataclasses -----------------------------------------------------


@dataclass(frozen=True)
class JarvisRuntime:
    """Assembled runtime — every cross-layer wire lives here.

    Frozen so the composition root can hand it across to ``run_turn``
    and tests / canary scans without worrying about mutation. The
    ``conn`` field is the only mutable resource by nature
    (``sqlite3.Connection``); everything else is immutable values.

    L5 :class:`SurfaceState` is deliberately not a field here.
    Per spec §3.6.7 (Inherent boundaries), local surface state has no
    truth, doesn't survive across turns, and doesn't affect decisions —
    so ``run_turn`` allocates a fresh empty :class:`SurfaceState`
    locally each invocation and discards it after
    :func:`jarvis.surface.cli_render.render_response`.

    Attributes:
        config: Raw parsed YAML config (read-only mapping form).
        runtime_paths: Bootstrapped L6 layout (root + event_log +
            artifacts_root + registry).
        conn: Open Event Log connection. The caller closes it when
            the process exits.
        tool_registry: L4 default registry (spawn_worker + verify_diff).
        lifecycle: L4 per-process action lifecycle FSM.
        llm_client: L3 multi-provider LLM client.
        system_prompt: Rendered system prompt string (verbatim
            content of ``prompts/jarvis_v1.md``).
        tier0_table: Spec §17 Tier 0 whitelist loaded from
            ``config/tier0_patterns.yaml``; empty tuple = Tier 0
            disabled.
        entity_bookmarks: ``(alias, absolute-path)`` pairs loaded from
            ``config/file_targets.yaml`` (ADR-0011 D4) — seeds the
            EntityRegistry projection's config route. Empty tuple =
            no bookmarks configured.
        confirm_grammar_table: ADR-0012 §3 D6 exact-sentence yes/no
            grammar loaded from ``config/confirm_grammar.yaml``; empty
            tuple = the answer-path grammar hook disabled (same "off
            means inert" posture as an empty ``tier0_table``).
    """

    config: Mapping[str, Any]
    runtime_paths: RuntimePaths
    conn: sqlite3.Connection
    tool_registry: ToolRegistry
    lifecycle: ActionLifecycle
    llm_client: LLMClient
    system_prompt: str
    tier0_table: Tier0Table = ()
    entity_bookmarks: tuple[tuple[str, str], ...] = ()
    confirm_grammar_table: ConfirmGrammarTable = ()


@dataclass(frozen=True)
class RunTurnResult:
    """Outcome of :func:`run_turn` for one conversation turn.

    Attributes:
        response_text: The exact text the surface adapter wrote to
            stdout (post channel-split: ``document`` channel, falling
            back to ``voice`` if document was empty).
        response_plan: The final approved :class:`ResponsePlan` from
            :func:`jarvis.decision.decide`.
        turn_id: Correlation id used across the trace.
        iterations: Number of :func:`jarvis.decision.decide` invocations
            this turn drove (1 for sync-only, 2 for the Day-1 happy
            path with one ``worker.reported`` re-entry).
        events_emitted: Frozen tuple of every Event the decide()
            invocations emitted (concatenated across iterations).
        attention_channel: L3 Attention Policy verdict from the final
            decide() invocation. Drives the Step-18 surface-render
            dispatch in :func:`jarvis.surface.cli_render.render_response`.
    """

    response_text: str
    response_plan: ResponsePlan
    turn_id: str
    iterations: int
    events_emitted: tuple[Event, ...]
    attention_channel: str


# --- bootstrap_runtime_app --------------------------------------------------


def _locate_repo_root(start: Path) -> Path:
    """Walk up from ``start`` until ``config/jarvis.yaml`` exists; return that dir."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / _DEFAULT_CONFIG_FILENAME).is_file():
            return candidate
    msg = (
        f"runtime: could not locate config/jarvis.yaml by walking up from {start}. "
        "Pass config_path=Path(...) explicitly to bootstrap_runtime_app."
    )
    raise RuntimeBootstrapError(msg)


def _load_full_config(path: Path) -> Mapping[str, Any]:
    """Parse the entire YAML config file (not just the ``llm:`` block)."""
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        msg = f"config at {path} is not a YAML mapping"
        raise RuntimeBootstrapError(msg)
    return raw


def _positive_float(value: object, fallback: float) -> float:
    """Coerce a YAML scalar to a positive float; ``fallback`` on anything else.

    ``bool`` is excluded explicitly because it is an ``int`` subclass —
    ``poll_interval_s: true`` would otherwise become a 1-second poll.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    return float(value) if value > 0 else fallback


def _observer_poll_interval_s(config: Mapping[str, Any]) -> float:
    """Return ``observer.poll_interval_s`` in seconds (ADR-0009 D5).

    Read by TWO consumers, which is the whole reason it lives here in the
    composition root rather than inside the daemon module: the observer
    task polls at this cadence, and :func:`drive_turn` hands the same
    number to L3 so the Status Board note's stale threshold (3x the poll
    interval, ADR-0009 D6 v0) is derived from the cadence the daemon
    actually runs at. Configure the interval away from 60 and the note's
    judgement moves with it instead of silently diverging.
    """
    block = config.get("observer")
    if not isinstance(block, Mapping):
        return _FALLBACK_OBSERVER_POLL_INTERVAL_S
    return _positive_float(
        block.get("poll_interval_s"), _FALLBACK_OBSERVER_POLL_INTERVAL_S,
    )


def _confirmation_ttl_ms(config: Mapping[str, Any]) -> int:
    """Return ``confirmation.ttl_ms`` (ADR-0012 §3 D4/V2 — default 10 min).

    Config-overridable so the live burn (ADR-0012 §7 acceptance row
    C3, TTL expiry) can use a short value without touching code. Same
    fill-only-with-fallback shape as :func:`_observer_poll_interval_s`
    above; ``bool`` is excluded explicitly for the same reason
    :func:`_positive_float` excludes it (``bool`` is an ``int``
    subclass).
    """
    block = config.get("confirmation")
    if not isinstance(block, Mapping):
        return _FALLBACK_CONFIRMATION_TTL_MS
    value = block.get("ttl_ms")
    if isinstance(value, bool) or not isinstance(value, int):
        return _FALLBACK_CONFIRMATION_TTL_MS
    return value if value > 0 else _FALLBACK_CONFIRMATION_TTL_MS


def _observer_repo_paths(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the watched repos from ``observer.repos``; empty = observer off.

    ``~`` is expanded here so a config line like ``~/Projects/jarvis``
    does not degrade into a silent per-cycle F6 skip. Non-string and
    blank entries are dropped rather than raising: a typo in one row of
    an Allen-managed list must not refuse to boot the daemon.
    """
    block = config.get("observer")
    if not isinstance(block, Mapping):
        return ()
    raw = block.get("repos")
    if not isinstance(raw, list):
        return ()
    return tuple(
        str(Path(item).expanduser())
        for item in raw
        if isinstance(item, str) and item.strip()
    )


def _obsidian_vault_root(config: Mapping[str, Any]) -> Path:
    """Return `tools.obsidian.vault_root`, `~`-expanded (ADR-0011 D7).

    Threaded into `build_default_registry`'s `search_notes` closure at
    registry-build time — L4 handlers do not load YAML themselves
    (same reason `tier0_table` / `observer_poll_interval_s` are read
    here and threaded down rather than re-parsed inside `jarvis.decision`
    or `jarvis.execution`). A missing/malformed `tools:` or `obsidian:`
    block degrades to the shipped default rather than failing boot —
    `search_notes` on a misconfigured key still resolves quietly to
    "vault not found" (same posture as `_observer_poll_interval_s`).
    """
    block = config.get("tools")
    if isinstance(block, Mapping):
        obsidian_block = block.get("obsidian")
        if isinstance(obsidian_block, Mapping):
            raw = obsidian_block.get("vault_root")
            if isinstance(raw, str) and raw.strip():
                return Path(raw).expanduser()
    return DEFAULT_OBSIDIAN_VAULT_ROOT


def _web_search_provider_config(config: Mapping[str, Any]) -> tuple[str, str | None]:
    """Return `(search_provider, api_key)` from `tools.web.*`.

    The key is resolved HERE, from the env var named by
    `tools.web.search_api_key_env` — same indirection the LLM presets
    use (`api_key_env`), so no credential is ever written into
    `config/jarvis.yaml`. L4 receives the resolved value; it does not
    read YAML or the environment itself.

    Best-effort like its `tools.web` siblings: a missing block, an
    unknown provider name, or an unset variable all degrade inside
    `_resolve_search_backend` rather than failing boot.
    """
    provider = DEFAULT_WEB_SEARCH_PROVIDER
    key_env: str | None = None
    block = config.get("tools")
    if isinstance(block, Mapping):
        web_block = block.get("web")
        if isinstance(web_block, Mapping):
            raw_provider = web_block.get("search_provider")
            if isinstance(raw_provider, str) and raw_provider.strip():
                provider = raw_provider.strip()
            raw_key_env = web_block.get("search_api_key_env")
            if isinstance(raw_key_env, str) and raw_key_env.strip():
                key_env = raw_key_env.strip()
    api_key = os.environ.get(key_env) if key_env else None
    return provider, (api_key.strip() if api_key else None)


def _web_tools_config(config: Mapping[str, Any]) -> tuple[int, int, int, float]:
    """Return `(search_max_results, fetch_max_bytes, fetch_max_text_bytes, timeout_s)`.

    Read from `tools.web.*`.

    ADR-0011 D7. Same best-effort posture as `_obsidian_vault_root` — a
    missing/malformed `tools.web` block degrades to the shipped
    defaults rather than failing boot; these are UX knobs, not trust
    sources.

    D7's schema ships ONE `timeout_s` shared by `web_search` and
    `web_fetch`, though D5's prose separately gives the two tools
    different timeouts (15s / 20s) — see `jarvis.execution.tools`'s
    `DEFAULT_WEB_TIMEOUT_S` docstring for the full reconciliation
    (ADR-0011 §12 errata candidate). The single configured value is
    applied to both tools here.
    """
    search_max_results = DEFAULT_WEB_SEARCH_MAX_RESULTS
    fetch_max_bytes = DEFAULT_WEB_FETCH_MAX_BYTES
    fetch_max_text_bytes = DEFAULT_WEB_FETCH_MAX_TEXT_BYTES
    timeout_s = DEFAULT_WEB_TIMEOUT_S
    block = config.get("tools")
    if isinstance(block, Mapping):
        web_block = block.get("web")
        if isinstance(web_block, Mapping):
            raw_results = web_block.get("search_max_results")
            if (
                isinstance(raw_results, int)
                and not isinstance(raw_results, bool)
                and raw_results > 0
            ):
                search_max_results = raw_results
            raw_bytes = web_block.get("fetch_max_bytes")
            if isinstance(raw_bytes, int) and not isinstance(raw_bytes, bool) and raw_bytes > 0:
                fetch_max_bytes = raw_bytes
            raw_text_bytes = web_block.get("fetch_max_text_bytes")
            if (
                isinstance(raw_text_bytes, int)
                and not isinstance(raw_text_bytes, bool)
                and raw_text_bytes > 0
            ):
                fetch_max_text_bytes = raw_text_bytes
            raw_timeout = web_block.get("timeout_s")
            if (
                isinstance(raw_timeout, (int, float))
                and not isinstance(raw_timeout, bool)
                and raw_timeout > 0
            ):
                timeout_s = float(raw_timeout)
    return search_max_results, fetch_max_bytes, fetch_max_text_bytes, timeout_s


def _screen_tools_config(config: Mapping[str, Any]) -> tuple[str, int]:
    """Return `(vision_preset_name, max_width_px)` from `tools.screen.*` (ADR-0011 D7).

    Same best-effort posture as `_web_tools_config` — a missing/malformed
    `tools.screen` block degrades to the shipped defaults rather than
    failing boot; these are UX/cost knobs, not trust sources.
    """
    preset_name = _DEFAULT_VISION_PRESET_NAME
    max_width_px = DEFAULT_SCREEN_MAX_WIDTH_PX
    block = config.get("tools")
    if isinstance(block, Mapping):
        screen_block = block.get("screen")
        if isinstance(screen_block, Mapping):
            raw_preset = screen_block.get("vision_preset")
            if isinstance(raw_preset, str) and raw_preset.strip():
                preset_name = raw_preset
            raw_width = screen_block.get("max_width_px")
            if isinstance(raw_width, int) and not isinstance(raw_width, bool) and raw_width > 0:
                max_width_px = raw_width
    return preset_name, max_width_px


_VISION_CALL_TIMEOUT_S: Final[float] = 20.0
"""Bound on the vision preset's OpenAI SDK call (MUST-FIX 2, ADR-0011
§12). Same 20 s order as `DEFAULT_WEB_TIMEOUT_S` (Step 6's web tools) —
every other seam in `screen_look` is bounded (`_SCREEN_CAPTURE_TIMEOUT_S`
/ `_SIPS_TIMEOUT_S` = 10 s each), yet without this the one network call
had NO timeout: the SDK's own default is `Timeout(connect=5, read=600,
write=600, pool=600)` with `max_retries=2`, i.e. up to ~1800 s blocking
the synchronous `decide()` loop on a hung proxy. Deliberately scoped to
ONLY the vision client — the decision loop's own `LLMClient` (built
separately, below) shares the same unbounded-timeout omission, but a
`deep` preset with a large `max_tokens` can legitimately run long;
bounding it is a separate decision, out of this ADR's scope."""

_VISION_CALL_MAX_RETRIES: Final[int] = 1
"""Paired with `_VISION_CALL_TIMEOUT_S` so the worst case is a small
multiple of the timeout (~40 s: one attempt + one retry) rather than
half an hour."""

_VISION_ERROR_EXCERPT_MAX_CHARS: Final[int] = 300
"""Bound on the redacted excerpt `VisionCallError` carries (MUST-FIX 1a,
ADR-0011 §12) — independent of, and a first line of defense ahead of,
`_emit_tool_error`'s own byte cap in `jarvis.execution.tools`."""

_BASE64_DATA_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"data:[\w./+-]*;base64,[A-Za-z0-9+/=]+",
)
_LONG_BASE64_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9+/]{64,}={0,2}")


class VisionCallError(RuntimeError):
    """Raised by `_LLMVisionClient.describe_image` in place of the raw SDK error.

    MUST-FIX 1a, ADR-0011 §12: this is the ONE call site that holds the
    screenshot's base64 data URL, so an upstream (proxy or provider)
    that echoes the request body it failed on — a common thin-proxy
    error shape — would otherwise ship image bytes into `str(exc)`,
    which `screen_look`'s handler interpolates verbatim into
    `action.result_observed` (durable SQLite, spec §3.3.9 forbids this)
    and into the tool-result message the decision LLM sees. Redaction
    is by PATTERN (a `data:...;base64,` URL, or any standalone long
    base64-alphabet run), not by trusting the upstream's error shape —
    the whole point is that an arbitrary upstream can echo the request
    in a format this code has never seen.
    """


def _redact_vision_error(exc: Exception) -> str:
    """Scrub base64 image payloads out of a vision-call exception's message.

    Returns `"{type name}: {bounded, redacted excerpt}"` — diagnosable
    (the real exception type survives) without risking a fresh
    un-redacted echo downstream. `raise ... from None` at the call site
    severs the exception chain so nothing that later prints a full
    traceback (e.g. `__cause__`/`__context__`) can resurrect the
    original, un-redacted message either.
    """
    text = str(exc)
    text = _BASE64_DATA_URL_RE.sub("data:[redacted-base64]", text)
    text = _LONG_BASE64_RUN_RE.sub("[redacted-base64]", text)
    if len(text) > _VISION_ERROR_EXCERPT_MAX_CHARS:
        text = text[:_VISION_ERROR_EXCERPT_MAX_CHARS] + "…[truncated]"
    return f"{type(exc).__name__}: {text}"


class _LLMVisionClient:
    """Adapts `LLMClient` (L3) to `jarvis.execution.tools.VisionClient` (ADR-0011 D5/D7).

    L4 cannot import L3 (`.importlinter` sibling isolation), so
    `screen_look`'s one vision call is injected through this
    composition-root-only adapter. Reads the image bytes and
    base64-encodes them HERE, at call time, inside the vision request
    only — never in the event-log payload (spec §3.3.9 / ADR-0011 §3
    D5): the L4 handler only ever hands this a `Path` and gets a `str`
    back.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        """Wrap a vision-preset-bound `LLMClient` (see `_build_vision_client`)."""
        self._llm_client = llm_client

    def describe_image(self, image_path: Path, *, question: str | None) -> str:
        """Send one image + optional question to the vision preset; return text.

        Raises `VisionCallError` — never the raw SDK/HTTP exception —
        on any failure (MUST-FIX 1a, ADR-0011 §12): see that class's
        docstring for why.
        """
        data_url = "data:image/png;base64," + base64.b64encode(
            image_path.read_bytes(),
        ).decode("ascii")
        prompt_text = question or "Describe what is currently on this screen."
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        try:
            result = self._llm_client.chat(messages=messages, system=_VISION_SYSTEM_PROMPT)
        except Exception as exc:  # noqa: BLE001 — deliberately re-raised, scrubbed, as VisionCallError; see MUST-FIX 1a.
            raise VisionCallError(_redact_vision_error(exc)) from None
        return result.text or ""


def _build_vision_client(config: Mapping[str, Any], preset_name: str) -> VisionClient | None:
    """Build the `screen_look` vision seam from `llm.presets.<preset_name>` (ADR-0011 D7).

    Returns `None` when the preset is absent/malformed — either
    STRUCTURALLY (missing `llm`/`presets` block, preset not a mapping)
    or by CONTENT (an unsupported `provider` string, a non-numeric
    `max_tokens` — SHOULD-FIX 3, ADR-0011 §12): `screen_look` still
    registers (the menu stays complete) but every call degrades to a
    `vision_unconfigured` error observation (ADR-0011 §5). A malformed
    preset is one optional tool misconfigured, not a trust source going
    missing (contrast `_load_file_targets_config`'s deliberate
    fail-boot posture, which protects a trust source the gate
    consults) — so it must not take the whole daemon down at boot.

    A DEDICATED `LLMClient` is built here rather than reusing the
    decision loop's own client (constructed at step 4, below):
    `LLMClient.switch_model`/`_apply_preset` mutates the client's
    active provider/model/base_url/key in place, so sharing one
    instance between the decision loop's `chat()` calls and
    `screen_look`'s vision calls would risk leaving the WRONG model
    active for the next turn. This reuses the SAME loader
    (`LLMClient.__init__` / `_apply_preset` reading a `presets` dict) —
    not a parallel YAML parser — just a second instance scoped to one
    preset, with an explicit bounded `timeout_s`/`max_retries`
    (MUST-FIX 2) the decision client does not get.
    """
    llm_block = config.get("llm")
    if not isinstance(llm_block, Mapping):
        return None
    presets = llm_block.get("presets")
    if not isinstance(presets, Mapping):
        return None
    preset = presets.get(preset_name)
    if not isinstance(preset, Mapping):
        return None
    vision_llm_config: dict[str, Any] = {
        "provider": "openai",
        "presets": {preset_name: dict(preset)},
        "default_preset": preset_name,
        "timeout_s": _VISION_CALL_TIMEOUT_S,
        "max_retries": _VISION_CALL_MAX_RETRIES,
    }
    try:
        llm_client = LLMClient(vision_llm_config)
    except (ValueError, TypeError) as exc:
        LOGGER.warning(
            "screen_look: llm.presets.%s is malformed (%s: %s); screen_look will "
            "report vision_unconfigured at use time instead of the daemon failing to boot",
            preset_name,
            type(exc).__name__,
            exc,
        )
        return None
    return _LLMVisionClient(llm_client)


def bootstrap_runtime_app(
    *,
    config_path: Path | None = None,
    prompt_path: Path | None = None,
    runtime_root: Path | None = None,
) -> JarvisRuntime:
    """Assemble a frozen :class:`JarvisRuntime` ready for :func:`run_turn`.

    Wiring (bottom-up across the layer DAG):

    1. L6 bootstrap — :func:`jarvis.deployment.bootstrap_runtime` resolves
       the runtime root (env var / explicit / default) and creates the
       base directories.
    2. L2 event log — :func:`jarvis.state.event_log.open_event_log`
       opens (or creates) the SQLite spine, idempotently installing
       the schema / indexes / append-only triggers.
    3. L4 registry + lifecycle — :func:`jarvis.execution.tools.build_default_registry`
       registers ``spawn_worker`` + ``verify_diff``; the
       :class:`ActionLifecycle` is per-process FSM.
    3b. L3 Tier 0 whitelist — ``config/tier0_patterns.yaml`` parsed and
       cross-checked against the registry's ``regex_router`` surface.
    4. L3 LLM client — :class:`jarvis.decision.llm.LLMClient` reads the
       ``llm:`` block from the YAML config. Lazy SDK construction —
       no network call until ``run_turn`` actually invokes ``chat``.

    L5 :class:`SurfaceState` is allocated per turn inside :func:`run_turn`
    (spec §3.6.7 — local surface state owns no truth and doesn't survive
    across turns), so the composition root does not wire it here.

    Args:
        config_path: Optional explicit path to ``config/jarvis.yaml``.
            Default: resolved by walking up from this module's file.
        prompt_path: Optional explicit path to ``prompts/jarvis_v1.md``.
            Default: ``${repo_root}/prompts/jarvis_v1.md``.
        runtime_root: Optional override for the L6 runtime root
            (tests / sandboxed runs).

    Returns:
        Frozen :class:`JarvisRuntime`.

    Raises:
        RuntimeBootstrapError: When config / prompt files cannot be
            located or parsed.
    """
    # Path resolution. The composition root tolerates either
    # repo-rooted defaults or explicit overrides; we never look at
    # ``os.getcwd()``.
    if config_path is None:
        repo_root = _locate_repo_root(Path(__file__).parent)
        config_path = repo_root / _DEFAULT_CONFIG_FILENAME
    else:
        repo_root = config_path.resolve().parent.parent

    if prompt_path is None:
        prompt_path = repo_root / _DEFAULT_PROMPT_FILENAME

    if not config_path.is_file():
        msg = f"runtime: config file {config_path} does not exist"
        raise RuntimeBootstrapError(msg)
    if not prompt_path.is_file():
        msg = f"runtime: prompt file {prompt_path} does not exist"
        raise RuntimeBootstrapError(msg)

    # 1. L6 paths.
    paths = bootstrap_runtime(runtime_root)

    # 1b. ADR-0009 D1 — fill-only ``${runtime_root}/env`` loader, BEFORE
    #     any surface preflight reads the environment (launchd strips the
    #     shell env; secrets must not live in the 0644 plist).
    load_env_file(paths.root)

    # 2. L2 event log.
    conn = open_event_log(paths.event_log)

    # 3. L4 registry + lifecycle. Config is loaded here (ahead of step 4's
    #    LLM-config read) because `search_notes`/`web_search`/`web_fetch`
    #    need `tools.obsidian.vault_root` / `tools.web.*` threaded into
    #    the registry at build time (ADR-0011 D7) — L4 handlers do not
    #    load YAML themselves.
    full_config = _load_full_config(config_path)
    (
        web_search_max_results,
        web_fetch_max_bytes,
        web_fetch_max_text_bytes,
        web_timeout_s,
    ) = _web_tools_config(full_config)
    web_search_provider, web_search_api_key = _web_search_provider_config(full_config)
    vision_preset_name, screen_max_width_px = _screen_tools_config(full_config)
    registry = build_default_registry(
        obsidian_vault_root=_obsidian_vault_root(full_config),
        web_search_max_results=web_search_max_results,
        web_search_provider=web_search_provider,
        web_search_api_key=web_search_api_key,
        web_fetch_max_bytes=web_fetch_max_bytes,
        web_fetch_max_text_bytes=web_fetch_max_text_bytes,
        web_timeout_s=web_timeout_s,
        vision_client=_build_vision_client(full_config, vision_preset_name),
        screen_max_width_px=screen_max_width_px,
    )
    lifecycle = ActionLifecycle()

    # 3b. Spec §17 Tier 0 whitelist — sits next to jarvis.yaml so Allen
    #     edits one config directory. Invalid content fails the boot
    #     loudly (no silent pattern drops); missing file = Tier 0 off.
    tier0_path = config_path.parent / "tier0_patterns.yaml"
    try:
        tier0_table = load_tier0_table(tier0_path)
        regex_router_tools = registry.for_caller(CallerPrincipal.REGEX_ROUTER)
        validate_tier0_table(
            tier0_table,
            allowed_tool_names=frozenset(t.name for t in regex_router_tools),
            async_tool_names=frozenset(t.name for t in regex_router_tools if t.is_async),
            entity_required_tool_names=frozenset(
                t.name for t in regex_router_tools if t.requires_entity
            ),
            # ADR-0012 §3 D5 — a Tier 0 row must never target a
            # `requires_confirmation` tool (Tier 0 has no LLM to
            # receive Allen's confirmation answer).
            requires_confirmation_tool_names=frozenset(
                t.name for t in regex_router_tools if t.requires_confirmation
            ),
        )
    except Tier0ConfigError as exc:
        msg = f"runtime: {tier0_path} invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 3c. ADR-0011 D2 — boot invariant: every registered tool's
    #     `requires_confirmation` must equal `risk_rank(risk_level) >=
    #     risk_rank(confirmation_threshold)`, so the two fields can never
    #     drift (spec §14.2 / ADR-0011 V6). Loud failure, same shape as
    #     the Tier 0 block above.
    try:
        validate_requires_confirmation(
            registry.get_definitions(),
            effective_policy().confirmation_threshold,
        )
    except PolicyConsistencyError as exc:
        msg = f"runtime: tool registry requires_confirmation invariant violated: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 3d. ADR-0011 D4 — EntityRegistry config seed. A missing/empty
    #     `file_targets.yaml` still degrades to no bookmarks (same
    #     posture as `open_path`'s own use of this config) — genuinely
    #     best-effort. A MALFORMED file is different: it would silently
    #     remove a trust source the Pre-action Gate consults (ADR-0011
    #     §12.2), so `load_file_targets_config` raising
    #     `FileTargetsConfigError` fails the boot loudly instead of
    #     degrading, matching steps 3b/3c above — a silent trust
    #     reduction is worse than a loud boot failure.
    try:
        entity_bookmarks = _entity_bookmarks()
    except FileTargetsConfigError as exc:
        msg = f"runtime: config/file_targets.yaml invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 3e. ADR-0012 §3 D6 — answer-path grammar table. Same posture as
    #     3b above: sits next to jarvis.yaml, missing file = the
    #     grammar hook disabled (inert, not broken), malformed content
    #     fails the boot loudly.
    confirm_grammar_path = config_path.parent / "confirm_grammar.yaml"
    try:
        confirm_grammar_table = load_confirm_grammar(confirm_grammar_path)
    except ConfirmGrammarConfigError as exc:
        msg = f"runtime: {confirm_grammar_path} invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 4. L3 LLM client. `full_config` was already loaded at step 3 above.
    llm_config = load_llm_config(config_path)
    llm_client = LLMClient(llm_config)

    # 5. Prompt.
    system_prompt = prompt_path.read_text(encoding="utf-8")

    return JarvisRuntime(
        config=full_config,
        runtime_paths=paths,
        conn=conn,
        tool_registry=registry,
        lifecycle=lifecycle,
        llm_client=llm_client,
        system_prompt=system_prompt,
        tier0_table=tier0_table,
        entity_bookmarks=entity_bookmarks,
        confirm_grammar_table=confirm_grammar_table,
    )


# --- run_turn ---------------------------------------------------------------


def _new_turn_id() -> str:
    """Mint a fresh turn id (``"T" + 8-hex``)."""
    return "T" + uuid.uuid4().hex[:8]


def _latest_row_id(conn: sqlite3.Connection) -> int:
    """Return the current MAX(events.id) or 0 if the table is empty."""
    cursor = conn.execute("SELECT IFNULL(MAX(id), 0) FROM events")
    row = cursor.fetchone()
    if row is None:
        return 0
    return int(row[0])


# Candidate rows for the in-turn waiter. NOT ``LIMIT 1``: since
# ADR-0009 D4 the waiter also filters by action_id (see
# :func:`_wait_for_next_trigger`), so a foreign orphan's terminal row
# sitting at the head of the cursor must be skipped over rather than
# blocking every later row this turn actually owns.
_SELECT_NEXT_TRIGGER_SQL = (  # noqa: S608 — placeholders interpolation is over a hard-coded type tuple, not user input.
    "SELECT id, event_uid, type, schema_version, ts_epoch_ms, "
    "payload_json, source_event_id, correlation_json "
    "FROM events WHERE id > ? AND type IN ({placeholders}) "
    "ORDER BY id ASC"
).format(placeholders=",".join("?" for _ in _RUNTIME_TRIGGER_TYPES))


def _hydrate_event_row(row: tuple[Any, ...]) -> Event:
    """Re-hydrate an events-table row into a frozen :class:`Event`.

    Mirrors ``jarvis.state.event_log._row_to_event`` (which is module-private
    by L2 design). The runtime composition root may legitimately read
    rows back out of the log when waiting for cross-thread triggers.
    """
    (
        _id,
        event_uid,
        type_,
        schema_version,
        ts_epoch_ms,
        payload_json,
        source_event_id,
        correlation_json,
    ) = row
    payload: dict[str, Any] = json.loads(payload_json)
    correlation: dict[str, str] | None = (
        None if correlation_json is None else json.loads(correlation_json)
    )
    return Event(
        event_uid=event_uid,
        type=type_,
        schema_version=schema_version,
        ts_epoch_ms=ts_epoch_ms,
        payload=payload,
        source_event_id=source_event_id,
        correlation=correlation,
    )


def _event_action_id(event: Event) -> str | None:
    """Return the action_id this event belongs to, or ``None``.

    Correlation first (the canonical slot every L4 emitter fills via
    ``_action_correlation``), payload second (all four trigger types
    mirror the id there, and the supervisor sweep's
    ``action.timeout_assumed`` fills both).

    ADR-0009 D4 partitions terminal events between two consumers — the
    in-turn waiter (``action_id`` IS one of the turn's own) and
    ``_system_trigger_watcher`` (``action_id`` is in NO live turn's set).
    Both predicates read the key through THIS function, so the two
    filters are complements of one another by construction and no event
    can be claimed twice or dropped by both.
    """
    for source in (event.correlation, event.payload):
        if source is None:
            continue
        value = source.get("action_id")
        if isinstance(value, str):
            return value
    return None


def _wait_for_next_trigger(
    conn: sqlite3.Connection,
    *,
    after_id: int,
    action_ids: frozenset[str],
    timeout: float,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> tuple[Event, int]:
    """Poll the event log for the next L3 trigger event after ``after_id``.

    Trigger types: ``worker.reported`` (spawn_worker happy-path
    Timer thread), ``action.result_observed`` (defensive — sync
    tools emit this inline so decide() already absorbed it, but a
    late re-entry from a stale Timer is accepted to keep the poll
    loop drainable), and ``action.timeout_assumed`` /
    ``action.failed`` (B-0003b — spawn_worker terminal failures
    emitted by the L4 handler when the Codex turn times out or the
    subprocess crashes; the runtime waiter must wake decide() so L3
    can fold a Limitation Claim).

    Scoping (ADR-0009 D4 / F9). Type + ``after_id`` alone is NOT a
    correlation: a supervisor sweep or wake reconciliation closing some
    *foreign* orphan writes an ``action.timeout_assumed`` into the same
    log, and the pre-D4 waiter would hand it to whichever turn happened
    to be in flight — which then adopts the orphan's correlation and
    ends the wrong turn with the wrong limitation. Every candidate row
    is therefore matched against ``action_ids``, the set of actions THIS
    turn dispatched; foreign rows are skipped over (the local cursor
    still advances past them, so they are examined once, not once per
    poll). This is a filter, not a new trigger type — the
    ``_RUNTIME_TRIGGER_TYPES`` tuple is untouched.

    The runtime composition root explicitly polls across thread
    boundaries; ``time.sleep`` is the cleanest primitive here. The
    "no time.sleep" rule is for L3/L4 gate machinery that must not
    block on wall-clock; the orchestration loop is a different
    concern (spec §3.4.1).

    Args:
        conn: Open Event Log connection. NOTE: this is the same
            connection :func:`bootstrap_runtime_app` opened — the
            ``threading.Timer`` callback opens its OWN connection per
            ``jarvis.execution.tools`` so ``check_same_thread=True``
            stays honored.
        after_id: The most recent SQLite row id the caller has
            already consumed. Returned events all have ``id >
            after_id``.
        action_ids: The action_ids this turn owns — read fresh per call
            from :func:`jarvis.execution.tools.turn_action_ids` so an
            action dispatched during the previous decide() iteration is
            already in scope. A row whose ``action_id`` is outside this
            set belongs to somebody else and is never returned.
        timeout: Hard wall-clock cap in seconds; raise
            :class:`TriggerWaitTimeout` if exceeded.
        poll_interval_s: Sleep between polls (default 10 ms).

    Returns:
        Tuple of ``(event, new_after_id)`` — the freshly-folded
        :class:`Event` plus the SQLite row id to use for the next
        wait call.

    Raises:
        TriggerWaitTimeout: No matching trigger arrived within
            ``timeout`` seconds.
    """
    deadline = time.monotonic() + timeout
    cursor_id = after_id
    while True:
        rows = conn.execute(
            _SELECT_NEXT_TRIGGER_SQL,
            (cursor_id, *_RUNTIME_TRIGGER_TYPES),
        ).fetchall()
        for row in rows:
            cursor_id = int(row[0])
            event = _hydrate_event_row(row)
            if _event_action_id(event) in action_ids:
                return event, cursor_id
        if time.monotonic() >= deadline:
            msg = (
                f"runtime: no trigger event of types {_RUNTIME_TRIGGER_TYPES!r} "
                f"for action_ids={sorted(action_ids)!r} arrived within "
                f"{timeout!r}s (after_id={after_id})."
            )
            raise TriggerWaitTimeout(msg)
        time.sleep(poll_interval_s)


def run_turn(
    runtime: JarvisRuntime,
    *,
    utterance: str,
    turn_id: str | None = None,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    trigger_timeout_s: float = _DEFAULT_TRIGGER_TIMEOUT_S,
) -> RunTurnResult:
    """Drive one conversation turn end-to-end (CLI / scenario entrypoint).

    Steps (spec §3.4.1 multi-trigger loop):

    1. Mint or accept a ``turn_id`` (composition root owns this; L3
       sees the same id on the trace correlation).
    2. Emit ``surface.user_intent`` via L5 surface adapter (spec
       §3.6.1) — this is the first trigger event.
    3. Delegate the post-emit body to :func:`drive_turn`, which drives
       the L3 decide loop, runs the stash-pop finalizer, and renders
       the final :class:`ResponsePlan` across the surfaces dictated by
       the L3 Attention Policy.

    The split into :func:`run_turn` (emit + delegate) and
    :func:`drive_turn` (post-emit body) exists so non-CLI surfaces can
    drive a turn without re-emitting ``surface.user_intent``. The
    daemon HTTP handler (ADR-0003 Step 7) writes the intent event from
    the request body, and the daemon watcher (Step 8) picks it up and
    calls :func:`drive_turn` directly — both bypass :func:`run_turn`.

    Args:
        runtime: Assembled :class:`JarvisRuntime`.
        utterance: User text to drive the turn.
        turn_id: Optional explicit turn id (tests). Default: minted.
        max_iterations: Hard ceiling on decide() invocations.
        trigger_timeout_s: Per-trigger wait timeout.

    Returns:
        Frozen :class:`RunTurnResult` describing what was written and
        the events emitted.
    """
    effective_turn_id = turn_id if turn_id is not None else _new_turn_id()

    utterance_event = emit_surface_user_intent(
        runtime.conn,
        transcript=utterance,
        turn_id=effective_turn_id,
    )

    return drive_turn(
        runtime,
        user_intent_event=utterance_event,
        max_iterations=max_iterations,
        trigger_timeout_s=trigger_timeout_s,
    )


def drive_turn(  # noqa: PLR0913 — composition-root entrypoint; argument set is the cross-surface contract (CLI + daemon watcher) and intentionally explicit.
    runtime: JarvisRuntime,
    *,
    user_intent_event: Event,
    available_surfaces: frozenset[str] | None = None,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    trigger_timeout_s: float = _DEFAULT_TRIGGER_TIMEOUT_S,
    streaming_enabled: bool = False,
) -> RunTurnResult:
    """Drive the post-emit body of one turn from an already-emitted intent event.

    Same body as the pre-extract ``run_turn`` (decide loop + finalize +
    render). The ``surface.user_intent`` event is supplied externally;
    :func:`drive_turn` does NOT re-emit it. The effective ``turn_id``
    is read from ``user_intent_event.payload["turn_id"]``.

    The caller is responsible for emitting ``surface.user_intent``
    first. This function exists so non-CLI surfaces — specifically the
    ADR-0003 daemon HTTP handler (writes the intent) and the daemon
    watcher (picks it up and drives) — can drive a turn from a
    pre-emitted event without double-emitting and looping the watcher.

    Steps (spec §3.4.1 multi-trigger loop):

    1. Call :func:`jarvis.decision.decide` with ``user_intent_event``
       as the first trigger. If it returns a :class:`ResponsePlan`,
       finalize immediately.
    2. Otherwise the decide() call paused on an async tool
       (spawn_worker); poll the event log for the next
       ``worker.reported`` / ``action.result_observed`` /
       ``action.timeout_assumed`` / ``action.failed`` row, re-enter
       decide() with that trigger, repeat.
    3. Run the stash-pop finalizer (ADR-0002 § Dirty-tree policy,
       amended 2026-08-25) from the ``finally`` — lexically after the
       last ``decide(...)`` call so the ``verify_command`` subprocess
       saw exactly Codex's tree, and unconditionally so an exception
       between decide() and finalization cannot orphan the pre-task
       stash. Canary ``test_canary_stash_pop_after_verify`` enforces
       the ordering inside :func:`drive_turn`.
    4. Record the Pre-emit token on a fresh :class:`SurfaceState`, then
       call :func:`jarvis.surface.cli_render.render_response` which
       routes the channel-split text across the L3 attention channel's
       physical surfaces (say / banner / stdout when attached) AND
       enforces the Pre-emit token check (canary H3) AND emits the
       audit ``surface.response_emitted`` event. When
       ``streaming_enabled=True`` (daemon path, ADR-0003 Step 2),
       render_response ALSO emits ``surface.response_open`` plus N
       ``surface.response_chunk`` rows BEFORE the audit
       ``surface.response_emitted``; the user transcript (lifted from
       ``user_intent_event.payload["transcript"]``) lands on the open
       payload as ``query``.

    The Pre-emit token check protects against a runtime that
    accidentally re-uses an old plan or fails to refresh the token —
    :class:`PreEmitTokenError` propagates out. It — and every other
    exception — still passes through the ``finally`` that releases this
    turn's live action_ids (ADR-0009 D4).

    Args:
        runtime: Assembled :class:`JarvisRuntime`.
        user_intent_event: The already-emitted ``surface.user_intent``
            :class:`Event`. Its ``payload["turn_id"]`` is the
            canonical turn id used through this turn.
        available_surfaces: Passed through to
            :func:`jarvis.surface.cli_render.render_response`; daemon
            callers use ``frozenset()`` to suppress physical surfaces.
        max_iterations: Hard ceiling on decide() invocations.
        trigger_timeout_s: Per-trigger wait timeout.
        streaming_enabled: Forwarded to
            :func:`jarvis.surface.cli_render.render_response`; the
            daemon watcher (ADR-0003 Step 2 Build 5) passes ``True``
            so the renderer emits the 3-event Inherent taxonomy.
            Default ``False`` preserves CLI single-emit semantics.

    Returns:
        Frozen :class:`RunTurnResult` describing what was written and
        the events emitted.
    """
    effective_turn_id = str(user_intent_event.payload["turn_id"])
    # ADR-0009 D4 — every action this turn dispatches registers itself
    # in the L4 live set (`ToolRegistry.dispatch`). The release MUST run
    # even when the turn raises: a leaked action_id is one the supervisor
    # sweep will refuse to close for the life of the process, so a crashed
    # turn would permanently protect the very orphan it created.
    try:
        decide_ctx = DecideContext(
            conn=runtime.conn,
            runtime_paths=runtime.runtime_paths,
            # ``ToolRegistry`` / ``ActionLifecycle`` satisfy the L3
            # Protocols structurally; the casts pin the boundary because
            # mypy's invariant generic stance over Protocol attribute
            # types treats the more-specific RawResult / LifecycleState
            # return types as a conflict.
            tool_registry=cast("ToolRegistryLike", runtime.tool_registry),
            lifecycle=cast("LifecycleLike", runtime.lifecycle),
            llm_client=runtime.llm_client,
            system_prompt=runtime.system_prompt,
            tier0_table=runtime.tier0_table,
            # ADR-0009 D5/D6 — the Status Board note calls an observation
            # "stale" at 3x the observer's poll interval. Read from the
            # same config key the observer task polls on, so the two can
            # never disagree about what "stale" means.
            observer_poll_interval_s=int(_observer_poll_interval_s(runtime.config)),
            # ADR-0011 D4 — EntityRegistry config seed, threaded the same
            # way tier0_table is threaded.
            entity_bookmarks=runtime.entity_bookmarks,
            # ADR-0011 D4 — resolve-on-propose. Built fresh per turn (a
            # trivial closure) rather than stored on JarvisRuntime: it
            # closes over `runtime.conn`, which the JarvisRuntime fields
            # above already carry, so there is nothing to cache.
            entity_resolver=_make_entity_resolver(runtime.conn),
            # ADR-0012 D1 — write-target resolve-on-propose. Same
            # per-turn-closure rationale as `entity_resolver` above.
            write_entity_resolver=_make_write_entity_resolver(runtime.conn),
            # ADR-0012 §3 D4/V2 — confirmation TTL, config-overridable
            # via `confirmation.ttl_ms` so the live burn can shorten it.
            confirmation_ttl_ms=_confirmation_ttl_ms(runtime.config),
            # ADR-0012 §3 D6 — answer-path grammar, threaded the same
            # way tier0_table is threaded.
            confirm_grammar_table=runtime.confirm_grammar_table,
        )

        # SQLite row id of the surface.user_intent event — used as the
        # "after_id" anchor for the trigger poll loop.
        last_seen_id = _latest_row_id(runtime.conn)

        collected_events: list[Event] = []
        trigger_event: Event = user_intent_event
        response_plan: ResponsePlan | None = None
        # Track the attention_channel from the final decide() iteration so
        # we can route the response to the right surfaces in Step 18.
        # Default ``"voice_notify"`` covers the flagship ADR-0002 scenario
        # when an L3 branch returns a plan without explicitly setting the
        # field; the L3 Attention Policy emits one of the 9 channels for
        # canonical branches today.
        final_attention_channel: str = "voice_notify"
        iterations = 0

        while response_plan is None and iterations < max_iterations:
            iterations += 1
            result = decide(trigger_event, decide_ctx)
            collected_events.extend(result.events_emitted)
            response_plan = result.response_plan
            if response_plan is not None:
                final_attention_channel = result.attention_channel
                break

            # No final plan -> decide() paused on an async tool. Wait for the
            # next L4-side trigger event.
            # ADR-0009 D4 / F9 — the waiter is scoped to the actions THIS
            # turn dispatched. Read fresh each iteration: the action
            # that paused decide() was registered inside the call above.
            next_event, last_seen_id = _wait_for_next_trigger(
                runtime.conn,
                after_id=last_seen_id,
                action_ids=turn_action_ids(effective_turn_id),
                timeout=trigger_timeout_s,
            )
            trigger_event = next_event

        if response_plan is None:
            msg = (
                f"drive_turn: exhausted max_iterations={max_iterations} without a final "
                f"ResponsePlan (turn_id={effective_turn_id!r})."
            )
            raise RuntimeBootstrapError(msg)

        # L5 emission (Step 18 — channel-split + multi-surface dispatch).
        # The Pre-emit token guard inside render_response() preserves the
        # canary H3 runtime check — calling record_pre_emit_token() then
        # render_response() in this order is the only legal path.
        # SurfaceState is allocated fresh per turn (spec §3.6.7 — local
        # surface state owns no truth and doesn't survive across turns)
        # and discarded once render_response returns the cleared state.
        surface_state = SurfaceState(last_gate_response_hash=None)
        primed_state = record_pre_emit_token(surface_state, response_plan.response_hash)

        # The in-memory capture stream serves two ends at once. First the
        # operator console: render_response() writes the cli_stdout slice
        # into it so the runtime can re-emit the same bytes to the real
        # sys.stdout. Second the test / programmatic caller: the returned
        # RunTurnResult.response_text is the captured string. Beyond
        # stdout, render_response also routes voice text to say and
        # document text to the notify banner per the channel mapping, and
        # emits the audit surface.response_emitted event itself.
        capture: io.StringIO = io.StringIO()
        transcript_raw = user_intent_event.payload.get("transcript", "")
        query = transcript_raw if isinstance(transcript_raw, str) else ""
        _, render_event = render_response(
            primed_state,
            response_plan,
            conn=runtime.conn,
            turn_id=effective_turn_id,
            attention_channel=final_attention_channel,
            stream=capture,
            available_surfaces=available_surfaces,
            streaming_enabled=streaming_enabled,
            query=query,
        )
        rendered = capture.getvalue()
        sys.stdout.write(rendered)
        sys.stdout.flush()
        collected_events.append(render_event)

        return RunTurnResult(
            response_text=rendered,
            response_plan=response_plan,
            turn_id=effective_turn_id,
            iterations=iterations,
            events_emitted=tuple(collected_events),
            attention_channel=final_attention_channel,
        )
    finally:
        # --- Stash-pop finalizer (ADR-0002 § Dirty-tree policy, amended
        # 2026-08-25). Lives in the finally: lexically after the last
        # decide() call — canary ``test_canary_stash_pop_after_verify``
        # enforces the ordering, so verify_diff read exactly Codex's
        # tree — and unconditionally, so an exception between decide()
        # and finalization cannot orphan Allen's pre-task stash. The
        # guard keeps the finally from masking the original exception.
        try:
            _pop_pending_stashes(
                runtime.conn,
                artifacts_root=runtime.runtime_paths.artifacts_root,
                turn_id=effective_turn_id,
            )
        except Exception:
            LOGGER.exception(
                "drive_turn: stash-pop finalizer failed (turn_id=%r)",
                effective_turn_id,
            )
        release_turn_actions(effective_turn_id)


# --- Stash-pop finalizer (ADR-0002 § Dirty-tree policy) --------------------


def _task_repo_path(conn: sqlite3.Connection, task_id: str) -> Path | None:
    """Look up the ``repo_path`` payload from the latest ``task.created`` event.

    Walks the event log directly (L2 read is allowed from the
    composition root per ``.importlinter``); returns ``None`` when the
    task_id is unknown or carries no ``repo_path``. The stash-pop helper
    treats ``None`` as "skip this run" — we never `git -C` into a
    nonexistent dir.
    """
    latest_repo: str | None = None
    for evt in iter_events(conn):
        if evt.type != "task.created":
            continue
        if evt.payload.get("task_id") != task_id:
            continue
        raw = evt.payload.get("repo_path")
        if isinstance(raw, str) and raw:
            latest_repo = raw
    return Path(latest_repo) if latest_repo is not None else None


def _pop_pending_stashes(  # noqa: C901 — composition walker folds the clean / conflict / git-error stash-pop outcomes per worker.reported row; splitting the guard ladder hurts readability.
    conn: sqlite3.Connection,
    *,
    artifacts_root: Path,
    turn_id: str,
) -> None:
    """Restore every pre-task stash recorded by this turn's terminal events.

    Walks the event log for terminal worker / action events —
    ``worker.reported``, ``action.failed``, ``action.timeout_assumed`` —
    whose ``correlation.turn_id`` matches ``turn_id``. L4's
    ``spawn_worker_handler`` stamps the ``stash_ref`` onto each of
    those payloads at emit time (success via the ``worker.reported``
    literal, failure paths via ``_spawn_worker_emit_terminal_failure``);
    the ``run_id`` rides on the payload (``worker.reported``) or on the
    correlation (failure events). For each stash_ref-carrying row we
    call :func:`jarvis.execution.diff_capture.restore_pretask_changes`
    with the repo cwd resolved from ``task.created.repo_path``. The
    runtime invokes this finalizer from ``drive_turn``'s ``finally`` —
    strictly after the last ``decide()`` call, so verify_diff always
    read exactly Codex's tree, and unconditionally, so an exception
    mid-turn cannot orphan the stash.

    This call is the SOLE legitimate site for
    ``restore_pretask_changes``; canary
    ``test_canary_stash_pop_after_verify`` enforces that L4 never pops
    the stash and that L3 / runtime own the order (verify_diff first,
    pop second).

    Args:
        conn: Open Event Log connection.
        artifacts_root: ``RuntimePaths.artifacts_root`` — conflict
            patches land at ``<artifacts_root>/run_<run_id>/conflict.patch``.
        turn_id: The composition root's turn correlation id. Only
            terminal events tagged with this turn are popped.

    Returns:
        None. A clean pop adds no events. A stash-pop CONFLICT is routed
        through :func:`jarvis.decision.result_interpreter.emit_stash_conflict_surfacing`
        so it becomes observable — ``worker.artifact_observed(kind=stash_conflict)``
        plus a ``Limitation`` Claim — rather than a silent ``conflict.patch``
        write nothing references (ADR-0002 J13). Non-conflict
        :class:`StashError` is logged and skipped so a single stuck stash
        doesn't mask the user-facing response.
    """
    # Terminal event types that may carry a pre-task stash_ref. A run that
    # appears under two types (e.g. worker.reported + action.timeout_assumed)
    # is popped once via the shared seen_run_ids dedup below.
    terminal_types = {"worker.reported", "action.failed", "action.timeout_assumed"}
    seen_run_ids: set[str] = set()
    for evt in iter_events(conn):
        if evt.type not in terminal_types:
            continue
        if evt.correlation is None or evt.correlation.get("turn_id") != turn_id:
            continue
        # worker.reported carries run_id in the payload; the failure
        # events carry it on the correlation only.
        run_id_raw = evt.payload.get("run_id")
        if not isinstance(run_id_raw, str):
            run_id_raw = evt.correlation.get("run_id")
        stash_ref_raw = evt.payload.get("stash_ref")
        task_id_raw = evt.correlation.get("task_id") if evt.correlation is not None else None
        if not isinstance(run_id_raw, str) or run_id_raw in seen_run_ids:
            continue
        seen_run_ids.add(run_id_raw)
        # stash_ref may be None on a clean tree at spawn-time — pass
        # through; restore_pretask_changes is a no-op for None.
        stash_ref: str | None = stash_ref_raw if isinstance(stash_ref_raw, str) else None
        if stash_ref is None:
            continue
        if not isinstance(task_id_raw, str):
            continue
        repo_path = _task_repo_path(conn, task_id_raw)
        if repo_path is None:
            continue
        try:
            conflict = restore_pretask_changes(
                repo_path,
                stash_ref,
                artifact_dir=artifacts_root,
                run_id=run_id_raw,
            )
        except StashError:
            # Don't propagate — a non-conflict error here means git
            # itself failed (e.g. stash ref vanished); log and continue
            # so the user-facing response is not held hostage.
            LOGGER.exception(
                "runtime: failed to pop stash %s for run %s",
                stash_ref,
                run_id_raw,
            )
            continue
        if conflict is None:
            continue
        # Stash-pop CONFLICT: restore_pretask_changes preserved the patch
        # and left the tree holding Codex's edits (a merge conflict is
        # reset to Codex's HEAD; an uncommitted-overwrite abort is left
        # as-is). Route it through L3 so the conflict surfaces as
        # worker.artifact_observed + a Limitation Claim
        # rather than a silent file write — ADR-0002 J13 / dirty-tree policy
        # ("never silently overwrite Allen's work"). The worker.reported row
        # carries the spawn_worker action that created the stash.
        action_id_raw = evt.payload.get("action_id")
        if not isinstance(action_id_raw, str):
            continue
        emit_stash_conflict_surfacing(
            conn,
            patch_path=conflict.patch_path,
            reason=conflict.reason,
            run_id=run_id_raw,
            action_id=action_id_raw,
            task_id=task_id_raw,
            source_event_id=evt.event_uid,
            correlation={
                "run_id": run_id_raw,
                "task_id": task_id_raw,
                "turn_id": turn_id,
            },
        )


__all__ = [
    "JarvisRuntime",
    "PreEmitTokenError",
    "RunTurnResult",
    "RuntimeBootstrapError",
    "TriggerWaitTimeout",
    "bootstrap_runtime_app",
    "drive_turn",
    # Re-exported for jarvis.cli: H13 forbids a single file importing two
    # middle-layer siblings, and the CLI already imports jarvis.deployment.
    # Same route PreEmitTokenError above already takes.
    "parse_response_channels",
    "run_turn",
]
