"""Unit tests for :func:`jarvis.runtime.drive_turn` streaming plumbing (ADR-0003 Step 2 Build 4).

Build 3 added ``streaming_enabled`` + ``query`` kwargs to
:func:`render_response`. Build 4 (this commit) threads them through
:func:`drive_turn` so the daemon watcher (Build 5) can enable Inherent
streaming while the CLI path keeps Step-1 single-emit semantics.

Coverage:

1. Default ``streaming_enabled=False``: drive_turn called WITHOUT
   ``streaming_enabled`` -> only ``surface.response_emitted`` lands; no
   ``surface.response_open`` / ``surface.response_chunk`` rows.
2. Explicit ``streaming_enabled=True``: drive_turn forwards the flag +
   the lifted transcript so the renderer emits the 3-event Inherent
   taxonomy with ``query`` on the open payload.
3. Missing ``transcript`` key in ``user_intent_event.payload``:
   drive_turn defaults ``query`` to ``""`` (no KeyError); the
   ``surface.response_open`` row carries ``query=""``.
4. ``run_turn`` (CLI path) regression: forwards the default
   ``streaming_enabled=False`` so the event log has only the
   single audit row.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from jarvis.decision import DecideResult
from jarvis.decision.gates import ResponsePlan
from jarvis.runtime import (
    JarvisRuntime,
    bootstrap_runtime_app,
    drive_turn,
    run_turn,
)
from jarvis.shared import Event
from jarvis.state.event_log import iter_events
from jarvis.surface.cli import emit_surface_user_intent

if TYPE_CHECKING:
    from collections.abc import Iterator


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "jarvis.yaml"
_PROMPT_PATH = _REPO_ROOT / "prompts" / "jarvis_v1.md"

# Two-sentence response so the streaming branch produces exactly 2
# ``surface.response_chunk`` rows under sentence-mode splitting.
_STUB_RESPONSE_TEXT: str = "句一。句二。"


# --- Fixtures -------------------------------------------------------------


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
def mocked_notify() -> Iterator[tuple[object, object]]:
    """Patch subprocess primitives so no macOS surface fires for real."""
    with (
        patch("jarvis.surface.notify.subprocess.Popen") as popen_mock,
        patch("jarvis.surface.notify.subprocess.run") as run_mock,
    ):
        yield popen_mock, run_mock


def _stub_response_plan() -> ResponsePlan:
    """Build a deterministic two-sentence :class:`ResponsePlan`.

    ``required_gate_mode="sentence"`` ensures the streaming branch
    splits the text into two ``surface.response_chunk`` rows.
    """
    text = _STUB_RESPONSE_TEXT
    return ResponsePlan(
        text=text,
        permission="force_limitation_language",
        downgrade_required=False,
        active_claim_levels=(),
        response_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        output_risk_class="routine",
        required_gate_mode="sentence",
    )


@pytest.fixture
def stub_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``jarvis.runtime.decide`` with a one-shot final-plan stub.

    The stub returns the deterministic two-sentence plan on the first
    invocation so :func:`drive_turn` finalizes in one iteration without
    network traffic.
    """

    def _fake_decide(trigger: Event, _ctx: object) -> DecideResult:
        turn_id = trigger.payload.get("turn_id") if trigger.type == "surface.user_intent" else None
        return DecideResult(
            response_plan=_stub_response_plan(),
            events_emitted=(),
            turn_id=turn_id if isinstance(turn_id, str) else None,
            attention_channel="voice_notify",
        )

    monkeypatch.setattr("jarvis.runtime.decide", _fake_decide)


# --- Helpers --------------------------------------------------------------


def _row_types(runtime: JarvisRuntime) -> list[str]:
    """Return all event types in the log, in id order."""
    return [evt.type for evt in iter_events(runtime.conn)]


def _select_open_row_payload(runtime: JarvisRuntime) -> dict[str, object]:
    """Return the single ``surface.response_open`` row's payload as a dict."""
    cursor = runtime.conn.execute(
        "SELECT payload_json FROM events WHERE type = ? ORDER BY id ASC",
        ("surface.response_open",),
    )
    rows = cursor.fetchall()
    assert len(rows) == 1, f"expected exactly one surface.response_open row, got {len(rows)}"
    payload = json.loads(rows[0][0])
    assert isinstance(payload, dict)
    return payload


# --- 1. Default behaviour preserved (streaming_enabled omitted) ------------


def test_drive_turn_default_streaming_disabled_emits_only_response_emitted(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001 — fixture installs the monkeypatch
    mocked_notify: tuple[object, object],  # noqa: ARG001 — fixture installs the patches
) -> None:
    """Default ``streaming_enabled=False`` MUST preserve Step-1 single-emit semantics.

    drive_turn called WITHOUT ``streaming_enabled`` -> render_response
    receives ``False`` -> only ``surface.response_emitted`` lands; no
    ``surface.response_open`` / ``surface.response_chunk`` rows.
    """
    user_intent_event = emit_surface_user_intent(
        runtime.conn,
        transcript="hello",
        turn_id="T_default",
    )

    drive_turn(runtime, user_intent_event=user_intent_event)

    types = _row_types(runtime)
    # Exactly two row types from the turn: the input intent and the audit emit.
    assert types == ["surface.user_intent", "surface.response_emitted"], (
        f"default drive_turn must not emit streaming rows; got {types!r}"
    )


# --- 2. Explicit streaming_enabled=True forwards flag + query --------------


def test_drive_turn_streaming_enabled_true_emits_three_event_taxonomy(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001
    mocked_notify: tuple[object, object],  # noqa: ARG001
) -> None:
    """drive_turn forwards ``streaming_enabled=True`` + ``query`` from the intent payload.

    With ``transcript="你好世界"`` and sentence-mode text ``"句一。句二。"``,
    the event log row order MUST be:
    user_intent -> response_open -> response_chunk x2 -> response_emitted.
    The open payload's ``query`` MUST equal the transcript.
    """
    user_intent_event = emit_surface_user_intent(
        runtime.conn,
        transcript="你好世界",
        turn_id="T_stream",
    )

    drive_turn(
        runtime,
        user_intent_event=user_intent_event,
        streaming_enabled=True,
    )

    types = _row_types(runtime)
    assert types == [
        "surface.user_intent",
        "surface.response_open",
        "surface.response_chunk",
        "surface.response_chunk",
        "surface.response_emitted",
    ], f"unexpected event sequence under streaming: {types!r}"

    open_payload = _select_open_row_payload(runtime)
    assert open_payload == {
        "turn_id": "T_stream",
        "query": "你好世界",
        "kind": "text",
        "required_gate_mode": "sentence",
        "attention_channel": "voice_notify",
    }, f"surface.response_open payload mismatch: {open_payload!r}"


# --- 3. Missing transcript key handled gracefully --------------------------


def test_drive_turn_streaming_with_missing_transcript_defaults_query_to_empty(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001
    mocked_notify: tuple[object, object],  # noqa: ARG001
) -> None:
    """Missing ``transcript`` key MUST default ``query=""`` (no KeyError).

    Constructs the ``Event`` in-memory (bypassing :func:`emit_event`'s
    schema check, which requires ``transcript``) so the test exercises
    the malformed-payload branch in :func:`drive_turn` itself. The
    stubbed ``decide`` does not poll the log, so the unwritten event
    drives the decide loop directly via the function-argument path.
    """
    user_intent_event = Event(
        event_uid="evt_missing_transcript",
        type="surface.user_intent",
        schema_version=1,
        ts_epoch_ms=0,
        payload={"turn_id": "T_no_transcript"},  # NO transcript key
        source_event_id=None,
        correlation={"turn_id": "T_no_transcript"},
    )

    drive_turn(
        runtime,
        user_intent_event=user_intent_event,
        streaming_enabled=True,
    )

    open_payload = _select_open_row_payload(runtime)
    assert open_payload == {
        "turn_id": "T_no_transcript",
        "query": "",
        "kind": "text",
        "required_gate_mode": "sentence",
        "attention_channel": "voice_notify",
    }, f"surface.response_open payload mismatch: {open_payload!r}"


# --- 4. run_turn (CLI path) regression ------------------------------------


def test_run_turn_cli_path_keeps_streaming_disabled(
    runtime: JarvisRuntime,
    stub_decide: None,  # noqa: ARG001
    mocked_notify: tuple[object, object],  # noqa: ARG001
) -> None:
    """``run_turn`` MUST forward the default ``streaming_enabled=False``.

    No new event types in the log beyond the Step-1 pair
    (``surface.user_intent`` + ``surface.response_emitted``); the CLI
    path is byte-for-byte unchanged by Build 4.
    """
    run_turn(runtime, utterance="hi", turn_id="T_cli")

    types = _row_types(runtime)
    assert types == ["surface.user_intent", "surface.response_emitted"], (
        f"run_turn must not light up streaming rows; got {types!r}"
    )
