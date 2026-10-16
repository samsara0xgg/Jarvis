"""ADR 0237, 0284: an address to a point, and a point to its street, by Google's Geocoding API.

The API takes its key as the ``key`` query parameter, so the request URL carries the key. No URL,
no address and no exception that holds the URL leaves this module: :class:`Unplaced` says only
what went wrong.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final

_URL: Final = "https://maps.googleapis.com/maps/api/geocode/json"
_TIMEOUT_S: Final = 10.0
_MESSAGE_CHARS: Final = 160


class Unplaced(Exception):  # noqa: N818 — what happened to the address, not an error kind
    """Why an address got no point; the message never carries the key or the address."""


def reverse(
    lat: float, lng: float, api_key: str, *, timeout_s: float = _TIMEOUT_S,
) -> dict[str, str]:
    """The street address and the intersection Google gives a point; absent keys were not found.

    One request filtered to ``street_address|intersection`` results. ``ZERO_RESULTS`` is an
    answer (an empty dict, and it is billed).

    Raises:
        Unplaced: the request failed or the API's status is neither ``OK`` nor ``ZERO_RESULTS``.
    """
    query = urllib.parse.urlencode({
        "latlng": f"{lat:.6f},{lng:.6f}", "result_type": "street_address|intersection",
        "key": api_key,
    })
    body = _get(f"{_URL}?{query}", timeout_s)
    status = body.get("status")
    if status == "ZERO_RESULTS":
        return {}
    _require_ok(body)
    found: dict[str, str] = {}
    for result in body.get("results") or []:
        types, text = result.get("types") or [], str(result.get("formatted_address") or "")
        if "intersection" in types and "intersection" not in found:
            found["intersection"] = text.split(",", maxsplit=1)[0].strip()
        elif "street_address" in types and "address" not in found:
            found["address"] = text.split(",", maxsplit=1)[0].strip()
            route = next(
                (c.get("long_name") for c in result.get("address_components") or []
                 if "route" in c.get("types", [])), None,
            )
            if route:
                found["street"] = str(route)
    return found


def _get(url: str, timeout_s: float) -> dict[str, Any]:
    """The JSON object at ``url``; nothing that holds the URL (it carries the key) escapes."""
    request = urllib.request.Request(url)  # noqa: S310 — a fixed https URL
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            body: Any = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        msg = f"HTTP {exc.code}"
        raise Unplaced(msg) from None
    except (OSError, ValueError) as exc:
        raise Unplaced(type(exc).__name__) from None
    if not isinstance(body, dict):
        msg = "an answer that is not an object"
        raise Unplaced(msg)
    return body


def _require_ok(body: dict[str, Any]) -> None:
    status = body.get("status")
    if status != "OK":
        said = str(body.get("error_message") or "")[:_MESSAGE_CHARS]
        msg = f"status {status}" + (f": {said}" if said else "")
        raise Unplaced(msg)


def geocode(address: str, api_key: str, *, timeout_s: float = _TIMEOUT_S) -> tuple[float, float]:
    """The one point Google gives ``address``, as ``(lat, lng)``.

    Raises :class:`Unplaced` when the request fails, the API's status is not ``OK``, the answer
    names more than one place, or its precision is ``APPROXIMATE`` (a region, not a place).
    """
    query = urllib.parse.urlencode({"address": address, "key": api_key})
    body = _get(f"{_URL}?{query}", timeout_s)
    _require_ok(body)
    results = body.get("results")
    if not isinstance(results, list) or len(results) != 1:
        count = len(results) if isinstance(results, list) else 0
        msg = f"{count} results"
        raise Unplaced(msg)
    try:
        geometry = results[0]["geometry"]
        lat, lng = float(geometry["location"]["lat"]), float(geometry["location"]["lng"])
        precision = str(geometry.get("location_type") or "")
    except (KeyError, TypeError, ValueError):
        msg = "a result without a location"
        raise Unplaced(msg) from None
    if precision == "APPROXIMATE":
        msg = "only an approximate match"
        raise Unplaced(msg)
    return lat, lng
