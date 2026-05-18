"""Unit tests for L5 surface adapter (``jarvis.surface.cli``).

Covers:
- ``parse_response_channels`` byte-equivalence with the legacy parser
  across the canonical input shapes.
- ``emit_utterance_received`` writes a well-formed ``utterance.received``
  row with the expected payload + correlation.
- ``write_output`` renders the document side of a channel-split
  response and refuses on a missing / mismatched Pre-emit token.
"""

from __future__ import annotations

import io
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from jarvis.state.event_log import open_event_log
from jarvis.surface.cli import (
    PreEmitTokenError,
    ResponseChannels,
    SurfaceState,
    emit_utterance_received,
    parse_response_channels,
    record_pre_emit_token,
    write_output,
)

if TYPE_CHECKING:
    from pathlib import Path


# --- Local ResponsePlan double (mirrors `jarvis.decision.ResponsePlan` shape) ---


@dataclass(frozen=True)
class _PlanStub:
    """Minimal duck-typed stand-in for ``jarvis.decision.ResponsePlan``."""

    text: str
    response_hash: str


# --- parse_response_channels -------------------------------------------------


def test_parse_bare_text_returns_both_channels_equal() -> None:
    """Non-channelized text fills voice AND document with the stripped value."""
    result = parse_response_channels("  hello world  ")
    assert isinstance(result, ResponseChannels)
    assert result.has_channels is False
    assert result.voice == "hello world"
    assert result.document == "hello world"
    assert result.raw == "  hello world  "


def test_parse_only_voice_returns_empty_document() -> None:
    """Only ``<voice>`` present -> document is empty string."""
    result = parse_response_channels("prelude <voice>语音</voice> tail")
    assert result.has_channels is True
    assert result.voice == "语音"
    assert result.document == ""


def test_parse_only_document_returns_empty_voice() -> None:
    """Only ``<document>`` present -> voice is empty string."""
    result = parse_response_channels("<document>doc body</document>")
    assert result.has_channels is True
    assert result.voice == ""
    assert result.document == "doc body"


def test_parse_both_channels() -> None:
    """Both ``<voice>`` and ``<document>`` parsed independently."""
    text = "<voice>spoken</voice>\n<document>written</document>"
    result = parse_response_channels(text)
    assert result.has_channels is True
    assert result.voice == "spoken"
    assert result.document == "written"


def test_parse_channels_case_insensitive_and_multiline() -> None:
    """Tags match case-insensitive; bodies span newlines (DOTALL)."""
    text = "<VOICE>line1\nline2</VOICE><Document>doc\nlines</Document>"
    result = parse_response_channels(text)
    assert result.voice == "line1\nline2"
    assert result.document == "doc\nlines"
    assert result.has_channels is True


def test_parse_first_occurrence_wins() -> None:
    """Repeat tags: only the FIRST occurrence is recorded per channel."""
    text = "<voice>first</voice><voice>second</voice>"
    result = parse_response_channels(text)
    assert result.voice == "first"


def test_parse_empty_string() -> None:
    """Empty string -> empty channels, stripped values equal."""
    result = parse_response_channels("")
    assert result.has_channels is False
    assert result.voice == ""
    assert result.document == ""


def test_parse_malformed_channels_fall_through_to_raw() -> None:
    """Unmatched / malformed tags do not match the regex -> bare-text path."""
    text = "<voice>missing close"
    result = parse_response_channels(text)
    assert result.has_channels is False
    # Bare-text path strips the raw input.
    assert result.voice == text.strip()
    assert result.document == text.strip()


# --- emit_utterance_received -------------------------------------------------


def test_emit_utterance_received_writes_row(tmp_path: Path) -> None:
    """The L5 emission lands as one ``utterance.received`` row with payload + correlation."""
    db_path = tmp_path / "events.db"
    with closing(open_event_log(db_path)) as conn:
        event = emit_utterance_received(
            conn,
            transcript="今晚去吃点啥",
            turn_id="T12345678",
        )
        assert event.type == "utterance.received"
        assert event.payload["transcript"] == "今晚去吃点啥"
        assert event.payload["turn_id"] == "T12345678"
        assert event.payload["channel"] == "cli_stdin"
        assert event.payload["language"] == "zh-CN"
        assert event.correlation is not None
        assert event.correlation["turn_id"] == "T12345678"

        # Confirm it's queryable.
        cursor = conn.execute("SELECT type, schema_version FROM events ORDER BY id ASC")
        rows = cursor.fetchall()
        assert rows == [("utterance.received", 1)]


def test_emit_utterance_received_custom_channel_language(tmp_path: Path) -> None:
    """Channel + language flags flow through into the payload."""
    db_path = tmp_path / "events.db"
    with closing(open_event_log(db_path)) as conn:
        event = emit_utterance_received(
            conn,
            transcript="hi",
            turn_id="T11111111",
            channel="test_stub",
            language="en-US",
        )
    assert event.payload["channel"] == "test_stub"
    assert event.payload["language"] == "en-US"


# --- record_pre_emit_token ---------------------------------------------------


def test_record_pre_emit_token_sets_field() -> None:
    """Recording replaces whatever token was on the surface state."""
    empty = SurfaceState(last_gate_response_hash=None)
    primed = record_pre_emit_token(empty, "abc123")
    assert primed.last_gate_response_hash == "abc123"
    # Re-recording supersedes.
    refreshed = record_pre_emit_token(primed, "def456")
    assert refreshed.last_gate_response_hash == "def456"


# --- write_output ------------------------------------------------------------


def test_write_output_renders_document_side() -> None:
    """A channelized plan writes the document body to the stream."""
    plan = _PlanStub(
        text="<voice>spoken</voice>\n<document>written</document>",
        response_hash="HASH",
    )
    state = SurfaceState(last_gate_response_hash="HASH")
    buf = io.StringIO()

    after = write_output(state, plan, stream=buf)

    assert "written" in buf.getvalue()
    assert "spoken" not in buf.getvalue()
    assert buf.getvalue().endswith("\n")
    # Token consumed.
    assert after.last_gate_response_hash is None


def test_write_output_falls_back_to_voice_when_document_empty() -> None:
    """When document channel is empty, voice content is written."""
    plan = _PlanStub(text="<voice>语音回复</voice>", response_hash="HASH")
    state = SurfaceState(last_gate_response_hash="HASH")
    buf = io.StringIO()

    write_output(state, plan, stream=buf)

    assert "语音回复" in buf.getvalue()


def test_write_output_renders_bare_text() -> None:
    """Non-channelized response is written verbatim (post-strip)."""
    plan = _PlanStub(text="bare body", response_hash="HASH")
    state = SurfaceState(last_gate_response_hash="HASH")
    buf = io.StringIO()

    write_output(state, plan, stream=buf)

    assert buf.getvalue() == "bare body\n"


def test_write_output_raises_on_token_mismatch() -> None:
    """A stale token must trip the runtime check (canary H3)."""
    plan = _PlanStub(text="body", response_hash="NEW_HASH")
    state = SurfaceState(last_gate_response_hash="OLD_HASH")
    buf = io.StringIO()

    with pytest.raises(PreEmitTokenError):
        write_output(state, plan, stream=buf)


def test_write_output_raises_when_no_token_recorded() -> None:
    """A never-recorded token must trip the runtime check."""
    plan = _PlanStub(text="body", response_hash="HASH")
    state = SurfaceState(last_gate_response_hash=None)
    buf = io.StringIO()

    with pytest.raises(PreEmitTokenError):
        write_output(state, plan, stream=buf)
