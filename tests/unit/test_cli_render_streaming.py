"""Unit tests for ``render_response`` streaming_enabled / query kwargs.

Build 3 of ADR-0003 Step 2 (Inherent text). The renderer gains two new
keyword-only parameters that, when activated by the daemon path, emit
the 3-event Inherent taxonomy registered in Build 2:

    surface.response_open -> surface.response_chunk * N -> surface.response_emitted

in that order, all carrying ``correlation={"turn_id": turn_id}``. The
default (``streaming_enabled=False``) preserves Step-1 single-emit
semantics byte-for-byte — the CLI path still emits only the audit
``surface.response_emitted`` row.

Coverage:

1. Default behaviour preserved (no streaming events fire when
   ``streaming_enabled=False``) — both ``required_gate_mode="sentence"``
   and ``required_gate_mode="full_text"``.
2. ``streaming_enabled=True`` + ``required_gate_mode="sentence"`` ->
   one open + N chunks (split per :func:`split_into_sentences`) + one
   emitted, in that order.
3. ``streaming_enabled=True`` + ``required_gate_mode="full_text"`` ->
   one open + ONE chunk carrying the full text + one emitted.
4. ``streaming_enabled=True`` + ``required_gate_mode="structured"`` ->
   same single-chunk behaviour as ``full_text``.
5. ``streaming_enabled=True`` + sentence mode + no-punctuation text ->
   one chunk with the stripped full text (splitter ``[text.strip()]``
   no-boundary fallback).
6. ``streaming_enabled=True`` + ``query=""`` -> open still emits with
   empty-string query (the kwarg is allowed to be empty, not skipped).
7. Pre-emit token error fires BEFORE any streaming event lands — no
   partial open / chunk / emitted rows on a gate failure.
"""

# ruff: noqa: RUF001 — CJK punctuation literals are part of the splitter contract.

from __future__ import annotations

import hashlib
import io
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from jarvis.state.event_log import iter_events, open_event_log
from jarvis.surface import notify as nf
from jarvis.surface.cli import PreEmitTokenError, SurfaceState
from jarvis.surface.cli_render import render_response

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


# --- Helpers ---------------------------------------------------------------


@dataclass(frozen=True)
class _PlanStub:
    """Minimal duck-typed stand-in for :class:`jarvis.decision.ResponsePlan`.

    Carries the three fields the ``ResponsePlanLike`` Protocol consumes
    (``text`` + ``response_hash`` + ``required_gate_mode``).
    """

    text: str
    response_hash: str
    required_gate_mode: str


def _make_plan(text: str, required_gate_mode: str = "sentence") -> _PlanStub:
    return _PlanStub(
        text=text,
        response_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        required_gate_mode=required_gate_mode,
    )


def _primed_state(plan: _PlanStub) -> SurfaceState:
    """Prime a :class:`SurfaceState` with the plan's hash (Pre-emit token)."""
    return SurfaceState(last_gate_response_hash=plan.response_hash)


@pytest.fixture
def event_log_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a fresh event-log SQLite connection scoped to the test."""
    db_path = tmp_path / "events.db"
    with closing(open_event_log(db_path)) as conn:
        yield conn


@pytest.fixture
def mocked_notify() -> Iterator[tuple[object, object]]:
    """Patch subprocess primitives so no macOS surface fires for real."""
    with (
        patch.object(nf.subprocess, "Popen") as popen_mock,
        patch.object(nf.subprocess, "run") as run_mock,
    ):
        yield popen_mock, run_mock


# --- 1. Default behaviour preserved (streaming_enabled=False) ---------------


@pytest.mark.parametrize("gate_mode", ["sentence", "full_text"])
def test_streaming_disabled_emits_only_response_emitted(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[object, object],  # noqa: ARG001 — fixture installs the patches
    gate_mode: str,
) -> None:
    """Default ``streaming_enabled=False`` MUST preserve Step-1 single-emit semantics.

    For BOTH ``required_gate_mode="sentence"`` and ``"full_text"``, the
    only event row in the log MUST be ``surface.response_emitted``. No
    ``surface.response_open`` or ``surface.response_chunk`` rows land.
    """
    plan = _make_plan("你好。世界。", required_gate_mode=gate_mode)
    state = _primed_state(plan)

    _, event = render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T_default",
        attention_channel="voice_notify",
        stream=io.StringIO(),
        available_surfaces=frozenset(),
    )

    assert event.type == "surface.response_emitted"

    rows = list(iter_events(event_log_conn))
    assert [row.type for row in rows] == ["surface.response_emitted"]


# --- 2. streaming_enabled + sentence mode -----------------------------------


def test_streaming_sentence_mode_emits_open_two_chunks_then_emitted(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[object, object],  # noqa: ARG001 — fixture installs the patches
) -> None:
    """``streaming_enabled=True`` + sentence mode MUST split into N chunks.

    Text ``"你好。世界。"`` splits to 2 sentences, so the log row order MUST
    be: open -> chunk("你好。") -> chunk("世界。") -> emitted. All four
    rows carry ``correlation={"turn_id": "T_stream_sent"}``. Open
    payload carries ``query="hi"`` + ``kind="text"``; each chunk
    payload carries ``{turn_id, text}``.
    """
    plan = _make_plan("你好。世界。", required_gate_mode="sentence")
    state = _primed_state(plan)

    render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T_stream_sent",
        attention_channel="voice_notify",
        stream=io.StringIO(),
        available_surfaces=frozenset(),
        streaming_enabled=True,
        query="hi",
    )

    rows = list(iter_events(event_log_conn))
    assert [row.type for row in rows] == [
        "surface.response_open",
        "surface.response_chunk",
        "surface.response_chunk",
        "surface.response_emitted",
    ]

    # Open payload + correlation.
    open_row = rows[0]
    assert open_row.payload == {
        "turn_id": "T_stream_sent",
        "query": "hi",
        "kind": "text",
        "required_gate_mode": "sentence",
    }
    assert open_row.correlation == {"turn_id": "T_stream_sent"}

    # Chunk payloads match the splitter output in order.
    assert rows[1].payload == {"turn_id": "T_stream_sent", "text": "你好。"}
    assert rows[1].correlation == {"turn_id": "T_stream_sent"}
    assert rows[2].payload == {"turn_id": "T_stream_sent", "text": "世界。"}
    assert rows[2].correlation == {"turn_id": "T_stream_sent"}

    # Audit event still emits last with the matching correlation.
    assert rows[3].correlation == {"turn_id": "T_stream_sent"}


# --- 3. streaming_enabled + full_text mode ----------------------------------


def test_streaming_full_text_mode_emits_open_single_chunk_then_emitted(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[object, object],  # noqa: ARG001 — fixture installs the patches
) -> None:
    """``full_text`` mode MUST emit one chunk carrying the FULL text (no split)."""
    plan = _make_plan("完整文本，未分句。", required_gate_mode="full_text")
    state = _primed_state(plan)

    render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T_full",
        attention_channel="voice_notify",
        stream=io.StringIO(),
        available_surfaces=frozenset(),
        streaming_enabled=True,
        query="q",
    )

    rows = list(iter_events(event_log_conn))
    assert [row.type for row in rows] == [
        "surface.response_open",
        "surface.response_chunk",
        "surface.response_emitted",
    ]
    assert rows[1].payload == {"turn_id": "T_full", "text": "完整文本，未分句。"}


# --- 4. streaming_enabled + structured mode ---------------------------------


def test_streaming_structured_mode_emits_open_single_chunk_then_emitted(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[object, object],  # noqa: ARG001 — fixture installs the patches
) -> None:
    """``structured`` mode MUST behave like ``full_text`` — a single chunk."""
    plan = _make_plan("结构化输出。一二三。", required_gate_mode="structured")
    state = _primed_state(plan)

    render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T_struct",
        attention_channel="voice_notify",
        stream=io.StringIO(),
        available_surfaces=frozenset(),
        streaming_enabled=True,
        query="q",
    )

    rows = list(iter_events(event_log_conn))
    assert [row.type for row in rows] == [
        "surface.response_open",
        "surface.response_chunk",
        "surface.response_emitted",
    ]
    # No speculative split: the chunk text is the FULL plan text.
    assert rows[1].payload == {"turn_id": "T_struct", "text": "结构化输出。一二三。"}


# --- 5. streaming_enabled + sentence mode + no-punctuation text --------------


def test_streaming_sentence_mode_no_punctuation_emits_single_chunk(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[object, object],  # noqa: ARG001 — fixture installs the patches
) -> None:
    """No-boundary text under sentence mode MUST yield one chunk (splitter fallback).

    ``split_into_sentences`` returns ``[text.strip()]`` for input that
    contains no boundary punctuation; the renderer must therefore emit
    a single chunk with that stripped text.
    """
    plan = _make_plan("hello world", required_gate_mode="sentence")
    state = _primed_state(plan)

    render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T_nopunc",
        attention_channel="voice_notify",
        stream=io.StringIO(),
        available_surfaces=frozenset(),
        streaming_enabled=True,
        query="hi",
    )

    rows = list(iter_events(event_log_conn))
    assert [row.type for row in rows] == [
        "surface.response_open",
        "surface.response_chunk",
        "surface.response_emitted",
    ]
    assert rows[1].payload == {"turn_id": "T_nopunc", "text": "hello world"}


# --- 6. streaming_enabled + empty query -------------------------------------


def test_streaming_with_empty_query_still_emits_open(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[object, object],  # noqa: ARG001 — fixture installs the patches
) -> None:
    """``query=""`` MUST still produce an open event with empty-string query.

    Empty query is allowed (e.g. timer-triggered turns with no
    transcript); the kwarg is not gated, only the streaming_enabled
    flag is.
    """
    plan = _make_plan("Done.", required_gate_mode="sentence")
    state = _primed_state(plan)

    render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T_empty_query",
        attention_channel="voice_notify",
        stream=io.StringIO(),
        available_surfaces=frozenset(),
        streaming_enabled=True,
        query="",
    )

    rows = list(iter_events(event_log_conn))
    open_rows = [row for row in rows if row.type == "surface.response_open"]
    assert len(open_rows) == 1
    assert open_rows[0].payload == {
        "turn_id": "T_empty_query",
        "query": "",
        "kind": "text",
        "required_gate_mode": "sentence",
    }


# --- 7. Pre-emit token error fires before any streaming event ---------------


def test_pre_emit_token_mismatch_raises_before_any_event_emit(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[object, object],  # noqa: ARG001 — fixture installs the patches
) -> None:
    """Token check MUST fire BEFORE any open/chunk/emitted row is appended.

    A mismatched ``SurfaceState`` token raises :class:`PreEmitTokenError`
    at the top of ``render_response``. The Event Log MUST remain empty —
    no partial Inherent rows.
    """
    plan = _make_plan("Hello.", required_gate_mode="sentence")
    # Wrong token deliberately.
    state = SurfaceState(last_gate_response_hash="deadbeef")

    with pytest.raises(PreEmitTokenError):
        render_response(
            state,
            plan,
            conn=event_log_conn,
            turn_id="T_bad_token",
            attention_channel="voice_notify",
            stream=io.StringIO(),
            available_surfaces=frozenset(),
            streaming_enabled=True,
            query="hi",
        )

    rows = list(iter_events(event_log_conn))
    assert rows == []
