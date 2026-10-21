"""One runtime root with the real tools, a fake clock and a recording voice."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime.daemon.routes import notice_deps
from jarvis.runtime.reminders import Reminders
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state import reminders as reminder_state
from jarvis.state import things
from jarvis.state.event_log import iter_events_of_types, open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from pathlib import Path

PRIVATE = {"name": "AirPods Pro", "private": True}


class ReminderWorld:
    """One runtime root with the real tools, a fake clock and a recording voice."""

    def __init__(self, tmp_path: Path) -> None:
        self.paths = bootstrap_runtime(tmp_path)
        self.conn = open_event_log(self.paths.event_log)
        self.zone = ZoneInfo("America/Los_Angeles")  # his zone, not whatever the machine leaks
        self.registry = build_default_registry(local_zone=self.zone)
        self.lifecycle = ActionLifecycle()
        self.start = datetime.now(UTC).replace(microsecond=0)
        self.clock = self.start
        self.said: list[str] = []
        self.device: dict[str, Any] = dict(PRIVATE)
        self.hold: str | None = None
        self.n = 0
        self.reminders = Reminders(
            self.paths.event_log,
            moment=SimpleNamespace(hold=lambda: self.hold),  # type: ignore[arg-type]
        )
        self.reminders.now = lambda: self.clock
        self.reminders.may_speak = lambda: True
        self.reminders.say = self.said.append
        self.reminders.output = lambda fresh=False: dict(self.device)  # noqa: ARG005

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.n += 1
        action_id = f"A{self.n}"
        request = ActionRequest(
            action_id=action_id,
            tool_name=name,
            target_entity_ref=None,
            caller_principal=CallerPrincipal.JARVIS_LLM,
            risk_level="L1",
            arguments=args,
            authorization_lease=None,
            run_id=None,
            turn_id=None,
        )
        self.lifecycle.register(action_id)
        self.lifecycle.transition(action_id, "authorized")
        bundle = self.registry.dispatch(request, self.conn, self.paths, self.lifecycle)
        out = json.loads(bundle.tool_output or "{}")
        return out if isinstance(out, dict) else {"value": out}

    def set(self, minutes: float, text: str = "和 employer 一对一") -> dict[str, Any]:
        """Keep a thing at ``minutes`` from now; its ring's id, and the words the ring carries."""
        when = (self.start + timedelta(minutes=minutes)).astimezone(self.zone)
        made = self.call(
            "schedule",
            {"action": "add", "what": text, "at": when.isoformat(timespec="seconds")},
        )
        (ring,) = [
            r for r in self.waiting() if r["id"].startswith(f"{things.RING_PREFIX}{made['id']}")
        ]
        return {**made, "thing": made["id"], "id": ring["id"], "line": f"{when:%H:%M} {text}"}

    def waiting(self) -> list[dict[str, Any]]:
        """The rings the ledger armed that have not rung."""
        return [
            {"id": r.reminder_id, "text": r.text}
            for r in reminder_state.pending(self.conn)
            if r.reminder_id.startswith(things.RING_PREFIX)
        ]

    def fired(self) -> list[dict[str, Any]]:
        return [dict(e.payload) for e in iter_events_of_types(self.conn, ("reminder.fired",))]

    def app(self, job_mail: object | None = None) -> TestClient:
        deps = notice_deps(job_mail, self.reminders, None)  # type: ignore[arg-type]
        return TestClient(
            create_app(
                InherentDeps(
                    submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(), **deps
                )
            )
        )
