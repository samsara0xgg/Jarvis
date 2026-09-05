"""Replay L5 presentation facts without deriving speech or upgrading evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

_SHA256_LENGTH = 64

if TYPE_CHECKING:
    from jarvis.shared import Event


def text_hash(text: object) -> str | None:
    """Malformed historical Unicode is missing evidence, never a replay crash."""
    if not isinstance(text, str):
        return None
    try:
        return hashlib.sha256(text.encode()).hexdigest()
    except UnicodeEncodeError:
        return None


@dataclass(frozen=True)
class HeardPrefix:
    """A conservative cursor tied to one durable L5 activation."""

    text: str
    quality: Literal["measured_dac", "estimated"]
    source_event_uid: str
    playback_generation_id: int
    session_id: str
    activation_event_uid: str


@dataclass
class PlaybackHistory:
    """Only the latest legitimate activation can advance a response's cursor."""

    identity: tuple[str, int] | None = None
    activation_uid: str | None = None
    speech_hash: str | None = None
    first_segment_hash: str | None = None
    generations: dict[str, int] = field(default_factory=dict)
    retired_sessions: set[str] = field(default_factory=set)
    generation_owners: dict[int, str] = field(default_factory=dict)
    segments: dict[int, str] = field(default_factory=dict)
    heard: HeardPrefix | None = None
    last_sequence: int = -1
    last_submitted: int = 0
    last_text: str = ""
    terminal: bool = False
    consistent: bool = True

    def fold(
        self,
        event: Event,
        *,
        channel: str,
        chunks: dict[int, tuple[str, str]],
        surface_sources: set[str],
    ) -> None:
        """Validate source identities before interpreting any cursor text."""
        payload = event.payload
        session, generation = payload.get("session_id"), payload.get("playback_generation_id")
        if (
            not isinstance(session, str)
            or not session
            or type(generation) is not int
            or generation < 0
        ):
            self.consistent = False
            return
        identity = (session, generation)
        if event.type == "surface.playback_started":
            self._start(event, identity=identity, channel=channel, surface_sources=surface_sources)
            return
        if self.identity is None:
            return  # Legacy rows have no durable activation evidence.
        if identity != self.identity:
            # Late callbacks from another boot/lease cannot extend the current prefix.
            return
        if event.source_event_id != self.activation_uid:
            self.consistent = False
            return
        if self.terminal:
            return
        if event.type == "surface.playback_segment_prepared":
            self._prepare(event, chunks)
        else:
            if event.type != "surface.playback_checkpoint":
                self.terminal = True
            self._cursor(event)

    def _start(
        self,
        event: Event,
        *,
        identity: tuple[str, int],
        channel: str,
        surface_sources: set[str],
    ) -> None:
        session, generation = identity
        digest = event.payload.get("speech_text_hash")
        if (
            channel not in {"speech", "both"}
            or event.source_event_id not in surface_sources
            or not isinstance(digest, str)
            or len(digest) != _SHA256_LENGTH
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            self.consistent = False
            return
        if generation in self.generation_owners and self.generation_owners[generation] != session:
            self.consistent = False
            return
        incremental = event.payload.get("incremental") is True
        if not incremental and self.speech_hash is not None and digest != self.speech_hash:
            self.consistent = False
            return
        if session in self.retired_sessions or generation < max(self.generation_owners, default=-1):
            return
        if generation == self.generations.get(session):
            if event.event_uid != self.activation_uid:
                self.consistent = False
            return
        if self.identity is not None and self.identity[0] != session:
            self.retired_sessions.add(self.identity[0])
        self.generations[session] = generation
        self.generation_owners[generation] = session
        self.identity = identity
        self.activation_uid = event.event_uid
        # An incremental lease commits only its first segment at activation;
        # the terminal binds the full hash to the prepared concatenation.
        self.speech_hash = None if incremental else digest
        self.first_segment_hash = digest if incremental else None
        self.segments.clear()
        self.last_sequence, self.last_submitted, self.last_text = -1, 0, ""
        self.terminal = False

    def _prepare(self, event: Event, chunks: dict[int, tuple[str, str]]) -> None:
        payload = event.payload
        sequence, text = payload.get("sequence"), payload.get("speech_text")
        if (
            type(sequence) is not int
            or sequence not in chunks
            or not isinstance(text, str)
            or not text
        ):
            self.consistent = False
            return
        source_uid, raw_text = chunks[sequence]
        if (
            payload.get("source_chunk_event_uid") != source_uid
            or text_hash(raw_text) != payload.get("segment_hash")
            or text_hash(text) is None
            or text_hash(text) != payload.get("speech_text_hash")
            or sequence <= max(self.segments, default=-1)
            or (
                not self.segments
                and self.first_segment_hash is not None
                and text_hash(text) != self.first_segment_hash
            )
        ):
            self.consistent = False
            return
        self.segments[sequence] = text

    def _cursor(self, event: Event) -> None:  # noqa: C901, PLR0911 - fail-closed cursor evidence table
        payload = event.payload
        text, digest = payload.get("heard_text"), payload.get("heard_text_hash")
        sequence, submitted = (
            payload.get("heard_through_sequence"),
            payload.get("submitted_samples"),
        )
        speech_hash = self.speech_hash
        if speech_hash is None:
            speech_hash = text_hash("".join(self.segments.values()))
        if (
            not isinstance(text, str)
            or text_hash(text) is None
            or text_hash(text) != digest
            or type(submitted) is not int
            or submitted < self.last_submitted
            or ("speech_text_hash" in payload and payload["speech_text_hash"] != speech_hash)
        ):
            self.consistent = False
            return
        if sequence is None and not text:
            cursor_sequence = -1
        elif type(sequence) is int and sequence in self.segments and submitted > 0:
            cursor_sequence = sequence
        else:
            self.consistent = False
            return
        expected = "".join(value for key, value in self.segments.items() if key <= cursor_sequence)
        if (
            cursor_sequence < self.last_sequence
            or text != expected
            or not text.startswith(self.last_text)
        ):
            self.consistent = False
            return
        if (
            event.type == "surface.playback_completed"
            and text_hash("".join(self.segments.values())) != speech_hash
        ):
            self.consistent = False
            return
        self.last_sequence, self.last_submitted, self.last_text = cursor_sequence, submitted, text
        quality = payload.get("cursor_quality")
        if not isinstance(quality, str) or quality not in {"estimated", "measured_dac"}:
            return
        if self.identity is None or self.activation_uid is None:
            self.consistent = False
            return
        if self.heard is not None:
            if self.heard.text.startswith(text) and len(self.heard.text) > len(text):
                return  # A new replay does not erase an already proven prefix.
            if not text.startswith(self.heard.text):
                self.consistent = False
                return
        self.heard = HeardPrefix(
            text=text,
            quality="measured_dac" if quality == "measured_dac" else "estimated",
            source_event_uid=event.event_uid,
            playback_generation_id=self.identity[1],
            session_id=self.identity[0],
            activation_event_uid=self.activation_uid,
        )
