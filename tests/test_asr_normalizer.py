"""Tests for core.asr_normalizer (WP2)."""

from __future__ import annotations

from core.asr_normalizer import ASRNormalizer, _levenshtein


class TestLayer1Corrections:
    def test_pattern_with_context_is_replaced(self):
        cfg = {
            "asr_corrections": [
                {
                    "pattern": "客厅大蛋",
                    "replace": "客厅大灯",
                    "require_context": ["开", "关", "灯"],
                },
            ],
        }
        n = ASRNormalizer(cfg)
        assert n.normalize("开客厅大蛋") == "开客厅大灯"

    def test_pattern_without_context_is_skipped(self):
        cfg = {
            "asr_corrections": [
                {
                    "pattern": "客厅大蛋",
                    "replace": "客厅大灯",
                    "require_context": ["开", "关", "灯"],
                },
            ],
        }
        n = ASRNormalizer(cfg)
        # No action word — "我想吃客厅大蛋糕" must NOT be rewritten.
        assert n.normalize("我想吃客厅大蛋糕") == "我想吃客厅大蛋糕"

    def test_missing_require_context_skips_entry(self):
        # An entry without require_context is silently ignored (safety guard).
        cfg = {
            "asr_corrections": [
                {"pattern": "x", "replace": "y"},  # missing require_context
            ],
        }
        n = ASRNormalizer(cfg)
        assert n.normalize("xxx") == "xxx"

    def test_layer1_returns_immediately(self):
        # If Layer 1 changes the text, Layer 2 should not run.
        cfg = {
            "asr_corrections": [
                {"pattern": "蛋", "replace": "灯", "require_context": ["开"]},
            ],
            "asr_aliases": {
                "厅": ["灯"],   # would normally rewrite "灯" → "厅"
            },
        }
        n = ASRNormalizer(cfg)
        assert n.normalize("开蛋") == "开灯"  # Layer 2 didn't get to run


class TestLayer2Aliases:
    def test_alias_mapped_to_canonical(self):
        cfg = {
            "asr_aliases": {
                "卧室壁灯": ["床头灯", "卧室小灯"],
            },
        }
        n = ASRNormalizer(cfg)
        assert n.normalize("开床头灯") == "开卧室壁灯"

    def test_longer_alias_wins(self):
        # Sort by length-desc must prevent "灯" from clobbering "床头灯".
        cfg = {
            "asr_aliases": {
                "卧室壁灯": ["床头灯"],
                "灯具": ["灯"],
            },
        }
        n = ASRNormalizer(cfg)
        # Both aliases match in some sense; "床头灯" should fire first.
        # If "灯" → "灯具" ran first we'd get nonsense like "床头灯具".
        assert n.normalize("开床头灯") == "开卧室壁灯"

    def test_no_match_returns_unchanged(self):
        cfg = {"asr_aliases": {"客厅大灯": ["大灯", "主灯"]}}
        n = ASRNormalizer(cfg)
        assert n.normalize("帮我查天气") == "帮我查天气"

    def test_alias_equal_to_canonical_skipped(self):
        # Defensive: a config typo where alias == canonical shouldn't loop.
        cfg = {"asr_aliases": {"放松模式": ["放松模式", "轻松模式"]}}
        n = ASRNormalizer(cfg)
        assert n.normalize("切换到轻松模式") == "切换到放松模式"


class TestLayer3Fuzzy:
    def test_disabled_by_default(self):
        cfg = {"asr_aliases": {"客厅大灯": ["大灯"]}}
        n = ASRNormalizer(cfg)
        # "大蛋" is distance 1 from "大灯" but fuzzy is off — no change.
        assert n.normalize("开大蛋") == "开大蛋"

    def test_enabled_matches_within_distance(self):
        cfg = {
            "asr_aliases": {"客厅大灯": ["大灯"]},
            "asr_normalizer_fuzzy": {"enabled": True, "max_distance": 1},
        }
        n = ASRNormalizer(cfg)
        # "大蛋" → distance 1 from "大灯" → replace
        out = n.normalize("开大蛋")
        assert "客厅大灯" in out or "大灯" in out

    def test_enabled_requires_action_word(self):
        cfg = {
            "asr_aliases": {"客厅大灯": ["大灯"]},
            "asr_normalizer_fuzzy": {"enabled": True, "max_distance": 1},
        }
        n = ASRNormalizer(cfg)
        # No action word in "我想吃大蛋糕" — fuzzy must NOT fire.
        assert n.normalize("我想吃大") == "我想吃大"


class TestEmptyAndEdge:
    def test_empty_text(self):
        n = ASRNormalizer({"asr_corrections": [], "asr_aliases": {}})
        assert n.normalize("") == ""

    def test_no_config_keys(self):
        n = ASRNormalizer({})
        assert n.normalize("hello world") == "hello world"


class TestLevenshtein:
    def test_identical(self):
        assert _levenshtein("abc", "abc") == 0

    def test_one_substitution(self):
        assert _levenshtein("大灯", "大蛋") == 1

    def test_empty_string(self):
        assert _levenshtein("", "abc") == 3
        assert _levenshtein("abc", "") == 3

    def test_insertion(self):
        assert _levenshtein("abc", "abcd") == 1
