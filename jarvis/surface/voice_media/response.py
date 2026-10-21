"""What the media owner holds of a response: its chunks and the playback now active."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from jarvis.surface.voice_tts import TTSSession, _preprocess_for_speech

if TYPE_CHECKING:
    from jarvis.shared import Event
    from jarvis.surface.voice_ledger import GenerationLease
    from jarvis.surface.voice_media.contract import MediaSubmitOutcome

CHANNEL_TAG_RE = re.compile(r"</?(?:voice|document)>")

_ChannelMode = Literal["plain", "voice", "document"]


def _structured_voice_spans(raw_text: str) -> list[tuple[int, int]]:
    """Return voice-only spans after lexing the complete bounded response text.

    Parsing the joined response is the lexical carry: a tag may be split at
    any byte boundary between renderer chunks, while the returned spans are
    later projected back onto those original chunk identities.
    """
    spans: list[tuple[int, int]] = []
    mode: _ChannelMode = "plain"
    cursor = 0
    for match in CHANNEL_TAG_RE.finditer(raw_text):
        if mode == "voice" and cursor < match.start():
            spans.append((cursor, match.start()))
        tag = match.group(0)
        if tag == "<voice>":
            mode = "voice"
        elif tag == "<document>":
            mode = "document"
        elif (tag == "</voice>" and mode == "voice") or (
            tag == "</document>" and mode == "document"
        ):
            mode = "plain"
        cursor = match.end()
    if mode == "voice" and cursor < len(raw_text):
        spans.append((cursor, len(raw_text)))
    return spans


@dataclass(frozen=True)
class ResponseChunk:
    """One renderer chunk of a response, with the hash it was committed under."""

    sequence: int
    raw_text: str
    segment_hash: str
    source_event_uid: str = ""


@dataclass
class ResponseBuffer:
    """A response as its committed rows built it up: header, chunks and scheduling flags."""

    row_id: int
    source_event_id: str
    response_id: str
    response_group_id: str
    turn_id: str
    phase: str
    channel: str
    gate_mode: str
    chunks: dict[int, ResponseChunk] = field(default_factory=dict)
    emitted: bool = False
    stream: bool = False
    incremental: bool = False
    scheduled: bool = False
    tagged: bool = False
    updated: asyncio.Event = field(default_factory=asyncio.Event)

    def append_voice_suffix(self, voice_text: object, *, source_event_uid: str) -> None:
        """Speak the emitted voice text no chunk carried as one final segment."""
        sequences = sorted(self.chunks)
        if not isinstance(voice_text, str) or sequences != list(range(len(sequences))):
            return
        committed = "".join(self.chunks[index].raw_text for index in sequences)
        if len(voice_text) <= len(committed) or not voice_text.startswith(committed):
            return
        suffix = voice_text[len(committed) :]
        self.chunks[len(sequences)] = ResponseChunk(
            sequence=len(sequences),
            raw_text=suffix,
            segment_hash=hashlib.sha256(suffix.encode()).hexdigest(),
            source_event_uid=source_event_uid,
        )

    def speech_segments(self) -> list[tuple[int, str, str]]:
        """Map joined structured spans back to exact renderer identities."""
        ordered = [self.chunks[key] for key in sorted(self.chunks)]
        raw = "".join(chunk.raw_text for chunk in ordered)
        structured = CHANNEL_TAG_RE.search(raw) is not None
        voice_spans = _structured_voice_spans(raw) if structured else []
        segments: list[tuple[int, str, str]] = []
        chunk_start = 0
        for chunk in ordered:
            if structured:
                chunk_end = chunk_start + len(chunk.raw_text)
                raw_speech = "".join(
                    raw[max(chunk_start, start) : min(chunk_end, end)]
                    for start, end in voice_spans
                    if start < chunk_end and end > chunk_start
                )
            else:
                raw_speech = chunk.raw_text
            text = _preprocess_for_speech(raw_speech)
            if text:
                segments.append((chunk.sequence, text, chunk.segment_hash))
            chunk_start += len(chunk.raw_text)
        return segments

    def speech_text(self) -> str:
        """Return the text this response speaks, its segments joined."""
        return "".join(text for _sequence, text, _hash in self.speech_segments())


@dataclass
class ActiveResponse:
    """The response now on the player: its lease, tasks, session and fallback handles."""

    response: ResponseBuffer
    lease: GenerationLease
    task: asyncio.Task[None] | None = None
    presentation_task: asyncio.Task[None] | None = None
    session: TTSSession | None = None
    fallback_process: asyncio.subprocess.Process | None = None
    fallback_spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None
    fallback_janitor_task: asyncio.Task[bool] | None = None
    output_lease: bool = False
    last_checkpoint_text: str = ""
    provider_label: str = "minimax_ws_streaming"
    advance_after_cleanup: bool = False
    terminal_commit_pending: bool = False
    activation_event_uid: str | None = None
    prepared_text: str = ""
    starvation_gaps_at_start: int = 0
    host_underflows_at_start: int = 0
    last_position: tuple[int, int, bool] | None = None
    position_stopped: bool = False


@dataclass(frozen=True)
class MediaCommand:
    """One committed row handed to the actor, with the future its submitter awaits."""

    row_id: int
    event: Event
    origin: str
    acknowledged: asyncio.Future[MediaSubmitOutcome]
