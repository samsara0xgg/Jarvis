"""A small synthetic BC Transit export (ADR 0342): a zip of the files the timetable store reads.

Every stop, route and trip is invented. BOMs are written as BC Transit ships them.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class StopSpec:
    """One stop: where it is, what the pole says and what it is called."""

    stop_id: str
    name: str
    lat: float
    lng: float
    code: str = ""


@dataclass(frozen=True)
class TripSpec:
    """One trip of route ``route`` (its short name); ``calls`` are ``(stop_id, H:MM:SS)``."""

    trip_id: str
    route: str
    calls: tuple[tuple[str, str], ...]
    service: str = "WK"
    direction: int = 0
    headsign: str = ""


@dataclass(frozen=True)
class ServiceSpec:
    """A weekly service: its days (monday first, ``1``/``0``) between two dates (``YYYYMMDD``)."""

    service_id: str
    days: str
    first: str = "20260101"
    last: str = "20261231"


WEEKDAYS = ServiceSpec("WK", "1111100")
EVERY_DAY = ServiceSpec("WK", "1111111")
_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class Export:
    """The pieces of an export."""

    stops: tuple[StopSpec, ...]
    trips: tuple[TripSpec, ...]
    services: tuple[ServiceSpec, ...] = (EVERY_DAY,)
    dates: tuple[tuple[str, str, bool], ...] = field(default=())
    """``(service_id, YYYYMMDD, added)``: calendar_dates.txt rows."""
    zone: str = "America/Vancouver"
    calendars: bool = True
    """False: no calendar files at all."""


def write(path: Path, export: Export) -> None:
    """Write ``export`` as the zip ``path``."""
    routes = sorted({t.route for t in export.trips})
    rows = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]
    for trip in export.trips:
        rows += [
            f"{trip.trip_id},{at},{at},{stop},{n}" for n, (stop, at) in enumerate(trip.calls, 1)
        ]
    files = {
        "agency.txt": f"agency_id,agency_name,agency_timezone\n1,Test Transit,{export.zone}\n",
        "routes.txt": "route_id,route_short_name\n" + "".join(f"R{r},{r}\n" for r in routes),
        "trips.txt": "route_id,service_id,trip_id,trip_headsign,direction_id\n" + "".join(
            f"R{t.route},{t.service},{t.trip_id},{t.headsign},{t.direction}\n"
            for t in export.trips
        ),
        "stops.txt": "stop_id,stop_code,stop_name,stop_lat,stop_lon\n" + "".join(
            f"{s.stop_id},{s.code},{s.name},{s.lat},{s.lng}\n" for s in export.stops
        ),
        "stop_times.txt": "\n".join(rows) + "\n",
    }
    if export.calendars:
        files["calendar.txt"] = (
            "service_id," + ",".join(_NAMES) + ",start_date,end_date\n" + "".join(
                f"{s.service_id},{','.join(s.days)},{s.first},{s.last}\n" for s in export.services
            )
        )
        files["calendar_dates.txt"] = "service_id,date,exception_type\n" + "".join(
            f"{sid},{day},{1 if added else 2}\n" for sid, day, added in export.dates
        )
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, "﻿" + text)
