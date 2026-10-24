"""ADR 0388: ``body``, every Health measure from his watch and phone, by day, days or hours.

Acceptance checks against the real event log and the real tool, on an invented fortnight: heart
rate every five minutes from 2 pm to 6 pm, a step total each hour, a night's sleep with its
stages, one workout, and the watch charging through a hole in the heart rate. The answers keep
the tool standard (docs/spec.html#tools) and end in a read's receipt.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from jarvis.execution import body_tool
from jarvis.execution.body_tool import build_body_tool
from jarvis.execution.tools import Receipted, ToolError
from jarvis.state.event_log import emit_event, open_event_log
from scripts import tool_exam
from scripts.tool_exam_tools import result_problems
from tests.support.clock import FakeClock
from tests.support.tool_context import tool_context

REPO = Path(__file__).resolve().parents[2]
ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 23, 17, 36, tzinfo=ZONE)  # a Friday
TODAY = NOW.date()
MEASURE, HEALTH, STATE = "phone.measure_observed", "phone.health_observed", "phone.state_observed"
LATE = timedelta(minutes=40)  # how long after the watch took it a reading reaches the log
USUAL_HR = (68, 72, 76, 80)  # the days before, 2 pm to 6 pm; the first two while still
TODAY_HR = (84, 88, 92, 96)  # today from 2 pm; the first two while still


def _ms(at: datetime) -> int:
    return round(at.timestamp() * 1000)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)


class _World:
    """The rows of one invented fortnight, appended to a log in the order they arrived."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any], int]] = []
        self._uuid = 0

    def _next_uuid(self) -> str:
        self._uuid += 1
        return f"00000000-0000-4000-8000-{self._uuid:012d}"

    def reading(
        self, metric: str, at: datetime, value: float, *, rest: bool | None = None,
        uuid: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "metric": metric, "start_ms": _ms(at), "end_ms": _ms(at), "value": value,
            "unit": "count/min", "sample_uuid": uuid or self._next_uuid(), "source": "Watch",
        }
        if rest is not None:
            payload["meta"] = {"motion_context": "sedentary" if rest else "not_set"}
        self.rows.append((MEASURE, payload, _ms(at + LATE)))

    def heart(self, day: date, values: tuple[int, ...], start: int, until: datetime) -> None:
        at, place = _at(day, start), 0
        while at < until:
            self.reading("heart_rate", at, values[place % 4], rest=place % 4 < 2)
            at, place = at + timedelta(minutes=5), place + 1

    def total(self, metric: str, hour: datetime, value: float) -> None:
        end = hour + timedelta(hours=1)
        payload = {"metric": metric, "start_ms": _ms(hour), "end_ms": _ms(end), "value": value}
        self.rows.append((HEALTH, payload, _ms(end)))

    def span(
        self, metric: str, start: datetime, end: datetime, detail: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "metric": metric, "start_ms": _ms(start), "end_ms": _ms(end), "value": 0,
            "unit": "category", "detail": detail, "sample_uuid": self._next_uuid(),
        }
        if meta:
            payload["meta"] = meta
        self.rows.append((HEALTH, payload, _ms(end)))

    def night(self, woke: date, minutes_late: int = 0) -> None:
        """23:30 to 07:00 (plus ``minutes_late``): core, deep, core, REM, awake, core."""
        at = _at(woke, 0) - timedelta(minutes=30)
        for stage, minutes in (
            ("core", 90), ("deep", 60), ("core", 120), ("rem", 60), ("awake", 10),
            ("core", 120 + minutes_late),
        ):
            self.span("sleep", at, at + timedelta(minutes=minutes), stage)
            at += timedelta(minutes=minutes)

    def charge(self, at: datetime, percent: int, state: str) -> None:
        payload = {"signal": "watch_battery", "value": str(percent), "detail": state}
        self.rows.append((STATE, payload, _ms(at)))

    def write(self, path: Path) -> None:
        conn = open_event_log(path)
        for kind, payload, ts in sorted(self.rows, key=lambda row: row[2]):
            if ts <= _ms(NOW):
                emit_event(conn, type=kind, payload=payload, ts_epoch_ms=ts, ingestion_node="phone")
        conn.close()


def _fortnight() -> _World:
    world = _World()
    for back in range(1, 15):
        day = TODAY - timedelta(days=back)
        if back == 1:  # yesterday the watch came off from 2:30 pm to 4:10 pm
            world.heart(day, USUAL_HR, 14, _at(day, 14, 30))
            world.heart(day, USUAL_HR, 16, _at(day, 18))
            world.rows = [r for r in world.rows if not (
                r[1].get("metric") == "heart_rate"
                and _ms(_at(day, 14, 30)) <= r[1]["start_ms"] < _ms(_at(day, 16, 10))
            )]
            world.charge(_at(day, 14, 40), 15, "charging")
            world.charge(_at(day, 15, 50), 100, "full")
        else:
            world.heart(day, USUAL_HR, 14, _at(day, 18))
        for hour in range(8, 18):
            world.total("steps", _at(day, hour), 500)
        world.night(day)
        world.span("stand_hour", _at(day, 15), _at(day, 16), "stood")
    world.heart(TODAY, TODAY_HR, 14, NOW)
    for hour in range(8, 17):
        world.total("steps", _at(TODAY, hour), 300)
    world.total("steps", _at(TODAY, 9), 100)  # sent again, later, for the same hour: 100 stands
    world.night(TODAY, minutes_late=-30)  # woke at 6:30 am, half an hour short
    world.span("stand_hour", _at(TODAY, 15), _at(TODAY, 16), "idle")
    yesterday = TODAY - timedelta(days=1)
    world.span(
        "workout", _at(yesterday, 18, 5), _at(yesterday, 18, 47), "running",
        {"total_energy_kcal": 388.2, "distance_m": 6120.0, "avg_heart_rate": 148.0},
    )
    world.reading("resting_heart_rate", _at(TODAY, 8), 55)
    return world


@pytest.fixture(scope="module")
def log(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fortnight's log, written once for the module."""
    path = tmp_path_factory.mktemp("body") / "events.db"
    _fortnight().write(path)
    return path


def _call(log: Path, **args: Any) -> Receipted:  # noqa: ANN401 - the tool's own arguments
    (tool,) = build_body_tool(log, ZONE)
    out = tool.handler(args, tool_context(clock=FakeClock(NOW)))
    assert isinstance(out, Receipted)
    assert result_problems(dict(out)) == [], dict(out)
    assert out.receipt.status == "done"
    assert out.receipt.reads_only
    assert out.receipt.covered is not None
    assert out.receipt.covered.window == out["window"]
    return out


def _refused(log: Path, **args: Any) -> str:  # noqa: ANN401 - the tool's own arguments
    (tool,) = build_body_tool(log, ZONE)
    with pytest.raises(ToolError) as caught:
        tool.handler(args, tool_context(clock=FakeClock(NOW)))
    assert caught.value.code == "invalid_arguments"
    return str(caught.value)


def test_his_heart_rate_from_two_pm_to_now_stands_beside_the_same_hours_before(log: Path) -> None:
    """「下午2点到现在心率是不是有点偏高」: the hours to now, beside the same hours of 14 days."""
    out = _call(log, measures=["heart_rate"], **{"from": "14:00", "until": "now"})
    heart = out["heart_rate"]
    assert out["window"] == "today (Fri) 2:00 pm to 5:36 pm, now"
    # Readings until 4:56 pm have reached the log (each lands 40 minutes after it was taken).
    assert heart["readings"] == 36  # 2:00 pm to 4:55 pm, every five minutes
    assert (heart["low"], heart["median"], heart["high"]) == (84, 90, 96)
    assert heart["while_still"]["readings"] == 18
    assert heart["while_still"]["median"] == 86
    # Said "at rest", the brain called these his resting heart rate, which is its own measure.
    assert "not his resting heart rate" in heart["while_still"]["note"]
    assert "at rest" not in str(dict(out))
    assert heart["usual"]["median"] == 74  # all of 68, 72, 76, 80
    assert heart["usual"]["while_still_median"] == 70  # 68 and 72
    assert heart["usual"]["days"] == 14
    assert heart["vs_usual"] == "above"
    assert heart["while_still"]["vs_usual"] == "above"
    assert heart["latest"] == "4:55 pm, 96 bpm"
    assert "4:55 pm" in heart["still_arriving"]
    assert heart["by"][0] == "2:00 pm: 88 bpm (84 to 96, 6)"  # 30-minute stretches for 3.6 hours
    assert len(heart["by"]) == 6  # 2:00 to 4:30; nothing has come after 4:55
    assert out["summary"].startswith(
        "Today (Fri) 2:00 pm to 5:36 pm, now: heart rate median 90 bpm, 84 to 96 over 36 readings;"
        " usual 74 bpm for the same hours"
    )
    assert "so above it" in out["summary"]
    assert out.receipt.line == "read his heart rate, today (Fri) 2:00 pm to 5:36 pm, now"


def test_a_hole_in_his_heart_rate_is_named_with_the_watch_charging_in_it(log: Path) -> None:
    """Yesterday's 2:30 pm to 4:10 pm without readings, and what the watch said of its charge."""
    out = _call(log, measures=["heart_rate"], when="yesterday")
    heart = out["heart_rate"]
    assert heart["no_readings"] == [
        "12:00 am to 2:00 pm",
        "2:25 pm to 4:10 pm (the watch said charging then full, 15% at 2:40 pm)",
        "5:55 pm to 12:00 am",
    ]
    assert "none from 12:00 am to 2:00 pm and 2 other stretches" in out["summary"]
    assert "still_arriving" not in heart


def test_a_reading_sent_twice_counts_once(tmp_path: Path) -> None:
    """The same sample_uuid, in either case, after a reinstall resent it."""
    world = _World()
    world.reading("heart_rate", _at(TODAY, 15), 70, uuid="00000000-0000-4000-8000-0000000000AA")
    world.reading("heart_rate", _at(TODAY, 15), 70, uuid="00000000-0000-4000-8000-0000000000aa")
    world.reading("heart_rate", _at(TODAY, 15, 5), 74)
    world.write(tmp_path / "events.db")
    heart = _call(tmp_path / "events.db", measures=["heart_rate"], when="today")["heart_rate"]
    assert heart["readings"] == 2
    assert heart["usual"] == "only 0 of the 14 days before have readings, too few to say"


def test_steps_count_each_hour_once_and_say_the_first_hour_counts_whole(log: Path) -> None:
    """A total sent again stands as the later; hours that start mid-hour count that hour whole."""
    today = _call(log, measures=["steps"])["steps"]
    assert today["total"] == 2500  # eight hours of 300, and 100 for 9 am
    # The days before by 5:36 pm: nine whole hours, and 36 minutes of the hour still filling today.
    assert today["usual"]["median"] == 4800
    assert today["vs_usual"] == "below"
    part = _call(log, measures=["steps"], **{"from": "9:30", "until": "11:00"})
    assert part["steps"]["total"] == 400  # 9 am (100) and 10 am (300)
    assert part["steps"]["note"].startswith("Health keeps these by the hour")
    assert part["steps"]["by"] == ["9:00 am: 100 steps", "10:00 am: 300 steps"]
    assert part["summary"] == (
        "Today (Fri) 9:30 am to 11:00 am: 400 steps; usual 1,000 steps, so below it."
    )


def test_steps_over_days_are_a_day_over_the_full_days(log: Path) -> None:
    """A span that runs to now leaves today out of the day's average."""
    out = _call(log, measures=["steps"], when="past 3 days")
    steps = out["steps"]
    assert steps["total"] == 12500  # two days of 5,000 and today's 2,500
    assert (steps["per_day"], steps["full_days"]) == (5000, 2)
    assert steps["vs_usual"] == "within"
    assert steps["by"] == ["Wed Oct 21: 5,000 steps", "yesterday (Thu): 5,000 steps",
                           "today (Fri): 2,500 steps"]
    assert "12,500 steps in all; 5,000 steps a day over 2 full days" in out["summary"]


def test_his_sleep_is_the_night_he_woke_from_with_its_stages(log: Path) -> None:
    """「我睡咋样」, and 昨晚 for sleep, are the night he woke from today."""
    for args in ({"measures": ["sleep"]}, {"measures": ["sleep"], "when": "昨晚"}):
        out = _call(log, **args)
        assert out["sleep"]["nights"] == [{
            "asleep": "7h",
            "from": "yesterday (Thu) 11:30 pm",
            "to": "today (Fri) 6:40 am",
            "stages": {"deep": "1h", "core": "5h", "REM": "1h", "awake": "10m"},
            "woke": 1,
        }]
        assert out["sleep"]["usual"] == {
            "asleep": "7h 30m", "middle_half": "7h 30m to 7h 30m", "nights": 14,
        }
        assert out["sleep"]["vs_usual"] == "below"
        assert out["summary"] == (
            "Today (Fri) so far, to 5:36 pm: slept 7h, 11:30 pm to 6:40 am; usual 7h 30m, so"
            " below it."
        )


def test_sleep_over_days_is_the_average_night_with_the_shortest_and_longest(log: Path) -> None:
    """「我这几天睡眠分析一下」: every night of the span, its average, and the extremes."""
    sleep = _call(log, measures=["sleep"], when="最近3天")["sleep"]
    assert len(sleep["nights"]) == 3
    assert sleep["average"] == "7h 20m"
    assert sleep["shortest"] == "7h, woke today (Fri) 6:40 am"
    assert sleep["longest"] == "7h 30m, woke Wed Oct 21, 7:10 am"
    assert "no_sleep_recorded" not in sleep


def test_days_of_a_span_with_no_sleep_are_named(log: Path) -> None:
    """Unnamed, the brain took the span's first day for the first night it had."""
    out = _call(log, measures=["sleep"], when="the past 20 days")
    sleep = out["sleep"]
    assert sleep["no_sleep_recorded"] == [
        "Sun Oct 4", "Mon Oct 5", "Tue Oct 6", "Wed Oct 7", "Thu Oct 8",
    ]
    assert "no sleep recorded on 5 of the 20 days" in out["summary"]


def test_last_night_for_anything_but_sleep_says_how_to_ask(log: Path) -> None:
    """It spans two days: sleep is today's, the evening is yesterday's from 18:00."""
    said = _refused(log, measures=["heart_rate"], when="last night")
    assert "when=today" in said
    assert "from=18:00" in said


def test_the_overview_has_a_line_on_each_measure_recorded_and_names_the_rest(log: Path) -> None:
    """No measure named: one line each, and the measures with nothing recorded by name."""
    out = _call(log, when="yesterday")
    assert list(out["measures"]) == [
        "heart_rate", "sleep", "workouts", "steps", "stand_hours",
    ]
    assert out["measures"]["workouts"] == "1 workout, 42m: running"
    assert "resting_heart_rate" in out["not_recorded"]
    assert out["summary"].startswith("Yesterday (Thu): heart rate median 74 bpm")
    assert out.receipt.line == "read all his measures, yesterday (Thu)"


def test_a_workout_says_what_health_knew_of_it(log: Path) -> None:
    """Its activity, start, length, energy, distance and average heart rate."""
    out = _call(log, measures=["workouts"], when="yesterday")
    assert out["workouts"]["workouts"] == [
        "running, yesterday (Thu) 6:05 pm, 42m, 388 kcal, 6.12 km, average heart rate 148 bpm",
    ]


def test_one_reading_is_said_as_one_reading(log: Path) -> None:
    """A resting heart rate is one reading a day: its value and time, no low or high."""
    out = _call(log, measures=["resting_heart_rate"])
    assert out["summary"] == (
        "Today (Fri) so far, to 5:36 pm: resting heart rate 55 bpm, one reading, at 8:00 am."
    )
    assert out["resting_heart_rate"]["usual"] == (
        "only 0 of the 14 days before have readings, too few to say"
    )


def test_nothing_recorded_says_when_the_last_record_was(log: Path) -> None:
    """A measure never taken, and one not taken today, are told apart."""
    never = _call(log, measures=["hrv"])
    assert never["hrv"] == {"recorded": False, "nearest": {"before": "none in the 30 days before"}}
    assert never["summary"].endswith("no HRV (SDNN) recorded, nor in the 30 days before.")
    worked = _call(log, measures=["workouts"])
    assert worked["workouts"]["nearest"]["before"] == "yesterday (Thu) 6:47 pm"


def test_every_reading_of_an_hour_is_listed_on_request(log: Path) -> None:
    """detail=readings: each reading with its time and whether he was still or moving."""
    out = _call(
        log, measures=["heart_rate"], when="yesterday", detail="readings",
        **{"from": "17:00", "until": "17:30"},
    )
    assert out["heart_rate"]["each"][:2] == ["5:00 pm 68 bpm still", "5:05 pm 72 bpm still"]
    assert out["heart_rate"]["each"][2] == "5:10 pm 76 bpm"


@pytest.mark.parametrize(
    ("args", "says"),
    [
        ({"when": "tomorrow"}, "has not happened yet"),
        ({"from": "18:00"}, "today has not come yet"),
        ({"when": "yesterday", "from": "16:00", "until": "15:00"}, "is not after"),
        ({"when": "yesterday", "until": "now"}, "until=now is only for today"),
        ({"when": "past 3 days", "from": "14:00"}, "hours of one day"),
        ({"when": "past 40 days"}, "at most 31 days"),
        ({"measures": ["heart_rate"], "detail": "readings"}, "at most six hours"),
        ({"measures": ["sleep"], "detail": "readings"}, "one reading measure"),
        ({"measures": ["heart_rate"], "step": "day"}, "a day step is for several days"),
        ({"measures": ["heart_rate"], "step": "15min"}, "take a longer step"),
        ({"measures": ["pulse"]}, "no measure 'pulse'"),
        ({"from": "lunch"}, "a time of day"),
        ({"bogus": 1}, "no argument 'bogus'"),
    ],
)
def test_bad_arguments_say_what_to_do(log: Path, args: dict[str, Any], says: str) -> None:
    """Each refusal is a retry with other arguments, and says which."""
    assert says in _refused(log, **args)


def test_an_answer_too_long_loses_its_earliest_rows_and_says_so(
    log: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whole rows go, the earliest first, and the answer and its receipt say how many."""
    monkeypatch.setattr(body_tool, "_BUDGET", 1000)
    out = _call(
        log, measures=["heart_rate"], when="yesterday", detail="readings",
        **{"from": "16:00", "until": "18:00"},
    )
    each = out["heart_rate"]["each"]
    assert each[-1] == "5:55 pm 80 bpm"
    assert not each[0].startswith("4:10 pm")
    covered = out.receipt.covered
    assert covered is not None
    assert covered.more > 0
    assert out["cut"].startswith(f"{covered.more} rows left out to fit, the earliest first")


def test_a_log_it_cannot_open_is_unavailable(tmp_path: Path) -> None:
    """An event log that cannot be read is something she cannot do now, not empty health data."""
    (tool,) = build_body_tool(tmp_path / "missing.db", ZONE)
    with pytest.raises(ToolError) as caught:
        tool.handler({}, tool_context(clock=FakeClock(NOW)))
    assert caught.value.code == "unavailable"


def test_body_is_on_her_menu(tmp_path: Path) -> None:
    """Asked about his body on any turn, she has the tool without searching for it first."""
    recorded = tmp_path / "mcp-tools.json"
    recorded.write_text("{}", encoding="utf-8")
    config = yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    menu = {one["name"] for one in tool_exam.offered_tools(config, recorded)}
    assert "body" in menu
