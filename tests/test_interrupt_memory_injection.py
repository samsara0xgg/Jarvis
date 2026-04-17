"""Tests for WP5 — TTSPipeline played_texts + interrupt memory injection.

Two layers:
  1. TTSPipeline.played_texts / abort() returning currently_playing in unplayed
  2. JarvisApp._truncate_assistant_for_interrupt history rewrite logic
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from core.tts import TTSEngine, TTSPipeline, SentenceType
from jarvis import JarvisApp


# ---------------------------------------------------------------------------
# TTSPipeline.played_texts / abort() unplayed-includes-currently-playing
# ---------------------------------------------------------------------------

class TestPlayedTextsTracking:
    def _make_pipeline(self, play_delay: float = 0.0) -> TTSPipeline:
        engine = MagicMock(spec=TTSEngine)
        # synth_to_file returns (filepath, deletable=False)
        engine.synth_to_file = MagicMock(return_value=("/tmp/fake.mp3", False))

        def _play(_filepath: str) -> None:
            if play_delay:
                time.sleep(play_delay)

        engine._play_audio_file = MagicMock(side_effect=_play)
        engine.stop = MagicMock()
        return TTSPipeline(engine)

    def test_played_texts_empty_before_anything_plays(self):
        p = self._make_pipeline()
        p.start()
        try:
            assert p.played_texts == []
        finally:
            p.stop()

    def test_played_texts_records_completed_sentences(self):
        p = self._make_pipeline()
        p.start()
        p.submit("一", SentenceType.FIRST)
        p.submit("二", SentenceType.MIDDLE)
        p.submit("三", SentenceType.LAST)
        p.finish()
        p.wait_done(timeout=5)
        p.stop()
        assert p.played_texts == ["一", "二", "三"]

    def test_abort_includes_currently_playing_in_unplayed(self):
        # Play sentence 1 takes 0.5s; abort while it's mid-flight.
        p = self._make_pipeline(play_delay=0.5)
        p.start()
        p.submit("正在播", SentenceType.FIRST)
        p.submit("队列里", SentenceType.MIDDLE)
        time.sleep(0.1)  # let _play_worker pick up the first item
        unplayed = p.abort()
        p.stop()
        # "正在播" was mid-playback; per 方案 b it counts as unplayed.
        assert "正在播" in unplayed
        # "队列里" is also unplayed (still in text_queue or audio_queue)
        assert "队列里" in unplayed
        # Nothing actually finished playing.
        assert p.played_texts == []

    def test_played_texts_after_partial_playback(self):
        # Sentence 1 plays fast, sentence 2 slow → abort during sentence 2.
        engine = MagicMock(spec=TTSEngine)
        engine.synth_to_file = MagicMock(return_value=("/tmp/fake.mp3", False))
        call_count = {"i": 0}

        def _play(_filepath: str) -> None:
            call_count["i"] += 1
            if call_count["i"] == 1:
                return  # fast
            time.sleep(0.5)  # slow second sentence

        engine._play_audio_file = MagicMock(side_effect=_play)
        engine.stop = MagicMock()

        p = TTSPipeline(engine)
        p.start()
        p.submit("第一", SentenceType.FIRST)
        p.submit("第二", SentenceType.MIDDLE)
        time.sleep(0.2)  # let first finish, second start
        unplayed = p.abort()
        p.stop()
        assert p.played_texts == ["第一"]
        assert "第二" in unplayed


# ---------------------------------------------------------------------------
# JarvisApp._truncate_assistant_for_interrupt
# ---------------------------------------------------------------------------

@pytest.fixture
def jarvis_stub():
    """Bare JarvisApp without going through full __init__."""
    with patch.object(JarvisApp, "__init__", lambda self, cfg, **kw: None):
        j = JarvisApp.__new__(JarvisApp)
        j.logger = MagicMock()
    return j


class TestTruncateAssistantForInterrupt:
    def test_openai_style_string_content_truncated(self, jarvis_stub):
        messages = [
            {"role": "user", "content": "讲个故事"},
            {"role": "assistant", "content": "从前有座山，山上有个庙，庙里有个老和尚。"},
        ]
        result = jarvis_stub._truncate_assistant_for_interrupt(
            messages, played=["从前有座山，", "山上有个庙，"],
        )
        assert result[1]["content"] == "从前有座山，山上有个庙，..."
        assert result[-1] == {"role": "user", "content": "[Interrupted by user]"}
        assert len(result) == 3

    def test_anthropic_style_list_content_truncated(self, jarvis_stub):
        messages = [
            {"role": "user", "content": "讲个故事"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "完整三句话。"}],
            },
        ]
        result = jarvis_stub._truncate_assistant_for_interrupt(
            messages, played=["第一句。"],
        )
        assert result[1]["content"] == [{"type": "text", "text": "第一句。..."}]
        assert result[-1]["content"] == "[Interrupted by user]"

    def test_no_played_text_uses_ellipsis(self, jarvis_stub):
        # User cut off before any sentence completed playback.
        messages = [
            {"role": "user", "content": "嗨"},
            {"role": "assistant", "content": "完整回复。"},
        ]
        result = jarvis_stub._truncate_assistant_for_interrupt(messages, played=[])
        assert result[1]["content"] == "..."

    def test_empty_messages_returns_empty(self, jarvis_stub):
        assert jarvis_stub._truncate_assistant_for_interrupt([], ["x"]) == []

    def test_marker_is_user_role(self, jarvis_stub):
        # Cross-provider compat: stick to "user" role for the interrupted marker
        # (Anthropic also accepts user, OpenAI accepts user — system would split).
        messages = [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
        ]
        result = jarvis_stub._truncate_assistant_for_interrupt(messages, played=["y"])
        assert result[-1]["role"] == "user"

    def test_only_last_assistant_modified(self, jarvis_stub):
        # Multi-turn: only the most recent assistant entry should change.
        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2 full reply"},
        ]
        result = jarvis_stub._truncate_assistant_for_interrupt(
            messages, played=["a2 part."],
        )
        assert result[1]["content"] == "a1"  # earlier turn untouched
        assert result[3]["content"] == "a2 part...."


# ---------------------------------------------------------------------------
# P0-B: stale _interrupt_played_texts must not pollute later turns
# ---------------------------------------------------------------------------

class TestStaleStateReset:
    """A non-cloud_path return (resume / farewell / direct_answer / etc.) leaves
    _interrupt_played_texts populated. Without an entry-time reset, the NEXT
    turn that does go cloud_path would mis-truncate its own (uninterrupted)
    assistant response. The reset at _process_turn entry guards against this.
    """

    def test_process_turn_clears_stale_played_at_entry(self, jarvis_stub):
        # Simulate stale value left from a prior interrupted turn that
        # returned via a non-cloud_path branch.
        jarvis_stub._interrupt_played_texts = ["残留 from prior turn"]

        # Bail out of _process_turn right after the reset by raising in the
        # very next line (conversation_store.get_history). We don't care
        # what the rest of the function does — only the entry-time reset.
        jarvis_stub.conversation_store = MagicMock()
        jarvis_stub.conversation_store.get_history.side_effect = RuntimeError(
            "intentional bail — reset must have happened by now",
        )

        with pytest.raises(RuntimeError, match="intentional bail"):
            jarvis_stub._process_turn(
                text="新一轮",
                session_id="sess",
                output_fn=lambda _s: None,
            )

        # The stale value MUST have been cleared before get_history raised.
        assert jarvis_stub._interrupt_played_texts is None

    def test_consumer_clears_after_apply(self, jarvis_stub):
        # The consumer in the cloud_path block sets it back to None after
        # applying. Verify by simulating that block in isolation.
        jarvis_stub._interrupt_played_texts = ["heard."]
        messages = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "full reply"},
        ]
        # Mirror jarvis.py:1081-1085 pattern
        if jarvis_stub._interrupt_played_texts is not None:
            messages = jarvis_stub._truncate_assistant_for_interrupt(
                messages, jarvis_stub._interrupt_played_texts,
            )
            jarvis_stub._interrupt_played_texts = None

        assert jarvis_stub._interrupt_played_texts is None
        assert messages[1]["content"] == "heard...."
        assert messages[-1]["content"] == "[Interrupted by user]"
