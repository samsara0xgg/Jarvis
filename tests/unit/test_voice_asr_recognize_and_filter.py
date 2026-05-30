"""ADR-0005 voice_asr — recognize + is_empty_or_too_short."""
from __future__ import annotations

from unittest.mock import MagicMock

from jarvis.surface import voice_asr


def test_transcription_result_dataclass_has_required_fields() -> None:
    """TranscriptionResult carries text/confidence/language_detected/emotion."""
    tr = voice_asr.TranscriptionResult(
        text="你好",
        confidence=0.9,
        language_detected="zh-CN",
        emotion="HAPPY",
    )
    assert tr.text == "你好"
    assert tr.confidence == 0.9
    assert tr.language_detected == "zh-CN"
    assert tr.emotion == "HAPPY"


def test_is_empty_or_too_short_returns_true_for_blank_text() -> None:
    """Blank / 1-char text fails the min-length gate even with audio."""
    assert voice_asr.is_empty_or_too_short("", audio_pcm=b"\x00" * 100) is True
    assert voice_asr.is_empty_or_too_short("   ", audio_pcm=b"\x00" * 100) is True
    assert voice_asr.is_empty_or_too_short("a", audio_pcm=b"\x00" * 100) is True  # <2 chars


def test_is_empty_or_too_short_returns_true_for_silent_audio() -> None:
    """Pure-zero PCM frames RMS = 0, fails the audio-energy gate."""
    # 1000 frames of pure zero -> RMS = 0.
    assert voice_asr.is_empty_or_too_short("你好", audio_pcm=b"\x00" * 2000) is True


def test_is_empty_or_too_short_returns_true_for_punctuation_only() -> None:
    """CJK-punctuation-only text fails the punctuation gate."""
    # Audio is non-silent (alternating bytes) but text is just CJK punctuation.
    audio = b"\x10\x00" * 1000
    assert voice_asr.is_empty_or_too_short("。。。", audio_pcm=audio) is True


def test_is_empty_or_too_short_returns_false_for_real_utterance() -> None:
    """Real utterance + non-silent audio passes all three filter gates."""
    audio = b"\x10\x00" * 1000  # non-zero RMS
    assert voice_asr.is_empty_or_too_short("现在几点", audio_pcm=audio) is False


def test_recognizer_protocol_returns_transcription_result() -> None:
    """SenseVoice provider returns a TranscriptionResult; we test via stub."""
    fake_recognizer = MagicMock(spec=voice_asr.AsrRecognizer)
    fake_recognizer.recognize.return_value = voice_asr.TranscriptionResult(
        text="你好",
        confidence=0.9,
        language_detected="zh-CN",
        emotion=None,
    )
    result = fake_recognizer.recognize(b"\x10\x00" * 16000)
    assert result.text == "你好"
    assert result.confidence == 0.9
