"""ADR-0005 voice_tts.TTSPipeline — gate-mode routing + fallback."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.surface import voice_tts


@pytest.fixture
def fake_provider() -> MagicMock:
    """Provider returning a small PCM payload from ``synthesize``."""
    p = MagicMock(spec=voice_tts.MiniMaxWSClient)
    p.synthesize = AsyncMock(return_value=b"\x00" * 1920)
    return p


@pytest.fixture
def fake_player() -> MagicMock:
    """Player whose queue is empty unless the test sets ``bytes_pending``."""
    pl = MagicMock(spec=voice_tts.AudioStreamPlayer)
    pl.bytes_pending.return_value = 0
    return pl


def _noop_fallback(_text: str) -> None:
    """Discarding fallback used by tests that don't observe say invocations."""


def test_sentence_mode_speaks_each_chunk_immediately(
    fake_provider: MagicMock, fake_player: MagicMock,
) -> None:
    """``sentence`` mode synthesizes every chunk at arrival (no buffering)."""
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider, player=fake_player, fallback=_noop_fallback,
    )
    pipeline.begin_turn("T1", gate_mode="sentence")
    pipeline.handle_chunk("T1", "你好。")
    pipeline.handle_chunk("T1", "今天天气怎么样?")
    pipeline.end_turn("T1")
    # synthesize called once per sentence (2 chunks total).
    assert fake_provider.synthesize.await_count == 2


def test_full_text_mode_buffers_until_emitted(
    fake_provider: MagicMock, fake_player: MagicMock,
) -> None:
    """``full_text`` mode delays synthesis until ``handle_emitted`` fires."""
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider, player=fake_player, fallback=_noop_fallback,
    )
    pipeline.begin_turn("T2", gate_mode="full_text")
    pipeline.handle_chunk("T2", "你好。")
    pipeline.handle_chunk("T2", "今天天气怎么样?")
    # No synth yet.
    assert fake_provider.synthesize.await_count == 0
    pipeline.handle_emitted("T2")
    # One synth with the joined text.
    assert fake_provider.synthesize.await_count == 1
    args, _ = fake_provider.synthesize.await_args
    joined = args[0]
    assert "你好" in joined
    assert "天气" in joined


def test_structured_mode_treated_as_full_text(
    fake_provider: MagicMock, fake_player: MagicMock,
) -> None:
    """``structured`` mode buffers like ``full_text`` per ADR §10 F10 degrade rule."""
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider, player=fake_player, fallback=_noop_fallback,
    )
    pipeline.begin_turn("T3", gate_mode="structured")
    pipeline.handle_chunk("T3", "ok")
    assert fake_provider.synthesize.await_count == 0
    pipeline.handle_emitted("T3")
    assert fake_provider.synthesize.await_count == 1


def test_missing_gate_mode_defaults_to_sentence(
    fake_provider: MagicMock, fake_player: MagicMock,
) -> None:
    """Legacy events without ``required_gate_mode`` route as ``sentence``."""
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider, player=fake_player, fallback=_noop_fallback,
    )
    pipeline.begin_turn("T4", gate_mode=None)
    pipeline.handle_chunk("T4", "hi.")
    assert fake_provider.synthesize.await_count == 1


def test_provider_failure_triggers_fallback(fake_player: MagicMock) -> None:
    """``MiniMaxUnavailableError`` from synth routes the cleaned text to ``fallback``."""
    fake_provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
    fake_provider.synthesize = AsyncMock(
        side_effect=voice_tts.MiniMaxUnavailableError("boom"),
    )
    fallback_calls: list[str] = []
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider,
        player=fake_player,
        fallback=fallback_calls.append,
    )
    pipeline.begin_turn("T5", gate_mode="sentence")
    pipeline.handle_chunk("T5", "测试")
    pipeline.end_turn("T5")
    assert fallback_calls == ["测试"]


def test_is_speaking_returns_true_while_buffer_nonempty() -> None:
    """``is_speaking`` mirrors ``player.bytes_pending`` for wake suppression."""
    player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    player.bytes_pending.return_value = 100
    pipeline = voice_tts.TTSPipeline(
        provider=MagicMock(spec=voice_tts.MiniMaxWSClient),
        player=player,
        fallback=_noop_fallback,
    )
    assert pipeline.is_speaking() is True
    player.bytes_pending.return_value = 0
    assert pipeline.is_speaking() is False
