"""H1 — no direct INSERT/UPDATE/DELETE outside L2-owned tables.

Per ADR 0001 § Acceptance criterion H1:

> AST scan of ``jarvis/`` for direct INSERT/UPDATE/DELETE on projection
> tables. Canonical events may only be inserted by
> ``jarvis/state/event_log.py``. Bounded operational idempotency/outbox
> tables may only be inserted by their explicit L2 owner modules.

Implementation: walk every ``.py`` under ``jarvis/`` with ``ast.parse``;
for each :class:`ast.Constant` (string) in the module, regex-scan for
SQL write statements. Trigger DDL strings inside ``event_log.py`` that
contain ``RAISE(ABORT, ...)`` are NOT INSERT/UPDATE/DELETE statements
per the canary (they declare the trigger that raises if anyone tries
one); we special-case strings that contain ``RAISE(ABORT,`` and exempt
them from the UPDATE/DELETE match.
"""

from __future__ import annotations

import ast
import re

from tests.canary._helpers import iter_jarvis_py_files, parse, relative_to_repo

# Three alternations; named groups capture the table name from whichever
# alternative matches. Case-insensitive so `insert into events` lower-case
# cannot bypass H1. Each form is also tightened with a SQL-syntax tail so
# English prose like "INSERT into the events table" does not false-trip:
#
# - INSERT INTO <table> must be followed by `(` / VALUES / SELECT /
#   DEFAULT / SET, or be at end-of-string (a bare fragment like
#   `INSERT INTO events` as the entire literal still counts).
# - UPDATE <table> SET is already structural — `\s+SET\b` is not a
#   common English bigram.
# - DELETE FROM <table> must be followed by WHERE / ORDER / LIMIT /
#   RETURNING / `;` / end-of-string / `)`.
_SQL_WRITE_RE = re.compile(
    r"\bINSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+(?P<insert_table>\w+)\s*"
    r"(?:\(|VALUES\b|SELECT\b|DEFAULT\b|SET\b|$)"
    r"|\bUPDATE\s+(?P<update_table>\w+)\s+SET\b"
    r"|\bDELETE\s+FROM\s+(?P<delete_table>\w+)\s*"
    r"(?:WHERE\b|ORDER\b|LIMIT\b|RETURNING\b|;|\)|$)",
    re.IGNORECASE,
)


_L2_OPERATIONAL_INSERTS: dict[str, frozenset[str]] = {
    "jarvis/state/authorized_dispatch_outbox.py": frozenset(
        {"confirmation_consumption_claims", "authorized_dispatch_outbox"},
    ),
    "jarvis/state/cost_accounting.py": frozenset({"cost_accounting_dispositions"}),
    # ADR-0014 D6: the log's own lineage row, written once per file by
    # `_assign_log_epoch_once`. Operational, not projection truth.
    "jarvis/state/event_log.py": frozenset({"event_log_metadata"}),
    "jarvis/state/trigger_consumption.py": frozenset({"decision_trigger_consumptions"}),
}


def _allowed_insert(rel_path: str, table: str) -> bool:
    """Allow canonical events plus explicitly-owned L2 idempotency debt."""
    normalized = table.lower()
    if rel_path == "jarvis/state/event_log.py" and normalized == "events":
        return True
    return normalized in _L2_OPERATIONAL_INSERTS.get(rel_path, frozenset())


# One-shot schema-migration backfills are the single sanctioned UPDATE:
# they run inside `_migrate_schema_v0_to_v1`'s EXCLUSIVE transaction with
# the `events_no_update` trigger dropped, and every such literal must be
# tagged with this marker AND live in event_log.py. Anything else that
# says UPDATE — including an untagged migration in any other file — still
# trips H1.
_MIGRATION_MARKER = "/* L2 schema migration"


def _allowed_migration_update(rel_path: str, source: str, table: str) -> bool:
    """Allow tagged migrations and the L2 outbox's one-way admission CAS."""
    if rel_path == "jarvis/state/authorized_dispatch_outbox.py":
        return (
            table.lower() == "authorized_dispatch_outbox"
            and source == "UPDATE authorized_dispatch_outbox SET state = 'dispatched' "
            "WHERE dispatch_id = ? AND state = 'pending'"
        )
    return (
        rel_path == "jarvis/state/event_log.py"
        and table.lower() == "events"
        and source.lstrip().startswith(_MIGRATION_MARKER)
    )


def _is_trigger_ddl(source: str) -> bool:
    """True iff this string literal looks like SQLite trigger DDL.

    The append-only triggers in ``event_log.py`` contain the words
    ``UPDATE`` / ``DELETE`` inside ``BEFORE UPDATE ON events`` clauses
    plus a ``RAISE(ABORT, ...)`` body. They are NOT INSERT/UPDATE/DELETE
    statements themselves — they declare guards that abort anyone who
    tries to perform such a statement. Recognize this pattern and
    exempt it.
    """
    return "RAISE(ABORT" in source.upper().replace(" ", "")


def _classify(match: re.Match[str]) -> tuple[str, str] | None:
    """Return ``(kind, table)`` for a regex hit, or None if no group matched."""
    if match.group("insert_table") is not None:
        return ("INSERT", match.group("insert_table"))
    if match.group("update_table") is not None:
        return ("UPDATE", match.group("update_table"))
    if match.group("delete_table") is not None:
        return ("DELETE", match.group("delete_table"))
    return None


def _format_violation(rel: str, line_no: int, kind: str, table: str) -> str:
    """Format one violation message without SQL-looking f-strings."""
    if kind == "INSERT":
        suffix = "outside allowlist (only event_log may write to events)"
    elif kind == "UPDATE":
        suffix = "is forbidden (projection tables are read-only)"
    else:
        suffix = "is forbidden (events is append-only)"
    return f"{rel}:{line_no}: {kind} on {table!r} " + suffix


def test_no_projection_writes() -> None:
    """Fail if any ``jarvis/*.py`` file embeds an SQL write outside the allowlist."""
    violations: list[str] = []

    for path in iter_jarvis_py_files():
        rel = relative_to_repo(path)
        module = parse(path)
        for node in ast.walk(module):
            if not isinstance(node, ast.Constant):
                continue
            value = node.value
            if not isinstance(value, str):
                continue

            # Strategy (a) from H1: strings whose body is a trigger DDL
            # (RAISE(ABORT, ...)) are not INSERT/UPDATE/DELETE statements
            # per the canary; skip them. They live in event_log.py.
            if _is_trigger_ddl(value):
                continue

            for match in _SQL_WRITE_RE.finditer(value):
                classified = _classify(match)
                if classified is None:
                    continue
                kind, table = classified
                if kind == "INSERT" and _allowed_insert(rel, table):
                    continue
                if kind == "UPDATE" and _allowed_migration_update(rel, value, table):
                    continue
                violations.append(
                    _format_violation(rel, getattr(node, "lineno", 0), kind, table)
                )

    assert not violations, (
        "H1 — direct projection writes detected:\n  " + "\n  ".join(violations)
    )
