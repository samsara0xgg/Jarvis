"""L2 — read-only invariants over a runtime root's event log (ADR 0324).

Hermetic tests cannot see the real environment: the Pi has no ``afconvert``, a
Pi without a battery clock boots in the past, a real device sends what no
fixture imagined. This module asks the *real* log what the spec and the code
promise about it. Every invariant is a query over rows the log already holds
(plus, for the audio refs, a ``stat`` of the recordings directory); each names
the rule it checks, and a violation is reported as ids, types, counts and
timestamps only. A payload is never read into the report: the log holds the
owner's words and possibly secrets.

The check opens the log read-only (``mode=ro``; ``immutable=1`` when no ``-wal``
file shows a writer), takes no daemon lock and writes nothing, so it is safe
while the daemon runs and on a backup copy. It is anchored at the log's newest
timestamp, not at the wall clock, so a copy checked a week later still checks
the days it holds (a "not yet closed" run is judged against the copy's own
last moment). Each invariant is bounded by the window and by the indexes the
log already has (``docs/adr/0324``); ``ALL_QUERIES`` lists every statement for
the ``EXPLAIN QUERY PLAN`` test.

Layer: stdlib plus ``jarvis.state.event_log``'s registry.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from jarvis.state.event_log import EventTypeRegistry

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

# The file ``bootstrap_runtime`` places (``RuntimePaths.event_log``). A read-only tool cannot call
# ``bootstrap_runtime``: it creates directories.
EVENT_LOG_FILENAME: Final = "mac_events.db"
AUDIO_DIR_PARTS: Final = ("memory", "audio")

DAY_MS: Final = 86_400_000
DEFAULT_SINCE_DAYS: Final = 2.0
MAX_SINCE_DAYS: Final = 3650.0
DEFAULT_EXAMPLES: Final = 3

# A run, action or playback is "not yet closed" only after this long. The longest legitimate wait
# is an action's supervisor budget (700 s, `supervisor.default_budget_s`) with the 30 s sweep after
# it; the response run of a turn that waits on such a lookup stays open as long.
CLOSE_GRACE_MS: Final = 20 * 60_000
# Rows written after another one by a different thread or by a queued write (the audio file, the
# cost row of a request that just ended) need a moment; this is how long.
SETTLE_MS: Final = 2 * 60_000
# `ts_epoch_ms` is read inside `BEGIN IMMEDIATE`, so ids and timestamps agree to the millisecond
# unless the clock moves. A step of more than this is a step of the clock.
CLOCK_BACK_TOLERANCE_MS: Final = 2_000
CLOCK_FUTURE_TOLERANCE_MS: Final = 60_000

# Event types whose emitters never pass a timestamp of their own (night.*, mac.*, ride.*, thing.*,
# doings.stated and everything a terminal relays carry one of the caller's or the device's), so
# their `ts_epoch_ms` is the append instant and follows `id`.
CLOCK_STAMPED_TYPES: Final[tuple[str, ...]] = (
    "turn.started",
    "turn.ended",
    "turn.failed",
    "turn.timing",
    "cost.recorded",
    "gate.evaluated",
    "response.started",
    "response.completed",
    "response.cancelled",
    "response.failed",
    "action.dispatched",
    "action.running",
    "action.result_observed",
    "action.failed",
    "action.timeout_assumed",
    "action.cancelled",
    "surface.response_emitted",
)

RESPONSE_STARTED: Final = "response.started"
RESPONSE_TERMINALS: Final[tuple[str, ...]] = (
    "response.completed",
    "response.cancelled",
    "response.failed",
)
ACTION_DISPATCHED: Final = "action.dispatched"
ACTION_TERMINALS: Final[tuple[str, ...]] = (
    "action.result_observed",
    "action.failed",
    "action.timeout_assumed",
    "action.cancelled",
)
PLAYBACK_STARTED: Final = "surface.playback_started"
PLAYBACK_TERMINALS: Final[tuple[str, ...]] = (
    "surface.playback_completed",
    "surface.playback_interrupted",
    "surface.playback_failed",
)
CONFIRMATION_TERMINALS: Final[tuple[str, ...]] = (
    "confirmation.accepted",
    "confirmation.rejected",
    "confirmation.expired",
)
REMINDER_ENDS: Final[tuple[str, ...]] = ("reminder.fired", "reminder.cancelled")

# What a payload must never hold (spec: key, JWT and device-token shapes stay out of the log). Each
# shape has the literal anchors SQLite looks for first, so the regexes run on a few rows only.
_SECRET_SHAPES: Final[tuple[tuple[str, tuple[str, ...], re.Pattern[str]], ...]] = (
    ("pem_private_key", ("-----BEGIN",), re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("sk_api_key", ("sk-",), re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}")),
    ("aws_access_key_id", ("AKIA", "ASIA"), re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "github_token",
        ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"),
    ),
    ("slack_token", ("xox",), re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", ("AIza",), re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "jwt",
        ("eyJ",),
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    ("bearer_token", ("Bearer ",), re.compile(r"\bBearer [A-Za-z0-9._~+/=-]{24,}")),
)
SECRET_SHAPE_NAMES: Final[tuple[str, ...]] = tuple(name for name, _anchors, _p in _SECRET_SHAPES)
_SECRET_ANCHORS: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(anchor for _name, anchors, _pattern in _SECRET_SHAPES for anchor in anchors)
)
_PRINTABLE_TYPE: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")

# --- The statements --------------------------------------------------------
#
# Every statement the checks run, so `ALL_QUERIES` can hand them to `EXPLAIN QUERY PLAN`. The log
# is append-only and grows without end; each statement is bounded by `ts_epoch_ms`/`id` of the
# window or by the cardinality of one event type, and none scans the table.

_SQL_HEAD: Final = "SELECT MAX(ts_epoch_ms) FROM events"
# Named, because the planner would walk the table from its first row to find the least id.
_SQL_START_ID: Final = "SELECT MIN(id) FROM events INDEXED BY idx_events_ts WHERE ts_epoch_ms >= ?"
_SQL_WINDOW_COUNT: Final = "SELECT COUNT(*) FROM events WHERE ts_epoch_ms >= ?"
# Loose index scan: one seek per distinct type, not one row per event.
_SQL_DISTINCT_TYPES: Final = (
    "WITH RECURSIVE kinds(type) AS ("
    "SELECT MIN(type) FROM events "
    "UNION ALL SELECT (SELECT MIN(type) FROM events WHERE type > kinds.type) FROM kinds "
    "WHERE kinds.type IS NOT NULL) "
    "SELECT type FROM kinds WHERE type IS NOT NULL"
)
_SQL_TYPE_IN_WINDOW: Final = (
    "SELECT id, ts_epoch_ms FROM events WHERE type = ? AND ts_epoch_ms >= ?"
)
_SQL_SOURCE_DANGLING: Final = (
    "SELECT e.id, e.ts_epoch_ms FROM events e "
    "WHERE e.id >= ? AND e.source_event_id IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM events s WHERE s.event_uid = e.source_event_id)"
)
_SQL_CLOCK_ROWS: Final = (
    "SELECT id, ts_epoch_ms FROM events "
    "WHERE id >= ? AND type IN (SELECT value FROM json_each(?)) ORDER BY id"
)
_SQL_CLOCK_FUTURE: Final = (
    "SELECT id, ts_epoch_ms FROM events "
    "WHERE type IN (SELECT value FROM json_each(?)) AND ts_epoch_ms > ? ORDER BY id"
)
_SQL_DUPLICATE_TERMINALS: Final = (
    "SELECT MAX(id), ts_epoch_ms FROM events "
    "WHERE type IN (SELECT value FROM json_each(?)) AND ts_epoch_ms >= ? "
    "GROUP BY json_extract(payload_json, '$.{key}') HAVING COUNT(*) > 1"
)
_SQL_DUPLICATE_PLAYBACK: Final = (
    "SELECT MAX(id), ts_epoch_ms FROM events "
    "WHERE type IN (SELECT value FROM json_each(?)) AND ts_epoch_ms >= ? "
    "GROUP BY json_extract(payload_json, '$.response_id'), "
    "json_extract(payload_json, '$.playback_generation_id') HAVING COUNT(*) > 1"
)
_SQL_UNCLOSED: Final = (
    "SELECT s.id, s.ts_epoch_ms FROM events s "
    "WHERE s.type = ? AND s.ts_epoch_ms >= ? AND s.ts_epoch_ms <= ? "
    "AND NOT EXISTS (SELECT 1 FROM events t "
    "WHERE t.type IN (SELECT value FROM json_each(?)) "
    "AND json_extract(t.payload_json, '$.{key}') = json_extract(s.payload_json, '$.{key}'))"
)
_SQL_UNCLOSED_PLAYBACK: Final = (
    "SELECT s.id, s.ts_epoch_ms FROM events s "
    "WHERE s.type = ? AND s.ts_epoch_ms >= ? AND s.ts_epoch_ms <= ? "
    "AND NOT EXISTS (SELECT 1 FROM events t "
    "WHERE t.type IN (SELECT value FROM json_each(?)) "
    "AND json_extract(t.payload_json, '$.response_id') "
    "= json_extract(s.payload_json, '$.response_id') "
    "AND json_extract(t.payload_json, '$.playback_generation_id') "
    "= json_extract(s.payload_json, '$.playback_generation_id'))"
)
_SQL_TIMING_REQUESTS: Final = (
    "SELECT t.id, t.ts_epoch_ms, json_extract(r.value, '$.llm_request_id') "
    "FROM events t, json_each(t.payload_json, '$.requests') AS r "
    "WHERE t.type = 'turn.timing' AND t.ts_epoch_ms >= ? AND t.ts_epoch_ms <= ?"
)
_SQL_COST_IDS: Final = (
    "SELECT json_extract(payload_json, '$.llm_request_id') FROM events "
    "WHERE type = 'cost.recorded' AND ts_epoch_ms >= ?"
)
_SQL_COST_TWICE: Final = (
    "SELECT MAX(id), ts_epoch_ms FROM events "
    "WHERE type = 'cost.recorded' AND ts_epoch_ms >= ? "
    "AND json_extract(payload_json, '$.llm_request_id') IS NOT NULL "
    "GROUP BY json_extract(payload_json, '$.llm_request_id') HAVING COUNT(*) > 1"
)
_SQL_AUDIO_REFS: Final = (
    "SELECT id, ts_epoch_ms, json_extract(payload_json, '$.audio_artifact_ref') FROM events "
    "WHERE type = 'utterance.received' AND ts_epoch_ms >= ? AND ts_epoch_ms <= ? "
    "AND ingestion_node = 'mac' "
    "AND json_extract(payload_json, '$.audio_artifact_ref') IS NOT NULL"
)
_SQL_REMINDERS_UNFIRED: Final = (
    "SELECT s.id, s.ts_epoch_ms FROM events s "
    "WHERE s.type = 'reminder.scheduled' "
    "AND json_extract(s.payload_json, '$.due_at_epoch_ms') BETWEEN ? AND ? "
    "AND NOT EXISTS (SELECT 1 FROM events t "
    "WHERE t.type IN (SELECT value FROM json_each(?)) "
    "AND json_extract(t.payload_json, '$.reminder_id') "
    "= json_extract(s.payload_json, '$.reminder_id'))"
)
# The anchors are the module's own literals (checked to hold no quote by the tests), never input.
_ANCHOR_TESTS: Final = " OR ".join(f"instr(payload_json, '{one}') > 0" for one in _SECRET_ANCHORS)
_SQL_SHAPE_CANDIDATES: Final = (
    "SELECT id, ts_epoch_ms, payload_json FROM events WHERE id >= ? "  # noqa: S608
    f"AND ({_ANCHOR_TESTS})"
)

# The key is one of three literals here, never input: the statement stays static.
_UNCLOSED_RESPONSE: Final = _SQL_UNCLOSED.format(key="response_id")
_UNCLOSED_ACTION: Final = _SQL_UNCLOSED.format(key="action_id")
_DUPLICATE_RESPONSE: Final = _SQL_DUPLICATE_TERMINALS.format(key="response_id")
_DUPLICATE_ACTION: Final = _SQL_DUPLICATE_TERMINALS.format(key="action_id")
_DUPLICATE_CONFIRMATION: Final = _SQL_DUPLICATE_TERMINALS.format(key="confirmation_id")

ALL_QUERIES: Final[tuple[tuple[str, str], ...]] = (
    ("head", _SQL_HEAD),
    ("start_id", _SQL_START_ID),
    ("window_count", _SQL_WINDOW_COUNT),
    ("distinct_types", _SQL_DISTINCT_TYPES),
    ("type_in_window", _SQL_TYPE_IN_WINDOW),
    ("source_dangling", _SQL_SOURCE_DANGLING),
    ("clock_rows", _SQL_CLOCK_ROWS),
    ("clock_future", _SQL_CLOCK_FUTURE),
    ("duplicate_response", _DUPLICATE_RESPONSE),
    ("duplicate_action", _DUPLICATE_ACTION),
    ("duplicate_confirmation", _DUPLICATE_CONFIRMATION),
    ("duplicate_playback", _SQL_DUPLICATE_PLAYBACK),
    ("unclosed_response", _UNCLOSED_RESPONSE),
    ("unclosed_action", _UNCLOSED_ACTION),
    ("unclosed_playback", _SQL_UNCLOSED_PLAYBACK),
    ("timing_requests", _SQL_TIMING_REQUESTS),
    ("cost_ids", _SQL_COST_IDS),
    ("cost_twice", _SQL_COST_TWICE),
    ("audio_refs", _SQL_AUDIO_REFS),
    ("reminders_unfired", _SQL_REMINDERS_UNFIRED),
    ("shape_candidates", _SQL_SHAPE_CANDIDATES),
)

# --- The report ------------------------------------------------------------

Status = Literal["ok", "violated", "skipped"]


class LogCheckError(Exception):
    """The log could not be opened or is not an event log (the CLI exits 2)."""


@dataclass(frozen=True)
class Example:
    """One violating row: its id, its time, and a note from a fixed vocabulary (no payload)."""

    event_id: int
    ts_epoch_ms: int
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        """The JSON shape."""
        return {"event_id": self.event_id, "ts": iso(self.ts_epoch_ms), "note": self.note}


@dataclass(frozen=True)
class InvariantResult:
    """What one invariant found in the window."""

    name: str
    rule: str
    ref: str
    status: Status
    violations: int = 0
    examples: tuple[Example, ...] = ()
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        """The JSON shape."""
        return {
            "name": self.name,
            "status": self.status,
            "violations": self.violations,
            "examples": [one.to_dict() for one in self.examples],
            "detail": self.detail,
            "rule": self.rule,
            "ref": self.ref,
        }


@dataclass(frozen=True)
class LogReport:
    """The whole check: the window it covered and each invariant's result."""

    event_log: str
    checked_at_ms: int
    since_ms: int
    until_ms: int
    events: int
    results: tuple[InvariantResult, ...]

    @property
    def violated(self) -> tuple[InvariantResult, ...]:
        """The invariants with at least one violation."""
        return tuple(one for one in self.results if one.status == "violated")

    def to_dict(self) -> dict[str, object]:
        """The JSON shape: ids, types, counts and timestamps, no payload."""
        return {
            "ok": not self.violated,
            "event_log": self.event_log,
            "checked_at": iso(self.checked_at_ms),
            "window": {
                "since": iso(self.since_ms),
                "until": iso(self.until_ms),
                "events": self.events,
            },
            "violated": len(self.violated),
            "invariants": [one.to_dict() for one in self.results],
        }


def iso(epoch_ms: int) -> str:
    """UTC, second precision: ``2026-10-21T03:30:00Z``; the number itself when it is no date."""
    try:
        moment = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return f"{epoch_ms} ms"
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


# --- The scope every check reads --------------------------------------------


@dataclass(frozen=True)
class _Scope:
    conn: sqlite3.Connection
    runtime_root: Path
    now_ms: int
    ref_ms: int
    since_ms: int
    start_id: int | None
    events: int
    max_examples: int


@dataclass
class _Found:
    """What a check collects: a count, the first few examples, and a skip reason or a detail."""

    limit: int
    count: int = 0
    examples: list[Example] = field(default_factory=list)
    skipped: str | None = None
    detail: str | None = None

    def add(self, event_id: int, ts_epoch_ms: int, note: str | None = None) -> None:
        self.count += 1
        if len(self.examples) < self.limit:
            self.examples.append(Example(int(event_id), int(ts_epoch_ms), note))


@dataclass(frozen=True)
class Invariant:
    """One promise: its name, why, where it is made, and the check."""

    name: str
    rule: str
    ref: str
    check: Callable[[_Scope, _Found], None]


def _rows(scope: _Scope, sql: str, *params: object) -> Iterator[tuple[object, ...]]:
    yield from scope.conn.execute(sql, params)


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _json_list(values: Sequence[str]) -> str:
    return json.dumps(list(values))


# --- The invariants ---------------------------------------------------------


def _types_registered(scope: _Scope, found: _Found) -> None:
    known = set(EventTypeRegistry.iter_types())
    for (type_,) in _rows(scope, _SQL_DISTINCT_TYPES):
        if type_ in known:
            continue
        label = type_ if isinstance(type_, str) and _PRINTABLE_TYPE.fullmatch(type_) else "?"
        for event_id, ts in _rows(scope, _SQL_TYPE_IN_WINDOW, type_, scope.since_ms):
            found.add(_as_int(event_id), _as_int(ts), label)


def _sources_resolve(scope: _Scope, found: _Found) -> None:
    if scope.start_id is None:
        return
    for event_id, ts in _rows(scope, _SQL_SOURCE_DANGLING, scope.start_id):
        found.add(_as_int(event_id), _as_int(ts))


def _clock_does_not_run_backwards(scope: _Scope, found: _Found) -> None:
    if scope.start_id is None:
        return
    types = _json_list(CLOCK_STAMPED_TYPES)
    previous: int | None = None
    for event_id, ts in _rows(scope, _SQL_CLOCK_ROWS, scope.start_id, types):
        now = _as_int(ts)
        if previous is not None and previous - now > CLOCK_BACK_TOLERANCE_MS:
            found.add(_as_int(event_id), now, f"back {(previous - now) // 1000} s")
        previous = now


def _clock_is_not_ahead(scope: _Scope, found: _Found) -> None:
    types = _json_list(CLOCK_STAMPED_TYPES)
    limit = scope.now_ms + CLOCK_FUTURE_TOLERANCE_MS
    for event_id, ts in _rows(scope, _SQL_CLOCK_FUTURE, types, limit):
        found.add(_as_int(event_id), _as_int(ts), f"ahead {(_as_int(ts) - scope.now_ms) // 1000} s")


def _terminals_are_single(scope: _Scope, found: _Found) -> None:
    lifecycles = (
        ("response", _DUPLICATE_RESPONSE, RESPONSE_TERMINALS),
        ("action", _DUPLICATE_ACTION, ACTION_TERMINALS),
        ("playback", _SQL_DUPLICATE_PLAYBACK, PLAYBACK_TERMINALS),
        ("confirmation", _DUPLICATE_CONFIRMATION, CONFIRMATION_TERMINALS),
    )
    rows: list[tuple[int, int, str]] = []
    for label, sql, terminals in lifecycles:
        rows.extend(
            (_as_int(event_id), _as_int(ts), label)
            for event_id, ts in _rows(scope, sql, _json_list(terminals), scope.since_ms)
        )
    for event_id, ts, label in sorted(rows):
        found.add(event_id, ts, label)


def _unclosed(
    scope: _Scope, found: _Found, sql: str, started: str, terminals: Sequence[str],
) -> None:
    cutoff = scope.ref_ms - CLOSE_GRACE_MS
    for event_id, ts in _rows(scope, sql, started, scope.since_ms, cutoff, _json_list(terminals)):
        found.add(_as_int(event_id), _as_int(ts))


def _responses_close(scope: _Scope, found: _Found) -> None:
    _unclosed(scope, found, _UNCLOSED_RESPONSE, RESPONSE_STARTED, RESPONSE_TERMINALS)


def _actions_close(scope: _Scope, found: _Found) -> None:
    _unclosed(scope, found, _UNCLOSED_ACTION, ACTION_DISPATCHED, ACTION_TERMINALS)


def _playbacks_close(scope: _Scope, found: _Found) -> None:
    _unclosed(scope, found, _SQL_UNCLOSED_PLAYBACK, PLAYBACK_STARTED, PLAYBACK_TERMINALS)


def _every_request_has_its_cost(scope: _Scope, found: _Found) -> None:
    recorded = {
        one
        for (one,) in _rows(scope, _SQL_COST_IDS, scope.since_ms - CLOSE_GRACE_MS)
        if isinstance(one, str)
    }
    settled = scope.ref_ms - SETTLE_MS
    reported: set[int] = set()
    for event_id, ts, request_id in _rows(scope, _SQL_TIMING_REQUESTS, scope.since_ms, settled):
        row = _as_int(event_id)
        if isinstance(request_id, str) and request_id not in recorded and row not in reported:
            reported.add(row)
            found.add(row, _as_int(ts))


def _cost_is_recorded_once(scope: _Scope, found: _Found) -> None:
    for event_id, ts in _rows(scope, _SQL_COST_TWICE, scope.since_ms):
        found.add(_as_int(event_id), _as_int(ts))


def _audio_refs_exist(scope: _Scope, found: _Found) -> None:
    audio_dir = scope.runtime_root.joinpath(*AUDIO_DIR_PARTS)
    have_dir = audio_dir.is_dir()
    unverifiable = 0
    checked = 0
    settled = scope.ref_ms - SETTLE_MS
    for event_id, ts, ref in _rows(scope, _SQL_AUDIO_REFS, scope.since_ms, settled):
        if not isinstance(ref, str):
            found.add(_as_int(event_id), _as_int(ts), "not text")
            continue
        path = _plain_path(ref)
        if path is not None and path.is_absolute() and path.is_file():
            checked += 1
        elif not have_dir:
            unverifiable += 1
        elif path is not None and (audio_dir / path.name).is_file():
            checked += 1
        else:
            checked += 1
            found.add(_as_int(event_id), _as_int(ts))
    if unverifiable:
        found.detail = (
            f"{unverifiable} ref(s) not checked: the root has no {'/'.join(AUDIO_DIR_PARTS)} "
            "directory (a copy of the databases only?)"
        )
        if not checked and not found.count:
            found.skipped = found.detail


def _plain_path(ref: str) -> Path | None:
    return None if not ref or "\x00" in ref else Path(ref)


def _reminders_fire(scope: _Scope, found: _Found) -> None:
    cutoff = scope.ref_ms - CLOSE_GRACE_MS
    for event_id, ts in _rows(
        scope, _SQL_REMINDERS_UNFIRED, scope.since_ms, cutoff, _json_list(REMINDER_ENDS),
    ):
        found.add(_as_int(event_id), _as_int(ts))


def _no_secret_shapes(scope: _Scope, found: _Found) -> None:
    if scope.start_id is None:
        return
    for event_id, ts, payload in _rows(scope, _SQL_SHAPE_CANDIDATES, scope.start_id):
        if not isinstance(payload, str):
            continue
        names = [name for name, _anchors, pattern in _SECRET_SHAPES if pattern.search(payload)]
        if names:
            found.add(_as_int(event_id), _as_int(ts), ", ".join(names))


INVARIANTS: Final[tuple[Invariant, ...]] = (
    Invariant(
        "event.type_registered",
        "Every event in the log has a type the registry knows; an unregistered type cannot be "
        "appended, so one in the log came from a writer that bypassed or outran the registry.",
        "docs/spec.html#event-log; jarvis/state/event_log.py EventTypeRegistry",
        _types_registered,
    ),
    Invariant(
        "event.source_resolves",
        "An event's source_event_id names an event that exists; the append validates it.",
        "jarvis/state/event_log.py append_event_in_transaction (DanglingSourceEventError)",
        _sources_resolve,
    ),
    Invariant(
        "event.clock_not_backwards",
        "Timestamps of events the brain stamps itself never run backwards in id order by more "
        f"than {CLOCK_BACK_TOLERANCE_MS // 1000} s: ts is read inside BEGIN IMMEDIATE, so a step "
        "back is the host clock stepping (a Pi without a battery clock booting in the past).",
        "jarvis/state/event_log.py (ordering authority is id / ts_epoch_ms); docs/adr/0324",
        _clock_does_not_run_backwards,
    ),
    Invariant(
        "event.clock_not_ahead",
        "No brain-stamped event is dated more than "
        f"{CLOCK_FUTURE_TOLERANCE_MS // 1000} s after the time of this check.",
        "jarvis/state/event_log.py (ts is the append instant); docs/adr/0324",
        _clock_is_not_ahead,
    ),
    Invariant(
        "lifecycle.single_terminal",
        "A response, action, playback generation or confirmation ends with at most one terminal "
        "event; the terminal is a compare-and-set.",
        "jarvis/state/lifecycle_terminal.py (ADR 0008); docs/spec.html#event-log",
        _terminals_are_single,
    ),
    Invariant(
        "response.run_terminates",
        "A response.started older than the grace has a response.completed, .cancelled or .failed; "
        "a run a crash abandoned is closed as daemon_restart at the next boot.",
        "jarvis/decision/response_run.py reconcile_open_responses (ADR 0008)",
        _responses_close,
    ),
    Invariant(
        "action.dispatch_terminates",
        "An action.dispatched older than the grace has a terminal event; the supervisor sweep "
        "closes an action nobody reported on as action.timeout_assumed.",
        "jarvis/deployment/sleep_wake.py sweep_overdue_actions (ADR 0009 D4)",
        _actions_close,
    ),
    Invariant(
        "playback.started_terminates",
        "A surface.playback_started older than the grace has a playback terminal; a generation "
        "a crash abandoned is closed at the next boot.",
        "jarvis/surface/playback_recovery.py reconcile_open_playback (ADR 0008)",
        _playbacks_close,
    ),
    Invariant(
        "cost.recorded_per_request",
        "Every model request a turn.timing lists has a cost.recorded row with the same "
        "llm_request_id (the exactly-once cost guard wraps every provider call).",
        "docs/spec.html#local-endpoints (turn.timing requests); "
        "tests/canary/test_canary_cost_recorded_emitted_per_llm_call.py",
        _every_request_has_its_cost,
    ),
    Invariant(
        "cost.recorded_once",
        "No llm_request_id has more than one cost.recorded row.",
        "jarvis/state/cost_accounting.py record_cost_disposition_once",
        _cost_is_recorded_once,
    ),
    Invariant(
        "utterance.audio_ref_exists",
        "An utterance the brain heard names an audio file that exists (absolute, or by name under "
        "memory/audio of the root); terminal-heard utterances name a file on the terminal and are "
        "not checked. Keep --since-days under memory.audio_retention_days.",
        "jarvis/surface/voice_artifact_store.py planned_ref / persist; docs/spec.html#retention",
        _audio_refs_exist,
    ),
    Invariant(
        "reminder.fires_when_due",
        "A reminder whose due time is past the grace was fired or cancelled; the tick fires every "
        "pending reminder that is due, late if it must.",
        "jarvis/runtime/reminders.py tick; jarvis/state/reminders.py (ADR 0179)",
        _reminders_fire,
    ),
    Invariant(
        "payload.no_secret_shapes",
        "No payload holds the shape of a key, token or private key (reported by shape name only).",
        "docs/spec.html#threat-table (credential leaks); docs/spec.html#key-storage",
        _no_secret_shapes,
    ),
)

# --- Opening and running -----------------------------------------------------


def open_read_only(event_log: Path) -> sqlite3.Connection:
    """Open ``event_log`` without writing to it or its directory.

    ``mode=ro`` reads a WAL log a daemon is writing (its ``-wal`` and ``-shm`` already exist).
    With no ``-wal`` beside the file nothing is writing, and a plain read-only open of a WAL
    file would create the ``-shm`` and ``-wal`` files; ``immutable=1`` does not.

    Raises:
        LogCheckError: There is no such file, or it is not an event log.
    """
    if not event_log.is_file():
        message = f"no event log at {event_log}"
        raise LogCheckError(message)
    written_by_a_daemon = event_log.with_name(event_log.name + "-wal").exists()
    flags = "mode=ro" if written_by_a_daemon else "mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(f"{event_log.resolve().as_uri()}?{flags}", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        message = f"cannot open {event_log}: {exc}"
        raise LogCheckError(message) from exc
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA cache_size = -65536")
        conn.execute("SELECT 1 FROM events LIMIT 1").fetchall()
    except sqlite3.Error as exc:
        conn.close()
        message = f"{event_log} is not an event log: {exc}"
        raise LogCheckError(message) from exc
    return conn


def _scope(
    conn: sqlite3.Connection, runtime_root: Path, *, since_days: float, now_ms: int, examples: int,
) -> _Scope:
    head = conn.execute(_SQL_HEAD).fetchone()[0]
    ref_ms = now_ms if head is None else min(now_ms, int(head))
    since_ms = ref_ms - math.ceil(since_days * DAY_MS)
    start_id = conn.execute(_SQL_START_ID, (since_ms,)).fetchone()[0]
    events = int(conn.execute(_SQL_WINDOW_COUNT, (since_ms,)).fetchone()[0])
    return _Scope(conn, runtime_root, now_ms, ref_ms, since_ms, start_id, events, examples)


def _run_one(invariant: Invariant, scope: _Scope) -> InvariantResult:
    found = _Found(limit=scope.max_examples)
    invariant.check(scope, found)
    status: Status = "violated" if found.count else "skipped" if found.skipped else "ok"
    return InvariantResult(
        name=invariant.name,
        rule=invariant.rule,
        ref=invariant.ref,
        status=status,
        violations=found.count,
        examples=tuple(found.examples),
        detail=found.detail,
    )


def check_event_log(
    event_log: Path,
    runtime_root: Path,
    *,
    since_days: float = DEFAULT_SINCE_DAYS,
    now_ms: int | None = None,
    max_examples: int = DEFAULT_EXAMPLES,
) -> LogReport:
    """Run every invariant over the last ``since_days`` of ``event_log``.

    The window ends at the log's newest timestamp (or ``now_ms`` when that is earlier), so a copy
    is judged by its own last moment. ``runtime_root`` is where the recordings are looked for.

    Raises:
        LogCheckError: The log cannot be opened or is not an event log.
        ValueError: ``since_days`` is not a number of days between 0 and ``MAX_SINCE_DAYS``.
    """
    if not 0 < since_days <= MAX_SINCE_DAYS:
        message = f"since_days must be above 0 and at most {MAX_SINCE_DAYS:g}"
        raise ValueError(message)
    clock = int(time.time() * 1000) if now_ms is None else now_ms
    conn = open_read_only(event_log)
    try:
        try:
            scope = _scope(
                conn, runtime_root, since_days=since_days, now_ms=clock, examples=max_examples,
            )
            results = tuple(_run_one(one, scope) for one in INVARIANTS)
        except sqlite3.Error as exc:
            message = f"cannot read {event_log}: {exc}"
            raise LogCheckError(message) from exc
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return LogReport(
        event_log=str(event_log),
        checked_at_ms=clock,
        since_ms=scope.since_ms,
        until_ms=scope.ref_ms,
        events=scope.events,
        results=results,
    )


__all__ = [
    "ALL_QUERIES",
    "EVENT_LOG_FILENAME",
    "INVARIANTS",
    "SECRET_SHAPE_NAMES",
    "Example",
    "Invariant",
    "InvariantResult",
    "LogCheckError",
    "LogReport",
    "check_event_log",
    "iso",
    "open_read_only",
]
