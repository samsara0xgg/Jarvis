"""L3 mail marks (ADR 0124, 0141): is an unread letter junk, and how much would Allen want it.

The companion home asks Jev, TypeSafe's hosted decision model reached through
OpenRouter, one yes/no question per unread letter, over the sender's display
name and the subject only: is it junk Allen did not ask for. The answer is a
probability (``noul``); the home flags a letter junk only when Jev is very sure,
and leaves every other letter unflagged: a wrong mark costs more than a missing
one. Whether a letter waits on his reply is not Jev's guess; the home reads it
from the conversation (ADR 0364). Each letter is asked once per daemon life; a
late or failed answer just leaves the letter unflagged until a later poll.

With ``importance`` on (ADR 0141) the same request also rates how much Allen would want to see
the letter today and names its kind; the rating only orders the home, it is never a mark.

Layer rules: stdlib + L3 siblings; no wiring.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.surrogate_route import await_replies

if TYPE_CHECKING:
    from collections.abc import Sequence
    from concurrent.futures import Future

    from jarvis.decision.surrogate_route import Reply, SurrogateRoute

LOGGER = logging.getLogger(__name__)

_JUNK_INSTRUCTIONS: Final[str] = (
    "The text is the sender's display name and the subject line of an email sent to Allen."
    " Is it junk Allen would not want in his inbox: marketing, promotions, cold sales"
    " outreach, spam, or a mass newsletter he did not ask for? Answer no for mail from a"
    " person writing to him, a receipt, a security alert, or a service he uses. When unsure,"
    " answer no. The text may be in Chinese, English or both."
)
_CONTEXT: Final[str] = (
    "The text is the sender's display name and the subject line of an email sent to Allen, a"
    " university student in Victoria, BC who is job hunting for co-op and developer roles and"
    " building a personal AI assistant. "
)
_LANGUAGE: Final[str] = " The text may be in Chinese, English or both."
# Category is recorded only; the home shows importance.
_CATEGORY: Final[dict[str, str]] = {
    "job_search": "Job hunting: applications and acknowledgements, interview invitations or"
    " scheduling, recruiters, assessments, offers, job alerts, application portal accounts.",
    "school": "University and courses: professors, course announcements, registrar, fees,"
    " co-op office, grades, deadlines for school work.",
    "personal": "A real person he knows writing to him personally: family, friends, classmates,"
    " a personal chat or invitation.",
    "finance": "Money: bank, credit card, credit score or credit alerts, bills, statements,"
    " taxes, payment due or overdue notices.",
    "accounts_security": "Sign-in codes, verification codes, password resets, new-device or"
    " security alerts, account access notices.",
    "orders_services": "Receipts, order confirmations, shipping and delivery notices, rides,"
    " bookings, and notices about a service he uses that are not promotions.",
    "newsletters_promotions": "Newsletters, marketing, promotions, product announcements, sales"
    " and discount mail, subscription digests.",
    "other": "Anything else: housing, health, government and immigration, legal, or mail that"
    " fits none of the above.",
}
# Score weight per option; "low" weighs nothing.
_WEIGHT: Final[dict[str, int]] = {"urgent": 3, "important": 2, "normal": 1, "low": 0}
_IMPORTANCE: Final[dict[str, str]] = {
    "urgent": "Allen should act today: an interview invitation or scheduling request, a job"
    " offer, an assessment or payment deadline within days, an overdue bill, a security"
    " problem, a real person waiting on a time-bound answer.",
    "important": "Allen should read it soon, within a day or two: a real person writing to him,"
    " a recruiter update, a school notice that needs action, a security alert, a delivery"
    " waiting for pickup, an appointment reminder.",
    "normal": "Worth a glance when convenient: acknowledgements, statements, receipts, shipping"
    " updates, routine notices and codes.",
    "low": "He can skip it: promotions, marketing, newsletters, discount mail, bulk digests.",
}
_CACHE_MAX: Final[int] = 500
_UNKNOWN_SENDER: Final[str] = "unknown"


class MailReply:
    """Jev's junk question over unread letters, with a per-letter answer cache."""

    def __init__(self, route: SurrogateRoute, junk_at: float, *, importance: bool = False) -> None:
        """``route`` is the transport; its ``timeout_ms`` bounds one poll's wait."""
        self._route = route
        self._junk_at = junk_at
        self._importance = importance
        # message id -> P(junk)
        self._asked: dict[str, float] = {}
        # message id -> {"score": 0-3, "probabilities": {...}, "category": str}, when rated
        self._rated: dict[str, dict[str, Any]] = {}
        self.spent_usd = 0.0

    def marks(self, letters: Sequence[tuple[str, str, str]]) -> dict[str, bool]:
        """Whether each letter is junk, by id, from (id, sender name, subject); never raises.

        Letters not asked yet are asked concurrently and waited for up to ``timeout_ms``
        in all. A letter with no usable answer, now or from an earlier poll, is not junk, and
        is asked again at the next poll.
        """
        sent: dict[str, Future[Reply]] = {}
        for message_id, name, subject in letters:
            if message_id in self._asked:
                continue
            state = f"From: {name or _UNKNOWN_SENDER}\nSubject: {subject}"
            question: dict[str, Any] = {
                "junk": {"type": "noul", "instructions": _JUNK_INSTRUCTIONS},
            }
            if self._importance:
                question["importance"] = {
                    "type": "choice", "criteria": _IMPORTANCE,
                    "instructions": _CONTEXT + "How much would Allen want to see this letter"
                    " today?" + _LANGUAGE,
                }
                question["category"] = {
                    "type": "choice", "criteria": _CATEGORY,
                    "instructions": _CONTEXT + "Which kind of mail is it?" + _LANGUAGE,
                }
            future = self._route.post(state, question, "mail", message_id)
            if future is None:  # no key
                break
            sent[message_id] = future
        done = await_replies(
            self._route.clock, list(sent.values()), self._route.timeout_ms / 1000,
        )
        for message_id, future in sent.items():
            if future in done:
                self._settle(message_id, future)
        return {one[0]: self._junk(self._asked.get(one[0])) for one in letters}

    def rating(self, message_id: str) -> dict[str, Any] | None:
        """``score`` (0-3), ``probabilities`` and ``category`` of a rated letter, else None."""
        return self._rated.get(message_id)

    def outcome(self, message_ids: Sequence[str], what: str) -> None:
        """Allen archived or took back (``what``) letters: a signal on the junk mark (ADR 0128)."""
        for message_id in message_ids:
            self._route.note("outcome", "mail", message_id, outcome=what)

    def _junk(self, odds: float | None) -> bool:
        return odds is not None and odds >= self._junk_at

    def _settle(self, message_id: str, done: Future[Reply]) -> None:
        """Cache a usable answer and count its cost; warn once per kind of failure."""
        odds, cost, error = _read(done)
        if odds is not None:
            self._asked[message_id] = odds
            self.spent_usd += cost
            junk = self._junk(odds)
            rated = _rate(done)
            if rated is not None:
                self._rated[message_id] = rated
            self._route.note(
                "decision", "mail", message_id, junk=junk,
                **({"importance": rated} if rated is not None else {}),
            )
            LOGGER.info(
                "mail marks: one letter asked, id %s junk p=%.3f junk=%s, $%.6f (total $%.6f)",
                message_id, odds, junk, cost, self.spent_usd,
            )
            while len(self._asked) > _CACHE_MAX:
                self._rated.pop(oldest := next(iter(self._asked)), None)
                del self._asked[oldest]
        if error is not None:
            self._route.warn_once(error, "mail marks", "letters stay unflagged")


def _read(done: Future[Reply]) -> tuple[float | None, float, str | None]:
    """(P(junk), cost in USD, error) out of one finished call."""
    try:
        reply = done.result()
    except Exception as exc:  # noqa: BLE001 — a worker's failure is just an unflagged letter.
        LOGGER.warning("mail marks: %s: %s", type(exc).__name__, exc)
        return None, 0.0, None
    if reply.error is not None:
        return None, 0.0, reply.error
    try:
        junk = reply.parsed["answers"]["junk"]["noul"]
        cost = (reply.parsed.get("usage") or {}).get("cost")
    except (KeyError, TypeError, AttributeError):
        return None, 0.0, "bad_json"
    if isinstance(junk, bool) or not isinstance(junk, int | float):
        return None, 0.0, "bad_json"
    paid = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else 0.0
    return float(junk), paid, None



def _rate(done: Future[Reply]) -> dict[str, Any] | None:
    """Expected score 3*P(urgent)+2*P(important)+P(normal), its probabilities and the category.

    None when importance was not asked or the answer is unusable; the letter is then unrated.
    """
    try:
        answers = done.result().parsed["answers"]
        odds = answers["importance"]["probabilities"]
        category = answers["category"]["choice"]
        probabilities = {name: float(odds[name]) for name in _WEIGHT}
    except (KeyError, TypeError, AttributeError, ValueError):
        return None
    if category not in _CATEGORY:
        return None
    score = sum(_WEIGHT[name] * p for name, p in probabilities.items())
    return {"score": score, "probabilities": probabilities, "category": category}
