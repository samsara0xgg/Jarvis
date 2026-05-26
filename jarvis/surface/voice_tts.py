"""L5 voice TTS pipeline — MiniMax streaming WS + PortAudio playback.

ADR-0005 §4.2 / §5.3 / §10 (F6-F7 fallback chain).

Layer rules: imports only stdlib, third-party (`websockets`, `sounddevice`,
`numpy`), `jarvis.shared`, and `jarvis.state.event_log`. Does NOT name
`jarvis.decision`, `jarvis.execution`, `jarvis.deployment`,
`jarvis.runtime`, or `jarvis.cli`.

This file lands in 4 commits per ADR-0005 §14:
  Task 13: _preprocess_for_speech (this one)
  Task 14: AudioStreamPlayer
  Task 15: MiniMaxWSClient + MiniMaxUnavailableError
  Task 16: TTSPipeline + macos_say_fallback
"""
from __future__ import annotations

import re

# --- Text preprocessor (ported inline from legacy core/tts_preprocessor.py) ---

# Emoji + symbol Unicode ranges that should be stripped from TTS input.
_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map symbols
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\U00002600-\U000027bf"  # dingbats
    "\U0001f1e6-\U0001f1ff"  # regional indicator (flags)
    "]+",
    flags=re.UNICODE,
)

_MARKDOWN_BOLD_RE = re.compile(r"\*\*(.*?)\*\*")
_MARKDOWN_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")


def _preprocess_for_speech(text: str) -> str:
    """Strip TTS-hostile chars (emoji, markdown markers) before synthesis.

    Verbatim behavior port of legacy `core/tts_preprocessor.py`. Does NOT
    apply content safety; the Pre-emit Gate has already vetted the text.
    """
    if not text:
        return ""
    out = _EMOJI_RE.sub("", text)
    out = _MARKDOWN_BOLD_RE.sub(r"\1", out)
    out = _MARKDOWN_LINK_RE.sub(r"\1", out)
    out = _MARKDOWN_ITALIC_RE.sub(r"\1", out)
    return out.strip()


__all__ = ["_preprocess_for_speech"]
