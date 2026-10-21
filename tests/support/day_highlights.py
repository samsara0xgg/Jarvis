"""The ledger world's day for the highlight tests, served as the route folds it."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime.daemon.routes import read_day
from jarvis.runtime.highlight_marks import HighlightMarks
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state import picture_notes as note_records
from jarvis.state.attachments import AttachmentRef
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import emit_event, open_event_log
from tests.support.ledger import NOW, ZONE, ledger_at

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.state.ledger import LedgerSources

VANCOUVER = ZoneInfo("America/Vancouver")  # UTC-7 until 11-01: the ledger world's own offset


DAY = date(2026, 10, 7)  # a complete day of the ledger's world: Chrome 13:00-13:45


TODAY = date(2026, 10, 8)  # NOW is 15:00 of it


DAY_KEY, TODAY_KEY = DAY.isoformat(), TODAY.isoformat()


NOW_MS = int(NOW.timestamp()) * 1000


def moment_ms(moment: datetime) -> int:
    return int(moment.timestamp()) * 1000


def work_id(day: int, hour: int, minute: int = 0) -> str:
    """The id of the work block that starts at this time."""
    return f"work-{moment_ms(ledger_at(day, hour, minute))}"


def visit_row(
    src: LedgerSources,
    uid: str,
    arrived: datetime,
    departed: datetime | None = None,
    place: str | None = None,
) -> None:
    """A ``phone.visit_observed`` row, which the day line folds into the stay ``uid``."""
    payload: dict[str, Any] = {
        "lat": 1.0,
        "lng": 2.0,
        "accuracy_m": 30.0,
        "arrived_at_ms": moment_ms(arrived),
    }
    if departed is not None:
        payload["departed_at_ms"] = moment_ms(departed)
    if place is not None:
        payload["place"] = place
    with closing(open_event_log(src.event_log)) as conn:
        emit_event(
            conn,
            type="phone.visit_observed",
            payload=payload,
            ts_epoch_ms=moment_ms(arrived),
            event_uid=uid,
            ingestion_node="phone",
        )


def picture_note(src: LedgerSources, picture: str, at: datetime, note: str) -> None:
    """A picture he sent at ``at``, with the note written for it (ADR 0223)."""
    with closing(open_event_log(src.event_log)) as conn:
        emit_event(
            conn,
            type="surface.user_intent",
            ts_epoch_ms=moment_ms(at),
            correlation={"turn_id": f"T-{picture}"},
            payload={"transcript": "x", "turn_id": f"T-{picture}", "attachments": [picture]},
        )
    ref = AttachmentRef(picture, "image", "IMG_1.png", 1, "image/png")
    note_records.append(src.memory_db, ref, note)


def served_day(src: LedgerSources, day: date = DAY, now_ms: int = NOW_MS) -> dict[str, Any]:
    """``GET /inherent/day`` for ``day``, as the route folds it."""
    return read_day(src.event_log, VANCOUVER, src.memory_db, src.timesink, day, now_ms)


def marks_of(src: LedgerSources) -> HighlightMarks:
    return HighlightMarks(
        src.event_log,
        DaySources(ZONE, src.timesink, src.memory_db, None, None),
        clock=lambda: NOW_MS / 1000,
    )


class Dispatcher:
    """The real registry, as the model calls it: one tool call, its JSON answer."""

    def __init__(self, tmp_path: Path, src: LedgerSources) -> None:
        """The registry with the runtime's marker, and a log to dispatch against."""
        self.registry = build_default_registry(highlight_marker=marks_of(src).mark)
        self.paths = bootstrap_runtime(tmp_path / "runtime")
        self.conn = open_event_log(self.paths.event_log)
        self.calls = 0

    def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        """Call ``mark_highlight`` with ``args``; the tool's answer or its error."""
        self.calls += 1
        request = ActionRequest(
            action_id=f"A-{self.calls}",
            tool_name="mark_highlight",
            target_entity_ref=None,
            caller_principal=CallerPrincipal.JARVIS_LLM,
            risk_level="L1",
            arguments=args,
            authorization_lease=None,
            run_id=None,
            turn_id=None,
        )
        lifecycle = ActionLifecycle()
        lifecycle.register(request.action_id)
        lifecycle.transition(request.action_id, "authorized")
        bundle = self.registry.dispatch(request, self.conn, self.paths, lifecycle)
        out = json.loads(bundle.tool_output or "{}")
        return out if isinstance(out, dict) else {"value": out}


def ids_of(day: dict[str, Any]) -> list[str]:
    return [h["id"] for h in day["highlights"]]
