"""L3 deep lane (ADR 0251): the turn the voice lane hands a heavy task to.

A deep turn is a ``surface.user_intent`` on :data:`DEEP_CHANNEL` that the runtime writes when a
voice turn calls ``think_deeper`` or his sentence is a request in ``llm.deep.on_words``. It runs
through the same pump, ``drive_turn`` and ``decide()`` as every other turn; this module holds what
differs for it: the settings, the rules it reads, whether he is still talking with her when it
ends, and the Tier 0 hit that forces it.
"""

# ruff: noqa: RUF001, RUF002 - his words carry fullwidth punctuation
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.pre_route import SPOKEN_CHANNELS
from jarvis.decision.tier0 import Tier0Hit
from jarvis.state.event_log import DEEP_CHANNEL, PHONE_VOICE_CHANNEL

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from jarvis.shared import Event

TASK_MAX_CHARS: Final = 1500
"""The longest task ``think_deeper`` takes: it rides the deep turn's opening row and every
later turn's state block while the task runs."""
LOST_AFTER_S: Final = 30 * 60
"""A deep turn with no end row this long after it began is lost (a crash writes none)."""
DEFAULT_DEADLINE_S: Final = 20 * 60
"""How long a deep run may work before it is stopped with the timeout notice. It is checked before
each request, so the run ends by this plus one request's timeout: under :data:`LOST_AFTER_S`."""
DEFAULT_MAX_TOOL_ITERATIONS: Final = 60
DEFAULT_LIVE_WINDOW_S: Final = 120

# Where Mac voice is heard: a phone's voice is played by the phone itself, from its own turns.
_MAC_VOICE: Final = tuple(sorted(SPOKEN_CHANNELS - {PHONE_VOICE_CHANNEL}))

DEEP_RULES: Final = """\
# Deep task
This turn is not a new sentence from him. The voice lane handed you a task, written from his
words and what it knew, and it is the message below. He is not waiting on this turn: nothing is
said until you finish.
- Do as many steps as the task needs. Every tool you have is on your list; there is no tool_search.
  You are the deep lane, so do not call think_deeper.
- Every fact in your answer comes from a tool result this turn or from the conversation. Look
  again rather than guess, and say which part you could not find.
- Do not narrate between steps. Text you write ends the turn and is your answer.
- A tool that asks him first still shows him its card. A task that needs such a write stops at
  that card.
- Write the answer as <voice>...</voice><document>...</document>. In <voice>: at most three short
  sentences in his language; the first says what this answers, in the words he used for it, then
  the conclusion. In <document>: a first line naming what this answers, then the full result in
  order, with the evidence (names, dates, where each fact came from). The document is what stays
  in the conversation, so "the result from earlier" must be found in it.
- If you could not do the task, say so plainly in <voice> and put what you did find in <document>.\
"""


class DeepConfigError(ValueError):
    """``llm.deep`` names a missing preset or holds an invalid number."""


@dataclass(frozen=True)
class DeepSettings:
    """``llm.deep``: the preset, step cap, deadline and live window of a deep turn.

    ``request`` matches the imperative phrases that hand a sentence over without a model.
    """

    preset: str
    max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS
    live_window_s: int = DEFAULT_LIVE_WINDOW_S
    deadline_s: int = DEFAULT_DEADLINE_S
    request: re.Pattern[str] | None = None


def _positive(block: Mapping[str, Any], key: str, default: int) -> int:
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"llm.deep.{key} must be a positive whole number"
        raise DeepConfigError(msg)
    return value


# What may stand before an on-word and still leave the sentence a request addressed to her.
_ADDRESSED = r"(?:请|麻烦|你|给我|帮我|帮忙|please|can you|could you|would you)[,，\s]*"
# A second command in the sentence: the model decides, since the word covers only one of them.
_AND_THEN = re.compile(
    r"然后|接着|之后|再帮|再给|顺便|并且|而且|以及|还有|同时|and then|and also|after that|also",
    re.IGNORECASE,
)


def _deadline(block: Mapping[str, Any]) -> int:
    deadline = _positive(block, "deadline_s", DEFAULT_DEADLINE_S)
    if deadline >= LOST_AFTER_S:
        msg = f"llm.deep.deadline_s must be under {LOST_AFTER_S}: a longer run reads as lost"
        raise DeepConfigError(msg)
    return deadline


def _request_pattern(block: Mapping[str, Any]) -> re.Pattern[str] | None:
    words = block.get("on_words")
    if words is None:
        return None
    if not isinstance(words, str) or not words:
        msg = "llm.deep.on_words must be a non-empty regular expression"
        raise DeepConfigError(msg)
    try:
        return re.compile(rf"\W*(?:{_ADDRESSED})*(?:{words})", re.IGNORECASE)
    except re.error as exc:
        msg = f"llm.deep.on_words is not a usable pattern: {exc}"
        raise DeepConfigError(msg) from exc


def is_request(settings: DeepSettings, sentence: str) -> bool:
    """Whether ``sentence`` is, from its start, a phrase of ``llm.deep.on_words`` addressed to her.

    "仔细查一下这个月的花销" is; "我想一想" and "关灯，然后仔细查一下明天天气" are not.
    """
    request = settings.request
    return (
        request is not None and request.match(sentence) is not None
        and _AND_THEN.search(sentence) is None
    )


def load_deep_settings(llm: Mapping[str, Any]) -> DeepSettings | None:
    """Parse ``llm.deep``; absent means no deep lane, a broken block fails boot."""
    block = llm.get("deep")
    if block is None:
        return None
    presets = llm.get("presets") or {}
    preset = block.get("preset") if isinstance(block, dict) else None
    if not isinstance(preset, str) or preset not in presets:
        msg = f"llm.deep.preset {preset!r} is not one of llm.presets"
        raise DeepConfigError(msg)
    return DeepSettings(
        preset=preset,
        max_tool_iterations=_positive(block, "max_tool_iterations", DEFAULT_MAX_TOOL_ITERATIONS),
        live_window_s=_positive(block, "live_window_s", DEFAULT_LIVE_WINDOW_S),
        deadline_s=_deadline(block),
        request=_request_pattern(block),
    )


def deep_turn(trigger: Event) -> bool:
    """Whether ``trigger`` opens a deep task rather than words of his."""
    return trigger.type == "surface.user_intent" and trigger.payload.get("channel") == DEEP_CHANNEL


def deep_line() -> str:
    """How this turn reached her and how its answer goes out, for the state block."""
    return (
        "This turn: a deep task the voice lane handed over, not his words. Your answer is read "
        "aloud if he is still talking with her when it is ready, else shown to him as a card."
    )


def live_conversation(conn: sqlite3.Connection, *, now_ms: int, window_s: int) -> bool:
    """Whether he is still talking with her by voice at the Mac.

    True when his latest spoken words were heard within ``window_s`` and no dismissal came after
    them. Typed words and a phone's voice do not count: nothing here would speak to either.
    """
    marks = ", ".join("?" * len(_MAC_VOICE))
    row = conn.execute(
        f"""
        SELECT type FROM events
        WHERE ts_epoch_ms >= ? AND (
          (type IN ('utterance.received', 'surface.user_intent')
            AND json_extract(payload_json, '$.channel') IN ({marks}))
          OR (type = 'surface.conversation_words'
            AND json_extract(payload_json, '$.reason') = 'dismissed'))
        ORDER BY id DESC LIMIT 1
        """,  # noqa: S608 - only placeholders are interpolated
        (now_ms - window_s * 1000, *_MAC_VOICE),
    ).fetchone()
    return row is not None and row[0] != "surface.conversation_words"


def forced_hit(sentence: str) -> Tier0Hit:
    """The Tier 0 hit that hands ``sentence`` (his words, a request for the deep lane) over."""
    return Tier0Hit(
        pattern_id="deep_words",
        tool_name="think_deeper",
        tool_args={"task": sentence[:TASK_MAX_CHARS]},
        response_template="tier0.deep",
    )


__all__ = [
    "DEEP_RULES",
    "LOST_AFTER_S",
    "TASK_MAX_CHARS",
    "DeepConfigError",
    "DeepSettings",
    "deep_line",
    "deep_turn",
    "forced_hit",
    "is_request",
    "live_conversation",
    "load_deep_settings",
]
