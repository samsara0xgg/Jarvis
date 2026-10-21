"""The rest of a bus answer's world (ADR 0342, 0346): Text Search, BC Transit's store and feed.

:func:`serve_text_search` answers the place reader's one Text Search from a hand-written table and
passes every other request to the transport already installed (``serve_routes``'s, say).
:func:`bus_live` is a real ``BusLive`` over a synthetic export (:mod:`tests.support.gtfs`), and
:func:`realtime` writes the TripUpdates feed it reads. Every place, stop and trip is invented or a
public landmark. No network.
"""

from __future__ import annotations

import io
import json
import urllib.request
from typing import TYPE_CHECKING, Any

from google.transit import gtfs_realtime_pb2

from jarvis.runtime.bus_live import BusLive
from tests.support import gtfs

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    import pytest

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"


def google_place(
    name: str, address: str, lat: float, lng: float, place_id: str,
) -> dict[str, Any]:
    """One place as Places Text Search returns it under the reader's field mask."""
    return {
        "id": place_id,
        "displayName": {"text": name, "languageCode": "en"},
        "formattedAddress": address,
        "location": {"latitude": lat, "longitude": lng},
    }


def serve_text_search(
    monkeypatch: pytest.MonkeyPatch, found: Mapping[str, dict[str, Any]],
) -> list[urllib.request.Request]:
    """Answer Text Search with ``found[textQuery]`` (missing: no places); returns the requests.

    Install it after the other fakes: any other URL goes to the ``urlopen`` in place now.
    """
    inner = urllib.request.urlopen
    asked: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if request.full_url != TEXT_SEARCH_URL:
            return inner(request, *args, **kwargs)
        asked.append(request)
        assert isinstance(request.data, bytes)
        hit = found.get(json.loads(request.data)["textQuery"])
        return io.BytesIO(json.dumps({"places": [hit]} if hit else {}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return asked


def realtime(*departures: tuple[str, str, int]) -> bytes:
    """A TripUpdates feed: each ``(trip_id, stop_id, epoch ms)`` is a predicted departure."""
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    for number, (trip, stop, ms) in enumerate(departures):
        update = message.entity.add(id=str(number)).trip_update
        update.trip.trip_id = trip
        update.stop_time_update.add(stop_id=stop).departure.time = ms // 1000
    return message.SerializeToString()


def bus_live(
    cache: Path, export: gtfs.Export, feed: Callable[[], bytes] = lambda: b"",
) -> BusLive:
    """A ``BusLive`` whose export is ``export`` (no download) and whose feed is ``feed()``."""
    cache.mkdir(parents=True, exist_ok=True)
    gtfs.write(cache / "bctransit-gtfs.zip", export)
    return BusLive(cache, feed=feed, download=lambda _p: None)
