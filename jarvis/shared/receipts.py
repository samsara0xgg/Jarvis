"""ADR 0319: the receipt every tool call ends in.

A receipt is the tool's own account of what one call did: whether it only read, what it changed,
one line she can say, and one of four statuses. Only ``done`` is a success. The dispatcher keeps
it in the call's ``action.result_observed`` row (``receipt``); a tool that writes none gets a
derived one (:func:`derive`), flagged ``derived``.

Layer rules: stdlib only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, cast, get_args

from jarvis.shared.here import HereReading

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.shared import RiskLevel

Status = Literal["done", "pending", "needs_choice", "failed"]
"""``pending``: accepted, not yet confirmed. ``needs_choice``: the tool stopped to ask."""

FailureKind = Literal["needs_him", "impossible", "retry_other_args", "dont_retry"]
"""The spec's four kinds of failure: what only he can do, what cannot be done, what another
argument may fix, and what must not be tried again."""

STATUSES: Final[tuple[str, ...]] = get_args(Status)
FAILURE_KINDS: Final[tuple[str, ...]] = get_args(FailureKind)

LINE_MAX: Final = 200
"""A receipt's line is cut to this many characters."""
_ARG_CHARS: Final = 24
_OBJECT: Final = re.compile(r"^[a-z][a-z_]*:\S+$")


def _line(text: str) -> str:
    """One line of at most :data:`LINE_MAX` characters, without control characters or brackets.

    The history line wraps receipts in ``[Receipts: ...]``; a bracket inside would end it.
    """
    plain = re.sub(r"[\x00-\x1f\x7f]", " ", text).replace("[", "(").replace("]", ")")
    line = " ".join(plain.split())
    return line if len(line) <= LINE_MAX else line[: LINE_MAX - 1] + "…"


@dataclass(frozen=True)
class Covered:
    """What a read looked at: its sources, its window, what it showed and what it left out."""

    sources: tuple[str, ...]
    window: str
    shown: int
    more: int
    how_more: str = ""

    def to_json(self) -> dict[str, Any]:
        """The row form."""
        return {
            "sources": list(self.sources),
            "window": self.window,
            "shown": self.shown,
            "more": self.more,
            "how_more": self.how_more,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Covered:
        """Read the row form; ``ValueError`` when it is not one."""
        sources = _field(data, "sources", list)
        if not all(isinstance(one, str) for one in sources):
            msg = "covered.sources must be names"
            raise ValueError(msg)
        return cls(
            tuple(sources),
            _field(data, "window", str),
            _field(data, "shown", int),
            _field(data, "more", int),
            data.get("how_more", ""),
        )


@dataclass(frozen=True)
class Receipt:
    """One call's account of itself (ADR 0319).

    ``object`` is ``<kind>:<the short id she sees>`` for a change and ``undo`` the event id an
    undo reverses; ``covered`` is a read's account of what it looked at. The constructor keeps
    those on the side they belong to, and ``failure`` on a failed receipt only. ``here`` is the
    reading of where he is that the call used (ADR 0322); the dispatcher fills it, a tool never.
    """

    status: Status
    reads_only: bool
    line: str
    object: str | None = None
    undo: str | None = None
    covered: Covered | None = None
    failure: FailureKind | None = None
    derived: bool = False
    here: HereReading | None = None

    def __post_init__(self) -> None:
        """Refuse a receipt that contradicts itself; cut the line to one short line."""
        if self.status not in STATUSES:
            msg = f"status must be one of {STATUSES}, not {self.status!r}"
            raise ValueError(msg)
        if (self.status == "failed") != (self.failure is not None):
            msg = "a receipt is failed if and only if it names a failure kind"
            raise ValueError(msg)
        if self.failure is not None and self.failure not in FAILURE_KINDS:
            msg = f"failure must be one of {FAILURE_KINDS}, not {self.failure!r}"
            raise ValueError(msg)
        if self.covered is not None and not self.reads_only:
            msg = "only a read says what it covered"
            raise ValueError(msg)
        if self.reads_only and (self.object is not None or self.undo is not None):
            msg = "only a change has an object or an undo id"
            raise ValueError(msg)
        if self.object is not None and not _OBJECT.match(self.object):
            msg = f"object must be <kind>:<short id>, not {self.object!r}"
            raise ValueError(msg)
        object.__setattr__(self, "line", _line(self.line))

    @property
    def ok(self) -> bool:
        """Only ``done`` counts as success."""
        return self.status == "done"

    def to_json(self) -> dict[str, Any]:
        """The row form: the keys that have a value."""
        out: dict[str, Any] = {
            "status": self.status,
            "reads_only": self.reads_only,
            "line": self.line,
        }
        if self.object is not None:
            out["object"] = self.object
        if self.undo is not None:
            out["undo"] = self.undo
        if self.covered is not None:
            out["covered"] = self.covered.to_json()
        if self.failure is not None:
            out["failure"] = self.failure
        if self.derived:
            out["derived"] = True
        if self.here is not None:
            out["here"] = self.here.to_json()
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Receipt:
        """Read the row form; ``ValueError`` when it is not a receipt."""
        status = _field(data, "status", str)
        covered = data.get("covered")
        return cls(
            status,
            _field(data, "reads_only", bool),
            _field(data, "line", str),
            object=data.get("object"),
            undo=data.get("undo"),
            covered=None if covered is None else Covered.from_json(covered),
            failure=data.get("failure"),
            derived=data.get("derived") is True,
            here=None if data.get("here") is None else HereReading.from_json(data["here"]),
        )


def _field(data: Mapping[str, Any], key: str, kind: type) -> Any:  # noqa: ANN401 — the checked value.
    """``data[key]`` when it is a ``kind`` (a bool is not an int), else ``ValueError``."""
    if not isinstance(data, dict):
        msg = "a receipt and its covered are objects"
        raise ValueError(msg)  # noqa: TRY004
    value = data.get(key)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        msg = f"{key} must be a {kind.__name__}"
        raise ValueError(msg)
    return value


_NEEDS_HIM: Final = frozenset({
    "not_configured", "places_not_enabled", "vision_unconfigured", "device_not_connected",
    "screen_recording_permission_denied", "allens_step", "no_vehicle",
})
_IMPOSSIBLE: Final = frozenset({
    "unavailable", "no_store", "binary_file", "screen_image_too_large", "ssrf_guard_refused",
    "device_lacks_tool", "no_ride", "spend_cap", "closed", "full", "location_unavailable",
    "stream_inactive", "stream_finished",
})
_RETRY_OTHER_ARGS: Final = frozenset({
    "invalid_arguments", "not_found", "no_resolved_target", "file_not_found", "target_not_found",
    "no_such_stop", "stop_passed", "no_item", "empty_text", "empty_message", "empty_ids",
    "too_long",
    "invalid_time", "time_in_past", "too_far_ahead", "bad_cwd", "cwd_not_allowed", "secret_field",
    "no_plan",
})


def failure_kind(code: str) -> FailureKind:
    """The kind of failure a tool's error code stands for (ADR 0319).

    One table for every code that names no kind of its own; a code it does not know is
    ``dont_retry``, the one that does not invite the model to try again.
    """
    if code in _NEEDS_HIM:
        return "needs_him"
    if code in _IMPOSSIBLE:
        return "impossible"
    if code in _RETRY_OTHER_ARGS:
        return "retry_other_args"
    return "dont_retry"


def only_reads(tool: object, arguments: Mapping[str, Any]) -> bool:
    """Whether this call only reads: the tool's ``reads`` of its arguments, else ``read_only``.

    The one answer the answer hold, the turn's call list, the receipt and the repeat guard
    share (ADR 0319). A tool that declares nothing keeps its fixed flag.
    """
    reads = getattr(tool, "reads", None)
    if reads is not None:
        return bool(reads(arguments))
    if getattr(tool, "effect", None) is not None:
        return call_risk(tool, arguments) == "L0"
    return bool(getattr(tool, "read_only", False))


_LEVELS: Final = frozenset({"L0", "L1", "L2", "L3", "L4"})


def call_risk(tool: object, arguments: Mapping[str, Any]) -> RiskLevel:
    """The level of this call (ADR 0361): the tool's ``effect`` of its arguments, else its level.

    A tool that declares no ``effect`` has one level for every call. An ``effect`` that cannot
    take these arguments, or answers with no level, counts as the tool's own (highest) level.
    """
    effect = getattr(tool, "effect", None)
    if effect is not None:
        try:
            level = effect(arguments)
        except Exception:  # noqa: BLE001 — arguments the model made up must not lower it.
            level = None
        if level in _LEVELS:
            return cast("RiskLevel", level)
    return cast("RiskLevel", getattr(tool, "risk_level", "L3"))


_CARD_LEVELS: Final = frozenset({"L3", "L4"})
"""The confirmation floor (ADR 0011 I7): a call at these levels waits for his card."""


def call_asks(tool: object, arguments: Mapping[str, Any]) -> bool:
    """Whether this call needs a card (ADR 0361): its effect is at the floor, else the flag."""
    if getattr(tool, "effect", None) is None:
        return bool(getattr(tool, "requires_confirmation", False))
    return call_risk(tool, arguments) in _CARD_LEVELS


@dataclass(frozen=True)
class Completed:
    """What a tool's completion gave back (ADR 0361): the full arguments, or why it could not."""

    arguments: Mapping[str, Any] | None
    error: str = ""
    code: str = ""
    kind: FailureKind = "dont_retry"


def receipt_id(action_id: str) -> str:
    """The id she cites a receipt by (ADR 0320): ``r`` and 6 hex digits of the action id's sha256.

    Worked out when the receipt is shown, never stored in it.
    """
    return "r" + hashlib.sha256(action_id.encode()).hexdigest()[:6]


def said(receipt: Receipt, rid: str | None = None) -> str:
    """One receipt as the history line and the in-flight note carry it (ADR 0319).

    Its sentence, then in brackets its id (``rid``, when it has one), the object (a change) and
    the status; a read that left rows out adds how many and how to get them. A read says what it
    covered, never what it found.
    """
    parts = [*([rid] if rid else []), *([receipt.object] if receipt.object else []), receipt.status]
    inside = ", ".join(parts)
    if receipt.covered is not None and receipt.covered.more > 0:
        how = f", {receipt.covered.how_more}" if receipt.covered.how_more else ""
        inside += f"; {receipt.covered.more} more not shown{how}"
    return f"{receipt.line} ({inside})"


def _call(tool: str, arguments: Mapping[str, Any]) -> str:
    """``tool(key=value, ...)`` with short scalar values; anything longer or nested is ``…``."""
    parts = []
    for key, value in arguments.items():
        if isinstance(value, (str, int, float, bool)):
            text = " ".join(str(value).split())
            shown = text if len(text) <= _ARG_CHARS else text[: _ARG_CHARS - 1] + "…"
        else:
            shown = "…"
        parts.append(f"{key}={shown}")
    return f"{tool}({', '.join(parts)})"


def derive(
    tool: str,
    arguments: Mapping[str, Any] | None,
    *,
    reads: bool,
    code: str | None = None,
    failure: FailureKind | None = None,
) -> Receipt:
    """The receipt of a call whose tool wrote none.

    A read is a done read; any other call is done on an answer that is not an error and failed on
    one (``code`` names the error; ``failure`` its kind, else the table's). Never an object, an
    undo id or a ``covered``: only the tool knows those.
    """
    call = _call(tool, arguments or {})
    if code is not None:
        return Receipt(
            "failed", reads, f"{call} gave error {code}",
            failure=failure or failure_kind(code), derived=True,
        )
    return Receipt("done", reads, f"read {call}" if reads else f"ran {call}", derived=True)
