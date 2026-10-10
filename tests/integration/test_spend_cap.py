"""ADR 0173 — the daily spend card over a real event log and memory.db."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from jarvis.runtime.inherent_loop import _alert_deps, _notice_deps
from jarvis.runtime.spend_cap import SpendCap, SpendCapSettings
from jarvis.shared import lang
from jarvis.state import job_ledger
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from pathlib import Path

NOON = datetime.combine(datetime.now().astimezone().date(), time(12)).astimezone()


def _cost(log: Path, usd: float | None, at: datetime) -> None:
    with closing(open_event_log(log)) as conn:
        emit_event(
            conn, type="cost.recorded", payload={"kind": "decision", "model": "m", "cost_usd": usd},
            ts_epoch_ms=int(at.timestamp() * 1000),
        )


def _cards(db: Path, quiet: str = "off") -> list[str]:
    return [n["title"] for n in job_ledger.alerts_for_client(db, quiet, datetime.now(UTC))]


def test_one_card_per_local_day(tmp_path: Path) -> None:
    """Below the limit none; crossing one; again the same day none; a new day starts over."""
    log, db = tmp_path / "events.db", tmp_path / "memory.db"
    cap = SpendCap(SpendCapSettings(daily_usd=1.0), log, db)
    with closing(open_event_log(log)):
        pass
    _cost(log, 0.6, NOON)
    _cost(log, None, NOON)  # a model with no price adds nothing
    _cost(log, 5.0, NOON - timedelta(days=1))  # yesterday does not count today
    assert not cap.check(NOON)
    assert _cards(db) == []
    _cost(log, 0.5, NOON)  # 1.10 today: crosses
    assert cap.check(NOON)
    assert _cards(db) == [lang.t("spend.cap.title")]
    _cost(log, 0.5, NOON)
    assert not cap.check(NOON + timedelta(minutes=1))  # same day: no second card
    assert not SpendCap(SpendCapSettings(daily_usd=1.0), log, db).check(NOON)  # nor after a restart
    assert len(_cards(db)) == 1
    assert _cards(db, "dnd") == []  # quiet levels hold it like any alert
    tomorrow = NOON + timedelta(days=1)
    _cost(log, 1.2, tomorrow)  # a new day starts from zero and can cross again
    assert cap.check(tomorrow)
    assert len(job_ledger.alerts_for_client(db, "off", datetime.now(UTC) + timedelta(days=1))) == 2


def test_card_is_served_with_job_mail_off(tmp_path: Path) -> None:
    """No JobMail: the card still reaches ``GET /inherent/notices`` and is settled by ``POST``."""
    log, db = tmp_path / "events.db", tmp_path / "memory.db"
    with closing(open_event_log(log)):
        pass
    _cost(log, 1.5, NOON)
    assert SpendCap(SpendCapSettings(daily_usd=1.0), log, db).check(NOON)
    quiet = ["dnd"]
    speakers = lambda **_: {"private": False}  # noqa: E731
    deps = _notice_deps(
        None, None, None, alerts=_alert_deps(db, lambda: quiet[0], None, speakers),
    )
    client = TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(), **deps,
    )))
    assert client.get("/inherent/notices").json()["notices"] == []  # held at dnd, still pending
    quiet[0] = "off"
    (card,) = client.get("/inherent/notices").json()["notices"]
    assert card["title"] == lang.t("spend.cap.title")
    assert card["level"] == "card"
    seen = client.post(f"/inherent/notices/{card['id']}", json={"action": "seen"})
    assert seen.status_code == 200
    assert client.get("/inherent/notices").json()["notices"] == []
