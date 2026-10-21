"""ADR 0324: the invariants a runtime root's event log must hold, asked of the log itself.

Every log here is built through the real L2 functions (``open_event_log``, ``emit_event``, the
response, action, playback, confirmation and cost primitives) on a fake clock, so each
timestamp is chosen. A raw INSERT is used only where the append API refuses the row (an
unregistered type, a source that does not exist), because that refusal is the promise being
checked. The table at the bottom of the violations section is the contract: one row per
invariant (and per lifecycle or secret shape), each built to break exactly that one.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.response_run import (
    ResponseTerminalizer,  # noqa: F401 - pins the module the rule cites.
)
from jarvis.shared.realtime import CostAccountingDisposition
from jarvis.state import event_log, lifecycle_terminal, log_invariants
from jarvis.state.cost_accounting import record_cost_disposition_once
from jarvis.state.event_log import EventTypeRegistry, emit_event, open_event_log
from jarvis.state.lifecycle_terminal import (
    terminalize_action,
    terminalize_confirmation,
    terminalize_playback,
    terminalize_response,
)
from jarvis.state.log_invariants import (
    ALL_QUERIES,
    INVARIANTS,
    SECRET_SHAPE_NAMES,
    LogCheckError,
    LogReport,
    check_event_log,
)
from jarvis.state.response_runs import append_response_started
from jarvis.surface import playback_recovery

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.shared import Event

NOW = 1_780_000_000_000  # 2026-05-28T20:26:40Z, the instant the check "runs".
SEC = 1000
MIN = 60 * SEC
HOUR = 60 * MIN
DAY = 24 * HOUR
REPO = Path(__file__).resolve().parents[2]


class _Clock:
    """What ``event_log`` reads as the time; a test sets it before each append."""

    def __init__(self) -> None:
        self.now = NOW

    def ago(self, ms: int) -> None:
        self.now = NOW - ms


@dataclass
class _Rig:
    root: Path
    conn: sqlite3.Connection
    clock: _Clock
    count: int = 0

    @property
    def db(self) -> Path:
        return self.root / "mac_events.db"

    def emit(self, type_: str, ago: int | None = None, **fields: Any) -> Event:  # noqa: ANN401 - payload values.
        """Append one event; unnamed required fields get a placeholder."""
        if ago is not None:
            self.clock.ago(ago)
        body = {name: f"{name}-x" for name in EventTypeRegistry.requires(type_)} | fields
        node = body.pop("ingestion_node", "mac")
        source = body.pop("source_event_id", None)
        return emit_event(
            self.conn, type=type_, payload=body, ingestion_node=node, source_event_id=source,
        )

    def row_id(self, event: Event) -> int:
        row = self.conn.execute(
            "SELECT id FROM events WHERE event_uid = ?", (event.event_uid,),
        ).fetchone()
        return int(row[0])

    def check(self, **kwargs: Any) -> LogReport:  # noqa: ANN401 - check_event_log's keywords.
        self.conn.commit()
        kwargs.setdefault("now_ms", NOW)
        return check_event_log(self.db, self.root, **kwargs)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401 - yields a _Rig.
    """A fake clock, a root with an audio directory, and an open event log in it."""
    clock = _Clock()
    monkeypatch.setattr(event_log, "_now_epoch_ms", lambda: clock.now)
    (tmp_path / "memory" / "audio").mkdir(parents=True)
    conn = open_event_log(tmp_path / "mac_events.db")
    try:
        yield _Rig(tmp_path, conn, clock)
    finally:
        conn.close()


# --- A clean log: one of everything the invariants look at, all in order -------------------


def _response_payload(response_id: str, turn_id: str) -> dict[str, object]:
    return {
        "response_id": response_id, "response_group_id": f"G-{response_id}", "turn_id": turn_id,
        "phase": "final", "channel": "both", "emission_mode": "full_text",
        "output_risk_class": "unknown", "required_gate_mode": "full_text", "policy_hash": "p",
        "active_subject_ref": "unknown", "evidence_snapshot_hash": "e",
    }


def _playback_payload(response_id: str, **extra: object) -> dict[str, object]:
    return {
        "session_id": "S", "response_id": response_id, "turn_id": "T", "playback_generation_id": 1,
        "heard_through_sequence": 0, "submitted_samples": 0, "speech_text_hash": "h",
        "heard_text_hash": "h", "phase": "final", "channel": "both", **extra,
    }


def _open_response(rig: _Rig, response_id: str, ago: int) -> Event:
    rig.clock.ago(ago)
    return append_response_started(
        rig.conn, payload=_response_payload(response_id, "T-" + response_id),
        source_event_id=rig.emit("turn.started", turn_id="T-" + response_id).event_uid,
    )


def _close_response(rig: _Rig, response_id: str, ago: int) -> None:
    rig.clock.ago(ago)
    terminalize_response(
        rig.conn, event_type="response.completed",
        payload={
            "response_id": response_id, "response_group_id": f"G-{response_id}",
            "turn_id": "T-" + response_id, "response_hash": "h",
        },
    )


def _dispatch(rig: _Rig, action_id: str, ago: int) -> Event:
    return rig.emit("action.dispatched", ago, action_id=action_id)


def _end_action(rig: _Rig, action_id: str, ago: int, kind: str = "action.result_observed") -> None:
    rig.clock.ago(ago)
    terminalize_action(
        rig.conn, event_type=kind, payload={"action_id": action_id, "semantics": "observation"},
    )


def _cost(rig: _Rig, request_id: str, ago: int) -> None:
    rig.clock.ago(ago)
    record_cost_disposition_once(
        rig.conn,
        CostAccountingDisposition(
            llm_request_id=request_id, kind="decision", provider="p", model="m",
            outcome="completed", usage_status="provider_final", input_tokens=1, output_tokens=1,
            cost_usd=0.01,
        ),
    )


def _timing(rig: _Rig, request_ids: list[str | None], ago: int) -> Event:
    return rig.emit(
        "turn.timing", ago, turn_id="T-timing",
        requests=[{"kind": "decision", "llm_request_id": one} for one in request_ids],
    )


def _audio(rig: _Rig, name: str, ago: int, *, written: str | None = "m4a", **fields: Any) -> Event:  # noqa: ANN401 - payload values.
    if written:
        (rig.root / "memory" / "audio" / f"{name}.{written}").write_bytes(b"\0")
    return rig.emit(
        "utterance.received", ago, turn_id=name, transcript="words",
        audio_artifact_ref=str(rig.root / "memory" / "audio" / f"{name}.m4a"), **fields,
    )


def seed_history(rig: _Rig) -> None:
    """Hours of ordinary life that ended 80 minutes before the check; nothing in it is wrong."""
    _audio(rig, "heard-here", 6 * HOUR)
    # A terminal heard this one: its file is on the terminal's disk, never on this machine.
    rig.emit(
        "utterance.received", 6 * HOUR - SEC, turn_id="heard-there", transcript="words",
        audio_artifact_ref="/Users/someone/.jarvis/memory/audio/heard-there.m4a",
        ingestion_node="macbook",
    )
    _open_response(rig, "r1", 5 * HOUR)
    _close_response(rig, "r1", 5 * HOUR - MIN)
    _dispatch(rig, "a1", 4 * HOUR)
    _end_action(rig, "a1", 4 * HOUR - MIN)
    _dispatch(rig, "a2", 4 * HOUR - 2 * MIN)
    _end_action(rig, "a2", 4 * HOUR - 3 * MIN, "action.timeout_assumed")
    _cost(rig, "req-1", 3 * HOUR)
    _timing(rig, ["req-1", None], 3 * HOUR - MIN)
    rig.emit("surface.playback_started", 2 * HOUR, **_playback_payload("r1"))
    terminalize_playback(
        rig.conn, event_type="surface.playback_completed", payload=_playback_payload("r1"),
    )
    rig.emit("confirmation.requested", 2 * HOUR - MIN, confirmation_id="c1", expires_at_ms=1)
    revision = rig.conn.execute(
        "SELECT id FROM events WHERE type = 'confirmation.requested'",
    ).fetchone()[0]
    terminalize_confirmation(
        rig.conn, event_type="confirmation.accepted", expected_revision=revision,
        payload={"confirmation_id": "c1", "utterance_raw": "yes", "grammar_rule_id": "g"},
    )
    rig.emit(
        "reminder.scheduled", 2 * HOUR - 2 * MIN, reminder_id="m1",
        due_at_epoch_ms=NOW - 100 * MIN,
    )
    rig.emit("reminder.fired", 100 * MIN, reminder_id="m1", fired_at=NOW - 100 * MIN, late_ms=0)
    source = rig.emit("phone.location_observed", 90 * MIN, lat=1.0, lng=2.0, accuracy_m=5)
    rig.emit("turn.ended", 80 * MIN, turn_id="T-r1", source_event_id=source.event_uid)


def seed_present(rig: _Rig) -> None:
    """The last minutes before the check: work still in flight, inside the grace."""
    _open_response(rig, "r-now", 5 * MIN)
    _dispatch(rig, "a-now", 5 * MIN)
    rig.emit("surface.playback_started", 4 * MIN, **_playback_payload("r-now"))
    _timing(rig, ["req-now"], MIN)
    _audio(rig, "heard-just-now", MIN, written=None)
    rig.emit("reminder.scheduled", MIN, reminder_id="m2", due_at_epoch_ms=NOW - 5 * MIN)
    rig.emit("turn.started", 0, turn_id="T-last")


def seed_clean(rig: _Rig) -> None:
    """History, then the instant of the check: every invariant holds."""
    seed_history(rig)
    seed_present(rig)


def _by_name(report: LogReport) -> dict[str, log_invariants.InvariantResult]:
    return {one.name: one for one in report.results}


def test_a_clean_log_passes_every_invariant(rig: _Rig) -> None:
    """Nothing in a log built only through the real primitives is flagged."""
    seed_clean(rig)
    report = rig.check()
    assert [(one.name, one.status) for one in report.results if one.status != "ok"] == []
    assert report.violated == ()
    assert report.events > 20
    assert 10 <= len(INVARIANTS) <= 15
    assert len({one.name for one in INVARIANTS}) == len(INVARIANTS)


def test_an_empty_log_is_clean(rig: _Rig) -> None:
    """A log with no events violates nothing."""
    report = rig.check()
    assert report.violated == ()
    assert report.events == 0


# --- One invariant broken at a time ------------------------------------------------------


@dataclass(frozen=True)
class _Break:
    invariant: str
    build: Callable[[_Rig], list[Event | int]]
    note: str | None = None
    late: bool = False  # built after the present, for a row that must be the newest of all.


def _raw_insert(rig: _Rig, type_: str, source: str | None = None) -> int:
    """A row the append API would refuse (the refusal is the promise under test)."""
    rig.clock.ago(30 * MIN)
    rig.conn.execute(
        "INSERT INTO events (event_uid, type, schema_version, ts_epoch_ms, payload_json, "
        "source_event_id, actor, ingestion_node) VALUES (?, ?, 1, ?, '{}', ?, 'x', 'mac')",
        (f"raw-{rig.count}", type_, rig.clock.now, source),
    )
    rig.count += 1
    rig.conn.commit()
    return int(rig.conn.execute("SELECT MAX(id) FROM events").fetchone()[0])


def _twice(rig: _Rig, type_: str, **fields: Any) -> list[Event | int]:  # noqa: ANN401 - payload values.
    """A terminal the compare-and-set would refuse, appended a second time by plain emit."""
    return [rig.emit(type_, 10 * MIN, **fields)]


def _step_back(rig: _Rig) -> list[Event | int]:
    """The host clock jumping an hour into the past between two appends."""
    rig.emit("turn.started", 20 * MIN, turn_id="T-b1")
    return [rig.emit("turn.started", 20 * MIN + HOUR, turn_id="T-b2")]


BREAKS: dict[str, _Break] = {
    "an unregistered type": _Break(
        "event.type_registered", lambda rig: [_raw_insert(rig, "legacy.thing")], "legacy.thing",
    ),
    "a source that does not exist": _Break(
        "event.source_resolves", lambda rig: [_raw_insert(rig, "turn.started", "no-such-uid")],
    ),
    "the clock stepping back": _Break(
        "event.clock_not_backwards",
        _step_back,
        "back 3600 s",
    ),
    "the clock ahead of the check": _Break(
        "event.clock_not_ahead",
        lambda rig: [rig.emit("turn.started", -10 * MIN, turn_id="T-a")],
        "ahead 600 s",
        late=True,
    ),
    "a second response terminal": _Break(
        "lifecycle.single_terminal",
        lambda rig: _twice(
            rig, "response.failed", response_id="r1", response_group_id="G", turn_id="T-r1",
            reason="late",
        ),
        "response",
    ),
    "a second action terminal": _Break(
        "lifecycle.single_terminal",
        lambda rig: _twice(rig, "action.failed", action_id="a1"),
        "action",
    ),
    "a second playback terminal": _Break(
        "lifecycle.single_terminal",
        lambda rig: _twice(
            rig, "surface.playback_failed", **_playback_payload("r1", reason="late"),
        ),
        "playback",
    ),
    "a second confirmation terminal": _Break(
        "lifecycle.single_terminal",
        lambda rig: _twice(
            rig, "confirmation.expired", confirmation_id="c1", expired_at_ms=1,
        ),
        "confirmation",
    ),
    "a response run that never ended": _Break(
        "response.run_terminates", lambda rig: [_open_response(rig, "r-stuck", 30 * MIN)],
    ),
    "an action that never ended": _Break(
        "action.dispatch_terminates", lambda rig: [_dispatch(rig, "a-stuck", 30 * MIN)],
    ),
    "a playback that never ended": _Break(
        "playback.started_terminates",
        lambda rig: [rig.emit("surface.playback_started", 30 * MIN, **_playback_payload("r-p"))],
    ),
    "a request with no cost row": _Break(
        "cost.recorded_per_request", lambda rig: [_timing(rig, ["req-lost"], 30 * MIN)],
    ),
    "a request costed twice": _Break(
        "cost.recorded_once",
        lambda rig: [
            rig.emit("cost.recorded", 10 * MIN, kind="decision", model="m", llm_request_id="req-1"),
        ],
    ),
    "audio named .m4a but written .wav": _Break(
        "utterance.audio_ref_exists",
        lambda rig: [_audio(rig, "no-afconvert", 30 * MIN, written="wav")],
    ),
    "a reminder that never rang": _Break(
        "reminder.fires_when_due",
        lambda rig: [
            rig.emit(
                "reminder.scheduled", HOUR, reminder_id="m-lost", due_at_epoch_ms=NOW - 40 * MIN,
            ),
        ],
    ),
}

# One sample per shape, built from pieces so this file holds no key-looking literal.
SECRET_SAMPLES: dict[str, str] = {
    "pem_private_key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "sk_api_key": "sk-" + "Ab1" * 10,
    "aws_access_key_id": "AKIA" + "0123456789ABCDEF",
    "github_token": "ghp_" + "a1" * 20,
    "slack_token": "xoxb-" + "1234567890-abcdef",
    "google_api_key": "AIza" + "Sy" + "0123456789abcdefghijklmnopqrstuvw",
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0In0" + ".sig" + "nature12345",
    "bearer_token": "Authorization: Bearer " + "abcdefghij0123456789klmnop",
}


def _leak(sample: str) -> Callable[[_Rig], list[Event | int]]:
    """A builder that puts ``sample`` into the words of a conversation event."""
    def build(rig: _Rig) -> list[Event | int]:
        words = f"he said {sample} aloud"
        return [rig.emit(
            "surface.conversation_words", 10 * MIN, turn_id="T", reason="r", transcript=words,
        )]
    return build


for _name, _sample in SECRET_SAMPLES.items():
    BREAKS[f"a {_name} in a payload"] = _Break("payload.no_secret_shapes", _leak(_sample), _name)


def _seed_with(rig: _Rig, broken: _Break) -> list[Event | int]:
    """The clean log with one fault in it, appended in the order time ran."""
    seed_history(rig)
    made = [] if broken.late else broken.build(rig)
    seed_present(rig)
    return broken.build(rig) if broken.late else made


@pytest.mark.parametrize("case", list(BREAKS), ids=list(BREAKS))
def test_each_invariant_flags_its_violation_and_only_its_own(rig: _Rig, case: str) -> None:
    """Each fault is flagged once, by its own invariant, with the event it is on."""
    broken = BREAKS[case]
    made = _seed_with(rig, broken)
    report = rig.check()

    assert [one.name for one in report.violated] == [broken.invariant]
    result = _by_name(report)[broken.invariant]
    assert result.violations == 1
    wanted = [item if isinstance(item, int) else rig.row_id(item) for item in made]
    assert [one.event_id for one in result.examples] == wanted
    assert [one.note for one in result.examples] == [broken.note]
    assert result.rule
    assert result.ref


def test_every_invariant_has_a_case_in_the_table() -> None:
    """No invariant, lifecycle or secret shape is left without a case above."""
    covered = {one.invariant for one in BREAKS.values()}
    assert covered == {one.name for one in INVARIANTS}
    single = "lifecycle.single_terminal"
    lifecycles = {one.note for one in BREAKS.values() if one.invariant == single}
    assert lifecycles == {"response", "action", "playback", "confirmation"}
    shapes = {one.note for one in BREAKS.values() if one.invariant == "payload.no_secret_shapes"}
    assert shapes == set(SECRET_SHAPE_NAMES)


# --- The edges --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age_min", "flagged"), [(19, 0), (21, 1)], ids=["inside the grace", "past the grace"],
)
def test_a_run_is_late_only_after_the_grace(rig: _Rig, age_min: int, flagged: int) -> None:
    """A run open for 19 minutes is in flight; one open for 21 is not closed."""
    _open_response(rig, "r", age_min * MIN)
    rig.emit("turn.started", 0, turn_id="T-last")
    assert _by_name(rig.check())["response.run_terminates"].violations == flagged


def test_a_clock_step_within_the_tolerance_is_not_a_step(rig: _Rig) -> None:
    """Two appends 1.5 s apart the wrong way round are not a clock step."""
    rig.emit("turn.started", 10 * SEC, turn_id="T1")
    rig.emit("turn.started", 10 * SEC + 1500, turn_id="T2")  # 1.5 s back
    rig.emit("turn.started", 0, turn_id="T3")
    assert _by_name(rig.check())["event.clock_not_backwards"].violations == 0


def test_rows_other_than_the_brains_own_are_not_held_to_the_clock(rig: _Rig) -> None:
    """A terminal's relayed row and a night event carry their own timestamps."""
    rig.emit("turn.started", MIN, turn_id="T1")
    rig.emit("phone.location_observed", 3 * HOUR, lat=1.0, lng=2.0, accuracy_m=5)
    rig.emit("night.ended", -3 * HOUR, night_id="n", reason="r")
    rig.emit("turn.started", 0, turn_id="T2")
    report = rig.check()
    assert _by_name(report)["event.clock_not_backwards"].violations == 0
    assert _by_name(report)["event.clock_not_ahead"].violations == 0


def test_a_violation_older_than_the_window_is_left_out_until_the_window_reaches_it(
    rig: _Rig,
) -> None:
    """--since-days bounds what is looked at."""
    _open_response(rig, "r-old", 3 * DAY)
    rig.emit("turn.started", 0, turn_id="T-last")
    assert _by_name(rig.check())["response.run_terminates"].violations == 0
    assert _by_name(rig.check(since_days=5))["response.run_terminates"].violations == 1


def test_a_copy_is_judged_by_its_own_last_moment_not_by_the_date_it_is_checked(rig: _Rig) -> None:
    """A backup checked a week later gets the same answer as the day it was taken."""
    _seed_with(rig, BREAKS["a response run that never ended"])
    now = rig.check()
    week_later = rig.check(now_ms=NOW + 7 * DAY)
    assert [(one.name, one.violations) for one in week_later.results] == [
        (one.name, one.violations) for one in now.results
    ]
    assert [one.name for one in week_later.violated] == ["response.run_terminates"]
    assert week_later.until_ms == now.until_ms == NOW


def test_examples_are_capped_and_the_count_is_not(rig: _Rig) -> None:
    """max_examples limits the ids listed, never the number counted."""
    for index in range(5):
        _open_response(rig, f"r-stuck-{index}", 40 * MIN - index * SEC)
    rig.emit("turn.started", 0, turn_id="T-last")
    result = _by_name(rig.check(max_examples=2))["response.run_terminates"]
    assert (result.violations, len(result.examples)) == (5, 2)
    assert _by_name(rig.check(max_examples=0))["response.run_terminates"].examples == ()


def test_audio_refs_of_a_copy_without_recordings_are_skipped_not_passed(rig: _Rig) -> None:
    """No recordings directory means the check could not run, and says so."""
    _audio(rig, "u1", 30 * MIN, written=None)
    rig.emit("turn.started", 0, turn_id="T-last")
    (rig.root / "memory" / "audio").rmdir()
    result = _by_name(rig.check())["utterance.audio_ref_exists"]
    assert (result.status, result.violations) == ("skipped", 0)
    assert result.detail is not None
    assert "1 ref(s) not checked" in result.detail


def test_audio_refs_follow_a_moved_root_by_the_file_name(rig: _Rig) -> None:
    """A copy of the root elsewhere: the ref names the old absolute path, the file is by name."""
    rig.emit(
        "utterance.received", 30 * MIN, turn_id="u1", transcript="words",
        audio_artifact_ref="/home/allen/.jarvis/memory/audio/u1.m4a",
    )
    rig.emit("turn.started", 0, turn_id="T-last")
    assert _by_name(rig.check())["utterance.audio_ref_exists"].violations == 1
    (rig.root / "memory" / "audio" / "u1.m4a").write_bytes(b"\0")
    assert _by_name(rig.check())["utterance.audio_ref_exists"].status == "ok"


@pytest.mark.parametrize("days", [0, -1, float("nan"), float("inf"), 4000])
def test_a_window_that_is_no_number_of_days_is_refused(rig: _Rig, days: float) -> None:
    """Zero, negative, NaN, infinite and absurd windows are errors, not empty windows."""
    with pytest.raises(ValueError, match="since_days"):
        rig.check(since_days=days)


def test_a_file_that_is_no_event_log_is_refused(tmp_path: Path) -> None:
    """A missing file, another database and junk bytes each raise LogCheckError."""
    with pytest.raises(LogCheckError, match="no event log"):
        check_event_log(tmp_path / "mac_events.db", tmp_path)
    other = tmp_path / "other.db"
    with closing(sqlite3.connect(other)) as conn:
        conn.execute("CREATE TABLE notes (x)")
    with pytest.raises(LogCheckError, match="not an event log"):
        check_event_log(other, tmp_path)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(LogCheckError):
        check_event_log(junk, tmp_path)


def test_the_report_holds_ids_types_counts_and_times_but_never_payload_text(rig: _Rig) -> None:
    """Words, a secret and an odd type name in the log stay out of the report."""
    seed_clean(rig)
    marker = "PRIVATE-WORDS-OF-THE-OWNER"
    sample = SECRET_SAMPLES["sk_api_key"]
    rig.emit("surface.conversation_words", 10 * MIN, turn_id="T", reason="r",
             transcript=f"{marker} {sample}")
    rig.emit("utterance.received", 20 * MIN, turn_id="u", transcript=marker,
             audio_artifact_ref=f"/nowhere/{marker}.m4a")
    rig.conn.execute(
        "INSERT INTO events (event_uid, type, schema_version, ts_epoch_ms, payload_json, actor, "
        "ingestion_node) VALUES ('w', ?, 1, ?, ?, 'x', 'mac')",
        (f"weird type {marker}", NOW - 5 * MIN, json.dumps({"transcript": marker})),
    )
    rig.conn.commit()
    rig.emit("turn.started", 0, turn_id="T-last")
    report = rig.check()
    text = json.dumps(report.to_dict())
    assert {one.name for one in report.violated} >= {
        "payload.no_secret_shapes", "utterance.audio_ref_exists", "event.type_registered",
    }
    assert marker not in text
    assert sample not in text
    assert "weird type" not in text


# --- What the module promises about itself ----------------------------------------------


def test_the_terminal_sets_are_the_ones_the_lifecycle_owners_use() -> None:
    """A terminal type added to a lifecycle owner must be added here too."""
    assert set(log_invariants.ACTION_TERMINALS) == lifecycle_terminal._ACTION_TERMINALS  # noqa: SLF001
    assert set(log_invariants.RESPONSE_TERMINALS) == lifecycle_terminal._RESPONSE_TERMINALS  # noqa: SLF001
    assert set(log_invariants.PLAYBACK_TERMINALS) == lifecycle_terminal._PLAYBACK_TERMINALS  # noqa: SLF001
    assert set(log_invariants.CONFIRMATION_TERMINALS) == lifecycle_terminal._CONFIRMATION_TERMINALS  # noqa: SLF001
    assert set(log_invariants.PLAYBACK_TERMINALS) == set(playback_recovery._PLAYBACK_TERMINALS)  # noqa: SLF001


def test_every_event_type_the_module_names_is_registered() -> None:
    """A renamed event type breaks this test and not the nightly report."""
    named = {
        log_invariants.RESPONSE_STARTED, log_invariants.ACTION_DISPATCHED,
        log_invariants.PLAYBACK_STARTED, *log_invariants.CLOCK_STAMPED_TYPES,
        *log_invariants.RESPONSE_TERMINALS, *log_invariants.ACTION_TERMINALS,
        *log_invariants.PLAYBACK_TERMINALS, *log_invariants.CONFIRMATION_TERMINALS,
        *log_invariants.REMINDER_ENDS, "turn.timing", "cost.recorded", "utterance.received",
        "reminder.scheduled",
    }
    assert [one for one in sorted(named) if EventTypeRegistry.get(one) is None] == []


def test_every_rule_points_at_a_file_or_a_spec_anchor_that_exists() -> None:
    """Each invariant's pointer resolves: a file, a spec anchor or an ADR."""
    spec = (REPO / "docs" / "spec.html").read_text(encoding="utf-8")
    anchors = set(re.findall(r'id="([^"]+)"', spec))
    problems: list[str] = []
    for one in INVARIANTS:
        for pointer in re.split(r"; ", one.ref):
            cited = re.match(
                r"(docs/spec\.html)#([\w-]+)|docs/adr/(\d{4})|([\w/.-]+\.(?:py|md))", pointer,
            )
            if cited is None:
                problems.append(f"{one.name}: nothing to resolve in {pointer!r}")
            elif cited.group(2) is not None and cited.group(2) not in anchors:
                problems.append(f"{one.name}: no anchor {cited.group(2)} in the spec")
            elif cited.group(3) is not None and not list(
                (REPO / "docs" / "adr").glob(f"{cited.group(3)}-*.md"),
            ):
                problems.append(f"{one.name}: no ADR {cited.group(3)}")
            elif cited.group(4) is not None and not (REPO / cited.group(4)).is_file():
                problems.append(f"{one.name}: no file {cited.group(4)}")
    assert problems == []


def test_the_statements_carry_no_quote_and_the_anchor_list_is_the_shapes() -> None:
    """The SQL pre-filter is built from the shapes and cannot be quoted out of its string."""
    anchors = log_invariants._SECRET_ANCHORS  # noqa: SLF001
    assert all("'" not in one and '"' not in one for one in anchors)
    from_shapes = {a for _n, group, _p in log_invariants._SECRET_SHAPES for a in group}  # noqa: SLF001
    assert set(anchors) == from_shapes


def test_every_secret_shape_is_found_by_its_own_anchor_first() -> None:
    """The SQL pre-filter must let each regex's own sample through, or the shape never runs."""
    for name, anchors, pattern in log_invariants._SECRET_SHAPES:  # noqa: SLF001
        sample = SECRET_SAMPLES[name]
        assert pattern.search(sample), name
        assert any(anchor in sample for anchor in anchors), name


def test_every_statement_is_a_search_never_a_scan_of_the_table(rig: _Rig) -> None:
    """EXPLAIN QUERY PLAN: the log only grows, so no check may walk all of it."""
    seed_clean(rig)
    rig.conn.commit()
    names = {name for name, _sql in ALL_QUERIES}
    assert len(names) == len(ALL_QUERIES)
    table_aliases = {"events", "e", "s", "t"}
    for name, sql in ALL_QUERIES:
        plan = [
            str(row[3])
            for row in rig.conn.execute(
                "EXPLAIN QUERY PLAN " + sql, (1,) * sql.count("?"),
            )
        ]
        scans = [
            line for line in plan
            if line.startswith("SCAN ") and line.split()[1] in table_aliases
        ]
        assert scans == [], f"{name} scans the table: {plan}"
        assert any(line.startswith("SEARCH ") for line in plan), f"{name}: {plan}"


@pytest.mark.parametrize(
    "writer_open", [True, False], ids=["a writer has it open", "nobody has it open"],
)
def test_the_connection_cannot_write_even_with_its_own_guard_switched_off(
    rig: _Rig, *, writer_open: bool,
) -> None:
    """The open mode, not just ``PRAGMA query_only``, is what keeps the check from writing."""
    seed_clean(rig)
    rig.conn.commit()
    if not writer_open:
        rig.conn.close()
    assert (rig.root / "mac_events.db-wal").exists() is writer_open
    with closing(log_invariants.open_read_only(rig.db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 20
        conn.execute("PRAGMA query_only = OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM events")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE sneaked_in (x)")
