"""The 合适吗 feedback of every proactive card that is not job mail (ADR 0160).

The client raises these cards itself (agent finishes, needs-you asks, what waited while quiet, the
night run); this keeps what it tells the daemon: a snapshot of each card when it is shown, and
Allen's reaction. Rows go to the same tables as job mail (``attention_log``, ``notice_feedback``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision import attention
from jarvis.shared import lang
from jarvis.state import job_ledger as ledger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

# The proactive card kinds the client reports; anything else is refused.
KINDS: Final[frozenset[str]] = frozenset({"pop", "wait", "req", "digest", "night", "morning"})
# right / a level / acted on the card (an answer, a session opened) / put away.
_REACTIONS: Final[frozenset[str]] = frozenset({"right", "acted", "dismissed"})
_LEVEL_PREFIX: Final[str] = "level:"
# A card's facts are short typed values (a title, a count, a tool name), never a message body.
_MAX_FACTS_CHARS: Final[int] = 1500
_MAX_TEXT: Final[int] = 200
_MAX_KEY: Final[int] = 40


def _plain(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _checked(raw: Mapping[str, Any], what: str) -> dict[str, Any]:
    """``raw`` when it is flat typed values and short, else a ValueError (a body is not welcome)."""
    for key, value in raw.items():
        items = value if isinstance(value, list) else [value]
        if len(key) > _MAX_KEY or not all(_plain(one) for one in items):
            msg = f"{what}.{key[:_MAX_KEY]} must be a plain value or a list of them"
            raise ValueError(msg)
        if any(isinstance(one, str) and len(one) > _MAX_TEXT for one in items):
            msg = f"{what}.{key} is longer than {_MAX_TEXT} characters"
            raise ValueError(msg)
    if len(json.dumps(raw, ensure_ascii=False)) > _MAX_FACTS_CHARS:
        msg = f"{what} is too long"
        raise ValueError(msg)
    return dict(raw)


def _known_reaction(reaction: str | None) -> bool:
    if reaction is None:
        return False
    if reaction.startswith(_LEVEL_PREFIX):
        return reaction[len(_LEVEL_PREFIX) :] in lang.JOB_LEVEL_NAMES
    return reaction in _REACTIONS


class CardFeedback:
    """``POST /inherent/cards/{id}``: ``seen`` stores a snapshot, feedback stores a reaction."""

    def __init__(self, db_path: Path, quiet: Callable[[], str]) -> None:
        """``db_path`` is memory.db; ``quiet`` reads the daemon's level when a post arrives."""
        self._db = db_path
        self._quiet = quiet
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def act(self, card_id: str, body: Mapping[str, Any]) -> None:
        """Apply one post; LookupError (404): no such card shown, ValueError (400): a bad post."""
        action, reaction = body["action"], body.get("reaction")
        now = self.now()
        if action == "seen":
            self._seen(card_id, body, now)
            return
        if action == "dismissed":
            reaction = "dismissed"
        if not _known_reaction(reaction):
            msg = f"not a reaction: {reaction!r}"
            raise ValueError(msg)
        if not ledger.add_card_feedback(self._db, card_id, str(reaction), now):
            msg = f"no such card: {card_id}"
            raise LookupError(msg)

    def _seen(self, card_id: str, body: Mapping[str, Any], now: datetime) -> None:
        kind, level = body.get("kind"), body.get("level")
        if kind not in KINDS or level not in attention.CARD_LEVELS:
            msg = f"a card needs a kind of {sorted(KINDS)} and a level of {attention.CARD_LEVELS}"
            raise ValueError(msg)
        facts = _checked(body.get("facts") or {}, "facts")
        local = now.astimezone()
        # The daemon's own clock and quiet level win over whatever the client says.
        situation = {
            **_checked(body.get("situation") or {}, "situation"),
            "hour": local.hour,
            "weekday": local.weekday(),
            "quiet": self._quiet(),
        }
        judgement = attention.card_judgement(kind, level)
        ledger.snapshot_card(
            self._db,
            kind=kind,
            card_id=card_id,
            pack_json=attention.card_pack(kind, card_id, facts, situation).to_json(),
            judge_id=judgement.judge_id,
            judge_version=judgement.judge_version,
            level=judgement.level,
            reason=judgement.reason,
            now=now,
        )
