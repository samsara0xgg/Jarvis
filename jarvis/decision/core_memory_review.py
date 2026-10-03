"""L3 core memory review: Jev reads each add and rewrite before it lands (ADR 0146).

After the model's change list passes the gates, every ``add`` and ``rewrite`` goes to Jev as
one choice question: does this belong in the user's long-term memory? Only ``keep`` at
``min_confidence`` or above lets that one change through; any other answer drops just it,
and the drop is written into the version's ``changes`` log. A call that gives no usable
answer (no key, timeout, HTTP error, unreadable body) raises :class:`ReviewFailed`: an
unreviewed item is never written, because a missing memory costs less than a wrong one.
``stale`` changes only remove, so they are not reviewed. Every call is in the Jev dataset
(ADR 0128), through the route.

Layer rules: stdlib + L2 state + L3 siblings; no wiring.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import core_memory

if TYPE_CHECKING:
    from collections.abc import Sequence
    from concurrent.futures import Future

    from jarvis.decision.surrogate_route import Reply, SurrogateRoute

LOGGER = logging.getLogger(__name__)

USE: Final[str] = "core_memory"
KEEP: Final[str] = "keep"
_REVIEWED: Final[frozenset[str]] = frozenset({"add", "rewrite"})
_QUESTION: Final[dict[str, Any]] = {
    "type": "choice",
    "instructions": (
        "The text is one item that a note-keeping assistant proposes to file in a user's"
        " long-term memory, with its section and the conversation lines it was drawn from"
        " (time | speaker | words; allen is the user, jarvis and jarvis_live are the"
        " assistant). Does this item belong in the user's long-term memory? The lines are"
        " machine transcription in Chinese, English or both, and may be garbled."
    ),
    "criteria": {
        KEEP: (
            "A clear, sensible fact or preference about the user that will still matter in weeks."
        ),
        "garbled": ("The words do not make clear sense, likely a speech-recognition error."),
        "one_off": (
            "A one-time request, a joke, an instruction for a single task or session, or"
            " something short-lived."
        ),
        "not_users": "The assistant's suggestion or someone else's words, not the user's own.",
    },
}
_ANSWER: Final[str] = "review"


class ReviewFailed(Exception):  # noqa: N818 - reads as the fact: the review failed
    """A review call gave no usable answer; the whole day is rejected."""


def _section_of(doc: core_memory.Doc, number: int) -> str:
    """The section of item ``number`` in ``numbered(doc)`` order."""
    for section in core_memory.SECTIONS:
        if number <= len(doc[section]):
            return section
        number -= len(doc[section])
    msg = "item number out of range"  # check_core_memory has gated it
    raise ValueError(msg)


def review_state(
    change: dict[str, Any],
    *,
    doc: core_memory.Doc,
    lines: Sequence[str],
) -> str:
    """The item, its section, then the cited records' lines as the model saw them."""
    section = change.get("section") or _section_of(doc, change["item"])
    return "\n".join(
        [f"Section: {section}", f"Item: {change['text']}", "Cited lines:", *lines],
    )


def read_verdict(reply: Reply) -> tuple[str, float, float]:
    """``(choice, confidence, cost in USD)`` out of one reply; raises on any failure."""
    if reply.error is not None:
        msg = f"Jev call failed ({reply.error})"
        raise ReviewFailed(msg)
    try:
        answer = reply.parsed["answers"][_ANSWER]
        choice, confidence = answer["choice"], answer["confidence"]
        cost = (reply.parsed.get("usage") or {}).get("cost")
    except (KeyError, TypeError, AttributeError):
        msg = "Jev answer unreadable"
        raise ReviewFailed(msg) from None
    if (
        choice not in _QUESTION["criteria"]
        or isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
    ):
        msg = "Jev answer unreadable"
        raise ReviewFailed(msg)
    paid = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else 0.0
    return str(choice), float(confidence), paid


def _verdicts(
    asked: Sequence[tuple[dict[str, Any], str]],
    route: SurrogateRoute,
    day: str,
) -> list[tuple[str, float, float]]:
    """One verdict per (change, state), asked concurrently; raises on the first failure."""
    sent: list[Future[Reply]] = []
    for number, (_change, state) in enumerate(asked, start=1):
        future = route.post(state, {_ANSWER: _QUESTION}, USE, f"{day}#{number}")
        if future is None:
            msg = "no Jev key"
            raise ReviewFailed(msg)
        sent.append(future)
    verdicts = []
    for future in sent:
        try:
            reply = future.result(route.timeout_ms / 1000)
        except TimeoutError:
            msg = "Jev call timed out"
            raise ReviewFailed(msg) from None
        verdicts.append(read_verdict(reply))
    return verdicts


def review_changes(  # noqa: PLR0913 - the day, its gated changes, what the model saw, the route and the bar
    changes: Sequence[dict[str, Any]],
    *,
    doc: core_memory.Doc,
    records: Sequence[tuple[str, str, str, str]],
    day: str,
    route: SurrogateRoute,
    min_confidence: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(kept, dropped)``: ``kept`` is the changes that may land, in order.

    ``dropped`` are log entries ``{"op": "dropped", "change", "choice", "confidence"}``.
    ``records`` are the day's ``(id, time, speaker, words)`` rows; raises :class:`ReviewFailed`.
    """
    lines = {record[0]: f"{record[1]} | {record[2]} | {record[3]}" for record in records}
    reviewed = [change for change in changes if change["op"] in _REVIEWED]
    asked = [
        (change, review_state(change, doc=doc, lines=[lines[rid] for rid in change["sources"]]))
        for change in reviewed
    ]
    verdicts = dict(zip(map(id, reviewed), _verdicts(asked, route, day), strict=True))
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for change in changes:
        if id(change) not in verdicts:
            kept.append(change)
            continue
        choice, confidence, _cost = verdicts[id(change)]
        if choice == KEEP and confidence >= min_confidence:
            kept.append(change)
        else:
            dropped.append(
                {"op": "dropped", "change": change, "choice": choice, "confidence": confidence},
            )
    spent = sum(cost for _choice, _confidence, cost in verdicts.values())
    LOGGER.info(
        "core_memory review: %s reviewed %d, kept %d, dropped %d, $%.6f",
        day,
        len(reviewed),
        len(reviewed) - len(dropped),
        len(dropped),
        spent,
    )
    return kept, dropped


__all__ = ["KEEP", "ReviewFailed", "read_verdict", "review_changes", "review_state"]
