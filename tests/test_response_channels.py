"""Tests for assistant voice/document channel parsing."""

from __future__ import annotations

from core.response_channels import parse_response_channels


def test_parse_voice_and_document_channels():
    parsed = parse_response_channels(
        "<voice>我写好了，代码放在屏幕上。</voice>\n"
        "<document>\n```python\nprint('ok')\n```\n</document>"
    )

    assert parsed.has_channels is True
    assert parsed.voice == "我写好了，代码放在屏幕上。"
    assert "print('ok')" in parsed.document


def test_parse_legacy_response_as_both_channels():
    parsed = parse_response_channels("好的，已经记录。")

    assert parsed.has_channels is False
    assert parsed.voice == "好的，已经记录。"
    assert parsed.document == "好的，已经记录。"


def test_missing_document_stays_empty():
    parsed = parse_response_channels("<voice>我需要确认一个信息。</voice>")

    assert parsed.has_channels is True
    assert parsed.voice == "我需要确认一个信息。"
    assert parsed.document == ""
