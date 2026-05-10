"""Helpers for splitting assistant output into voice and screen channels."""

from __future__ import annotations

from dataclasses import dataclass
import re


_CHANNEL_RE = re.compile(
    r"<(?P<tag>voice|document)>\s*(?P<body>.*?)\s*</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ResponseChannels:
    """Parsed assistant response channels."""

    raw: str
    voice: str
    document: str
    has_channels: bool


def parse_response_channels(text: str) -> ResponseChannels:
    """Parse ``<voice>`` and ``<document>`` sections from an assistant response.

    If the response is not channelized, return it as both voice and document so
    legacy callers keep working. When only one channel is present, the missing
    channel is an empty string.
    """
    raw = text or ""
    values: dict[str, str] = {}
    for match in _CHANNEL_RE.finditer(raw):
        tag = match.group("tag").lower()
        if tag not in values:
            values[tag] = match.group("body").strip()

    if not values:
        stripped = raw.strip()
        return ResponseChannels(
            raw=raw,
            voice=stripped,
            document=stripped,
            has_channels=False,
        )

    return ResponseChannels(
        raw=raw,
        voice=values.get("voice", ""),
        document=values.get("document", ""),
        has_channels=True,
    )
