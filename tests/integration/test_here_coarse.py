"""ADR 0356: roughly where he is, for a choice a few kilometres do not change.

A table over a real event log and a fake clock: the last report of the turn's device up to a day
old, else home, else nothing; and the read asks no phone and leaves no reading on the call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from jarvis.runtime.locate import HerePolicy, Locator
from jarvis.shared.here import Coarse
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.locate_wait import LocateWaits
from tests.support.clock import FakeClock

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 21, 9, 30, tzinfo=UTC)
HOUR_MS = 3_600_000
HOME = (48.43, -123.37)
PHONE = (48.50, -123.50)
IPAD = (49.28, -123.12)


def _log(path: Path, reports: list[tuple[str, float, tuple[float, float]]]) -> Path:
    """An event log holding one location report per ``(device, hours ago, point)``."""
    conn = open_event_log(path)
    now_ms = int(NOW.timestamp() * 1000)
    for device, hours_ago, (lat, lng) in reports:
        emit_event(
            conn, type="phone.location_observed", ingestion_node=device,
            ts_epoch_ms=now_ms - int(hours_ago * HOUR_MS),
            payload={"lat": lat, "lng": lng, "accuracy_m": 40.0},
        )
    conn.close()
    return path


@pytest.mark.parametrize(
    ("reports", "home", "expected"),
    [
        pytest.param([("iphone", 2, PHONE)], HOME, Coarse(*PHONE), id="a report from this morning"),
        pytest.param([("iphone", 23.9, PHONE)], HOME, Coarse(*PHONE), id="a report 23.9 h old"),
        pytest.param(
            [("iphone", 24.1, PHONE)], HOME, Coarse(*HOME, home=True), id="past a day it is home",
        ),
        pytest.param([], HOME, Coarse(*HOME, home=True), id="no report is home"),
        pytest.param([], None, None, id="no report and no home is nothing"),
        pytest.param([("iphone", 30, PHONE)], None, None, id="an old report and no home"),
        pytest.param(
            [("iphone", 5, PHONE), ("iphone", 1, IPAD)], HOME, Coarse(*IPAD), id="the latest wins",
        ),
        pytest.param(
            [("iphone", 3, PHONE), ("ipad", 1, IPAD)], HOME, Coarse(*IPAD),
            id="the newest report of any phone, when none is named",
        ),
    ],
)
def test_the_last_report_within_a_day_else_home(
    tmp_path: Path, reports: list[tuple[str, float, tuple[float, float]]],
    home: tuple[float, float] | None, expected: Coarse | None,
) -> None:
    """A report up to a day old says roughly where he is; past that it is home, else nothing."""
    log = _log(tmp_path / "events.db", reports)
    here = HerePolicy(log, FakeClock(NOW), home=home).bind(None)
    assert here.coarse() == expected


def test_it_asks_no_phone_and_leaves_no_reading_on_the_call(tmp_path: Path) -> None:
    """A stale report is not worth a fix here: the phone is never asked and ``used`` stays empty."""
    clock = FakeClock(NOW)
    asked: list[str | None] = []

    def ask(_request_id: str, device: str | None) -> int:
        asked.append(device)
        return 1

    locator = Locator(LocateWaits(), clock)
    locator.ask_socket = locator.ask_push = ask
    log = _log(tmp_path / "events.db", [("iphone", 20, PHONE)])
    here = HerePolicy(log, clock, locator=locator, home=HOME).bind(None)
    assert here.coarse() == Coarse(*PHONE)
    assert asked == []
    assert here.used is None


def test_a_log_that_cannot_be_read_leaves_home(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The hint is never worth failing a call: an unreadable log says home, and why in the log."""
    broken = tmp_path / "events.db"
    broken.write_text("this is not a database")
    here = HerePolicy(broken, FakeClock(NOW), home=HOME).bind(None)
    with caplog.at_level("WARNING"):
        assert here.coarse() == Coarse(*HOME, home=True)
    assert [r.getMessage() for r in caplog.records] == [
        "here: no coarse position from the log (DatabaseError)",
    ]
