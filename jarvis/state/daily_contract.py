"""Validated inputs and bounded, resumable results for the daily-loop tools."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from typing import Any

PAGE_BUDGET = 11000
MAX_TEXT_JSON_CHARS = 4000
_CURSOR_PARTS = 2
DETAIL_CHARS = 3500


class DailyError(Exception):
    """An expected input, reference or revision failure."""

    def __init__(self, message: str, code: str = "invalid_argument") -> None:
        """Carry a stable tool-facing error code."""
        self.code = code
        super().__init__(message)


def text_field(maximum: int = 200, *, nullable: bool = False) -> dict[str, Any]:
    """Describe bounded text, optionally cleared with null."""
    return {
        "type": ["string", "null"] if nullable else "string",
        "maxLength": maximum,
        "minLength": 1,
    }


def enum_field(*values: str) -> dict[str, Any]:
    """Describe a closed vocabulary."""
    return {"type": "string", "enum": list(values)}


def object_fields(fields: dict[str, Any], *required: str) -> dict[str, Any]:
    """Describe a closed object."""
    return {
        "type": "object",
        "properties": fields,
        "required": list(required),
        "additionalProperties": False,
    }


_CURSOR = text_field(4096)
_PAGE = {"limit": {"type": "integer", "minimum": 1, "maximum": 50}, "cursor": _CURSOR}
_VERSION = {"type": "integer", "minimum": 0}
_REFS = {
    "type": "array",
    "items": text_field(240),
    "maxItems": 20,
    "uniqueItems": True,
    "description": (
        "Evidence references: record:<record_id>, event:<event_uid>, or the exact timesink: / "
        "timesink-capture: reference returned by a tool. For activity evidence, copy values "
        "from source_refs, NOT the activity id (activity:... is a lookup ID, not a source "
        "reference)."
    ),
}
_PROJECT = text_field(512, nullable=True)
_TODO_FIELDS = {
    "title": text_field(500),
    "project": _PROJECT,
    "due_at": text_field(50, nullable=True),
    "priority": enum_field("low", "normal", "high"),
    "status": enum_field("open", "done", "cancelled"),
}
_COVERAGE = object_fields(
    {
        name: enum_field("available", "partial", "unknown", "unavailable", "not_implemented")
        for name in ("records", "git", "app", "screen", "agent", "todos", "knowledge")
    }
)

SCHEMAS: dict[str, dict[str, Any]] = {
    "search_records": object_fields(
        {"keyword": text_field(500), "from": text_field(50), "to": text_field(50), **_PAGE}
    ),
    "read_records": object_fields(
        {
            "record_ids": {
                "type": "array",
                "items": text_field(200),
                "minItems": 1,
                "maxItems": 20,
                "uniqueItems": True,
            },
            "cursor": _CURSOR,
        },
        "record_ids",
    ),
    "query_activity": object_fields(
        {
            "from": text_field(50),
            "to": text_field(50),
            "project": text_field(512),
            "sources": {
                "type": "array",
                "items": enum_field("git", "app", "screen", "agent"),
                "minItems": 1,
                "maxItems": 4,
                "uniqueItems": True,
            },
            **_PAGE,
        },
        "from",
        "to",
    ),
    "read_activity": object_fields(
        {"activity_id": text_field(200), "cursor": _CURSOR}, "activity_id"
    ),
    "refresh_work_state": object_fields(
        {
            "question": text_field(500),
            "note": text_field(2000),
            "force": {"type": "boolean"},
        }
    ),
    "daily_work_report": object_fields(
        {
            "local_date": text_field(10),
            "timezone": text_field(100),
            "regenerate": {"type": "boolean"},
        }
    ),
    "search_knowledge": object_fields(
        {
            "query": text_field(500),
            "project": text_field(512),
            "kind": enum_field("fact", "decision", "lesson", "preference"),
            "status": enum_field("active", "deprecated", "needs_confirmation", "all"),
            **_PAGE,
        }
    ),
    "save_knowledge": object_fields(
        {
            "knowledge_id": text_field(200),
            "statement": text_field(4000),
            "kind": enum_field("fact", "decision", "lesson", "preference"),
            "project": _PROJECT,
            "source_refs": {**_REFS, "minItems": 1},
            "basis": enum_field("user_statement", "observation", "inference"),
            "status": enum_field("active", "deprecated", "needs_confirmation"),
            "expected_version": _VERSION,
            "request_id": text_field(200),
        },
        "statement",
        "kind",
        "source_refs",
        "basis",
        "request_id",
    ),
    "create_todo": object_fields(
        {
            **{k: v for k, v in _TODO_FIELDS.items() if k != "status"},
            "source_refs": _REFS,
            "request_id": text_field(200),
        },
        "title",
        "request_id",
    ),
    "list_todos": object_fields(
        {
            "status": enum_field("open", "done", "cancelled", "all"),
            "project": text_field(512),
            "due_before": text_field(50),
            **_PAGE,
        }
    ),
    "update_todo": object_fields(
        {
            "todo_id": text_field(200),
            "expected_version": _VERSION,
            "patch": {**object_fields(_TODO_FIELDS), "minProperties": 1},
            "request_id": text_field(200),
        },
        "todo_id",
        "expected_version",
        "patch",
        "request_id",
    ),
    "save_briefing": object_fields(
        {
            "local_date": text_field(10),
            "timezone": text_field(100),
            "content": text_field(16000),
            "source_refs": _REFS,
            "coverage": _COVERAGE,
            "expected_version": _VERSION,
            "request_id": text_field(200),
        },
        "local_date",
        "timezone",
        "content",
        "source_refs",
        "coverage",
        "request_id",
    ),
    "get_briefing": object_fields(
        {"local_date": text_field(10), "timezone": text_field(100), "cursor": _CURSOR},
        "local_date",
        "timezone",
    ),
}


def validate(value: Any, schema: dict[str, Any], path: str = "args") -> None:  # noqa: ANN401 — JSON input is validated recursively here.
    """Enforce the subset of JSON Schema used by this fixed tool catalog."""
    kind = schema["type"]
    if value is None and isinstance(kind, list) and "null" in kind:
        return
    kind = "string" if isinstance(kind, list) else kind
    checks = {
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }
    if not checks[kind]:
        msg = f"{path}: expected {kind}"
        raise DailyError(msg)
    if "enum" in schema and value not in schema["enum"]:
        msg = f"{path}: must be one of {schema['enum']}"
        raise DailyError(msg)
    if kind == "boolean":
        return
    if kind == "string":
        if not value.strip() or len(value) > schema.get("maxLength", 200):
            msg = f"{path}: empty or over length limit"
            raise DailyError(msg)
    elif kind == "integer":
        if not schema.get("minimum", 0) <= value <= schema.get("maximum", 2**63 - 1):
            msg = f"{path}: out of range"
            raise DailyError(msg)
    else:
        _validate_container(value, schema, path, kind)


def _validate_container(value: Any, schema: dict[str, Any], path: str, kind: str) -> None:  # noqa: ANN401 — recursive JSON container validation.
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema["maxItems"]:
            msg = f"{path}: invalid list size"
            raise DailyError(msg)
        for entry in value:
            validate(entry, schema["items"], path)
        if schema.get("uniqueItems") and len(set(value)) != len(value):
            msg = f"{path}: duplicate entries"
            raise DailyError(msg)
    else:
        if set(value) - schema["properties"].keys() or set(schema["required"]) - value.keys():
            msg = f"{path}: unknown or missing fields"
            raise DailyError(msg)
        if len(value) < schema.get("minProperties", 0):
            msg = f"{path}: empty object"
            raise DailyError(msg)
        for key, entry in value.items():
            validate(entry, schema["properties"][key], f"{path}.{key}")


def encoded(value: Any) -> str:  # noqa: ANN401 — JSON payload serializer.
    """Serialize without ASCII expansion, including stable fingerprint order."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def fingerprint(value: Any) -> str:  # noqa: ANN401 — arbitrary validated JSON.
    """Bind a request or cursor to its complete arguments."""
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def timestamp(value: str) -> datetime:
    """Require an explicit timezone; never guess a date's local offset."""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is not None:
            return parsed
    except ValueError:
        pass
    msg = "Timestamp must be ISO 8601 with UTC offset"
    raise DailyError(msg)


def window(args: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    """Validate an optional half-open date range."""
    start = timestamp(args["from"]) if "from" in args else None
    end = timestamp(args["to"]) if "to" in args else None
    if start and end and start >= end:
        msg = "from must precede to"
        raise DailyError(msg)
    return start, end


def make_cursor(binding: str, position: list[int]) -> str:
    """Encode a resumable position tied to a query and its database."""
    return base64.urlsafe_b64encode(encoded([binding, position]).encode()).decode()


def cursor_position(raw: str | None, binding: str, initial: list[int]) -> list[int]:
    """Reject malformed or cross-query cursors before reading any rows."""
    if raw is None:
        return initial
    try:
        token = json.loads(base64.b64decode(raw, altchars=b"-_", validate=True))
        if (
            isinstance(token, list)
            and len(token) == _CURSOR_PARTS
            and token[0] == binding
            and isinstance(token[1], list)
            and len(token[1]) == len(initial)
            and all(type(x) is int and 0 <= x < 2**63 for x in token[1])
        ):
            return list(token[1])
    except (ValueError, UnicodeError):
        pass
    msg = "Invalid cursor or changed query"
    raise DailyError(msg, "invalid_cursor")


def page_rows(  # noqa: PLR0913 — page query, binding and its persisted position travel together.
    rows: list[dict[str, Any]],
    args: dict[str, Any],
    binding: str,
    snapshot: int,
    offset: int,
    *,
    key: str = "items",
) -> dict[str, Any]:
    """Return whole rows within a budget; never silently cut a record in half."""
    selected = fit(rows[offset : offset + args.get("limit", 20)], PAGE_BUDGET)
    if not selected and offset < len(rows):
        msg = "Record exceeds page budget; use its detail reader"
        raise DailyError(msg, "result_too_large")
    end = offset + len(selected)
    return {
        key: selected,
        "count": len(selected),
        "snapshot": snapshot,
        "next_cursor": make_cursor(binding, [snapshot, end]) if end < len(rows) else None,
    }


def fit(rows: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """Take whole leading rows while their JSON encoding stays within budget."""
    selected: list[dict[str, Any]] = []
    for row in rows:
        if len(encoded([*selected, row])) > budget:
            break
        selected.append(row)
    return selected


def text_chunk(content: str, offset: int) -> tuple[str, int]:
    """Bound JSON-encoded text without discarding its continuation."""
    if offset > len(content):
        msg = "Text cursor out of range"
        raise DailyError(msg, "invalid_cursor")
    chunk = content[offset : offset + DETAIL_CHARS]
    while len(encoded(chunk)) > MAX_TEXT_JSON_CHARS:
        chunk = chunk[: len(chunk) // 2]
    return chunk, offset + len(chunk)
