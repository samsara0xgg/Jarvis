"""L3 deterministic lifecycle commentary (ADR-0008 D6, as ADR 0116 amends it).

D6 splits a response into two phases: a short ``commentary`` while real work
continues, and the ``final`` answer once the evidence is in.  This module is
the whole of that decision: one pure function from a single committed event
(plus the turn's own words and the dispatched tool's name) to the ephemeral
:class:`~jarvis.shared.realtime.PresentationIntent` spec §3.6.3 defines.

The function is deliberately total and side-effect free: no clock, no DB
read, no LLM, no timer, no randomness (the runtime hands in the choice).  The
two rows that speak are the dispatch of a slow tool (ADR 0320) and,
since ADR 0116, the row that carries Allen's words; the runtime owns the clocks
that let the second one speak (ADR 0121), and every suppression rule.  D6's ban on a phrase that
claims a result before ``action.result_observed`` stands for the runtime's own
lines, with one owner's exception named in ADR 0116 (「马上好」); "a deep model
is never called only to generate 我在查" is a property of the call graph.

Whether an intent is *delivered* — origin, confirmation, one per turn — is the
runtime observer's business.  This module only answers "what would be true to
say about this row".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from jarvis.shared.lang import variants
from jarvis.shared.realtime import PresentationIntent, PresentationIntentType
from jarvis.shared.text import is_english

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.shared import Event

COMMENTARY_ATTENTION_CHANNEL: Final = "voice_notify"
"""Attention channel every commentary render uses.

L3 owns the channel decision (spec §3.6.4); the runtime observer passes this
constant through to L5 rather than picking one itself.  It matches
:data:`jarvis.decision.pre_route.ROUTINE_ATTENTION_CHANNEL` because commentary
is routine by definition — short, interruptible, independently permitted.
"""

_D6_ROWS: Final[dict[str, tuple[PresentationIntentType, str]]] = {
    "action.dispatched": ("acknowledge", ""),
    "utterance.received": ("acknowledge", "commentary.wait"),
    "route.tool_predicted": ("acknowledge", "commentary.wait"),
}
"""The rows that speak -> the key of the phrases they permit, in Chinese and
in English, in the language table.

A dispatch speaks the tool's line from the step table (ADR 0320), and only for a tool the
runtime found slow; that line is passed in, so its key is empty. The utterance row is the
"nothing of the answer has started" check, run by the runtime once its clock says the turn is
taking a while, and again for the follow-ups ("still working") of a longer wait. The
predicted row (ADR 0140: Jev's tool group for the line) is the first line, said before any
tool is called. The result row's 「结果回来了」 was heard with nothing before it after a quick tool
(ADR 0045), and a tool's own end says nothing about when the answer comes, so no
other row speaks.

The key holds a small set because one fixed phrase repeated on every turn is
the "one moment while I process that" shape OpenAI's Realtime preamble
guidance names as the thing to avoid. The phrases claim only that the turn is
still working, except the owner's 「马上好」 / "Almost there.", which claims
progress nobody knows (ADR 0116).
"""

_GROUP_LINES: Final = (
    ("calendar_todo_read", "commentary.tool.calendar"),
    ("mail_read", "commentary.tool.mail"),
    ("web_search", "commentary.tool.web"),
    ("comms_write", "commentary.tool.generic"),
)
"""Jev's predicted tool group (ADR 0140) -> the phrase key it says before any tool is called."""


def commentary_intent_for(
    event: Event,
    *,
    user_text: str = "",
    tool_line: str | None = None,
    still: bool = False,
    pick: Callable[[tuple[str, ...]], str],
) -> PresentationIntent | None:
    """Return the D6 intent this event permits, or ``None``.

    ``None`` for every event type but ``action.dispatched`` and ``utterance.received``, for a
    dispatch with no ``tool_line`` (the runtime passes one only for a slow tool, ADR 0320), and
    for a dispatch row that carries no usable ``action_id``, since ``subject_ref``
    is that id and an intent about nothing cannot be coalesced or superseded.
    The utterance row's subject is the row itself.

    The phrase is English when ``user_text`` (what Allen said this turn) reads
    as English; ``pick`` chooses one of the language's phrases. ``still`` makes
    the utterance row say a follow-up of a wait that goes on, not the first line.
    """
    row = _D6_ROWS.get(event.type)
    if row is None:
        return None
    subject = event.event_uid
    intent_type, key = row
    hint = ""
    if event.type == "action.dispatched":
        action_id = event.payload.get("action_id")
        if not tool_line or not isinstance(action_id, str) or not action_id:
            return None
        subject, hint = action_id, tool_line
    elif event.type == "route.tool_predicted":
        predicted_key = next(
            (line for group, line in _GROUP_LINES if group == event.payload.get("group")), None,
        )
        if predicted_key is None:
            return None
        key = predicted_key
    elif still:
        key = "commentary.still"
    return PresentationIntent(
        intent_type=intent_type,
        surface_hint="speech",
        subject_ref=subject,
        content_hint=hint or pick(variants(key, "en" if is_english(user_text) else "zh")),
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
