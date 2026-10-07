"""ADR 0172: a voice terminal listens, and the brain holds the turn.

A voice terminal runs the capture session (wake, VAD, endpointing, ASR, barge-in) as a daemon
does. What the session reaches in brain state crosses the ``/terminal/ws`` link of
:mod:`jarvis.surface.terminal_link` as one frame pair::

    terminal -> brain   {"type": "ask", "id": "<32 hex>", "op": "words", "args": {...}}
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
utterance the final words, as the pipeline     ``{"event_uid"}``: written once as
         writes them (transcript, turn_id,     ``utterance.received`` under the device's
         utterance_id, ...)                    name, so a second send of it writes nothing
words    turn_id, text, recent, over_her,      ``{"choice": str | null}``: the word judge
         confirm
working  (none)                                ``{"working": bool}``: a turn is in flight
recent   (none)                                ``{"text": str}``: what she said lately
interrupt source                               ``{"outcome": str}``: generation cancel
supersede turn_id                              ``{}``: unspoken answers dropped and cancelled
cancel_runs (none)                             ``{}``
controls (none)                                ``{"conversation": bool, "quiet": str}``
conversation on, reason                        ``{}``: set conversation mode
quiet    level                                 ``{}``
hold     held                                  (tell) the run-hold half of hold-output
note     turn_id, verdict, text, over_her,     (tell) the word judge's note
         conversation
begin    turn_id, text, recent, over_her,      (tell) the word judge's one request
         conversation
say      turn_id, reason, text                 (tell) one spoken line back
======== ===================================== ==============================================

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
from jarvis.surface.terminal_speaker import BrainCallError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

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
        "utterance", "words", "working", "recent", "interrupt", "supersede", "cancel_runs",
        "controls", "conversation", "quiet",
    },
)
TELLS: Final = frozenset({"hold", "note", "begin", "say"})

UTTERANCE_TIMEOUT_S: Final = 8.0
WORDS_TIMEOUT_S: Final = 3.0
"""The word judge blocks up to its own ``words_timeout_ms`` (0.8 s) on the brain."""
WORKING_TIMEOUT_S: Final = 1.0
RECENT_TIMEOUT_S: Final = 1.0
INTERRUPT_TIMEOUT_S: Final = 3.0
RUNS_TIMEOUT_S: Final = 5.0
CONTROLS_TIMEOUT_S: Final = 2.0
CONTROLS_POLL_S: Final = 1.0
_MAX_TEXT_CHARS: Final = 20_000
_MAX_ROUTED: Final = 256
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
    said lately, no run to hold or cancel.
    """

    ask_words: Callable[[str, str, str, bool, bool], str | None] | None = None
    note_words: Callable[[str, str, str, bool, bool], None] | None = None
    begin_line: Callable[[str, str, str, bool, bool], None] | None = None
    answer_words: Callable[[str, str, str], None] | None = None
    turn_working: Callable[[], bool] | None = None
    recent_speech: Callable[[], str] | None = None
    hold_runs: Callable[[bool], None] | None = None
    interrupt: Callable[[str], str] | None = None
    supersede: Callable[[str], None] | None = None
    cancel_runs: Callable[[], None] | None = None
    set_conversation: Callable[[bool, str], None] | None = None
    set_quiet: Callable[[str], None] | None = None
    controls: Callable[[], Mapping[str, Any]] | None = None


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
            "words": self._words, "working": self._working, "recent": self._recent,
            "interrupt": self._interrupt, "supersede": self._supersede,
            "cancel_runs": self._cancel_runs, "controls": self._controls,
            "conversation": self._conversation, "quiet": self._quiet,
            "hold": self._hold, "note": self._note, "begin": self._begin, "say": self._say,
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
                reply["value"] = self._utterance(link, args)
            else:
                reply["value"] = await asyncio.to_thread(self._ops[str(op)], args)
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

    # -- the asks ----------------------------------------------------------------------

    def _utterance(self, link: _Link, args: Mapping[str, Any]) -> dict[str, Any]:
        """Write the utterance once, and make the terminal that heard it the answer's place."""
        turn_id = args.get("turn_id")
        if isinstance(turn_id, str) and link.speech is not None and self._hub.voice is not None:
            self._hub.voice.route(turn_id, link.speech)  # before the row, so no answer beats it
        return {"event_uid": self._events.record_utterance(link.name, args)}

    def _words(self, args: Mapping[str, Any]) -> dict[str, Any]:
        line = (
            _text(args, "turn_id", _ID_CHARS), _text(args, "text"), _text(args, "recent"),
            _flag(args, "over_her"), _flag(args, "confirm"),
        )
        hook = self.hooks.ask_words
        choice = hook(*line) if hook is not None else None
        return {"choice": choice if isinstance(choice, str) else None}

    def _working(self, _args: Mapping[str, Any]) -> dict[str, Any]:
        hook = self.hooks.turn_working
        return {"working": bool(hook()) if hook is not None else False}

    def _recent(self, _args: Mapping[str, Any]) -> dict[str, Any]:
        hook = self.hooks.recent_speech
        return {"text": hook() if hook is not None else ""}

    def _interrupt(self, args: Mapping[str, Any]) -> dict[str, Any]:
        hook = self.hooks.interrupt
        source = _text(args, "source", _ID_CHARS)
        return {"outcome": hook(source) if hook is not None else "no_open_run"}

    def _supersede(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.hooks.supersede is not None:
            self.hooks.supersede(_text(args, "turn_id", _ID_CHARS))
        return {}

    def _cancel_runs(self, _args: Mapping[str, Any]) -> dict[str, Any]:
        if self.hooks.cancel_runs is not None:
            self.hooks.cancel_runs()
        return {}

    def _controls(self, _args: Mapping[str, Any]) -> dict[str, Any]:
        hook = self.hooks.controls
        state = hook() if hook is not None else {}
        return {"conversation": state.get("conversation") is True,
                "quiet": state.get("quiet", "off")}

    def _conversation(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.hooks.set_conversation is not None:
            self.hooks.set_conversation(_flag(args, "on"), _text(args, "reason", _ID_CHARS))
        return {}

    def _quiet(self, args: Mapping[str, Any]) -> dict[str, Any]:
        level = _text(args, "level", _ID_CHARS)
        if level not in QUIET_LEVELS:
            msg = f"unknown quiet level {level!r}"
            raise _Refused(msg)
        if self.hooks.set_quiet is not None:
            self.hooks.set_quiet(level)
        return {}

    # -- the tells ---------------------------------------------------------------------

    def _hold(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.hooks.hold_runs is not None:
            self.hooks.hold_runs(_flag(args, "held"))
        return {}

    def _note(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.hooks.note_words is not None:
            self.hooks.note_words(
                _text(args, "turn_id", _ID_CHARS), _text(args, "verdict", _ID_CHARS),
                _text(args, "text"), _flag(args, "over_her"), _flag(args, "conversation"),
            )
        return {}

    def _begin(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.hooks.begin_line is not None:
            self.hooks.begin_line(
                _text(args, "turn_id", _ID_CHARS), _text(args, "text"), _text(args, "recent"),
                _flag(args, "over_her"), _flag(args, "conversation"),
            )
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

        The ``drop_unspoken`` of :class:`~jarvis.surface.voice_media.StreamingTTSPipeline`, over
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
    there when it cannot be answered: no judge means a turn, no read means nothing is in
    flight and nothing was said, and a stop that cannot be asked of the brain still stopped
    the speaker. Only the utterance has none: an utterance the brain never took is an error.
    """

    def __init__(self, link: VoiceLink) -> None:
        """Ask and tell the brain over ``link``."""
        self._link = link

    def ask_words(
        self, turn_id: str, text: str, recent: str, over_her: bool, confirm: bool,  # noqa: FBT001
    ) -> str | None:
        """The word judge's choice, or ``None`` (a turn) when it cannot be asked."""
        try:
            value = self._link.ask(
                "words", {"turn_id": turn_id, "text": text, "recent": recent,
                          "over_her": over_her, "confirm": confirm},
                timeout_s=WORDS_TIMEOUT_S,
            )
        except BrainCallError as exc:
            LOGGER.warning("the word judge could not be asked (%s); the line stays a turn", exc)
            return None
        choice = value.get("choice") if isinstance(value, dict) else None
        return choice if isinstance(choice, str) else None

    def note_words(
        self, turn_id: str, verdict: str, text: str, over_her: bool, conversation: bool,  # noqa: FBT001
    ) -> None:
        """Tell the brain a line the regexes settled alone."""
        self._link.tell("note", {"turn_id": turn_id, "verdict": verdict, "text": text,
                                 "over_her": over_her, "conversation": conversation})

    def begin_line(
        self, turn_id: str, text: str, recent: str, over_her: bool, conversation: bool,  # noqa: FBT001
    ) -> None:
        """Tell the brain to send the line's one word-judge request."""
        self._link.tell("begin", {"turn_id": turn_id, "text": text, "recent": recent,
                                  "over_her": over_her, "conversation": conversation})

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

    def recent_speech(self) -> str:
        """What she said in the last minute; ``""`` when it cannot be asked."""
        try:
            value = self._link.ask("recent", {}, timeout_s=RECENT_TIMEOUT_S)
        except BrainCallError:
            return ""
        text = value.get("text") if isinstance(value, dict) else None
        return text if isinstance(text, str) else ""

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

    def supersede(self, turn_id: str) -> None:
        """Have the brain drop and cancel the unspoken answers a new line replaces."""
        try:
            self._link.ask("supersede", {"turn_id": turn_id}, timeout_s=RUNS_TIMEOUT_S)
        except BrainCallError as exc:
            LOGGER.warning("an unspoken answer could not be superseded: %s", exc)

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

        Raises:
            BrainCallError: the brain did not take it; nothing was written, and the owner is
                not answered.
        """
        try:
            value = self._link.ask("utterance", payload, timeout_s=UTTERANCE_TIMEOUT_S)
        except BrainCallError as exc:
            LOGGER.warning("the brain is not reachable; this utterance was not delivered (%s)", exc)
            raise
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


class LinkedControls:
    """The brain's conversation mode and quiet level, as the capture session reads them.

    Both belong to the brain: one switch each, with effects there (ADR 0174 ends a spoken
    volume or speed with the conversation, proactive speech waits for it, quiet is saved and
    shown). The session reads ``conversation`` on every idle frame, so a local copy answers it
    and a worker keeps the copy and the brain in step: it applies the session's changes in
    order (waiting for the brain to take each), and reads the brain's state once a second, so a
    switch flipped from a surface reaches the terminal. Mute is per device and not here.
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

    def conversation(self) -> bool:
        """Whether conversation mode is on."""
        return self._conversation

    def set_conversation(self, on: bool, reason: str) -> None:  # noqa: FBT001 - session callback shape
        """The session turned conversation on (a wake) or off (a dismissal, quiet)."""
        self._conversation = on
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
        try:
            state = self._link.ask("controls", {}, timeout_s=CONTROLS_TIMEOUT_S)
        except BrainCallError:
            return
        if not isinstance(state, dict) or not self._changes.empty():
            return
        on = state.get("conversation") is True
        was, self._conversation = self._conversation, on
        if was and not on and self.on_surface_exit is not None:
            try:
                self.on_surface_exit()
            except Exception:  # noqa: BLE001 - the exit is best effort; the switch is already off
                LOGGER.warning("leaving conversation mode failed", exc_info=True)
