"""The guard on a model turn's own loop (ADR 0240): a call it repeats.

A pure function over the turn's calls so the decision loops and the tool exam judge the same
way. ``repeated_call`` answers a call identical to an earlier one in the turn. What an answer
claims is checked against receipts instead (ADR 0320, :mod:`jarvis.decision.claims`).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class TurnCall(NamedTuple):
    """A call that ran this turn, or was refused: ``ok`` is false for an error or a refusal."""

    name: str
    read_only: bool
    ok: bool
    key: str = ""


def call_key(arguments: Mapping[str, Any]) -> str:
    """A call's arguments as one canonical string, for comparing two calls."""
    return json.dumps(arguments, sort_keys=True)


# The tool result a repeated call gets in place of running again.
REPEATED_CALL: Final[str] = json.dumps(
    {"note": "You already made this exact call this turn; its result is above. Use it."}
)
REPEATED_FAILED: Final[str] = json.dumps(
    {
        "note": "This exact call already failed this turn with the error above; change what "
        "the error names, or tell him what failed."
    }
)


def repeated_call(calls: Sequence[TurnCall], name: str, key: str) -> str | None:
    """The tool result for a call identical to an earlier one this turn, or None to run it.

    A call that changed something (a non-read-only call that succeeded) after the earlier
    one may have changed what the repeat returns, so the repeat runs then.
    """
    for index in range(len(calls) - 1, -1, -1):
        earlier = calls[index]
        if earlier.name != name or earlier.key != key:
            continue
        if any(later.ok and not later.read_only for later in calls[index + 1 :]):
            return None
        return REPEATED_CALL if earlier.ok else REPEATED_FAILED
    return None


CALENDAR_LINE: Final = "Next on his calendar"
"""How the state block's calendar line begins (ADR 0249); its producer is in the runtime layer."""

RIDE_LINE: Final = "Bus ride: "
"""How the state block's bus-ride line begins; its producer is in the runtime layer."""
