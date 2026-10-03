"""L3 turn-end asks (ADR 0125): does a coding agent's final message ask Allen for something.

When a coding-agent session finishes a turn, the end of its last message is sent to
Jev, TypeSafe's hosted decision model reached through OpenRouter, as one yes/no
question (``noul``). A probability at or above ``at`` makes the finish count as
"needs you", and so does one at or above ``question_at`` when the final paragraph
holds a question mark outside URLs and inline code, or a ``needs input:`` line
(2026-10-02, offline on 298 real endings: 88 -> 101 of 134 asks caught, 0 of 164
reports flagged); every other outcome (below the bar, timeout, HTTP error, no key) means
nothing: the finish is told as a plain finish, exactly as before. Each turn ending is
asked once, keyed by session id and a hash of the text.

The first message Allen sends in a session after a finish that was scored is its outcome
(ADR 0128): one ``outcome`` line in the Jev dataset, with how long after the finish and the
verdict that finish got, never his words; a finish nobody answers gets no line.

Layer rules: stdlib + L3 siblings; no wiring.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from concurrent.futures import Future, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

    from jarvis.decision.surrogate_route import Reply, SurrogateRoute

LOGGER = logging.getLogger(__name__)

TAIL_CHARS: Final[int] = 600
_INSTRUCTIONS: Final[str] = (
    "The text is the end of a coding assistant's message to its owner after finishing a turn"
    " of work. Does it ask the owner to decide, choose, approve, answer a question or provide"
    " something before it can go on? Answer no if it only reports what it did, even if it ends"
    " with a polite offer like 'let me know if you need anything'. Saying it is waiting for the"
    " owner's go-ahead, answer or choice before it starts or continues also counts as asking."
    " The text may be in Chinese, English or both."
)
_QUESTION: Final[dict[str, dict[str, str]]] = {
    "asks": {"type": "noul", "instructions": _INSTRUCTIONS},
}
_CACHE_MAX: Final[int] = 500
_ENDINGS_MAX: Final[int] = 500
# The final paragraph: after the last blank line, else the last _PARAGRAPH_CHARS.
_PARAGRAPH_CHARS: Final[int] = 300
# A question mark in a URL's query or in inline code asks nobody.
_NOT_PROSE: Final = re.compile(r"https?://\S+|`[^`]*`")
_NEEDS_INPUT: Final = re.compile(r"needs input:", re.IGNORECASE)


def _asks_text(tail: str) -> bool:
    """Whether the ending itself reads as a question to Allen (the lower bar applies)."""
    if _NEEDS_INPUT.search(tail):
        return True
    paragraph = tail.rsplit("\n\n", 1)[1] if "\n\n" in tail else tail[-_PARAGRAPH_CHARS:]
    prose = _NOT_PROSE.sub("", paragraph)
    return "?" in prose or "\uff1f" in prose


@dataclass(slots=True)
class _Ending:
    """A session's latest ending: when Jev was first asked, its verdict, and if it was answered."""

    key: tuple[str, str]
    at: float
    asks: bool | None = None
    answered: bool = False


class TurnEndAsks:
    """Jev's question over a turn ending, with one answer kept per turn ending."""

    def __init__(self, route: SurrogateRoute, at: float, question_at: float) -> None:
        """``route`` is the transport; its ``timeout_ms`` bounds :meth:`asks`."""
        self._route = route
        self._at = at
        self._question_at = question_at
        self._lock = threading.RLock()  # a call already done runs its callback under it
        self._calls: dict[tuple[str, str], Future[Reply]] = {}
        self._endings: dict[str, _Ending] = {}
        self.spent_usd = 0.0

    def asks(self, session_id: str, text: str) -> bool | None:
        """Block up to ``timeout_ms``: True at the bar, False below it, None with no answer."""
        call = self._call(session_id, text)
        if call is None:
            return None
        wait([call], timeout=self._route.timeout_ms / 1000)
        return self._verdict(call, text[-TAIL_CHARS:])

    def peek(self, session_id: str, text: str, send: bool = True) -> bool | None:  # noqa: FBT001, FBT002 — the board's callable.
        """Never blocks: the answer if in, else (with ``send``) start the call; None for now."""
        call = self._call(session_id, text, send=send)
        return None if call is None else self._verdict(call, text[-TAIL_CHARS:])

    def _call(self, session_id: str, text: str, *, send: bool = True) -> Future[Reply] | None:
        """The call for this turn ending, sent now if it is new; None when there is no key."""
        tail = text[-TAIL_CHARS:]
        key = (session_id, hashlib.sha256(tail.encode()).hexdigest())
        with self._lock:
            call = self._calls.get(key)
            if call is None:
                call = self._route.post(tail, _QUESTION, "turn_end", session_id) if send else None
                if call is None:  # no key: nothing was sent, nothing is kept
                    return None
                self._calls[key] = call
                self._endings.pop(session_id, None)  # re-inserted last: the oldest go first
                self._endings[session_id] = _Ending(key, time.time())
                while len(self._endings) > _ENDINGS_MAX:
                    del self._endings[next(iter(self._endings))]
                call.add_done_callback(lambda done: self._settle(key, tail, done))
                while len(self._calls) > _CACHE_MAX:
                    del self._calls[next(iter(self._calls))]
            return call

    def _verdict(self, call: Future[Reply], tail: str) -> bool | None:
        if not call.done():
            return None
        odds = _read(call)[0]
        return None if odds is None else self._decide(odds, tail)

    def _decide(self, odds: float, tail: str) -> bool:
        return odds >= self._at or (odds >= self._question_at and _asks_text(tail))

    def answered(self, session_id: str, said_at: Iterable[float]) -> None:
        """Allen's messages in a session (epoch seconds); the first after a scored finish is logged.

        One ``outcome`` line per finish, however often this is told; nothing for a session whose
        latest finish was never scored. Only the time and the verdict are written, no text.
        """
        with self._lock:
            ending = self._endings.get(session_id)
            if ending is None or ending.asks is None or ending.answered:
                return
            first = min((at for at in said_at if at > ending.at), default=None)
            if first is None:
                return
            ending.answered = True
        self._route.note(
            "outcome", "turn_end", session_id,
            outcome="answered", after_s=round(first - ending.at, 1), asks=ending.asks,
        )

    def _settle(self, key: tuple[str, str], tail: str, call: Future[Reply]) -> None:
        """On the worker, once per call: count its cost, log it (never the text), warn once."""
        session_id = key[0]
        odds, cost, error = _read(call)
        if odds is not None:
            self.spent_usd += cost
            asks = self._decide(odds, tail)
            with self._lock:
                ending = self._endings.get(session_id)
                if ending is not None and ending.key == key:
                    ending.asks = asks
            self._route.note("decision", "turn_end", session_id, asks=asks)
            LOGGER.info(
                "turn end asks: session %s p=%.3f asks=%s, $%.6f (total $%.6f)",
                session_id, odds, asks, cost, self.spent_usd,
            )
        if error is not None:
            self._route.warn_once(error, "turn end asks", "finishes stay plain finishes")


def _read(call: Future[Reply]) -> tuple[float | None, float, str | None]:
    """(probability, cost in USD, error) out of one finished call."""
    try:
        reply = call.result()
    except Exception as exc:  # noqa: BLE001 — a worker's failure is just no answer.
        LOGGER.warning("turn end asks: %s: %s", type(exc).__name__, exc)
        return None, 0.0, None
    if reply.error is not None:
        return None, 0.0, reply.error
    try:
        odds = reply.parsed["answers"]["asks"]["noul"]
        cost = (reply.parsed.get("usage") or {}).get("cost")
    except (KeyError, TypeError, AttributeError):
        return None, 0.0, "bad_json"
    if isinstance(odds, bool) or not isinstance(odds, int | float):
        return None, 0.0, "bad_json"
    paid = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else 0.0
    return float(odds), paid, None
