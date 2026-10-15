"""Two guards on a model turn's own loop (ADR 0240): a call it repeats, an answer no call backs.

Pure functions over the turn's calls so the decision loops and the tool exam judge the same
way. ``repeated_call`` answers a call identical to an earlier one in the turn; ``unbacked_said``
reads a turn-ending answer that says something was done, will be looked up, or was looked up
(ADR 0250) when no call this turn did it.
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
# An answer that says it looked something up, or says what one of his stores holds, in a turn
# that made no call (replay 2026-10-12: 「我查了你的默认日历」, "Your calendar is
# clear tomorrow"). The lookup verbs are the act itself; 看 and 翻 count only with a store of his
# as the object, so 「看了一下你说的」 is plain talk. A store's contents are claimed only for his
# calendar: the state block holds mail and reminders, so those are not read this way.
_STORE_ZH: Final = "日历|日程|行程|邮件|邮箱|收件箱|提醒|备忘录|待办|记录|聊天记录|消息|笔记"
_STORE_EN: Final = "calendar|schedule|inbox|e-?mails?|mail|reminders?|notes|memos?|records|messages"
_CALENDAR_ZH: Final = "日历|日程|行程"
_WHEN_ZH: Final = "(?:[今明后]天|[这本下]周|周[一二三四五六日末]|[上下]午|晚上|目前|现在)*"
# The act of looking: the block is not a lookup, so these need a call whatever the block holds.
_ACT: Final = (
    "(?<!没)(?<!未)(?<!不)(?<!没有)(?:查看|查询|查|搜索|搜|检索|翻查)(?:了|过(?!去)|到)"
    f"|(?<!没)(?<!未)(?<!不)(?<!没有)[看翻读](?:了|过)(?:一下|一遍|下)?你?的?(?:默认)?(?:{_STORE_ZH})"
    r"|\bI(?:'ve| have)?(?: just| already)? (?:checked(?! out\b)|looked (?:it |that |this )?up"
    r"|looked into|searched|pulled up|gone through|went through)\b"
    rf"|\bI(?:'ve| have)?(?: just| already)? looked (?:at|through|over|in) (?:your|his) "
    rf"(?:{_STORE_EN})\b"
)
# What his calendar holds, said as fact: backed when the state block carries the calendar line.
_CONTENT: Final = (
    f"[根按]据你的(?:{_CALENDAR_ZH})"
    f"|你?的?(?:{_CALENDAR_ZH})[里上中]?{_WHEN_ZH}(?:是空的|空着|显示|写着|没有|没安排|有(?!点))"
    r"|\b(?:according to|based on) your (?:calendar|schedule)\b"
    r"|\byour (?:calendar|schedule) (?:is|are|looks?|shows?|says?|has|had|lists?)\b"
    r"|\b(?:nothing|no \w+) (?:on|in) your (?:calendar|schedule)\b"
)
_LOOKED_ACT: Final = re.compile(_ACT, re.IGNORECASE)
_LOOKED: Final = re.compile(f"{_ACT}|{_CONTENT}", re.IGNORECASE)
CALENDAR_LINE: Final = "Next on his calendar"
"""How the state block's calendar line begins (ADR 0249); its producer is in the runtime layer."""
_CALENDAR_LINE: Final = re.compile(rf"^{CALENDAR_LINE} \(", re.MULTILINE)


def calendar_in_view(state: str) -> bool:
    """Whether a turn's state block carries the calendar line, so it backs what it says."""
    return _CALENDAR_LINE.search(state) is not None


RIDE_LINE: Final = "Bus ride: "
"""How the state block's bus-ride line begins; its producer is in the runtime layer."""
_RIDE_STOP: Final = re.compile(rf"^{RIDE_LINE}.*\bHis stop: ", re.MULTILINE)


def ride_stop_in_view(state: str) -> bool:
    """Whether a turn's state block says where he gets off, so he is warned too (ADR 0263)."""
    return _RIDE_STOP.search(state) is not None


# An answer about his get-off stop, and a claim of the kind the ride line already shows true:
# the stop is set and he is warned one stop before it (ADR 0263).
_RIDE_TOPIC: Final = re.compile(
    r"下车|到站|前一站|那一站|\bget(?:ting)? off\b|\byour stop\b|\bone stop before\b",
    re.IGNORECASE,
)
_SET_CLAIM: Final = re.compile(r"设|提醒|\bremind|\bset\b|\bwarn", re.IGNORECASE)


# A sentence that cites the program's own state is not a claim of a lookup.
_CITES: Final = re.compile(
    r"\b(?:state|status) (?:line|block)\b|\bthe (?:program|block)\b"
    "|状态(?:行|块|栏)|程序(?:给|提供)的?",
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


def may_claim_done(text: str) -> bool:
    """Whether a piece of an answer could be a claim that something was done or looked up.

    A superset of what :func:`unbacked_said` sends back for those two cases (it ignores
    questions and the written part): the spoken stream holds such a piece until the guard
    has judged the answer (ADR 0263). An announced lookup is not held: it is heard before
    its call anyway.
    """
    return _DID.search(text) is not None or _LOOKED.search(text) is not None


def unbacked_said(
    text: str, calls: Sequence[TurnCall], *, calendar_seen: bool = False,
    ride_stop_seen: bool = False,
) -> Literal["did it", "will check", "looked it up"] | None:
    """What a turn-ending answer claims that no call this turn did, or None.

    ``"did it"``: it says something was saved, set, sent or shown and no non-read-only call
    succeeded. ``"will check"``: it only announces a lookup and the turn made no call at all.
    ``"looked it up"``: it says it checked, or says what his calendar holds, and the turn made
    no call at all; a sentence citing the program's state is not such a claim. With
    ``calendar_seen`` (the turn's state block carries the calendar line, see
    :func:`calendar_in_view`) what his calendar holds is backed, and only the act of looking
    (「我查了」, "I checked") still needs a call. With ``ride_stop_seen`` (see
    :func:`ride_stop_in_view`) an answer about his get-off stop may say the stop or the
    warning is set: the ride line shows it, so no call is needed.
    """
    said, written = _spoken(text)
    asserted = [s for s in _SENTENCE_END.split(said) if s.strip() and not _QUESTION.search(s)]
    claims = [s for s in asserted if _DID.search(s) and not (written and _SCREEN.search(s))]
    if ride_stop_seen and _RIDE_TOPIC.search(said):
        claims = [
            s for s in claims
            if not all(_SET_CLAIM.search(m.group()) for m in _DID.finditer(s))
        ]
    if claims and not any(c.ok and not c.read_only for c in calls):
        return "did it"
    if not calls and len(said) <= _PROMISE_MAX_CHARS and any(_WILL.search(s) for s in asserted):
        return "will check"
    looked = _LOOKED_ACT if calendar_seen else _LOOKED
    if not calls and any(looked.search(s) and not _CITES.search(s) for s in asserted):
        return "looked it up"
    return None


def unbacked_claim(
    text: str, calls: Sequence[TurnCall], *, calendar_seen: bool = False,
    ride_stop_seen: bool = False,
) -> str | None:
    """The note to send for an answer no call backs, or None."""
    said = unbacked_said(text, calls, calendar_seen=calendar_seen, ride_stop_seen=ride_stop_seen)
    return None if said is None else UNBACKED_NOTE.format(said=said)
