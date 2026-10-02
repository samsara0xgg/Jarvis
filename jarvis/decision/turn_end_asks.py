"""L3 turn-end asks (ADR 0125): does a coding agent's final message ask Allen for something.

When a coding-agent session finishes a turn, the end of its last message is sent to
Jev, TypeSafe's hosted decision model reached through OpenRouter, as one yes/no
question (``noul``). A probability at or above ``at`` makes the finish count as
"needs you"; every other outcome (below the bar, timeout, HTTP error, no key) means
nothing: the finish is told as a plain finish, exactly as before. Each turn ending is
asked once, keyed by session id and a hash of the text.

Layer rules: stdlib + L3 siblings; no wiring.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import Future, wait
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from jarvis.decision.surrogate_route import Reply, SurrogateRoute

LOGGER = logging.getLogger(__name__)

TAIL_CHARS: Final[int] = 600
_INSTRUCTIONS: Final[str] = (
    "The text is the end of a coding assistant's message to its owner after finishing a turn"
    " of work. Does it ask the owner to decide, choose, approve, answer a question or provide"
    " something before it can go on? Answer no if it only reports what it did, even if it ends"
    " with a polite offer like 'let me know if you need anything'. The text may be in Chinese,"
    " English or both."
)
_QUESTION: Final[dict[str, dict[str, str]]] = {
    "asks": {"type": "noul", "instructions": _INSTRUCTIONS},
}
_CACHE_MAX: Final[int] = 500


class TurnEndAsks:
    """Jev's question over a turn ending, with one answer kept per turn ending."""

    def __init__(self, route: SurrogateRoute, at: float) -> None:
        """``route`` is the transport; its ``timeout_ms`` bounds :meth:`asks`."""
        self._route = route
        self._at = at
        self._lock = threading.Lock()
        self._calls: dict[tuple[str, str], Future[Reply]] = {}
        self.spent_usd = 0.0

    def asks(self, session_id: str, text: str) -> bool | None:
        """Block up to ``timeout_ms``: True at the bar, False below it, None with no answer."""
        call = self._call(session_id, text)
        if call is None:
            return None
        wait([call], timeout=self._route.timeout_ms / 1000)
        return self._verdict(call)

    def peek(self, session_id: str, text: str, send: bool = True) -> bool | None:  # noqa: FBT001, FBT002 — the board's callable.
        """Never blocks: the answer if in, else (with ``send``) start the call; None for now."""
        call = self._call(session_id, text, send=send)
        return None if call is None else self._verdict(call)

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
                call.add_done_callback(lambda done: self._settle(session_id, done))
                while len(self._calls) > _CACHE_MAX:
                    del self._calls[next(iter(self._calls))]
            return call

    def _verdict(self, call: Future[Reply]) -> bool | None:
        if not call.done():
            return None
        odds = _read(call)[0]
        return None if odds is None else odds >= self._at

    def _settle(self, session_id: str, call: Future[Reply]) -> None:
        """On the worker, once per call: count its cost, log it (never the text), warn once."""
        odds, cost, error = _read(call)
        if odds is not None:
            self.spent_usd += cost
            self._route.note("decision", "turn_end", session_id, asks=odds >= self._at)
            LOGGER.info(
                "turn end asks: session %s p=%.3f asks=%s, $%.6f (total $%.6f)",
                session_id, odds, odds >= self._at, cost, self.spent_usd,
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
