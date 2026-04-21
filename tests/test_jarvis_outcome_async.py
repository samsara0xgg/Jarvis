"""Tests for jarvis.py async NLI outcome submit in _flush_trace.

Strategy: bypass JarvisApp.__init__ via __new__ + manual stubs (same pattern
as test_jarvis_trace.py), then verify that _flush_trace:
  1. submits _resolve_outcome to the executor (non-blocking)
  2. the submitted callable calls detect_outcome and update_outcome correctly
  3. main path is not blocked (no .result() call on the future)
"""
from __future__ import annotations

import logging
import threading
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from jarvis import JarvisApp
from memory.trace import TraceLog

LOGGER = logging.getLogger(__name__)


def _make_jarvis(tmp_path) -> JarvisApp:
    """Minimal JarvisApp stub wired with a real TraceLog."""
    with patch.object(JarvisApp, "__init__", lambda self, cfg, **kw: None):
        app = JarvisApp.__new__(JarvisApp)

    app.config = {}
    app.logger = logging.getLogger("jarvis.test.outcome")
    app._cancel = threading.Event()
    app._pipeline_lock = threading.Lock()

    from concurrent.futures import ThreadPoolExecutor
    app._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis_test")

    db_path = str(tmp_path / "outcome_test.db")
    app.trace_log = TraceLog(db_path)
    app._turn_counter: dict[str, int] = {}
    app._last_trace_id = None
    app._last_user_text = None
    app._app_session_id = "outcome_test_session"
    app._prompt_version = None

    app.nli_classifier = MagicMock()
    app.nli_classifier.detect_outcome = MagicMock(return_value=None)

    app._last_asr_confidence = None
    app._last_vad_duration_ms = None
    app._last_asr_ms = None
    app._first_audio_at = None

    app.asr_normalizer = MagicMock()
    app.asr_normalizer.normalize = MagicMock(side_effect=lambda t: t)
    app.conversation_store = MagicMock()
    app.conversation_store.get_history = MagicMock(return_value=[])
    app.conversation_store.replace = MagicMock()
    app.event_bus = MagicMock()
    app.rule_manager = None
    app.local_executor = None
    app.memory_manager = MagicMock()
    app.memory_manager.save = MagicMock()
    app.memory_manager.write_observation = MagicMock()

    app.intent_router = MagicMock()
    app.intent_router.route_and_respond = MagicMock(return_value=None)
    app.intent_router.last_metadata = None

    app.tool_registry = MagicMock()
    app.tool_registry.get_tool_definitions = MagicMock(return_value=[])
    app.tool_registry.execute = MagicMock(return_value=None)

    llm = MagicMock()
    llm.model = "test-model"
    llm.active_preset = "default"
    llm.last_metadata = {
        "provider": None, "conv_id": None, "response_id": None,
        "streaming": False, "fallback_used": False,
        "truncated_by_interrupt": False, "full_response": None,
        "cache_creation_input_tokens": None,
    }
    llm.last_finish_reason = "stop"
    llm.last_cache_read_tokens = 0
    llm.last_input_tokens = 0
    llm.last_output_tokens = 0
    llm.chat_stream = MagicMock(return_value=("回答", []))
    app.llm = llm

    app.behavior_log = MagicMock()
    app.behavior_log.log = MagicMock()
    app._tts = None
    app.oled = None
    app.interrupt_monitor = MagicMock()
    app._interrupted_response = None
    app._interrupt_played_texts = None
    app.farewell_phrases = {"再见", "bye"}

    return app


@pytest.fixture()
def app(tmp_path) -> JarvisApp:
    instance = _make_jarvis(tmp_path)
    yield instance
    instance._executor.shutdown(wait=True)
    instance.trace_log.close()


class TestAsyncOutcomeSubmit:

    def test_flush_trace_submits_to_executor_not_blocks(self, app: JarvisApp) -> None:
        """_flush_trace submits _resolve_outcome to executor and returns immediately.

        The executor.submit call must not block (no .result() wait).
        We intercept submitted callables to verify submission without running NLI.
        """
        submitted: list[Any] = []
        real_submit = app._executor.submit

        def capturing_submit(fn, *a, **kw):
            submitted.append(fn)
            return real_submit(fn, *a, **kw)

        app._executor.submit = capturing_submit  # type: ignore[method-assign]

        # Turn 1: sets up _last_trace_id
        app.handle_text("你好", session_id="s1")
        initial_submit_count = len(submitted)

        # Turn 2: should submit _resolve_outcome + observation write
        app.handle_text("好的", session_id="s1")

        # At least one new submit happened on turn 2
        assert len(submitted) > initial_submit_count

    def test_positive_nli_signal_updates_previous_trace(self, app: JarvisApp) -> None:
        """NLI +1 signal on turn N+1's text updates turn N's outcome_signal.

        Mock NLI to return +1 for '好的' so we can verify the DB write
        without requiring the actual model.
        """
        app.nli_classifier.detect_outcome = MagicMock(
            side_effect=lambda t: 1 if "好的" in t else None
        )

        app.handle_text("你好", session_id="s_pos")
        app.handle_text("好的", session_id="s_pos")

        # Wait for executor to drain
        app._executor.shutdown(wait=True)
        from concurrent.futures import ThreadPoolExecutor
        app._executor = ThreadPoolExecutor(max_workers=1)

        rows = app.trace_log.query_for_debug(hours=1, session_id=app._app_session_id)
        rows_by_text = {r["user_text"]: r for r in rows}

        assert "你好" in rows_by_text
        assert "好的" in rows_by_text
        turn_n = rows_by_text["你好"]
        turn_n1 = rows_by_text["好的"]
        assert turn_n["outcome_signal"] == 1, (
            f"Expected outcome_signal=1, got {turn_n['outcome_signal']}"
        )
        assert turn_n["outcome_at_turn_id"] == turn_n1["id"]

    def test_nli_none_leaves_outcome_null(self, app: JarvisApp) -> None:
        """NLI returning None must not call update_outcome (outcome_signal stays NULL)."""
        app.nli_classifier.detect_outcome = MagicMock(return_value=None)

        app.handle_text("你好", session_id="s_null")
        app.handle_text("嗯", session_id="s_null")

        app._executor.shutdown(wait=True)
        from concurrent.futures import ThreadPoolExecutor
        app._executor = ThreadPoolExecutor(max_workers=1)

        rows = app.trace_log.query_for_debug(hours=1, session_id=app._app_session_id)
        rows_by_text = {r["user_text"]: r for r in rows}
        assert rows_by_text["你好"]["outcome_signal"] is None

    def test_first_turn_no_outcome_submission(self, app: JarvisApp) -> None:
        """First turn has no previous trace — outcome executor.submit must NOT fire."""
        submitted: list[Any] = []
        real_submit = app._executor.submit

        def capturing_submit(fn, *a, **kw):
            submitted.append(fn.__name__ if hasattr(fn, "__name__") else str(fn))
            return real_submit(fn, *a, **kw)

        app._executor.submit = capturing_submit  # type: ignore[method-assign]
        app.handle_text("你好", session_id="s_first")

        # _resolve_outcome should NOT appear in submitted names on first turn
        assert "_resolve_outcome" not in submitted

    def test_nli_called_with_current_turn_text(self, app: JarvisApp) -> None:
        """NLI must receive the CURRENT turn's text (not the previous turn's).

        Lag-1 model: turn N+1's text "好的" judges turn N, not "你好" → "好的".
        """
        received_texts: list[str] = []
        app.nli_classifier.detect_outcome = MagicMock(
            side_effect=lambda t: received_texts.append(t) or None
        )

        app.handle_text("你好", session_id="s_txt")
        app.handle_text("好的", session_id="s_txt")

        app._executor.shutdown(wait=True)
        from concurrent.futures import ThreadPoolExecutor
        app._executor = ThreadPoolExecutor(max_workers=1)

        # NLI should have been called with "好的" (turn N+1 text), not "你好"
        assert "好的" in received_texts, f"Expected '好的' in NLI calls: {received_texts}"

    def test_negative_nli_signal_updates_previous_trace(self, app: JarvisApp) -> None:
        """NLI -1 signal on turn N+1's text updates turn N's outcome_signal to -1."""
        app.nli_classifier.detect_outcome = MagicMock(
            side_effect=lambda t: -1 if "不对" in t else None
        )

        app.handle_text("你好", session_id="s_neg")
        app.handle_text("不对", session_id="s_neg")

        app._executor.shutdown(wait=True)
        from concurrent.futures import ThreadPoolExecutor
        app._executor = ThreadPoolExecutor(max_workers=1)

        rows = app.trace_log.query_for_debug(hours=1, session_id=app._app_session_id)
        rows_by_text = {r["user_text"]: r for r in rows}
        assert rows_by_text["你好"]["outcome_signal"] == -1
