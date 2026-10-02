"""L3 mail reply flag (ADR 0123): does an unread letter need Allen's own reply.

The companion home asks Jev, TypeSafe's hosted decision model reached through
OpenRouter, one yes/no question per unread letter, over the sender's display
name and the subject only. The answer is a probability (``noul``); the home
marks a letter ``yes`` only when Jev is very sure it needs a reply and ``fyi``
only when very sure it does not, and leaves every other letter unmarked: a
wrong mark costs more than a missing one. Each letter is asked once per daemon
life; a late or failed answer just leaves the letter unmarked until a later poll.

Layer rules: stdlib + L3 siblings; no wiring.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, wait
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jarvis.decision.surrogate_route import Reply, SurrogateRoute

LOGGER = logging.getLogger(__name__)

_INSTRUCTIONS: Final[str] = (
    "The text is the sender's display name and the subject line of an email sent to Allen."
    " Does the sender expect Allen to reply personally: a question, a request, scheduling,"
    " or a person writing to him? Answer no for a notification, receipt, newsletter,"
    " automated message or FYI. The text may be in Chinese, English or both."
)
_CACHE_MAX: Final[int] = 500
_UNKNOWN_SENDER: Final[str] = "unknown"


class MailReply:
    """Jev's reply question over unread letters, with a per-letter answer cache."""

    def __init__(self, route: SurrogateRoute, yes_at: float, fyi_at: float) -> None:
        """``route`` is the transport; its ``timeout_ms`` bounds one poll's wait."""
        self._route = route
        self._yes_at = yes_at
        self._fyi_at = fyi_at
        self._asked: dict[str, float] = {}  # message id -> P(needs a reply)
        self.spent_usd = 0.0

    def marks(self, letters: Sequence[tuple[str, str, str]]) -> dict[str, str | None]:
        """``reply`` per message id from (id, sender name, subject); never raises.

        Letters not asked yet are asked concurrently and waited for up to ``timeout_ms``
        in all. A letter with no usable answer, now or from an earlier poll, is None, and
        is asked again at the next poll.
        """
        sent: dict[str, Future[Reply]] = {}
        for message_id, name, subject in letters:
            if message_id in self._asked:
                continue
            state = f"From: {name or _UNKNOWN_SENDER}\nSubject: {subject}"
            question = {"reply": {"type": "noul", "instructions": _INSTRUCTIONS}}
            future = self._route.post(state, question)
            if future is None:  # no key
                break
            sent[message_id] = future
        done, _late = wait(sent.values(), timeout=self._route.timeout_ms / 1000)
        for message_id, future in sent.items():
            if future in done:
                self._settle(message_id, future)
        return {one[0]: self._mark(self._asked.get(one[0])) for one in letters}

    def _mark(self, probability: float | None) -> str | None:
        if probability is None:
            return None
        if probability >= self._yes_at:
            return "yes"
        return "fyi" if probability <= self._fyi_at else None

    def _settle(self, message_id: str, done: Future[Reply]) -> None:
        """Cache a usable answer and count its cost; warn once per kind of failure."""
        probability, cost, error = _read(done)
        if probability is not None:
            self._asked[message_id] = probability
            self.spent_usd += cost
            LOGGER.info("mail reply: one letter asked, $%.6f (total $%.6f)", cost, self.spent_usd)
            while len(self._asked) > _CACHE_MAX:
                del self._asked[next(iter(self._asked))]
        if error is not None:
            self._route.warn_once(error, "mail reply", "letters stay unmarked")


def _read(done: Future[Reply]) -> tuple[float | None, float, str | None]:
    """(probability, cost in USD, error) out of one finished call."""
    try:
        reply = done.result()
    except Exception as exc:  # noqa: BLE001 — a worker's failure is just an unmarked letter.
        LOGGER.warning("mail reply: %s: %s", type(exc).__name__, exc)
        return None, 0.0, None
    if reply.error is not None:
        return None, 0.0, reply.error
    try:
        probability = reply.parsed["answers"]["reply"]["noul"]
        cost = (reply.parsed.get("usage") or {}).get("cost")
    except (KeyError, TypeError, AttributeError):
        return None, 0.0, "bad_json"
    if isinstance(probability, bool) or not isinstance(probability, int | float):
        return None, 0.0, "bad_json"
    paid = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else 0.0
    return float(probability), paid, None

