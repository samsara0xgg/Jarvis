"""Typed presentation history folded from durable output and playback facts.

Generated/available text is never promoted to heard text. Only an explicit,
hash-validated playback prefix with a usable cursor can populate that field.
The cursor label remains attached: estimated DAC evidence is not a microphone
measurement or proof that the human attended to the sound.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

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
        "surface.playback_checkpoint",
        "surface.playback_completed",
        "surface.playback_interrupted",
        "surface.playback_failed",
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


def fold_conversation_history(  # noqa: C901 - bounded two-pass input/output fold
    events: Iterable[Event],
    *,
    max_turns: int = 20,
) -> ConversationHistory:
    """Fold the same materialized Event Log used by the other packet projections."""
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
        if event.type in {"utterance.received", "surface.user_intent"}:
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
        response_id = event.payload.get("response_id")
        if not isinstance(response_id, str) or not response_id:
            response_id = "legacy:" + turn_id
        if response_id not in responses:
            if per_turn_counts.get(turn_id, 0) >= MAX_RESPONSES_PER_TURN:
                truncated = True
                continue
            responses[response_id] = _Response(response_id, turn_id)
            per_turn_counts[turn_id] = per_turn_counts.get(turn_id, 0) + 1
        response = responses[response_id]
        _fold_output(response, event)
    grouped: dict[str, list[PresentationRecord]] = {}
    for response in responses.values():
        grouped.setdefault(response.turn_id, []).append(response.freeze())
    turns = tuple(
        ConversationTurn(
            turn_id=turn_id,
            user_text=str(event.payload.get("transcript", "")),
            input_event_uid=event.event_uid,
            input_channel=str(event.payload.get("channel", "unknown")),
            responses=tuple(grouped.get(turn_id, ())),
        )
        for turn_id, event in retained.items()
    )
    return ConversationHistory(
        turns=turns,
        truncated=truncated or any(record.truncated for turn in turns for record in turn.responses),
        consistent=consistent
        and all(record.consistent for turn in turns for record in turn.responses),
    )
