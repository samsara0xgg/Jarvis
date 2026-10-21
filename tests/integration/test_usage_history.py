"""The day pane's quota bar: ``GET /inherent/usage/history``, Claude's and Codex's windows.

Contract checks on the route's payload, read from the log the real observer writes. Only the
providers' hosts and logins are stood in for, and time is the test's own clock, so every reading is
observed at the minute the test says. The checks:

- a span answers each service that has windows, oldest reading first, led by the last reading
  before the span; a poll that failed stays in the list with no windows, and a service without
  windows (Anthropic's spend, DeepSeek's balance, a key that is not set) never shows;
- a reading belongs to the span by when it was observed, both ends counted, however soon after the
  observation the log stamped it; a lead older than a week is not given;
- an end that is missing or not whole milliseconds, a span that ends before it starts or runs past
  48 hours is a 400 and reads nothing;
- the route is behind the guard every route is, and is absent where nothing observes usage.
"""

from __future__ import annotations

import functools
import urllib.error
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Self

import pytest
from fastapi.testclient import TestClient

from jarvis.shared.lang import t
from jarvis.state import event_log
from jarvis.surface import usage_observer
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.usage_observer import UsageObserver, UsageSnapshot, usage_history
from tests.support.clock import DEFAULT_ZONE, FakeClock
from tests.support.terminal import brain_log

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from fastapi import FastAPI
    from httpx2 import Response  # what the test client answers with

PATH = "/inherent/usage/history"
HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS
LOCAL_KEY = "local-key-for-this-test"
CLAUDE_LOGIN = {
    "claudeAiOauth": {"accessToken": "stand-in", "rateLimitTier": "default_claude_max_5x"},
}
FULL_WIDTH = str.maketrans("0123456789", "".join(chr(0xFF10 + digit) for digit in range(10)))

# Five-hour windows end at these moments (UTC); the owner's day is October 14th in Vancouver.
EVENING_END = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
MORNING_END = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
MIDDAY_END = datetime(2026, 10, 14, 21, 0, tzinfo=UTC)
NEXT_DAY_END = datetime(2026, 10, 15, 13, 0, tzinfo=UTC)
CODEX_END = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
WEEK_END = datetime(2026, 10, 19, 19, 0, tzinfo=UTC)
SEVEN_DAY_PERCENT = 20.0


def pdt(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """A moment of October 2026 on the owner's clock."""
    return datetime(2026, 10, day, hour, minute, second, tzinfo=DEFAULT_ZONE)


def ms(moment: datetime) -> int:
    """The event log's unit."""
    return round(moment.timestamp() * 1000)


def five_hour(percent: float, ends: datetime) -> dict[str, Any]:
    """The five-hour window of a reading, as the route gives it (Claude's key; Codex's differs)."""
    label = t("usage.window_hours", hours=5)
    return {"key": "five_hour", "label": label, "percent": percent, "resets_at": ends.isoformat()}


def seven_day() -> dict[str, Any]:
    """Claude's weekly window, which every answer of the stand-in carries."""
    return {
        "key": "seven_day", "label": t("usage.window_week_total"), "percent": SEVEN_DAY_PERCENT,
        "resets_at": WEEK_END.isoformat(),
    }


def claude_at(moment: datetime, percent: float, ends: datetime) -> dict[str, Any]:
    """One good Claude reading in the route's shape."""
    return {
        "at_ms": ms(moment), "status": "ok", "windows": [five_hour(percent, ends), seven_day()],
    }


def codex_at(moment: datetime, percent: float, ends: datetime) -> dict[str, Any]:
    """One good Codex reading in the route's shape."""
    window = {**five_hour(percent, ends), "key": "primary_window"}
    return {"at_ms": ms(moment), "status": "ok", "windows": [window]}


def _app(read: Callable[[int, int], dict[str, Any]] | None) -> FastAPI:
    """The daemon's app with ``read`` as the usage history, or without one."""
    return create_app(
        InherentDeps(
            submit_callable=lambda _text: None,
            broadcaster=InherentBroadcaster(),
            usage_history=read,
        )
    )


class _Reads:
    """A stand-in for the log that keeps what it was asked and answers that nothing was seen."""

    def __init__(self) -> None:
        self.asked: list[tuple[int, int]] = []

    def __call__(self, from_ms: int, to_ms: int) -> dict[str, Any]:
        self.asked.append((from_ms, to_ms))
        return {"services": {}}


class _Later:
    """A clock a number of seconds ahead of another: what stamps a row after it was observed."""

    def __init__(self, clock: FakeClock, seconds: float) -> None:
        self._clock, self.seconds = clock, seconds

    def time(self) -> float:
        return self._clock.time() + self.seconds

    def monotonic(self) -> float:
        return self._clock.monotonic()


class World:
    """One daemon's usage log: the real observer polling stand-in providers, and the route."""

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, stamp_delay_s: float = 0.0,
    ) -> None:
        """Start the evening before the day; the log stamps each row ``stamp_delay_s`` late."""
        self.clock = FakeClock(pdt(13, 22))
        monkeypatch.setattr(usage_observer, "time", self.clock)
        self.late = _Later(self.clock, stamp_delay_s)
        monkeypatch.setattr(event_log, "time", self.late)
        monkeypatch.setattr(usage_observer, "_read_claude_credentials", lambda: CLAUDE_LOGIN)
        monkeypatch.setattr(usage_observer, "_codex_headers", lambda: {"Authorization": "x"})
        monkeypatch.setattr(usage_observer, "_get_json", self._provider)
        for name in (
            "OPENAI_ADMIN_KEY", "MINIMAX_API_KEY", "ANTHROPIC_ADMIN_KEY", "ANTHROPIC_API_KEY",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "stand-in")
        # What the providers answer on the next poll: (percent, end of its window); ``None`` fails.
        self.claude: tuple[float, datetime] | None = (50.0, EVENING_END)
        self.codex: tuple[float, datetime] = (10.0, CODEX_END)
        self.deepseek = "4.50"
        self.conn = brain_log(tmp_path / "events.db")
        self.observer = UsageObserver(
            self.conn, services=("claude", "codex", "openai", "deepseek", "minimax"),
        )
        self.client: TestClient | None = None

    def __enter__(self) -> Self:
        """Serve the route over the log."""
        self.client = TestClient(_app(functools.partial(usage_history, self.conn))).__enter__()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop serving and close the log."""
        assert self.client is not None
        self.client.__exit__(None, None, None)
        self.conn.close()

    def _provider(self, url: str, headers: object, *, timeout_s: float) -> dict[str, Any]:
        """The three hosts the observer calls here."""
        del headers, timeout_s
        host = url.split("/")[2]
        if host == "api.anthropic.com":
            if self.claude is None:
                reason = "unreachable"
                raise urllib.error.URLError(reason)
            percent, ends = self.claude
            return {
                "five_hour": {"utilization": percent, "resets_at": ends.isoformat()},
                "seven_day": {"utilization": SEVEN_DAY_PERCENT, "resets_at": WEEK_END.isoformat()},
            }
        if host == "chatgpt.com":
            percent, ends = self.codex
            window = {
                "limit_window_seconds": 18000, "used_percent": percent,
                "reset_at": ends.timestamp(),
            }
            return {"plan_type": "pro", "rate_limit": {"primary_window": window}}
        if host == "api.deepseek.com":
            balance = {"currency": "USD", "total_balance": self.deepseek}
            return {"is_available": True, "balance_infos": [balance]}
        msg = f"an unexpected request to {url}"
        raise AssertionError(msg)

    def poll(self, when: datetime) -> None:
        """Move the clock to ``when`` and poll every provider; a changed one adds a row."""
        self.clock.set(when)
        self.observer.poll_once()

    def spend(self, when: datetime, usd: float) -> None:
        """The brain's own Anthropic spend, a service that keeps changing and has no windows."""
        self.clock.set(when)
        self.observer.emit([UsageSnapshot("anthropic", "ok", {"today_usd": usd})])

    def history(self, from_ms: int, to_ms: int) -> Response:
        """``GET /inherent/usage/history`` for the span ``from_ms`` to ``to_ms``."""
        assert self.client is not None
        return self.client.get(PATH, params={"from_ms": from_ms, "to_ms": to_ms})


def test_the_bar_gets_each_windowed_service_oldest_first_led_by_the_last_reading_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The day's readings, the evening's last reading to lead, a failed poll kept, no keyed rows."""
    with World(tmp_path, monkeypatch) as world:
        world.claude, world.codex = (40.0, EVENING_END), (10.0, CODEX_END)
        world.poll(pdt(13, 22))  # every service reads once, the evening before
        world.claude = (55.0, EVENING_END)
        world.poll(pdt(13, 23, 30))
        world.claude = (70.0, EVENING_END)
        world.poll(pdt(14, 0, 30))  # the day opens at midnight
        world.spend(pdt(14, 1), 0.25)
        world.claude = (5.0, MORNING_END)
        world.poll(pdt(14, 3, 10))
        world.claude, world.codex, world.deepseek = (30.0, MIDDAY_END), (25.0, MIDDAY_END), "4.00"
        world.poll(pdt(14, 9))
        world.claude = None
        world.poll(pdt(14, 11))  # Claude does not answer
        world.claude = (45.0, MIDDAY_END)
        world.poll(pdt(14, 12))
        world.spend(pdt(14, 13), 0.5)
        world.claude = (90.0, NEXT_DAY_END)
        world.poll(pdt(15, 3))  # after the day's end

        answer = world.history(ms(pdt(14, 0)), ms(pdt(15, 0)))

    assert answer.status_code == 200
    assert answer.json() == {
        "services": {
            "claude": [
                claude_at(pdt(13, 23, 30), 55.0, EVENING_END),
                claude_at(pdt(14, 0, 30), 70.0, EVENING_END),
                claude_at(pdt(14, 3, 10), 5.0, MORNING_END),
                claude_at(pdt(14, 9), 30.0, MIDDAY_END),
                {"at_ms": ms(pdt(14, 11)), "status": "error", "windows": []},
                claude_at(pdt(14, 12), 45.0, MIDDAY_END),
            ],
            "codex": [
                codex_at(pdt(13, 22), 10.0, CODEX_END),
                codex_at(pdt(14, 9), 25.0, MIDDAY_END),
            ],
        },
    }


def test_a_span_with_no_reading_of_a_windowed_service_answers_no_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing observed, or only services without windows, is an empty answer and not an error."""
    with World(tmp_path, monkeypatch) as world:
        empty = world.history(ms(pdt(14, 0)), ms(pdt(15, 0)))
        world.spend(pdt(14, 1), 0.25)
        only_spend = world.history(ms(pdt(14, 0)), ms(pdt(15, 0)))

    assert (empty.status_code, empty.json()) == (200, {"services": {}})
    assert (only_spend.status_code, only_spend.json()) == (200, {"services": {}})


@pytest.mark.parametrize("stamp_delay_s", [0, 3])
def test_a_reading_belongs_to_the_span_by_when_it_was_observed_both_ends_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamp_delay_s: int,
) -> None:
    """A second either side of an end falls out, and a log that stamps late changes nothing."""
    start, end = pdt(14, 6), pdt(14, 18)
    with World(tmp_path, monkeypatch, stamp_delay_s=stamp_delay_s) as world:
        for when, percent in (
            (start - timedelta(seconds=1), 10.0),  # the lead
            (start, 20.0),  # the span's first moment
            (end, 30.0),  # its last
            (end + timedelta(seconds=1), 40.0),  # after it
        ):
            world.claude = (percent, MIDDAY_END)
            world.poll(when)

        answer = world.history(ms(start), ms(end))

    claude = answer.json()["services"]["claude"]
    assert [(row["at_ms"] - ms(start), row["windows"][0]["percent"]) for row in claude] == [
        (-1000, 10.0), (0, 20.0), (12 * HOUR_MS, 30.0),
    ]


def test_readings_are_oldest_first_by_when_observed_whatever_order_the_log_stamped_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow stamp on one row must not put a later reading ahead of an earlier one."""
    with World(tmp_path, monkeypatch) as world:
        world.late.seconds = 30
        world.claude = (10.0, MIDDAY_END)
        world.poll(pdt(14, 6))  # stamped half a minute after it was observed
        world.late.seconds = 0
        world.claude = (20.0, MIDDAY_END)
        world.poll(pdt(14, 6, 0) + timedelta(seconds=10))  # stamped before the first one was

        answer = world.history(ms(pdt(14, 5)), ms(pdt(14, 7)))

    claude = answer.json()["services"]["claude"]
    assert [row["windows"][0]["percent"] for row in claude] == [10.0, 20.0]


def test_the_read_searches_the_log_by_type_and_time_and_never_walks_the_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The log only grows and the read runs on the loop thread, so it may not scan the table."""
    with World(tmp_path, monkeypatch) as world:
        world.poll(pdt(13, 22))
        asked: list[str] = []
        world.conn.set_trace_callback(asked.append)
        world.history(ms(pdt(14, 0)), ms(pdt(15, 0)))
        world.conn.set_trace_callback(None)
        plan = [
            str(row[3])
            for sql in asked
            if sql.lstrip().upper().startswith("SELECT")
            for row in world.conn.execute(f"EXPLAIN QUERY PLAN {sql}")
        ]

    assert [line for line in plan if line.startswith("SCAN ")] == []
    assert any("ts_epoch_ms>" in line and "ts_epoch_ms<" in line for line in plan), plan


@pytest.mark.parametrize(
    ("before", "led"),
    [
        pytest.param(timedelta(days=6), True, id="six days"),
        pytest.param(timedelta(days=7), True, id="exactly a week"),
        pytest.param(timedelta(days=7, seconds=1), False, id="a second over a week"),
    ],
)
def test_a_lead_observed_over_a_week_before_the_span_is_not_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: timedelta, *, led: bool,
) -> None:
    """Every window ends within seven days, so an older reading holds none still open."""
    start = pdt(14, 0)
    with World(tmp_path, monkeypatch) as world:
        world.poll(start - before)

        answer = world.history(ms(start), ms(pdt(15, 0)))

    assert answer.status_code == 200
    assert set(answer.json()["services"]) == ({"claude", "codex"} if led else set())


def test_a_failed_poll_before_the_span_leads_it_and_the_service_stays_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The level at the start is that the quota is unknown, which the bar needs to say."""
    with World(tmp_path, monkeypatch) as world:
        world.poll(pdt(13, 22))
        world.claude = None
        world.poll(pdt(13, 23))  # Claude stops answering, and nothing changes after

        answer = world.history(ms(pdt(14, 0)), ms(pdt(15, 0)))

    assert answer.json() == {
        "services": {
            "claude": [{"at_ms": ms(pdt(13, 23)), "status": "error", "windows": []}],
            "codex": [codex_at(pdt(13, 22), 10.0, CODEX_END)],
        },
    }


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({}, id="neither end"),
        pytest.param({"from_ms": "1"}, id="no to_ms"),
        pytest.param({"to_ms": "1"}, id="no from_ms"),
        pytest.param({"from_ms": "", "to_ms": "1"}, id="an empty end"),
        pytest.param({"from_ms": "abc", "to_ms": "1"}, id="text"),
        pytest.param({"from_ms": "1.5", "to_ms": "2"}, id="a fraction"),
        pytest.param({"from_ms": "1", "to_ms": "1e3"}, id="an exponent"),
        pytest.param({"from_ms": "-1", "to_ms": "5"}, id="a negative end"),
        pytest.param({"from_ms": "0x10", "to_ms": "32"}, id="hex"),
        pytest.param({"from_ms": " 1", "to_ms": "2"}, id="a space"),
        pytest.param({"from_ms": "1_000", "to_ms": "2000"}, id="an underscore"),
        pytest.param(
            {"from_ms": "12".translate(FULL_WIDTH), "to_ms": "34".translate(FULL_WIDTH)},
            id="full-width digits",
        ),
        pytest.param({"from_ms": "9" * 16, "to_ms": "9" * 16}, id="more digits than a clock has"),
        pytest.param({"from_ms": "5", "to_ms": "4"}, id="ending before it starts"),
        pytest.param({"from_ms": "0", "to_ms": str(48 * HOUR_MS + 1)}, id="a millisecond too long"),
    ],
)
def test_a_missing_malformed_reversed_or_overlong_span_is_a_400_and_reads_nothing(
    params: dict[str, str],
) -> None:
    """The caller's mistake is said, and the log is never asked."""
    reads = _Reads()
    with TestClient(_app(reads)) as client:
        answer = client.get(PATH, params=params)

    assert answer.status_code == 400
    assert isinstance(answer.json()["detail"], str)
    assert reads.asked == []


@pytest.mark.parametrize(
    ("from_ms", "to_ms"),
    [
        pytest.param("5", "5", id="an instant"),
        pytest.param("0", str(48 * HOUR_MS), id="exactly 48 hours"),
        pytest.param("007", "00008", id="leading zeros"),
        pytest.param("1900000000000", str(1_900_000_000_000 + DAY_MS), id="a span in the future"),
    ],
)
def test_a_whole_span_of_at_most_48_hours_is_read_as_asked(from_ms: str, to_ms: str) -> None:
    """Both ends reach the read as the numbers they spell."""
    reads = _Reads()
    with TestClient(_app(reads)) as client:
        answer = client.get(PATH, params={"from_ms": from_ms, "to_ms": to_ms})

    assert (answer.status_code, answer.json()) == (200, {"services": {}})
    assert reads.asked == [(int(from_ms), int(to_ms))]


def test_the_route_is_behind_the_guard_every_route_is() -> None:
    """Without the local key it is a 401; with it, a 200."""
    app = _app(_Reads())
    require_local_key(app, lambda header: header == f"Bearer {LOCAL_KEY}")
    span = {"from_ms": 1, "to_ms": 2}
    with TestClient(app, base_url="http://127.0.0.1:8006", client=("127.0.0.1", 50000)) as client:
        keyless = client.get(PATH, params=span)
        wrong = client.get(PATH, params=span, headers={"Authorization": "Bearer x"})
        right = client.get(PATH, params=span, headers={"Authorization": f"Bearer {LOCAL_KEY}"})

    assert (keyless.status_code, wrong.status_code, right.status_code) == (401, 401, 200)


def test_where_nothing_observes_usage_there_is_no_route() -> None:
    """Without the read the daemon never registers it, as it does not register the latest read."""
    with TestClient(_app(None)) as client:
        assert client.get(PATH, params={"from_ms": 1, "to_ms": 2}).status_code == 404
