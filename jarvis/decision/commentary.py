"""L3 deterministic lifecycle commentary (ADR-0008 D6, as ADR 0116 amends it).

D6 splits a response into two phases: a short ``commentary`` while real work
continues, and the ``final`` answer once the evidence is in.  This module is
the whole of that decision: one pure function from a single committed event
(plus the turn's own words and the dispatched tool's name) to the ephemeral
:class:`~jarvis.shared.realtime.PresentationIntent` spec §3.6.3 defines.

The function is deliberately total and side-effect free: no clock, no DB
read, no LLM, no timer, no randomness (the runtime hands in the choice).  The
two rows that speak are the dispatch of one of the two slowest tools and,
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

import re
from typing import TYPE_CHECKING, Final

from jarvis.shared.lang import CONFIRMED_TOOLS, LONG_WAIT_TOOLS, variants
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
    "action.dispatched": ("acknowledge", "commentary.wait"),
    "utterance.received": ("acknowledge", "commentary.wait"),
    "route.tool_predicted": ("acknowledge", "commentary.wait"),
}
"""The rows that speak -> the key of the phrases they permit, in Chinese and
in English, in the language table.

A dispatch speaks for every tool but the quiet ones (ADR 0136), and says
``commentary.long_wait`` for a tool on ``LONG_WAIT_TOOLS`` (ADR 0117), else the
line of what the tool does. The utterance row is the "nothing of the answer has
started" check, run by the runtime once its clock says the turn is taking a
while, and again for the follow-ups ("still working") of a longer wait. The
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
"""Jev's predicted tool group (ADR 0140) -> the phrase key it says before any tool is called,
the same lines a dispatch of that kind of tool says (ADR 0136)."""

_QUIET_TOOLS: Final = (
    "get_current_time",
    "ask_user",
    "start_night_run",
    "end_night_run",
    "set_voice",
    "weather",
    "transit",
    "pin_departure",
    "where_am_i",
    "create_memo",
    "set_reminder",
    "list_reminders",
    "cancel_reminder",
    "remember",
    "withdraw_card",
    "show_on_dashboard",
    "mcp__hue__",
)
"""Tools (a name, or a ``mcp__hue__`` prefix) whose answer follows at once or
that speak or ask for themselves; with every tool on ``CONFIRMED_TOOLS`` they
say nothing at dispatch (ADR 0136)."""

_TOOL_LINES: Final = (
    ("web_search", "commentary.tool.web"),
    ("web_fetch", "commentary.tool.web"),
    ("screen_look", "commentary.tool.screen"),
    ("search_records", "commentary.tool.records"),
    ("recall", "commentary.tool.records"),
    ("mcp__gmail__", "commentary.tool.mail"),
    ("mail_inbox", "commentary.tool.mail"),
    ("mcp__microsoft__", "commentary.tool.calendar"),
)
"""Tool name prefix -> the phrase key of what the tool does; any other tool says
``commentary.tool.generic``."""


def _dispatch_key(tool_name: str | None) -> str | None:
    """The phrase key a dispatch of ``tool_name`` says, ``None`` for a quiet tool."""
    if tool_name is None or tool_name in CONFIRMED_TOOLS or tool_name.startswith(_QUIET_TOOLS):
        return None
    if tool_name in LONG_WAIT_TOOLS:
        return "commentary.long_wait"
    return next(
        (key for prefix, key in _TOOL_LINES if tool_name.startswith(prefix)),
        "commentary.tool.generic",
    )


_LEAD_IN_MAX_CHARS: Final = 60
"""The longest line before a call that is spoken: one speech candidate, the
sentence assembler's bound. A longer line gives way to the fixed phrase."""

_RESULT_CLAIM: Final = (
    r"已经|已[查找发关开完做]|[查找]到|好了|完成|搞定|结果是"
    r"|\b(?:done|finished|completed|found|sent|already|here(?:'s| is| are))\b"
)
"""A line before a call says what is about to happen. One that states a result
would be "已经查到了" before ``action.result_observed``, which D6 forbids."""


def lead_in_speech_text(line: str | None) -> str | None:
    """The model's line before a call as speech, or ``None`` for the fixed phrase.

    docs/plans/speak-as-written-proposal.md: spoken in place of the fixed
    phrase, so the doing has started. ``None`` for no line,
    one longer than a speech candidate, one carrying a tag, or one that states
    a result.
    """
    text = (line or "").strip()
    if (
        not text
        or len(text) > _LEAD_IN_MAX_CHARS
        or "<" in text
        or re.search(_RESULT_CLAIM, text, re.IGNORECASE) is not None
    ):
        return None
    return f"<voice>{text}</voice>"


def commentary_intent_for(
    event: Event,
    *,
    user_text: str = "",
    tool_name: str | None = None,
    still: bool = False,
    pick: Callable[[tuple[str, ...]], str],
) -> PresentationIntent | None:
    """Return the D6 intent this event permits, or ``None``.

    ``None`` for every event type but ``action.dispatched`` of a tool that is not
    quiet (ADR 0136) and ``utterance.received``, and for
    a dispatch row that carries no usable ``action_id``, since ``subject_ref``
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
    if event.type == "action.dispatched":
        action_id = event.payload.get("action_id")
        dispatch_key = _dispatch_key(tool_name)
        if dispatch_key is None or not isinstance(action_id, str) or not action_id:
            return None
        subject = action_id
        key = dispatch_key
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
        content_hint=pick(variants(key, "en" if is_english(user_text) else "zh")),
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
    "lead_in_speech_text",
]
