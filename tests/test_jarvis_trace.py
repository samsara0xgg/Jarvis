"""Integration tests verifying that JarvisApp._process_turn writes correct v3 trace rows.

Strategy: bypass JarvisApp.__init__ entirely via __new__ + manual attribute stubs
(same pattern as test_jarvis_asr_integration.py), then wire a real TraceLog backed
by a tmp_path SQLite so assertions can query actual rows.

Most tests short-circuit by stubbing llm.chat_stream to return a fixed
(text, messages) tuple and leaving create_tts_pipeline unset, so the cloud
path executes without touching TTS / interrupt_monitor subsystems.
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jarvis import JarvisApp
from memory.trace import TraceLog

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_llm_stub() -> MagicMock:
    """Return a minimal LLMClient stub with v3 metadata properties."""
    stub = MagicMock()
    stub.model = "test-model"
    stub.active_preset = "default"
    stub.last_metadata = {
        "provider": None,
        "conv_id": None,
        "response_id": None,
        "streaming": False,
        "fallback_used": False,
        "truncated_by_interrupt": False,
        "full_response": None,
        "cache_creation_input_tokens": None,
    }
    stub.last_finish_reason = "stop"
    stub.last_cache_read_tokens = 0
    stub.last_input_tokens = 0
    stub.last_output_tokens = 0
    # chat_stream returns (text, updated_messages) — fed into the cloud branch
    # so trace rows are written without invoking a real LLM.
    stub.chat_stream = MagicMock(return_value=("テスト応答", []))
    return stub


def _make_jarvis(tmp_path) -> JarvisApp:
    """Construct a minimal JarvisApp stub around a real TraceLog.

    All subsystems that _process_turn_inner touches are replaced with MagicMocks.
    The real TraceLog is wired so trace rows land in a tmp_path SQLite file.
    The intent_router stub returns None (skipping the local branch) and the
    llm stub's chat_stream returns a fixed (text, []) tuple, so turns flow
    through the cloud path without invoking real LLM / TTS / interrupt code.

    Args:
        tmp_path: pytest tmp_path fixture value.

    Returns:
        A JarvisApp instance ready for handle_text calls.
    """
    with patch.object(JarvisApp, "__init__", lambda self, cfg, **kw: None):
        app = JarvisApp.__new__(JarvisApp)

    # ── Config (only llm_pricing key used in _flush_trace) ──
    app.config = {}

    # ── Logger ──
    app.logger = logging.getLogger("jarvis.test")

    # ── Thread / cancellation primitives ──
    app._cancel = threading.Event()
    app._pipeline_lock = threading.Lock()
    from concurrent.futures import ThreadPoolExecutor
    app._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis_test")

    # ── Trace v3 cross-turn state ──
    db_path = str(tmp_path / "jarvis_test.db")
    app.trace_log = TraceLog(db_path)
    app._turn_counter: dict[str, int] = {}
    app._last_trace_id = None
    # Trace v3: per-launch session id (separate from conversation_store
    # session). Real __init__ uses datetime + uuid; tests only need any
    # stable string.
    app._app_session_id = "test_session_0001"

    # NLI classifier (lazy-loaded; stub returns None so outcome tests are unit-scope)
    app.nli_classifier = MagicMock()
    app.nli_classifier.min_text_length = 2
    app.nli_classifier.max_text_length = 500
    app.nli_classifier.detect_outcome = MagicMock(return_value=None)

    # ── prompt_version (16-char SHA prefix of personality.py) ──
    try:
        import hashlib
        from pathlib import Path
        personality_src = Path(__file__).parent.parent / "core" / "personality.py"
        app._prompt_version: str | None = hashlib.sha256(
            personality_src.read_bytes()
        ).hexdigest()[:16]
    except OSError:
        app._prompt_version = None

    # ── Voice-path per-turn captures (always None on text path) ──
    app._last_asr_confidence = None
    app._last_vad_duration_ms = None
    app._last_asr_ms = None
    app._first_audio_at = None

    # ── Subsystems replaced with MagicMocks ──
    app.asr_normalizer = MagicMock()
    app.asr_normalizer.normalize = MagicMock(side_effect=lambda t: t)

    app.conversation_store = MagicMock()
    app.conversation_store.get_history = MagicMock(return_value=[])
    app.conversation_store.replace = MagicMock()

    app.event_bus = MagicMock()

    app.rule_manager = None  # skip keyword-rule branch
    app.local_executor = None

    app.memory_manager = MagicMock()
    # The cloud path doesn't call build_prompt_context in this test (user_id
    # is not set on the stub), but the memory_manager attribute must still
    # behave as a MagicMock that tolerates any access.
    app.memory_manager.save = MagicMock()
    app.memory_manager.write_observation = MagicMock()

    app.intent_router = MagicMock()
    app.intent_router.route_and_respond = MagicMock(return_value=None)
    # Prevent _flush_trace from injecting a MagicMock into llm_metadata
    # via getattr(intent_router, "last_metadata") on the cloud path.
    app.intent_router.last_metadata = None

    app.tool_registry = MagicMock()
    app.tool_registry.get_tool_definitions = MagicMock(return_value=[])
    app.tool_registry.execute = MagicMock(return_value=None)

    app.llm = _make_llm_stub()

    app.behavior_log = MagicMock()
    app.behavior_log.log = MagicMock()

    app._tts = None
    app.oled = None
    app.interrupt_monitor = MagicMock()

    # ── Interrupt-state reset ──
    app._interrupted_response = None
    app._interrupt_played_texts = None

    # ── Session config ──
    app.farewell_phrases = {"再见", "bye", "goodbye"}

    return app


@pytest.fixture()
def app(tmp_path) -> JarvisApp:
    """Yield a JarvisApp stub backed by a real tmp TraceLog; shut down after test.

    Yields:
        Configured JarvisApp stub.
    """
    instance = _make_jarvis(tmp_path)
    yield instance
    instance._executor.shutdown(wait=False)
    instance.trace_log.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestJarvisTraceV3:
    """Verify that _process_turn writes correct v3 trace rows via handle_text."""

    def test_handle_text_writes_trace_row(self, app: JarvisApp) -> None:
        """handle_text must write a trace row with correct scalar fields.

        Calls handle_text once and queries the trace DB for the resulting row.
        Asserts user_text, trigger_source, end_reason, session_id, and a
        well-formed 16-char prompt_version.
        """
        app.handle_text("你好", session_id="test_session_1")

        rows = app.trace_log.query_for_debug(hours=1)
        assert len(rows) >= 1
        row = rows[0]
        assert row["user_text"] == "你好"
        assert row["trigger_source"] == "web_text"
        assert row["end_reason"] == "success"
        # trace.session_id is the per-launch _app_session_id, NOT the
        # conversation_store session_id passed to handle_text. The two
        # decoupled intentionally — see _flush_trace docstring.
        assert row["session_id"] == app._app_session_id
        assert row["prompt_version"] is not None
        assert len(row["prompt_version"]) == 16

    def test_prompt_version_stable_across_turns(self, app: JarvisApp) -> None:
        """prompt_version must be identical across consecutive turns.

        It is computed once in __init__ (here in the fixture) from the SHA of
        personality.py, so it must not drift between turns.
        """
        app.handle_text("第一轮", session_id="sess_stable")
        app.handle_text("第二轮", session_id="sess_stable")

        # All trace rows now key on app._app_session_id, not the conversation
        # session passed by handle_text.
        rows = app.trace_log.query_for_debug(hours=1, session_id=app._app_session_id)
        assert len(rows) == 2
        versions = {r["prompt_version"] for r in rows}
        assert len(versions) == 1, f"prompt_version changed between turns: {versions}"

    def test_end_reason_error_on_exception(self, app: JarvisApp) -> None:
        """When _process_turn_inner raises, end_reason must be 'error' and error must capture the traceback.

        Patches _process_turn_inner to raise RuntimeError("boom"), then asserts
        the exception propagates to the caller AND the trace row records the failure.
        """
        with patch.object(app, "_process_turn_inner", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                app.handle_text("触发异常", session_id="sess_err")

        rows = app.trace_log.query_for_debug(hours=1, session_id=app._app_session_id)
        assert len(rows) >= 1
        row = rows[0]
        assert row["end_reason"] == "error"
        assert row["error"] is not None
        assert "RuntimeError" in row["error"]
        assert "boom" in row["error"]

    def test_text_path_ttfs_ms_is_none(self, app: JarvisApp) -> None:
        """ttfs_ms must be None on the text path because no TTS audio plays.

        _first_audio_at is reset to None in _reset_turn_state and the cloud
        path (with no tts_pipeline) never sets it.
        """
        app.handle_text("查个东西", session_id="sess_ttfs")

        rows = app.trace_log.query_for_debug(hours=1, session_id=app._app_session_id)
        assert len(rows) >= 1
        assert rows[0]["ttfs_ms"] is None

    def test_outcome_lag_one_turn(self, app: JarvisApp) -> None:
        """Turn N+1's approval text updates turn N's outcome_signal via async NLI.

        NLI is mocked to return +1 for "好的". When it fires on turn N+1 and
        _last_trace_id points to turn N, the async outcome job writes
        outcome_signal=1 on turn N and sets outcome_at_turn_id to turn N+1's id.
        """
        # Mock NLI to return +1 for "好的" (simulates positive NLI detection).
        app.nli_classifier.detect_outcome = lambda t: 1 if "好的" in t else None

        app.handle_text("你好", session_id="sess_lag")   # turn N
        app.handle_text("好的", session_id="sess_lag")   # turn N+1 → +1 signal

        # Wait for async executor to complete outcome write.
        app._executor.shutdown(wait=True)
        from concurrent.futures import ThreadPoolExecutor
        app._executor = ThreadPoolExecutor(max_workers=1)

        rows = app.trace_log.query_for_debug(hours=1, session_id=app._app_session_id)
        # query_for_debug returns newest-first; reverse for chronological order
        rows_by_text = {r["user_text"]: r for r in rows}
        assert "你好" in rows_by_text, f"turn N row missing; rows: {[r['user_text'] for r in rows]}"
        assert "好的" in rows_by_text, f"turn N+1 row missing"

        turn_n = rows_by_text["你好"]
        turn_n1 = rows_by_text["好的"]

        assert turn_n["outcome_signal"] == 1, (
            f"Expected outcome_signal=1 on turn N, got {turn_n['outcome_signal']}"
        )
        assert turn_n["outcome_at_turn_id"] == turn_n1["id"], (
            f"outcome_at_turn_id mismatch: {turn_n['outcome_at_turn_id']} != {turn_n1['id']}"
        )

    def test_long_user_text_does_not_trigger_outcome(self, app: JarvisApp) -> None:
        """NLI returning None for a long ambiguous utterance leaves outcome_signal NULL.

        The mock NLI returns None for ambiguous text, so turn N's
        outcome_signal must remain None.
        """
        # Default mock returns None — no outcome for ambiguous text.
        app.handle_text("你好", session_id="sess_long")
        app.handle_text("谢谢你刚才说的那件事，其实我有别的疑问", session_id="sess_long")

        app._executor.shutdown(wait=True)
        from concurrent.futures import ThreadPoolExecutor
        app._executor = ThreadPoolExecutor(max_workers=1)

        rows = app.trace_log.query_for_debug(hours=1, session_id=app._app_session_id)
        rows_by_text = {r["user_text"]: r for r in rows}
        assert "你好" in rows_by_text

        turn_n = rows_by_text["你好"]
        assert turn_n["outcome_signal"] is None, (
            f"Expected outcome_signal=None on turn N for long text, got {turn_n['outcome_signal']}"
        )
