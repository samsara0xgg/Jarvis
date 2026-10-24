"""ADR 0411: ``GET /inherent/body``, his body over a window beside his usual level then.

Acceptance checks through the real app and the real reader, on the ``body`` tool's invented
fortnight (``tests/support/body.py``), as a paired phone asks:

- 2 pm to now: heart rate and steps are the numbers the ``body`` tool gives for the same hours,
  beside the same usual level (median, middle half, days), and a window past now ends now;
- today so far: the night he woke from beside the nights before, a measure too new to have a
  usual level, and a measure nothing recorded;
- a window across midnight, 10 pm to 8 am, sets his night beside the same hours of the nights
  before;
- an empty, future or longer-than-a-day window is a 400; missing or negative times a 422; a peer
  without a token is refused; a daemon without the reader has no route.
"""

from __future__ import annotations

import functools
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.execution.body_tool import build_body_tool
from jarvis.runtime.daemon.routes import serve_body
from jarvis.state.body_usual import BASELINE
from tests.support.body import NOW, TODAY, ZONE, epoch_ms, fortnight, local_at
from tests.support.clock import FakeClock
from tests.support.timeline import Peers, peers_with
from tests.support.tool_context import tool_context

if TYPE_CHECKING:
    from pathlib import Path

NOW_MS = epoch_ms(NOW)
HOUR_MS = 3_600_000
PATH = "/inherent/body"
NOTHING = {"median": None, "low": None, "high": None}


@pytest.fixture(scope="module")
def log(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fortnight's log, written once for the module."""
    path = tmp_path_factory.mktemp("body-route") / "events.db"
    fortnight().write(path)
    return path


@pytest.fixture
def peers(tmp_path: Path, log: Path) -> Peers:
    """The route wired as the runtime wires it, at the fortnight's clock."""
    read = functools.partial(serve_body, log, ZONE, clock=NOW.timestamp)
    return peers_with(tmp_path, body_read=read)


def _body(peers: Peers, start_ms: int, end_ms: int) -> dict[str, Any]:
    answer = peers.paired.get(PATH, params={"start_ms": start_ms, "end_ms": end_ms})
    assert answer.status_code == 200, answer.text
    body: dict[str, Any] = answer.json()
    return body


def test_two_pm_to_now_is_what_the_body_tool_says_of_those_hours(peers: Peers, log: Path) -> None:
    """Heart rate and steps, value and usual level alike, and the window ends now."""
    served = _body(peers, epoch_ms(local_at(TODAY, 14)), NOW_MS + HOUR_MS)
    (tool,) = build_body_tool(log, ZONE)
    said = tool.handler(
        {"measures": ["heart_rate", "steps"], "from": "14:00", "until": "now"},
        tool_context(clock=FakeClock(NOW)),
    )

    assert (served["tz"], served["end_ms"], served["now_ms"]) == ("America/Vancouver", NOW_MS,
                                                                  NOW_MS)
    assert served["baseline"] == BASELINE
    heart, steps = served["measures"]["heart_rate"], served["measures"]["steps"]
    assert heart == {
        "kind": "reading", "unit": "bpm", "value": 90, "readings": 36, "low": 84, "high": 96,
        "usual": {"median": 74, "low": 71, "high": 77, "days": 14}, "vs_usual": "above",
    }
    told = said["heart_rate"]
    assert (told["median"], told["readings"], told["low"], told["high"]) == (90, 36, 84, 96)
    assert told["usual"]["median"] == heart["usual"]["median"]
    assert told["usual"]["middle_half"] == "71 to 77 bpm"
    assert told["vs_usual"] == heart["vs_usual"]
    # The days before by 5:36 pm: three whole hours of 500, and 36 minutes of the hour now fills.
    assert steps == {
        "kind": "total", "unit": "steps", "value": 900,
        "usual": {"median": 1800, "low": 1800, "high": 1800, "days": 14}, "vs_usual": "below",
    }
    assert (said["steps"]["total"], said["steps"]["usual"]["median"]) == (900, 1800)
    assert served["measures"]["sleep"] == {
        "kind": "sleep", "unit": "min", "value": None, "sleeps": 0,
        "usual": {**NOTHING, "days": 0}, "vs_usual": None,
    }


def test_today_so_far_sets_his_night_beside_the_nights_before(peers: Peers) -> None:
    """7h against 7h 30m; a resting heart rate with no days before; no HRV at all."""
    served = _body(peers, epoch_ms(local_at(TODAY, 0)), NOW_MS)
    measures = served["measures"]
    assert list(measures) == [
        "steps", "heart_rate", "resting_heart_rate", "hrv", "breathing_rate", "blood_oxygen",
        "sleep",
    ]
    assert measures["sleep"] == {
        "kind": "sleep", "unit": "min", "value": 420, "sleeps": 1,
        "usual": {"median": 450, "low": 450, "high": 450, "days": 14}, "vs_usual": "below",
    }
    assert measures["resting_heart_rate"] == {
        "kind": "reading", "unit": "bpm", "value": 55, "readings": 1, "low": 55, "high": 55,
        "usual": {**NOTHING, "days": 0}, "vs_usual": None,
    }
    assert measures["hrv"] == {
        "kind": "reading", "unit": "ms", "value": None, "readings": 0, "low": None, "high": None,
        "usual": {**NOTHING, "days": 0}, "vs_usual": None,
    }
    assert measures["steps"]["value"] == 2500  # eight hours of 300, and 100 for 9 am


def test_a_window_across_midnight_is_set_beside_the_same_hours_of_the_nights_before(
    peers: Peers,
) -> None:
    """10 pm yesterday to 8 am: the night he woke from at 6:40, and each night before woke at 7."""
    yesterday = TODAY - timedelta(days=1)
    served = _body(peers, epoch_ms(local_at(yesterday, 22)), epoch_ms(local_at(TODAY, 8)))
    sleep = served["measures"]["sleep"]
    assert (sleep["value"], sleep["usual"]["median"], sleep["usual"]["days"]) == (420, 450, 14)
    assert served["measures"]["heart_rate"]["value"] is None


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "status"),
    [
        (NOW_MS - HOUR_MS, NOW_MS - HOUR_MS, 400),  # empty
        (NOW_MS, NOW_MS - HOUR_MS, 400),  # backwards
        (NOW_MS + HOUR_MS, NOW_MS + 2 * HOUR_MS, 400),  # not begun
        (NOW_MS - 26 * HOUR_MS, NOW_MS, 400),  # longer than a day
        (-1, NOW_MS, 422),
        (None, NOW_MS, 422),
        (NOW_MS - HOUR_MS, None, 422),
    ],
)
def test_a_window_it_cannot_read_is_refused(
    peers: Peers, start_ms: int | None, end_ms: int | None, status: int,
) -> None:
    """Nothing is read for a window that is not one; a time that is not one is FastAPI's."""
    params = {k: v for k, v in (("start_ms", start_ms), ("end_ms", end_ms)) if v is not None}
    assert peers.paired.get(PATH, params=params).status_code == status


def test_only_his_devices_reach_it_and_a_daemon_without_the_reader_has_none(
    peers: Peers, tmp_path: Path,
) -> None:
    """The local key and a paired token read it; no credential is 401; no reader is no route."""
    params = {"start_ms": NOW_MS - HOUR_MS, "end_ms": NOW_MS}
    assert peers.local.get(PATH, params=params).status_code == 200
    assert peers.bare_remote.get(PATH, params=params).status_code == 401
    assert peers.bare_local.get(PATH, params=params).status_code == 401
    bare = tmp_path / "bare"
    bare.mkdir()
    assert peers_with(bare).paired.get(PATH, params=params).status_code == 404
