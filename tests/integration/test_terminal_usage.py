"""ADR 0170: the usage observer's two halves, on the machine each one's credentials live on.

Claude's and Codex's quota are read with the logins Claude Code and Codex keep on the owner's
machine; the providers' keyed services (OpenAI, DeepSeek, MiniMax) are read with keys that live on
the brain. A terminal observes the first two and pushes their ``usage.state_observed`` events into
the brain's log; the brain observes the rest, and the Usage page reads the one log. The checks:
the events through the outbox are the one-machine observer's, with no login in them; a restarted
terminal continues from the brain's baseline; the brain's own observer touches no login and still
reads with its keys; and a real terminal over a real socket fills the brain's Usage page.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sqlite3
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from jarvis.runtime import bootstrap_runtime_app
from jarvis.runtime.inherent_loop import _make_usage_observer
from jarvis.runtime.terminal import _Watched
from jarvis.state.device_tokens import pair_device
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import usage_observer
from jarvis.surface.terminal_events import EventOutbox
from jarvis.surface.usage_observer import (
    DEVICE_SERVICES,
    KEYED_SERVICES,
    SERVICES,
    UsageObserver,
    latest_usage,
)
from tests.integration.test_terminal_observers import (
    _brain_client,
    _unstamped,
    _wait_for,
    rows,
)
from tests.integration.test_terminal_reads import _Brain, _Terminal

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

CLAUDE_TOKEN = "fake-claude-login-0000"  # noqa: S105 — a stand-in, never a real credential.
CODEX_TOKEN = "fake-codex-login-1111"  # noqa: S105
DEEPSEEK_KEY = "fake-deepseek-key-2222"
CLAUDE_BODY: dict[str, Any] = {
    "five_hour": {"utilization": 27.0, "resets_at": "2026-10-07T20:00:00+00:00"},
    "seven_day": {"utilization": 50.0, "resets_at": "2026-10-10T12:00:00+00:00"},
}
CODEX_BODY: dict[str, Any] = {
    "plan_type": "pro",
    "rate_limit": {"primary_window": {
        "limit_window_seconds": 18000, "used_percent": 12.0, "reset_at": 1_791_000_000,
    }},
    "rate_limit_reset_credits": {"available_count": 1},
}
DEEPSEEK_BODY: dict[str, Any] = {
    "is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "4.50"}],
}


class _Providers:
    """The three hosts the observer calls, with each request's host and credential kept."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.claude_percent = 27.0

    def get(self, url: str, headers: Mapping[str, str], *, timeout_s: float) -> dict[str, Any]:
        del timeout_s
        self.calls.append((url.split("/")[2], headers.get("Authorization", "")))
        if "anthropic" in url:
            body: dict[str, Any] = json.loads(json.dumps(CLAUDE_BODY))
            body["five_hour"]["utilization"] = self.claude_percent
            return body
        if "chatgpt" in url:
            return CODEX_BODY
        if "deepseek" in url:
            return DEEPSEEK_BODY
        msg = f"an unexpected request to {url}"
        raise AssertionError(msg)


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> _Providers:
    """This machine's Claude Code and Codex logins, and the providers answering over them."""
    fake = _Providers()
    monkeypatch.setattr(usage_observer, "_read_claude_credentials", lambda: {
        "claudeAiOauth": {"accessToken": CLAUDE_TOKEN, "rateLimitTier": "default_claude_max_20x"},
    })
    monkeypatch.setattr(usage_observer, "_codex_headers", lambda: {
        "Authorization": f"Bearer {CODEX_TOKEN}", "ChatGPT-Account-Id": "acct",
        "User-Agent": "codex-cli",
    })
    monkeypatch.setattr(usage_observer, "_get_json", fake.get)
    for key in ("OPENAI_ADMIN_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return fake


def _sent(outbox: EventOutbox) -> list[dict[str, Any]]:
    return [json.loads(text) for text in outbox._pending.values()]  # noqa: SLF001


def test_a_terminals_usage_events_are_the_one_machine_events_and_hold_no_login(
    tmp_path: Path, providers: _Providers,
) -> None:
    """A change is one event per service, an unchanged poll none; the payload is the log's."""

    async def scenario() -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]], int]:
        log = open_event_log(tmp_path / "local.db")
        local = UsageObserver(log, services=DEVICE_SERVICES)
        outbox = EventOutbox()
        remote = UsageObserver(
            open_event_log(tmp_path / "scratch.db"), services=DEVICE_SERVICES,
            emit_event=outbox.emit_event,
        )
        for observer in (local, remote, local, remote):
            observer.emit(observer.collect())
        quiet = len(_sent(outbox))
        providers.claude_percent = 31.0
        for observer in (local, remote):
            observer.emit(observer.collect())
        on_one_machine = [
            (kind, json.loads(payload)) for kind, payload in log.execute(
                "SELECT type, payload_json FROM events ORDER BY id",
            )
        ]
        return on_one_machine, _sent(outbox), quiet

    on_one_machine, sent, quiet = asyncio.run(scenario())
    assert quiet == 2  # the second, identical poll added nothing
    assert [(f["payload"]["service"], f["payload"]["status"]) for f in sent] == [
        ("claude", "ok"), ("codex", "ok"), ("claude", "ok"),
    ]
    assert [(f["event_type"], _unstamped(f["payload"])) for f in sent] == [
        (kind, _unstamped(payload)) for kind, payload in on_one_machine
    ]
    assert sent[0]["payload"]["data"]["windows"][0]["percent"] == 27.0
    assert sent[2]["payload"]["data"]["windows"][0]["percent"] == 31.0
    # The logins were used, from here, and are in no frame.
    assert {auth for _, auth in providers.calls} == {
        f"Bearer {CLAUDE_TOKEN}", f"Bearer {CODEX_TOKEN}",
    }
    text = json.dumps(sent)
    assert CLAUDE_TOKEN not in text
    assert CODEX_TOKEN not in text


def test_the_brains_own_observer_reads_no_login_and_still_reads_with_its_keys(
    tmp_path: Path, providers: _Providers, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brain collects the keyed services alone; Claude's and Codex's logins stay untouched."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", DEEPSEEK_KEY)
    asked: list[str] = []

    def no_claude() -> None:
        asked.append("claude")

    def no_codex() -> None:
        asked.append("codex")

    monkeypatch.setattr(usage_observer, "_read_claude_credentials", no_claude)
    monkeypatch.setattr(usage_observer, "_codex_headers", no_codex)
    log = open_event_log(tmp_path / "brain.db")
    observer = UsageObserver(log, services=KEYED_SERVICES)
    snapshots = observer.collect()
    assert [s.service for s in snapshots] == list(KEYED_SERVICES)
    assert asked == []
    assert providers.calls == [("api.deepseek.com", f"Bearer {DEEPSEEK_KEY}")]
    by_service = {s.service: s for s in snapshots}
    assert by_service["deepseek"].status == "ok"
    assert by_service["openai"].status == by_service["minimax"].status == "unconfigured"
    # One machine keeps all five.
    assert [s.service for s in UsageObserver(log).collect()] == list(SERVICES)


def test_a_brain_runs_the_keyed_half_and_a_one_machine_daemon_all_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``observer.usage`` is no longer forced off on a brain; it just does not read the logins."""
    monkeypatch.setenv("HOME", str(tmp_path))
    built: dict[str, Any] = {}
    for name, role in (("one", "all"), ("brain", "brain")):
        root = tmp_path / name
        root.mkdir()
        (root / "settings.yaml").write_text(
            f"runtime:\n  role: {role}\nobserver:\n  usage:\n    enabled: true\n", encoding="utf-8",
        )
        built[name] = bootstrap_runtime_app(runtime_root=root)
    try:
        for runtime in built.values():
            assert runtime.config["observer"]["usage"]["enabled"] is True
        one = _make_usage_observer(built["one"])
        brain = _make_usage_observer(built["brain"])
        assert one is not None
        assert brain is not None
        assert one._services == SERVICES  # noqa: SLF001
        assert brain._services == KEYED_SERVICES  # noqa: SLF001
    finally:
        for runtime in built.values():
            runtime.conn.close()


def test_the_brains_baseline_carries_the_latest_usage_row_of_each_service(
    tmp_path: Path,
) -> None:
    """What the terminal's observer folds its change-baseline from; no other type of row."""
    _client, hub, log = _brain_client(tmp_path)
    conn = sqlite3.connect(log)
    for service, percent in (("claude", 10.0), ("codex", 5.0), ("claude", 20.0)):
        emit_event(conn, type="usage.state_observed", payload={
            "service": service, "status": "ok", "error": None, "observed_at_ms": 1,
            "data": {"windows": [{"percent": percent}]}, "actor": "observer",
        })
    emit_event(conn, type="mac.sleeping", payload={"ts_epoch_ms": 1})
    assert hub.events is not None
    baseline = hub.events.baseline()
    assert sorted(
        (r["event_type"], r["payload"]["service"], r["payload"]["data"]["windows"][0]["percent"])
        for r in baseline
    ) == [("usage.state_observed", "claude", 20.0), ("usage.state_observed", "codex", 5.0)]


def test_a_real_terminal_fills_the_brains_usage_page_and_a_restart_repeats_nothing(
    tmp_path: Path, providers: _Providers,
) -> None:
    """Over a real socket: the page shows the terminal's reading; unchanged, nothing is re-sent."""
    token = pair_device(tmp_path, "macbook")
    log = tmp_path / "brain-events.db"
    open_event_log(log).close()
    reader = sqlite3.connect(log, check_same_thread=False)
    watched = _Watched((), 0.2, None, 0.2, usage_interval_s=0.1)
    brain = _Brain(
        tmp_path, log, deps=lambda _hub: {
            "usage_read": functools.partial(latest_usage, reader),
            "usage_refresh": _page(reader),
        },
    )
    with brain:
        with _Terminal(brain.url, token, store=None, repos=(), watched=watched):
            _wait_for(
                lambda: len(rows(log, "usage.state_observed")) >= len(DEVICE_SERVICES),
                "the first readings never arrived",
            )
        page = httpx.get(
            f"{brain.url}/inherent/usage", headers={"Authorization": f"Bearer {token}"},
        ).json()["services"]
        assert {name: row["status"] for name, row in page.items()} == {
            "claude": "ok", "codex": "ok",
        }
        assert [w["percent"] for w in page["claude"]["data"]["windows"]] == [27.0, 50.0]
        assert page["codex"]["data"]["plan"]
        assert {node for _, node, _ in rows(log, "usage.state_observed")} == {"macbook"}

        # A restarted terminal asks the brain what it last heard and re-sends none of it.
        with _Terminal(brain.url, token, store=None, repos=(), watched=watched):
            before = len(providers.calls)
            _wait_for(lambda: len(providers.calls) >= before + 4, "no polls after the restart")
            providers.claude_percent = 33.0
            _wait_for(
                lambda: len(rows(log, "usage.state_observed")) == len(DEVICE_SERVICES) + 1,
                "the changed reading never arrived",
            )
            time.sleep(0.5)
    found = rows(log, "usage.state_observed")
    assert [(p["service"], p["data"]["windows"][0]["percent"]) for _, _, p in found] == [
        ("claude", 27.0), ("codex", 12.0), ("claude", 33.0),
    ]
    reader.close()


def _page(conn: sqlite3.Connection) -> Any:  # noqa: ANN401
    async def refresh() -> dict[str, Any]:
        return latest_usage(conn)

    return refresh
