"""Tests for tools/obsidian.py — obsidian_add_to_inbox skill."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from tools import _TOOL_REGISTRY


@pytest.fixture(autouse=True)
def _ensure_registered():
    """Re-register the tool if a prior test cleared the global registry."""
    import tools.obsidian as ob
    if "obsidian_add_to_inbox" not in _TOOL_REGISTRY:
        importlib.reload(ob)
    yield


import tools.obsidian as ob  # noqa: E402


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    """Init obsidian with a fresh tmp inbox dir; yield the dir."""
    target = tmp_path / "inbox"
    ob.init(inbox_dir=str(target))
    return target


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_obsidian_add_to_inbox_registered():
    assert "obsidian_add_to_inbox" in _TOOL_REGISTRY


def test_obsidian_add_to_inbox_not_read_only():
    assert _TOOL_REGISTRY["obsidian_add_to_inbox"]["read_only"] is False


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


def test_init_creates_missing_dir(tmp_path: Path):
    target = tmp_path / "deep" / "inbox"
    assert not target.exists()
    ob.init(inbox_dir=str(target))
    assert target.is_dir()


def test_init_expands_user(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    ob.init(inbox_dir="~/myinbox")
    assert (tmp_path / "myinbox").is_dir()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_empty_content_rejected(inbox: Path):
    msg = ob.obsidian_add_to_inbox(content="")
    assert "empty" in msg.lower()
    assert list(inbox.iterdir()) == []


def test_whitespace_only_rejected(inbox: Path):
    msg = ob.obsidian_add_to_inbox(content="   \n\t  ")
    assert "empty" in msg.lower()
    assert list(inbox.iterdir()) == []


def test_uninitialized_returns_error(monkeypatch):
    monkeypatch.setattr(ob, "_inbox_dir", None)
    msg = ob.obsidian_add_to_inbox(content="hi")
    assert "not initialized" in msg.lower()


# ---------------------------------------------------------------------------
# Slug derivation
# ---------------------------------------------------------------------------


def test_ascii_title_produces_kebab_slug(inbox: Path):
    msg = ob.obsidian_add_to_inbox(content="body", title="Dentist Reminder")
    assert "dentist-reminder" in msg
    files = list(inbox.iterdir())
    assert len(files) == 1
    assert "dentist-reminder" in files[0].name


def test_slug_caps_at_five_words(inbox: Path):
    ob.obsidian_add_to_inbox(
        content="body", title="one two three four five six seven"
    )
    files = list(inbox.iterdir())
    assert len(files) == 1
    name = files[0].stem
    # Drop the timestamp prefix YYYY-MM-DD-HHMM (4 dash-separated segments)
    slug = "-".join(name.split("-")[4:])
    assert slug == "one-two-three-four-five"


def test_chinese_title_falls_back_to_literal(inbox: Path):
    ob.obsidian_add_to_inbox(content="body", title="提醒看牙医")
    files = list(inbox.iterdir())
    assert len(files) == 1
    assert "提醒看牙医" in files[0].name


def test_no_title_derives_slug_from_content(inbox: Path):
    ob.obsidian_add_to_inbox(content="meeting notes from monday")
    files = list(inbox.iterdir())
    assert len(files) == 1
    name = files[0].stem
    slug = "-".join(name.split("-")[4:])
    assert slug == "meeting-notes-from-monday"


def test_no_ascii_no_title_chinese_content(inbox: Path):
    ob.obsidian_add_to_inbox(content="提醒自己明天去看牙医")
    files = list(inbox.iterdir())
    assert len(files) == 1
    # filename should keep at least the leading Chinese chars
    assert files[0].name.endswith(".md")
    assert "提醒" in files[0].name


# ---------------------------------------------------------------------------
# Filename pattern + content
# ---------------------------------------------------------------------------


_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}-")


def test_filename_starts_with_timestamp(inbox: Path):
    ob.obsidian_add_to_inbox(content="hello world")
    fname = next(inbox.iterdir()).name
    assert _STAMP_RE.match(fname)
    assert fname.endswith(".md")


def test_content_without_title_is_raw(inbox: Path):
    ob.obsidian_add_to_inbox(content="just the body")
    fname = next(inbox.iterdir())
    text = fname.read_text(encoding="utf-8")
    assert text == "just the body\n"


def test_content_with_title_is_h1_then_body(inbox: Path):
    ob.obsidian_add_to_inbox(content="body line", title="My Title")
    fname = next(inbox.iterdir())
    text = fname.read_text(encoding="utf-8")
    assert text == "# My Title\n\nbody line\n"


def test_strips_surrounding_whitespace(inbox: Path):
    ob.obsidian_add_to_inbox(content="  hello  \n", title="  Tag  ")
    fname = next(inbox.iterdir())
    text = fname.read_text(encoding="utf-8")
    assert text == "# Tag\n\nhello\n"
