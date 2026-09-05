"""Incremental TTS from permitted segments: L2 lease rule and L5 segment loop."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from jarvis.state.conversation import fold_conversation_history
from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_playback

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.shared import Event

_SEGMENTS = ("第一句。", "第二句。", "第三句。")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class _Trail:
    """Emit one incremental playback trail row by row, in Event Log order."""

    def __init__(self, conn: sqlite3.Connection, *, response_id: str = "R") -> None:
        self.conn = conn
        self.response_id = response_id
        self.chunks: list[Event] = []
        self.started: Event | None = None
        self.prepared: list[str] = []
        emit_event(
            conn,
            type="utterance.received",
            payload={"turn_id": "T", "transcript": "synthetic", "channel": "voice"},
        )
        self.open = emit_event(
            conn,
            type="surface.response_open",
            payload={
                "turn_id": "T",
                "query": "q",
                "kind": "stream",
                "response_id": response_id,
                "response_group_id": "G",
                "phase": "final",
                "channel": "speech",
            },
        )

    def _base(self) -> dict[str, Any]:
        return {
            "session_id": "S",
            "response_id": self.response_id,
            "turn_id": "T",
            "playback_generation_id": 1,
        }

    def chunk(self, text: str) -> Event:
        event = emit_event(
            self.conn,
            type="surface.response_chunk",
            payload={
                "turn_id": "T",
                "response_id": self.response_id,
                "sequence": len(self.chunks),
                "text": text,
                "segment_hash": _sha(text),
            },
        )
        self.chunks.append(event)
        return event

    def start(self, *, first_segment: str, source: Event | None = None) -> Event:
        self.started = emit_event(
            self.conn,
            type="surface.playback_started",
            payload={
                **self._base(),
                "phase": "final",
                "channel": "speech",
                "speech_text_hash": _sha(first_segment),
                "incremental": True,
            },
            source_event_id=(source or self.open).event_uid,
        )
        return self.started

    def prepare(self, sequence: int, *, source: Event | None = None) -> None:
        assert self.started is not None
        chunk = source or self.chunks[sequence]
        text = str(chunk.payload.get("text") or chunk.payload["voice_text"])
        if source is not None:
            committed = "".join(str(item.payload["text"]) for item in self.chunks)
            text = str(chunk.payload["voice_text"])[len(committed) :]
        self.prepared.append(text)
        emit_event(
            self.conn,
            type="surface.playback_segment_prepared",
            payload={
                **self._base(),
                "sequence": sequence,
                "speech_text": text,
                "speech_text_hash": _sha(text),
                "segment_hash": _sha(text),
                "source_chunk_event_uid": chunk.event_uid,
            },
            source_event_id=self.started.event_uid,
        )

    def _cursor(self, through: int) -> dict[str, Any]:
        heard = "".join(self.prepared[: through + 1])
        return {
            **self._base(),
            "heard_through_sequence": through,
            "submitted_samples": 160 * (through + 1),
            "heard_text": heard,
            "heard_text_hash": _sha(heard),
            "cursor_quality": "estimated",
        }

    def checkpoint(self, through: int) -> None:
        assert self.started is not None
        emit_event(
            self.conn,
            type="surface.playback_checkpoint",
            payload=self._cursor(through),
            source_event_id=self.started.event_uid,
        )

    def emitted(self, voice_text: str) -> Event:
        return emit_event(
            self.conn,
            type="surface.response_emitted",
            payload={
                "turn_id": "T",
                "text": voice_text,
                "voice_text": voice_text,
                "response_id": self.response_id,
                "phase": "final",
                "channel": "speech",
            },
        )

    def terminal(self, event_type: str, *, through: int) -> None:
        assert self.started is not None
        payload = self._cursor(through)
        if event_type == "surface.playback_completed":
            payload["speech_text_hash"] = _sha("".join(self.prepared))
        else:
            payload["reason"] = "synthetic stop"
        terminalize_playback(
            self.conn,
            event_type=event_type,
            payload=payload,
            source_event_id=self.started.event_uid,
        )


def _spoken(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    history = fold_conversation_history(iter_events(conn))
    record = history.turns[0].responses[0]
    heard = record.spoken_heard.text if record.spoken_heard is not None else None
    return history.consistent and record.consistent, heard


def test_incremental_trail_with_two_checkpoints_and_completion_stays_consistent(
    tmp_path: Path,
) -> None:
    """The activation binds only the first segment; the terminal binds the prepared concat."""
    conn = open_event_log(tmp_path / "incremental.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.start(first_segment=_SEGMENTS[0])
        trail.prepare(0)
        trail.chunk(_SEGMENTS[1])
        trail.prepare(1)
        trail.checkpoint(0)
        trail.chunk(_SEGMENTS[2])
        trail.prepare(2)
        trail.checkpoint(1)
        trail.emitted("".join(_SEGMENTS))
        trail.terminal("surface.playback_completed", through=2)
        assert _spoken(conn) == (True, "".join(_SEGMENTS))
    finally:
        conn.close()


def test_interrupted_incremental_trail_keeps_only_the_played_prefix(tmp_path: Path) -> None:
    """An interrupted incremental lease contributes only its fully played prefix."""
    conn = open_event_log(tmp_path / "interrupted.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.start(first_segment=_SEGMENTS[0])
        trail.prepare(0)
        trail.chunk(_SEGMENTS[1])
        trail.prepare(1)
        trail.checkpoint(0)
        trail.checkpoint(1)
        trail.chunk(_SEGMENTS[2])
        trail.prepare(2)
        trail.terminal("surface.playback_interrupted", through=1)
        assert _spoken(conn) == (True, _SEGMENTS[0] + _SEGMENTS[1])
    finally:
        conn.close()


def test_incremental_completion_hash_must_match_the_prepared_concatenation(
    tmp_path: Path,
) -> None:
    """A completed terminal whose hash is not the prepared concatenation is rejected."""
    conn = open_event_log(tmp_path / "wrong-hash.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.start(first_segment=_SEGMENTS[0])
        trail.prepare(0)
        trail.chunk(_SEGMENTS[1])
        trail.prepare(1)
        trail.emitted(_SEGMENTS[0] + _SEGMENTS[1])
        payload = {**trail._cursor(1), "speech_text_hash": _sha(_SEGMENTS[0])}  # noqa: SLF001
        assert trail.started is not None
        terminalize_playback(
            conn,
            event_type="surface.playback_completed",
            payload=payload,
            source_event_id=trail.started.event_uid,
        )
        assert _spoken(conn) == (False, None)
    finally:
        conn.close()


def test_non_incremental_activation_keeps_exact_full_text_equality(tmp_path: Path) -> None:
    """Without ``incremental`` the activation hash must still equal the full speech hash."""
    conn = open_event_log(tmp_path / "legacy.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.chunk(_SEGMENTS[1])
        surface = trail.emitted(_SEGMENTS[0] + _SEGMENTS[1])
        trail.started = emit_event(
            conn,
            type="surface.playback_started",
            payload={
                **trail._base(),  # noqa: SLF001
                "phase": "final",
                "channel": "speech",
                "speech_text_hash": _sha(_SEGMENTS[0]),
            },
            source_event_id=surface.event_uid,
        )
        trail.prepare(0)
        trail.prepare(1)
        trail.terminal("surface.playback_completed", through=1)
        assert _spoken(conn) == (False, None)
    finally:
        conn.close()


def test_voice_suffix_beyond_the_committed_chunks_is_one_more_prepared_segment(
    tmp_path: Path,
) -> None:
    """The emitted row's unspoken voice suffix validates as a segment it sourced."""
    conn = open_event_log(tmp_path / "suffix.db")
    try:
        trail = _Trail(conn)
        trail.chunk(_SEGMENTS[0])
        trail.start(first_segment=_SEGMENTS[0])
        trail.prepare(0)
        trail.checkpoint(0)
        surface = trail.emitted(_SEGMENTS[0] + "这是没有被门放行的长尾巴。")
        trail.prepare(1, source=surface)
        assert trail.prepared[1] == "这是没有被门放行的长尾巴。"
        trail.terminal("surface.playback_completed", through=1)
        assert _spoken(conn) == (True, _SEGMENTS[0] + "这是没有被门放行的长尾巴。")
    finally:
        conn.close()
