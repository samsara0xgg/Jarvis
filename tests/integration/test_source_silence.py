"""ADR 0382: a data source that stops arriving while it should be producing is found and carded.

Every world here is what a Mac can send: TimeSink's copy goes through the brain's own intake into
``imports/shipped.db``, the recording pieces are filed by the archive's own naming, and the event
log is a real one. Time is absolute (``NOW``); nothing waits. The table of scenarios is the
contract of the judgement: healthy, empty for one window, empty for two, the precondition false,
recovered. The nightly report is then asked of the same worlds (the card is in
``test_source_silence_card.py``).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.state import source_silence
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.log_invariants import check_event_log, open_read_only
from tests.support.check_log import MIN, NOW
from tests.support.source_silence import DAY, HOUR, MARKER, MacWorld, window_start

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

SPANS, OCR, MIC = source_silence.SPANS, source_silence.OCR, source_silence.MIC
INVARIANT = "sources.keep_arriving"


def _ago(*hours: float) -> tuple[int, int]:
    """The span of time from ``hours[0]`` hours before the check to ``hours[1]`` hours before."""
    return NOW - round(hours[0] * HOUR), NOW - round(hours[1] * HOUR)


def _judged(root: Path, *, ref_ms: int = NOW) -> source_silence.Judgement:
    shipped = source_silence.shipped_path(root)
    with closing(sqlite3.connect(f"{shipped.as_uri()}?mode=ro", uri=True)) as conn:
        return source_silence.check(
            conn, root / "lifelog", since_ms=ref_ms - 2 * DAY, ref_ms=ref_ms,
        )


def _world(root: Path, build: Callable[[MacWorld], None]) -> MacWorld:
    world = MacWorld(root)
    build(world)
    world.ship()
    return world


def _live(
    world: MacWorld, **kwargs: Any,  # noqa: ANN401 - MacWorld.live's keywords.
) -> None:
    world.live(window_start(), NOW, **kwargs)


# --- The judgement: one row per situation ------------------------------------------------------


def _locked_with_no_arrivals(world: MacWorld) -> None:
    world.state(NOW - 6 * HOUR, "lock")
    world.state(NOW - 1 * HOUR, "unlock")
    _live(world, dark={"spans": [_ago(6, 1)], "ocr": [_ago(6, 1)]})


def _asleep_with_no_arrivals(world: MacWorld) -> None:
    world.state(NOW - 6 * HOUR, "sleep")
    world.state(NOW - 1 * HOUR, "wake")
    _live(world, dark={name: [_ago(6, 1)] for name in ("spans", "ocr", "mic")})


def _lock_in_the_middle_of_a_silence(unlock_hours_ago: float) -> Callable[[MacWorld], None]:
    def build(world: MacWorld) -> None:
        world.state(NOW - round(2.5 * HOUR), "lock")
        world.state(NOW - round(unlock_hours_ago * HOUR), "unlock")
        _live(world, dark={"ocr": [_ago(3, 0)], "spans": [_ago(2.5, unlock_hours_ago)]})
    return build


def _phone_records_instead(world: MacWorld) -> None:
    _live(world, dark={"mic": [_ago(6, 0)]})
    for end in range(NOW - 6 * HOUR, NOW, 10 * MIN):
        world.piece(end, device="iphone-2")


def _mic_dark_for_thirteen_days(world: MacWorld) -> None:
    _live(world, dark={"mic": [(window_start(), NOW)]})
    world.piece(NOW - 13 * DAY)


def _mic_dark_for_fifteen_days(world: MacWorld) -> None:
    _live(world, dark={"mic": [(window_start(), NOW)]})
    world.piece(NOW - 15 * DAY)


# name -> (build the Mac's side, the silences expected as (source, still going), sources skipped)
SCENARIOS: dict[str, tuple[Callable[[MacWorld], None], list[tuple[str, bool]], list[str]]] = {
    "everything arrives": (_live, [], []),
    "screen text fails for 40 minutes: one window is a pause": (
        lambda w: _live(w, failing=[_ago(3, 3 - 40 / 60)]), [], [],
    ),
    "screen text fails for 3 hours and recovers": (
        lambda w: _live(w, failing=[_ago(8, 5)]), [(OCR, False)], [],
    ),
    "screen text still failing after 5 hours": (
        lambda w: _live(w, failing=[_ago(5, 0)]), [(OCR, True)], [],
    ),
    "spans stop for 4 hours and are still silent": (
        lambda w: _live(w, dark={"spans": [_ago(4, 0)]}), [(SPANS, True)], [],
    ),
    "the screen is locked, nothing arrives from TimeSink": (_locked_with_no_arrivals, [], []),
    "the Mac is asleep, nothing arrives from anywhere": (_asleep_with_no_arrivals, [], []),
    "the microphone is silent for 90 minutes: a pause": (
        lambda w: _live(w, dark={"mic": [_ago(6, 4.5)]}), [], [],
    ),
    "the microphone is silent for 3 hours and recovers": (
        lambda w: _live(w, dark={"mic": [_ago(6, 3)]}), [(MIC, False)], [],
    ),
    "the microphone is silent and the Mac awake: still going": (
        lambda w: _live(w, dark={"mic": [_ago(6, 0)]}), [(MIC, True)], [],
    ),
    "the phone records while the Mac's microphone is silent": (_phone_records_instead, [], []),
    "eligible time adds up across a lock": (
        _lock_in_the_middle_of_a_silence(0.5), [(OCR, True)], [],
    ),
    "eligible time that is not enough yet": (_lock_in_the_middle_of_a_silence(0.1), [], []),
    "a microphone dark for 13 days is silent": (
        _mic_dark_for_thirteen_days, [(MIC, True)], [],
    ),
    "a microphone dark for 15 days was switched off": (
        _mic_dark_for_fifteen_days, [], [MIC],
    ),
    "no lifelog archive at all": (
        lambda w: _live(w, dark={"mic": [(window_start(), NOW)]}), [], [MIC],
    ),
}


@pytest.mark.parametrize("case", list(SCENARIOS), ids=list(SCENARIOS))
def test_each_source_is_judged_by_its_own_gap_in_the_time_it_should_produce(
    tmp_path: Path, case: str,
) -> None:
    """The silences found are exactly the expected ones, and a source not judged says so."""
    build, expected, skipped = SCENARIOS[case]
    _world(tmp_path, build)
    judged = _judged(tmp_path)
    assert sorted((one.source, one.ongoing) for one in judged.silences) == sorted(expected)
    assert sorted(name for name, _why in judged.skipped) == skipped


def test_a_silence_trips_when_the_allowed_gap_of_eligible_time_has_run_out(tmp_path: Path) -> None:
    """Screen text last worked 5 h 10 min ago; an hour later it was silent, failures counted."""
    _world(tmp_path, lambda w: _live(w, failing=[_ago(5, 0)]))
    (silence,) = _judged(tmp_path).silences
    last_success = NOW - 5 * HOUR - 10 * MIN
    assert silence.last_arrival_ms == last_success
    assert silence.tripped_ms == last_success + HOUR
    assert silence.ended_ms is None
    assert silence.failed == 300  # ten failed runs in each of the 30 rows since


def test_a_recovered_silence_names_the_arrival_that_ended_it(tmp_path: Path) -> None:
    """The stretch is over at the first row that worked again."""
    _world(tmp_path, lambda w: _live(w, failing=[_ago(8, 5)]))
    (silence,) = _judged(tmp_path).silences
    assert silence.ended_ms == NOW - 5 * HOUR
    assert not silence.ongoing


def test_the_table_names_each_source_with_its_gap_and_its_gate() -> None:
    """Three sources, each with how its arrivals are read, what must hold and a positive gap."""
    assert [one.name for one in source_silence.SOURCES] == [SPANS, OCR, MIC]
    for one in source_silence.SOURCES:
        assert one.arrivals
        assert one.precondition
        assert one.allowed_gap_ms > 0


# --- The nightly check -------------------------------------------------------------------------


def _event_log(root: Path) -> None:
    """The log the check is anchored at: its newest event is the instant of the check."""
    with closing(open_event_log(root / "mac_events.db")) as conn:
        emit_event(
            conn, type="cost.recorded", payload={"kind": "decision", "model": "m", "cost_usd": 0},
            ts_epoch_ms=NOW,
        )


def _result(root: Path, **kwargs: Any) -> Any:  # noqa: ANN401 - check_event_log's keywords.
    kwargs.setdefault("now_ms", NOW)
    report = check_event_log(root / "mac_events.db", root, **kwargs)
    return {one.name: one for one in report.results}[INVARIANT]


def test_a_healthy_mac_passes(tmp_path: Path) -> None:
    """Every source arrives: the invariant holds."""
    _world(tmp_path, _live)
    _event_log(tmp_path)
    assert _result(tmp_path).status == "ok"


def test_a_log_without_the_mac_copy_is_skipped_with_the_reason_not_passed(tmp_path: Path) -> None:
    """A copy of the log alone cannot say; the report says it was not judged."""
    _event_log(tmp_path)
    result = _result(tmp_path)
    assert result.status == "skipped"
    assert "shipped.db" in (result.detail or "")


def test_a_log_whose_sources_are_all_switched_off_is_skipped(tmp_path: Path) -> None:
    """Nothing arrived for 15 days: nothing is judged, and the detail names each source."""
    _world(tmp_path, lambda w: w.live(NOW - 16 * DAY, NOW - 15 * DAY))
    _event_log(tmp_path)
    result = _result(tmp_path)
    assert result.status == "skipped"
    for name in (SPANS, OCR, MIC):
        assert name in (result.detail or "")


def test_a_broken_source_is_reported_by_name_time_and_gap_without_a_row_to_point_at(
    tmp_path: Path,
) -> None:
    """One violation per stretch; the example has a time and a note, no event id, no words."""
    _world(tmp_path, lambda w: _live(w, failing=[_ago(5, 0)]))
    _event_log(tmp_path)
    result = _result(tmp_path)
    assert result.status == "violated"
    assert result.violations == 1
    (example,) = result.examples
    assert example.event_id is None
    assert example.ts_epoch_ms == NOW - 4 * HOUR - 10 * MIN
    assert example.note is not None
    assert example.note.startswith(OCR)
    assert "still silent" in example.note
    assert "300 failed runs" in example.note
    assert result.rule
    assert result.ref


def test_a_recovered_source_is_still_reported_inside_the_window(tmp_path: Path) -> None:
    """The night after a break the report still names it, and says it arrived again."""
    _world(tmp_path, lambda w: _live(w, failing=[_ago(8, 5)]))
    _event_log(tmp_path)
    result = _result(tmp_path)
    assert result.status == "violated"
    (example,) = result.examples
    assert example.note is not None
    assert "arrived again" in example.note


def test_a_silence_two_days_old_is_left_out_of_the_window(tmp_path: Path) -> None:
    """Only the window is judged: a break that recovered three days ago is not this report's."""
    _world(
        tmp_path,
        lambda w: w.live(NOW - 5 * DAY, NOW, failing=[(NOW - 4 * DAY, NOW - 4 * DAY + 5 * HOUR)]),
    )
    _event_log(tmp_path)
    assert _result(tmp_path).status == "ok"


def test_a_copy_is_judged_by_its_own_last_moment_not_by_the_date_it_is_checked(
    tmp_path: Path,
) -> None:
    """Checked a month later the copy still ends where it ended, and is still silent there."""
    _world(tmp_path, lambda w: _live(w, failing=[_ago(5, 0)]))
    _event_log(tmp_path)
    result = _result(tmp_path, now_ms=NOW + 30 * DAY)
    assert result.status == "violated"
    assert result.examples[0].ts_epoch_ms == NOW - 4 * HOUR - 10 * MIN


def test_the_report_holds_names_times_and_gaps_but_never_what_the_screen_said(
    tmp_path: Path,
) -> None:
    """The copy carries window titles and screen text; none of it reaches the report."""
    _world(tmp_path, lambda w: _live(w, failing=[_ago(5, 0)], dark={"mic": [_ago(6, 0)]}))
    _event_log(tmp_path)
    report = check_event_log(tmp_path / "mac_events.db", tmp_path, now_ms=NOW)
    text = json.dumps(report.to_dict())
    assert MARKER not in text
    assert INVARIANT in text


def test_the_check_changes_nothing_in_the_imports_it_reads(tmp_path: Path) -> None:
    """Read-only: the directory holds the same files, with the same bytes, afterwards."""
    _world(tmp_path, _live)
    _event_log(tmp_path)
    imports = tmp_path / "imports"
    before = {p.name: p.read_bytes() for p in imports.rglob("*") if p.is_file()}
    _result(tmp_path)
    assert {p.name: p.read_bytes() for p in imports.rglob("*") if p.is_file()} == before


def test_every_statement_is_a_search_on_the_time_index_never_a_scan_of_the_table(
    tmp_path: Path,
) -> None:
    """EXPLAIN QUERY PLAN: the copy holds 100,000 spans; no check may walk them all."""
    _world(tmp_path, _live)
    shipped = source_silence.shipped_path(tmp_path)
    with closing(sqlite3.connect(f"{shipped.as_uri()}?mode=ro", uri=True)) as conn:
        for name, sql in source_silence.ALL_QUERIES:
            plan = [
                str(row[3])
                for row in conn.execute("EXPLAIN QUERY PLAN " + sql, ("span",) * sql.count("?"))
            ]
            assert not [line for line in plan if line.startswith("SCAN import_entries")], name
            assert any(
                line.startswith("SEARCH import_entries USING INDEX") for line in plan
            ), f"{name}: {plan}"


# --- The brain judges only the time it has heard from the Mac (review findings) -----------------


def _asleep_unannounced(world: MacWorld) -> None:
    """The Mac slept 3 hours ago; its sleep mark ships only after it wakes, so none is here yet."""
    world.live(window_start(), NOW - 3 * HOUR)


def test_a_mac_that_has_not_been_heard_from_is_not_judged_for_the_time_it_was_silent(
    tmp_path: Path,
) -> None:
    """Nothing shipped for 3 hours and no mark yet: eligible time ends where it last heard."""
    _world(tmp_path, _asleep_unannounced)
    judged = _judged(tmp_path)
    assert judged.silences == ()
    assert judged.ref_ms <= NOW - 3 * HOUR + 10 * MIN


def test_the_sleep_that_ships_after_the_wake_leaves_nothing_to_report(tmp_path: Path) -> None:
    """Once the marks arrive the stretch is a sleep, which is no silence."""
    def build(world: MacWorld) -> None:
        _asleep_unannounced(world)
        world.state(NOW - 3 * HOUR + MIN, "sleep")
        world.state(NOW - 5 * MIN, "wake")
        world.live(NOW - 5 * MIN, NOW)

    _world(tmp_path, build)
    assert _judged(tmp_path).silences == ()


def test_the_nightly_check_stops_at_the_last_shipment_too(tmp_path: Path) -> None:
    """A copy whose Mac slept for the last 3 hours reports no silence."""
    _world(tmp_path, _asleep_unannounced)
    _event_log(tmp_path)
    assert _result(tmp_path).status == "ok"


def test_a_long_span_is_alive_while_its_end_keeps_moving(tmp_path: Path) -> None:
    """One span of 3 hours, shipped again every hour with a later end, is not a silence."""
    def build(world: MacWorld) -> None:
        _live(world, dark={"spans": [(NOW - 3 * HOUR, NOW)]})
        for hours_ago in (2, 1, 0):
            world.span(NOW - 3 * HOUR, end_ms=NOW - hours_ago * HOUR - 5 * MIN, ident=777)

    _world(tmp_path, build)
    assert [one.source for one in _judged(tmp_path).silences] == []


def test_a_hands_off_stretch_is_no_time_a_span_is_owed(tmp_path: Path) -> None:
    """A call he is on but not touching: no new span for 3 hours is not a silence for spans."""
    def build(world: MacWorld) -> None:
        world.state(NOW - 3 * HOUR, "hands_off")
        _live(world, dark={"spans": [(NOW - 3 * HOUR, NOW)]})

    _world(tmp_path, build)
    assert [one.source for one in _judged(tmp_path).silences] == []


def test_the_screen_capture_pause_gates_screen_text_only(tmp_path: Path) -> None:
    """Capture paused: no OCR is owed, but spans still are; tracking paused: neither is."""
    def idle_capture_rows(world: MacWorld) -> None:
        """TimeSink still writes its capture-health rows (no OCR run in them) while paused."""
        for at in range(NOW - 5 * HOUR, NOW, 10 * MIN):
            world.ocr(at, ok=0, failed=0)

    def captures_paused(world: MacWorld) -> None:
        world.state(NOW - 5 * HOUR, "pause")
        _live(world, dark={"ocr": [_ago(5, 0)], "spans": [_ago(5, 0)]})
        idle_capture_rows(world)

    def tracking_paused(world: MacWorld) -> None:
        world.state(NOW - 5 * HOUR, "tracking_pause")
        _live(world, dark={"ocr": [_ago(5, 0)], "spans": [_ago(5, 0)]})
        idle_capture_rows(world)

    _world(tmp_path / "a", captures_paused)
    assert [one.source for one in _judged(tmp_path / "a").silences] == [SPANS]
    _world(tmp_path / "b", tracking_paused)
    assert _judged(tmp_path / "b").silences == ()


def test_timesink_starting_again_clears_what_it_said_before(tmp_path: Path) -> None:
    """Idle, stopped, started: the idle mark no longer holds, so a silence after it counts."""
    def build(world: MacWorld) -> None:
        world.state(NOW - 6 * HOUR, "idle")
        world.state(NOW - 5 * HOUR, "stop")
        world.state(NOW - 4 * HOUR, "start")
        _live(world, dark={"ocr": [_ago(4, 0)]})

    _world(tmp_path, build)
    assert [(one.source, one.ongoing) for one in _judged(tmp_path).silences] == [(OCR, True)]


def test_the_state_marks_are_read_by_key_not_by_walking_the_days_of_every_source(
    tmp_path: Path,
) -> None:
    """EXPLAIN QUERY PLAN: TimeSink's state marks come from the (source, key) index range."""
    _world(tmp_path, _live)
    shipped = source_silence.shipped_path(tmp_path)
    queries = dict(source_silence.ALL_QUERIES)
    with closing(sqlite3.connect(f"{shipped.as_uri()}?mode=ro", uri=True)) as conn:
        plan = [
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN " + queries["silence_states"], (0, 0),
            )
        ]
    assert any("sqlite_autoindex_import_entries_2" in line for line in plan), plan


def test_the_mac_copy_is_read_with_its_locks_since_a_daemon_writes_it(tmp_path: Path) -> None:
    """shipped.db is a rollback-journal file: opened immutable, no commit would ever be seen."""
    _world(tmp_path, _live)
    shipped = source_silence.shipped_path(tmp_path)
    with closing(
        open_read_only(shipped, probe=source_silence.SHIPPED_PROBE, rollback_journal=True),
    ) as conn:
        before = conn.execute("SELECT COUNT(*) FROM import_entries").fetchone()[0]
        with closing(sqlite3.connect(shipped)) as writer:
            writer.execute("DELETE FROM import_entries WHERE kind = 'span'")
            writer.commit()
        assert conn.execute("SELECT COUNT(*) FROM import_entries").fetchone()[0] < before
