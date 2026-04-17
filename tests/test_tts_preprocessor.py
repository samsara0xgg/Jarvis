"""Tests for core.tts_preprocessor."""

from __future__ import annotations

from core import tts_preprocessor


class TestRemoveSpecialChar:
    def test_strips_emoji(self):
        assert tts_preprocessor.clean("好的 😊 收到") == "好的 收到"

    def test_keeps_punctuation(self):
        # Punctuation must survive (TTS uses it for prosody). NFKC normalizes
        # full-width punctuation to ASCII; either form is fine for TTS.
        out = tts_preprocessor.clean("好的，知道了。")
        assert "好的" in out and "知道了" in out
        assert any(c in out for c in (",", "，"))
        assert any(c in out for c in (".", "。"))

    def test_disabled_keeps_emoji(self):
        cfg = {"remove_special_char": False, "ignore_brackets": False,
               "ignore_parentheses": False, "ignore_asterisks": False,
               "ignore_angle_brackets": False}
        assert tts_preprocessor.clean("好的 😊", cfg) == "好的 😊"


class TestBrackets:
    def test_strips_brackets_content(self):
        assert tts_preprocessor.clean("好的 [开心] 收到") == "好的 收到"

    def test_handles_nested_brackets(self):
        assert tts_preprocessor.clean("外层 [a [b] c] 收到") == "外层 收到"

    def test_unmatched_closer_kept(self):
        # Avoid silently eating chars; a stray ']' should pass through.
        out = tts_preprocessor.clean("好的] 收到")
        assert "]" in out and "好的" in out and "收到" in out

    def test_strips_chinese_tortoise_brackets(self):
        # 【xxx】 should be stripped when ignore_brackets is on.
        assert tts_preprocessor.clean("【开心】正文") == "正文"


class TestParentheses:
    def test_strips_ascii_parens(self):
        assert tts_preprocessor.clean("好的 (旁白) 收到") == "好的 收到"

    def test_strips_chinese_parens(self):
        assert tts_preprocessor.clean("好的 （旁白） 收到") == "好的 收到"

    def test_strips_mixed(self):
        assert (
            tts_preprocessor.clean("一(英) 二（中） 三")
            == "一 二 三"
        )


class TestAsterisks:
    def test_strips_emphasis(self):
        assert tts_preprocessor.clean("好的 *强调* 收到") == "好的 收到"

    def test_strips_double_asterisk(self):
        assert tts_preprocessor.clean("好的 **粗体** 收到") == "好的 收到"


class TestAngleBrackets:
    def test_strips_tags(self):
        assert tts_preprocessor.clean("好的 <break time='1s'/> 收到") == "好的 收到"

    def test_strips_chinese_angle_brackets(self):
        # 〈xxx〉 (U+3008/U+3009) — chinese book title marks — stripped.
        assert tts_preprocessor.clean("〈标签〉正文") == "正文"

    def test_strips_math_angle_brackets_after_nfkc(self):
        # ⟨xxx⟩ (U+27E8/U+27E9) — math angle brackets. NFKC leaves these
        # unchanged (they do NOT collapse to ASCII <>), so filter must list
        # this pair explicitly to strip them.
        assert tts_preprocessor.clean("⟨标签⟩正文") == "正文"


class TestCurrencyAndDigits:
    """WP3 T1.2 + T3.1: Sc (currency) whitelist + digit/latin preservation."""

    def test_preserves_currency_symbols(self):
        # ¥ $ € £ ￥ are Unicode category Sc — must survive the filter.
        out = tts_preprocessor.clean("¥100 花了 $5 换 €3")
        assert "¥" in out and "$" in out and "€" in out
        assert "100" in out and "5" in out and "3" in out

    def test_drops_emoji_but_keeps_currency(self):
        # Emoji (So) dropped, currency (Sc) kept.
        out = tts_preprocessor.clean("😊 ¥100")
        assert "😊" not in out
        assert "¥" in out and "100" in out

    def test_preserves_digits_and_latin(self):
        out = tts_preprocessor.clean("ABC 123 abc")
        assert out == "ABC 123 abc"

    def test_drops_math_symbols(self):
        # Sm (math) stays dropped by design (the whitelist only adds Sc).
        # Input "1+2" — `+` is Sm, should drop.
        out = tts_preprocessor.clean("1+2=3")
        assert "+" not in out and "=" not in out
        assert "1" in out and "2" in out and "3" in out


class TestComposite:
    def test_plan_example(self):
        # From the plan §4.4 acceptance test: should leave only "这是正文"
        # (Chinese version - ASCII-period after 正文 is OK; the plan said TTS only "念"这是正文,
        # which we interpret as "all wrapped content stripped").
        out = tts_preprocessor.clean("好的 😊 [开心] *强调* <标签> (旁白) 这是正文")
        assert out == "好的 这是正文"

    def test_independent_toggles(self):
        # Disable only brackets — emoji should still be stripped, brackets retained.
        cfg = {"remove_special_char": True, "ignore_brackets": False,
               "ignore_parentheses": True, "ignore_asterisks": True,
               "ignore_angle_brackets": True}
        out = tts_preprocessor.clean("😊 [keep this]", cfg)
        assert out == "[keep this]"

    def test_empty_string(self):
        assert tts_preprocessor.clean("") == ""

    def test_collapses_whitespace(self):
        # Filtering can leave behind multiple spaces; they should collapse.
        out = tts_preprocessor.clean("a    b")
        assert out == "a b"
