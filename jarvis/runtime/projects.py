"""The project view and its sorting job (ADR 0037).

Composition-root code: L2 activities, answers and view, L3 request/parse and
a preset-bound client joined into one service. The HTTP surface only receives
the two callables built from it, and both run off the loop thread.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from jarvis.decision.projects import (
    ProjectsParseError,
    assign_tool,
    build_request,
    parse_assignments,
)
from jarvis.state import timesink
from jarvis.state.daily_report import resolve_zone
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.projects import (
    answers,
    catalog_fingerprint,
    compose_view,
    gather,
    save_answers,
    unsorted,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.decision.llm import ChatResult
    from jarvis.state.projects import Project, Window

LOGGER = logging.getLogger(__name__)
BATCH = 300


class Sorter(Protocol):
    """One forced-tool call; tests substitute a canned reply."""

    def analyze(
        self,
        conn: sqlite3.Connection,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> ChatResult:
        """Send the request through the given log connection's cost accounting."""
        ...


@dataclass
class _Run:
    result: dict[str, Any] | None = None


class ProjectsService:
    """Read the view (never a model call) and sort what is new (single-flight)."""

    def __init__(  # noqa: PLR0913 — the stores, the catalog, the sorter and the clock.
        self,
        *,
        event_log_path: Path,
        timesink_path: Path | None,
        projects: Sequence[Project],
        sorter: Sorter | None,
        model: str,
        tz: tzinfo | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind store locations; nothing is opened until a read or refresh."""
        self._event_log_path = event_log_path
        self._timesink_path = timesink_path
        self._projects = tuple(projects)
        self._catalog = catalog_fingerprint(self._projects)
        self._sorter = sorter
        self._model = model
        self._zone = resolve_zone(None, tz)[1]
        self._clock = clock
        self._cond = threading.Condition()
        self._running: _Run | None = None
        self._last: dict[str, Any] = {"outcome": None, "error": None}

    def _window(self) -> Window | None:
        with timesink.snapshot(self._timesink_path) as snap:
            if snap is None:
                return None
            try:
                return gather(snap, self._zone, self._clock())
            except (sqlite3.Error, ValueError) as exc:
                LOGGER.warning("projects: TimeSink read failed: %s", exc)
                return None

    def read(self) -> dict[str, Any]:
        """``GET /inherent/projects``: the derived view plus the last run's outcome."""
        conn = open_runtime_event_log(self._event_log_path)
        try:
            answered, sorted_at = answers(conn, self._catalog)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        view = compose_view(
            self._window(), answered, sorted_at, self._projects, self._zone, self._clock()
        )
        return {**view, "refreshing": self._running is not None, **self._last}

    def refresh(self, *, trigger: str = "dashboard") -> dict[str, Any]:
        """Sort every unsorted activity of the window; a call during a run joins that run."""
        with self._cond:
            run, owner = self._running, self._running is None
            if run is None:
                run = self._running = _Run()
        if owner:
            result: dict[str, Any] | None = None
            try:
                result = self._sort(trigger)
            finally:
                with self._cond:
                    run.result = result or {"outcome": "failed", "error": "refresh aborted"}
                    self._last = run.result
                    self._running = None
                    self._cond.notify_all()
        else:
            with self._cond:
                while run.result is None:
                    self._cond.wait()
        final = run.result or {"outcome": "failed", "error": "refresh aborted"}
        return {**self.read(), **final, **({} if owner else {"joined": True})}

    def _sort(self, trigger: str) -> dict[str, Any]:
        window = self._window()
        if window is None or not window.activities:
            return {"outcome": "no_evidence", "error": None}
        conn = open_runtime_event_log(self._event_log_path)
        saved = 0
        try:
            answered, _ = answers(conn, self._catalog)
            todo = unsorted(window, answered)
            if not todo:
                return {"outcome": "reused", "error": None}
            if self._sorter is None:
                return {"outcome": "failed", "error": "sorting model is not configured"}
            ids = {p.id for p in self._projects}
            tool = assign_tool(self._projects)
            for start in range(0, len(todo), BATCH):
                system, messages, keys = build_request(self._projects, todo[start : start + BATCH])
                reply = self._sorter.analyze(conn, system=system, messages=messages, tools=[tool])
                found = parse_assignments(reply, keys, ids)
                if found:
                    save_answers(
                        conn,
                        catalog=self._catalog,
                        answered=found,
                        activities=window.activities,
                        model=self._model,
                        trigger=trigger,
                    )
                    saved += 1
        except (ProjectsParseError, sqlite3.Error) as exc:
            LOGGER.warning("projects: sorting stopped after %d saved batch(es): %s", saved, exc)
            return {"outcome": "failed", "error": f"{saved} batch(es) saved; {exc}"[:300]}
        except Exception as exc:  # noqa: BLE001 — provider/SDK failures keep saved batches.
            LOGGER.warning("projects: sorting failed: %s: %s", type(exc).__name__, exc)
            error = f"{saved} batch(es) saved; {type(exc).__name__}: {exc}"[:300]
            return {"outcome": "failed", "error": error}
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        return {"outcome": "classified", "error": None}
