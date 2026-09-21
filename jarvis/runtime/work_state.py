"""The one work-state refresh workflow (ADR 0023): dashboard and conversation share it.

Composition-root code: it wires L2 evidence + persistence, L3 request/parse
and a dedicated L3 client into one job. Tools (L4) and the HTTP surface (L5)
only receive callables built here.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Protocol

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.work_state import (
    REPORT_TOOL,
    WorkStateParseError,
    build_request,
    parse_report,
)
from jarvis.shared.pricing import load_pricing_table
from jarvis.shared.realtime import new_response_id
from jarvis.state.daily_contract import DailyError
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.work_state import (
    compose_state,
    current_state,
    gather_evidence,
    save_state,
)
from jarvis.surface.timesink_observer import TimesinkHead, collect, latest_observation

if TYPE_CHECKING:
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.decision.llm import ChatResult

LOGGER = logging.getLogger(__name__)
_CALL_TIMEOUT_S = 90.0
_CALL_MAX_RETRIES = 1
OUTCOMES = ("analyzed", "reused", "no_evidence", "failed")


class Analyst(Protocol):
    """One bounded analysis request; tests substitute a canned reply."""

    def analyze(
        self, conn: sqlite3.Connection, *, system: str, messages: list[dict[str, Any]]
    ) -> ChatResult:
        """Send the request through the given log connection's cost accounting."""
        ...


class LLMAnalyst:
    """A dedicated preset-bound client per call, accounted like ``screen_look``'s vision call.

    One analysis job per instance: ``kind`` is what its calls are accounted as,
    and the default tool catalog is the work-state report tool. A caller with
    its own catalog (ADR 0024's daily report) passes ``tools`` per call.
    """

    def __init__(
        self,
        llm_config: Mapping[str, Any],
        *,
        pricing_path: Path | None,
        account_cost: bool,
        kind: str = "work_state",
    ) -> None:
        """Freeze the preset; malformed presets raise here, at boot, not at use."""
        self._factory = LLMSessionFactory(llm_config)
        self._snapshot = self._factory.snapshot()
        self._pricing = {} if pricing_path is None else load_pricing_table(pricing_path)
        self._account_cost = account_cost
        self._kind = kind

    def analyze(
        self,
        conn: sqlite3.Connection,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str = "required",
    ) -> ChatResult:
        """One call over the given catalog; the model has nothing outside it to call.

        ``required`` forces a tool call; a thinking preset (DeepSeek rejects
        ``required`` with thinking on) needs ``auto`` and an instruction to call.
        """
        catalog = [REPORT_TOOL] if tools is None else list(tools)
        client = self._factory.create(self._snapshot, response_id=new_response_id())
        cost_recorder = (
            CostRecorder(conn, pricing_table=self._pricing) if self._account_cost else None
        )
        if cost_recorder is None:
            return client.chat(
                messages=messages, system=system, tools=catalog, tool_choice=tool_choice
            )
        return cost_recorder.chat(
            client,
            messages=messages,
            system=system,
            tools=catalog,
            tool_choice=tool_choice,
            kind=self._kind,
            turn_id=None,
        )


def build_analyst(  # noqa: PLR0913 — the preset plus this job's accounting identity and budget.
    config: Mapping[str, Any],
    preset_name: str,
    *,
    pricing_path: Path | None,
    account_cost: bool,
    kind: str = "work_state",
    timeout_s: float = _CALL_TIMEOUT_S,
) -> LLMAnalyst | None:
    """Build from ``llm.presets.<preset_name>``; ``None`` degrades every run to ``failed``."""
    llm_block = config.get("llm")
    presets = llm_block.get("presets") if isinstance(llm_block, Mapping) else None
    preset = presets.get(preset_name) if isinstance(presets, Mapping) else None
    if not isinstance(preset, Mapping):
        LOGGER.warning("%s: llm.presets.%s missing; the run reports failed", kind, preset_name)
        return None
    llm_config: dict[str, Any] = {
        "provider": "openai",
        "presets": {preset_name: dict(preset)},
        "default_preset": preset_name,
        "timeout_s": timeout_s,
        "max_retries": _CALL_MAX_RETRIES,
    }
    try:
        return LLMAnalyst(
            llm_config, pricing_path=pricing_path, account_cost=account_cost, kind=kind
        )
    except (ValueError, TypeError, KeyError) as exc:
        LOGGER.warning("%s: llm.presets.%s malformed (%s); the run fails", kind, preset_name, exc)
        return None


def _freshness(
    state: dict[str, Any] | None, head: dict[str, Any] | None, checked_at_ms: int | None
) -> dict[str, Any]:
    """Three different clocks: when the head was last checked, the newest data, the analysis."""
    latest = None
    if head is not None:
        instants = [head.get("capture_latest_seen"), head.get("span_latest_end")]
        latest = max((x for x in instants if x), default=None)
    return {
        "checked_at_ms": checked_at_ms,
        "latest_observed_at": latest,
        "analyzed_at": state.get("analyzed_at") if state else None,
        "analysis_observed_until": state.get("observed_until") if state else None,
    }


@dataclass
class _Run:
    """One in-flight refresh: its input and, once finished, its own result."""

    key: tuple[str | None, str | None, bool]
    result: dict[str, Any] | None = None


class WorkStateService:
    """Read and refresh the one record.

    A refresh with the same input (question, note, force) as the one running
    joins it and returns that run's result; a different input waits its turn
    and then runs on the record the first one saved. Every caller gets the
    result of the analysis it asked for or joined, never whatever finished
    last.
    """

    def __init__(  # noqa: PLR0913 — the configured stores, clock zone and the one analyst.
        self,
        *,
        event_log_path: Path,
        memory_path: Path | None,
        timesink_path: Path | None,
        repos: tuple[str, ...],
        analyst: Analyst | None,
        model: str,
        tz: tzinfo | None = None,
    ) -> None:
        """Bind store locations; nothing is opened until a read or refresh."""
        self._event_log_path = event_log_path
        self._memory_path = memory_path
        self._timesink_path = timesink_path
        self._repos = repos
        self._analyst = analyst
        self._model = model
        self._tz = tz
        self._cond = threading.Condition()
        self._running: _Run | None = None
        self._last: dict[str, Any] = {"outcome": None, "error": None}
        self._checked: tuple[int, dict[str, Any]] | None = None

    def note_checked(self, head: TimesinkHead) -> None:
        """One head read happened (poll or refresh), changed or not: the "checked" clock."""
        at_ms = int(time.time() * 1000)
        self._checked = (at_ms, {**asdict(head), "observed_at_ms": at_ms})

    def read(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """The dashboard's read model: saved record, latest data head, freshness, in-flight flag."""
        return self._view(conn, {**self._last, "state": current_state(conn)})

    def _view(
        self, conn: sqlite3.Connection, result: dict[str, Any], *, joined: bool = False
    ) -> dict[str, Any]:
        head = latest_observation(conn)
        checked_at_ms = head.get("observed_at_ms") if head else None
        if self._checked is not None:
            checked_at_ms, head = self._checked
        state = result["state"]
        view = {
            "state": state,
            "data": head,
            "freshness": _freshness(state, head, checked_at_ms),
            "refreshing": self._running is not None,
            "outcome": result["outcome"],
            "error": result["error"],
        }
        if joined:
            view["joined"] = True
        return view

    def refresh(  # noqa: PLR0913 — one keyword per request field.
        self,
        conn: sqlite3.Connection,
        *,
        question: str | None = None,
        note: str | None = None,
        force: bool = False,
        trigger: str = "dashboard",
        action_id: str | None = None,
    ) -> dict[str, Any]:
        """Analyse now if the evidence or the input changed (or forced); see the class docstring."""
        key = (question, note, force)
        with self._cond:
            while self._running is not None:
                if self._running.key == key:
                    run = self._running
                    while run.result is None:
                        self._cond.wait()
                    return self._view(conn, run.result, joined=True)
                self._cond.wait()
            run = self._running = _Run(key)
        result: dict[str, Any] | None = None
        try:
            result = self._run(
                conn,
                question=question,
                note=note,
                force=force,
                trigger=trigger,
                action_id=action_id,
            )
        finally:
            with self._cond:
                run.result = result or {
                    "outcome": "failed",
                    "error": "refresh aborted",
                    "state": None,
                }
                self._last = {"outcome": run.result["outcome"], "error": run.result["error"]}
                self._running = None
                self._cond.notify_all()
        return self._view(conn, run.result)

    def refresh_in_own_connection(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401 — forwarded to refresh().
        """For callers off the loop thread (HTTP): open, refresh, close."""
        conn = open_runtime_event_log(self._event_log_path)
        try:
            return self.refresh(conn, **kwargs)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    def _run(  # noqa: PLR0913 — mirrors refresh().
        self,
        conn: sqlite3.Connection,
        *,
        question: str | None,
        note: str | None,
        force: bool,
        trigger: str,
        action_id: str | None,
    ) -> dict[str, Any]:
        previous = None
        try:
            previous = current_state(conn)
            evidence = gather_evidence(
                conn,
                memory_path=self._memory_path,
                timesink_path=self._timesink_path,
                repos=self._repos,
                note=note,
                question=question,
                tz=self._tz,
            )
            self.note_checked(collect(self._timesink_path))
            if evidence.empty:
                return {"outcome": "no_evidence", "error": None, "state": previous}
            if (
                previous is not None
                and not force
                and previous["evidence"]["fingerprint"] == evidence.fingerprint
            ):
                return {"outcome": "reused", "error": None, "state": previous}
            if self._analyst is None:
                error = "analysis model is not configured"
                return {"outcome": "failed", "error": error, "state": previous}
            system, messages = build_request(evidence, question=question, previous=previous)
            report = parse_report(self._analyst.analyze(conn, system=system, messages=messages))
            item = compose_state(report, evidence, question=question, model=self._model)
            saved = save_state(
                conn,
                item,
                expected_version=previous["version"] if previous else 0,
                trigger=trigger,
                action_id=action_id,
            )
        except (WorkStateParseError, DailyError) as exc:
            LOGGER.warning("work_state: refresh kept the previous record: %s", exc)
            return {"outcome": "failed", "error": str(exc), "state": previous}
        except Exception as exc:  # noqa: BLE001 — provider/SDK failures keep the old record.
            LOGGER.warning("work_state: analysis failed: %s: %s", type(exc).__name__, exc)
            error = f"{type(exc).__name__}: {exc}"[:300]
            return {"outcome": "failed", "error": error, "state": previous}
        return {"outcome": "analyzed", "error": None, "state": saved}
