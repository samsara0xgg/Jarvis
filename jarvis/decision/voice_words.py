"""L3 voice words (ADR 0130): what a short line heard over her voice, or in hands-free mode, means.

The surface's regexes decide what they know (a listening sound, a stop, a wait, a dismissal).
For a short line they call a turn, and for a dismissal they only found inside a sentence, one
choice question goes to Jev, TypeSafe's hosted decision model reached through OpenRouter: does
the line ask her to go on, stop, wait, leave hands-free mode, or none of these. The caller acts
only on an answer at or above ``at``; every other outcome (``none``, below the bar, timeout,
HTTP error, no key, bad JSON) means the line is a turn, as it was before. The call blocks for
up to the route's ``timeout_ms`` while she is held.

Every call is in the Jev dataset (ADR 0128); the lines the regexes absorbed on their own are
noted there too, so no word is lost to the next local model.

Layer rules: stdlib + L3 siblings; no wiring.
"""

from __future__ import annotations

import re
from concurrent.futures import TimeoutError as FutureTimeout
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from jarvis.decision.surrogate_route import SurrogateRoute

USE: Final[str] = "voice_words"
NONE: Final[str] = "none"
CHOICES: Final[tuple[str, ...]] = ("keep_going", "stop", "wait", "dismiss")
_INSTRUCTIONS: Final[str] = (
    "The text is a short transcript. The last line starting with 'User:' is the one being"
    " judged; earlier lines are context. A user speaks to a voice assistant. The judged line was"
    " heard while the assistant was speaking, or while it was silent in hands-free mode, as the"
    " bracketed note before it says. The user may be talking to the assistant, to another"
    " person, or to a video. What does the judged line mean for the assistant right now? Talk"
    " addressed to someone else, a real new question or request, an answer, or anything else is"
    " none. The text is machine transcription in Chinese, English or both, and may be garbled."
)
_CRITERIA: Final[dict[str, str]] = {
    "keep_going": (
        "A listening sound or acknowledgement said while the assistant talks (mm, yeah, okay,"
        " I see, right, haha). It is not meant to interrupt: the assistant should go on."
    ),
    "stop": (
        "Asks the assistant to stop talking right now (stop, enough, be quiet, shut up) and"
        " wants nothing else; it is not a question."
    ),
    "wait": (
        "Asks the assistant to pause and keep listening because the user will continue or needs"
        " a moment (hold on, wait for me, let me think, give me a minute)."
    ),
    "dismiss": (
        "Tells the assistant the exchange is over and it can leave hands-free mode (dismissed,"
        " that's all, no need anymore, you can go, bye)."
    ),
    NONE: (
        "Anything else: a new question or request to the assistant, an answer, a correction, a"
        " confirmation, or talk that is not addressed to the assistant (to another person, about"
        " someone else, quoting someone, a video)."
    ),
}
_QUESTION: Final[dict[str, Any]] = {
    "words": {"type": "choice", "instructions": _INSTRUCTIONS, "criteria": _CRITERIA},
}
_NOTES: Final[dict[bool, str]] = {
    True: "[The assistant was speaking when this was heard]",
    False: "[The assistant was silent; hands-free mode]",
}
_RECENT_CHARS: Final[int] = 200
_NOT_WORD: Final = re.compile(r"[\W_]+")


def _said(who: str, text: str) -> str:
    """One transcript line; ``who`` is the speaker tag, ``User`` until the audio can name one."""
    return f"{who}: {' '.join(text.split())}"


def words_state(text: str, recent: str, *, over_her: bool) -> str:
    """Her latest words as context, the bracketed note, then ``User: <text>``."""
    lines: list[str] = []
    tail = " ".join(recent.split())[-_RECENT_CHARS:]
    if tail:
        lines.append(_said("Assistant", tail))
    lines += [_NOTES[over_her], _said("User", text)]
    return "\n".join(lines)


class VoiceWords:
    """Jev's question over a short line the regexes did not settle."""

    def __init__(self, route: SurrogateRoute, at: float, max_chars: int) -> None:
        """``route`` is the transport (its ``timeout_ms`` bounds :meth:`ask`); ``at`` the bar."""
        self._route = route
        self._at = at
        self._max_chars = max_chars

    def ask(  # noqa: PLR0911 - one early return per way there is no usable answer; the session's callable
        self, turn_id: str, text: str, recent: str,
        over_her: bool, confirm: bool,  # noqa: FBT001
    ) -> str | None:
        """Block up to ``timeout_ms``: the confident choice, or None (a turn).

        A line longer than ``max_chars`` is never sent, except with ``confirm`` (a dismissal the
        regex found only inside a sentence, which Jev must confirm).
        """
        if not confirm and len(_NOT_WORD.sub("", text)) > self._max_chars:
            return None
        state = words_state(text, recent, over_her=over_her)
        call = self._route.post(state, _QUESTION, USE, turn_id)
        if call is None:
            return None
        try:
            reply = call.result(self._route.timeout_ms / 1000)
        except FutureTimeout:
            self._route.warn_once("timeout", "voice words", "the line stays a turn")
            return None
        if reply.error is not None:
            self._route.warn_once(reply.error, "voice words", "the line stays a turn")
            return None
        try:
            answer = reply.parsed["answers"]["words"]
            choice, confidence = answer["choice"], answer["confidence"]
        except (KeyError, TypeError, AttributeError):
            return None
        if (
            choice not in CHOICES
            or isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or confidence < self._at
        ):
            return None
        return str(choice)

    def note(
        self, turn_id: str, verdict: str, text: str,
        over_her: bool, conversation: bool,  # noqa: FBT001
    ) -> None:
        """A line the regexes settled alone, for the dataset; nothing is sent."""
        self._route.note(
            "regex", USE, turn_id,
            verdict=verdict, text=text, over_her=over_her, conversation=conversation,
        )
