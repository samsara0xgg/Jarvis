"""The whole-turn runner meters its spend and quotes a run before it is paid for (ADR 0317).

Prices come from a table of the test's own, so the figures are exact: a model that costs $1 per
million prompt tokens and $5 per million answer tokens, and one five times dearer.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jarvis.decision.llm import ChatResult
from scripts import tool_exam_steps as steps

TABLE = {
    "small": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    "large": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
}


def _usage(i: int, o: int, read: int = 0, write: int = 0) -> dict[str, int]:
    return {"input": i, "output": o, "cache_read": read, "cache_write": write}


def _line(
    case: str, calls: list[dict[str, int]], total: dict[str, int] | None = None
) -> dict[str, Any]:
    held = {k: sum(c[k] for c in calls) for k in calls[0]}
    return {
        "kind": "run",
        "case": case,
        "steps": [{"usage": c} for c in calls],
        "usage": total or held,
        "error": None,
    }


# (measured runs, cases asked, margin, runs, usd per pass with the margin, guessed cases)
QUOTES = [
    # one call of 1M in / 0.1M out on "large": 5 + 2.5 = $7.50 per run
    ([_line("a", [_usage(1_000_000, 100_000)])], ["a"], 0.0, 1, 7.5, 0),
    # the margin is added to the figure, the runs multiply the pass
    ([_line("a", [_usage(1_000_000, 100_000)])], ["a"], 0.2, 3, 9.0, 0),
    # a case never measured is priced at the median of the measured ones
    (
        [_line("a", [_usage(1_000_000, 0)]), _line("b", [_usage(3_000_000, 0)])],
        ["a", "b", "c"],
        0.0,
        1,
        5.0 + 15.0 + 10.0,
        1,
    ),
    # usage the steps do not hold (decide()'s side requests) is one more call
    ([_line("a", [_usage(1_000_000, 0)], _usage(1_200_000, 0))], ["a"], 0.0, 1, 5.0 + 1.0, 0),
    # cache reads are billed at their own rate: 1M prompt, 0.8M of it read from cache
    ([_line("a", [_usage(1_000_000, 0, read=800_000)])], ["a"], 0.0, 1, 0.2 * 5.0 + 0.8 * 0.5, 0),
]


@pytest.mark.parametrize(("measured", "asked", "margin", "runs", "per_pass", "guessed"), QUOTES)
def test_quote_prices_the_cases_from_the_tokens_a_run_measured(  # noqa: PLR0913 - table columns
    measured: list[dict[str, Any]],
    asked: list[str],
    margin: float,
    runs: int,
    per_pass: float,
    guessed: int,
) -> None:
    """Measured tokens at the target model's rates, plus the margin, times the runs."""
    got = steps.quote(
        measured,
        [{"id": c} for c in asked],
        "large",
        TABLE,
        runs=runs,
        margin=margin,
    )
    assert got["usd_per_pass"] == pytest.approx(per_pass, rel=1e-4)
    assert got["usd_total"] == pytest.approx(got["usd_per_pass"] * runs, rel=1e-4)
    assert got["guessed"] == guessed


def test_quote_refuses_a_model_with_no_price_and_a_run_with_nothing_measured() -> None:
    """No price, or no finished run, is no quote."""
    with pytest.raises(SystemExit, match="no price"):
        steps.quote(
            [_line("a", [_usage(10, 10)])], [{"id": "a"}], "nameless", TABLE, runs=1, margin=0.0
        )
    with pytest.raises(SystemExit, match="no finished run"):
        steps.quote(
            [{**_line("a", [_usage(10, 10)]), "error": "boom"}],
            [{"id": "a"}],
            "large",
            TABLE,
            runs=1,
            margin=0.0,
        )


def _answer(model: str, tokens_in: int, tokens_out: int) -> ChatResult:
    return ChatResult(
        text="ok",
        tool_calls=(),
        finish_reason="stop",
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        raw={},
        model_used=model,
    )


def test_the_meter_adds_each_response_and_stops_before_the_call_that_would_cross_the_cap() -> None:
    """$1 a million in, $5 out: two 100k/10k answers cost $0.30; a third would pass $0.40."""
    meter = steps.Meter(0.40, TABLE)
    for _ in range(2):
        meter.before("small", 400_000)  # ~133k tokens in, 400 out: about $0.13
        meter.after(_answer("small", 100_000, 10_000))
    assert meter.spent == pytest.approx(0.30)
    assert meter.calls == 2
    with pytest.raises(steps.SpendCap, match="would take"):
        meter.before("small", 400_000)
    assert meter.capped
    assert "stopped at the cap" in meter.report()
    assert meter.report().startswith("spend: $0.3000 of $0.4 over 2 model calls")


def test_the_meter_cannot_keep_a_cap_on_a_model_it_cannot_price() -> None:
    """An unpriced model under a cap is refused rather than run unmetered."""
    meter = steps.Meter(1.0, TABLE)
    with pytest.raises(steps.SpendCap, match="no price"):
        meter.before("nameless", 100)
    unlimited = steps.Meter(None, TABLE)
    unlimited.before("nameless", 100)  # no cap, nothing to keep
    unlimited.after(_answer("small", 1_000_000, 0))
    assert unlimited.spent == pytest.approx(1.0)


def test_the_substituting_client_meters_every_chat_it_sends() -> None:
    """The seam every model call of a case goes through is the one that counts them."""
    model = steps.ScriptedModel([{"text": "one", "usage": [200_000, 0]}, {"text": "two"}])
    model.model = "small"
    meter = steps.Meter(None, TABLE)
    sub = steps._Substituting(model, "sys", [{"role": "user", "content": "hi"}], meter)  # noqa: SLF001
    sub.chat(messages=[{"role": "user", "content": "hi"}], system="sys")
    assert meter.calls == 1
    assert json.dumps(meter.report())
