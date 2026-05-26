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


def test_sentence_mode_aggregates_voice_region_into_one_synth_call(
    fake_provider: MagicMock, fake_player: MagicMock,
) -> None:
    """``sentence`` mode: per ``<voice>...</voice>`` region, ONE MiniMax call.

    Bug 3 (post-ADR-0005 smoke): previously every chunk arrival triggered
    a fresh ``asyncio.run(provider.synthesize)`` — a new WebSocket
    connect + handshake + close per chunk. For a 3-sentence voice
    response that's 3 round trips with 1-3s of WS-connect overhead
    each, audible as the "5-second bursts" the user reported. The new
    contract aggregates everything inside a ``<voice>...</voice>``
    region and synthesizes once when the closing tag arrives.
    """
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider, player=fake_player, fallback=_noop_fallback,
    )
    pipeline.begin_turn("T1", gate_mode="sentence")
    pipeline.handle_chunk("T1", "<voice>")
    pipeline.handle_chunk("T1", "在的,Allen。")
    pipeline.handle_chunk("T1", "我在。")
    pipeline.handle_chunk("T1", "你要我看什么?")
    pipeline.handle_chunk("T1", "</voice>")
    pipeline.end_turn("T1")
    assert fake_provider.synthesize.await_count == 1
    args, _ = fake_provider.synthesize.await_args
    sent = args[0]
    assert "在的" in sent
    assert "我在" in sent
    assert "看什么" in sent
    # Tags must be stripped — synthesizing literal "<voice>" produces the
    # garbled "less-than voice greater-than" speech the user heard.
    assert "<voice>" not in sent
    assert "</voice>" not in sent


def test_sentence_mode_skips_document_region_entirely(
    fake_provider: MagicMock, fake_player: MagicMock,
) -> None:
    """``<document>...</document>`` carries visual content; TTS must NOT speak it.

    The render layer interleaves ``<voice>`` (spoken) and ``<document>``
    (displayed) regions in the same chunk stream. The pipeline must
    extract only the voice portion so the document body does not bleed
    into synthesis.
    """
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider, player=fake_player, fallback=_noop_fallback,
    )
    pipeline.begin_turn("T2", gate_mode="sentence")
    pipeline.handle_chunk("T2", "<voice>")
    pipeline.handle_chunk("T2", "hi")
    pipeline.handle_chunk("T2", "</voice>")
    pipeline.handle_chunk("T2", "<document>")
    pipeline.handle_chunk("T2", "this is doc content")
    pipeline.handle_chunk("T2", "</document>")
    pipeline.end_turn("T2")

    assert fake_provider.synthesize.await_count == 1
    args, _ = fake_provider.synthesize.await_args
    sent = args[0]
    assert "hi" in sent
    assert "this is doc content" not in sent
    assert "<document>" not in sent
    assert "</document>" not in sent


def test_sentence_mode_flushes_unclosed_voice_on_end_turn(
    fake_provider: MagicMock, fake_player: MagicMock,
) -> None:
    """If ``</voice>`` never arrives, ``end_turn`` flushes whatever was buffered.

    Defensive: a streaming response that gets truncated mid-region
    should still produce audible output for what was emitted, not
    silently swallow it.
    """
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider, player=fake_player, fallback=_noop_fallback,
    )
    pipeline.begin_turn("T3", gate_mode="sentence")
    pipeline.handle_chunk("T3", "<voice>")
    pipeline.handle_chunk("T3", "你好")
    pipeline.end_turn("T3")
    assert fake_provider.synthesize.await_count == 1
    args, _ = fake_provider.synthesize.await_args
    assert "你好" in args[0]


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
    pipeline.end_turn("T4")  # Sentence-mode now flushes at end_turn (Bug 3 aggregation).
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
