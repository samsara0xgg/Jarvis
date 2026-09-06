"""L3 deterministic lifecycle commentary (ADR-0008 D6).

D6 splits a response into two phases: a short ``commentary`` while real work
continues, and the ``final`` answer once the evidence is in.  V1 commentary is
lifecycle-driven, and this module is the whole of that decision: one pure
function from a single committed action event to the ephemeral
:class:`~jarvis.shared.realtime.PresentationIntent` spec §3.6.3 defines.

The function is deliberately total and side-effect free — no clock, no DB
read, no LLM, no timer.  D6 forbids exactly the three things a stateful
version would tempt: "马上好" with no evidence, "已经查到了" before
``action.result_observed``, and timer-based fake progress when no lifecycle
row changed.  Deriving the phrase from the event alone makes all three
unreachable rather than merely discouraged, and "a deep model is never called
only to generate 我在查" is a property of the call graph.

Whether an intent is *delivered* — origin, confirmation, coalescing — is the
runtime observer's business.  This module only answers "what would be true to
say about this row".
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

from jarvis.shared.realtime import PresentationIntent, PresentationIntentType

if TYPE_CHECKING:
    from jarvis.shared import Event

COMMENTARY_ATTENTION_CHANNEL: Final = "voice_notify"
"""Attention channel every commentary render uses.

L3 owns the channel decision (spec §3.6.4); the runtime observer passes this
constant through to L5 rather than picking one itself.  It matches
:data:`jarvis.decision.pre_route.ROUTINE_ATTENTION_CHANNEL` because commentary
is routine by definition — short, interruptible, independently permitted.
"""

_D6_ROWS: Final[dict[str, tuple[PresentationIntentType, tuple[str, ...]]]] = {
    "action.dispatched": ("acknowledge", ("我去查一下。", "这就去办。", "我去看看。")),
    "action.running": ("progress", ("任务已经在运行。", "这件事正在做。", "还在跑着。")),
    "action.result_observed": (
        "progress",
        (
            "结果回来了，我整理一下。",  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.
            "拿到结果了，我看一下。",  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.
            "数据回来了，我过一遍。",  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.
        ),
    ),
    "action.failed": (
        "error",
        (
            "这一步失败了，我告诉你具体原因。",  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.
            "这一步没成，我说说原因。",  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.
            "这里出错了，我讲一下怎么回事。",  # noqa: RUF001 — fullwidth comma is intentional Chinese punctuation.
        ),
    ),
}
"""ADR-0008 D6's four action rows: observed truth -> the phrases it permits.

Each row carries a small set rather than one sentence because the per-turn cap
makes the acknowledge the phrase actually heard, and one fixed acknowledge
repeated on every turn is the "one moment while I process that" shape OpenAI's
Realtime preamble guidance names as the thing to avoid. Every member of a set
says the same observed truth; only the wording differs, so the choice cannot
make any of them less true.

The "utterance accepted, route selected" row of the same D6 table is not here;
it is not an action lifecycle event and is out of this slice's scope.
"""


def _phrase_for(action_id: str, phrases: tuple[str, ...]) -> str:
    """Pick this action's phrasing from its row's set, stably.

    ``hashlib.sha256`` and not builtin ``hash()``: the latter is randomised
    per process by ``PYTHONHASHSEED``, so the same action would say different
    things across daemon restarts and no test could pin the choice. A digest
    keeps the module pure — same action id, same sentence, on every machine
    and every process.
    """
    return phrases[hashlib.sha256(action_id.encode("utf-8")).digest()[0] % len(phrases)]


def commentary_intent_for(event: Event) -> PresentationIntent | None:
    """Return the D6 intent this action event permits, or ``None``.

    ``None`` for every event type outside the four-row table — including
    ``run.started``, ``gate.evaluated`` and ``action.cancelled`` — and for a
    mapped row that carries no usable ``action_id``, since ``subject_ref`` is
    that id and an intent about nothing cannot be coalesced or superseded.
    """
    row = _D6_ROWS.get(event.type)
    if row is None:
        return None
    action_id = event.payload.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        return None
    intent_type, phrases = row
    return PresentationIntent(
        intent_type=intent_type,
        surface_hint="speech",
        subject_ref=action_id,
        content_hint=_phrase_for(action_id, phrases),
        freshness_required=True,
    )


def commentary_speech_text(intent: PresentationIntent) -> str:
    """Render one intent as the channelized text L5 splits into surfaces.

    Commentary is speech and never a document: the run declares
    ``channel="speech"`` and its policy admits no other channel.  L5 does not
    read that declaration — it derives the delivered channel from the text
    itself (``parse_response_channels``), and an untagged phrase lands in both
    slots, so the run and its own surface rows would disagree.  That
    disagreement is not cosmetic: ``ConversationHistory`` folds a
    ``speech`` -> ``both`` change on one response into ``consistent=False``,
    and ``pre_route`` answers ``unknown`` for every later turn in the history
    window, silently switching routine streaming off.

    The ``<voice>`` tag is the same L3 -> L5 convention every ordinary answer
    already uses, so nothing downstream learns a new shape.
    """
    return f"<voice>{intent.content_hint}</voice>"


__all__ = [
    "COMMENTARY_ATTENTION_CHANNEL",
    "commentary_intent_for",
    "commentary_speech_text",
]
