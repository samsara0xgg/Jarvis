# ruff: noqa: RUF001 - the questions are Chinese, with its full-width punctuation.
r"""Weather items for the tool exam (ADR 0285): build ``cases-weather.json`` for ``tool_exam run``.

    .venv/bin/python -m scripts.tool_exam_weather_cases \
        --base ~/.jarvis/evals/tool-exam/cases.json \
        --out ~/.jarvis/evals/tool-exam/cases-weather.json

The base is a logged decision request (case ``wx-today``) whose system prompt and menu are the
program's own; each item keeps that prompt, replaces the conversation by the few turns it needs and
sets the clock to ``--today``. The file is private (it holds the prompt) and lives beside the other
cases; this script holds the questions, so a change to the tool's description is judged by running
it again. It also writes the second hop of a few items: the model has already called ``weather`` and
holds a faked WeatherKit result built by the test world, and must answer without another call.

The first-hop items score the first tool choice, the arguments (place, day, granularity) and, with
``max_calls``, that one call is enough. The scoring itself is ``tool_exam score``.
"""

from __future__ import annotations

import argparse
import copy
import json
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jarvis.execution.tools import build_default_registry
from jarvis.runtime.home import weather_lookup
from tests.integration.weatherkit_world import Net, kit_for, make_key

BASE_ID = "wx-today"
HOME = {"latitude": 48.48, "longitude": -123.38, "country_code": "CA"}
ZONE = "America/Vancouver"

# (id, said, history, expectation, tags); history is [(user, assistant), ...] before the question
Item = tuple[str, str, list[tuple[str, str]], dict[str, Any], list[str]]


def _first(ok: list[str], bad: list[str], **more: Any) -> dict[str, Any]:  # noqa: ANN401
    return {"first": {"ok": ok, "bad": bad, **more}}


def items(today: date) -> list[Item]:
    """The questions, with the dates they mean worked out from ``today``."""
    def d(n: int) -> str:
        return (today + timedelta(days=n)).isoformat()

    sat = (5 - today.weekday()) % 7 or 7
    sun, wed = sat + 1, 9 - today.weekday()  # the Wednesday of next week
    bad = ["web_search", "tool_search", "where_am_i"]
    one = {"max_calls": 1}
    home = {"weather": {"place": "*"}}
    return [
        ("wk-now-zh", "现在外面冷吗", [],
         _first(["weather"], bad, args_must_not=home, **one), ["weather", "now"]),
        ("wk-umbrella-zh", "我现在出门要带伞吗", [],
         _first(["weather"], bad, args_must_not={"weather": {"place": "*", "hourly": True}}, **one),
         ["weather", "next_hour"]),
        ("wk-rain-start-zh", "几点开始下雨？", [],
         _first(["weather"], bad, args_must_not=home, **one), ["weather", "next_hour"]),
        ("wk-tomorrow-en", "What's the weather like tomorrow?", [],
         _first(["weather"], bad, args_must_not={"weather": {"place": "*", "hourly": True}}, **one),
         ["weather", "day"]),
        ("wk-toronto-zh", "多伦多现在天气怎么样", [],
         _first(["weather"], bad, args_must={"weather": [
             {"place": "多伦多"}, {"place": "Toronto"},
         ]}, **one), ["weather", "place"]),
        ("wk-vancouver-sat-en", "What's the weather in Vancouver this Saturday?", [],
         _first(["weather"], bad, args_must={"weather": [
             {"place": "Vancouver", "start_date": d(sat)},
             {"place": "Vancouver, BC", "start_date": d(sat)},
         ]}, **one), ["weather", "place", "day"]),
        ("wk-next-wed-zh", "下周三温哥华会下雨吗", [],
         _first(["weather"], bad, args_must={"weather": [
             {"place": "温哥华", "start_date": d(wed)},
             {"place": "Vancouver", "start_date": d(wed)},
         ]}, **one), ["weather", "place", "day"]),
        ("wk-sunday-zh", "周日天气怎么样", [],
         _first(["weather"], bad, args_must={"weather": [{"start_date": d(sun)}]},
                args_must_not=home, **one), ["weather", "day"]),
        ("wk-hourly-en", "Hour by hour, what's the weather this afternoon?", [],
         _first(["weather"], bad, args_must={"weather": [{"hourly": True}]}, **one),
         ["weather", "hourly"]),
        ("wk-hourly-zh", "明天每个小时的温度", [],
         _first(["weather"], bad, args_must={"weather": [{"hourly": True}]}, **one),
         ["weather", "hourly"]),
        ("wk-alerts-zh", "最近有天气预警吗", [],
         _first(["weather"], bad, args_must_not=home, **one), ["weather", "alerts"]),
        ("wk-alerts-en", "Are there any storm warnings today?", [],
         _first(["weather"], bad, args_must_not=home, **one), ["weather", "alerts"]),
        ("wk-here-zh", "我这里现在下雨吗", [],
         _first(["weather"], bad, args_must={"weather": [{"place": "here"}]}, **one),
         ["weather", "here"]),
        ("wk-aqi-en", "How's the air quality today?", [],
         _first(["weather"], bad, args_must={"weather": [{"air_quality": True}]}, **one),
         ["weather", "air"]),
        ("wk-correct-place-zh", "我说的是多伦多的",
         [("明天天气怎么样", "明天维多利亚多云，最高14度，最低8度。")],
         _first(["weather"], bad, args_must={"weather": [
             {"place": "多伦多"}, {"place": "Toronto"},
         ]}, **one), ["weather", "place", "correction"]),
        ("wk-far-zh", "三个星期以后的天气怎么样", [],
         _first(["text", "web_search"], ["weather", "tool_search"]), ["weather", "range"]),
        ("wk-yesterday-zh", "昨天下过雨吗", [],
         _first(["text", "web_search"], ["weather", "tool_search"]), ["weather", "range"]),
        ("wk-news-zh", "最近有什么新闻", [],
         _first(["web_search"], ["weather"]), ["weather", "negative"]),
    ]


def _result() -> str:
    """The faked WeatherKit result of ``weather`` with no arguments, built by the tool itself."""
    with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as tmp:
        Net(mp)
        kit = kit_for(make_key(Path(tmp))[0])
        registry = build_default_registry(weather_lookup=weather_lookup(HOME, kit, ZONE))
        (tool,) = (t for t in registry.get_definitions() if t.name == "weather")
        reply = dict(tool.handler({}, None))  # type: ignore[arg-type,call-arg,misc]
    return json.dumps(reply, ensure_ascii=False)


def _second_hops(base: dict[str, Any], today: date) -> list[dict[str, Any]]:
    result = _result()
    hops = [
        ("wk2-now-zh", "现在外面冷吗"),
        ("wk2-rain-start-zh", "几点开始下雨？"),
        ("wk2-alerts-en", "Are there any storm warnings today?"),
    ]
    out = []
    for case_id, said in hops:
        case = _case(base, case_id, said, [], today, ["weather", "second-hop"])
        case["request"]["input"] += [
            {"type": "function_call", "call_id": "call_wk", "name": "weather",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_wk", "output": result},
        ]
        case["expect"] = _first(["text"], ["weather", "web_search", "tool_search"])
        out.append(case)
    return out


def _header(today: date) -> str:
    """The state header of a turn at 09:30 in Victoria on ``today``."""
    stamp = f"{today.isoformat()}T09:30-07:00 {today.strftime('%A')}"
    return (
        "[State when this was said | from the program, not the user's words]\n"
        f"Time: {stamp}\nChannel: voice\nWhere you run: on this Mac."
    )


def _case(  # noqa: PLR0913 - the base request and the item's id, words, history, day and tags.
    base: dict[str, Any], case_id: str, said: str, history: list[tuple[str, str]], today: date,
    tags: list[str],
) -> dict[str, Any]:
    request = copy.deepcopy(base["request"])
    language = "Chinese" if any("一" <= ch <= "鿿" for ch in said) else "English"

    def turn(text: str) -> dict[str, str]:
        line = f"[Reply language for this turn: {language}]"
        return {"role": "user", "content": f"{_header(today)}\n\n{text}\n\n{line}"}

    messages: list[dict[str, str]] = []
    for user, assistant in history:
        messages += [turn(user), {"role": "assistant", "content": assistant}]
    request["input"] = [*messages, turn(said)]
    return {"id": case_id, "source": "weatherkit-exam", "said": said, "request": request,
            "tags": tags}


def main() -> int:
    """Write the cases file."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    today = datetime.now().astimezone().date()
    parser.add_argument("--today", type=date.fromisoformat, default=today)
    args = parser.parse_args()
    base = next(c for c in json.loads(args.base.read_text(encoding="utf-8")) if c["id"] == BASE_ID)
    cases = []
    for case_id, said, history, expect, tags in items(args.today):
        case = _case(base, case_id, said, history, args.today, tags)
        case["expect"] = expect
        cases.append(case)
    cases += _second_hops(base, args.today)
    args.out.write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
