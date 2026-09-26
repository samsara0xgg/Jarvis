"""ADR 0050 — balances Allen records on the Usage page, and what the page then shows.

Each check asserts an observable: the route's status, the ``usage.balance_recorded``
row it leaves in the log, the Costs query the OpenAI collector sends, or the snapshot
data ``GET /inherent/usage`` serves.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
from typing import TYPE_CHECKING, Any

from fastapi.testclient import TestClient

from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import usage_observer
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import pytest

DESKTOP = {"Authorization": "Bearer desktop"}
# 2026-09-25 18:00 UTC; its UTC day starts at 1_790_294_400.
RECORDED_MS = 1_790_359_200_000
RECORDED_DAY_S = 1_790_294_400


def _balances(db: Path) -> list[tuple[str, float]]:
    with contextlib.closing(open_event_log(db)) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE type = 'usage.balance_recorded' ORDER BY id"
        ).fetchall()
    return [(p["service"], p["usd"]) for p in (json.loads(r[0]) for r in rows)]


def test_balance_route_records_only_a_plausible_desktop_balance(tmp_path: Path) -> None:
    """No credential 401; unknown service, text, negative or NaN 400; neither writes a row."""
    db = tmp_path / "events.db"

    # The route runs on the app's thread, so it opens the log there, as the daemon's loop does.
    def record(service: str, usd: float) -> object:
        with contextlib.closing(open_event_log(db)) as conn:
            return usage_observer.UsageObserver(conn).record_balance(service, usd)

    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: None,
            broadcaster=InherentBroadcaster(),
            plugin_authorize=lambda header: header == DESKTOP["Authorization"],
            usage_record_balance=record,
        )
    )
    with TestClient(app) as client:
        url = "/inherent/usage/balance"
        assert client.post(url, json={"service": "openai", "usd": 25}).status_code == 401
        for bad in (
            {"service": "deepseek", "usd": 5},
            {"service": "openai", "usd": "25"},
            {"service": "openai", "usd": -1},
            {"service": "openai", "usd": True},
        ):
            assert client.post(url, headers=DESKTOP, json=bad).status_code == 400, bad
        nan = client.post(url, headers=DESKTOP, content=b'{"service": "openai", "usd": NaN}')
        assert nan.status_code == 400
        assert _balances(db) == []
        ok = client.post(url, headers=DESKTOP, json={"service": "openai", "usd": 25.5})
        assert ok.status_code == 200
    assert _balances(db) == [("openai", 25.5)]


def test_openai_balance_is_the_recording_minus_costs_since_its_utc_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Costs query starts at the recording's UTC day; the page gets recorded minus spent."""
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-test")
    asked: list[str] = []

    def answer(url: str, headers: Mapping[str, str], *, timeout_s: float) -> dict[str, Any]:
        del headers, timeout_s
        asked.append(url)
        if "/costs" in url and f"start_time={RECORDED_DAY_S}" in url:
            day = [{"line_item": "gpt-5.6-luna, input", "amount": {"value": 1.25}}]
            later = [{"line_item": "gpt-5.4-mini, output", "amount": {"value": 0.5}}]
            return {
                "data": [
                    {"start_time": RECORDED_DAY_S, "results": day},
                    {"start_time": RECORDED_DAY_S + 86_400, "results": later},
                ]
            }
        return {"data": []}

    monkeypatch.setattr(usage_observer, "_get_json", answer)
    snapshot = usage_observer.collect_openai(
        timeout_s=1,
        now=dt.datetime(2026, 9, 26, 12, tzinfo=dt.UTC),
        balance=usage_observer.RecordedBalance(25.0, RECORDED_MS),
    )
    assert snapshot.status == "ok"
    assert snapshot.data["balance_usd"] == 23.25
    assert snapshot.data["balance_recorded_usd"] == 25.0
    assert snapshot.data["balance_recorded_at"] == "2026-09-25T18:00:00+00:00"
    assert sum(f"start_time={RECORDED_DAY_S}" in url for url in asked) == 1


def test_minimax_uses_the_newer_recording_after_a_restart(tmp_path: Path) -> None:
    """A page recording newer than the YAML anchor wins, and survives a restart."""
    config = usage_observer.UsageConfig(
        minimax_anchor_usd=17.82, minimax_anchor_at_ms=1_000, minimax_usd_per_million_chars=60.0
    )
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        usage_observer.UsageObserver(conn, config).record_balance("minimax", 30.0)
        recorded_at = conn.execute(
            "SELECT ts_epoch_ms FROM events WHERE type = 'usage.balance_recorded'"
        ).fetchone()[0]
        for at, characters in ((recorded_at - 1, 900_000), (recorded_at + 1, 100_000)):
            emit_event(
                conn,
                type="tts.usage_observed",
                ts_epoch_ms=at,
                payload={
                    "provider": "minimax",
                    "characters": characters,
                    "response_id": f"r{at}",
                    "sequence": 0,
                    "actor": "observer",
                },
            )
        restarted = usage_observer.UsageObserver(conn, config)
        restarted.recover_baselines()
        restarted.emit([])
        minimax = usage_observer.latest_usage(conn)["services"]["minimax"]["data"]
        assert minimax["anchor_usd"] == 30.0
        assert minimax["characters_since_anchor"] == 100_000
        assert minimax["estimate_usd"] == 24.0
