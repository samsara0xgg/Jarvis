"""Where he is, as a tool is handed it: a Here that answers what the test says, and a policy rig.

:class:`FixedHere` is the source a registry takes (``build_default_registry(here=...)``) when the
test is about a tool, not about how Here chooses: every call is bound to a Here that gives the same
reading, or raises what the test names. The reading and the Mac's fix are built by
:func:`reading` and :func:`mac_reading`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.shared.here import Coarse, Here, HereReading
from jarvis.state.event_log import MAC_NODE

if TYPE_CHECKING:
    from collections.abc import Callable


def reading(  # noqa: PLR0913 - a reading is these columns.
    lat: float = 48.46, lng: float = -123.31, accuracy_m: float = 40.4, *, age_s: int = 0,
    device: str = "phone", fresh: bool = False, place: str | None = "Ring Rd, Saanich",
    why: str = "", snapshot: str | None = None,
) -> HereReading:
    """One reading of where he is; the defaults are the Saanich fix the tool tests share."""
    return HereReading(
        lat, lng, accuracy_m, age_s, device, fresh, place=place, why=why, snapshot=snapshot,
    )


def mac_reading(**changes: object) -> HereReading:
    """This Mac's own fix: taken now, from ``MAC_NODE``."""
    base: dict[str, object] = {"device": MAC_NODE, "fresh": True, "age_s": 0}
    return reading(**{**base, **changes})  # type: ignore[arg-type]


class FixedHere:
    """A source whose every Here answers ``answer`` (a reading, or a call that makes one).

    ``coarse`` is what each Here's :meth:`~jarvis.shared.here.Here.coarse` says, never counted
    in ``asked``: it asks no device.
    """

    def __init__(
        self, answer: HereReading | Callable[[], HereReading] | None = None,
        *, error: Exception | None = None, coarse: Coarse | None = None,
    ) -> None:
        self._answer = answer
        self._error = error
        self._coarse = coarse
        self.asked: list[tuple[str | None, bool]] = []
        """``(turn id, fresh?)`` of each reading asked for."""

    def bind(self, turn_id: str | None) -> Here:
        return Here(
            lambda: self._give(turn_id, fresh=False), lambda: self._give(turn_id, fresh=True),
            lambda: self._coarse,
        )

    def _give(self, turn_id: str | None, *, fresh: bool) -> HereReading:
        self.asked.append((turn_id, fresh))
        if self._error is not None:
            raise self._error
        if self._answer is None:
            return reading()
        return self._answer() if callable(self._answer) else self._answer
