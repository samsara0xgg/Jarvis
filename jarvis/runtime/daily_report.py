"""The daily work report workflow (ADR 0028): the skill's procedure, owned by the runtime.

Composition-root code: it wires L2 day evidence + the briefing store, L3
request/rule/check/compose and a preset-bound analyst into one job. The
daemon runs it once a day for the day before (ADR 0101). Three steps: the model drafts
the report (querying the day first if it wants), the program rules on every
completion claim and asks the model to check the rest against the cited
originals, and only then is the summary written from the checked table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from jarvis.decision.daily_report import (
    DETAILS_TOOL,
    DETAILS_TOOL_NAME,
    JUDGE_TOOL,
    QUERY_MORE,
    REPORT_AGAIN,
    REPORT_NOW,
    REPORT_TOOL,
    REPORT_TOOL_NAME,
    SEARCH_TOOL,
    SUMMARY_TOOL,
    DailyReportParseError,
    build_check_request,
    build_request,
    build_summary_request,
    compose_report,
    parse_main_line,
    parse_report,
    parse_verdicts,
    requested_queries,
    screen_claims,
    summary_of,
    summary_table,
    title_terms,
)
from jarvis.shared.lang import t
from jarvis.state.daily_contract import DailyError
from jarvis.state.daily_report import (
    claim_originals,
    existing_report,
    gather_day,
    read_detail,
    resolve_day,
    resolve_zone,
    save_report,
    search_day,
)
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence
    from concurrent.futures import Future
    from datetime import tzinfo

    from jarvis.decision.daily_report import Claim
    from jarvis.decision.llm import ChatResult
    from jarvis.execution.mcp_tools import McpServers
    from jarvis.shared.device_link import DeviceLink
    from jarvis.state.daily_report import DayEvidence

    type PlanReader = Callable[[datetime, datetime], dict[str, Any]]

LOGGER = logging.getLogger(__name__)
OUTCOMES = ("generated", "reused", "no_evidence", "failed")
KIND = "daily_report"
QUERY_ROUNDS = 3
"""Rounds in which the model may search the day or ask for originals before it must report."""
MAX_ROUNDS = 5
"""Drafting calls per report: the query rounds, the report, and one retry of an unusable one."""
CHECK_BUDGET = 12
"""Verification calls per report: one per item holding a claim the program could not rule on."""
PLAN_SERVER = "microsoft"
"""ADR 0036: the configured MCP server whose calendar and To Do the report reads."""
_EVENT_FIELDS = "subject,start,end,isAllDay,isCancelled,location"


def _graph(servers: McpServers, tool: str, args: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = servers.call(PLAN_SERVER, tool, args)
    # The server returns Graph's JSON as a text block, not as structured content.
    body = json.loads(payload["text"]) if "text" in payload else payload
    return list(body.get("value", []))


def _utc(stamp: Mapping[str, Any]) -> str:
    """Graph's ``{dateTime, timeZone}``; calendarView answers in UTC unless asked otherwise."""
    return datetime.fromisoformat(str(stamp["dateTime"])).replace(tzinfo=UTC).isoformat()


def _date_of(stamp: Mapping[str, Any] | None) -> str | None:
    """To Do keeps due and completion as a date at midnight, not an instant."""
    return str(stamp["dateTime"])[:10] if stamp else None


def microsoft_plan(servers: McpServers, start: datetime, end: datetime) -> dict[str, Any]:
    """Calendar events in ``[start, end)`` and every To Do task, through read-only tools."""
    view = {
        "startDateTime": start.isoformat(),
        "endDateTime": end.isoformat(),
        "select": _EVENT_FIELDS,
        "fetchAllPages": True,
    }
    events = []
    for event in _graph(servers, "get-calendar-view", view):
        if event.get("isCancelled"):
            continue
        row: dict[str, Any] = {
            "subject": str(event.get("subject") or ""),
            "location": str((event.get("location") or {}).get("displayName") or ""),
            "all_day": bool(event.get("isAllDay")),
        }
        if row["all_day"]:
            row["date"] = str(event["start"]["dateTime"])[:10]
        else:
            row.update(start=_utc(event["start"]), end=_utc(event["end"]))
        events.append(row)
    todos = []
    for one in _graph(servers, "list-todo-task-lists", {"fetchAllPages": True}):
        # No $select here: Graph answers 400 (RequestBroker--ParseUri) for To Do tasks.
        tasks = {"todoTaskListId": one["id"], "fetchAllPages": True}
        todos += [
            {
                "title": str(task.get("title") or ""),
                "list": str(one.get("displayName") or ""),
                "done": task.get("status") == "completed",
                "important": task.get("importance") == "high",
                "due": _date_of(task.get("dueDateTime")),
                "completed": _date_of(task.get("completedDateTime")),
            }
            for task in _graph(servers, "list-todo-tasks", tasks)
        ]
    return {"events": events, "todos": todos}


class Reporter(Protocol):
    """One bounded model call with a given tool catalog; tests substitute a canned reply."""

    def analyze(
        self,
        conn: sqlite3.Connection,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        tool_choice: str,
    ) -> ChatResult:
        """Send the request through the given log connection's cost accounting."""
        ...


def _record_verdicts(index: int, pending: list[Claim], future: Future[ChatResult]) -> None:
    """One item's verdicts from its finished call; a failed or malformed one leaves its claims."""
    try:
        parse_verdicts(future.result(), pending)
    except DailyReportParseError as exc:
        LOGGER.warning("daily_report: item %s check unusable: %s", index, exc)
    except Exception as exc:  # noqa: BLE001 — the provider failed; nothing is assumed.
        LOGGER.warning("daily_report: item %s check failed: %s", index, exc)
    for claim in pending:
        if claim.verdict is None:
            claim.unchecked = t("report.unchecked.failed")


class DailyReportService:
    """Generate, reuse or refuse one day's report; every outcome is explicit."""

    def __init__(  # noqa: PLR0913 — the configured stores, the analyst and the local zone.
        self,
        *,
        memory_path: Path | None,
        timesink_path: Path | None,
        repos: Sequence[str],
        reporter: Reporter | None,
        model: str,
        tz: tzinfo | None = None,
        codex_sessions_path: Path | None = None,
        check_budget: int = CHECK_BUDGET,
        device: DeviceLink | None = None,
    ) -> None:
        """Bind store locations; nothing is opened until a run.

        ``device`` (ADR 0170: this is a brain) is the link to the terminal that holds TimeSink
        and the repositories; the day's reads of them are asked of it.
        """
        self._memory_path = memory_path
        self._timesink_path = timesink_path
        self._codex_sessions_path = codex_sessions_path
        self._repos = tuple(repos)
        self._reporter = reporter
        self._model = model
        self._tz = tz
        self._check_budget = check_budget
        self._device = device
        # ponytail: one lock for all days; per-day locks only if two reports must run at once.
        self._lock = threading.Lock()
        self.plan_reader: PlanReader | None = None
        """Set by the runtime once MCP servers are up (ADR 0036); None means not wired."""

    def saved(
        self, conn: sqlite3.Connection, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """Yesterday's saved report as a reused run returns it; None when there is none."""
        zone_name, zone = resolve_zone(None, self._tz)
        day = resolve_day(None, zone, now or datetime.now(UTC))
        existing = existing_report(conn, day.isoformat(), zone_name)
        if existing is None:
            return None
        return _reused({"local_date": day.isoformat(), "timezone": zone_name}, existing)

    def run(  # noqa: PLR0913 — one keyword per request field plus the writer and the clock.
        self,
        conn: sqlite3.Connection,
        *,
        local_date: str | None = None,
        timezone: str | None = None,
        regenerate: bool = False,
        action_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Reuse a saved report, or gather the day, call the model and save the next version."""
        moment = now or datetime.now(UTC)
        zone_name, zone = resolve_zone(timezone, self._tz)
        day = resolve_day(local_date, zone, moment)
        base: dict[str, Any] = {"local_date": day.isoformat(), "timezone": zone_name}
        with self._lock:
            existing = existing_report(conn, day.isoformat(), zone_name)
            if existing is not None and not regenerate:
                return _reused(base, existing)
            version = existing["version"] if existing else 0
            try:
                return self._generate(
                    conn, base=base, day=day, zone_name=zone_name, zone=zone,
                    version=version, action_id=action_id, moment=moment,
                )
            except (DailyError, DailyReportParseError) as exc:
                LOGGER.warning("daily_report: %s kept version %s: %s", day, version, exc)
                return {**base, "outcome": "failed", "version": version, "error": str(exc)[:300]}
            except Exception as exc:  # noqa: BLE001 — provider failures keep the saved version.
                LOGGER.warning("daily_report: %s failed: %s: %s", day, type(exc).__name__, exc)
                return {
                    **base,
                    "outcome": "failed",
                    "version": version,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }

    def _generate(  # noqa: PLR0913 — the resolved request, its writer and its clock.
        self,
        conn: sqlite3.Connection,
        *,
        base: dict[str, Any],
        day: date,
        zone_name: str,
        zone: tzinfo,
        version: int,
        action_id: str,
        moment: datetime,
    ) -> dict[str, Any]:
        """Gather, draft, check, summarise, compose and save; a failure keeps the saved version."""
        evidence = gather_day(
            conn,
            memory_path=self._memory_path,
            timesink_path=self._timesink_path,
            repos=self._repos,
            day=day,
            zone_name=zone_name,
            zone=zone,
            now=moment,
            codex_sessions_path=self._codex_sessions_path,
            plan=self._plan(day, zone),
            device=self._device,
        )
        if evidence.empty:
            return {
                **base,
                "outcome": "no_evidence",
                "version": version,
                "window": evidence.window,
                "coverage": evidence.coverage,
                "served": evidence.served,
                "limits": evidence.limits,
                "device_gap": evidence.device_gap,
                "error": None,
            }
        if self._reporter is None:
            message = "report model is not configured"
            raise DailyError(message, "unavailable")
        report, drafted = self._draft(conn, evidence)
        claims = screen_claims(report, evidence)
        checks = self._check(conn, evidence, report, claims)
        main_line, summarised = self._summarise(conn, evidence, report, claims)
        content, refs, coverage = compose_report(
            report,
            evidence,
            claims,
            main_line=main_line,
            model=self._model,
            generated_at=moment.astimezone(zone),
        )
        receipt = save_report(
            conn,
            memory_path=self._memory_path,
            timesink_path=self._timesink_path,
            day=day.isoformat(),
            zone=zone_name,
            content=content,
            source_refs=refs,
            coverage=coverage,
            expected_version=version,
            action_id=action_id,
            device=self._device,
        )
        return {
            **base,
            "outcome": "generated",
            "briefing_id": receipt["briefing_id"],
            "version": receipt["version"],
            "source_ref": receipt["source_ref"],
            "saved_at": moment.astimezone(zone).isoformat(timespec="seconds"),
            "coverage": coverage,
            "total_chars": len(content),
            "source_refs_saved": len(refs),
            "evidence_counts": evidence.counts,
            "limits": evidence.limits,
            "device_gap": evidence.device_gap,
            "summary": summary_of(content),
            "model_calls": drafted + checks + summarised,
            "checks": checks,
            "error": None,
        }

    def _plan(self, day: date, zone: tzinfo) -> dict[str, Any] | None:
        """This day's and the next day's calendar plus To Do; a failed read is stated, not fatal."""
        if self.plan_reader is None:
            return None
        start = datetime.combine(day, time(), zone)
        end = datetime.combine(day + timedelta(days=2), time(), zone)
        try:
            return self.plan_reader(start, end)
        except Exception as exc:  # noqa: BLE001 — an unreadable plan never blocks the report.
            LOGGER.warning("daily_report: plan unreadable: %s: %s", type(exc).__name__, exc)
            return {"error": f"{type(exc).__name__}: {exc}"[:200]}

    def _draft(self, conn: sqlite3.Connection, evidence: DayEvidence) -> tuple[dict[str, Any], int]:
        """Serve queries, require the draft, retry an unusable one; bounded by ``MAX_ROUNDS``.

        A model that asks for details instead of reporting is answered with its
        material and told to report. A reply that does not parse — the provider
        malforms or truncates long arguments now and then — is answered with
        that error and asked again. The last round's failure ends the run.
        """
        assert self._reporter is not None  # noqa: S101 — checked by the caller.
        system, messages = build_request(evidence)
        # One catalog for every round: the tools lead the cached prompt prefix, so dropping the
        # query tools re-bills the whole day's material. Report-only rounds force the call instead.
        tools: list[dict[str, Any]] = [SEARCH_TOOL, DETAILS_TOOL, REPORT_TOOL]
        choice = "auto"
        for round_number in range(1, MAX_ROUNDS + 1):
            result = self._reporter.analyze(
                conn, system=system, messages=messages, tools=tools, tool_choice=choice
            )
            last = round_number == MAX_ROUNDS
            queries = requested_queries(result)
            if queries and round_number <= QUERY_ROUNDS:
                messages.append(_assistant_turn(result))
                for call_id, name, argument in queries:
                    reply = self._answer(conn, evidence, name, argument)
                    messages.append(_tool_reply(call_id, reply))
                if round_number == QUERY_ROUNDS:
                    messages.append({"role": "user", "content": REPORT_NOW})
                    choice = REPORT_TOOL_NAME
                else:
                    messages.append({"role": "user", "content": QUERY_MORE})
                continue
            try:
                return parse_report(result), round_number
            except DailyReportParseError as exc:
                message = f"{exc} (round {round_number}, finish_reason={result.finish_reason})"
                if last:
                    raise DailyReportParseError(message) from exc
                LOGGER.warning("daily_report: retrying after an unusable reply: %s", message)
                messages.append(_assistant_turn(result))
                # Every tool call must be answered before the next instruction.
                messages += [_tool_reply(call.call_id, str(exc)) for call in result.tool_calls]
                messages.append({"role": "user", "content": REPORT_AGAIN})
                choice = REPORT_TOOL_NAME
        message = "report_daily_work was never produced"
        raise DailyReportParseError(message)

    def _check(
        self,
        conn: sqlite3.Connection,
        evidence: DayEvidence,
        report: dict[str, Any],
        claims: list[Claim],
    ) -> int:
        """One verification call per item with claims the program could not rule on, in order.

        The call sees the item, its claimed parts and the cited originals, and
        nothing else. Items past the budget stay unverified — never assumed.
        The calls run together, so a failed call (or a malformed verdict)
        leaves only its own item's claims unverified.
        """
        assert self._reporter is not None  # noqa: S101 — checked by the caller.
        jobs: list[tuple[int, list[Claim], str, list[dict[str, Any]]]] = []
        for index, item in enumerate(report["items"], 1):
            pending = [c for c in claims if c.item == index and c.needs_check]
            if not pending:
                continue
            if len(jobs) >= self._check_budget:
                for claim in pending:
                    claim.unchecked = t("report.unchecked.budget")
                continue
            keys = list(dict.fromkeys(key for claim in pending for key in claim.refs))
            originals = claim_originals(
                keys,
                evidence,
                terms=title_terms(item["title"]),
                conn=conn,
                memory_path=self._memory_path,
                timesink_path=self._timesink_path,
                device=self._device,
            )
            jobs.append((index, pending, *build_check_request(item, pending, originals)))
        if not jobs:
            return 0
        # The log connection is bound to this thread and the model call writes its cost row
        # through it, so each worker opens its own on the same file.
        log_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        with ThreadPoolExecutor(len(jobs), thread_name_prefix="jarvis-report-check") as pool:
            futures = [
                pool.submit(self._judge, log_path, system, messages)
                for _, _, system, messages in jobs
            ]
            for (index, pending, _, _), future in zip(jobs, futures, strict=True):
                _record_verdicts(index, pending, future)
        return len(jobs)

    def _judge(
        self, log_path: Path, system: str, messages: list[dict[str, Any]]
    ) -> ChatResult:
        """One verification call on a worker thread, accounted through its own log connection."""
        assert self._reporter is not None  # noqa: S101 — checked by the caller.
        with closing(open_runtime_event_log(log_path)) as conn:
            return self._reporter.analyze(
                conn, system=system, messages=messages, tools=[JUDGE_TOOL], tool_choice="auto"
            )

    def _summarise(
        self,
        conn: sqlite3.Connection,
        evidence: DayEvidence,
        report: dict[str, Any],
        claims: list[Claim],
    ) -> tuple[str | None, int]:
        """The model's one sentence on the day's main line, given the checked table only.

        None — the program writes the line itself — when the model asserts
        completion, replies unusably, or the provider fails after the checks.
        """
        assert self._reporter is not None  # noqa: S101 — checked by the caller.
        if not report["items"]:
            return None, 0
        system, messages = build_summary_request(
            evidence, summary_table(report, claims, evidence)
        )
        try:
            result = self._reporter.analyze(
                conn, system=system, messages=messages, tools=[SUMMARY_TOOL], tool_choice="auto"
            )
        except Exception as exc:  # noqa: BLE001 — the checked table still makes the summary.
            LOGGER.warning("daily_report: summary call failed: %s", exc)
            return None, 1
        return parse_main_line(result), 1

    def _answer(
        self,
        conn: sqlite3.Connection,
        evidence: DayEvidence,
        name: str,
        argument: Any,  # noqa: ANN401 — a query string, or a list of keys.
    ) -> str:
        """One query's reply: search hits for a query, or the whole originals behind the keys."""
        if name == DETAILS_TOOL_NAME:
            return self._details(conn, evidence, argument) or "No readable keys."
        return search_day(
            evidence,
            str(argument),
            timesink_path=self._timesink_path,
            zone=resolve_zone(evidence.zone, self._tz)[1],
            device=self._device,
        )

    def _details(self, conn: sqlite3.Connection, evidence: DayEvidence, keys: list[str]) -> str:
        """The whole originals behind the keys."""
        return "\n\n".join(
            read_detail(
                key,
                evidence,
                conn=conn,
                memory_path=self._memory_path,
                timesink_path=self._timesink_path,
                device=self._device,
            )
            for key in keys
        )


class DailySchedule:
    """ADR 0101: the daemon writes the day before's report once a day; a turn only reads it.

    On a brain (ADR 0170) the day's TimeSink and git are the terminal's. A day written while the
    terminal could not be reached keeps its coverage marked unavailable, and is written once
    more as soon as a terminal that can read them is connected (at most ``CATCH_UPS`` times).
    """

    CATCH_UPS = 2

    def __init__(  # noqa: PLR0913 — the service, the hour, its zone and the brain's seam.
        self,
        service: DailyReportService,
        *,
        event_log_path: Path,
        at: time,
        zone: tzinfo,
        poll_s: float = 60.0,
        device_ready: Callable[[], bool] | None = None,
    ) -> None:
        """Bind the service and the local hour; nothing runs until :meth:`run`.

        ``device_ready`` (a brain only) says whether a terminal that reads TimeSink and git is
        connected right now.
        """
        self._service = service
        self._event_log_path = event_log_path
        self._at = at
        self._zone = zone
        self._poll_s = poll_s
        self._device_ready = device_ready
        self._last: date | None = None
        self._gap: date | None = None
        self._catch_ups = 0

    def due(self, now: datetime) -> date | None:
        """Yesterday, once ``at`` has passed today and yesterday was not tried yet."""
        local = now.astimezone(self._zone)
        day = local.date() - timedelta(days=1)
        return None if local.time() < self._at or day == self._last else day

    def catch_up(self) -> date | None:
        """The day last written without its terminal, once one is connected to write it again."""
        if self._gap is None or self._device_ready is None or self._catch_ups >= self.CATCH_UPS:
            return None
        return self._gap if self._device_ready() else None

    def write(
        self, day: date, *, now: datetime | None = None, catch_up: bool = False,
    ) -> dict[str, Any]:
        """One attempt per day: a saved report is reused, a failure waits for the next day.

        A catch-up writes the day again over the version made without the device.
        """
        if not catch_up:
            self._last = day
        with closing(open_runtime_event_log(self._event_log_path)) as conn:
            result = self._service.run(
                conn, local_date=day.isoformat(), regenerate=catch_up, now=now,
                action_id=f"daily-report-{day}" + ("-catch-up" if catch_up else ""),
            )
        if result.get("device_gap"):
            self._gap = day
            self._catch_ups = self._catch_ups + 1 if catch_up else 0
        elif self._gap == day:
            self._gap = None
        return result

    async def run(self) -> None:
        """Check every ``poll_s``; a Mac asleep at ``at`` writes on its first check after waking."""
        while True:
            await asyncio.sleep(self._poll_s)
            day = self.due(datetime.now(UTC))
            catching = day is None
            if day is None:
                day = self.catch_up()
            if day is None:
                continue
            try:
                result = await asyncio.to_thread(partial(self.write, day, catch_up=catching))
            except Exception:
                LOGGER.exception("daily_report: scheduled run for %s failed", day)
                continue
            LOGGER.info("daily_report: scheduled %s -> %s", day, result["outcome"])


# "What did I do yesterday" kept reaching refresh_work_state, whatever its description said
# (2026-10-01: 11.5 s of analysing today, answered with today's state). A question naming a past
# day is the saved report's: yesterday's is returned at once, another past day is pointed at
# daily_work_report.
_PAST_DAY = re.compile(
    r"前天|上周|上个?星期|上个?礼拜|上个?月|\d+\s*天前"
    r"|day before yesterday|last (?:week|month)|\d+\s*days? ago",
    re.IGNORECASE,
)
_YESTERDAY = re.compile(r"昨天|昨日|昨晚|yesterday", re.IGNORECASE)
_NOT_NOW = (
    "This question names a past day, so the current work state was not analysed. If the user meant "
    "what is happening now, call refresh_work_state again with a question that names no past day."
)


def past_day_answer(
    service: DailyReportService,
    conn: sqlite3.Connection,
    question: object,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """For refresh_work_state: the saved report a past-day question needs; None for now."""
    if not isinstance(question, str):
        return None
    if not (_PAST_DAY.search(question) or _YESTERDAY.search(question)):
        return None
    report = None if _PAST_DAY.search(question) else service.saved(conn, now=now)
    if report is not None:
        return {**report, "note": "Yesterday's saved daily report (summary). " + _NOT_NOW}
    return {
        "outcome": "past_day",
        "error": None,
        "state": None,
        "note": "Call daily_work_report with that day's local_date: it reuses a saved report "
        "at once. " + _NOT_NOW,
    }


def _reused(base: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    return {
        **base,
        "outcome": "reused",
        "briefing_id": existing["id"],
        "version": existing["version"],
        "source_ref": existing["source_ref"],
        "saved_at": existing["updated_at"],
        "coverage": existing["coverage"],
        "total_chars": existing["total_chars"],
        "summary": summary_of(str(existing["content"])),
        "model_calls": 0,
        "checks": 0,
        "error": None,
    }


def _tool_reply(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _assistant_turn(result: ChatResult) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": result.text or "",
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json},
            }
            for call in result.tool_calls
        ],
    }
