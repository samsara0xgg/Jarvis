"""ADR-0005 voice_asr.normalize — 3-layer cascade."""
from __future__ import annotations

from jarvis.surface import voice_asr


def test_normalize_layer1_context_guarded_correction() -> None:
    """Layer-1 manual correction only fires when context word present."""
    norm = voice_asr.AsrNormalizer(
        corrections=[
            {"pattern": "大灯", "replace": "大厅", "require_context": ["客厅"]},
        ],
        aliases={},
        fuzzy_enabled=False,
    )
    assert norm.normalize("客厅大灯") == "客厅大厅"
    assert norm.normalize("打开大灯") == "打开大灯"  # no context — no replace


def test_normalize_layer2_structured_alias_longest_first() -> None:
    """Layer-2 alias replaces canonical; longest alias wins on overlap."""
    norm = voice_asr.AsrNormalizer(
        corrections=[],
        aliases={"卧室主灯": ["卧室灯", "卧室主灯"]},
        fuzzy_enabled=False,
    )
    assert norm.normalize("打开卧室主灯") == "打开卧室主灯"  # already canonical
    assert norm.normalize("打开卧室灯") == "打开卧室主灯"   # alias -> canonical


def test_normalize_layer3_fuzzy_disabled_by_default() -> None:
    """Layer-3 fuzzy is opt-in; off by default, near-homophones survive."""
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    assert norm.normalize("打开卧室登") == "打开卧室登"


def test_normalize_layer3_fuzzy_requires_action_verb() -> None:
    """Layer-3 Levenshtein fires only when action verb present + alias match."""
    norm = voice_asr.AsrNormalizer(
        corrections=[],
        aliases={"卧室主灯": ["卧室主灯"]},
        fuzzy_enabled=True,
    )
    # "登" differs from "灯" by 1 edit, "卧室主灯" has 4 chars.
    assert norm.normalize("打开卧室主登") == "打开卧室主灯"
    # No action verb — fuzzy stays off.
    assert norm.normalize("卧室主登") == "卧室主登"


def test_normalize_returns_unchanged_text_when_no_rules_apply() -> None:
    """Empty config + arbitrary text — passthrough."""
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    assert norm.normalize("现在几点") == "现在几点"


def test_normalize_handles_empty_string() -> None:
    """Empty input returns empty without crashing."""
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    assert norm.normalize("") == ""
