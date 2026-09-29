"""Replay L5 presentation facts without deriving speech or upgrading evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

_SHA256_LENGTH = 64
_WORD_BOUNDARY_FIELDS = 2

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    # The activation completed with every prepared segment played: the
    # whole answer was heard, whatever the text cleanup made of its spacing.
    complete: bool = False
    submitted_samples: int = 0
    ended: str | None = None


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
    word_boundaries: dict[int, dict[int, int]] = field(default_factory=dict)
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
        elif event.type == "surface.playback_alignment":
            self._align(event)
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
        self.word_boundaries.clear()
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

    def _align(self, event: Event) -> None:
        """Bind append-only word ends to an already validated speech segment."""
        payload = event.payload
        sequence, rows = payload.get("sequence"), payload.get("word_boundaries")
        if type(sequence) is not int or sequence not in self.segments or not isinstance(rows, list):
            self.consistent = False
            return
        text = self.segments[sequence]
        if payload.get("speech_text_hash") != text_hash(text):
            self.consistent = False
            return
        boundaries = self.word_boundaries.setdefault(sequence, {})
        last_end = max(boundaries, default=0)
        last_sample = boundaries.get(last_end, 0)
        for row in rows:
            if (
                not isinstance(row, list) or len(row) != _WORD_BOUNDARY_FIELDS
                or type(row[0]) is not int or type(row[1]) is not int
                or not last_end < row[0] <= len(text) or row[1] < last_sample
            ):
                self.consistent = False
                return
            last_end, last_sample = row
            boundaries[last_end] = last_sample

    def _partial_prefix(
        self, payload: Mapping[str, object], sequence: int, submitted: int,
    ) -> str | None:
        """Only a durable word end behind the audible sample horizon is admissible."""
        partial = payload.get("heard_partial_sequence")
        if partial is None:
            return ""
        following = next((key for key in self.segments if key > sequence), None)
        end = payload.get("heard_partial_text_end")
        audible = payload.get("estimated_audible_samples")
        if (
            type(partial) is not int or partial != following
            or type(end) is not int or type(audible) is not int
            or not 0 <= audible <= submitted
        ):
            return None
        sample_end = self.word_boundaries.get(partial, {}).get(end)
        if sample_end is None or sample_end > audible:
            return None
        return self.segments[partial][:end]

    def _cursor(self, event: Event) -> None:  # noqa: C901, PLR0911, PLR0912 - fail-closed cursor table
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
        if sequence is None and (not text or payload.get("heard_partial_sequence") is not None):
            cursor_sequence = -1
        elif type(sequence) is int and sequence in self.segments and submitted > 0:
            cursor_sequence = sequence
        else:
            self.consistent = False
            return
        expected = "".join(value for key, value in self.segments.items() if key <= cursor_sequence)
        partial = self._partial_prefix(payload, cursor_sequence, submitted)
        if partial is None:
            self.consistent = False
            return
        expected += partial
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
        quality = _heard_quality(event, submitted=submitted)
        if quality is None:
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
            quality=quality,
            source_event_uid=event.event_uid,
            playback_generation_id=self.identity[1],
            session_id=self.identity[0],
            activation_event_uid=self.activation_uid,
            complete=event.type == "surface.playback_completed"
            and text == "".join(self.segments.values()),
            submitted_samples=submitted,
            ended=(
                None if event.type == "surface.playback_checkpoint"
                else event.type.rsplit("_", 1)[-1]
            ),
        )


def _heard_quality(event: Event, *, submitted: int) -> Literal["measured_dac", "estimated"] | None:
    """The cursor's quality label, or None when it proves no heard prefix."""
    quality = event.payload.get("cursor_quality")
    if quality == "measured_dac":
        return "measured_dac"
    if quality == "estimated":
        return "estimated"
    # An activation that ended before one sample reached the device was never
    # measured by a callback, yet its empty prefix is certain.
    if event.type != "surface.playback_checkpoint" and not submitted:
        return "estimated"
    return None
