"""Unit tests for ``jarvis.surface.notify`` (Step 14 of ADR-0002).

All subprocess invocations are mocked — we never actually fire ``say``
or ``osascript`` from the test suite.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from jarvis.surface import notify as nf
from jarvis.surface.notify import (
    ATTENTION_CHANNEL_TO_SURFACES,
    _apple_escape,
    deliver_banner,
    deliver_voice,
)

# ---------------------------------------------------------------------------
# deliver_voice
# ---------------------------------------------------------------------------


def test_deliver_voice_spawns_say_with_tingting() -> None:
    """Default voice is ``Tingting``; argv shape matches the contract."""
    with patch.object(nf.subprocess, "Popen") as mock_popen:
        deliver_voice("你好世界")
    mock_popen.assert_called_once_with(
        ["say", "-v", "Tingting", "你好世界"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_deliver_voice_custom_voice() -> None:
    """The ``voice`` kwarg overrides the default."""
    with patch.object(nf.subprocess, "Popen") as mock_popen:
        deliver_voice("test", voice="Daniel")
    assert mock_popen.call_args[0][0] == ["say", "-v", "Daniel", "test"]


def test_deliver_voice_empty_string_noop() -> None:
    """Empty text does not spawn a subprocess."""
    with patch.object(nf.subprocess, "Popen") as mock_popen:
        deliver_voice("")
    mock_popen.assert_not_called()


def test_deliver_voice_whitespace_only_noop() -> None:
    """Whitespace-only text does not spawn a subprocess."""
    with patch.object(nf.subprocess, "Popen") as mock_popen:
        deliver_voice("   \n\t")
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# deliver_banner — shape
# ---------------------------------------------------------------------------


def test_deliver_banner_short_body() -> None:
    """Short body renders inline in the AppleScript with the title."""
    with patch.object(nf.subprocess, "run") as mock_run:
        deliver_banner("Jarvis", "task done")
    args, kwargs = mock_run.call_args
    argv = args[0]
    assert argv[0] == "/usr/bin/osascript"
    assert argv[1] == "-e"
    script = argv[2]
    assert 'display notification "task done"' in script
    assert 'with title "Jarvis"' in script
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 2
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL


def test_deliver_banner_both_empty_noop() -> None:
    """Empty/whitespace title and body together suppress the call."""
    with patch.object(nf.subprocess, "run") as mock_run:
        deliver_banner("", "")
        deliver_banner("   ", "\n\t")
    mock_run.assert_not_called()


def test_deliver_banner_empty_body_still_fires_when_title_present() -> None:
    """Non-empty title alone is enough reason to render the banner."""
    with patch.object(nf.subprocess, "run") as mock_run:
        deliver_banner("Jarvis", "")
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# deliver_banner — truncation
# ---------------------------------------------------------------------------


def test_deliver_banner_truncates_at_240() -> None:
    """Body > 240 chars is truncated to 239 chars + an ellipsis."""
    with patch.object(nf.subprocess, "run") as mock_run:
        deliver_banner("J", "x" * 500)
    script = mock_run.call_args[0][0][2]
    # Body sits between the first pair of double quotes after the
    # ``display notification `` literal.
    prefix = 'display notification "'
    start = script.index(prefix) + len(prefix)
    end = script.index('"', start)
    rendered_body = script[start:end]
    assert rendered_body == "x" * 239 + "…"
    assert len(rendered_body) == 240


def test_deliver_banner_no_truncation_at_exactly_240() -> None:
    """Body of exactly 240 chars is rendered verbatim with no ellipsis."""
    with patch.object(nf.subprocess, "run") as mock_run:
        deliver_banner("J", "y" * 240)
    script = mock_run.call_args[0][0][2]
    assert "…" not in script
    assert "y" * 240 in script


def test_deliver_banner_no_truncation_below_threshold() -> None:
    """Body below the threshold is rendered verbatim."""
    with patch.object(nf.subprocess, "run") as mock_run:
        deliver_banner("J", "z" * 239)
    script = mock_run.call_args[0][0][2]
    assert "…" not in script
    assert "z" * 239 in script


def test_deliver_banner_custom_max_body_chars() -> None:
    """Caller can override the truncation threshold."""
    with patch.object(nf.subprocess, "run") as mock_run:
        deliver_banner("J", "abcdef", max_body_chars=3)
    script = mock_run.call_args[0][0][2]
    # 2 chars + ellipsis = 3 chars total.
    assert '"ab…"' in script


# ---------------------------------------------------------------------------
# deliver_banner — escaping
# ---------------------------------------------------------------------------


def test_deliver_banner_escapes_double_quotes_in_body() -> None:
    """Double quotes in the body are backslash-escaped for AppleScript."""
    with patch.object(nf.subprocess, "run") as mock_run:
        deliver_banner("J", 'said "hi"')
    script = mock_run.call_args[0][0][2]
    assert '\\"hi\\"' in script


def test_deliver_banner_escapes_backslash_in_body() -> None:
    """Backslashes in the body are doubled for AppleScript."""
    with patch.object(nf.subprocess, "run") as mock_run:
        deliver_banner("J", "path\\here")
    script = mock_run.call_args[0][0][2]
    assert "path\\\\here" in script


def test_deliver_banner_escapes_quotes_in_title() -> None:
    """Title escaping uses the same AppleScript rules as the body."""
    with patch.object(nf.subprocess, "run") as mock_run:
        deliver_banner('title "quoted"', "body")
    script = mock_run.call_args[0][0][2]
    assert 'with title "title \\"quoted\\""' in script


def test_apple_escape_handles_backslash_before_quote() -> None:
    """Backslashes must be escaped first, otherwise the quote escape would re-escape."""
    assert _apple_escape('a\\"b') == '"a\\\\\\"b"'


def test_apple_escape_empty_string() -> None:
    """Empty string renders as an empty AppleScript string literal."""
    assert _apple_escape("") == '""'


def test_apple_escape_plain_text() -> None:
    """Text with no special characters renders verbatim inside quotes."""
    assert _apple_escape("hello") == '"hello"'


# ---------------------------------------------------------------------------
# Attention channel → surface mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "surfaces"),
    [
        ("silent_log",     ()),
        ("queue_review",   ("cli_stdout",)),
        ("badge_card",     ("osascript_banner_title_only",)),
        ("soft_suggest",   ("cli_stdout",)),
        ("voice_notify",   ("say", "osascript_banner", "cli_stdout")),
        ("interrupt_now",  ("say_bell", "osascript_banner")),
        ("ask_confirm",    ("osascript_banner", "cli_stdout")),
        ("delegate_agent", ()),
        ("suppress",       ()),
    ],
)
def test_attention_channel_mapping_complete(
    channel: str, surfaces: tuple[str, ...]
) -> None:
    """Every Day-2 attention channel maps to the exact surface tuple from the ADR."""
    assert ATTENTION_CHANNEL_TO_SURFACES[channel] == surfaces


def test_attention_channel_mapping_has_all_9_channels() -> None:
    """Mac-only architecture drops ``ambient_act``, leaving 9 logical channels."""
    assert len(ATTENTION_CHANNEL_TO_SURFACES) == 9
