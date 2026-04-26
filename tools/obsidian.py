"""Obsidian tools — write voice-captured notes to the daily-capture inbox.

The single tool ``obsidian_add_to_inbox`` writes one markdown file per call,
named per the project Obsidian schema:

    YYYY-MM-DD-HHMM-<slug>.md

Slug rules (project ``_SCHEMA.md``):
- ASCII lowercase kebab-case, 1–5 words, ideally no digits.
- Pure-Chinese content has no clean ASCII slug; we fall back to the
  literal first chars of the cleaned text. macOS / iOS / linux all
  handle Unicode filenames fine, and these notes are always opened
  via Obsidian (which doesn't care).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from tools import jarvis_tool

LOGGER = logging.getLogger(__name__)

_inbox_dir: Path | None = None

_MAX_SLUG_WORDS = 5
_MAX_SLUG_CHARS = 60
_TITLE_SCAN_CHARS = 80


def init(inbox_dir: str = "~/Documents/Obsidian Vault/jarvis/inbox") -> None:
    """Configure the inbox directory; create it if missing."""
    global _inbox_dir
    _inbox_dir = Path(inbox_dir).expanduser()
    _inbox_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Obsidian inbox dir: %s", _inbox_dir)


def _make_slug(text: str) -> str:
    """Derive a filename slug from *text*.

    Tries an ASCII kebab first (matches the schema); falls back to a
    literal Chinese-character slice when no ASCII letters are present.
    """
    norm = unicodedata.normalize("NFKD", text)
    ascii_only = norm.encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[a-zA-Z]+", ascii_only.lower())
    if words:
        slug = "-".join(words[:_MAX_SLUG_WORDS])
        return slug[:_MAX_SLUG_CHARS]

    cleaned = re.sub(r"\s+", "", text)
    cleaned = re.sub(r"[\\/:*?\"<>|]", "", cleaned)
    cleaned = cleaned[:_MAX_SLUG_CHARS]
    return cleaned or "note"


def _build_path(now: datetime, slug: str) -> Path:
    """Compose the inbox file path for ``now``/``slug``."""
    assert _inbox_dir is not None
    stamp = now.strftime("%Y-%m-%d-%H%M")
    return _inbox_dir / f"{stamp}-{slug}.md"


@jarvis_tool(read_only=False)
def obsidian_add_to_inbox(content: str, title: str = "") -> str:
    """Save a voice-captured note to the Obsidian inbox.

    Use when the user asks to "remember", "记一下", "把…记到 inbox", or
    similar capture intents. Filename is derived from ``title`` if
    given, otherwise from the first words of ``content``.
    """
    if _inbox_dir is None:
        return "Inbox directory not initialized."

    body_text = content.strip()
    if not body_text:
        return "Cannot save an empty note."

    title_text = title.strip()
    slug_source = title_text or body_text[:_TITLE_SCAN_CHARS]
    slug = _make_slug(slug_source)

    path = _build_path(datetime.now(), slug)
    if title_text:
        markdown = f"# {title_text}\n\n{body_text}\n"
    else:
        markdown = f"{body_text}\n"
    path.write_text(markdown, encoding="utf-8")

    LOGGER.info("Inbox note written: %s", path.name)
    return f"Saved to inbox: {path.name}"
