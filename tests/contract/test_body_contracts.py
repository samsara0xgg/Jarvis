"""``GET /inherent/body``: his body over a window beside his usual level then (ADR 0411).

Two windows of the ``body`` tool's invented fortnight (``tests/support/body.py``), read by the real
route as a paired phone reads them: 2 pm to past now, which ends now, and today so far.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

from jarvis.runtime.daemon.routes import serve_body
from tests.support.body import NOW, TODAY, ZONE, epoch_ms, fortnight, local_at
from tests.support.timeline import peers_with

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.contracts import Contracts

ROUTE = "GET /inherent/body"
NOW_MS = epoch_ms(NOW)


def test_his_body_over_a_window_beside_his_usual_level(
    tmp_path: Path, contracts: Contracts,
) -> None:
    """Each measure's value, its usual level for those hours, and where it stands against it."""
    log = tmp_path / "events.db"
    fortnight().write(log)
    peers = peers_with(tmp_path, body_read=functools.partial(serve_body, log, ZONE,
                                                             clock=NOW.timestamp))

    def served(start_ms: int, end_ms: int) -> dict[str, Any]:
        answer = peers.paired.get(
            "/inherent/body", params={"start_ms": start_ms, "end_ms": end_ms},
        )
        assert answer.status_code == 200, answer.text
        body: dict[str, Any] = answer.json()
        return body

    afternoon = served(epoch_ms(local_at(TODAY, 14)), NOW_MS + 3_600_000)
    today = served(epoch_ms(local_at(TODAY, 0)), NOW_MS)

    contracts.output(
        "body/window",
        examples={"afternoon": afternoon, "today": today},
        exact=("kind", "tz", "unit", "vs_usual"),
        now_ms=NOW_MS, route=ROUTE,
        description=(
            "A few measures of his body over `[start_ms, end_ms)` (at most 25 hours; one that "
            "runs past now ends now, and `end_ms` says so), each beside his usual level for the "
            "same clock hours (`baseline`: the days before, in the owner's zone `tz`). "
            "`measures` maps a name to `{kind, unit, value, usual: {median, low, high, days}, "
            "vs_usual}`: `value` is null when nothing was recorded in the window, the usual "
            "numbers are null when fewer than `baseline.min_days` days hold any, and "
            "`vs_usual` (below, within or above the middle half `low`..`high`) is null without "
            "both. A reading (heart rates, HRV, breathing, blood oxygen) is the median of the "
            "readings, with their count `readings`, `low` and `high`; a total (steps) is the "
            "sum of every hour the window touches; sleep is the minutes asleep of the sleeps "
            "he woke from in the window, with their count `sleeps`. A window that is empty, "
            "has not begun or is longer is a 400. A client keeps what it does not know: new "
            "measures come."
        ),
    )

    assert afternoon["end_ms"] == NOW_MS
    assert afternoon["measures"]["heart_rate"]["vs_usual"] == "above"
    assert today["measures"]["sleep"]["value"] == 420
