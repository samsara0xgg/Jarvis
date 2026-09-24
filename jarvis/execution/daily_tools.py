"""Flat Tool adapters for local daily-loop state; dispatch owns action lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.shared.skills import load_skill
from jarvis.state import daily_activity, daily_records, daily_store
from jarvis.state.daily_contract import (
    ACTIVITY_PAGE_BUDGET,
    SCHEMAS,
    DailyError,
    encoded,
    validate,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jarvis.execution.tools import (
        DailyReportRun,
        FlatHandler,
        ToolContext,
        WorkStateRefresh,
    )

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
        "Saved activity in [from,to) as one compact table. Header: date, utc_offset, store, "
        "apps dictionary (A, B, ...), totals per app over the WHOLE window (foreground_s, spans, "
        "first, last from app spans; screen_rows from screen captures; only the sources queried "
        "appear), coverage and TimeSink state (idle/lock/sleep/pause). Rows: "
        "[ref, start, end, app, text] for Git observations (g<uid>), TimeSink app spans "
        "(s<id>:<rev>, front window title) and screen captures (c<id>:<rev>, window title — OCR "
        "excerpt). Filters: sources (git/app/screen), app (case-insensitive substring of app "
        "name or bundle id; reads app spans AND screen captures whatever sources says; when it "
        "matches nothing, coverage.<source>.apps_in_window lists "
        "the [name, bundle id] pairs seen in the window: retry with one of them), project "
        "(full repo path; excludes app/screen rows). "
        "summary_only=true returns header, totals and state without rows: use it for 'how long "
        "did I use X'. A page holds as many rows as its budget allows; follow next_cursor with "
        "identical arguments until null. cursor_mismatch = you changed the arguments, restart "
        "without cursor; invalid_cursor = rows changed, restart. read_activity(<ref>) returns a "
        "screen row's full OCR text. foreground_s is frontmost time with idle excluded, not "
        "attention or background playback; a screen row shows content was on screen, not that "
        "a message was sent. Gaps without a state event stay unknown."
    ),
    "read_activity": (
        "Read one saved row by a query_activity ref: s<id>:<rev> app span (JSON), c<id>:<rev> "
        "screen capture (full OCR text), g<uid> Git observation (JSON); full timesink:, "
        "timesink-capture:, activity:, git: and codex-session: references also work. Chunks "
        "with offset; follow next_cursor until null. Never takes a screenshot or inspects "
        "today's diff. source_changed means the row changed since the query: query again. The "
        "result's source_refs is the full reference to cite when saving."
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
# One activity page is the row budget plus its header (dictionary, totals, coverage, notes);
# the handler rejects anything larger instead of letting the dispatcher window strings.
_ACTIVITY_RESULT_CAP = ACTIVITY_PAGE_BUDGET + 16384
_RESULT_CAPS = {"query_activity": _ACTIVITY_RESULT_CAP}
_WRITES = frozenset({"save_knowledge", "save_briefing"})
_WORK_STATE_DESCRIPTION = (
    "Investigate and update Allen's persisted current work state. Call this when he asks what "
    "he is doing now, what he did today/recently, or how something discussed earlier is "
    "progressing. It reads the latest TimeSink app/window/screen data, recent conversation "
    "records, knowledge and Git observations, runs one analysis and saves the "
    "result; outcome=reused means nothing new was observed and the saved state still holds, "
    "no_evidence means there is no data, failed keeps the previous state. Pass the user's "
    "question verbatim and put facts he just stated into note (they count as new evidence). "
    "Every claim in the state carries basis stated/observed/inferred; never present an "
    "inferred item as fact, and never mark todos done from it. Use force only when asked to "
    "re-analyse. For more detail on one item use query_activity/read_activity/read_records."
)


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
        if len(encoded(result)) > _RESULT_CAPS.get(name, _RESULT_CAP):
            message = "Result metadata exceeds the output budget; narrow the request"
            raise ToolError(message, code="result_too_large")
        return result

    return handle


def build_work_state_tool(refresh: WorkStateRefresh | None) -> tuple[Tool, ...]:
    """ADR 0023: the conversation entry to the runtime's refresh workflow; absent when not wired."""
    if refresh is None:
        return ()

    def handle(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        values = dict(args)
        try:
            validate(values, SCHEMAS["refresh_work_state"])
            return refresh(values, ctx)
        except DailyError as exc:
            raise ToolError(str(exc), code=exc.code) from exc

    return (
        Tool(
            name="refresh_work_state",
            description=_WORK_STATE_DESCRIPTION,
            input_schema=SCHEMAS["refresh_work_state"],
            handler=handle,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
            max_result_chars=_RESULT_CAP,
        ),
    )


def build_daily_report_tool(run: DailyReportRun | None) -> tuple[Tool, ...]:
    """ADR 0024: the conversation entry to the runtime's daily-report workflow.

    The tool's description is the skill's own frontmatter description, so the
    trigger text the decision model reads and the instructions the report model
    follows cannot drift apart.
    """
    if run is None:
        return ()

    def handle(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        values = dict(args)
        try:
            validate(values, SCHEMAS["daily_work_report"])
            return run(values, ctx)
        except DailyError as exc:
            raise ToolError(str(exc), code=exc.code) from exc

    return (
        Tool(
            name="daily_work_report",
            description=load_skill("daily-work-report").description,
            input_schema=SCHEMAS["daily_work_report"],
            handler=handle,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
            max_result_chars=_RESULT_CAP,
        ),
    )


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
            max_result_chars=_RESULT_CAPS.get(name, _RESULT_CAP),
        )
        for name, description in _DESCRIPTIONS.items()
        if memory_path is not None or name not in {"search_records", "read_records"}
    )
