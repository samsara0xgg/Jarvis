"""Typed presentation history folded from durable output and playback facts.

Generated/available text is never promoted to heard text. Only an explicit,
hash-validated playback prefix with a usable cursor can populate that field.
The cursor label remains attached: estimated DAC evidence is not a microphone
measurement or proof that the human attended to the sound.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from jarvis.shared import Event

from jarvis.state.conversation_playback import HeardPrefix, PlaybackHistory, text_hash

MAX_RESPONSES_PER_TURN = 8
MAX_RESPONSE_TEXT_CHARS = 65_536
MAX_RESPONSE_FACTS = 1_024


@dataclass(frozen=True)
class PresentationRecord:
    """One response's disjoint knowledge channels, not an assistant utterance."""

    response_id: str
    turn_id: str
    phase: str
    channel: str
    panel_available: str
    audit_generated_hash: str | None
    spoken_heard: HeardPrefix | None
    source_event_uids: tuple[str, ...]
    consistent: bool
    truncated: bool = False
    # L5 dropped its speech before any of it played (surface.speech_dropped).
    unspoken: bool = False


@dataclass(frozen=True)
class ConversationTurn:
    """Actual user input plus typed output facts, ordered by input admission."""

    turn_id: str
    user_text: str
    input_event_uid: str
    input_channel: str
    responses: tuple[PresentationRecord, ...]


@dataclass(frozen=True)
class ConversationHistory:
    """Bounded turn window; truncation and consistency are explicit."""

    turns: tuple[ConversationTurn, ...] = ()
    truncated: bool = False
    consistent: bool = True


@dataclass
class _Response:
    response_id: str
    turn_id: str
    phase: str = "unknown"
    channel: str = "unknown"
    chunks: dict[int, tuple[str, str]] = field(default_factory=dict)
    panel_final: str | None = None
    audit_generated_hash: str | None = None
    playback: PlaybackHistory = field(default_factory=PlaybackHistory)
    surface_sources: set[str] = field(default_factory=set)
    sources: list[str] = field(default_factory=list)
    consistent: bool = True
    truncated: bool = False
    text_chars: int = 0
    dropped: bool = False

    def freeze(self) -> PresentationRecord:
        sequences = sorted(self.chunks)
        contiguous = sequences == list(range(len(sequences)))
        panel = self.panel_final
        if panel is None:
            panel = "".join(self.chunks[index][1] for index in sequences) if contiguous else ""
        valid = self.consistent and contiguous and self.playback.consistent and not self.truncated
        return PresentationRecord(
            response_id=self.response_id,
            turn_id=self.turn_id,
            phase=self.phase,
            channel=self.channel,
            panel_available=panel,
            audit_generated_hash=self.audit_generated_hash,
            spoken_heard=self.playback.heard if valid else None,
            source_event_uids=tuple(self.sources),
            consistent=valid,
            truncated=self.truncated,
            unspoken=self.dropped and self.playback.identity is None,
        )


_OUTPUT_TYPES = frozenset(
    {
        "response.started",
        "response.completed",
        "surface.response_open",
        "surface.response_chunk",
        "surface.response_emitted",
        "surface.playback_started",
        "surface.playback_segment_prepared",
        "surface.playback_alignment",
        "surface.playback_checkpoint",
        "surface.playback_completed",
        "surface.playback_interrupted",
        "surface.playback_failed",
        "surface.speech_dropped",
    }
)


def _bind_identity(response: _Response, event: Event) -> bool:
    payload = event.payload
    if str(payload.get("turn_id", response.turn_id)) != response.turn_id:
        response.consistent = False
        return False
    for key, allowed in (
        ("phase", {"commentary", "final"}),
        ("channel", {"speech", "document", "both"}),
    ):
        value = payload.get(key)
        if value is None:
            continue
        previous = getattr(response, key)
        if not isinstance(value, str) or value not in allowed or previous not in {"unknown", value}:
            response.consistent = False
        else:
            setattr(response, key, value)
    return True


def _fold_chunk(response: _Response, event: Event) -> None:
    payload = event.payload
    sequence, text = payload.get("sequence"), payload.get("text")
    if type(sequence) is not int or sequence < 0 or not isinstance(text, str):
        response.consistent = False
        return
    if sequence in response.chunks and response.chunks[sequence] != (event.event_uid, text):
        response.consistent = False
    response.text_chars += len(text)
    if response.text_chars > MAX_RESPONSE_TEXT_CHARS:
        response.truncated = True
        return
    response.chunks[sequence] = (event.event_uid, text)


def _fold_voice_suffix(response: _Response, event: Event) -> None:
    """The voice text no chunk carried is one more segment sourced from the emitted row."""
    voice = event.payload.get("voice_text")
    sequences = sorted(response.chunks)
    if not isinstance(voice, str) or sequences != list(range(len(sequences))):
        return
    committed = "".join(response.chunks[index][1] for index in sequences)
    if len(voice) <= len(committed) or not voice.startswith(committed):
        return
    suffix = voice[len(committed) :]
    response.text_chars += len(suffix)
    if response.text_chars > MAX_RESPONSE_TEXT_CHARS:
        response.truncated = True
        return
    response.chunks[len(sequences)] = (event.event_uid, suffix)


def _fold_output(response: _Response, event: Event) -> None:  # noqa: C901 - closed event-type fold
    payload = event.payload
    if response.truncated:
        return
    if len(response.sources) >= MAX_RESPONSE_FACTS:
        response.truncated = True
        return
    response.sources.append(event.event_uid)
    if not _bind_identity(response, event):
        return
    if event.type in {"surface.response_open", "surface.response_emitted"}:
        response.surface_sources.add(event.event_uid)
    response.dropped |= event.type == "surface.speech_dropped"
    if event.type == "surface.response_chunk":
        _fold_chunk(response, event)
    elif event.type == "surface.response_emitted":
        text = payload.get("text")
        if isinstance(text, str):
            if response.panel_final is not None and response.panel_final != text:
                response.consistent = False
            response.panel_final = text[:MAX_RESPONSE_TEXT_CHARS]
            response.truncated |= len(text) > MAX_RESPONSE_TEXT_CHARS
        _fold_voice_suffix(response, event)
    elif event.type == "response.completed":
        text = payload.get("generated_text")
        if text_hash(text) is not None and text_hash(text) == payload.get(
            "generated_text_hash",
        ):
            response.audit_generated_hash = text_hash(text)
    elif event.type.startswith("surface.playback_"):
        speech_text = payload.get("speech_text", "")
        if isinstance(speech_text, str) and len(speech_text) > MAX_RESPONSE_TEXT_CHARS:
            response.truncated = True
            return
        response.playback.fold(
            event,
            channel=response.channel,
            chunks=response.chunks,
            surface_sources=response.surface_sources,
        )


_INPUT_TYPES = frozenset({"utterance.received", "surface.user_intent"})


class RefoldRequired(Exception):  # noqa: N818 - a control signal between L2 layers, not an error
    """An incremental fold met a shape only a whole-log fold orders correctly."""


def _build_history(
    retained: Mapping[str, Event],
    responses_by_turn: Mapping[str, Iterable[_Response]],
    *,
    truncated: bool,
    consistent: bool,
) -> ConversationHistory:
    turns = tuple(
        ConversationTurn(
            turn_id=turn_id,
            user_text=str(event.payload.get("transcript", "")),
            input_event_uid=event.event_uid,
            input_channel=str(event.payload.get("channel", "unknown")),
            responses=tuple(r.freeze() for r in responses_by_turn.get(turn_id, ())),
        )
        for turn_id, event in retained.items()
    )
    return ConversationHistory(
        turns=turns,
        truncated=truncated or any(record.truncated for turn in turns for record in turn.responses),
        consistent=consistent
        and all(record.consistent for turn in turns for record in turn.responses),
    )


def _response_id(event: Event, turn_id: str) -> str:
    response_id = event.payload.get("response_id")
    return response_id if isinstance(response_id, str) and response_id else "legacy:" + turn_id


def fold_conversation_history(  # noqa: C901 - bounded two-pass input/output fold
    events: Iterable[Event],
    *,
    max_turns: int = 20,
) -> ConversationHistory:
    """Fold the same materialized Event Log used by the other packet projections.

    The whole-log form: it sees every input before it folds any output, so it
    is the reference :class:`ConversationFold` must equal.
    """
    if max_turns < 1:
        message = "conversation history requires a positive turn bound"
        raise ValueError(message)
    materialized = tuple(events)
    inputs: dict[str, Event] = {}
    responses: dict[str, _Response] = {}
    consistent = True
    for event in materialized:
        turn_id = event.payload.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        if event.type in _INPUT_TYPES:
            if turn_id in inputs and inputs[turn_id].event_uid != event.event_uid:
                consistent = False
            inputs.setdefault(turn_id, event)
    retained = dict(tuple(inputs.items())[-max_turns:])
    truncated = len(inputs) > max_turns
    per_turn_counts: dict[str, int] = {}
    for event in materialized:
        turn_id = event.payload.get("turn_id")
        if (
            not isinstance(turn_id, str)
            or turn_id not in retained
            or event.type not in _OUTPUT_TYPES
        ):
            continue
        response_id = _response_id(event, turn_id)
        if response_id not in responses:
            if per_turn_counts.get(turn_id, 0) >= MAX_RESPONSES_PER_TURN:
                truncated = True
                continue
            responses[response_id] = _Response(response_id, turn_id)
            per_turn_counts[turn_id] = per_turn_counts.get(turn_id, 0) + 1
        response = responses[response_id]
        _fold_output(response, event)
    grouped: dict[str, list[_Response]] = {}
    for response in responses.values():
        grouped.setdefault(response.turn_id, []).append(response)
    return _build_history(retained, grouped, truncated=truncated, consistent=consistent)


class ConversationFold:
    """The same history, absorbing one event at a time (state is never mutated once shared).

    :func:`fold_conversation_history` reads every input before any output; this
    sees them in log order. They agree when each turn's input precedes its
    outputs and no response id spans two turns, which the daemon's write path
    keeps true. A log that breaks either raises :class:`RefoldRequired` rather
    than guessing: an output whose turn has no input yet is remembered by turn
    id only, and an input arriving for such a turn needs the whole log again.
    """

    __slots__ = (
        "_capped",
        "_consistent",
        "_dropped",
        "_inputs",
        "_max_turns",
        "_owned",
        "_owner",
        "_retained",
        "_turns",
    )

    def __init__(self, max_turns: int = 20) -> None:
        """The history of an empty log."""
        if max_turns < 1:
            message = "conversation history requires a positive turn bound"
            raise ValueError(message)
        self._max_turns = max_turns
        self._inputs: dict[str, str] = {}  # every turn id ever seen -> its first input uid
        self._retained: dict[str, Event] = {}  # the last ``max_turns`` inputs, oldest first
        self._turns: dict[str, dict[str, _Response]] = {}  # retained turn -> its responses
        self._owner: dict[str, str] = {}  # retained response id -> its turn
        self._capped: set[str] = set()  # retained turns that hit MAX_RESPONSES_PER_TURN
        self._dropped: set[str] = set()  # turns whose outputs arrived with no input yet
        self._consistent = True
        self._owned: set[int] = set()  # responses this copy may mutate

    def copy(self) -> ConversationFold:
        """A twin that shares every response until it has to change one."""
        twin = ConversationFold(self._max_turns)
        twin._inputs = dict(self._inputs)
        twin._retained = dict(self._retained)
        twin._turns = {turn: dict(rs) for turn, rs in self._turns.items()}
        twin._owner = dict(self._owner)
        twin._capped = set(self._capped)
        twin._dropped = set(self._dropped)
        twin._consistent = self._consistent
        return twin

    def apply(self, event: Event) -> None:
        """Absorb the next event in log order."""
        turn_id = event.payload.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            return
        if event.type in _INPUT_TYPES:
            self._apply_input(turn_id, event)
        elif event.type in _OUTPUT_TYPES:
            self._apply_output(turn_id, event)

    def _apply_input(self, turn_id: str, event: Event) -> None:
        first = self._inputs.get(turn_id)
        if first is not None:
            self._consistent = self._consistent and first == event.event_uid
            return
        if turn_id in self._dropped:
            raise RefoldRequired
        self._inputs[turn_id] = event.event_uid
        self._retained[turn_id] = event
        if len(self._retained) > self._max_turns:
            evicted = next(iter(self._retained))
            del self._retained[evicted]
            self._capped.discard(evicted)
            for response_id in self._turns.pop(evicted, ()):
                del self._owner[response_id]
        self._turns[turn_id] = {}

    def _apply_output(self, turn_id: str, event: Event) -> None:
        if turn_id not in self._inputs:
            self._dropped.add(turn_id)
            return
        responses = self._turns.get(turn_id)
        if responses is None:
            return  # an evicted turn never comes back into the window
        response_id = _response_id(event, turn_id)
        owner = self._owner.get(response_id)
        if owner is None:
            if len(responses) >= MAX_RESPONSES_PER_TURN:
                self._capped.add(turn_id)
                return
            response = _Response(response_id, turn_id)
            responses[response_id] = response
            self._owner[response_id] = turn_id
            self._owned.add(id(response))
        elif owner != turn_id:
            raise RefoldRequired
        else:
            response = responses[response_id]
            if id(response) not in self._owned:
                response = copy.deepcopy(response)
                responses[response_id] = response
                self._owned.add(id(response))
        _fold_output(response, event)

    def freeze(self) -> ConversationHistory:
        """The history as :func:`fold_conversation_history` would return it."""
        return _build_history(
            self._retained,
            {turn: tuple(rs.values()) for turn, rs in self._turns.items()},
            truncated=len(self._inputs) > self._max_turns or bool(self._capped),
            consistent=self._consistent,
        )
