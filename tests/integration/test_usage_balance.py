"""ADR 0065 — balances Allen records on the Usage page, and what the page then shows.

Each check asserts an observable: the route's status, the ``usage.balance_recorded``
row it leaves in the log, the Costs query the OpenAI collector sends, or the snapshot
data ``GET /inherent/usage`` serves.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import urllib.error
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from jarvis.state.event_log import open_event_log
from jarvis.surface import usage_observer
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

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


def test_minimax_reads_its_balance_and_says_why_it_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key goes to query_balance; a debt reads negative; a refusal keeps MiniMax's words."""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-api-test")
    asked: list[tuple[str, str]] = []
    answers: list[dict[str, Any]] = [
        {"available_amount": "17.50", "owed_amount": "0.00", "base_resp": {"status_code": 0}},
        {"available_amount": "0.00", "owed_amount": "0.40", "base_resp": {"status_code": 0}},
        {"base_resp": {"status_code": 1004, "status_msg": "login fail"}},
    ]

    def answer(url: str, headers: Mapping[str, str], *, timeout_s: float) -> dict[str, Any]:
        del timeout_s
        asked.append((url, headers["Authorization"]))
        return answers.pop(0)

    monkeypatch.setattr(usage_observer, "_get_json", answer)
    ok, owed, refused = (usage_observer.collect_minimax(timeout_s=1) for _ in range(3))
    assert asked[0] == ("https://api.minimax.io/account/query_balance", "Bearer sk-api-test")
    assert (ok.status, ok.data) == ("ok", {"balance": 17.5})
    assert (owed.status, owed.data) == ("ok", {"balance": -0.4})
    assert (refused.status, refused.error) == ("error", "login fail")
    monkeypatch.delenv("MINIMAX_API_KEY")
    assert usage_observer.collect_minimax(timeout_s=1).status == "unconfigured"


def test_minimax_balance_can_no_longer_be_typed(tmp_path: Path) -> None:
    """MiniMax reports its own balance now, so recording one is refused and writes nothing."""
    with (
        contextlib.closing(open_event_log(tmp_path / "events.db")) as conn,
        pytest.raises(ValueError, match="minimax"),
    ):
        usage_observer.UsageObserver(conn).record_balance("minimax", 30.0)
    assert _balances(tmp_path / "events.db") == []


def test_anthropic_spend_is_the_brains_recorded_anthropic_calls(tmp_path: Path) -> None:
    """Today and month by model from ``cost.recorded``; other providers and months stay out."""
    now = dt.datetime(2026, 10, 12, 15, tzinfo=dt.UTC).astimezone()
    today_ms = int(now.timestamp() * 1000)
    last_month_ms = int((now - dt.timedelta(days=20)).timestamp() * 1000)
    earlier_ms = int((now - dt.timedelta(hours=now.hour + 1)).timestamp() * 1000)
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        rows = [
            (today_ms, "anthropic", "claude-haiku-5-5", 0.25, "provider_final"),
            (earlier_ms, "anthropic", "claude-haiku-5-5", 0.5, "provider_final"),
            (today_ms, "anthropic", "claude-sonnet-5-5", None, "provider_final"),
            (today_ms, "anthropic", "claude-haiku-5-5", None, "unavailable"),
            (today_ms, "openai", "gpt-5.6-luna", 9.0, "provider_final"),
            (last_month_ms, "anthropic", "claude-haiku-5-5", 4.0, "provider_final"),
        ]
        for ts_ms, provider, model, cost, status in rows:
            conn.execute(
                "INSERT INTO events (event_uid, schema_version, ts_epoch_ms, type, payload_json) "
                "VALUES (?, 1, ?, 'cost.recorded', ?)",
                (
                    f"u{ts_ms}{model}{cost}{status}",
                    ts_ms,
                    json.dumps(
                        {
                            "provider": provider,
                            "model": model,
                            "cost_usd": cost,
                            "usage_status": status,
                        }
                    ),
                ),
            )
        snapshot = usage_observer.recorded_spend(conn, "anthropic", now=now)
    assert snapshot.service == "anthropic"
    assert snapshot.status == "ok"
    assert snapshot.data["today_usd"] == 0.25
    assert snapshot.data["month_usd"] == 0.75
    assert snapshot.data["by_model"][0] == {
        "model": "claude-haiku-5-5",
        "today_usd": 0.25,
        "month_usd": 0.75,
    }
    assert snapshot.data["unpriced_calls"] == 1


def test_anthropic_official_cost_is_read_with_the_key_and_a_refusal_waits_an_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key goes in a header; a 403 leaves a short reason, no key, and is not retried."""
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT00:00:00Z")
    asked: list[tuple[str, str]] = []
    refuse = False

    def answer(url: str, headers: Mapping[str, str], *, timeout_s: float) -> dict[str, Any]:
        del timeout_s
        asked.append((url, headers["x-api-key"]))
        if refuse:
            raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)  # type: ignore[arg-type]
        rows = [{"model": "claude-haiku-5-5", "amount": "250.5"}, {"model": None, "amount": "10"}]
        return {"data": [{"starting_at": "2020-01-01T00:00:00Z", "results": rows[:1]},
                         {"starting_at": today, "results": rows}]}

    monkeypatch.setattr(usage_observer, "_get_json", answer)
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        observer = usage_observer.UsageObserver(conn, services=("anthropic",))
        observer.collect()
        (snapshot,) = observer.observe_recorded()
        official = snapshot.data["official"]
        assert official["today_usd"] == 2.605
        assert official["by_model"][0]["model"] == "claude-haiku-5-5"
        assert asked[0][1] == "sk-ant-test-key"
        assert "sk-ant" not in json.dumps(snapshot.data)

        refuse, observer._official_retry_at = True, 0.0  # noqa: SLF001
        observer.collect()
        observer.collect()
        (snapshot,) = observer.observe_recorded()
    assert snapshot.data["official_error"] == "HTTP 403"
    assert "official" not in snapshot.data
    assert len(asked) == 2


def test_anthropic_balance_is_the_recording_minus_official_cost_since_its_utc_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report starts at the earlier of the month and the recording's day."""
    asked: list[str] = []

    def answer(url: str, headers: Mapping[str, str], *, timeout_s: float) -> dict[str, Any]:
        del headers, timeout_s
        asked.append(url)

        def row(cents: int) -> list[dict[str, str]]:
            return [{"model": "claude-haiku-5-5", "amount": str(cents)}]

        return {
            "data": [
                {"starting_at": "2026-09-26T00:00:00Z", "results": row(100)},
                {"starting_at": "2026-10-01T00:00:00Z", "results": row(50)},
            ]
        }

    monkeypatch.setattr(usage_observer, "_get_json", answer)
    recorded = usage_observer.RecordedBalance(10.0, 1_790_424_000_000)  # 2026-09-26 12:00 UTC
    data = usage_observer.collect_anthropic_official(
        "k", timeout_s=1, now=dt.datetime(2026, 10, 2, 12, tzinfo=dt.UTC), balance=recorded,
    )
    assert "starting_at=2026-09-26T00%3A00%3A00Z" in asked[0]
    assert data["balance_usd"] == 8.5
    assert data["month_usd"] == 0.5
