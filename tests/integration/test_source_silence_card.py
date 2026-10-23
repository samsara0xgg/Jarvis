"""ADR 0381: the card a broken data source raises, and clears when it arrives again.

The worlds are the ones ``test_source_silence`` judges (TimeSink's copy through the brain's own
intake, recording pieces filed by the archive's naming); the card is the job-alert row the spend
card uses, read back through ``alerts_for_client`` and the notice routes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from jarvis.runtime.daemon.routes import alert_deps, notice_deps
from jarvis.runtime.source_silence import SourceSilenceWatch
from jarvis.shared import lang
from jarvis.state import job_ledger, source_silence
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps
from tests.support.check_log import MIN, NOW
from tests.support.source_silence import DAY, HOUR, MacWorld, window_start

if TYPE_CHECKING:
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
NOW_DT = datetime.fromtimestamp(NOW / 1000, tz=UTC)
OCR, MIC = source_silence.OCR, source_silence.MIC


def _ago(*hours: float) -> tuple[int, int]:
    """The span of time from ``hours[0]`` hours before the check to ``hours[1]`` hours before."""
    return NOW - round(hours[0] * HOUR), NOW - round(hours[1] * HOUR)


def _world(root: Path, **kwargs: Any) -> MacWorld:  # noqa: ANN401 - MacWorld.live's keywords.
    world = MacWorld(root)
    world.live(window_start(), NOW, **kwargs)
    world.ship()
    return world



def _watch(root: Path) -> SourceSilenceWatch:
    return SourceSilenceWatch(root, root / "lifelog", root / "memory.db", ZONE)


def _cards(root: Path, quiet: str = "off") -> list[dict[str, Any]]:
    return job_ledger.alerts_for_client(root / "memory.db", quiet, NOW_DT)


def test_a_silent_source_raises_one_card_naming_it_and_since_when(tmp_path: Path) -> None:
    """One card for the source that broke, silent at every quiet level a card is shown at."""
    _world(tmp_path, failing=[_ago(5, 0)])
    watch = _watch(tmp_path)
    assert watch.check(NOW_DT) == [OCR]
    (card,) = _cards(tmp_path)
    assert card["title"] == lang.t(
        "source.silence.title", source=lang.t(f"source.silence.{OCR}"),
    )
    last_success = datetime.fromtimestamp((NOW - 5 * HOUR - 10 * MIN) / 1000, tz=UTC)
    assert card["line"] == lang.t(
        "source.silence.line", since=last_success.astimezone(ZONE).strftime("%m-%d %H:%M"),
    )
    assert card["level"] == "card"
    assert _cards(tmp_path, "dnd") == []  # held like any alert


def test_a_source_that_stays_silent_does_not_raise_a_second_card(tmp_path: Path) -> None:
    """Asking again, or after a restart, finds the open card and adds nothing."""
    _world(tmp_path, failing=[_ago(5, 0)])
    assert _watch(tmp_path).check(NOW_DT) == [OCR]
    assert _watch(tmp_path).check(NOW_DT) == []
    job_ledger.mark_alert(tmp_path / "memory.db", _cards(tmp_path)[0]["id"], "shown", NOW_DT)
    assert _watch(tmp_path).check(NOW_DT) == []  # seen, still open: no nag
    assert len(_cards(tmp_path)) == 0


def test_a_card_is_cleared_when_the_source_arrives_again(tmp_path: Path) -> None:
    """Recovery takes the waiting card down; nothing is left for him to dismiss."""
    world = _world(tmp_path, failing=[_ago(5, 0)])
    watch = _watch(tmp_path)
    watch.check(NOW_DT)
    assert len(_cards(tmp_path)) == 1
    world.ocr(NOW - 20 * MIN)  # screen text worked again
    world.ship()
    assert watch.check(NOW_DT) == []
    assert _cards(tmp_path) == []


def test_a_second_break_after_a_recovery_makes_a_second_card(tmp_path: Path) -> None:
    """The open card of the first break was closed, so the next one is new, even if he saw it."""
    world = _world(tmp_path, failing=[_ago(5, 0)])
    watch = _watch(tmp_path)
    watch.check(NOW_DT)
    job_ledger.mark_alert(tmp_path / "memory.db", _cards(tmp_path)[0]["id"], "shown", NOW_DT)
    world.ocr(NOW - 20 * MIN)
    world.ship()
    assert watch.check(NOW_DT) == []
    world.live(NOW, NOW + 5 * HOUR, failing=[(NOW, NOW + 5 * HOUR)])  # and it fails again
    world.ship()
    later = datetime.fromtimestamp((NOW + 5 * HOUR) / 1000, tz=UTC)
    assert OCR in watch.check(later)
    shown = job_ledger.alerts_for_client(tmp_path / "memory.db", "off", later)
    titles = [one["title"] for one in shown]
    ocr_title = lang.t("source.silence.title", source=lang.t(f"source.silence.{OCR}"))
    assert titles.count(ocr_title) == 1


def test_several_broken_sources_raise_a_card_each(tmp_path: Path) -> None:
    """The card names its source, so two sources are two cards."""
    _world(tmp_path, failing=[_ago(5, 0)], dark={"mic": [_ago(6, 0)]})
    assert sorted(_watch(tmp_path).check(NOW_DT)) == [MIC, OCR]
    assert len(_cards(tmp_path)) == 2


def test_a_brain_without_the_mac_copy_judges_nothing_and_raises_nothing(tmp_path: Path) -> None:
    """The Mac's own daemon, or a brain before the first shipment, has no card to raise."""
    assert _watch(tmp_path).check(NOW_DT) == []
    assert _cards(tmp_path) == []


def test_the_card_is_served_and_settled_through_the_notice_routes(tmp_path: Path) -> None:
    """``GET /inherent/notices`` shows it, ``POST`` settles it, with job mail off.

    The routes judge a card's age against the real clock, so this one world is built back from it.
    """
    now = datetime.now(UTC)
    base = int(now.timestamp() * 1000) // (10 * MIN) * (10 * MIN)
    world = MacWorld(tmp_path)
    world.live(base - 2 * DAY, base, failing=[(base - 5 * HOUR, base)])
    world.ship()
    assert _watch(tmp_path).check(now) == [OCR]
    deps = notice_deps(
        None, None, None,
        alerts=alert_deps(
            tmp_path / "memory.db", lambda: "off", None, lambda **_: {"private": False},
        ),
    )
    client = TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(), **deps,
    )))
    (card,) = client.get("/inherent/notices").json()["notices"]
    assert card["title"] == lang.t(
        "source.silence.title", source=lang.t(f"source.silence.{OCR}"),
    )
    seen = client.post(f"/inherent/notices/{card['id']}", json={"action": "seen"})
    assert seen.status_code == 200
    assert client.get("/inherent/notices").json()["notices"] == []


def test_a_card_he_dismissed_is_not_raised_again_in_the_same_outage(tmp_path: Path) -> None:
    """Settled at once, the same silence 15 minutes on makes no new card; a new outage does."""
    world = _world(tmp_path, failing=[_ago(5, 0)])
    watch = _watch(tmp_path)
    assert watch.check(NOW_DT) == [OCR]
    job_ledger.mark_alert(tmp_path / "memory.db", _cards(tmp_path)[0]["id"], "done", NOW_DT)
    later = datetime.fromtimestamp((NOW + 15 * MIN) / 1000, tz=UTC)
    assert OCR not in watch.check(later)
    assert job_ledger.alerts_for_client(tmp_path / "memory.db", "off", later) == []
    world.ocr(NOW + 20 * MIN)  # it arrives again, then fails again
    world.ocr(NOW + 30 * MIN, ok=0, failed=10)
    for at in range(NOW + 40 * MIN, NOW + 5 * HOUR, 10 * MIN):
        world.ocr(at, ok=0, failed=10)
        world.span(at)
    world.ship()
    again = datetime.fromtimestamp((NOW + 5 * HOUR) / 1000, tz=UTC)
    assert OCR in watch.check(again)


def test_when_nothing_arrived_in_the_window_the_card_does_not_name_the_window_start(
    tmp_path: Path,
) -> None:
    """The last arrival is older than the two days read: the card says so, not a false time."""
    def build() -> None:
        world = MacWorld(tmp_path)
        world.live(window_start(), NOW, dark={"ocr": [(window_start(), NOW)]})
        world.ocr(NOW - 5 * DAY)
        world.ship()

    build()
    assert OCR in _watch(tmp_path).check(NOW_DT)
    titles = {one["title"]: one["line"] for one in _cards(tmp_path)}
    ocr_title = lang.t("source.silence.title", source=lang.t(f"source.silence.{OCR}"))
    assert titles[ocr_title] == lang.t("source.silence.line_long")


def test_a_sleeping_mac_raises_no_card_and_a_wake_that_ships_its_marks_leaves_none(
    tmp_path: Path,
) -> None:
    """The brain has not heard from the Mac for 3 hours: nothing is owed, nothing is carded."""
    world = MacWorld(tmp_path)
    world.live(window_start(), NOW - 3 * HOUR)
    world.ship()
    watch = _watch(tmp_path)
    assert watch.check(NOW_DT) == []
    world.state(NOW - 3 * HOUR + MIN, "sleep")
    world.state(NOW - 5 * MIN, "wake")
    world.live(NOW - 5 * MIN, NOW)
    world.ship()
    assert watch.check(NOW_DT) == []
    assert _cards(tmp_path) == []
