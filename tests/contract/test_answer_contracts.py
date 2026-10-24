"""``links``: the rows of his day her answer is about (ADR 0391), as the phone reads them.

One day of one fixed world. ``my_day`` reads it as the brain does, with the runtime's rows, in a
live turn; her answer's claims block names three rows, one of which she was never shown; the
links are what the turn's ``surface.response_emitted`` row carries to the phone (over
``/phone/ws`` and on ``GET /inherent/conversation``).
"""

from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING, Any

from jarvis.execution.my_day_tool import build_my_day_tool
from jarvis.execution.tools import register_live_action, release_turn_actions
from jarvis.runtime.day_rows import DayRows
from jarvis.runtime.turn.finish import turn_links
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.ledger import LedgerSources
from tests.support.clock import FakeClock
from tests.support.links import NOW, NOW_MS, ZONE, page_world
from tests.support.tool_context import tool_context

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.contracts import Contracts


def test_her_answer_links_to_the_rows_she_read_and_named(
    tmp_path: Path, contracts: Contracts,
) -> None:
    """A stay and an outing she was shown; the row she was not shown is not a link."""
    log, sources = page_world(tmp_path)
    assert sources.memory_db is not None
    ledger = LedgerSources(sources.memory_db, log, sources.timesink, ZONE)
    (tool,) = build_my_day_tool(ledger, DayRows(log, sources).read)
    register_live_action(turn_id="T1", action_id="A1")
    try:
        with closing(open_event_log(log)) as conn:
            day = tool.handler({}, tool_context(conn, "A1", clock=FakeClock(NOW)))
            ids = [line.rsplit("(id ", 1)[1].rstrip(")") for line in day["timeline"]]
            outing = next(one for one in ids if one.startswith("outing-"))
            show: list[dict[str, Any]] = [
                {"id": "stay-cafe", "what": "街角咖啡馆"},
                {"id": outing, "what": "走去咖啡馆"},
                {"id": "stay-somewhere-else", "what": "没读过的"},
            ]
            emit_event(
                conn, type="answer.claims_checked",
                payload={
                    "turn_id": "T1", "had_block": True, "entries": [], "sent_back": False,
                    "show": show,
                },
                correlation={"turn_id": "T1"},
            )
            links = turn_links(conn, "T1", lambda: None)
    finally:
        release_turn_actions("T1")

    assert [link["id"] for link in links] == ["stay-cafe", outing]
    contracts.output(
        "answer/links",
        example={"links": links},
        exact=("kind", "date"),
        now_ms=NOW_MS,
        route="surface.response_emitted (the /phone/ws row frame, GET /inherent/conversation)",
        description=(
            "The rows of his day her answer is about, on the answer's `surface.response_emitted` "
            "payload and on her row of `GET /inherent/conversation`; absent when there are none. "
            "At most 3, in her order. `kind` `row` is an item or span id of `GET /inherent/day` "
            "for `date`; `outing` is an outing id of `GET /inherent/footprint` for `date`. "
            "`start_ms` and `end_ms` are always set (a row still going ends at the time it was "
            "read): when the id is no longer on the page, open the row covering `start_ms`. "
            "`title` is at most 20 characters, in his language as she wrote it."
        ),
    )
