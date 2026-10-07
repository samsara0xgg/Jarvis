"""The ``job_ledger`` tool: the Dashboard's ledger, read by voice (ADR 0155)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from jarvis.execution.job_ledger_tool import MAX_APPLICATIONS, build_job_ledger_tool
from jarvis.execution.tools import Tool, ToolContext, ToolRegistry
from jarvis.runtime import _job_mail, _register_job_ledger
from jarvis.shared import CallerPrincipal
from tests.integration.test_job_mail import (  # noqa: F401 - the fixtures and harness of its sibling
    _harness,
    _zh,
    jev,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.test_job_mail import _Jev

_CTX = cast("ToolContext", None)


def _the_tool(read: Any) -> Tool:  # noqa: ANN401 - a ledger read
    (tool,) = build_job_ledger_tool(read)
    return tool


def _group(n: int, **over: Any) -> dict[str, Any]:  # noqa: ANN401
    mail = {
        "message_id": f"m{n}",
        "kind": "interview",
        "received_at": "2026-10-05T09:00:00+00:00",
        "subject": f"Interview with Company{n}",
        "event_at": "2026-10-09T15:00:00+00:00",
        "event_text": "Friday 3pm",
    }
    return {
        "company": f"Company{n}",
        "role": "Backend Co-op",
        "kind": "interview",
        "last_at": mail["received_at"],
        "next_event_at": mail["event_at"],
        "count": 1,
        "mails": [mail],
        "time_total_s": 600,
        **over,
    }


def test_the_tool_is_l0_read_only_for_the_model_and_absent_while_job_mail_is_off() -> None:
    """Off (``_job_mail`` is None): no tool. On: the declared shape, in the registry once."""
    assert _job_mail({}, cast("Any", None), None, cast("Any", None), cast("Any", None)) is None
    assert build_job_ledger_tool(None) == ()
    off = ToolRegistry()
    _register_job_ledger(off, None)
    assert [one.name for one in off.get_definitions()] == []
    tool = _the_tool(dict)
    assert tool.name == "job_ledger"
    assert (tool.risk_level, tool.read_only) == ("L0", True)
    assert tool.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert tool.input_schema.get("required") is None
    assert "job applications, interviews" in tool.description
    assert tool.description.isascii()
    registry = ToolRegistry()
    registry.register(tool)
    assert [one.name for one in registry.for_caller(CallerPrincipal.JARVIS_LLM)] == ["job_ledger"]
    on = ToolRegistry()
    _register_job_ledger(on, cast("Any", SimpleNamespace(ledger=dict)))
    assert [one.name for one in on.get_definitions()] == ["job_ledger"]


def test_a_row_reads_stage_last_mail_next_event_and_time_spent() -> None:
    """One company's row says what a voice answer needs and nothing else."""
    past = _group(
        2,
        kind="rejection",
        next_event_at=None,
        time_total_s=0,
        mails=[
            {
                "message_id": "m2",
                "kind": "rejection",
                "received_at": "2026-10-06T09:00:00+00:00",
                "subject": "x" * 200,
                "event_at": None,
                "event_text": None,
            }
        ],
    )
    out = _the_tool(lambda: {"ledger": [_group(1), past], "skipped": [{}, {}, {}]}).handler(
        {}, _CTX
    )
    first, second = out["applications"]
    assert first == {
        "company": "Company1",
        "role": "Backend Co-op",
        "stage": "interview",
        "mails": 1,
        "last_mail_at": "2026-10-05T09:00:00+00:00",
        "last_subject": "Interview with Company1",
        "spent_s": 600,
        "next_event_at": "2026-10-09T15:00:00+00:00",
        "next_event": "Friday 3pm",
    }
    assert second["stage"] == "rejection"
    assert len(second["last_subject"]) == 80
    assert "next_event_at" not in second
    assert (out["total"], out["truncated"], out["held_back_mails"]) == (2, False, 3)


def test_the_result_is_capped_in_rows_and_fits_the_dispatch_limit() -> None:
    """More rows than the cap are counted, not sent; the JSON stays under ``max_result_chars``."""
    long = {**_group(0)["mails"][0], "subject": "s" * 500}
    many = [_group(n, company="C" * 300, role="R" * 300, mails=[long]) for n in range(60)]
    tool = _the_tool(lambda: {"ledger": many, "skipped": []})
    out = tool.handler({}, _CTX)
    assert len(out["applications"]) == MAX_APPLICATIONS
    assert (out["total"], out["truncated"]) == (60, True)
    assert len(json.dumps(out)) < tool.max_result_chars
    empty = _the_tool(lambda: {"ledger": [], "skipped": []}).handler({}, _CTX)
    assert empty == {"applications": [], "total": 0, "truncated": False, "held_back_mails": 0}


def test_it_reads_the_ledger_the_dashboard_serves(tmp_path: Path, jev: _Jev) -> None:  # noqa: F811
    """Wired to ``JobMail.ledger`` (what ``GET /inherent/jobs`` serves), one row per its group."""
    h = _harness(tmp_path, jev)
    h.job.poll_once()
    served = h.client.get("/inherent/jobs").json()
    assert served["ledger"]
    out = _the_tool(h.job.ledger).handler({}, _CTX)
    assert [(a["company"], a["role"], a["stage"]) for a in out["applications"]] == [
        (g["company"], g["role"], g["kind"]) for g in served["ledger"]
    ]
    assert out["held_back_mails"] == len(served["skipped"])
    # A mail Allen deletes leaves the Dashboard and the tool together.
    only = next(g for g in served["ledger"] if g["count"] == 1)
    gone = only["mails"][0]["message_id"]
    assert h.client.post(f"/inherent/jobs/{gone}/delete").status_code == 200
    again = _the_tool(h.job.ledger).handler({}, _CTX)
    assert again["total"] == out["total"] - 1
    assert only["company"] not in {a["company"] for a in again["applications"]}
