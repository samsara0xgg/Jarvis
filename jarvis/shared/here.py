"""ADR 0322: the one Here a tool call reads where he is from.

The runtime builds one source of readings (the Locator plus one policy) and hands each call a
:class:`Here` bound to its turn. A tool asks it and nothing else, so a position has one device,
one age limit and one way of being fetched. The reading it last gave rides on the call's receipt.

Layer rules: stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class HereUnavailable(LookupError):  # noqa: N818 - reads as the fact it states
    """A device that should know where he is could not say (denied, a helper failure)."""


@dataclass(frozen=True)
class HereReading:
    """Where he is, from one device, and how far to trust it.

    ``fresh``: a fix taken for this call. ``why``: when a fresh fix was asked for and did not
    come, the reason in words (the reading is then the last report, ``fresh`` false); empty
    otherwise. ``snapshot`` is the id of a picture the phone sent with a fresh fix.
    """

    lat: float
    lng: float
    accuracy_m: float
    age_s: int
    device: str
    fresh: bool
    place: str | None = None
    why: str = ""
    snapshot: str | None = None

    def to_json(self) -> dict[str, Any]:
        """The receipt's form: the keys that have a value."""
        out: dict[str, Any] = {
            "lat": self.lat, "lng": self.lng, "accuracy_m": self.accuracy_m,
            "age_s": self.age_s, "device": self.device, "fresh": self.fresh,
        }
        for key in ("place", "why", "snapshot"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> HereReading:
        """Read the receipt's form; ``ValueError`` when it is not one."""
        try:
            return cls(
                float(data["lat"]), float(data["lng"]), float(data["accuracy_m"]),
                int(data["age_s"]), str(data["device"]), data["fresh"] is True,
                place=data.get("place"), why=data.get("why", ""), snapshot=data.get("snapshot"),
            )
        except (KeyError, TypeError) as exc:
            msg = f"not a here reading: {exc!r}"
            raise ValueError(msg) from exc


@dataclass(frozen=True)
class Coarse:
    """Roughly where he is, read without asking a device for a fix (ADR 0356).

    The last phone report up to a day old, else home (``home`` true).
    """

    lat: float
    lng: float
    home: bool = False


class Here:
    """One call's view of where he is; remembers the reading the call used.

    ``read`` is where he is under the policy, ``fresh`` always asks for a new fix. Either raises
    :class:`~jarvis.state.phone_location.NoPhoneReport` when no report exists and none came, or
    :class:`HereUnavailable`. ``coarse`` is roughly where he is, for a choice that a few
    kilometres do not change.
    """

    def __init__(
        self, read: Callable[[], HereReading], fresh: Callable[[], HereReading],
        coarse: Callable[[], Coarse | None] = lambda: None,
    ) -> None:
        """Bind the readings of one turn; ``coarse`` never asks a device for a fix."""
        self._read = read
        self._fresh = fresh
        self._coarse = coarse
        self.used: HereReading | None = None

    def read(self) -> HereReading:
        """Where he is: the device's report when it is recent enough, else a fresh fix."""
        self.used = self._read()
        return self.used

    def fresh(self) -> HereReading:
        """Where he is now: always asks for a new fix, falling back to the last report."""
        self.used = self._fresh()
        return self.used

    def coarse(self) -> Coarse | None:
        """Roughly where he is: the last report within a day, else home; None when neither is known.

        It never asks a device and never sets the reading a receipt carries.
        """
        return self._coarse()


class HereSource(Protocol):
    """What the runtime builds once: a :class:`Here` for the turn a call belongs to."""

    def bind(self, turn_id: str | None) -> Here:
        """The Here of ``turn_id``; ``None`` for a call outside any turn."""
        ...
