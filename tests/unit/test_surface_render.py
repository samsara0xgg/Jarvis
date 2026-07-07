"""Unit tests for ``jarvis.surface.cli_render`` (Step 18 of ADR-0002).

The renderer routes one approved :class:`ResponsePlan` across the
physical surfaces declared for the L3 Attention Policy channel, and
emits exactly one ``surface.response_emitted`` event recording what was
actually delivered (``delivered_via``) plus the logical L3 channel
(``attention_channel``).

All subprocess invocations are mocked — we never actually fire ``say``
or ``osascript`` from the test suite.
"""

from __future__ import annotations

import hashlib
import io
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from jarvis.state.event_log import iter_events, open_event_log
from jarvis.surface.cli import PreEmitTokenError, SurfaceState
from jarvis.surface.cli_render import render_response

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


# --- Helpers ---------------------------------------------------------------


@dataclass(frozen=True)
class _PlanStub:
    """Minimal duck-typed stand-in for :class:`jarvis.decision.ResponsePlan`."""

    text: str
    response_hash: str
    # Streaming is off in every test here, so the mode is never read;
    # the field exists to satisfy the ResponsePlanLike Protocol.
    required_gate_mode: str = "full_text"


def _make_plan(text: str) -> _PlanStub:
    return _PlanStub(text=text, response_hash=hashlib.sha256(text.encode()).hexdigest())


@pytest.fixture
def event_log_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a fresh event-log SQLite connection scoped to the test."""
    db_path = tmp_path / "events.db"
    with closing(open_event_log(db_path)) as conn:
        yield conn


@pytest.fixture
def mocked_notify() -> Iterator[tuple[MagicMock, MagicMock]]:
    """Patch both subprocess primitives the notify helpers spawn.

    Yields ``(popen_mock, run_mock)`` so per-channel assertions can
    inspect call counts + argv.
    """
    with (
        patch("jarvis.surface.notify.subprocess.Popen", new_callable=MagicMock) as popen_mock,
        patch("jarvis.surface.notify.subprocess.run", new_callable=MagicMock) as run_mock,
    ):
        yield popen_mock, run_mock


# --- Pre-emit token enforcement (canary H3 contract) -----------------------


@pytest.mark.usefixtures("mocked_notify")
def test_render_response_raises_when_no_token_recorded(
    event_log_conn: sqlite3.Connection,
) -> None:
    """Empty surface state -> :class:`PreEmitTokenError` (Pre-emit H3 contract)."""
    plan = _make_plan("hi")
    state = SurfaceState(last_gate_response_hash=None)
    with pytest.raises(PreEmitTokenError):
        render_response(
            state,
            plan,
            conn=event_log_conn,
            turn_id="T00000001",
            attention_channel="voice_notify",
        )


@pytest.mark.usefixtures("mocked_notify")
def test_render_response_raises_on_token_mismatch(
    event_log_conn: sqlite3.Connection,
) -> None:
    """A stale token must trip the runtime check, just like ``write_output``."""
    plan = _make_plan("hi")
    state = SurfaceState(last_gate_response_hash="stale")
    with pytest.raises(PreEmitTokenError):
        render_response(
            state,
            plan,
            conn=event_log_conn,
            turn_id="T00000002",
            attention_channel="voice_notify",
        )


# --- Per-channel dispatch --------------------------------------------------


@pytest.mark.usefixtures("mocked_notify")
@pytest.mark.parametrize(
    ("channel", "expected_surfaces"),
    [
        ("silent_log",     []),
        ("queue_review",   ["stdout"]),
        ("badge_card",     ["banner"]),
        ("soft_suggest",   ["stdout"]),
        ("voice_notify",   ["voice", "banner", "stdout"]),
        ("interrupt_now",  ["voice", "banner"]),
        ("ask_confirm",    ["banner", "stdout"]),
        ("delegate_agent", []),
        ("suppress",       []),
    ],
)
def test_render_per_channel_delivered_via_matches_table(
    channel: str,
    expected_surfaces: list[str],
    event_log_conn: sqlite3.Connection,
) -> None:
    """For each of the 9 channels, ``delivered_via`` matches the ADR mapping."""
    plan = _make_plan("hello world")
    state = SurfaceState(last_gate_response_hash=plan.response_hash)

    _, event = render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T1",
        attention_channel=channel,
        stream=io.StringIO(),
    )

    assert event.type == "surface.response_emitted"
    assert event.payload["delivered_via"] == expected_surfaces
    assert event.payload["attention_channel"] == channel


def test_render_voice_notify_fires_say_and_banner_and_writes_stdout(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[MagicMock, MagicMock],
) -> None:
    """Flagship channel: say + osascript banner + stdout write all happen."""
    popen_mock, run_mock = mocked_notify
    plan = _make_plan("<voice>spoken</voice><document>written</document>")
    state = SurfaceState(last_gate_response_hash=plan.response_hash)
    buf = io.StringIO()

    render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T2",
        attention_channel="voice_notify",
        stream=buf,
    )

    # ``say`` spawned via Popen with the voice text.
    popen_mock.assert_called_once()
    say_argv = popen_mock.call_args[0][0]
    assert say_argv[0] == "say"
    assert say_argv[-1] == "spoken"

    # ``osascript`` invoked via subprocess.run with the document text in the banner.
    run_mock.assert_called_once()
    script = run_mock.call_args[0][0][2]
    assert '"written"' in script
    assert 'with title "Jarvis"' in script

    # Stdout received the document body with a trailing newline.
    assert buf.getvalue() == "written\n"


def test_render_interrupt_now_prepends_bell_to_voice_text(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[MagicMock, MagicMock],
) -> None:
    """``interrupt_now`` uses the same TTS voice but prepends a BEL marker."""
    popen_mock, _ = mocked_notify
    plan = _make_plan("<voice>urgent</voice><document>doc</document>")
    state = SurfaceState(last_gate_response_hash=plan.response_hash)

    render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T3",
        attention_channel="interrupt_now",
        stream=io.StringIO(),
    )

    say_argv = popen_mock.call_args[0][0]
    assert say_argv[-1].startswith("\a")
    assert say_argv[-1].endswith("urgent")


def test_render_badge_card_uses_title_only_variant(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[MagicMock, MagicMock],
) -> None:
    """``badge_card`` calls ``deliver_banner`` with document_text as the title and empty body."""
    _, run_mock = mocked_notify
    plan = _make_plan("<document>BADGE-LABEL</document>")
    state = SurfaceState(last_gate_response_hash=plan.response_hash)

    render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T4",
        attention_channel="badge_card",
        stream=io.StringIO(),
    )

    script = run_mock.call_args[0][0][2]
    assert 'with title "BADGE-LABEL"' in script
    # Body is empty (deliver_banner still fires when the title is non-empty).
    assert 'display notification ""' in script


# --- Partial delivery on detached parent -----------------------------------


@pytest.mark.usefixtures("mocked_notify")
def test_render_omits_stdout_when_default_sys_stdout_not_tty(
    event_log_conn: sqlite3.Connection,
) -> None:
    """When the parent has detached and sys.stdout is not a TTY, drop ``stdout``."""
    plan = _make_plan("hi")
    state = SurfaceState(last_gate_response_hash=plan.response_hash)

    with patch("sys.stdout.isatty", return_value=False):
        _, event = render_response(
            state,
            plan,
            conn=event_log_conn,
            turn_id="T5",
            attention_channel="voice_notify",
            # stream=None triggers the TTY check against sys.stdout.
        )

    assert "stdout" not in event.payload["delivered_via"]
    assert event.payload["delivered_via"] == ["voice", "banner"]


@pytest.mark.usefixtures("mocked_notify")
def test_render_keeps_stdout_when_explicit_stream_given(
    event_log_conn: sqlite3.Connection,
) -> None:
    """An explicit stream (StringIO / captured pipe) is always considered attached."""
    plan = _make_plan("hi")
    state = SurfaceState(last_gate_response_hash=plan.response_hash)

    # sys.stdout could be anything; explicit stream wins.
    with patch("sys.stdout.isatty", return_value=False):
        _, event = render_response(
            state,
            plan,
            conn=event_log_conn,
            turn_id="T6",
            attention_channel="voice_notify",
            stream=io.StringIO(),
        )

    assert "stdout" in event.payload["delivered_via"]


# --- Channel-split parsing -------------------------------------------------


@pytest.mark.usefixtures("mocked_notify")
def test_render_emits_voice_and_document_text_payload_fields(
    event_log_conn: sqlite3.Connection,
) -> None:
    """``voice_text`` and ``document_text`` are populated from the channel parser."""
    plan = _make_plan("<voice>语音</voice><document>书面</document>")
    state = SurfaceState(last_gate_response_hash=plan.response_hash)

    _, event = render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T7",
        attention_channel="voice_notify",
        stream=io.StringIO(),
    )

    assert event.payload["voice_text"] == "语音"
    assert event.payload["document_text"] == "书面"
    assert event.payload["text"] == plan.text


@pytest.mark.usefixtures("mocked_notify")
def test_render_mirrors_bare_text_to_both_channel_payloads(
    event_log_conn: sqlite3.Connection,
) -> None:
    """Non-channelized text is mirrored to both voice_text + document_text."""
    plan = _make_plan("  hello world  ")
    state = SurfaceState(last_gate_response_hash=plan.response_hash)

    _, event = render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T8",
        attention_channel="voice_notify",
        stream=io.StringIO(),
    )

    assert event.payload["voice_text"] == "hello world"
    assert event.payload["document_text"] == "hello world"


# --- Audit event shape -----------------------------------------------------


@pytest.mark.usefixtures("mocked_notify")
def test_render_emits_exactly_one_surface_response_emitted_event(
    event_log_conn: sqlite3.Connection,
) -> None:
    """Each ``render_response`` call appends exactly one event to the log."""
    plan = _make_plan("hi")
    state = SurfaceState(last_gate_response_hash=plan.response_hash)

    render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T9",
        attention_channel="voice_notify",
        stream=io.StringIO(),
    )

    rows = list(iter_events(event_log_conn))
    assert len(rows) == 1
    assert rows[0].type == "surface.response_emitted"
    assert rows[0].payload["response_hash"] == plan.response_hash
    assert rows[0].correlation is not None
    assert rows[0].correlation["turn_id"] == "T9"


@pytest.mark.usefixtures("mocked_notify")
def test_render_clears_token_on_returned_state(
    event_log_conn: sqlite3.Connection,
) -> None:
    """The returned :class:`SurfaceState` has ``last_gate_response_hash=None``."""
    plan = _make_plan("hi")
    state = SurfaceState(last_gate_response_hash=plan.response_hash)

    next_state, _ = render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T10",
        attention_channel="voice_notify",
        stream=io.StringIO(),
    )

    assert next_state.last_gate_response_hash is None


# --- Unknown-channel fallback ----------------------------------------------


def test_render_unknown_channel_falls_back_to_silent_log(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[MagicMock, MagicMock],
) -> None:
    """An unknown channel name yields no surface calls and ``delivered_via=[]``.

    The unknown label still lands on the event so an auditor can spot the
    L3 anomaly.
    """
    popen_mock, run_mock = mocked_notify
    plan = _make_plan("hi")
    state = SurfaceState(last_gate_response_hash=plan.response_hash)

    _, event = render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T11",
        attention_channel="not_a_real_channel",
        stream=io.StringIO(),
    )

    popen_mock.assert_not_called()
    run_mock.assert_not_called()
    assert event.payload["delivered_via"] == []
    assert event.payload["attention_channel"] == "not_a_real_channel"
