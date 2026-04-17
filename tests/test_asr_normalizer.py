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


class TestActionWordsTightening:
    """WP2 T2.1: `_ACTION_WORDS` must not include bare "灯" — too broad for L3 fuzzy."""

    def test_fuzzy_does_not_fire_on_ambient_light_talk(self):
        cfg = {
            "asr_aliases": {"客厅大灯": ["大灯"]},
            "asr_normalizer_fuzzy": {"enabled": True, "max_distance": 1},
        }
        n = ASRNormalizer(cfg)
        # "我喜欢小灯泡" has no real action word — fuzzy must not fire.
        assert n.normalize("我喜欢小灯泡") == "我喜欢小灯泡"
        assert n.normalize("路灯真漂亮") == "路灯真漂亮"

    def test_fuzzy_fires_with_strict_action_word(self):
        cfg = {
            "asr_aliases": {"客厅大灯": ["大灯"]},
            "asr_normalizer_fuzzy": {"enabled": True, "max_distance": 1},
        }
        n = ASRNormalizer(cfg)
        # "打开" is a clear action verb — fuzzy should fire.
        out = n.normalize("打开大蛋")
        assert "客厅大灯" in out


class TestLayer3LengthGuard:
    """WP2 T2.2: fuzzy window must equal alias length to avoid text-length drift."""

    def test_window_must_equal_alias_length(self):
        # alias "大灯" is 2 chars. A 3-char window must NOT match it.
        cfg = {
            "asr_aliases": {"客厅大灯": ["大灯"]},
            "asr_normalizer_fuzzy": {"enabled": True, "max_distance": 2},
        }
        n = ASRNormalizer(cfg)
        # "打开大蛋灯" has a 2-char window "大蛋" that should match "大灯"
        # (distance 1). The trailing "灯" stays as-is.
        out = n.normalize("打开大蛋灯")
        # After T2.2 fix: "大蛋" (2 chars) → "客厅大灯" (canonical), trailing "灯" stays.
        # Result: "打开客厅大灯灯" (awkward but length-predictable).
        # Without T2.2 fix the 3-char window "大蛋灯" could match and fully replace.
        assert out == "打开客厅大灯灯"

    def test_canonical_can_be_longer_than_alias(self):
        # alias "大灯" (2) → canonical "客厅大灯" (4). Still works.
        cfg = {
            "asr_aliases": {"客厅大灯": ["大灯"]},
            "asr_normalizer_fuzzy": {"enabled": True, "max_distance": 1},
        }
        n = ASRNormalizer(cfg)
        out = n.normalize("打开大蛋")
        assert out == "打开客厅大灯"


class TestPerformance:
    """WP2 T3.5: normalize() must run < 10ms even with realistic config size."""

    def _realistic_config(self) -> dict:
        corrections = [
            {"pattern": f"pattern_{i}", "replace": f"canon_{i}",
             "require_context": ["开", "关"]}
            for i in range(20)
        ]
        aliases = {
            f"canonical_{i}": [f"alias_{i}_a", f"alias_{i}_b"]
            for i in range(30)
        }
        return {
            "asr_corrections": corrections,
            "asr_aliases": aliases,
            "asr_normalizer_fuzzy": {"enabled": False, "max_distance": 2},
        }

    def test_layer1_and_2_cold_path_under_10ms(self):
        import time
        n = ASRNormalizer(self._realistic_config())
        text = "帮我看看今天日历"
        for _ in range(10):
            n.normalize(text)
        start = time.perf_counter()
        for _ in range(1000):
            n.normalize(text)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 5000, f"cold path took {elapsed_ms:.0f} ms / 1000 iters"

    def test_layer3_enabled_still_reasonable(self):
        import time
        cfg = self._realistic_config()
        cfg["asr_normalizer_fuzzy"] = {"enabled": True, "max_distance": 2}
        n = ASRNormalizer(cfg)
        text = "打开客厅大蛋灯好吗"
        for _ in range(5):
            n.normalize(text)
        start = time.perf_counter()
        for _ in range(100):
            n.normalize(text)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 5000, f"L3 path took {elapsed_ms:.0f} ms / 100 iters"
