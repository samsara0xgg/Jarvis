"""WP2 T1.5: asr_normalizer must run on BOTH voice and text entry paths.

Moved from `handle_utterance` to `_process_turn` (shared pipeline) so that
MQTT / web-frontend text also benefits from ASR correction.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from jarvis import JarvisApp


@pytest.fixture
def jarvis_stub():
    with patch.object(JarvisApp, "__init__", lambda self, cfg, **kw: None):
        j = JarvisApp.__new__(JarvisApp)
        j.logger = MagicMock()
        j._interrupt_played_texts = None
        j.asr_normalizer = MagicMock()
        j.asr_normalizer.normalize = MagicMock(side_effect=lambda t: t + "_NORM")
        j.conversation_store = MagicMock()
        # Bail early via get_history raising — we only need to verify the
        # normalize call happened before conversation history is touched.
        j.conversation_store.get_history = MagicMock(side_effect=RuntimeError("stop here"))
    return j


class TestNormalizeCalledByProcessTurn:
    def test_process_turn_calls_normalize(self, jarvis_stub):
        """`_process_turn` must call asr_normalizer.normalize() at entry."""
        with pytest.raises(RuntimeError, match="stop here"):
            jarvis_stub._process_turn(
                text="开客厅大蛋",
                session_id="s",
                output_fn=lambda _s: None,
            )
        jarvis_stub.asr_normalizer.normalize.assert_called_once_with("开客厅大蛋")

    def test_text_path_normalizes_via_handle_text(self, jarvis_stub):
        """Text entry (`handle_text` → `_process_turn`) runs normalize too.

        Before T1.5, only the voice path (handle_utterance) called normalize.
        Moving the call to the shared `_process_turn` entry means text-path
        (MQTT/web/CC harness) also benefits. This test locks that in.
        """
        with pytest.raises(RuntimeError, match="stop here"):
            jarvis_stub.handle_text(
                text="开客厅大蛋",
                session_id="s",
            )
        jarvis_stub.asr_normalizer.normalize.assert_called_once_with("开客厅大蛋")
