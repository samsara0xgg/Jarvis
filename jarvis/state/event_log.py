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

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from pathlib import Path

# --- Schema constants --------------------------------------------------------

# Single table — projections (Step 5) live in their own module, no caches here.
_TABLE_NAME: Final[str] = "events"

_CREATE_TABLE_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    ts_epoch_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    source_event_id TEXT,
    correlation_json TEXT
)
"""

# Indexes: (type) for projection scans, (source_event_id) for cause-chain
# walks (acceptance A4), (ts_epoch_ms) for time-range / monotonic checks (A5).
_CREATE_INDEXES_SQL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)",
    "CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_epoch_ms)",
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


# --- EventTypeRegistry -------------------------------------------------------


@dataclass(frozen=True)
class EventTypeSchema:
    """One entry in the EventTypeRegistry (spec §5.4.1, Day-1 minimum).

    Day-1 keeps only the fields `emit_event` actually consumes
    (`required_payload`, `optional_payload`, `schema_version`) plus the
    `owner_layer` label used by canary H13. Producer / projection consumers
    / cross-domain policy / evidence semantics / artifact policy fields
    from the full spec §5.4.1 shape are deferred until a consumer exists.
    """

    event_type: str
    owner_layer: str
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
        # owner_layer is L4 per ADR-0002 § Day-2 EventTypeRegistry
        # extensions: in Day-1 the only emit-site was a hand-seeded test
        # fixture (L2); Day-2 Step 4 lands the `create_task` L4 tool, at
        # which point this declaration first becomes load-bearing.
        event_type="task.created",
        owner_layer="L4",
        required_payload=("task_id", "goal"),
        # `repo_path` + `verify_command` are populated by the Day-2
        # `create_task` L4 tool (ADR-0002 Step 4) from the JARVIS_LLM
        # ActionRequest; stored verbatim so L4 can later pass
        # `verify_command` to `/bin/sh -c` (Step 11).
        optional_payload=("source", "deadline", "repo_path", "verify_command"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="turn.started",
        owner_layer="L3",
        required_payload=("turn_id",),
        optional_payload=("trigger",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="turn.ended",
        owner_layer="L3",
        required_payload=("turn_id",),
        optional_payload=("final_response_hash",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="utterance.received",
        owner_layer="L5",
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
        event_type="entity.resolved",
        owner_layer="L3",
        required_payload=(
            "entity_type",
            "natural_ref",
            "resolved_to",
            "confidence",
            "candidates",
            "match_basis",
            "outcome",
        ),
        optional_payload=("resolver_warning",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.proposed",
        owner_layer="L3",
        required_payload=("action_id", "tool_name", "caller_principal", "risk_level"),
        optional_payload=("target_entity_ref", "run_id", "turn_id", "arguments"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="gate.evaluated",
        owner_layer="L3",
        required_payload=("gate", "outcome", "reasons"),
        # `attempt` carries the Pre-emit Gate retry index (0=initial,
        # 1=LLM retry, 2=forced template) so the audit trail captures
        # every verdict on the way to the final ResponsePlan, not just
        # the last one. Pre-action gate events omit it.
        optional_payload=(
            "action_id",
            "response_hash",
            "claim_levels",
            "check_results",
            "attempt",
        ),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.authorized",
        owner_layer="L3",
        required_payload=("action_id",),
        optional_payload=(),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.dispatched",
        owner_layer="L4",
        required_payload=("action_id",),
        optional_payload=(),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.running",
        owner_layer="L4",
        required_payload=("action_id",),
        optional_payload=(),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.result_observed",
        owner_layer="L4",
        required_payload=("action_id", "semantics"),
        # `error_payload` carries the L4 error-context dict for the
        # task-isolation error codes (`cross_task_artifact`,
        # `artifact_missing_task_id`); see
        # `jarvis.execution.tools._verify_diff_emit_error`.
        optional_payload=("tool_output", "error", "run_id", "error_payload"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.failed",
        owner_layer="L4",
        required_payload=("action_id",),
        optional_payload=("error", "reason"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.timeout_assumed",
        owner_layer="L4",
        required_payload=("action_id",),
        optional_payload=("error", "reason"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="action.cancelled",
        owner_layer="L4",
        required_payload=("action_id",),
        optional_payload=("error", "reason"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="run.started",
        owner_layer="L4",
        required_payload=("run_id", "task_id"),
        optional_payload=("runner",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="worker.reported",
        owner_layer="L4",
        required_payload=("run_id", "action_id", "status"),
        optional_payload=("summary", "artifact_path", "stash_ref"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="claim.created",
        owner_layer="L3",
        required_payload=(
            "claim_id",
            "type",
            "statement",
            "subject_ref",
            "produced_by_event_id",
        ),
        optional_payload=("supersedes",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="evidence.attached",
        owner_layer="L3",
        # ADR-0002 F8 / Step 12: `relation` added to required_payload so
        # every Evidence Record carries the (relation, level) pair from
        # spec §8.6. Deferred from Step 1 because the Day-1 emit-sites in
        # the Result Interpreter / unit fixtures had no relation
        # plumbing; Step 12 lifts every emit-site to derive relation
        # from the F2 ladder and amends the registry here.
        required_payload=("evidence_id", "claim_id", "relation", "level"),
        optional_payload=(
            "artifact_path",
            "content_hash",
            "scope",
            "freshness_ms",
            "source_type",
            "source_id",
            "observed_at",
            "freshness",
            "summary",
            "artifact_ref",
            "limitations",
        ),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="task.verified",
        owner_layer="L2",
        required_payload=("task_id", "by"),
        optional_payload=(),
        schema_version=1,
    ),
    # Fix 2 Option A — empty-diff + verify-pass paradox now emits
    # `task.no_op` instead of `task.verified`. Absent an artifact-change
    # Postcondition signal, the verify_command alone cannot support
    # `task.verified` (spec §8.9 — code task requires artifact changed +
    # verification passed). L3 emits this from
    # `_dispatch_one_tool_call` when the Result Interpreter signals
    # no_op. See amended ADR-0002 § Evidence ladder paradox row.
    EventTypeSchema(
        event_type="task.no_op",
        owner_layer="L3",
        required_payload=("task_id",),
        optional_payload=("reason", "verify_command"),
        schema_version=1,
    ),
    # --- Day-2 extensions (ADR-0002 § Day-2 EventTypeRegistry extensions) ---
    #
    # Eight Day-2 originals (worker.* heartbeat/artifact_observed/
    # report_missing, task.executor_assigned/reported, cost.recorded,
    # surface.user_intent/response_emitted) plus four sleep/wake events
    # (mac.sleeping/awake, worker.suspended_by_sleep/terminated_by_sleep).
    # Schema lifted verbatim from ADR-0002 lines 1162-1262.
    EventTypeSchema(
        event_type="worker.heartbeat",
        owner_layer="L4",
        required_payload=("run_id", "action_id"),
        optional_payload=("elapsed_ms", "last_log_line", "summary"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="worker.artifact_observed",
        owner_layer="L4",
        required_payload=("run_id", "action_id", "artifact_path"),
        optional_payload=("content_hash", "kind"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="worker.report_missing",
        owner_layer="L4",
        required_payload=("run_id", "action_id"),
        optional_payload=("reason",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="task.executor_assigned",
        owner_layer="L3",
        required_payload=("task_id", "executor", "action_id"),
        optional_payload=("model",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="task.executor_reported",
        owner_layer="L4",
        required_payload=("task_id", "run_id", "status"),
        optional_payload=("summary", "diff_path"),
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
        required_payload=("kind", "model"),
        optional_payload=(
            "tokens_in",
            "tokens_out",
            "cache_read_in",
            "cache_write_in",
            "cost_usd",
            "run_id",
        ),
        schema_version=1,
    ),
    # F6: surface.user_intent — spec.html §5.4 line 1442 canonical;
    # missing from Day-1 registry, restored here.
    EventTypeSchema(
        event_type="surface.user_intent",
        owner_layer="L5",
        required_payload=("transcript", "turn_id"),
        optional_payload=("channel", "language"),
        schema_version=1,
    ),
    # F6: surface.response_emitted — NOT in spec §5.4 canonical list
    # (deviation V1); Day-2 audit field for delivered_via +
    # attention_channel.
    EventTypeSchema(
        event_type="surface.response_emitted",
        owner_layer="L5",
        required_payload=("turn_id", "text"),
        optional_payload=(
            "delivered_via",
            "attention_channel",
            "voice_text",
            "document_text",
            "response_hash",
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
        required_payload=("turn_id", "query", "kind"),
        # ADR-0005 §7: L5 TTS consumers read ``required_gate_mode`` from the
        # open header to route between sentence-streaming and full-text TTS
        # playback per spec §3.6.6. Optional so legacy emitters (and the
        # event-log unit tests that emit directly via emit_event) keep working.
        optional_payload=("required_gate_mode",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="surface.response_chunk",
        owner_layer="L5",
        required_payload=("turn_id", "text"),
        optional_payload=(),
        schema_version=1,
    ),
    # Sleep/wake events per spec §3.7.8 — L6 owned (Deployment Domain).
    EventTypeSchema(
        event_type="mac.sleeping",
        owner_layer="L6",
        required_payload=("ts_epoch_ms",),
        optional_payload=("reason", "in_progress_action_ids"),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="mac.awake",
        owner_layer="L6",
        required_payload=("ts_epoch_ms", "slept_for_ms"),
        optional_payload=("reconciliation_summary",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="worker.suspended_by_sleep",
        owner_layer="L6",
        required_payload=("run_id", "action_id"),
        optional_payload=("last_heartbeat_ts",),
        schema_version=1,
    ),
    EventTypeSchema(
        event_type="worker.terminated_by_sleep",
        owner_layer="L6",
        required_payload=("run_id", "action_id"),
        optional_payload=("reason",),
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
        required_payload=("turn_id", "exception_repr"),
        optional_payload=("trigger_event_id",),
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


def open_event_log(path: Path) -> sqlite3.Connection:
    """Open (or create) the SQLite Event Log at `path`.

    Schema, indexes, and append-only triggers are installed via
    `CREATE ... IF NOT EXISTS` so repeated calls on the same path are
    idempotent — schema is not recreated, triggers are not duplicated.

    Connection behavior:
        - Default `isolation_level` (deferred transactions).
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
    conn.execute(_CREATE_TABLE_SQL)
    for index_sql in _CREATE_INDEXES_SQL:
        conn.execute(index_sql)
    conn.execute(_CREATE_TRIGGER_NO_UPDATE_SQL)
    conn.execute(_CREATE_TRIGGER_NO_DELETE_SQL)
    conn.commit()
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
) -> Event:
    """Validate, INSERT, and return one Event (single L3/L4/L5 append API).

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
        - Generates `event_uid` (default `uuid.uuid4().hex` — spec §5.1
          calls for UUIDv7-ish; Python 3.12 stdlib has no UUIDv7, so
          Day-1 uses uuid4 hex; the `jarvis.shared.Event` docstring
          already documents this as "UUIDv7-ish hex string").
        - Uses registry `schema_version` if caller did not supply one.
        - Uses `_now_epoch_ms()` if caller did not supply `ts_epoch_ms`.
        - Serializes `payload` and `correlation` to JSON via stdlib
          `json.dumps`; payload mappings are normalized to `dict` first
          so non-dict `Mapping` implementations (e.g. `MappingProxyType`)
          serialize without surprise.
        - INSERTs one row and commits the connection.
        - Returns a frozen `jarvis.shared.Event` whose attributes round-trip
          with the persisted row.

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

    Returns:
        The persisted Event as a frozen dataclass.
    """
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

    # Normalize payload / correlation to plain dict before JSON encoding —
    # MappingProxyType / TypedDict-as-dict / custom Mapping subclasses all
    # round-trip through dict() cleanly without JSONEncoder having to know
    # about Mapping protocol.
    payload_dict: dict[str, Any] = dict(payload)
    correlation_dict: dict[str, str] | None = None if correlation is None else dict(correlation)

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
            payload_json, source_event_id, correlation_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            effective_event_uid,
            type,
            schema.schema_version,
            effective_ts_epoch_ms,
            payload_json,
            source_event_id,
            correlation_json,
        ),
    )
    conn.commit()

    return Event(
        event_uid=effective_event_uid,
        type=type,
        schema_version=schema.schema_version,
        ts_epoch_ms=effective_ts_epoch_ms,
        payload=payload_dict,
        source_event_id=source_event_id,
        correlation=correlation_dict,
    )


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


def get_event(conn: sqlite3.Connection, event_uid: str) -> Event | None:
    """Return the Event with this `event_uid`, or None if absent."""
    cursor = conn.execute(_SELECT_BY_UID_SQL, (event_uid,))
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_event(row)


__all__ = [
    "DanglingSourceEventError",
    "EventLogError",
    "EventTypeRegistry",
    "EventTypeSchema",
    "MissingPayloadFieldError",
    "SchemaVersionMismatchError",
    "UnregisteredEventTypeError",
    "emit_event",
    "get_event",
    "iter_events",
    "open_event_log",
]
