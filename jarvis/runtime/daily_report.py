"""The daily work report workflow (ADR 0024): the skill's procedure, owned by the runtime.

Composition-root code: it wires L2 day evidence + the briefing store, L3
request/parse/compose and a preset-bound analyst into one job. The
conversation reaches it through one flat tool.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Protocol

from jarvis.decision.daily_report import (
    DETAILS_TOOL,
    REPORT_AGAIN,
    REPORT_NOW,
    REPORT_TOOL,
    DailyReportParseError,
    build_request,
    compose_report,
    parse_report,
    requested_details,
    summary_of,
)
from jarvis.state.daily_contract import DailyError
from jarvis.state.daily_report import (
    existing_report,
    gather_day,
    read_detail,
    resolve_day,
    resolve_zone,
    save_report,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.decision.llm import ChatResult
    from jarvis.state.daily_report import DayEvidence

LOGGER = logging.getLogger(__name__)
OUTCOMES = ("generated", "reused", "no_evidence", "failed")
KIND = "daily_report"
MAX_ROUNDS = 3
"""Model calls per report: the first, plus up to two that serve details and require the report."""


class Reporter(Protocol):
    """One bounded model call with a given tool catalog; tests substitute a canned reply."""

    def analyze(
        self,
        conn: sqlite3.Connection,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> ChatResult:
        """Send the request through the given log connection's cost accounting."""
        ...


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
    ) -> None:
        """Bind store locations; nothing is opened until a run."""
        self._memory_path = memory_path
        self._timesink_path = timesink_path
        self._repos = tuple(repos)
        self._reporter = reporter
        self._model = model
        self._tz = tz
        # ponytail: one lock for all days; per-day locks only if two reports must run at once.
        self._lock = threading.Lock()

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
        """Gather, analyse, compose and save; a failure here keeps the previous version."""
        evidence = gather_day(
            conn,
            memory_path=self._memory_path,
            timesink_path=self._timesink_path,
            repos=self._repos,
            day=day,
            zone_name=zone_name,
            zone=zone,
            now=moment,
        )
        if evidence.empty:
            return {
                **base,
                "outcome": "no_evidence",
                "version": version,
                "window": evidence.window,
                "coverage": evidence.coverage,
                "limits": evidence.limits,
                "error": None,
            }
        if self._reporter is None:
            message = "report model is not configured"
            raise DailyError(message, "unavailable")
        report, calls = self._ask(conn, evidence)
        content, refs, coverage = compose_report(
            report, evidence, model=self._model, generated_at=moment.astimezone(zone)
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
            "summary": summary_of(content),
            "model_calls": calls,
            "error": None,
        }

    def _ask(self, conn: sqlite3.Connection, evidence: DayEvidence) -> tuple[dict[str, Any], int]:
        """Serve details, require the report, retry an unusable one; bounded by ``MAX_ROUNDS``.

        A model that asks for details instead of reporting is answered with its
        material and told to report. A reply that does not parse — the provider
        malforms or truncates long arguments now and then — is answered with
        that error and asked again. The last round's failure ends the run.
        """
        assert self._reporter is not None  # noqa: S101 — checked by the caller.
        system, messages = build_request(evidence)
        tools: list[dict[str, Any]] = [DETAILS_TOOL, REPORT_TOOL]
        for round_number in range(1, MAX_ROUNDS + 1):
            result = self._reporter.analyze(conn, system=system, messages=messages, tools=tools)
            last = round_number == MAX_ROUNDS
            requests = requested_details(result)
            if requests and not last:
                messages.append(_assistant_turn(result))
                for call_id, keys in requests.items():
                    messages.append(
                        _tool_reply(call_id, self._details(conn, evidence, keys) or "没有可读的键")
                    )
                messages.append({"role": "user", "content": REPORT_NOW})
                tools = [REPORT_TOOL]
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
                tools = [REPORT_TOOL]
        message = "report_daily_work was never produced"
        raise DailyReportParseError(message)

    def _details(
        self, conn: sqlite3.Connection, evidence: DayEvidence, keys: list[str]
    ) -> str:
        """The originals behind the keys the model asked for, each bounded."""
        return "\n\n".join(
            read_detail(
                key,
                evidence,
                conn=conn,
                memory_path=self._memory_path,
                timesink_path=self._timesink_path,
            )
            for key in keys
        )


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
