"""L3 one-shot (ADR 0139, 0140): one Jev request per voice line, three questions.

The surface sends the line once, when the regexes call it a turn: *intent* (a control word for
her speech, one of Tier 0's instant functions, or none; ADR 0122 and 0130), *relation* to the
previous line (new, supplement, correction, unrelated) and *tool_group* (which kind of tool the
answer will need first). The same reply serves each reader at its own time:

- the surface's blocking check of a short line (:meth:`JevOneShot.ask`, the control words);
- the decision stage (:meth:`JevOneShot.take`, the instant functions);
- a supplement or correction at ``relation.at`` or above, acted on the moment the reply lands
  through :attr:`JevOneShot.relate` (the earlier line's unspoken answer is dropped);
- a confident group that has a line to say, written as a ``route.tool_predicted`` event once
  the turn exists, for the commentary watcher to speak (never two first lines: the watcher's
  per-turn count decides).

Every tool the model then proposes is noted next to the prediction (:meth:`note_tool`), so the
dataset can say later whether prefetching a read would pay. Every call is in the Jev dataset
(ADR 0128). Any failure leaves today's behaviour: the line is a turn and the model answers.

Layer rules: stdlib + L3 siblings; no wiring.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.surrogate_route import NONE
from jarvis.decision.voice_words import CONTROL_CRITERIA

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from concurrent.futures import Future

    from jarvis.decision.surrogate_route import Reply, SurrogateOption, SurrogateRoute

LOGGER = logging.getLogger(__name__)

USE: Final[str] = "oneshot"
CONTROL: Final[tuple[str, ...]] = tuple(CONTROL_CRITERIA)
RELATIONS: Final[tuple[str, ...]] = ("new", "supplement", "correction", "unrelated")
ACTING: Final[tuple[str, ...]] = ("supplement", "correction")
PREDICTED_EVENT: Final[str] = "route.tool_predicted"
_KEPT_LINES: Final[int] = 32
_KEPT_TOOLS: Final[int] = 64
_RECENT_CHARS: Final[int] = 200
_PREV_CHARS: Final[int] = 200
_ARGS_CHARS: Final[int] = 1000
_NOT_WORD: Final = re.compile(r"[\W_]+")

_PERSONA: Final[str] = (
    "The assistant is Jarvis, the user's voice assistant on his Mac; it lives in the notch and"
    " comes out only when called; dismiss means go back to the notch and stand by. "
)
_INTENT_INSTRUCTIONS: Final[str] = _PERSONA + (
    "The text is a short transcript. The last line starting with 'User:' is the one being"
    " judged; earlier lines are context. A user speaks to a voice assistant. The bracketed note"
    " before the judged line says whether the assistant was speaking when the line was heard and"
    " whether hands-free mode was on: the line may be a word said over the assistant's voice,"
    " something said while it was silent in hands-free mode, or a normal turn. The user may be"
    " talking to the assistant, to another person, or to a video. What does the judged line mean"
    " for the assistant right now: a control word for its speech (a listening sound, stop, wait,"
    " leave), one of its instant no-argument functions (they need no details), or none? Talk"
    " addressed to someone else, a real new question or request, an answer, or anything else is"
    " none. Choose none if the request needs details, a search, a conversation, an action on"
    " something specific, or names another time, day or thing than the function covers. The text"
    " is machine transcription in Chinese, English or both, and may be garbled."
)
_INTENT_NONE: Final[str] = (
    "Anything else: a new question or request to the assistant, including a request to go on or"
    " tell more (small talk, anything needing details, a search, an action on something"
    " specific), an answer, a correction, a confirmation, or talk that is not addressed to the"
    " assistant (to another person, about someone else, quoting someone, a video)."
)
_RELATION: Final[dict[str, Any]] = {
    "type": "choice",
    "instructions": (
        "The judged line is the last line starting with 'User:'. The line starting 'Previous"
        " user line' is what the user said just before, with its age in seconds. How does the"
        " judged line relate to that previous line? The text is machine transcription in Chinese,"
        " English or both, and may be garbled."
    ),
    "criteria": {
        "new": (
            "A new request or question that stands on its own, even on the same topic as the"
            " previous line."
        ),
        "supplement": (
            "Continues, completes or restates the previous line, or adds a detail to it (the"
            " previous line was cut off, or the user is finishing the thought). Both lines"
            " together are one request."
        ),
        "correction": (
            "Changes, fixes or cancels what the previous line asked (a different day, person,"
            " number or app; 'not X, I mean Y'; 'never mind')."
        ),
        "unrelated": (
            "Not a request to the assistant and not about the previous line: noise, a stray"
            " word, a video, talk to someone else, a listening sound."
        ),
    },
}
TOOL_GROUPS: Final[dict[str, str]] = {
    "calendar_todo_read": (
        "Look at the user's calendar, schedule, agenda, meetings, or to-do list: what is on"
        " today, tomorrow, next week, am I free, what tasks are due."
    ),
    "mail_read": (
        "Search or read the user's email: latest mail, a message from someone, anything"
        " important in the inbox, a receipt or order email."
    ),
    "web_search": (
        "Look something up on the web or get live facts: the weather in a city the user names,"
        " news, scores, prices, current events, a website, a recent release."
    ),
    # ADR 0188: the home weather is one quick tool read; no line is said for it.
    "home_weather": (
        "The weather here, with no city named: now, today, tonight or tomorrow, rain,"
        " temperature outside, whether to take an umbrella."
    ),
    "records_notes": (
        "Search what the user said or did in earlier conversations, saved records, notes,"
        " memos, files, or the clipboard: what did I say, remember when, find my note, read this"
        " file."
    ),
    "activity_work": (
        "The user's own computer activity and work: what they are working on now, what they did"
        " today or yesterday, time spent, project progress, the daily report or briefing."
    ),
    "screen": (
        "Look at the screen: what is on my screen, read this, what is this page, help me with"
        " what I am looking at."
    ),
    "device_actions": (
        "Do something on the computer or at home: open a file, app, link or plugin panel, turn"
        " lights on or off or change a lamp or room."
    ),
    "agents_night": (
        "Start, check, talk to, wait for or stop a background coding agent or a night run: do"
        " this task for me, run overnight, how is the agent doing."
    ),
    "memory_write": (
        "Save something: remember this, take a memo, note this down, save a briefing or"
        " knowledge."
    ),
    "comms_write": (
        "Send or draft an email, or create, change or delete a calendar event or to-do item."
    ),
    "plugin_other": (
        "Use another connected service such as Notion, Linear or GitHub, or ask which plugins or"
        " services are connected."
    ),
    NONE: (
        "Talking alone is enough: greeting, small talk, thanks, opinion or advice, joke,"
        " explanation from general knowledge, brainstorming, acknowledgement, a follow-up that"
        " needs only what was just said."
    ),
}
_TOOL_GROUP: Final[dict[str, Any]] = {
    "type": "choice",
    "instructions": (
        "The text is a short transcript. The last line starting with 'User:' is the one being"
        " judged; earlier lines are context. A user speaks to a voice assistant that can chat"
        " and also has tools. Which kind of tool will the assistant need first to answer the"
        " judged line? Pick the single best group; pick none if talking alone is enough"
        " (greeting, small talk, opinion, general knowledge, a follow-up on what was just"
        " said). The text is machine transcription in Chinese, English or both, and may be"
        " garbled or cut off."
    ),
    "criteria": TOOL_GROUPS,
}


def oneshot_state(  # noqa: PLR0913 - the lines of the transcript
    text: str, recent: str, *, over_her: bool, conversation: bool,
    prev: str | None, age_s: float,
) -> str:
    """Her latest words, the bracketed note, the previous line with its age, then the line."""
    lines: list[str] = []
    tail = " ".join(recent.split())[-_RECENT_CHARS:]
    if tail:
        lines.append(f"Assistant: {tail}")
    lines.append(
        f"[The assistant was {'speaking' if over_her else 'silent'} when this was heard;"
        f" hands-free mode {'on' if conversation else 'off'}]",
    )
    if prev is not None:
        lines.append(f"Previous user line ({age_s:.0f} s earlier): {' '.join(prev.split())}")
    lines.append(f"User: {' '.join(text.split())}")
    return "\n".join(lines)


def _pick(parsed: Any, key: str, allowed: Sequence[str]) -> tuple[str, float] | None:  # noqa: ANN401 - decoded JSON
    """One question's choice and confidence out of a reply body, or None when unreadable."""
    try:
        answer = parsed["answers"][key]
        choice, confidence = answer["choice"], answer["confidence"]
    except (KeyError, TypeError, AttributeError):
        return None
    if (
        choice not in allowed
        or isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
    ):
        return None
    return str(choice), float(confidence)


def _clip(args: object) -> str:
    return json.dumps(args, ensure_ascii=False, default=str)[:_ARGS_CHARS]


@dataclass(eq=False)
class Line:
    """One voice line's request in flight."""

    turn_id: str
    started: float
    future: Future[Reply]
    prev: str | None  # the earlier turn the relation question compared it with
    attached: bool = False  # the decision stage has taken it: the turn exists
    done: bool = False
    fired: bool = False


class JevOneShot:
    """Jev's one request per voice line and what each reader takes from its reply."""

    def __init__(  # noqa: PLR0913 - one keyword per switch the config block carries
        self,
        route: SurrogateRoute,
        instants: Sequence[SurrogateOption],
        *,
        words_at: float | None = None,
        words_timeout_ms: int = 800,
        max_chars: int = 24,
        relation_at: float | None = None,
        window_s: float = 30.0,
        tool_at: float | None = None,
        tool_groups: frozenset[str] = frozenset(),
        emit: Callable[[str, dict[str, Any], str], None] | None = None,
    ) -> None:
        """``route`` is the transport; ``None`` for a bar leaves that reader off.

        ``emit(type, payload, turn_id)`` writes an event on the turn (the runtime's).
        """
        self._route = route
        self._words_at = words_at
        self._words_timeout_ms = words_timeout_ms
        self._max_chars = max_chars
        self._relation_at = relation_at
        self._window_s = window_s
        self._tool_at = tool_at
        self._tool_groups = tool_groups
        self.emit = emit
        # Set where the speaker is wired: (earlier turn, this turn, relation) -> what came of it.
        self.relate: Callable[[str, str, str], str] | None = None
        self.instants = tuple(instants)
        self._questions_with = {
            "intent": {
                "type": "choice",
                "instructions": _INTENT_INSTRUCTIONS,
                "criteria": {
                    **CONTROL_CRITERIA,
                    **{o.id: o.description for o in self.instants},
                    NONE: _INTENT_NONE,
                },
            },
            "relation": _RELATION,
            "tool_group": _TOOL_GROUP,
        }
        self._lock = threading.Lock()
        self._lines: OrderedDict[str, Line] = OrderedDict()
        self._tools: OrderedDict[str, tuple[str, float] | None] = OrderedDict()
        self._prev: tuple[str, str, float] | None = None

    @property
    def words_enabled(self) -> bool:
        """Whether :meth:`ask` stands in for the control-word question (ADR 0130)."""
        return self._words_at is not None

    def begin(
        self, turn_id: str, text: str, recent: str, over_her: bool, conversation: bool,  # noqa: FBT001
    ) -> None:
        """Send the line's request now (a worker); the readers find it by ``turn_id``."""
        now = time.monotonic()
        with self._lock:
            before = self._prev
            self._prev = (turn_id, text, now)
        related = before if before is not None and now - before[2] <= self._window_s else None
        state = oneshot_state(
            text, recent, over_her=over_her, conversation=conversation,
            prev=related[1][:_PREV_CHARS] if related else None,
            age_s=now - related[2] if related else 0.0,
        )
        questions = {
            k: v for k, v in self._questions_with.items() if k != "relation" or related
        }
        future = self._route.post(state, questions, USE, turn_id)
        if future is None:
            return
        line = Line(turn_id, now, future, related[0] if related else None)
        with self._lock:
            self._lines[turn_id] = line
            self._tools[turn_id] = None
            while len(self._lines) > _KEPT_LINES:
                self._lines.popitem(last=False)
            while len(self._tools) > _KEPT_TOOLS:
                self._tools.popitem(last=False)
        future.add_done_callback(partial(self._done, line))

    def ask(
        self, turn_id: str, text: str, recent: str,  # noqa: ARG002
        over_her: bool, confirm: bool,  # noqa: ARG002, FBT001
    ) -> str | None:
        """The surface's blocking check: a confident control word, or None (a turn).

        Waits for what is left of ``words_timeout_ms`` since the request was sent. A line longer
        than ``max_chars`` is not judged, except with ``confirm`` (a dismissal the regex found
        only inside a sentence).
        """
        if self._words_at is None:
            return None
        if not confirm and len(_NOT_WORD.sub("", text)) > self._max_chars:
            return None
        with self._lock:
            line = self._lines.get(turn_id)
        if line is None:
            return None
        left = max(0.0, self._words_timeout_ms / 1000 - (time.monotonic() - line.started))
        try:
            reply = line.future.result(left)
        except FutureTimeout:
            self._route.warn_once("timeout", "voice words", "the line stays a turn")
            return None
        if reply.error is not None:
            self._route.warn_once(reply.error, "voice words", "the line stays a turn")
            return None
        picked = _pick(reply.parsed, "intent", CONTROL)
        return picked[0] if picked is not None and picked[1] >= self._words_at else None

    def take(self, turn_id: str) -> Line | None:
        """The decision stage claims the line's request (its turn exists now), or None."""
        with self._lock:
            line = self._lines.pop(turn_id, None)
            if line is not None:
                line.attached = True
        if line is not None:
            self._fire_tool(line)
        return line

    def note_tool(self, turn_id: str, tool: str, args: object) -> None:
        """A tool the model proposed, next to what Jev predicted for the line (the dataset)."""
        with self._lock:
            if turn_id not in self._tools:
                return
            predicted = self._tools[turn_id]
        group, confidence = predicted if predicted is not None else (None, None)
        self._route.note(
            "tool", USE, turn_id, tool=tool, args=_clip(args), group=group, confidence=confidence,
        )

    def _done(self, line: Line, future: Future[Reply]) -> None:
        """On the worker that finished the request: act on relation, remember the group."""
        try:
            reply = future.result()
            if reply.error is None:
                self._read(line, reply)
        except Exception:  # noqa: BLE001 - Jev never breaks a turn
            LOGGER.warning("jev oneshot: reading the reply failed", exc_info=True)
        with self._lock:
            line.done = True
        self._fire_tool(line)

    def _read(self, line: Line, reply: Reply) -> None:
        group = _pick(reply.parsed, "tool_group", tuple(TOOL_GROUPS))
        with self._lock:
            if line.turn_id in self._tools:
                self._tools[line.turn_id] = group
        relation = _pick(reply.parsed, "relation", RELATIONS)
        if (
            relation is not None and line.prev is not None and self.relate is not None
            and self._relation_at is not None
            and relation[0] in ACTING and relation[1] >= self._relation_at
        ):
            try:
                outcome = self.relate(line.prev, line.turn_id, relation[0])
            except Exception:  # noqa: BLE001 - an earlier answer left alone loses nothing
                LOGGER.warning("jev oneshot: relate failed", exc_info=True)
                outcome = "error"
            self._route.note(
                "relation", USE, line.turn_id,
                relation=relation[0], confidence=relation[1], target=line.prev, outcome=outcome,
            )

    def _fire_tool(self, line: Line) -> None:
        """Write ``route.tool_predicted`` once, when the reply is in and the turn exists."""
        with self._lock:
            if not (line.attached and line.done) or line.fired:
                return
            line.fired = True
            predicted = self._tools.get(line.turn_id)
        if (
            predicted is None or self.emit is None or self._tool_at is None
            or predicted[0] not in self._tool_groups or predicted[1] < self._tool_at
        ):
            return
        try:
            self.emit(
                PREDICTED_EVENT,
                {"turn_id": line.turn_id, "group": predicted[0], "confidence": predicted[1]},
                line.turn_id,
            )
        except Exception:  # noqa: BLE001 - a wait line is a courtesy
            LOGGER.warning("jev oneshot: tool_predicted not written", exc_info=True)
