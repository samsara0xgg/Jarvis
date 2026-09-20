"""Flat Tool adapters for local daily-loop state; dispatch owns action lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state import daily_activity, daily_records, daily_store
from jarvis.state.daily_contract import SCHEMAS, DailyError, encoded, validate

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jarvis.execution.tools import FlatHandler, ToolContext

_DESCRIPTIONS = {
    "search_records": (
        "Search past conversations by keyword/time [from,to). Returns identified excerpts, "
        "not full text. Follow next_cursor with identical arguments for every page; "
        "read_records retrieves originals."
    ),
    "read_records": (
        "Read exact original conversation text by record_ids from search_records. Returns "
        "chunks with offsets; follow next_cursor with the same IDs until null. Reports "
        "missing IDs."
    ),
    "query_activity": (
        "Query saved activity in [from,to): Git by observed time, TimeSink app/window/Chrome "
        "spans and screen captures (OCR text of the front window, summarized) by overlap. "
        "For 'what was I doing at <time>' use sources=['app','screen'], then read_activity on "
        "a screen item for its full text. App durations are estimates, not attention. agent is "
        "not_implemented. project is a full repo path and excludes unmapped app/screen rows. "
        "Check coverage, state_at_start and state_events (idle/lock/sleep/pause) before calling a "
        "gap rest; state events page with the items, so follow next_cursor to the end. "
        "Follow next_cursor with identical filters; invalid_cursor means a mutable TimeSink "
        "row changed: restart the query."
    ),
    "read_activity": (
        "Read a saved activity's original content in chunks by activity_id: JSON for Git and "
        "app spans, the full OCR text for screen captures. Does not take a screenshot or "
        "inspect today's diff. Follow next_cursor until null. TimeSink IDs pin a revision; "
        "source_changed requires a fresh query. For saving evidence, copy the returned "
        "source_refs, not the activity_id."
    ),
    "search_knowledge": (
        "Find saved reusable facts, decisions, lessons and preferences. Defaults to active; "
        "status=all includes deprecated and needs_confirmation. Returns full statements, "
        "versions and source refs."
    ),
    "save_knowledge": (
        "Save a sourced knowledge item, not a current activity state. source_refs must be "
        "record:<id>, event:<uid>, or timesink:<database>:<id>:<revision> returned by tools. "
        "TimeSink refs must still match when saved. basis describes origin, not "
        "verification. To correct/deprecate supply knowledge_id and expected_version, "
        "preserving history. Conflicting uncertain claims should use needs_confirmation. "
        "Reuse request_id only for exact retries."
    ),
    "create_todo": (
        "Create a LOCAL personal commitment, distinct from a memo or Codex execution. "
        "Optional due_at requires an ISO timestamp with UTC offset. No reminder or Microsoft "
        "sync is created. Choose a unique request_id; reuse it for exact retries only."
    ),
    "list_todos": (
        "List LOCAL personal todos, default status=open; status=all includes done/cancelled. "
        "due_before is exclusive and requires an offset. Returns IDs and versions for "
        "update_todo."
    ),
    "update_todo": (
        "Edit a local todo with its latest expected_version. patch may change title, project, "
        "due_at (null clears), priority or status (open/done/cancelled). Only mark done on "
        "the user's instruction, not an agent completion report. Reuse request_id for exact "
        "retries."
    ),
    "save_briefing": (
        "Save a daily briefing with source_refs and explicit coverage gaps. Copy source_refs "
        "from activity results exactly; activity:... IDs are NOT valid source refs. "
        "No notification, "
        "speech or scheduling. local_date is YYYY-MM-DD; timezone is an IANA name. Use "
        "expected_version to revise an existing date. Reuse request_id for exact retries."
    ),
    "get_briefing": (
        "Read a previously saved briefing by local_date and IANA timezone. Follow next_cursor "
        "with identical date/timezone for all content. Does not regenerate or deliver it."
    ),
}
_RESULT_CAP = 16384
_WRITES = frozenset({"save_knowledge", "create_todo", "update_todo", "save_briefing"})


def _read(  # noqa: PLR0913 — request context and independent configured source stores.
    name: str,
    values: dict[str, Any],
    ctx: ToolContext,
    memory_path: Path | None,
    repos: tuple[str, ...],
    timesink_path: Path | None,
) -> dict[str, Any]:
    if name == "search_records":
        return daily_records.search_records(memory_path, values)
    if name == "read_records":
        return daily_records.read_records(memory_path, values)
    if name == "query_activity":
        return daily_activity.query_activity(ctx.conn, values, repos, timesink_path)
    if name == "read_activity":
        return daily_activity.read_activity(ctx.conn, values, timesink_path)
    if name == "get_briefing":
        return daily_store.get_briefing(ctx.conn, values)
    return daily_store.search_items(ctx.conn, name, values)


def _handler(
    name: str,
    memory_path: Path | None,
    repos: tuple[str, ...],
    timesink_path: Path | None,
) -> FlatHandler:
    def handle(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        values = dict(args)
        try:
            validate(values, SCHEMAS[name])
            if name in _WRITES:
                result = daily_store.write_revision(
                    ctx.conn, memory_path, name, values, ctx.action_id, timesink_path=timesink_path
                )
            else:
                result = _read(name, values, ctx, memory_path, repos, timesink_path)
        except DailyError as exc:
            raise ToolError(str(exc), code=exc.code) from exc
        # IDs, source refs and cursors must never be damaged by generic string clipping.
        if len(encoded(result)) > _RESULT_CAP:
            message = "Result metadata exceeds the output budget; narrow the request"
            raise ToolError(message, code="result_too_large")
        return result

    return handle


def build_daily_tools(
    memory_path: Path | None,
    *,
    repos: tuple[str, ...] = (),
    timesink_path: Path | None = None,
) -> tuple[Tool, ...]:
    """Bind store configuration; no connections or state are created at registration."""
    return tuple(
        Tool(
            name=name,
            description=description,
            input_schema=SCHEMAS[name],
            handler=_handler(name, memory_path, repos, timesink_path),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1" if name in _WRITES else "L0",
            read_only=name not in _WRITES,
            max_result_chars=_RESULT_CAP,
        )
        for name, description in _DESCRIPTIONS.items()
        if memory_path is not None or name not in {"search_records", "read_records"}
    )
