"""L3 surrogate route (ADR 0122): Jev between the Tier 0 regex and the model.

When no Tier 0 row matched, one choice question goes to Jev, TypeSafe's hosted
decision model reached through OpenRouter: which of Tier 0's instant
no-argument functions does Allen want right now, or none. The caller runs the
function only when the answer is confident enough; every other outcome
(below the bar, ``none``, timeout, HTTP error, missing key, bad JSON) is a
fall-through to the model, and every call is recorded as a
``route.surrogate_decided`` event for a later local model to learn from.

Every call that ends, answered or failed, is also appended to the local Jev dataset
(ADR 0128) with what Jev saw, so a small model can be trained on it later.

Layer rules: stdlib + ``httpx`` + L2 state + L3 siblings; no wiring.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from jarvis.decision.stream_envelope import split_envelope
from jarvis.decision.tier0 import Tier0Hit
from jarvis.state.event_log import get_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path

    from jarvis.decision.packet import SituationPacket
    from jarvis.decision.tier0 import Tier0Table

LOGGER = logging.getLogger(__name__)

SURROGATE_URL: Final[str] = "https://openrouter.ai/api/alpha/decisions"
KEY_ENV: Final[str] = "OPENROUTER_API_KEY"
# Bump when an option, a description or the instructions change: the logged
# choices are only comparable within one version.
OPTIONS_VERSION: Final[str] = "2"
NONE: Final[str] = "none"
REPEAT: Final[str] = "repeat"
_CONTEXT_EXCHANGES: Final[int] = 2
_CONTEXT_WINDOW_MS: Final[int] = 600_000
_ANSWER_CHARS: Final[int] = 200
_KEEPALIVE_S: Final[float] = 30.0
_WORKERS: Final[int] = 4
_NO_ROUTE_STATUS: Final[int] = 404


class JevLog:
    """The Jev dataset (ADR 0128): one JSON line per call, decision or outcome, appended.

    Calls end on worker threads, so appends take one lock. The file is private to the owner
    (0600), made at the first line, never rotated. A write that fails is logged once and
    dropped: the dataset is never a reason for a decision to change.
    """

    def __init__(self, path: Path) -> None:
        """Append to ``path``; nothing is touched until the first line."""
        self._path = path
        self._lock = threading.Lock()
        self._warned = False

    def append(self, kind: str, use: str, ref: str | None, **fields: Any) -> None:  # noqa: ANN401 - the line's own fields
        """One line: when, kind, use, the ``ref`` of that use, then ``fields``; never raises."""
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        line = {"ts": stamp, "kind": kind, "use": use, "ref": ref, **fields}
        try:
            text = json.dumps(line, ensure_ascii=False, default=str) + "\n"
            with self._lock:
                self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8") as sink:
                    sink.write(text)
        except (OSError, TypeError, ValueError) as exc:
            if not self._warned:
                self._warned = True
                LOGGER.warning("jev log: cannot write %s: %s (logged once)", self._path, exc)


@dataclass(frozen=True)
class SurrogateOption:
    """One instant function Jev may choose: a Tier 0 row, or the repeat shortcut."""

    id: str
    pattern_id: str | None
    description: str


# The Tier 0 rows in config/tier0_patterns.yaml that take no argument from
# Allen's words, plus the repeat shortcut. note_capture and the open_* rows
# need free text, so they are not here.
OPTIONS: Final[tuple[SurrogateOption, ...]] = (
    SurrogateOption("time", "time_now", "Asks what time it is now."),
    SurrogateOption(
        "date", "date_today",
        "Asks today's date or which day of the week today is (not another day).",
    ),
    SurrogateOption(
        "clipboard", "read_clipboard", "Asks to read out or say what is on the clipboard.",
    ),
    SurrogateOption(
        "screen", "screen_look",
        "Asks the assistant to look at the screen or say what is on the user's screen.",
    ),
    SurrogateOption(
        "night_start", "night_start",
        "Says they are going to sleep and wants the computer to keep running overnight"
        " (start a night run).",
    ),
    SurrogateOption(
        "night_end", "night_end", "Says they are up or awake and wants the overnight run ended.",
    ),
    SurrogateOption(
        "list_notes", "note_list", "Asks to list or read back their saved notes or memos.",
    ),
    SurrogateOption(
        REPEAT, None,
        "Asks the assistant to say its very last answer again word for word, or says they did not"
        " hear or catch it. Not when they point to an earlier or particular answer (the story,"
        " what it said about X).",
    ),
)
_NONE_DESCRIPTION: Final[str] = "Anything else, including small talk and questions."
_INSTRUCTIONS: Final[str] = (
    "The text is a short transcript. The last line starting with 'User:' is the one being"
    " judged; earlier lines are context. A user speaks to a voice assistant. Which one of the"
    " assistant's instant no-argument functions is the user asking for right now, or none?"
    " Choose none if the request needs details, a search, a conversation, an action on something"
    " specific, or is anything else. The text is machine transcription in Chinese, English or"
    " both, and may be garbled."
)


def offered(table: Tier0Table | None) -> tuple[SurrogateOption, ...]:
    """The options this boot can run: repeat, and the rows that are in the Tier 0 table."""
    ids = {p.pattern_id for p in table or ()}
    return tuple(o for o in OPTIONS if o.pattern_id is None or o.pattern_id in ids)


def tier0_hit(option: SurrogateOption, table: Tier0Table | None) -> Tier0Hit | None:
    """The hit Tier 0 would have made for this option's row, so the same path runs it."""
    for row in table or ():
        if row.pattern_id == option.pattern_id:
            return Tier0Hit(
                pattern_id=row.pattern_id,
                tool_name=row.tool_name,
                tool_args=dict(row.arg_template),
                response_template=row.response_template,
                max_spoken_bytes=row.max_spoken_bytes,
            )
    return None


@dataclass(frozen=True)
class SurrogateAnswer:
    """What one call came to; ``error`` names why there is no usable choice."""

    choice: str | None
    confidence: float | None
    latency_ms: int
    cost_usd: float | None = None
    error: str | None = None


@dataclass(eq=False)
class SurrogateRoute:
    """Jev's settings and its HTTP client, built once at boot."""

    model: str
    min_confidence: float
    timeout_ms: int
    url: str = SURROGATE_URL
    # Spoken stream path only: the model's request is sent without waiting for Jev, and its
    # first output is held until Jev has answered (ADR 0122). Off, Jev is asked first.
    parallel: bool = False
    # OpenRouter's per-request ``provider.zdr``: route only to zero-data-retention endpoints.
    zdr: bool = True
    # The local dataset (ADR 0128); None = off.
    log: JevLog | None = None
    _client: httpx.Client = field(init=False, repr=False)
    _pool: ThreadPoolExecutor = field(init=False, repr=False)
    _warned: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        """Open the client and the worker pool; no network yet."""
        # httpx bounds each phase, not the whole call, so the call runs on a worker and
        # the turn stops waiting at the deadline; a stalled worker ends on its own phase
        # timeout, and its late answer is dropped.
        self._client = httpx.Client(
            timeout=self.timeout_ms / 1000, limits=httpx.Limits(keepalive_expiry=_KEEPALIVE_S),
        )
        self._pool = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="jarvis-jev")

    def accepts(self, answer: SurrogateAnswer) -> bool:
        """Whether the answer is a confident choice of a function, not ``none``."""
        return (
            answer.error is None
            and answer.choice not in (None, NONE)
            and answer.confidence is not None
            and answer.confidence >= self.min_confidence
        )

    def ask(self, state: str, options: Sequence[SurrogateOption]) -> SurrogateAnswer:
        """One choice question, waited for up to the deadline; never raises."""
        return self.start(state, options).result()

    def start(
        self, state: str, options: Sequence[SurrogateOption], ref: str | None = None,
    ) -> SurrogateCall:
        """Send the question now (``ref``: the turn id, for the dataset); read it later."""
        started = time.monotonic()
        question = {
            "type": "choice",
            "instructions": _INSTRUCTIONS,
            "criteria": {**{o.id: o.description for o in options}, NONE: _NONE_DESCRIPTION},
        }
        future = self.post(state, {"route": question}, "route", ref)
        return SurrogateCall(self, options, started, future)

    def post(
        self, state: str, questions: dict[str, Any], use: str, ref: str | None = None,
    ) -> Future[Reply] | None:
        """Send one decisions request on a worker; ``None`` when there is no key.

        ``use`` ("route", "mail" or "turn_end") and ``ref`` (the turn, letter or session the
        caller will name again) tag the call's line in the dataset.
        """
        key = os.environ.get(KEY_ENV, "").strip()
        if not key:
            return None
        body: dict[str, Any] = {"model": self.model, "state": state, "questions": questions}
        if self.zdr:
            body["provider"] = {"zdr": True}
        return self._pool.submit(self._fetch, key, body, use, ref)

    def note(self, kind: str, use: str, ref: str | None, **fields: Any) -> None:  # noqa: ANN401 - the line's own fields
        """Append a line to the dataset when it is on; never raises."""
        if self.log is not None:
            self.log.append(kind, use, ref, **fields)

    def _fetch(self, key: str, body: dict[str, Any], use: str, ref: str | None) -> Reply:
        """On a worker: one POST, then its line in the dataset; never an exception."""
        started = time.monotonic()
        reply = self._post(key, body)
        if self.log is not None:
            parsed = reply.parsed if isinstance(reply.parsed, dict) else {}
            answers = parsed.get("answers")
            usage = parsed.get("usage")
            cost = usage.get("cost") if isinstance(usage, dict) else None
            self.log.append(
                "call", use, ref, model=self.model, state=body["state"],
                questions=body["questions"], answers=answers,
                error=reply.error or (None if isinstance(answers, dict) else "bad_json"),
                latency_ms=int((reply.finished - started) * 1000), cost_usd=cost,
            )
        return reply

    def _post(self, key: str, body: dict[str, Any]) -> Reply:
        """One POST; the outcome and when it came, never an exception."""
        try:
            reply = self._client.post(
                self.url, json=body, headers={"Authorization": f"Bearer {key}"},
            )
            reply.raise_for_status()
            return Reply(reply.json(), None, time.monotonic())
        except httpx.TimeoutException:
            return Reply(None, "timeout", time.monotonic())
        except httpx.HTTPStatusError as exc:
            # OpenRouter answers 404 when no endpoint satisfies the provider preferences.
            # The call is never repeated without ``zdr``: no route means no call.
            no_route = self.zdr and exc.response.status_code == _NO_ROUTE_STATUS
            return Reply(None, "no_zdr_route" if no_route else "http", time.monotonic())
        except httpx.HTTPError:
            return Reply(None, "http", time.monotonic())
        except ValueError:
            return Reply(None, "bad_json", time.monotonic())

    def warn_once(
        self,
        error: str,
        what: str = "surrogate route",
        then: str = "turns fall through to the model",
    ) -> None:
        """Log a failure kind the first time only, so a dead endpoint is not a line per turn."""
        if error not in self._warned:
            self._warned.add(error)
            LOGGER.warning(
                "%s: %s; %s (logged once per kind)",
                what, "no zero-retention route" if error == "no_zdr_route" else error, then,
            )


@dataclass(frozen=True)
class Reply:
    """One decisions request's outcome: the decoded body or an error kind, and when it ended."""

    parsed: Any
    error: str | None
    finished: float


class SurrogateCall:
    """One question in flight; the deadline runs from when it was sent."""

    def __init__(  # noqa: PLR0913 - the call's own parts
        self, route: SurrogateRoute, options: Sequence[SurrogateOption], started: float,
        future: Future[Reply] | None, key: str = "route", others: frozenset[str] = frozenset(),
    ) -> None:
        """Wrap the worker's future; ``None`` means nothing was sent (no key).

        ``key`` names this question's answer in the reply (ADR 0139: the merged request
        carries it as ``intent``); ``others`` are its choices that are not options here (the
        control words), read as valid and never run.
        """
        self.route = route
        self._key = key
        self._others = others
        self._options = options
        self._started = started
        self._future = future
        self._answer: SurrogateAnswer | None = None

    def finished(self) -> bool:
        """Whether :meth:`result` would return without waiting."""
        return self._future is None or self._future.done()

    def result(self) -> SurrogateAnswer:
        """The answer, waiting only for what is left of the deadline; never raises."""
        if self._answer is None:
            self._answer = self._resolve()
            if self._answer.error is not None:
                self.route.warn_once(self._answer.error)
        return self._answer

    def _resolve(self) -> SurrogateAnswer:
        limit_ms = self.route.timeout_ms

        def failed(error: str, latency_ms: int) -> SurrogateAnswer:
            return SurrogateAnswer(None, None, latency_ms, None, error)

        if self._future is None:
            return failed("no_key", 0)
        left_s = max(0.0, limit_ms / 1000 - (time.monotonic() - self._started))
        try:
            reply = self._future.result(left_s)
        except FutureTimeout:
            return failed("timeout", limit_ms)
        latency_ms = int((reply.finished - self._started) * 1000)
        if reply.error is not None:
            return failed(reply.error, latency_ms)
        if latency_ms > limit_ms:
            return failed("timeout", latency_ms)  # the deadline is for the whole call
        return _read_answer(reply.parsed, self._options, latency_ms, self._key, self._others)


@dataclass(frozen=True)
class SurrogateAction:
    """What an accepted answer runs: the Tier 0 hit, or the last answer said again."""

    hit: Tier0Hit | None = None
    repeated: str | None = None


class PendingSurrogate:
    """A question sent for this turn and not yet acted on or dropped."""

    def __init__(
        self, call: SurrogateCall, options: Sequence[SurrogateOption], table: Tier0Table | None,
        repeatable: str | None, trigger_uid: str,
    ) -> None:
        """``repeatable`` is the last voice answer, what the repeat option would say."""
        self.trigger_uid = trigger_uid
        self.call = call
        self.settled = False
        self._options = options
        self._table = table
        self._repeatable = repeatable
        self._action: SurrogateAction | None = None
        self._resolved = False

    def action(self) -> SurrogateAction | None:
        """What to run, or None for the model; waits for what is left of the deadline."""
        if not self._resolved:
            self._resolved = True
            answer = self.call.result()
            chosen = next((o for o in self._options if o.id == answer.choice), None)
            if chosen is not None and self.call.route.accepts(answer):
                if chosen.id == REPEAT:
                    if self._repeatable is not None:
                        self._action = SurrogateAction(repeated=self._repeatable)
                else:
                    hit = tier0_hit(chosen, self._table)
                    if hit is not None:
                        self._action = SurrogateAction(hit=hit)
        return self._action

    def accepted_early(self) -> bool:
        """Without waiting: has it answered already with something that would run?"""
        return self.call.finished() and self.action() is not None


def _read_answer(
    parsed: Any,  # noqa: ANN401 — the decoded JSON body, any shape until checked
    options: Sequence[SurrogateOption],
    latency_ms: int,
    key: str = "route",
    others: frozenset[str] = frozenset(),
) -> SurrogateAnswer:
    """The choice and confidence out of Jev's body, or ``bad_json``."""
    bad = SurrogateAnswer(None, None, latency_ms, None, "bad_json")
    try:
        route = parsed["answers"][key]
        choice, confidence = route["choice"], route["confidence"]
        cost = (parsed.get("usage") or {}).get("cost")
    except (KeyError, TypeError, AttributeError):
        return bad
    if (
        choice not in {o.id for o in options} | {NONE} | others
        or isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
    ):
        return bad
    paid = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None
    return SurrogateAnswer(choice, float(confidence), latency_ms, paid, None)


def _line(text: str) -> str:
    return " ".join(text.split())


def conversation_state(packet: SituationPacket, conn: sqlite3.Connection, words: str) -> str:
    """The previous two exchanges within ten minutes, then ``User: <words>``.

    Her answer is the voice text of the last final response, cut to 200
    characters; a turn that got no answer contributes only his words.
    """
    history = packet.conversation_history
    turns = history.turns if history is not None else ()
    index = next((i for i, t in enumerate(turns) if t.turn_id == packet.current_turn_id), 0)
    now_ms = packet.trigger_event.ts_epoch_ms
    lines: list[str] = []
    for turn in turns[max(0, index - _CONTEXT_EXCHANGES):index]:
        said = get_event(conn, turn.input_event_uid)
        if said is None or now_ms - said.ts_epoch_ms > _CONTEXT_WINDOW_MS or not turn.user_text:
            continue
        lines.append(f"User: {_line(turn.user_text)}")
        answer = " ".join(
            _line(split_envelope(r.panel_available)[0])
            for r in turn.responses
            if r.phase == "final"
        ).strip()
        if answer:
            cut = answer if len(answer) <= _ANSWER_CHARS else answer[:_ANSWER_CHARS] + "..."
            lines.append(f"Assistant: {cut}")
    lines.append(f"User: {_line(words)}")
    return "\n".join(lines)
