"""Unit tests for the ``available_surfaces`` filter on ``render_response``.

Step 4 of ADR-0003 (Inherent Text Surface). The renderer gains a
keyword-only ``available_surfaces: frozenset[str] | None`` parameter
that, when set to a non-``None`` frozenset, gates the per-surface
dispatch loop: any surface ID NOT in the set is skipped silently.
``None`` resolves to the default 5-surface CLI set so Day-1 / Day-2
behaviour is preserved when no caller specifies the filter.

Why this exists: ADR-0003 Step 6 introduces an Inherent broadcast
channel (WebSocket push) as the daemon's sole observable physical
delivery; the daemon caller passes ``frozenset()`` to suppress every
physical macOS surface (say, banner, stdout) while keeping the audit
``surface.response_emitted`` event flowing (the L5 invariant
§3.6.4 — "L5 selects within channel" — the channel pick is still
recorded; only the physical-surface fire is gated).

Coverage:

1. Default behaviour preserved when ``available_surfaces`` is omitted
   (the channel mapping fires every surface in the tuple).
2. ``frozenset()`` skips every physical surface (no Popen, no
   subprocess.run, empty stream) but the audit event STILL lands with
   ``delivered_via=[]``.
3. Partial sets fire only the listed surfaces.
4. Unknown channel + ``frozenset()`` is safe; the unknown label still
   lands on the audit event's ``attention_channel`` (the dispatch loop
   has zero iterations since ``silent_log`` has no surfaces, so the
   filter never runs — the audit anomaly trace is what is tested).
5. The filter is per-surface, not per-channel: passing only one
   surface ID from a multi-surface channel fires exactly that one.
6. ``drive_turn`` forwards ``available_surfaces`` end-to-end to
   ``render_response``.
"""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from jarvis.decision import DecideResult
from jarvis.decision.gates import ResponsePlan
from jarvis.runtime import (
    JarvisRuntime,
    bootstrap_runtime_app,
    drive_turn,
)
from jarvis.state.event_log import iter_events, open_event_log
from jarvis.surface.cli import SurfaceState, emit_surface_user_intent
from jarvis.surface.cli_render import render_response

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

    from jarvis.shared import Event


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "jarvis.yaml"
_PROMPT_PATH = _REPO_ROOT / "prompts" / "jarvis_v1.md"

_STUB_RESPONSE_TEXT: str = (
    "available_surfaces unit-test response — status pending."
)


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


def _make_plan(text: str) -> _PlanStub:
    return _PlanStub(
        text=text,
        response_hash=hashlib.sha256(text.encode()).hexdigest(),
        required_gate_mode="sentence",
    )


@pytest.fixture
def event_log_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a fresh event-log SQLite connection scoped to the test."""
    db_path = tmp_path / "events.db"
    with closing(open_event_log(db_path)) as conn:
        yield conn


@pytest.fixture
def mocked_notify() -> Iterator[tuple[MagicMock, MagicMock]]:
    """Patch both subprocess primitives the notify helpers spawn.

    Yields ``(popen_mock, run_mock)`` so per-surface assertions can
    inspect call counts. Matches the pattern used in
    ``tests/unit/test_surface_render.py``.
    """
    with (
        patch("jarvis.surface.notify.subprocess.Popen") as popen_mock,
        patch("jarvis.surface.notify.subprocess.run") as run_mock,
    ):
        yield popen_mock, run_mock


# --- 1. Default behaviour preserved ----------------------------------------


def test_default_available_surfaces_fires_all_three_physical_surfaces(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[MagicMock, MagicMock],
) -> None:
    """Omitting ``available_surfaces`` MUST preserve Day-1 multi-surface dispatch.

    The flagship ``voice_notify`` channel maps to (say, osascript_banner,
    cli_stdout); ``delivered_via`` MUST list all three physical surfaces.
    """
    popen_mock, run_mock = mocked_notify
    plan = _make_plan("<voice>spoken</voice><document>written</document>")
    state = _primed_state(plan)
    buf = io.StringIO()

    _, event = render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T_default",
        attention_channel="voice_notify",
        stream=buf,
        # no available_surfaces kwarg -> CLI default applies
    )

    assert event.payload["delivered_via"] == ["voice", "banner", "stdout"]
    popen_mock.assert_called_once()
    run_mock.assert_called_once()
    assert buf.getvalue() == "written\n"


# --- 2. Empty frozenset suppresses all physical surfaces -------------------


def test_empty_frozenset_skips_all_physical_surfaces_but_emits_audit_event(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[MagicMock, MagicMock],
) -> None:
    """``frozenset()`` MUST suppress every physical surface.

    Critical invariant for the ADR-0003 daemon (Step 8): no macOS
    side-effect, but the audit ``surface.response_emitted`` event STILL
    lands so the L5 boundary trace is intact.
    """
    popen_mock, run_mock = mocked_notify
    plan = _make_plan("<voice>spoken</voice><document>written</document>")
    state = _primed_state(plan)
    buf = io.StringIO()

    _, event = render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T_empty",
        attention_channel="voice_notify",
        stream=buf,
        available_surfaces=frozenset(),
    )

    # No physical surface fired.
    popen_mock.assert_not_called()
    run_mock.assert_not_called()
    assert buf.getvalue() == ""

    # Audit event STILL emitted with empty delivered_via and original
    # logical channel preserved.
    assert event.type == "surface.response_emitted"
    assert event.payload["delivered_via"] == []
    assert event.payload["attention_channel"] == "voice_notify"
    assert event.payload["response_hash"] == plan.response_hash

    # The event row is in the log.
    rows = list(iter_events(event_log_conn))
    assert len(rows) == 1
    assert rows[0].type == "surface.response_emitted"
    assert rows[0].payload["delivered_via"] == []


# --- 3. Partial sets fire only the listed surfaces -------------------------


def test_partial_set_with_only_cli_stdout_fires_only_stdout(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[MagicMock, MagicMock],
) -> None:
    """``frozenset({"cli_stdout"})`` on ``voice_notify`` MUST fire only stdout."""
    popen_mock, run_mock = mocked_notify
    plan = _make_plan("<voice>spoken</voice><document>written</document>")
    state = _primed_state(plan)
    buf = io.StringIO()

    _, event = render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T_stdout_only",
        attention_channel="voice_notify",
        stream=buf,
        available_surfaces=frozenset({"cli_stdout"}),
    )

    popen_mock.assert_not_called()
    run_mock.assert_not_called()
    assert buf.getvalue() == "written\n"
    assert event.payload["delivered_via"] == ["stdout"]


def test_partial_set_with_only_say_fires_only_voice(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[MagicMock, MagicMock],
) -> None:
    """``frozenset({"say"})`` on ``voice_notify`` MUST fire only voice."""
    popen_mock, run_mock = mocked_notify
    plan = _make_plan("<voice>spoken</voice><document>written</document>")
    state = _primed_state(plan)
    buf = io.StringIO()

    _, event = render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T_voice_only",
        attention_channel="voice_notify",
        stream=buf,
        available_surfaces=frozenset({"say"}),
    )

    popen_mock.assert_called_once()
    run_mock.assert_not_called()  # banner skipped
    assert buf.getvalue() == ""  # stdout skipped
    assert event.payload["delivered_via"] == ["voice"]


# --- 4. Unknown channel + frozenset() audits the anomaly --------------------


def test_unknown_channel_with_empty_frozenset_emits_audit_with_anomaly_label(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[MagicMock, MagicMock],
) -> None:
    """Unknown ``attention_channel`` + ``frozenset()`` is safe and audited.

    The unknown channel falls back to ``silent_log`` (which maps to no
    surfaces), so the dispatch loop has zero iterations and the filter
    never runs. The real claim verified here is that the audit event
    STILL records the L3 anomaly via the unknown channel label, even
    though zero physical surfaces fired.
    """
    popen_mock, run_mock = mocked_notify
    plan = _make_plan("hi")
    state = _primed_state(plan)

    _, event = render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T_silent_filter",
        attention_channel="not_a_real_channel",
        stream=io.StringIO(),
        available_surfaces=frozenset(),
    )

    popen_mock.assert_not_called()
    run_mock.assert_not_called()
    assert event.payload["delivered_via"] == []
    # The unknown label still lands on the audit event (the audit
    # records the L3 anomaly regardless of filter state).
    assert event.payload["attention_channel"] == "not_a_real_channel"


# --- 5. Filter is per-surface, not per-channel -----------------------------


def test_filter_is_per_surface_not_per_channel_on_interrupt_now(
    event_log_conn: sqlite3.Connection,
    mocked_notify: tuple[MagicMock, MagicMock],
) -> None:
    """``interrupt_now`` maps to (say_bell, osascript_banner).

    Passing ``frozenset({"say_bell"})`` MUST fire voice (with bell
    marker) but skip the banner — proving the filter walks individual
    surface IDs rather than the whole channel tuple.
    """
    popen_mock, run_mock = mocked_notify
    plan = _make_plan("<voice>urgent</voice><document>doc</document>")
    state = _primed_state(plan)

    _, event = render_response(
        state,
        plan,
        conn=event_log_conn,
        turn_id="T_per_surface",
        attention_channel="interrupt_now",
        stream=io.StringIO(),
        available_surfaces=frozenset({"say_bell"}),
    )

    popen_mock.assert_called_once()
    run_mock.assert_not_called()  # banner skipped despite being in channel tuple
    assert event.payload["delivered_via"] == ["voice"]
    # Bell marker still prepended to the voice text (filter does not
    # alter the per-surface dispatch logic, only whether it runs).
    say_argv = popen_mock.call_args[0][0]
    assert say_argv[-1].startswith("\a")
    assert say_argv[-1].endswith("urgent")


# --- 6. drive_turn forwards available_surfaces -----------------------------


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[JarvisRuntime]:
    """Bootstrap a real :class:`JarvisRuntime` against ``tmp_path``."""
    rt = bootstrap_runtime_app(
        config_path=_CONFIG_PATH,
        prompt_path=_PROMPT_PATH,
        runtime_root=tmp_path,
    )
    yield rt
    rt.conn.close()


@pytest.fixture
def stub_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``jarvis.runtime.decide`` with a one-shot final-plan stub."""
    text = _STUB_RESPONSE_TEXT
    plan = ResponsePlan(
        text=text,
        permission="force_limitation_language",
        downgrade_required=False,
        active_claim_levels=(),
        response_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        output_risk_class="routine",
        required_gate_mode="sentence",
    )

    def _fake_decide(trigger: Event, _ctx: object) -> DecideResult:
        turn_id = (
            trigger.payload.get("turn_id")
            if trigger.type == "surface.user_intent"
            else None
        )
        return DecideResult(
            response_plan=plan,
            events_emitted=(),
            turn_id=turn_id if isinstance(turn_id, str) else None,
            attention_channel="voice_notify",
        )

    monkeypatch.setattr("jarvis.runtime.decide", _fake_decide)


def test_drive_turn_forwards_available_surfaces_frozenset_to_render(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001 — fixture installs the monkeypatch
    mocked_notify: tuple[MagicMock, MagicMock],
) -> None:
    """``drive_turn(..., available_surfaces=frozenset())`` MUST gate every physical surface.

    Verifies the kwarg is threaded end-to-end. The
    ``surface.response_emitted`` row in the log MUST carry
    ``delivered_via=[]`` proving the filter reached the renderer.
    """
    popen_mock, run_mock = mocked_notify
    user_intent_event = emit_surface_user_intent(
        runtime.conn,
        transcript="hello",
        turn_id="T_drive_filter",
    )
    drive_turn(
        runtime,
        user_intent_event=user_intent_event,
        available_surfaces=frozenset(),
    )

    popen_mock.assert_not_called()
    run_mock.assert_not_called()

    cursor = runtime.conn.execute(
        "SELECT payload_json FROM events WHERE type = ? ORDER BY id ASC",
        ("surface.response_emitted",),
    )
    rows = cursor.fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload["delivered_via"] == []
    assert payload["turn_id"] == "T_drive_filter"


# --- Small helper to keep test bodies tidy ---------------------------------


def _primed_state(plan: _PlanStub) -> SurfaceState:
    """Prime a :class:`SurfaceState` with the plan's hash (Pre-emit token)."""
    return SurfaceState(last_gate_response_hash=plan.response_hash)
