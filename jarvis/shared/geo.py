"""Distance between two positions, for every layer that compares them."""

from __future__ import annotations

import math
from typing import Final

_EARTH_M: Final = 6_371_000.0


def metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres between two ``(lat, lng)`` points."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + (
        math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    )
    return 2 * _EARTH_M * math.asin(math.sqrt(h))
