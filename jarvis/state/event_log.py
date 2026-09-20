"""L2 Event Log — SQLite append-only spine + EventTypeRegistry.

This module is the SINGLE place in `jarvis/` allowed to INSERT into the
`events` table (canary H1 in Step 11 will whitelist this file as the only
INSERT site). Every higher-layer event emission goes through `emit_event`.

Per spec.html §5.1 / §5.4 + ADR 0001 § Stub strategy L2 row +
§ Day-1 EventTypeRegistry extensions + § Canonical event trace.

Owns:

- The `events` SQLite table (schema, indexes, append-only triggers).
- `EventTypeRegistry` — Day-1 in-module authority for every event type
  reachable from the Mac-only flagship scenario. The registry IS the
  source of truth: any `emit_event(type=...)` for an unregistered type
  raises, and `schema_version` mismatches raise.
- `emit_event(...)` — typed append API that validates required-payload
  fields, schema version, and `source_event_id` FK-style references
  before INSERTing and committing.
- `iter_events` / `get_event` reading helpers (just enough for projections
  in Step 5 and scenario asserts in Step 12).

Append-only enforcement is **trigger-level**, not advisory: any UPDATE or
DELETE on `events` aborts with a SQLite OperationalError regardless of how
the connection is opened. Acceptance criterion A6 (Tier 2) attempts both
inside the test.

Layer boundary (`.importlinter` + canary H13 in Step 11): stdlib only
plus `jarvis.shared`. No imports from `jarvis.constitution`,
`jarvis.decision`, `jarvis.execution`, `jarvis.surface`,
`jarvis.deployment`, `jarvis.runtime`, `jarvis.cli`.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from jarvis.shared import Event
from jarvis.shared.realtime import new_log_epoch
from jarvis.state.committed_event_bus import CommittedEventBus

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from pathlib import Path

# --- Schema constants --------------------------------------------------------

# Single table — projections (Step 5) live in their own module, no caches here.
# The one exception is `event_log_metadata` below: the ADR-0014 D6 operational
# row that names this log's lineage. It is not an event and not projection
# truth — nothing is ever derived from it by replay.
_TABLE_NAME: Final[str] = "events"

# Schema v1 (spec §5.1 backfill, 2026-08-25). Deviations from the spec's
# literal DDL, each deliberate and documented:
#
# - `ts` (ISO 8601) and `correlation_id` are VIRTUAL generated columns
#   derived from `ts_epoch_ms` / `correlation_json` — always in sync by
#   construction, no writer can skew them, and rows inserted by an
#   old-code daemon during a rolling upgrade get correct values for
#   free. Cost: they cannot carry NOT NULL (spec-shaped in value, not
#   in constraint).
# - `correlation_id` is the scalar `turn_id` promoted out of the
#   `correlation_json` map (the map is the documented superset; spec
#   §5.1 wants one indexable scalar and turn is the runtime's primary
#   correlation axis).
# - `source_event_id` stays TEXT → `events.event_uid` (spec says
#   INTEGER → id; rationale near `_validate_source_event_id`).
# - `event_uid` stays uuid4 hex, NOT UUIDv7 — declared deviation: the
#   §3.7.10 sortable-ID clause exists for cross-domain ordering, which
#   is inactive under the Mac-only scope (single domain; ordering
#   authority is `id` / `ts_epoch_ms`). Old rows could never be
#   re-minted anyway (`source_event_id` references them). Revisit at
#   federation.
# Schema v2 (2026-09-04) adds `event_log_metadata` and its one `log_epoch`
# row. A v1-stamped file has no such table, so `open_runtime_event_log`
# refuses it with "requires bootstrap migration" until `open_event_log` has
# run — the intended fail-closed contract, and harmless in practice because
# the daemon always bootstraps before any runtime connection is opened.
_SCHEMA_USER_VERSION: Final[int] = 2

_TS_GENERATED_EXPR: Final[str] = (
    "strftime('%Y-%m-%dT%H:%M:%fZ', ts_epoch_ms / 1000.0, 'unixepoch')"
)
_CORRELATION_ID_GENERATED_EXPR: Final[str] = "json_extract(correlation_json, '$.turn_id')"

_CREATE_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    ts_epoch_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    source_event_id TEXT,
    correlation_json TEXT,
    actor TEXT NOT NULL DEFAULT 'unknown',
    ingestion_node TEXT NOT NULL DEFAULT 'mac',
    ts TEXT GENERATED ALWAYS AS ({_TS_GENERATED_EXPR}) VIRTUAL,
    correlation_id TEXT GENERATED ALWAYS AS ({_CORRELATION_ID_GENERATED_EXPR}) VIRTUAL
)
"""

# ADR-0014 D6 operational metadata: one row, one column of interest. Not an
# event, not a projection — the log's own lineage identity, which a client
# compares against the epoch it last saw to decide resume vs. resnapshot.
_CREATE_METADATA_TABLE_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS event_log_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    log_epoch TEXT NOT NULL
)
"""

# Indexes: (type) for projection scans, (source_event_id) for cause-chain
# walks (acceptance A4), (ts_epoch_ms) for time-range / monotonic checks (A5).
# Schema v1 adds the three spec §5.1 indexes: (type, ts_epoch_ms) and
# (actor, ts_epoch_ms) composites plus (correlation_id). `idx_events_ts`
# is an extra beyond spec §5.1's four — kept, it predates v1 and costs
# little. Wave 1's three expression indexes keep terminal arbitration from
# scanning lifecycle history while holding ``BEGIN IMMEDIATE``'s writer lock.
_CREATE_INDEXES_SQL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)",
    "CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_epoch_ms)",
    "CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts_epoch_ms)",
    "CREATE INDEX IF NOT EXISTS idx_events_actor_ts ON events(actor, ts_epoch_ms)",
    "CREATE INDEX IF NOT EXISTS idx_events_correlation ON events(correlation_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_response_terminal "
    "ON events(type, json_extract(payload_json, '$.response_id'))",
    "CREATE INDEX IF NOT EXISTS idx_events_action_terminal "
    "ON events(type, json_extract(payload_json, '$.action_id'))",
    # New name is a deliberate migration: SQLite would retain Wave 1's
    # original three-column definition under CREATE INDEX IF NOT EXISTS.
    "CREATE INDEX IF NOT EXISTS idx_events_playback_terminal_v2 "
    "ON events(type, json_extract(payload_json, '$.response_id'), "
    "json_extract(payload_json, '$.playback_generation_id'))",
)

# Append-only enforcement (acceptance A6). RAISE(ABORT, ...) raises
# sqlite3.IntegrityError up to the Python caller; the literal message is
# stable so callers can pattern-match in tests if needed.
_CREATE_TRIGGER_NO_UPDATE_SQL: Final[str] = """
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: UPDATE not allowed');
END
"""

_CREATE_TRIGGER_NO_DELETE_SQL: Final[str] = """
CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: DELETE not allowed');
END
"""


# --- Exceptions --------------------------------------------------------------


class EventLogError(Exception):
    """Base class for all `emit_event` validation failures."""


class UnregisteredEventTypeError(EventLogError):
    """Raised when `type` is not present in `EventTypeRegistry`."""


class SchemaVersionMismatchError(EventLogError):
    """Raised when the caller-provided `schema_version` mismatches registry."""


class MissingPayloadFieldError(EventLogError):
    """Raised when a required-payload field for `type` is absent."""


class DanglingSourceEventError(EventLogError):
    """Raised when `source_event_id` does not exist in `events.event_uid`."""


class InvalidCorrelationError(EventLogError):
    """Raised when a correlation key or stable identity is not a string."""


class TransactionRequiredError(EventLogError):
    """Raised when the no-commit append lacks a caller-owned transaction."""


class NestedEventTransactionError(EventLogError):
    """Raised when ``emit_event`` would commit a caller-owned transaction."""


# --- EventTypeRegistry -------------------------------------------------------


@dataclass(frozen=True)
class EventTypeSchema:
    """One entry in the EventTypeRegistry (spec §5.4.1, Day-1 minimum).

    Day-1 keeps only the fields `emit_event` actually consumes
    (`required_payload`, `optional_payload`, `schema_version`, `actor`)
    plus the `owner_layer` label used by canary H13. Producer / projection
    consumers / cross-domain policy / evidence semantics / artifact policy
    fields from the full spec §5.4.1 shape are deferred until a consumer
    exists.

    `actor` is the spec §5.1 provenance column value stamped by
    `emit_event` (schema-v1 migration, 2026-08-25) — who *caused* the
    event, distinct from L4's caller_principal. The §5.1 enumeration is
    open ("user" | "jarvis_llm" | "observer" | device ids | ...); this
    registry uses five values:

    - ``user`` — a human input crossing the surface.
    - ``jarvis_llm`` — the event records an LLM decision output.
    - ``jarvis_runtime`` — deterministic runtime machinery (gates,
      dispatch, interpreters, surface delivery bookkeeping).
    - ``codex_worker`` — a worker's self-report about itself.
    - ``observer`` — environment observation (repo watcher, power
      notifications).
    """

    event_type: str
    owner_layer: str
    actor: str
    required_payload: tuple[str, ...]
    optional_payload: tuple[str, ...]
    schema_version: int


# Day-1 in-module registry. Order mirrors the ADR § Canonical event trace
# (evt 00 .. evt 24) for ease of cross-reference.
#
# `gate.evaluated` schema is copied verbatim from ADR § Day-1
# EventTypeRegistry extensions; the rest follow the canonical-trace
# payload shape and the spec §5.4 owner-layer rules.
_REGISTRY_ENTRIES: Final[tuple[EventTypeSchema, ...]] = (
    EventTypeSchema(
        event_type="turn.started",
        owner_layer="L3",
        actor="jarvis_runtime",
        required_payload=("turn_id",),
        optional_payload=("trigger",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="turn.ended",
        owner_layer="L3",
        actor="jarvis_runtime",
        required_payload=("turn_id",),
        optional_payload=("final_response_hash", "consumed_trigger_event_uid"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="utterance.received",
        owner_layer="L5",
        actor="user",
        required_payload=("transcript", "turn_id"),
        optional_payload=(
            "channel",
            "language",
            "confidence",
            "language_detected",
            "emotion",
            "audio_artifact_ref",
        ),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.proposed",
        owner_layer="L3",
        actor="jarvis_llm",
        required_payload=("action_id", "tool_name", "caller_principal", "risk_level"),
        optional_payload=("target_entity_ref", "run_id", "turn_id", "arguments"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="gate.evaluated",
        owner_layer="L3",
        actor="jarvis_runtime",
        required_payload=("gate", "outcome", "reasons"),
        # `attempt` carries the Pre-emit Gate retry index (0=initial,
        # 1=LLM retry, 2=forced template) so the audit trail captures
        # every verdict on the way to the final ResponsePlan, not just
        # the last one. Pre-action gate events omit it.
        # `lease_id` (ADR-0012 D2.4): stamped when a lease-bearing
        # ActionRequest passes the Pre-action Gate. Step 6's single-use
        # fold reads lease consumption from this key, so it must be
        # declared here rather than smuggled through as an unregistered
        # extra payload field.
        optional_payload=(
            "action_id",
            "response_hash",
            "claim_levels",
            "check_results",
            "attempt",
            "lease_id",
            "authorization_id",
            "dispatch_id",
            "response_id",
            "sequence",
            "segment_hash",
            "candidate_risk",
            "policy_hash",
            "evidence_snapshot_hash",
            "risk_context_hash",
            "classifier_rule_version",
            "classification_elapsed_ms",
            "attention_channel",
            "required_gate_mode",
            "phase",
            "channel",
        ),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.authorized",
        owner_layer="L3",
        actor="jarvis_runtime",
        required_payload=("action_id",),
        optional_payload=(),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.dispatched",
        owner_layer="L4",
        actor="jarvis_runtime",
        required_payload=("action_id",),
        # ADR-0009 D4: `result_expected_by_ms` is the dispatcher-stamped
        # deadline (per-tool budget + grace) the supervisor sweep reads to
        # decide whether an open action is overdue (spec §3.4.8). Optional
        # so pre-ADR-0009 rows stay valid; the sweep falls back to the
        # dispatched ts + `supervisor.default_budget_s` for those.
        optional_payload=("result_expected_by_ms",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.running",
        owner_layer="L4",
        actor="jarvis_runtime",
        required_payload=("action_id",),
        # ADR-0008 D9: `action.running` is emitted only after the runner
        # holds the action's resource lease, so it is the one durable
        # record of what a crashed process was holding. A later boot reads
        # these two to re-establish quarantine for a write-exclusive action
        # that reached a canonical terminal without a cleanup event.
        # Optional so the legacy dispatcher's rows stay valid.
        optional_payload=("resource_keys", "resource_mode"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.result_observed",
        owner_layer="L4",
        actor="jarvis_runtime",
        required_payload=("action_id", "semantics"),
        # `error_payload` carries an L4 error-context dict.
        optional_payload=("tool_output", "error", "run_id", "error_payload"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.failed",
        owner_layer="L4",
        actor="jarvis_runtime",
        required_payload=("action_id",),
        optional_payload=("error", "reason"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.timeout_assumed",
        owner_layer="L4",
        actor="jarvis_runtime",
        required_payload=("action_id",),
        optional_payload=("error", "reason"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.cancelled",
        owner_layer="L4",
        actor="jarvis_runtime",
        required_payload=("action_id",),
        # ADR-0008 §4.2 additive fields on the existing type.
        optional_payload=("error", "reason", "requested_by_turn_id", "cancel_scope"),
        schema_version=1,
    ),
    EventTypeSchema(
        # owner_layer is L3 per spec §5.4.1 (single-value owner_layer).
        # L4 returns cost data inside RawResult.metadata["cost"] (codex
        # turn tokens come from Codex's turn/completed payload, attached
        # to spawn_worker's RawResult); L3 reads that and emits
        # cost.recorded. L4 never emits this event directly — see spec
        # §5.4.2 pattern where L4 returns RawResult and L3 emits
        # action.result_observed.
        event_type="cost.recorded",
        owner_layer="L3",
        actor="jarvis_runtime",
        required_payload=("kind", "model"),
        optional_payload=(
            "tokens_in",
            "tokens_out",
            "cache_read_in",
            "cache_write_in",
            "cost_usd",
            "run_id",
            "llm_request_id",
            "provider",
            "provider_response_id",
            "usage_status",
            "disposition",
            "error_code",
        ),
        schema_version=1,
    ),
    # ADR-0008 §4.2 / D8 (Step 4) — the adoption watermark.  One row per
    # named consumer, appended the first time it starts reading, recording
    # `MAX(events.id)` at that instant.  Recovery considers only rows after
    # it, which is what stops a freshly enabled realtime consumer from
    # replaying every historical utterance in the log (F24).
    EventTypeSchema(
        event_type="consumer.adopted",
        owner_layer="L2",
        actor="jarvis_runtime",
        required_payload=("name", "adoption_row_id"),
        optional_payload=(),
        schema_version=1,
    ),
    # ADR-0008 Wave 1 — L3 response lifecycle.  These registrations are
    # inert until the corresponding feature flag routes callers through the
    # shared lifecycle terminal owner.
    EventTypeSchema(
        event_type="response.request_admitted",
        owner_layer="L3",
        actor="jarvis_runtime",
        required_payload=("response_id", "admission_id", "kind"),
        optional_payload=(),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="response.started",
        owner_layer="L3",
        actor="jarvis_runtime",
        required_payload=(
            "response_id",
            "response_group_id",
            "turn_id",
            "phase",
            "channel",
            "emission_mode",
            "output_risk_class",
            "required_gate_mode",
            "policy_hash",
            "active_subject_ref",
            "evidence_snapshot_hash",
        ),
        optional_payload=(
            "provider",
            "model",
            "risk_context_hash",
            "classifier_rule_version",
            "corrects_response_id",
            "route",
        ),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="response.completed",
        owner_layer="L3",
        actor="jarvis_runtime",
        required_payload=("response_id", "response_group_id", "turn_id", "response_hash"),
        optional_payload=("provider_response_id", "generated_text_hash", "segment_count"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="response.cancelled",
        owner_layer="L3",
        actor="jarvis_runtime",
        required_payload=("response_id", "response_group_id", "turn_id", "reason"),
        optional_payload=(
            "interrupted_by_utterance_id",
            "interrupted_by_turn_id",
            "generated_text_hash",
            "committed_prefix_hash",
            "cancel_scope",
        ),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="response.failed",
        owner_layer="L3",
        actor="jarvis_runtime",
        required_payload=("response_id", "response_group_id", "turn_id", "reason"),
        optional_payload=("provider_response_id", "retryable", "committed_prefix_hash"),
        schema_version=1,
    ),
    # ADR-0006 Wave 1 registered the playback terminal truth. Wave 2 starts
    # emitting these milestones only behind realtime.streaming_output; the
    # same response/generation CAS remains their sole terminal arbiter.
    EventTypeSchema(
        event_type="surface.playback_started",
        owner_layer="L5",
        actor="jarvis_runtime",
        required_payload=(
            "session_id", "response_id", "turn_id", "playback_generation_id",
            "phase", "channel", "speech_text_hash",
        ),
        # ``incremental`` marks a lease minted from the first permitted
        # segment: ``speech_text_hash`` is then the first segment's hash and
        # the full speech hash is bound by ``surface.playback_completed``.
        optional_payload=("incremental", "estimated_output_latency_ns"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="surface.playback_segment_prepared",
        owner_layer="L5",
        actor="jarvis_runtime",
        required_payload=(
            "session_id", "response_id", "turn_id", "playback_generation_id",
            "sequence", "speech_text", "speech_text_hash", "segment_hash",
            "source_chunk_event_uid",
        ),
        optional_payload=(),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="surface.playback_checkpoint",
        owner_layer="L5",
        actor="jarvis_runtime",
        required_payload=(
            "session_id",
            "response_id",
            "turn_id",
            "playback_generation_id",
            "heard_through_sequence",
            "submitted_samples",
            "heard_text_hash",
        ),
        optional_payload=("cursor_quality", "heard_text"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="surface.playback_completed",
        owner_layer="L5",
        actor="jarvis_runtime",
        required_payload=(
            "session_id",
            "response_id",
            "turn_id",
            "playback_generation_id",
            "heard_through_sequence",
            "submitted_samples",
            "speech_text_hash",
        ),
        optional_payload=(
            "total_samples", "provider", "cursor_quality", "heard_text", "heard_text_hash",
            "starvation_gaps", "host_underflows", "tail_ramp_samples",
        ),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="surface.playback_interrupted",
        owner_layer="L5",
        actor="jarvis_runtime",
        required_payload=(
            "session_id",
            "response_id",
            "turn_id",
            "playback_generation_id",
            "heard_through_sequence",
            "submitted_samples",
            "heard_text_hash",
            "reason",
        ),
        optional_payload=(
            "heard_text",
            "total_samples",
            "interrupted_by_utterance_id",
            "interrupted_by_turn_id",
            "provider",
            "cursor_quality",
            "starvation_gaps",
            "host_underflows",
            "tail_ramp_samples",
        ),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="surface.playback_failed",
        owner_layer="L5",
        actor="jarvis_runtime",
        required_payload=(
            "session_id",
            "response_id",
            "turn_id",
            "playback_generation_id",
            "heard_through_sequence",
            "submitted_samples",
            "heard_text_hash",
            "reason",
        ),
        optional_payload=(
            "heard_text", "provider", "cursor_quality", "retryable",
            "starvation_gaps", "host_underflows", "tail_ramp_samples",
        ),
        schema_version=1,
    ),
    # ADR-0006 §4.2: the fourth exit from playback, which appends no terminal.
    # The media lane fails closed and speaks nothing further until the process
    # restarts; without this row that outcome is unprovable after the fact.
    # `reconcile_open_playback` joins this row on
    # `(response_id, playback_generation_id)` to close the orphan it left as
    # `media_lane_isolated` rather than `daemon_restart`; the specific
    # `isolation_reason` is readable only here.
    # `terminal_type`/`error_type` are null at the fallback-cleanup site, where
    # no terminal was being attempted and no exception reached the isolator.
    EventTypeSchema(
        event_type="surface.playback_lane_isolated",
        owner_layer="L5",
        actor="jarvis_runtime",
        required_payload=(
            "session_id",
            "response_id",
            "turn_id",
            "playback_generation_id",
            "terminal_type",
            "error_type",
            "isolation_reason",
        ),
        optional_payload=(),
        schema_version=1,
    ),
    # F6: surface.user_intent — spec.html §5.4 line 1442 canonical;
    # missing from Day-1 registry, restored here.
    EventTypeSchema(
        event_type="surface.user_intent",
        owner_layer="L5",
        actor="user",
        required_payload=("transcript", "turn_id"),
        # ADR-0016 D3: ``record_id`` names the memory.db row the submitting
        # surface already wrote, so drive_turn skips its own write.
        optional_payload=("channel", "language", "record_id"),
        schema_version=1,
    ),
    # F6: surface.response_emitted — NOT in spec §5.4 canonical list
    # (deviation V1); Day-2 audit field for delivered_via +
    # attention_channel.
    EventTypeSchema(
        event_type="surface.response_emitted",
        owner_layer="L5",
        actor="jarvis_runtime",
        required_payload=("turn_id", "text"),
        optional_payload=(
            "delivered_via",
            "attention_channel",
            "voice_text",
            "document_text",
            "response_hash",
            "response_id",
            "response_group_id",
            "phase",
            "channel",
        ),
        schema_version=1,
    ),
    # ADR-0003 Step 2 — chunked Inherent text response (D10).
    # Emit order per turn: surface.response_open → surface.response_chunk*
    # (1..N) → surface.response_emitted. The single response watcher
    # in runtime/inherent_loop.py polls all three types in one cursor
    # (WHERE type IN (...) ORDER BY id) and dispatches by event.type.
    EventTypeSchema(
        event_type="surface.response_open",
        owner_layer="L5",
        actor="jarvis_runtime",
        required_payload=("turn_id", "query", "kind"),
        # ADR-0005 §7: L5 TTS consumers read ``required_gate_mode`` from the
        # open header to route between sentence-streaming and full-text TTS
        # playback per spec §3.6.6. Optional so legacy emitters (and the
        # event-log unit tests that emit directly via emit_event) keep working.
        # ADR-0009 D4: ``attention_channel`` rides the same header so
        # `_tts_watcher` / the WS broadcaster can drop `queue_review` /
        # `silent_log` turns before they speak (spec §3.2.5 安静优先).
        optional_payload=(
            "required_gate_mode",
            "attention_channel",
            "response_id",
            "response_group_id",
            "phase",
            "channel",
        ),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="surface.response_chunk",
        owner_layer="L5",
        actor="jarvis_runtime",
        required_payload=("turn_id", "text"),
        optional_payload=(
            "response_id",
            "response_group_id",
            "sequence",
            "phase",
            "channel",
            "segment_hash",
        ),
        schema_version=1,
    ),
    # Sleep/wake events per spec §3.7.8 — L6 owned (Deployment Domain).
    EventTypeSchema(
        event_type="mac.sleeping",
        owner_layer="L6",
        actor="observer",
        required_payload=("ts_epoch_ms",),
        optional_payload=("reason", "in_progress_action_ids"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="mac.awake",
        owner_layer="L6",
        actor="observer",
        required_payload=("ts_epoch_ms", "slept_for_ms"),
        optional_payload=("reconciliation_summary",),
        schema_version=1,
    ),
    # --- ADR-0003 Inherent Text Surface extensions ---
    EventTypeSchema(
        # Watcher-level catch-all when drive_turn raises uncaught
        # (registered Day-2 per ADR-0003 D7; emit-site lands in Step 8
        # inside runtime/inherent_loop.py's _user_intent_watcher).
        # Records the meta-failure as a durable fact per spec §3.3.1.
        # No Day-2 projection consumer; the future task / claim ladder
        # may fold this into Limitation Claims (spec §3.4.11).
        # owner_layer is L5 because Inherent surface output is L5 and
        # turn.failed is the surface-layer's meta-record of a failed
        # turn; runtime/inherent_loop is the composition root, not a
        # numbered spec layer.
        event_type="turn.failed",
        owner_layer="L5",
        actor="jarvis_runtime",
        required_payload=("turn_id", "exception_repr"),
        optional_payload=("trigger_event_id",),
        schema_version=1,
    ),
    # --- ADR-0009 residency & perception extensions (§4 registry table) ---
    #
    # Both are `evidence_semantics=observation` per spec §5.4 — the
    # registry dataclass does not yet carry that field (Day-1 kept only
    # what emit_event consumes), so the semantics live here as the
    # declaration of record until a consumer exists.
    # owner_layer is L5: the repo observer is a Mac input adapter on the
    # INPUTS/Surface row of spec §2.1.
    # Provenance (ADR-0009 V6): the L2 schema has no actor column, so
    # `actor` is a REQUIRED payload field on both types — the §5.1
    # principal enumeration realized as a payload convention, exactly as
    # `payload.by` already is for `task.verified`.
    # Neither type joins any trigger tuple: observations fold silently
    # (spec §3.4.1 / §3.2.5 安静优先).
    # --- ADR-0018 usage observer (same observation conventions as ADR-0009) ---
    EventTypeSchema(
        # One row per *changed* service snapshot (claude / codex / openai /
        # deepseek / minimax). `data` is the bounded service-specific
        # snapshot; `status` is ok | error | unconfigured so a stale row
        # explains itself. Never a decision trigger.
        event_type="usage.state_observed",
        owner_layer="L5",
        actor="observer",
        required_payload=("service", "status", "data", "observed_at_ms", "actor"),
        optional_payload=("error",),
        schema_version=1,
    ),
    EventTypeSchema(
        # One row per finished TTS segment carrying the provider's own
        # character count; the MiniMax balance estimate folds these.
        event_type="tts.usage_observed",
        owner_layer="L5",
        actor="observer",
        required_payload=("provider", "characters", "response_id", "sequence", "actor"),
        optional_payload=("model",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="repo.state_observed",
        owner_layer="L5",
        actor="observer",
        # Emitted only when a field changes (§3.6.1 emit-on-change ladder).
        # `last_commit_subject` is capped at 200 chars by the producer per
        # spec §3.3.9 bounded payloads; `branch` is the literal "HEAD" on a
        # detached checkout so the contract stays total.
        required_payload=(
            "repo_path",
            "branch",
            "head_sha",
            "dirty_file_count",
            "last_commit_subject",
            "observed_at_ms",
            "actor",
        ),
        optional_payload=(),
        schema_version=1,
    ),
    EventTypeSchema(
        # The spec-canonical name (§6 Drift Watch fold sources). One event
        # per newly-seen first-parent commit; `truncated` + `skipped_count`
        # ride the final event of a burst-capped poll so ADR-0010 can tell
        # a gapped window from a contiguous one.
        event_type="project.commit_seen",
        owner_layer="L5",
        actor="observer",
        required_payload=(
            "repo_path",
            "commit_sha",
            "subject",
            "committed_at_ms",
            "actor",
        ),
        optional_payload=("truncated", "skipped_count"),
        schema_version=1,
    ),
    # --- ADR-0012 Confirmation Flow extensions (§3 D3) ---
    #
    # owner_layer=L3 for the two confirmation.* below per D5/D6: both
    # the ask path (`confirm_required` handling) and the answer path
    # (`_handle_utterance`'s pre-tier_0 grammar hook) live in
    # `jarvis/decision/__init__.py`.
    EventTypeSchema(
        # `action_snapshot` is a frozen dict; the registry only
        # validates top-level payload keys, so its inner shape is
        # documented here for Step 5 (the ask path, which freezes it)
        # and Step 6 (the answer path, which re-reads it to rebuild the
        # ActionRequest). Per ADR-0012 §3 D3 the snapshot carries six
        # keys: tool_name (the proposed tool's name); caller (the
        # caller_principal value); canonical_target (the resolved
        # path/target); target_entity_ref (the resolved entity ref, or
        # null); risk_level (one of L0 through L4); and args_meta,
        # itself a dict of the tool's non-content arguments plus three
        # always-present keys — content_sha256, content_bytes, and
        # content_artifact (the staged content's path).
        # The `content` argument itself never rides the event payload
        # (§3.3.9 bounded payloads) — it is staged to
        # `artifacts_root/pending_writes/<confirmation_id>` at request
        # time; args_meta's content_artifact is that path.
        event_type="confirmation.requested",
        owner_layer="L3",
        actor="jarvis_runtime",
        required_payload=(
            "confirmation_id",
            "action_snapshot",
            "template_line",
            "expires_at_ms",
        ),
        optional_payload=(),
        schema_version=1,
    ),
    EventTypeSchema(
        # `source_event_id` (an `emit_event` column, not a payload key)
        # points at the `confirmation.requested` event this answers —
        # D2.4's single-use fold and the audit chain both read it from
        # `events.source_event_id`, not from `payload`.
        event_type="confirmation.accepted",
        owner_layer="L3",
        actor="user",
        required_payload=("confirmation_id", "utterance_raw", "grammar_rule_id"),
        optional_payload=(),
        schema_version=1,
    ),
    EventTypeSchema(
        # Same shape as `confirmation.accepted`; `source_event_id`
        # again points at the `confirmation.requested` event (a column,
        # not a payload key — see that entry's comment above).
        event_type="confirmation.rejected",
        owner_layer="L3",
        actor="user",
        required_payload=("confirmation_id", "utterance_raw", "grammar_rule_id"),
        optional_payload=(),
        schema_version=1,
    ),
    EventTypeSchema(
        # ADR-0014 D14 — the third confirmation terminal, and the only one
        # no human utters: the runtime expiry sweep and its boot reconciler
        # append it once a live ask passes `expires_at_ms`, so an idle panel
        # is cleared by a committed row instead of by its own clock.
        # `actor` is `jarvis_runtime` (deterministic runtime machinery), not
        # `user` like its two siblings, because no one answered.
        # `source_event_id` again points at the exact
        # `confirmation.requested` event — an `emit_event` column, not a
        # payload key (see the `confirmation.accepted` entry's comment).
        event_type="confirmation.expired",
        owner_layer="L3",
        actor="jarvis_runtime",
        required_payload=("confirmation_id", "expired_at_ms"),
        optional_payload=(),
        schema_version=1,
    ),
    # `surface.dismissed` / `surface.clarified` — spec §3.6.3-named
    # UserResponse durable forms; ADR-0012 §3 D3 registers them as
    # placeholders with NO emitter yet, same idiom as `claim.accepted`
    # above (occupy the schema now; the emitter lands as its own
    # change). owner_layer/actor deliberately align with the existing
    # `surface.*` family rather than inventing new values: L5
    # (unanimous across every surface.* entry above) and `user`
    # (matching `surface.user_intent` specifically — a dismiss or
    # clarify is a human action crossing the surface, not
    # runtime-authored output like `surface.response_*`).
    # `required_payload=("turn_id",)` is the smallest defensible
    # shape: every per-turn surface.* entry above keys on `turn_id`,
    # and inventing a content shape ahead of a real emitter would
    # fight whatever consumes it later — same minimalism as
    # `claim.accepted`'s single identifying field.
    EventTypeSchema(
        event_type="surface.dismissed",
        owner_layer="L5",
        actor="user",
        required_payload=("turn_id",),
        optional_payload=(),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="surface.clarified",
        owner_layer="L5",
        actor="user",
        required_payload=("turn_id",),
        optional_payload=(),
        schema_version=1,
    ),
    # Memo inbox: one durable row per `/note ...` capture. The list view is
    # a fold over this type (`list_memos`); there is no separate projection.
    EventTypeSchema(
        event_type="memo.captured",
        owner_layer="L4",
        actor="user",
        required_payload=("text", "action_id"),
        optional_payload=(),
        schema_version=1,
    ),
)


_REGISTRY_MAP: Final[MappingProxyType[str, EventTypeSchema]] = MappingProxyType(
    {entry.event_type: entry for entry in _REGISTRY_ENTRIES}
)


class EventTypeRegistry:
    """Day-1 frozen authority over event-type schemas (spec §5.4).

    Backed by a `MappingProxyType[str, EventTypeSchema]` so the registry
    cannot be mutated at runtime. Day-1 ships in-module; a file-backed
    `registry.json` at `${JARVIS_RUNTIME_ROOT}/registry.json` (placed by L6)
    is OPTIONAL Day-1 and deferred — tests stay self-contained.
    """

    @staticmethod
    def get(event_type: str) -> EventTypeSchema | None:
        """Return the schema entry for `event_type`, or None if unregistered."""
        return _REGISTRY_MAP.get(event_type)

    @staticmethod
    def requires(event_type: str) -> tuple[str, ...]:
        """Return the required-payload tuple. KeyError if unregistered."""
        return _REGISTRY_MAP[event_type].required_payload

    @staticmethod
    def optional(event_type: str) -> tuple[str, ...]:
        """Return the optional-payload tuple. KeyError if unregistered."""
        return _REGISTRY_MAP[event_type].optional_payload

    @staticmethod
    def iter_types() -> Iterable[str]:
        """Yield every registered event type, in insertion order."""
        return _REGISTRY_MAP.keys()


# --- Connection lifecycle ----------------------------------------------------


def _migrate_schema_v0_to_v1(conn: sqlite3.Connection) -> None:
    """In-place migrate a pre-2026-08-25 `events` table to schema v1.

    v0 shape: eight columns, no `actor` / `ingestion_node` / generated
    `ts` / `correlation_id`. Detection is by column presence, not
    `PRAGMA user_version` — v0 predates any version stamp.

    The whole migration runs inside ONE EXCLUSIVE transaction:

    - `DROP TRIGGER events_no_update` opens the only window in the log's
      life where an UPDATE could slip past append-only enforcement; the
      exclusive lock guarantees no concurrent writer exists inside it
      (old-code daemons block on their 5 s busy_timeout and then fail
      their single emit — they retry-safe on the next event).
    - `ALTER TABLE ADD COLUMN` with a non-NULL DEFAULT gives every
      existing row the default without a rewrite (`ingestion_node` needs
      no backfill at all — every pre-v1 event was ingested on the Mac).
    - The `actor` backfill honors the ADR-0009 payload convention first
      (`payload.actor`, set by the repo observer), then stamps the
      registry's per-type actor; rows of types absent from the registry
      keep 'unknown'.
    - The generated columns need no backfill by construction.

    Crash-safety: any failure rolls the transaction back — the table is
    either fully v0 (with its trigger intact) or fully v1; `PRAGMA
    user_version` is stamped by `open_event_log` only after this returns.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'events'",
    ).fetchone()
    if row is None:
        return  # Fresh database — CREATE TABLE installs v1 directly.
    columns = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    if "ingestion_node" in columns:
        return  # Already v1.

    conn.execute("BEGIN EXCLUSIVE")
    try:
        conn.execute("DROP TRIGGER IF EXISTS events_no_update")
        conn.execute("ALTER TABLE events ADD COLUMN actor TEXT NOT NULL DEFAULT 'unknown'")
        conn.execute(
            "ALTER TABLE events ADD COLUMN ingestion_node TEXT NOT NULL DEFAULT 'mac'",
        )
        conn.execute(
            f"ALTER TABLE events ADD COLUMN ts TEXT "
            f"GENERATED ALWAYS AS ({_TS_GENERATED_EXPR}) VIRTUAL",
        )
        conn.execute(
            f"ALTER TABLE events ADD COLUMN correlation_id TEXT "
            f"GENERATED ALWAYS AS ({_CORRELATION_ID_GENERATED_EXPR}) VIRTUAL",
        )
        # H1-canary note: these are the ONLY UPDATE statements allowed to
        # exist in `jarvis/` — one-shot schema-migration backfill inside
        # the exclusive transaction, tagged for the canary's allowlist.
        conn.execute(
            "/* L2 schema migration v1 */ UPDATE events SET "
            "actor = json_extract(payload_json, '$.actor') "
            "WHERE json_extract(payload_json, '$.actor') IS NOT NULL",
        )
        for schema in _REGISTRY_ENTRIES:
            conn.execute(
                "/* L2 schema migration v1 */ UPDATE events SET actor = ? "
                "WHERE type = ? AND actor = 'unknown'",
                (schema.actor, schema.event_type),
            )
        conn.execute(_CREATE_TRIGGER_NO_UPDATE_SQL)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _assign_log_epoch_once(conn: sqlite3.Connection) -> None:
    """Give this log file its one and only `log_epoch` (ADR-0014 D6).

    Runs on every `open_event_log`, but `INSERT OR IGNORE` under
    `BEGIN IMMEDIATE` means only the first open of a given file ever
    writes a row: the primary key is the constant 1. That is what keeps
    the epoch identical across restart, `VACUUM`, and a byte copy of the
    file, and different for a log that was recreated from scratch.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO event_log_metadata (singleton, log_epoch) VALUES (1, ?)",
            (new_log_epoch(),),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def read_log_epoch(conn: sqlite3.Connection) -> str:
    """Return this log's `log_epoch`, failing closed when it is absent.

    A missing row means the connection was opened against a file that
    `open_event_log` never bootstrapped; serving a realtime hello without
    an epoch would let a client resume against an unnamed log.
    """
    row = conn.execute("SELECT log_epoch FROM event_log_metadata WHERE singleton = 1").fetchone()
    if row is None:
        message = "Event Log has no log_epoch row"
        raise sqlite3.OperationalError(message)
    epoch: str = row[0]
    return epoch


def open_event_log(path: Path) -> sqlite3.Connection:
    """Open (or create) the SQLite Event Log at `path`.

    Schema, indexes, and append-only triggers are installed via
    `CREATE ... IF NOT EXISTS` so repeated calls on the same path are
    idempotent — schema is not recreated, triggers are not duplicated.

    Connection behavior:
        - Default `isolation_level`, but `emit_event` opens its write with
          an explicit `BEGIN IMMEDIATE`: its `source_event_id` validation
          SELECT would otherwise take a read snapshot first and turn the
          INSERT into a read->write upgrade, which SQLite refuses with an
          immediate `SQLITE_BUSY` without ever calling the busy handler.
          Reserving the write lock up front keeps the `busy_timeout` below
          in force for every contended write.
        - `PRAGMA journal_mode = WAL` enables reader/writer concurrency.
          The mode persists in the db file once set; reapplying on every
          open is a no-op. Required because the daemon touches the event
          log from multiple threads (file-watcher, worker, ephemeral
          submit_report connection, idle-sweep Timer).
        - `PRAGMA busy_timeout = 5000` makes contending writers retry for
          up to 5 s instead of raising
          `OperationalError("database is locked")` immediately.
          Per-connection; must be reissued each open.
        - `PRAGMA synchronous = NORMAL` is the standard WAL pairing —
          fsync at checkpoint instead of every commit. A crash within the
          last second of writes may lose those commits; the db file
          itself stays consistent. Acceptable for an append-only event
          log.
        - `PRAGMA foreign_keys = ON` is intentionally NOT set — the
          `source_event_id` FK is application-validated by `emit_event`
          (the column is `TEXT` and references `events.event_uid`, a
          UNIQUE column rather than a primary key). The triggers above
          guard append-only at SQL level regardless.

    Commit behavior:
        - `emit_event` commits per successful INSERT (one event = one
          transaction). The caller does not need to call `conn.commit()`.

    Args:
        path: SQLite db file path. Parent directory must already exist
            (L6 `bootstrap_runtime` provisions `${root}`; this module does
            not own L6 path placement).

    Returns:
        Open `sqlite3.Connection`. The caller closes it (recommended via
        `contextlib.closing`).
    """
    conn = sqlite3.connect(path)
    # Concurrency / durability PRAGMAs. Order matters: journal_mode
    # first (persisted in db file), then per-connection busy_timeout
    # and synchronous. fetchall() drains the result row from
    # journal_mode so the cursor doesn't leak open.
    conn.execute("PRAGMA journal_mode = WAL").fetchall()
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    _migrate_schema_v0_to_v1(conn)
    conn.execute(_CREATE_TABLE_SQL)
    for index_sql in _CREATE_INDEXES_SQL:
        conn.execute(index_sql)
    conn.execute(_CREATE_TRIGGER_NO_UPDATE_SQL)
    conn.execute(_CREATE_TRIGGER_NO_DELETE_SQL)
    conn.execute(_CREATE_METADATA_TABLE_SQL)
    _assign_log_epoch_once(conn)
    # Stamped after table + indexes + triggers exist; idempotent.
    conn.execute(f"PRAGMA user_version = {_SCHEMA_USER_VERSION}")
    conn.commit()
    return conn


def open_runtime_event_log(
    path: Path,
    *,
    deadline: float | None = None,
) -> sqlite3.Connection:
    """Connect to an already bootstrapped log without migrations or writes.

    A monotonic deadline covers connection setup and the subsequent write
    lock wait. Hot paths must not rerun boot migrations with a fresh timeout.
    Missing or incompatible logs fail closed; only ``open_event_log`` may
    create or migrate them.
    """
    remaining = 5.0 if deadline is None else max(0.0, deadline - time.monotonic())
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=remaining)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_USER_VERSION:
            message = "runtime Event Log requires bootstrap migration"
            raise sqlite3.OperationalError(message)  # noqa: TRY301 - atomic transaction owns rollback
        conn.execute("PRAGMA synchronous = NORMAL")
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                message = "runtime Event Log connection deadline exceeded"
                raise sqlite3.OperationalError(message)  # noqa: TRY301 - atomic transaction owns rollback
            conn.execute(f"PRAGMA busy_timeout = {int(remaining * 1000)}")
    except BaseException:
        conn.close()
        raise
    return conn


# --- emit_event --------------------------------------------------------------


def _now_epoch_ms() -> int:
    """Return current wall-clock time as integer epoch milliseconds."""
    return int(time.time() * 1000)


def _validate_source_event_id(conn: sqlite3.Connection, source_event_id: str) -> None:
    """Raise DanglingSourceEventError if no row has this `event_uid`."""
    cursor = conn.execute(
        "SELECT 1 FROM events WHERE event_uid = ? LIMIT 1",
        (source_event_id,),
    )
    if cursor.fetchone() is None:
        msg = f"source_event_id={source_event_id!r} does not exist in events.event_uid"
        raise DanglingSourceEventError(msg)


def append_event_in_transaction(  # noqa: PLR0913 — one keyword per Event column; spec §5.1 shape is fixed.
    conn: sqlite3.Connection,
    *,
    type: str,  # noqa: A002 — matches `Event.type` field name from spec §5.1.
    payload: Mapping[str, Any],
    source_event_id: str | None = None,
    correlation: Mapping[str, str] | None = None,
    ts_epoch_ms: int | None = None,
    schema_version: int | None = None,
    event_uid: str | None = None,
    actor: str | None = None,
    ingestion_node: str = "mac",
) -> Event:
    """Validate and INSERT one Event without committing or publishing.

    The caller must already own an explicit transaction.  This is the only
    append API used by multi-record CAS/outbox primitives: validation and row
    construction are identical to :func:`emit_event`, while the caller keeps
    sole authority over COMMIT/ROLLBACK and after-commit publication.

    Validation order (each step raises before any INSERT):
        1. `type` must be in `EventTypeRegistry`
           → `UnregisteredEventTypeError`.
        2. `schema_version` (when provided) must equal registry value
           → `SchemaVersionMismatchError`.
        3. Required-payload keys per registry must all be present in
           `payload` → `MissingPayloadFieldError`. Optional keys may be
           absent. Unknown keys are accepted Day-1 (spec §5.4 strict-mode
           is deferred — not on the Day-1 trace).
        4. `source_event_id` (when non-None) must reference an existing
           `events.event_uid` → `DanglingSourceEventError`.

    On success:
        - Generates `event_uid` (default `uuid.uuid4().hex`). Declared
          deviation from spec §3.7.10 (UUIDv7/ULID): the sortable-ID
          clause targets cross-domain ordering, inactive under the
          Mac-only scope — single domain, ordering authority is
          `id` / `ts_epoch_ms`. Python 3.12 stdlib has no `uuid7`, old
          rows can never be re-minted (`source_event_id` references
          them), so `ORDER BY event_uid` would stay unsafe regardless.
          Revisit at federation.
        - Stamps `actor` from the registry entry (schema v1); an explicit
          `actor=` argument overrides — for a future caller relaying a
          foreign-provenance event, not for routine emits.
        - Stamps `ingestion_node` (default `"mac"` — the only node in
          the Mac-only scope).
        - Uses registry `schema_version` if caller did not supply one.
        - Uses `_now_epoch_ms()` if caller did not supply `ts_epoch_ms`.
        - Serializes `payload` and `correlation` to JSON via stdlib
          `json.dumps`; payload mappings are normalized to `dict` first
          so non-dict `Mapping` implementations (e.g. `MappingProxyType`)
          serialize without surprise.
        - INSERTs one row without committing the connection.
        - Returns a frozen `jarvis.shared.Event` whose attributes will
          round-trip after the caller commits.

    Args:
        conn: Open Event Log connection (use `open_event_log`).
        type: Canonical event type (must be in `EventTypeRegistry`).
        payload: Event-specific data; required keys per registry must
            all be present.
        source_event_id: Optional UID of the upstream event that triggered
            this one. None for surface-originated events. When non-None,
            FK-style-validated against `events.event_uid`.
        correlation: Optional mapping of correlation keys (`turn_id` /
            `run_id` / `action_id`). Stored as JSON in `correlation_json`.
        ts_epoch_ms: Optional override for the timestamp (used by tests
            for deterministic ordering); default is `_now_epoch_ms()`.
        schema_version: Optional caller-supplied version; if provided it
            must equal the registry value or `SchemaVersionMismatchError`
            is raised. Default = registry value.
        event_uid: Optional caller-supplied uid (used by tests to assert
            round-trip); default = `uuid.uuid4().hex`.
        actor: Optional spec §5.1 provenance override; default = the
            registry entry's `actor`. Routine emits never pass this.
        ingestion_node: Node this event entered the log on; default
            `"mac"` (the only node in the Mac-only scope).

    Returns:
        The inserted, not-yet-committed Event as a frozen dataclass.

    Raises:
        TransactionRequiredError: If the caller does not already own a
            transaction on ``conn``.
    """
    if not conn.in_transaction:
        msg = "append_event_in_transaction requires a caller-owned transaction"
        raise TransactionRequiredError(msg)

    schema = EventTypeRegistry.get(type)
    if schema is None:
        msg = f"event type {type!r} is not registered"
        raise UnregisteredEventTypeError(msg)

    effective_schema_version = schema.schema_version if schema_version is None else schema_version
    if effective_schema_version != schema.schema_version:
        msg = (
            f"schema_version={schema_version!r} for type={type!r} does not match "
            f"registry value {schema.schema_version!r}"
        )
        raise SchemaVersionMismatchError(msg)

    missing = tuple(field for field in schema.required_payload if field not in payload)
    if missing:
        msg = f"event type {type!r} is missing required payload fields: {missing!r}"
        raise MissingPayloadFieldError(msg)

    if source_event_id is not None:
        _validate_source_event_id(conn, source_event_id)

    effective_event_uid = uuid.uuid4().hex if event_uid is None else event_uid
    effective_ts_epoch_ms = _now_epoch_ms() if ts_epoch_ms is None else ts_epoch_ms
    effective_actor = schema.actor if actor is None else actor

    # Normalize payload / correlation to plain dict before JSON encoding —
    # MappingProxyType / TypedDict-as-dict / custom Mapping subclasses all
    # round-trip through dict() cleanly without JSONEncoder having to know
    # about Mapping protocol.
    payload_dict: dict[str, Any] = dict(payload)
    correlation_dict: dict[str, str] | None = None if correlation is None else dict(correlation)
    if correlation_dict is not None and any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in correlation_dict.items()
    ):
        msg = "event correlation keys and values must be strings"
        raise InvalidCorrelationError(msg)

    payload_json = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
    correlation_json = (
        None
        if correlation_dict is None
        else json.dumps(correlation_dict, sort_keys=True, separators=(",", ":"))
    )

    conn.execute(
        """
        INSERT INTO events (
            event_uid, type, schema_version, ts_epoch_ms,
            payload_json, source_event_id, correlation_json,
            actor, ingestion_node
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            effective_event_uid,
            type,
            schema.schema_version,
            effective_ts_epoch_ms,
            payload_json,
            source_event_id,
            correlation_json,
            effective_actor,
            ingestion_node,
        ),
    )
    return Event(
        event_uid=effective_event_uid,
        type=type,
        schema_version=schema.schema_version,
        ts_epoch_ms=effective_ts_epoch_ms,
        payload=payload_dict,
        source_event_id=source_event_id,
        correlation=correlation_dict,
    )


def emit_event(  # noqa: PLR0913 — one keyword per Event column; spec §5.1 shape is fixed.
    conn: sqlite3.Connection,
    *,
    type: str,  # noqa: A002 — matches `Event.type` field name from spec §5.1.
    payload: Mapping[str, Any],
    source_event_id: str | None = None,
    correlation: Mapping[str, str] | None = None,
    ts_epoch_ms: int | None = None,
    schema_version: int | None = None,
    event_uid: str | None = None,
    actor: str | None = None,
    ingestion_node: str = "mac",
    committed_event_bus: CommittedEventBus | None = None,
) -> Event:
    """Commit one validated Event and optionally publish it after COMMIT.

    This remains the compatibility convenience API for a single event.  It
    rejects a connection already inside a transaction: committing there
    would prematurely commit unrelated caller-owned claim/outbox work.  Such
    callers must use :func:`append_event_in_transaction` and publish only
    after their outermost COMMIT succeeds.
    """
    if conn.in_transaction:
        msg = (
            "emit_event cannot run inside a caller-owned transaction; "
            "use append_event_in_transaction"
        )
        raise NestedEventTransactionError(msg)

    conn.execute("BEGIN IMMEDIATE")
    try:
        event = append_event_in_transaction(
            conn,
            type=type,
            payload=payload,
            source_event_id=source_event_id,
            correlation=correlation,
            ts_epoch_ms=ts_epoch_ms,
            schema_version=schema_version,
            event_uid=event_uid,
            actor=actor,
            ingestion_node=ingestion_node,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    if committed_event_bus is not None:
        committed_event_bus.publish(event)
    return event


# --- Reading helpers ---------------------------------------------------------


def _row_to_event(row: tuple[Any, ...]) -> Event:
    """Hydrate one SELECT row into a frozen Event dataclass."""
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


# Read-side SELECT statements. Literal strings (not composed) — ruff S608 is a
# false positive against parameterized queries; we keep these as full literals
# so static scanners stay happy and so the column order is obvious next to
# `_row_to_event`.
_SELECT_ALL_ORDERED_SQL: Final[str] = (
    "SELECT id, event_uid, type, schema_version, ts_epoch_ms, "
    "payload_json, source_event_id, correlation_json "
    "FROM events ORDER BY id ASC"
)

_SELECT_BY_UID_SQL: Final[str] = (
    "SELECT id, event_uid, type, schema_version, ts_epoch_ms, "
    "payload_json, source_event_id, correlation_json "
    "FROM events WHERE event_uid = ?"
)


def iter_events(conn: sqlite3.Connection) -> Iterator[Event]:
    """Yield all events in `id` order (physical append order).

    Suitable as the input to a pure-fold projection (Step 5 Task Ledger
    rebuild). Payloads are deserialized JSON; `correlation` is either
    None or a `dict[str, str]`.
    """
    cursor = conn.execute(_SELECT_ALL_ORDERED_SQL)
    for row in cursor:
        yield _row_to_event(row)


def iter_events_of_types(
    conn: sqlite3.Connection,
    event_types: Iterable[str],
) -> Iterator[Event]:
    """Yield selected event types in canonical append order.

    Values remain bound parameters; only the placeholder count is composed.
    CAS owners use this to avoid decoding unrelated history while holding a
    SQLite writer reservation.
    """
    selected = tuple(dict.fromkeys(event_types))
    if not selected:
        return
    cursor = conn.execute(
        "SELECT id, event_uid, type, schema_version, ts_epoch_ms, "
        "payload_json, source_event_id, correlation_json "
        "FROM events WHERE type IN (SELECT value FROM json_each(?)) ORDER BY id ASC",
        (json.dumps(selected),),
    )
    for row in cursor:
        yield _row_to_event(row)


def get_event(conn: sqlite3.Connection, event_uid: str) -> Event | None:
    """Return the Event with this `event_uid`, or None if absent."""
    cursor = conn.execute(_SELECT_BY_UID_SQL, (event_uid,))
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_event(row)


def iter_events_for_turn(
    conn: sqlite3.Connection,
    turn_id: str,
    event_types: Iterable[str],
) -> Iterator[Event]:
    """Yield the selected event types correlated to ``turn_id``, in append order.

    Uses the indexed ``correlation_id`` column (the promoted ``turn_id``), so a
    caller polling for one turn's outcome never scans unrelated history.
    """
    selected = tuple(dict.fromkeys(event_types))
    if not selected:
        return
    cursor = conn.execute(
        "SELECT id, event_uid, type, schema_version, ts_epoch_ms, "
        "payload_json, source_event_id, correlation_json "
        "FROM events WHERE correlation_id = ? "
        "AND type IN (SELECT value FROM json_each(?)) ORDER BY id ASC",
        (turn_id, json.dumps(selected)),
    )
    for row in cursor:
        yield _row_to_event(row)


__all__ = [
    "CommittedEventBus",
    "DanglingSourceEventError",
    "EventLogError",
    "EventTypeRegistry",
    "EventTypeSchema",
    "InvalidCorrelationError",
    "MissingPayloadFieldError",
    "NestedEventTransactionError",
    "SchemaVersionMismatchError",
    "TransactionRequiredError",
    "UnregisteredEventTypeError",
    "append_event_in_transaction",
    "emit_event",
    "get_event",
    "iter_events",
    "iter_events_for_turn",
    "iter_events_of_types",
    "open_event_log",
]
