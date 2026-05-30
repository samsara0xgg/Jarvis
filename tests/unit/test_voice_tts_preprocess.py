"""ADR-0005 voice_tts._preprocess_for_speech — port of legacy tts_preprocessor."""
from __future__ import annotations

from jarvis.surface.voice_tts import _preprocess_for_speech


def test_strips_emoji() -> None:
    """Emoji code points (U+1F300..U+1FAFF etc.) drop out of TTS input."""
    assert _preprocess_for_speech("好啊 🎉") == "好啊"


def test_strips_markdown_asterisks() -> None:
    """Markdown bold ``**x**`` unwraps to ``x`` (asterisks not spoken)."""
    assert _preprocess_for_speech("**重要**事项") == "重要事项"


def test_strips_brackets() -> None:
    """Markdown link ``[label](url)`` collapses to bare ``label``."""
    assert _preprocess_for_speech("看 [这里](http://x)") == "看 这里"


def test_passthrough_for_plain_text() -> None:
    """Plain text with no emoji or markdown passes through untouched."""
    assert _preprocess_for_speech("现在是下午三点") == "现在是下午三点"


def test_handles_empty_string() -> None:
    """Empty input returns empty output without raising."""
    assert _preprocess_for_speech("") == ""
