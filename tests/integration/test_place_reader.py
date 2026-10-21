"""ADR 0346: the one reader of his words for a place, and `save_place` keeping what it finds.

Google is a stub: `urllib.request.urlopen` is replaced, and a request to a host it does not know
fails the test. Every place is invented.
"""

from __future__ import annotations

import io
import json
import urllib.request
from contextlib import closing
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution import place_reader, places_api
from jarvis.execution.place_tools import build_place_tools
from jarvis.execution.tools import ToolError
from jarvis.state import places
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import open_memory_db
from tests.support.clock import FakeClock
from tests.support.here import FixedHere, reading
from tests.support.tool_context import tool_context

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.execution.tools import ToolContext

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=ZoneInfo("America/Vancouver"))
HERE = reading(48.4284, -123.3656, 30.0, age_s=30, device="iphone", fresh=True)
SAVED = {"home": "48.48,-123.38", "school": "Example Campus, Example City"}


def _branch(n: int, north_m: float) -> dict[str, Any]:
    """An invented coffee-shop branch ``north_m`` metres north of HERE."""
    return {
        "id": f"ChIJbranch{n}0123456789abcdef",
        "displayName": {"text": "Bean Counter"},
        "formattedAddress": f"{n} Government St, Victoria, BC, Canada",
        "location": {"latitude": HERE.lat + north_m / 111_320, "longitude": HERE.lng},
        "viewport": {"low": {"latitude": HERE.lat, "longitude": HERE.lng - 0.001},
                     "high": {"latitude": HERE.lat + 0.002, "longitude": HERE.lng + 0.001}},
    }


class Google:
    """Answers Text Search and Place Details; ``seen`` is what was asked."""

    def __init__(self) -> None:
        """Start with no answers."""
        self.seen: list[urllib.request.Request] = []
        self.text: dict[str, Any] = {}
        self.named: dict[str, Any] = {}

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> io.BytesIO:  # noqa: ARG002
        """Answer one request."""
        self.seen.append(request)
        url = request.full_url
        if url == "https://places.googleapis.com/v1/places:searchText":
            return io.BytesIO(json.dumps(self.text).encode())
        if url.startswith("https://places.googleapis.com/v1/places/"):
            return io.BytesIO(json.dumps(self.named).encode())
        msg = f"a request to a host the test does not stub: {url}"
        raise AssertionError(msg)


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Google:
    """The stub, installed."""
    stub = Google()
    monkeypatch.setattr(urllib.request, "urlopen", stub)
    return stub


@pytest.fixture
def log(tmp_path: Path) -> Any:  # noqa: ANN401 - a sqlite connection, closed by the test's end
    """An event log."""
    conn = open_event_log(tmp_path / "events.db")
    yield conn
    conn.close()


def _reader(db: Path | None = None) -> place_reader.PlaceReader:
    return place_reader.PlaceReader(
        "test-key-not-real", places_api.PlaceBook(), saved=lambda: SAVED,
        kept=None if db is None else lambda: places.read_kept(db),
    )


def _ctx(log: sqlite3.Connection) -> ToolContext:
    return tool_context(log, clock=FakeClock(NOW), here=FixedHere(HERE).bind(None))


def test_here_a_point_and_a_saved_point_cost_no_request(
    google: Google, log: sqlite3.Connection,
) -> None:
    """The first steps of the order are free: here, lat,lng and a saved home point."""
    reader, ctx = _reader(), _ctx(log)
    meter: list[str] = []
    assert reader.read("here", ctx, meter).point == (HERE.lat, HERE.lng)
    assert reader.read("48.5, -123.4", ctx, meter).point == (48.5, -123.4)
    assert reader.read("Home", ctx, meter).point == (48.48, -123.38)
    assert not google.seen
    assert meter == []


def test_a_chain_name_is_one_search_ranked_by_distance_near_here(
    google: Google, log: sqlite3.Connection,
) -> None:
    """The request asks for the nearest, and the answer is said by name and address."""
    google.text = {"places": [_branch(1, 400)]}
    meter: list[str] = []
    found = _reader().read("bean counter", _ctx(log), meter)

    [request] = google.seen
    body = json.loads(request.data)  # type: ignore[arg-type]
    assert body["rankPreference"] == "DISTANCE"
    assert body["maxResultCount"] == 1
    assert body["locationBias"]["circle"]["radius"] == 50_000
    assert (request.get_header("X-goog-fieldmask") or "").split(",")[-1] == "places.viewport"
    assert found.said == "Bean Counter (1 Government St, Victoria, BC, Canada)"
    assert found.bounds is not None
    assert meter == ["places.text_search.pro"]


def test_without_a_position_the_search_is_unbiased_and_says_so(
    google: Google, log: sqlite3.Connection,
) -> None:
    """No Here: no bias and no distance ranking, and the find is marked unbiased."""
    google.text = {"places": [_branch(1, 400)]}
    gone = FixedHere(error=ToolError("no", code="x"))
    ctx = tool_context(log, clock=FakeClock(NOW), here=gone.bind(None))
    found = _reader().read("bean counter", ctx, [])
    body = json.loads(google.seen[0].data)  # type: ignore[arg-type]
    assert "locationBias" not in body
    assert "rankPreference" not in body
    assert found.unbiased


def test_search_off_answers_only_what_is_free(google: Google, log: sqlite3.Connection) -> None:
    """With search=False a name that needs Google is not_found, and nothing is sent."""
    with pytest.raises(ToolError) as caught:
        _reader().read("bean counter", _ctx(log), [], search=False)
    assert caught.value.code == "not_found"
    assert not google.seen


def test_the_cap_guard_runs_before_a_request(google: Google, log: sqlite3.Connection) -> None:
    """A refusal from the guard stops the search before it is sent."""
    def refuse(_ctx: ToolContext) -> None:
        msg = "cap"
        raise ToolError(msg, code="spend_cap")

    reader = place_reader.PlaceReader(
        "k", places_api.PlaceBook(), saved=lambda: SAVED, before_request=refuse,
    )
    with pytest.raises(ToolError) as caught:
        reader.read("bean counter", _ctx(log), [])
    assert caught.value.code == "spend_cap"
    assert not google.seen


def test_save_place_with_an_address_keeps_the_readers_point_and_undoes(
    google: Google, log: sqlite3.Connection, tmp_path: Path,
) -> None:
    """The address is read once; the place is kept there; the receipt names it and its change."""
    db = tmp_path / "memory.db"
    with closing(open_memory_db(db)):
        pass
    google.text = {"places": [_branch(1, 400)]}
    (_, save, _) = build_place_tools(db, _reader(db))
    out = save.handler({"name": "Bean", "where": "1 Government St"}, _ctx(log))

    (kept,) = places.read_kept(db)
    assert kept.name == "Bean"
    assert kept.lat == pytest.approx(HERE.lat + 400 / 111_320)
    receipt = out.receipt  # type: ignore[attr-defined]
    assert receipt.object == kept.id
    assert receipt.undo is not None
    assert len(google.seen) == 1
