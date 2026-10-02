"""L3 mail marks (ADR 0123, 0124): does an unread letter need Allen's own reply, is it junk.

The companion home asks Jev, TypeSafe's hosted decision model reached through
OpenRouter, two yes/no questions per unread letter in one request, over the
sender's display name and the subject only. Each answer is a probability
(``noul``); the home marks a letter ``yes`` only when Jev is very sure it needs
a reply and ``fyi`` only when very sure it does not, flags it junk only when
very sure it is junk Allen did not ask for, and leaves every other letter
unmarked: a wrong mark costs more than a missing one. Each letter is asked once
per daemon life; a late or failed answer just leaves the letter unmarked until a
later poll.

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
_JUNK_INSTRUCTIONS: Final[str] = (
    "The text is the sender's display name and the subject line of an email sent to Allen."
    " Is it junk Allen would not want in his inbox: marketing, promotions, cold sales"
    " outreach, spam, or a mass newsletter he did not ask for? Answer no for mail from a"
    " person writing to him, a receipt, a security alert, or a service he uses. When unsure,"
    " answer no. The text may be in Chinese, English or both."
)
_CACHE_MAX: Final[int] = 500
_UNKNOWN_SENDER: Final[str] = "unknown"


class MailReply:
    """Jev's reply question over unread letters, with a per-letter answer cache."""

    def __init__(
        self, route: SurrogateRoute, yes_at: float, fyi_at: float, junk_at: float,
    ) -> None:
        """``route`` is the transport; its ``timeout_ms`` bounds one poll's wait."""
        self._route = route
        self._yes_at = yes_at
        self._fyi_at = fyi_at
        self._junk_at = junk_at
        # message id -> (P(needs a reply), P(junk))
        self._asked: dict[str, tuple[float, float]] = {}
        self.spent_usd = 0.0

    def marks(self, letters: Sequence[tuple[str, str, str]]) -> dict[str, tuple[str | None, bool]]:
        """(``reply``, ``junk``) per message id from (id, sender name, subject); never raises.

        Letters not asked yet are asked concurrently and waited for up to ``timeout_ms``
        in all. A letter with no usable answer, now or from an earlier poll, is None, and
        is asked again at the next poll.
        """
        sent: dict[str, Future[Reply]] = {}
        for message_id, name, subject in letters:
            if message_id in self._asked:
                continue
            state = f"From: {name or _UNKNOWN_SENDER}\nSubject: {subject}"
            question = {
                "reply": {"type": "noul", "instructions": _INSTRUCTIONS},
                "junk": {"type": "noul", "instructions": _JUNK_INSTRUCTIONS},
            }
            future = self._route.post(state, question)
            if future is None:  # no key
                break
            sent[message_id] = future
        done, _late = wait(sent.values(), timeout=self._route.timeout_ms / 1000)
        for message_id, future in sent.items():
            if future in done:
                self._settle(message_id, future)
        return {one[0]: self._mark(self._asked.get(one[0])) for one in letters}

    def _mark(self, odds: tuple[float, float] | None) -> tuple[str | None, bool]:
        if odds is None:
            return None, False
        reply, junk = odds
        mark = "yes" if reply >= self._yes_at else "fyi" if reply <= self._fyi_at else None
        # A letter that may need a reply is never junk, whatever the second answer says.
        return mark, junk >= self._junk_at and mark != "yes"

    def _settle(self, message_id: str, done: Future[Reply]) -> None:
        """Cache a usable answer and count its cost; warn once per kind of failure."""
        odds, cost, error = _read(done)
        if odds is not None:
            self._asked[message_id] = odds
            self.spent_usd += cost
            mark, junk = self._mark(odds)
            LOGGER.info(
                "mail reply: one letter asked, id %s reply p=%.3f mark=%s junk p=%.3f junk=%s,"
                " $%.6f (total $%.6f)",
                message_id, odds[0], mark, odds[1], junk, cost, self.spent_usd,
            )
            while len(self._asked) > _CACHE_MAX:
                del self._asked[next(iter(self._asked))]
        if error is not None:
            self._route.warn_once(error, "mail reply", "letters stay unmarked")


def _read(done: Future[Reply]) -> tuple[tuple[float, float] | None, float, str | None]:
    """((reply, junk) probabilities, cost in USD, error) out of one finished call."""
    try:
        reply = done.result()
    except Exception as exc:  # noqa: BLE001 — a worker's failure is just an unmarked letter.
        LOGGER.warning("mail reply: %s: %s", type(exc).__name__, exc)
        return None, 0.0, None
    if reply.error is not None:
        return None, 0.0, reply.error
    try:
        answers = reply.parsed["answers"]
        odds = (answers["reply"]["noul"], answers["junk"]["noul"])
        cost = (reply.parsed.get("usage") or {}).get("cost")
    except (KeyError, TypeError, AttributeError):
        return None, 0.0, "bad_json"
    if any(isinstance(one, bool) or not isinstance(one, int | float) for one in odds):
        return None, 0.0, "bad_json"
    paid = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else 0.0
    return (float(odds[0]), float(odds[1])), paid, None

