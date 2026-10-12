"""Two guards on a model turn's own loop (ADR 0240): a call it repeats, an answer no call backs.

Pure functions over the turn's calls so the decision loops and the tool exam judge the same
way. ``repeated_call`` answers a call identical to an earlier one in the turn; ``unbacked_said``
reads a turn-ending answer that says something was done (or will be looked up) when no call
this turn did it.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class TurnCall(NamedTuple):
    """A call that ran this turn, or was refused: ``ok`` is false for an error or a refusal."""

    name: str
    read_only: bool
    ok: bool
    key: str = ""


def call_key(arguments: Mapping[str, Any]) -> str:
    """A call's arguments as one canonical string, for comparing two calls."""
    return json.dumps(arguments, sort_keys=True)


# The tool result a repeated call gets in place of running again.
REPEATED_CALL: Final[str] = json.dumps(
    {"note": "You already made this exact call this turn; its result is above. Use it."}
)
REPEATED_FAILED: Final[str] = json.dumps(
    {
        "note": "This exact call already failed this turn with the error above; change what "
        "the error names, or tell him what failed."
    }
)


def repeated_call(calls: Sequence[TurnCall], name: str, key: str) -> str | None:
    """The tool result for a call identical to an earlier one this turn, or None to run it.

    A call that changed something (a non-read-only call that succeeded) after the earlier
    one may have changed what the repeat returns, so the repeat runs then.
    """
    for index in range(len(calls) - 1, -1, -1):
        earlier = calls[index]
        if earlier.name != name or earlier.key != key:
            continue
        if any(later.ok and not later.read_only for later in calls[index + 1 :]):
            return None
        return REPEATED_CALL if earlier.ok else REPEATED_FAILED
    return None


# What the model is told when its answer says it did, or will do, what no call did.
UNBACKED_NOTE: Final[str] = (
    "[From the program, not the user's words] Your answer says you {said}, but no tool call "
    "did that this turn, so it has not happened. Do it now: call the tool, then answer from "
    "its result, without asking him first. Only if no tool can do it, or it was done in an "
    "earlier turn, say so plainly in one short sentence."
)

# An answer that says something was saved, remembered, set, sent, shown or cancelled. It reads
# what the model wrote (live replies 2026-10: 记下了, 我记住啦, 已记下, I've put the findings on
# screen); a question about it (吗, a question mark) is not a claim.
_DID: Final = re.compile(
    "记下(?:了|啦|来了)|已记|记住(?:了|啦)|我(?:会|一定会)?记住|记好了|记进"
    "|[记写存放加][在到进]备忘录|设好了|已设|(?:会|将会?)(?:准时|按时)?提醒|到时候?提醒"
    "|已经发[给出送]|已发[送出]|发出去了|放[到在]屏幕|在屏幕上了|取消好了|已经?取消"
    r"|\b(?:saved|noted|sent it|on screen now|reminder (?:is|has been) set)\b"
    r"|\bI(?:'ve| have|'ll| will) "
    r"(?:set|put|added|sent|saved|noted|cancel+ed|remind you|remember)\b",
    re.IGNORECASE,
)
# An answer that only announces a lookup.
_WILL: Final = re.compile(
    "我(?:先|再|去|来|这就|马上)*[查看找翻搜](?:一下|看|查|找)|让我(?:先)?[看查找]|稍等|等我(?:一下|查|看)"
    r"|\blet me (?:check|look|see|find|pull)\b"
    r"|\bI(?:'ll| will| need to| have to|'m going to) (?:check|look (?:it |that )?(?:up|into|at)"
    r"|find out|see if)\b"
    r"|\bchecking\b|\b(?:one moment|hold on|give me a (?:moment|second))\b",
    re.IGNORECASE,
)
_SENTENCE_END: Final = re.compile(r"(?<=[.!?。！？\n])")  # noqa: RUF001 - fullwidth sentence ends
_QUESTION: Final = re.compile(r"[?？]\s*$|吗")  # noqa: RUF001 - fullwidth question mark
# "Only announces": a longer answer says something besides the lookup.
_PROMISE_MAX_CHARS: Final[int] = 100
# The screen part lists what exists ("已设置两个提醒" after a read of his day); only the
# spoken part is what he is told was done (replay 2026-10-11: the written part bounced a
# correct answer about an interview and its reminders).
_DOCUMENT: Final = re.compile(r"<document>.*?(?:</document>|$)", re.DOTALL)
# "The times are on screen" is true when the answer has a written part: the screen shows it.
_SCREEN: Final = re.compile(r"屏幕|\bscreen\b", re.IGNORECASE)


def _spoken(text: str) -> tuple[str, bool]:
    """What he hears of an answer, and whether it also has a written part for the screen.

    What he hears is the JSON reply's ``spoken``, or the text minus its ``<document>``.
    """
    try:
        reply = json.loads(text)
    except ValueError:
        reply = None
    if isinstance(reply, dict) and isinstance(reply.get("spoken"), str):
        return str(reply["spoken"]).strip(), bool(str(reply.get("written") or "").strip())
    spoken = _DOCUMENT.sub("", text)
    return spoken.strip(), spoken != text


def unbacked_said(
    text: str, calls: Sequence[TurnCall],
) -> Literal["did it", "will check"] | None:
    """What a turn-ending answer claims that no call this turn did, or None.

    ``"did it"``: it says something was saved, set, sent or shown and no non-read-only call
    succeeded. ``"will check"``: it only announces a lookup and the turn made no call at all.
    """
    said, written = _spoken(text)
    asserted = [s for s in _SENTENCE_END.split(said) if s.strip() and not _QUESTION.search(s)]
    claims = [s for s in asserted if _DID.search(s) and not (written and _SCREEN.search(s))]
    if claims and not any(c.ok and not c.read_only for c in calls):
        return "did it"
    if not calls and len(said) <= _PROMISE_MAX_CHARS and any(_WILL.search(s) for s in asserted):
        return "will check"
    return None


def unbacked_claim(text: str, calls: Sequence[TurnCall]) -> str | None:
    """The note to send for an answer no call backs, or None."""
    said = unbacked_said(text, calls)
    return None if said is None else UNBACKED_NOTE.format(said=said)
