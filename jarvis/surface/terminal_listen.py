"""ADR 0172: a voice terminal listens, and the brain holds the turn.

A voice terminal runs the capture session (wake, VAD, endpointing, ASR, barge-in) as a daemon
does. What the session reaches in brain state crosses the ``/terminal/ws`` link of
:mod:`jarvis.surface.terminal_link` as one frame pair::

    terminal -> brain   {"type": "ask", "id": "<32 hex>", "op": "working", "args": {...}}
    brain -> terminal   {"type": "reply", "id": "<32 hex>", "ok": true, "value": ...}
    brain -> terminal   {"type": "reply", "id": "<32 hex>", "ok": false, "code": "...",
                         "message": "..."}
    terminal -> brain   {"type": "ask", "op": "hold", "args": {...}}     (no ``id``: no reply)

A frame with an ``id`` is answered and may run beside others; one without is fire and forget,
and those run one at a time in the order sent, each before any later answered frame starts.
The ``op`` names are :data:`ASKS` (answered) and :data:`TELLS` (not). Every one of them is the
brain's half of a callback of :class:`~jarvis.surface.voice_session.DuplexVoiceSession`:

======== ===================================== ==============================================
op       args                                  value
======== ===================================== ==============================================
utterance the final words, as the pipeline     ``{"verdict": "turn", "event_uid"}``: written
         writes them (transcript, turn_id,     once as ``utterance.received`` under the
         utterance_id, ...), and ``judge``     device's name, so a second send of it writes
         (``conversation``, ``over_her``)      nothing; or ``{"verdict": <other>}``: judged
                                               no turn on receipt (ADR 0277), nothing written
                                               (ADR 0278: first taking over from the device's
                                               pending earlier sentence, so the row holds the
                                               whole thought)
working  (none)                                ``{"working": bool}``: a turn is in flight
interrupt source                               ``{"outcome": str}``: generation cancel
cancel_runs (none)                             ``{}``
controls busy, hears, idle_s (each optional)  ``{"conversation": bool, "quiet": str,
                                               "floor": "mine" | "other" | "taken"}``
conversation on, reason                        ``{}``: set conversation mode
quiet    level                                 ``{}``
polish   raw, context, language, vocab         ``{"text": str}``: dictation's words polished
         (the words, where they will land,     by the brain's model, which this terminal has
         the recognizer's guess, the word      no key for (ADR 0183)
         list kept on the terminal)
hold     held                                  (tell) the run-hold half of hold-output
say      turn_id, reason, text                 (tell) one spoken line back
======== ===================================== ==============================================

An utterance that carries ``judge`` is judged by the brain as it arrives, in the one round trip
that delivers it (ADR 0277): a long utterance that is not in his voice first (``not_owner``,
ADR 0286: the optional ``voiceprint`` the utterance carries, scored against the profile of the
microphone that heard it), then same-words-from-another-device (ADR 0219), then the regexes
and Jev, then, for a turn, the unspoken answer it replaces is dropped (ADR 0074) before the row
is written; the row keeps the voiceprint's score, never its embedding. The terminal acts on the
verdict it gets back.

``controls`` is also the terminal's half of the voice floor (ADR 0407,
:mod:`jarvis.surface.voice_floor`): it says whether the terminal is in a conversation, whether
it can hear his wake word now and how many seconds ago he last used its keyboard or mouse, and
gets back where it stands. Conversation mode on the brain counts as the terminal being in a
conversation, and ``conversation`` with ``on`` says so at once.

``interrupt`` and ``cancel_runs`` act for the terminal that sent them, which the brain knows
from the link: they touch the turns that terminal opened and no others (ADR 0219), and the
frames carry no device.

The brain also commands the terminal, on the ordinary ``call`` frame, with two tool names that
are not menu tools: :data:`DROP_UNSPOKEN` and :data:`STOP_OUTPUT`, for supersede and
relate (ADR 0139), routed to the terminal that holds the playback.

This module holds both ends: :class:`BrainListening` (the brain) and :class:`LinkedTurn` and
:class:`LinkedControls` (the terminal).
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

from jarvis.shared import Event
from jarvis.shared.device_link import DeviceCallError
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state.quiet_mode import LEVELS as QUIET_LEVELS
from jarvis.state.voiceprint import Scored, Voiceprint, VoiceprintBook
from jarvis.surface import word_judge
from jarvis.surface.terminal_speaker import BrainCallError
from jarvis.surface.voice_floor import STANDINGS
from jarvis.surface.voice_pipeline import UtteranceJudgedError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from jarvis.surface.terminal_events import BrainEvents
    from jarvis.surface.terminal_link import Execute, TerminalHub, _Link
    from jarvis.surface.terminal_speaker import VoiceLink

LOGGER = logging.getLogger("jarvis.surface.terminal_listen")

DROP_UNSPOKEN: Final = "voice.drop_unspoken"
STOP_OUTPUT: Final = "voice.stop_output"
COMMAND_TIMEOUT_S: Final = 3.0
"""How long the brain waits for a terminal to drop or stop what it holds."""

ASKS: Final = frozenset(
    {
        "utterance", "working", "interrupt", "cancel_runs", "controls", "conversation", "quiet",
        "polish",
    },
)
_DEVICE_OPS: Final = frozenset({"interrupt", "cancel_runs", "controls", "conversation"})
"""The asks whose hook is told which terminal sent them (ADR 0219, 0407)."""
TELLS: Final = frozenset({"hold", "say"})

UTTERANCE_TIMEOUT_S: Final = 8.0
"""Covers the brain's judging of the words (Jev waits at most its own ``words_timeout_ms``)."""
WORKING_TIMEOUT_S: Final = 1.0
INTERRUPT_TIMEOUT_S: Final = 3.0
RUNS_TIMEOUT_S: Final = 5.0
POLISH_TIMEOUT_S: Final = 45.0
"""The polish's own client gives up at 30 s and tries once more."""
CONTROLS_TIMEOUT_S: Final = 2.0
CONTROLS_POLL_S: Final = 1.0
_MAX_TEXT_CHARS: Final = 20_000
_MAX_ROUTED: Final = 256
_CONTEXT_CHARS: Final = {"app": 200, "window": 500, "selected": 20_000, "before": 1000}
"""Where the words will land, with the limits of ``POST /inherent/dictation``'s body."""
_MAX_VOCAB_TERMS: Final = 2000
_MAX_VOCAB_TERM_CHARS: Final = 200
_ID_CHARS: Final = 64


class _Refused(Exception):  # noqa: N818 — a refusal, not a fault.
    """A frame the brain will not run; ``code`` goes back to the terminal."""

    def __init__(self, message: str, code: str = "bad_args") -> None:
        super().__init__(message)
        self.code = code


def _text(args: Mapping[str, Any], key: str, limit: int = _MAX_TEXT_CHARS) -> str:
    value = args.get(key)
    if not isinstance(value, str) or len(value) > limit:
        msg = f"{key} must be text of at most {limit} characters"
        raise _Refused(msg)
    return value


def _flag(args: Mapping[str, Any], key: str) -> bool:
    value = args.get(key)
    if not isinstance(value, bool):
        msg = f"{key} must be true or false"
        raise _Refused(msg)
    return value


# --- the brain's end ---------------------------------------------------------------------


@dataclass
class ListenHooks:
    """The brain's half of each capture-session callback; the runtime binds them.

    An unset hook is the neutral answer: no judge means a turn, nothing in flight, nothing
    said lately, no run to hold or cancel. The hooks that cancel or compare take the name of
    the terminal that sent the frame first (``link.name``), so they act on its turns alone.
    """

    ask_words: Callable[[str, str, str, bool, bool], str | None] | None = None
    note_words: Callable[[str, str, str, bool, bool], None] | None = None
    begin_line: Callable[[str, str, str, bool, bool], None] | None = None
    answer_words: Callable[[str, str, str], None] | None = None
    turn_working: Callable[[], bool] | None = None
    recent_speech: Callable[[], str] | None = None
    hold_runs: Callable[[bool], None] | None = None
    heard_elsewhere: Callable[[str, str, str], bool] | None = None
    """``(device, turn_id, text)``: another device recorded these words just now (ADR 0219)."""
    interrupt: Callable[[str, str], str] | None = None
    """``(device, source)``: cancel the answer being written for ``device``'s turns (ADR 0219)."""
    supersede: Callable[[str, str], None] | None = None
    """``(device, turn_id)``: drop ``device``'s earlier unspoken answers (ADR 0219)."""
    cancel_runs: Callable[[str], None] | None = None
    """``(device)``: end every answer being written for ``device``'s turns (ADR 0219)."""
    composes_into: Callable[[str, str, str], str | None] | None = None
    """``(device, turn_id, text)``: the earlier turn of ``device`` these words go on from, if
    its answer is not over (ADR 0278)."""
    take_over: Callable[[str, str], str] | None = None
    """``(earlier_turn_id, turn_id)``: cancel the earlier turn's open answer as superseded by
    ``turn_id``; ``"superseded"`` when it did (ADR 0271)."""
    voiceprints: VoiceprintBook | None = None
    """The owner's profiles, per terminal microphone: an utterance's voiceprint is scored first
    (ADR 0286)."""
    set_conversation: Callable[[bool, str], None] | None = None
    set_quiet: Callable[[str], None] | None = None
    controls: Callable[[], Mapping[str, Any]] | None = None
    polish: Callable[[str, Mapping[str, str], str, Sequence[str]], str] | None = None


@dataclass(eq=False)
class LinkState:
    """What :class:`BrainListening` keeps for one connected terminal."""

    tells: asyncio.Queue[tuple[str, dict[str, Any]]] = field(default_factory=asyncio.Queue)
    worker: asyncio.Task[None] | None = None
    tasks: set[asyncio.Task[None]] = field(default_factory=set)


class BrainListening:
    """The brain's end of a voice terminal's listening: answers its asks, commands it back.

    ``on_frame`` and ``detach`` run on the loop that serves the sockets; the hooks run on
    worker threads (they read the log and call models), except the utterance, which is
    written on the loop, where the event log's connection lives.
    """

    def __init__(self, hub: TerminalHub, events: BrainEvents) -> None:
        """Answer the terminals of ``hub``; ``events`` is the brain's log."""
        self._hub = hub
        self._events = events
        self.hooks = ListenHooks()
        self._last: _Link | None = None  # the terminal that last sent a turn
        self._ops: dict[str, Callable[[Mapping[str, Any]], Any]] = {
            "working": self._working, "quiet": self._quiet, "polish": self._polish,
            "hold": self._hold, "say": self._say,
        }
        self._device_ops: dict[str, Callable[[str, Mapping[str, Any]], Any]] = {
            "interrupt": self._interrupt, "cancel_runs": self._cancel_runs,
            "controls": self._controls, "conversation": self._conversation,
        }

    # -- frames ------------------------------------------------------------------------

    def on_frame(self, link: _Link, frame: Mapping[str, Any]) -> None:
        """One ``ask`` frame from the voice terminal ``link``."""
        op, rid, raw = frame.get("op"), frame.get("id"), frame.get("args")
        args: dict[str, Any] = raw if isinstance(raw, dict) else {}
        state = link.listen
        if state is None:
            state = link.listen = LinkState()
        self._last = link
        turn_id = args.get("turn_id")
        if isinstance(turn_id, str) and link.speech is not None and self._hub.voice is not None:
            self._hub.voice.route(turn_id, link.speech)
        if rid is None:
            if op in TELLS:
                state.tells.put_nowait((op, args))
                if state.worker is None:
                    state.worker = asyncio.create_task(self._tell_worker(state))
            return
        if not isinstance(rid, str) or len(rid) > _ID_CHARS:
            return
        task = asyncio.create_task(self._answer(link, state, rid, op, args))
        state.tasks.add(task)
        task.add_done_callback(state.tasks.discard)

    async def detach(self, link: _Link) -> None:
        """The terminal is gone: stop what is still running for it."""
        state, link.listen = link.listen, None
        if self._last is link:
            self._last = None
        if state is None:
            return
        self._hub.floor.left(link.name)
        running = [*state.tasks, *([state.worker] if state.worker is not None else [])]
        for task in running:
            task.cancel()
        if running:
            await asyncio.wait(running)

    async def _tell_worker(self, state: LinkState) -> None:
        """Run the fire-and-forget frames one at a time, in the order sent."""
        while True:
            op, args = await state.tells.get()
            try:
                await asyncio.to_thread(self._ops[op], args)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("terminal listening: %s failed", op)
            finally:
                state.tells.task_done()

    async def _answer(
        self, link: _Link, state: LinkState, rid: str, op: object, args: dict[str, Any],
    ) -> None:
        reply: dict[str, Any] = {"type": "reply", "id": rid, "ok": True}
        try:
            if op not in ASKS:
                msg = f"unknown ask {op!r}"
                raise _Refused(msg, "unknown_op")  # noqa: TRY301 — one place for every refusal.
            await state.tells.join()  # what the terminal told first has been done
            if op == "utterance":
                reply["value"] = await self._take_utterance(link, args)
            else:
                reply["value"] = await asyncio.to_thread(self._run, str(op), link.name, args)
        except asyncio.CancelledError:
            raise
        except _Refused as exc:
            reply |= {"ok": False, "code": exc.code, "message": str(exc)}
        except ValueError as exc:
            reply |= {"ok": False, "code": "bad_args", "message": str(exc)[:500]}
        except Exception as exc:  # whatever a hook raised is the answer.
            LOGGER.exception("terminal listening: %s failed", op)
            reply |= {"ok": False, "code": "brain_error", "message": type(exc).__name__}
        try:
            await link.send(json.dumps(reply, ensure_ascii=False))
        except Exception:  # noqa: BLE001 — the link may have dropped while the hook ran.
            LOGGER.debug("terminal listening: no one to answer %s", op)

    def _run(self, op: str, device: str, args: Mapping[str, Any]) -> Any:  # noqa: ANN401 — the op's JSON.
        """Run an answered op; the device is passed to those that act for one terminal."""
        if op in _DEVICE_OPS:
            return self._device_ops[op](device, args)
        return self._ops[op](args)

    # -- the asks ----------------------------------------------------------------------

    async def _take_utterance(self, link: _Link, args: Mapping[str, Any]) -> dict[str, Any]:
        """Judge the words if the terminal sent ``judge``, then write them if they are a turn."""
        judge = args.get("judge")
        if not isinstance(judge, dict):  # a terminal that judged the words itself
            return await self._utterance(link, args)
        scored = await asyncio.to_thread(self._score, link.name, args)
        verdict = await asyncio.to_thread(self._judge, link.name, args, judge, scored)
        if verdict != "turn":
            await asyncio.to_thread(self._learn, scored, verdict, args)
            return {"verdict": verdict}
        written = await self._utterance(link, args, scored)
        await asyncio.to_thread(self._learn, scored, verdict, args)
        return {"verdict": verdict, **written}

    def _learn(self, scored: Scored | None, verdict: str, args: Mapping[str, Any]) -> None:
        """Log the scored utterance with its verdict, and let a turn of his teach his profile."""
        book = self.hooks.voiceprints
        if scored is None or book is None:
            return
        try:
            book.finish(scored, verdict, _text(args, "turn_id", _ID_CHARS))
        except Exception:  # noqa: BLE001 - a profile cannot cost the owner his words
            LOGGER.warning("voiceprint: learning from %s failed", args.get("turn_id"),
                           exc_info=True)

    def _score(self, device: str, args: Mapping[str, Any]) -> Scored | None:
        """The utterance's voiceprint scored on this terminal's microphone, if it has one."""
        book = self.hooks.voiceprints
        voiceprint = Voiceprint.parse(args.get("voiceprint"))
        if book is None or voiceprint is None:
            return None
        try:
            return book.score(device, voiceprint)
        except Exception:  # noqa: BLE001 - a profile cannot cost the owner his words
            LOGGER.warning("voiceprint: scoring failed; the words are judged as they are",
                           exc_info=True)
            return None

    def _judge(
        self, device: str, args: Mapping[str, Any], judge: Mapping[str, Any],
        scored: Scored | None = None,
    ) -> str:
        """The word judge's verdict on an utterance, and for a turn the supersede that goes first.

        The hooks are those the terminal used to ask for one by one; a judge that fails leaves
        the words a turn, as it does on the Mac (ADR 0053, 0130, 0216, 0219). A stranger, by the
        utterance's ``scored`` voiceprint, is turned away before anything else (ADR 0286).
        """
        turn_id, text = _text(args, "turn_id", _ID_CHARS), _text(args, "transcript")
        hooks = self.hooks
        elsewhere = hooks.heard_elsewhere
        word_hooks = word_judge.WordHooks(
            ask=hooks.ask_words, note=hooks.note_words, begin=hooks.begin_line,
            recent=hooks.recent_speech, quiet=hooks.set_quiet is not None,
            heard_elsewhere=(
                None if elsewhere is None else lambda turn, words: elsewhere(device, turn, words)
            ),
        )
        conversation, over_her = _flag(judge, "conversation"), _flag(judge, "over_her")
        try:
            verdict = word_judge.words_verdict(
                word_hooks, turn_id, text, conversation=conversation, over_her=over_her,
                not_owner=scored is not None and scored.turn_away,
            )
        except Exception:  # noqa: BLE001 - the judge cannot lose the owner's words
            LOGGER.warning("judging %s failed; the words stay a turn", turn_id, exc_info=True)
            verdict = "turn"
        # A turn replaces the unspoken answer before it; a dismissal or a quiet phrase also ends
        # every answer still being written. The terminal sends nothing for either.
        ends_answers = verdict == "dismissed" or verdict.startswith("quiet:")
        if ends_answers and hooks.cancel_runs is not None:
            self._best_effort(hooks.cancel_runs, device)
        if (verdict == "turn" or ends_answers) and hooks.supersede is not None:
            self._best_effort(hooks.supersede, device, turn_id)
        return verdict

    @staticmethod
    def _best_effort(hook: Callable[..., None], *args: str) -> None:
        try:
            hook(*args)
        except Exception:  # an answer left alone never loses the owner's words
            LOGGER.exception("ending earlier answers failed")

    async def _utterance(
        self, link: _Link, args: Mapping[str, Any], scored: Scored | None = None,
    ) -> dict[str, Any]:
        """Write the utterance once, and make the terminal that heard it the answer's place.

        A sentence said before her answer to the one before it is over goes on from that one
        (ADR 0278): the earlier turn is taken over first, so the row that opens this turn holds
        the whole thought. Written on the loop, where the event log's connection lives.
        """
        turn_id = args.get("turn_id")
        if isinstance(turn_id, str) and link.speech is not None and self._hub.voice is not None:
            self._hub.voice.route(turn_id, link.speech)  # before the row, so no answer beats it
        self._events.utterance_payload(args)  # a malformed ask takes nothing over
        earlier = None
        if not self._events.has_utterance(link.name, args.get("utterance_id")):
            earlier = await self._take_over(link, args)
        return {
            "event_uid": self._events.record_utterance(
                link.name, args, continues=earlier,
                voiceprint=None if scored is None else scored.payload(),
            ),
        }

    async def _take_over(self, link: _Link, args: Mapping[str, Any]) -> str | None:
        """The earlier turn this utterance's words are composed into, once it is taken over.

        Its unspoken audio is dropped, what is audible stopped and its open answer cancelled as
        superseded by this turn, as a phone's ``say`` does (ADR 0271). ``None`` when the words
        stand alone; a failure is that too, since the words must not be lost.
        """
        pick, take = self.hooks.composes_into, self.hooks.take_over
        turn_id, text = args.get("turn_id"), args.get("transcript")
        if pick is None or take is None or not isinstance(turn_id, str) or not isinstance(
            text, str,
        ):
            return None
        try:
            earlier = await asyncio.to_thread(pick, link.name, turn_id, text)
            if earlier is None or self._events.continued_words(link.name, earlier, text) is None:
                return None
            await asyncio.to_thread(self.drop_unspoken, frozenset({earlier}))
            await asyncio.to_thread(self._stop_on, link, "barge_in")
            outcome = await asyncio.to_thread(take, earlier, turn_id)
        except Exception:
            LOGGER.exception("terminal %s: taking over from the earlier sentence failed", link.name)
            return None
        LOGGER.info("terminal %s: words go on from %s -> %s", link.name, earlier, outcome)
        return earlier if outcome == "superseded" else None

    def _working(self, _args: Mapping[str, Any]) -> dict[str, Any]:
        hook = self.hooks.turn_working
        return {"working": bool(hook()) if hook is not None else False}

    def _interrupt(self, device: str, args: Mapping[str, Any]) -> dict[str, Any]:
        hook = self.hooks.interrupt
        source = _text(args, "source", _ID_CHARS)
        return {"outcome": hook(device, source) if hook is not None else "no_open_run"}

    def _cancel_runs(self, device: str, _args: Mapping[str, Any]) -> dict[str, Any]:
        if self.hooks.cancel_runs is not None:
            self.hooks.cancel_runs(device)
        return {}

    def _controls(self, device: str, args: Mapping[str, Any]) -> dict[str, Any]:
        hook = self.hooks.controls
        state = hook() if hook is not None else {}
        conversation = state.get("conversation") is True
        hears, idle_s = args.get("hears"), args.get("idle_s")
        floor = self._hub.floor
        floor.report(
            device, busy=conversation or args.get("busy") is True,
            hears=hears if isinstance(hears, bool) else None,
            used_ago_s=float(idle_s) if isinstance(idle_s, int | float) else None,
        )
        return {"conversation": conversation, "quiet": state.get("quiet", "off"),
                "floor": floor.standing(device)}

    def _conversation(self, device: str, args: Mapping[str, Any]) -> dict[str, Any]:
        on = _flag(args, "on")
        if self.hooks.set_conversation is not None:
            self.hooks.set_conversation(on, _text(args, "reason", _ID_CHARS))
        self._hub.floor.report(device, busy=on)
        return {}

    def _quiet(self, args: Mapping[str, Any]) -> dict[str, Any]:
        level = _text(args, "level", _ID_CHARS)
        if level not in QUIET_LEVELS:
            msg = f"unknown quiet level {level!r}"
            raise _Refused(msg)
        if self.hooks.set_quiet is not None:
            self.hooks.set_quiet(level)
        return {}

    def _polish(self, args: Mapping[str, Any]) -> dict[str, Any]:
        hook = self.hooks.polish
        if hook is None:
            msg = "this brain does not polish dictation"
            raise _Refused(msg, "not_supported")
        raw_context = args.get("context")
        given = raw_context if isinstance(raw_context, dict) else {}
        context = {key: _text(given | {key: given.get(key, "")}, key, limit)
                   for key, limit in _CONTEXT_CHARS.items()}
        terms = args.get("vocab")
        if not isinstance(terms, list) or len(terms) > _MAX_VOCAB_TERMS or not all(
            isinstance(term, str) and len(term) <= _MAX_VOCAB_TERM_CHARS for term in terms
        ):
            msg = "vocab must be a list of short words"
            raise _Refused(msg)
        language = _text(args, "language", _ID_CHARS)
        return {"text": hook(_text(args, "raw"), context, language, terms)}

    # -- the tells ---------------------------------------------------------------------

    def _hold(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.hooks.hold_runs is not None:
            self.hooks.hold_runs(_flag(args, "held"))
        return {}

    def _say(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.hooks.answer_words is not None:
            self.hooks.answer_words(
                _text(args, "turn_id", _ID_CHARS), _text(args, "reason", _ID_CHARS),
                _text(args, "text"),
            )
        return {}

    # -- the brain's commands to a terminal ----------------------------------------------

    def _link_for_turn(self, turn_id: str) -> _Link | None:
        voice = self._hub.voice
        peer = voice.peer_for_turn(turn_id) if voice is not None else None
        return None if peer is None else self._hub.voice_link_of(peer)

    def drop_unspoken(self, turn_ids: frozenset[str]) -> frozenset[str]:
        """Drop, on the terminals that hold them, the answers of ``turn_ids`` not yet spoken.

        The ``drop_unspoken`` of
        :class:`~jarvis.surface.voice_media.pipeline.StreamingTTSPipeline`, over
        the link: each turn goes to the terminal that heard it (else the one connected last),
        and the turns that come back are those none of whose answers began playing. A terminal
        that does not answer drops nothing, so its turns' runs are left running. Call from a
        worker thread.
        """
        by_link: dict[_Link, set[str]] = {}
        for turn_id in turn_ids:
            link = self._link_for_turn(turn_id)
            if link is not None:
                by_link.setdefault(link, set()).add(turn_id)
        dropped: set[str] = set()
        for link, turns in by_link.items():
            try:
                reply = self._hub.call_on(
                    link, DROP_UNSPOKEN, {"turn_ids": sorted(turns)}, timeout_s=COMMAND_TIMEOUT_S,
                )
            except DeviceCallError as exc:
                LOGGER.warning("terminal %s did not drop unspoken answers: %s", link.name, exc)
                continue
            named = reply.get("dropped")
            if isinstance(named, list):
                dropped |= turns.intersection(item for item in named if isinstance(item, str))
        return frozenset(dropped)

    def stop_output(self, reason: str) -> str:
        """Stop what is audible now on the terminal that heard the owner last (ADR 0139).

        Answers the terminal's outcome (``applied``, ``stale``, ``uncertain``), or
        ``no_terminal`` / ``unreachable``. Call from a worker thread.
        """
        link = self._last
        if link is None or not self._hub.is_connected(link):
            voice = self._hub.voice
            peer = voice.peer_for_turn("") if voice is not None else None
            link = None if peer is None else self._hub.voice_link_of(peer)
        if link is None:
            return "no_terminal"
        return self._stop_on(link, reason)

    def _stop_on(self, link: _Link, reason: str) -> str:
        try:
            reply = self._hub.call_on(
                link, STOP_OUTPUT, {"reason": reason}, timeout_s=COMMAND_TIMEOUT_S,
            )
        except DeviceCallError as exc:
            LOGGER.warning("terminal %s did not stop its output: %s", link.name, exc)
            return "unreachable"
        outcome = reply.get("outcome")
        return outcome if isinstance(outcome, str) else "unknown"


# --- the terminal's end --------------------------------------------------------------------


class _Streaming(Protocol):
    """What the terminal's media actor answers the brain's commands with."""

    def drop_unspoken(self, turn_ids: frozenset[str]) -> frozenset[str]:
        """Drop the answers of ``turn_ids`` that never reached the speaker."""

    def stop_foreground_output(self, response_id: str | None, *, reason: str = ...) -> str:
        """Stop the speech audible right now."""


def with_voice_commands(execute: Execute, streaming: _Streaming) -> Execute:
    """``execute``, plus the brain's two commands to a voice terminal's media actor."""

    def run(
        tool: str, arguments: Mapping[str, Any], target_entity_ref: str | None,
    ) -> dict[str, Any]:
        if tool == DROP_UNSPOKEN:
            named = arguments.get("turn_ids")
            turns = frozenset(item for item in named if isinstance(item, str)) if isinstance(
                named, list,
            ) else frozenset()
            return {"ok": True, "output": {"dropped": sorted(streaming.drop_unspoken(turns))}}
        if tool == STOP_OUTPUT:
            reason = arguments.get("reason")
            outcome = streaming.stop_foreground_output(
                None, reason=reason if isinstance(reason, str) else "user_stop",
            )
            return {"ok": True, "output": {"outcome": outcome}}
        return execute(tool, arguments, target_entity_ref)

    return run


class LinkedTurn:
    """The capture session's brain-bound callbacks, answered over the link (ADR 0172).

    Each is the callback the session holds on one machine, with the fallback that callback has
    there when it cannot be answered: no read means nothing is in flight, and a stop that
    cannot be asked of the brain still stopped the speaker. Only the utterance has none: an
    utterance the brain never took is an error. The word judge is not among them: the brain
    judges the words as the utterance arrives (ADR 0277), so no call precedes it.
    """

    def __init__(self, link: VoiceLink) -> None:
        """Ask and tell the brain over ``link``."""
        self._link = link

    def answer_words(self, turn_id: str, reason: str, text: str) -> None:
        """Have the brain say one line back (it writes the answer rows; they come here)."""
        self._link.tell("say", {"turn_id": turn_id, "reason": reason, "text": text})

    def turn_working(self) -> bool:
        """Whether a turn is in flight on the brain; ``False`` when it cannot be asked."""
        try:
            value = self._link.ask("working", {}, timeout_s=WORKING_TIMEOUT_S)
        except BrainCallError:
            return False
        return isinstance(value, dict) and value.get("working") is True

    def hold_runs(self, held: bool) -> None:  # noqa: FBT001 - the capture side's one bit
        """Tell the brain to hold or release the completion of answers while he talks."""
        self._link.tell("hold", {"held": held})

    def interrupt(self, source: str) -> str:
        """Cancel the answer still being written (barge-in); the outcome, or why not."""
        try:
            value = self._link.ask("interrupt", {"source": source}, timeout_s=INTERRUPT_TIMEOUT_S)
        except BrainCallError as exc:
            LOGGER.warning("the answer being written could not be cancelled: %s", exc)
            return "brain_unreachable"
        outcome = value.get("outcome") if isinstance(value, dict) else None
        return outcome if isinstance(outcome, str) else "unknown"

    def cancel_runs(self) -> None:
        """Have the brain end every answer still being written for the speaker."""
        try:
            self._link.ask("cancel_runs", {}, timeout_s=RUNS_TIMEOUT_S)
        except BrainCallError as exc:
            LOGGER.warning("the answers being written could not be cancelled: %s", exc)

    def emit_utterance(
        self, payload: Mapping[str, object], correlation: Mapping[str, str],
    ) -> Event:
        """Send the utterance to the brain and wait until it is in the log.

        The brain judges the words as they arrive (``payload["judge"]``, ADR 0277): the reply is
        the event of a turn, or the verdict on words that are none. A brain that predates the
        judge writes every utterance as a turn and names no verdict.

        Raises:
            UtteranceJudgedError: the brain judged the words no turn; nothing was written.
            BrainCallError: the brain did not take it; nothing was written, and the owner is
                not answered.
        """
        try:
            value = self._link.ask("utterance", payload, timeout_s=UTTERANCE_TIMEOUT_S)
        except BrainCallError as exc:
            LOGGER.warning("the brain is not reachable; this utterance was not delivered (%s)", exc)
            raise
        verdict = value.get("verdict") if isinstance(value, dict) else None
        if isinstance(verdict, str) and verdict != "turn":
            record_realtime_trace("utterance_judged", turn_id=correlation.get("turn_id", ""),
                                  verdict=verdict)
            raise UtteranceJudgedError(verdict)
        uid = value.get("event_uid") if isinstance(value, dict) else None
        if not isinstance(uid, str):
            msg = "the brain's answer to an utterance names no event"
            raise BrainCallError(msg)
        record_realtime_trace("utterance_delivered", turn_id=correlation.get("turn_id", ""),
                              event_uid=uid)
        return Event(
            uid, "utterance.received", 1, int(time.time() * 1000), dict(payload), None,
            dict(correlation),
        )


class RemotePolish:
    """Dictation's polish for a terminal, which has no model key: the brain's model does it.

    The words, where they will land and the word list kept on this machine go over the link
    (the list is read per call, so an edit counts from the next dictation); the brain answers
    with the polished text and records the spend (ADR 0183).
    """

    def __init__(self, link: VoiceLink, vocab: Callable[[], Sequence[str]]) -> None:
        """Ask the brain over ``link``; ``vocab`` reads this machine's word list."""
        self._link = link
        self._vocab = vocab

    def warm(self) -> None:
        """Nothing to open here: the brain's own client holds the connection."""

    def __call__(self, raw: str, context: Mapping[str, str], language: str) -> str:
        """The polished text.

        Raises:
            BrainCallError: the brain is not reachable, does not polish, or did not answer in
                time; dictation shows the raw words with that reason.
        """
        value = self._link.ask(
            "polish",
            {"raw": raw, "context": dict(context), "language": language,
             "vocab": list(self._vocab())},
            timeout_s=POLISH_TIMEOUT_S,
        )
        text = value.get("text") if isinstance(value, dict) else None
        if not isinstance(text, str):
            msg = "the brain's answer to a polish names no text"
            raise BrainCallError(msg)
        return text


class LinkedControls:
    """The brain's conversation mode and quiet level, as the capture session reads them.

    Both belong to the brain: one switch each, with effects there (ADR 0174 ends a spoken
    volume or speed with the conversation, proactive speech waits for it, quiet is saved and
    shown). The session reads ``conversation`` on every idle frame, so a local copy answers it
    and a worker keeps the copy and the brain in step: it applies the session's changes in
    order (waiting for the brain to take each), and reads the brain's state once a second, so a
    switch flipped from a surface reaches the terminal. Mute is per device and not here.

    The same poll carries this terminal's half of the voice floor (ADR 0407): ``report``, when
    set, says whether it can hear his wake word and how long ago he last used this Mac, and
    ``floor`` is where the brain's answer puts it, ``mine`` until the brain says otherwise and
    as soon as this terminal turns conversation mode on itself.
    """

    def __init__(self, link: VoiceLink, *, poll_s: float = CONTROLS_POLL_S) -> None:
        """Start with conversation off; call :meth:`start` to begin keeping in step."""
        self._link = link
        self._poll_s = poll_s
        self._conversation = False
        self._changes: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_surface_exit: Callable[[], None] | None = None
        """Called when a surface turned conversation off while it was on here."""
        self.report: Callable[[], dict[str, Any]] | None = None
        """``{"hears": bool, "idle_s": float | None}`` of this terminal, read once a poll."""
        self._floor: str = "mine"

    def floor(self) -> str:
        """This terminal's standing on the voice floor: ``mine``, ``other`` or ``taken``."""
        return self._floor

    def conversation(self) -> bool:
        """Whether conversation mode is on."""
        return self._conversation

    def set_conversation(self, on: bool, reason: str) -> None:  # noqa: FBT001 - session callback shape
        """The session turned conversation on (a wake) or off (a dismissal, quiet)."""
        self._conversation = on
        if on:
            self._floor = "mine"  # the brain makes it so as it takes the change
        self._changes.put(("conversation", {"on": on, "reason": reason}))

    def set_quiet(self, level: str) -> None:
        """The session heard a quiet command."""
        self._changes.put(("quiet", {"level": level}))

    def start(self) -> None:
        """Begin keeping this terminal and the brain in step."""
        self._thread = threading.Thread(target=self._run, name="terminal-controls", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop; a change not yet taken by the brain is dropped."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=CONTROLS_TIMEOUT_S + 1.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                op, args = self._changes.get(timeout=self._poll_s)
            except queue.Empty:
                self._poll()
                continue
            try:
                self._link.ask(op, args, timeout_s=CONTROLS_TIMEOUT_S)
            except BrainCallError as exc:
                LOGGER.warning("the brain did not take %s %s: %s", op, args, exc)

    def _poll(self) -> None:
        args: dict[str, Any] = {"busy": self._conversation}
        if self.report is not None:
            try:
                args |= self.report()
            except Exception:  # noqa: BLE001 - the floor cannot stop the controls
                LOGGER.warning("reading this terminal's floor report failed", exc_info=True)
        try:
            state = self._link.ask("controls", args, timeout_s=CONTROLS_TIMEOUT_S)
        except BrainCallError:
            return
        if not isinstance(state, dict) or not self._changes.empty():
            return
        if state.get("floor") in STANDINGS:
            self._floor = state["floor"]
        on = state.get("conversation") is True
        was, self._conversation = self._conversation, on
        if was and not on and self.on_surface_exit is not None:
            try:
                self.on_surface_exit()
            except Exception:  # noqa: BLE001 - the exit is best effort; the switch is already off
                LOGGER.warning("leaving conversation mode failed", exc_info=True)
