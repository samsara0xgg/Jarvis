"""Mac notification helpers — voice (``say``) + banner (``osascript``).

Per ADR-0002 § Notification contract + § Attention channel → physical
surface mapping. Day-2 wiring into the surface render lands in Step 18.

References:
- ADR-0002 § Notification contract (lines 1002-1015)
- ADR-0002 § Attention channel → physical surface mapping (lines 1017-1044)
- jarvis-legacy/core/media_ducking.py:151-159 (osascript shape)
"""

from __future__ import annotations

import subprocess
from typing import Final

# Per ADR § Attention channel → physical surface mapping (Day-2 Mac-only:
# 9 logical channels mapped to physical surfaces). The mapping is the
# authoritative source for jarvis/surface/cli_render.py (Step 18) to consult
# when deciding which deliver_* helpers to fire per channel.
ATTENTION_CHANNEL_TO_SURFACES: Final[dict[str, tuple[str, ...]]] = {
    "silent_log":     (),
    "queue_review":   ("cli_stdout",),
    "badge_card":     ("osascript_banner_title_only",),  # banner is badge surrogate
    "soft_suggest":   ("cli_stdout",),
    "voice_notify":   ("say", "osascript_banner", "cli_stdout"),
    "interrupt_now":  ("say_bell", "osascript_banner"),  # bell tone variant
    "ask_confirm":    ("osascript_banner", "cli_stdout"),
    "delegate_agent": (),  # internal action only
    "suppress":       (),
}

_DEFAULT_VOICE: Final[str] = "Tingting"
_MAX_BANNER_BODY_CHARS: Final[int] = 240


def deliver_voice(text: str, *, voice: str = _DEFAULT_VOICE) -> None:
    """Fire-and-forget ``say`` subprocess. Returns immediately.

    Day-2 has no TTS preprocessing; the text is spoken verbatim. Empty
    text is a no-op (don't spawn an empty ``say``).
    """
    if not text.strip():
        return
    subprocess.Popen(  # noqa: S603 — `say` is the macOS API contract.
        ["say", "-v", voice, text],  # noqa: S607 — PATH lookup is the contract.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def deliver_banner(
    title: str,
    body: str,
    *,
    max_body_chars: int = _MAX_BANNER_BODY_CHARS,
) -> None:
    r"""Synchronous ``osascript display notification``. Truncates body.

    Day-2: body > max_body_chars is truncated with a trailing ellipsis
    (the ellipsis counts toward the cap so the final string is exactly
    ``max_body_chars`` characters). Title is rendered verbatim (no
    truncation — title is short).

    Escape rules (per jarvis-legacy/core/media_ducking.py:151-159): we
    pass the AppleScript via ``-e``, so the script body is the full
    AppleScript source. Inside that source the text literals are
    AppleScript double-quoted strings; ``\`` and ``"`` must be
    backslash-escaped.
    """
    if not body.strip() and not title.strip():
        return
    body_truncated = (
        body[: max_body_chars - 1] + "…" if len(body) > max_body_chars else body
    )
    script = (
        f"display notification {_apple_escape(body_truncated)} "
        f"with title {_apple_escape(title)}"
    )
    subprocess.run(  # noqa: S603 — fixed argv; osascript is the API contract.
        ["/usr/bin/osascript", "-e", script],
        check=False,
        timeout=2,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _apple_escape(text: str) -> str:
    r"""Render ``text`` as an AppleScript double-quoted string literal.

    AppleScript strings use double quotes; ``\`` and ``"`` must be
    escaped. Newlines stay as literal newlines (AppleScript handles
    them).
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


__all__ = [
    "ATTENTION_CHANNEL_TO_SURFACES",
    "deliver_banner",
    "deliver_voice",
]
