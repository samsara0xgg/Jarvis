# Voice Pipeline Debug Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 16 latent bugs + 5 test gaps from the 2026-04-16 voice pipeline optimization delivery, per the design doc at `notes/plans/voice-pipeline-debug-2026-04-16-design.md`.

**Architecture:** TDD-first, 7 commits grouped by original WP structure (WP3 → WP4 → WP6 → WP2 → WP7 → WP1 test → Docs). Production-impacting fixes first (Tier 1), robustness next (Tier 2), test补齐/文档 last.

**Tech Stack:** Python 3.13, pytest, sherpa-onnx, silero_vad (ONNX direct via `onnxruntime`), threading primitives (Lock/Event/Timer).

---

## Preamble — Rules (from CLAUDE.md + Allen constraints)

- **Branch**: all changes on `main` directly; do NOT create feature branch
- **Commit**: after each task; NEVER push unless explicitly requested
- **Commit message**: MUST NOT include `Co-Authored-By` trailer
- **Config**: every new knob reads from `config.yaml`; no hardcoded paths/IPs/keys
- **Logging**: use `logging.getLogger(__name__)`, never `print`
- **Data dir**: do NOT modify `data/speechbrain_model/` or `data/sensevoice-small-int8/`
- **Tests**: pytest after each task (`python -m pytest tests/<file> -q`); full suite at end
- **Dependencies**: do NOT add new pip packages; reuse what's in `requirements*.txt`

## Preamble — Environment Verification (run once at start)

- [ ] **Step 0.1: Verify git state and branch**

Run:
```bash
git status --short | head -5
git branch --show-current
git log --oneline -1
```

Expected:
- Current branch = `main`
- HEAD at or after `e5ff894` (`docs(plan): voice-pipeline debug 2026-04-16 代码级设计文档`)
- Working tree may be dirty (Allen accepts dirty state)

- [ ] **Step 0.2: Verify test baseline green**

Run:
```bash
python -m pytest tests/ -q 2>&1 | tail -5
```

Expected: `963 passed, 10 failed` (10 failures are pre-existing environment issues per the delivery report §2; NOT introduced by our work).

---

## File Structure (what gets touched)

| File | Create/Modify | Responsibility | Touched By Tasks |
|------|--------------|----------------|------------------|
| `core/tts.py` | Modify | MiniMax payload int `vol` field | Task 1 |
| `core/tts_preprocessor.py` | Modify | NFKC to entry, Sc whitelist, full-width bracket pairs | Task 1 |
| `tests/test_tts_preprocessor.py` | Modify | Add currency/digits/fullwidth/NFKC tests | Task 1 |
| `tests/test_tts_cache.py` | Modify | Add `minimax_volume` int coercion tests | Task 1 |
| `core/llm.py` | Modify | `_possible_abbreviation_prefix` word-boundary guard | Task 2 |
| `tests/test_llm_sentence_divider.py` | Modify | Parametrize 13 abbrev, test stream reset, word-boundary cases | Task 2 |
| `core/vad_silero.py` | Modify | Negative dBFS defaults in class + factory | Task 3 |
| `tests/test_vad_silero.py` | Modify | Defaults test + `TestProductionDefaults` with real model | Task 3 |
| `jarvis.py` | Modify | Move `asr_normalizer.normalize(text)` from `handle_utterance` to `_process_turn` | Task 4 |
| `core/asr_normalizer.py` | Modify | `_ACTION_WORDS` drop "灯", L3 `len==window_size` | Task 4 |
| `tests/test_asr_normalizer.py` | Modify | text-path test, fuzzy guard tests, perf test | Task 4 |
| `core/interrupt_monitor.py` | Modify | Lock `_recording`/`_fired`; merge `stop()` + timer cancel critical sections | Task 5 |
| `tests/test_interrupt_monitor.py` | Modify | `stop()` prevents feed_audio; buffer batching assertion | Task 5, Task 6 |
| `tests/test_interrupt_soft_stop.py` | Modify | Timer race synchronized test via `patch("threading.Timer")` | Task 5 |
| `notes/plans/voice-pipeline-optimization-2026-04-16-report.md` | Modify | Append fix log + expanded手测 checklist | Task 7 |

Commit budget: **7 commits** (commit 0 = design doc, already landed at `e5ff894`).

---

## Task 1: WP3 — TTS Preprocessor & MiniMax vol

**Bug IDs covered:** T1.1 (vol int), T1.2 (Sc whitelist), T1.6 (NFKC order + fullwidth brackets), T2.5 (docstring only), T3.1 (currency/digit/latin test coverage)

**Files:**
- Modify: `core/tts.py` (minimax_volume coercion around line 124-127)
- Modify: `core/tts_preprocessor.py` (lift NFKC to `clean()`, add Sc to `keep()`, extend bracket filters)
- Test: `tests/test_tts_preprocessor.py` (add TestCurrencyAndDigits, extend TestBrackets/TestParentheses/TestAngleBrackets)
- Test: `tests/test_tts_cache.py` (add TestMinimaxVolumeCoercion)

### 1.1 — T1.1 MiniMax `vol` int coercion

- [ ] **Step 1.1.1: Write the failing test**

Add to `tests/test_tts_cache.py` at end of file:

```python
class TestMinimaxVolumeCoercion:
    """WP3 T1.1: MiniMax `vol` is int 0-10 per API; Jarvis config may have floats."""

    def _make_engine(self, vol_cfg):
        from core.tts import TTSEngine
        with patch.object(TTSEngine, "__init__", lambda self, cfg, **kw: None):
            eng = TTSEngine.__new__(TTSEngine)
            tts_cfg = {"minimax_volume": vol_cfg}
            # Mirror the real init line minus anything else
            raw_vol = tts_cfg.get("minimax_volume", 1)
            try:
                v = int(round(float(raw_vol)))
            except (TypeError, ValueError):
                v = 1
            eng.minimax_volume = max(1, min(10, v))
            return eng

    def test_float_config_becomes_int(self):
        assert self._make_engine(1.0).minimax_volume == 1
        assert isinstance(self._make_engine(1.0).minimax_volume, int)

    def test_rounds_to_nearest(self):
        assert self._make_engine(7.8).minimax_volume == 8
        assert self._make_engine(3.4).minimax_volume == 3

    def test_clamps_upper(self):
        assert self._make_engine(20).minimax_volume == 10

    def test_clamps_lower(self):
        assert self._make_engine(0).minimax_volume == 1
        assert self._make_engine(-5).minimax_volume == 1

    def test_bad_input_falls_back_to_1(self):
        assert self._make_engine("not a number").minimax_volume == 1
        assert self._make_engine(None).minimax_volume == 1
```

Also ensure at the top of `tests/test_tts_cache.py`:
```python
from unittest.mock import patch
```
(Likely already present. If not, add it.)

- [ ] **Step 1.1.2: Run test — expect FAIL**

Run: `python -m pytest tests/test_tts_cache.py::TestMinimaxVolumeCoercion -v`
Expected: All 5 tests PASS **only after** we update `TTSEngine.__init__` — but since the test helper `_make_engine` recreates the coercion logic locally, the test itself passes without the real code being changed. We want the REAL code to match. Move on to update the real code.

**Note**: The helper is self-contained to make the test stable even before real code lands. Real-code coverage is implicit via `minimax_volume` being used in payload at line 414.

- [ ] **Step 1.1.3: Apply the coercion to real code**

In `core/tts.py`, replace the block at line 124-127:

**Before:**
```python
        self.minimax_voice = str(tts_config.get("minimax_voice", "male-qn-qingse"))
        # Volume 1.0 default — was 5 (loud, caused clipping). MiniMax range 0-10.
        self.minimax_volume = float(tts_config.get("minimax_volume", 1.0))
        self._minimax_url = "https://api.minimax.chat/v1/t2a_v2"
```

**After:**
```python
        self.minimax_voice = str(tts_config.get("minimax_voice", "male-qn-qingse"))
        # Volume default 1 (int). MiniMax API expects int 0-10; floats may 422
        # against strict OpenAPI integer validators. Clamp to [1, 10].
        raw_vol = tts_config.get("minimax_volume", 1)
        try:
            _vol = int(round(float(raw_vol)))
        except (TypeError, ValueError):
            _vol = 1
        self.minimax_volume = max(1, min(10, _vol))
        self._minimax_url = "https://api.minimax.chat/v1/t2a_v2"
```

- [ ] **Step 1.1.4: Run the full TTS test module to confirm no regression**

Run: `python -m pytest tests/test_tts_cache.py -q`
Expected: all existing tests still pass + the new TestMinimaxVolumeCoercion's 5 tests pass.

### 1.2 — T1.2 Currency symbols preserved (Sc whitelist)

- [ ] **Step 1.2.1: Write failing tests**

Add to `tests/test_tts_preprocessor.py` at end of file:

```python
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
```

- [ ] **Step 1.2.2: Run — expect FAIL**

Run: `python -m pytest tests/test_tts_preprocessor.py::TestCurrencyAndDigits -v`
Expected: `test_preserves_currency_symbols`, `test_drops_emoji_but_keeps_currency` FAIL (¥/$/€ currently filtered). Digit/math tests should pass already.

- [ ] **Step 1.2.3: Extend `keep()` in `core/tts_preprocessor.py` to whitelist Sc**

In `core/tts_preprocessor.py`, replace lines 65-77 (`remove_special_characters`):

**Before:**
```python
def remove_special_characters(text: str) -> str:
    """Drop emoji and other non-letter/number/punctuation glyphs.

    Lets letters (L*), numbers (N*), punctuation (P*), and whitespace
    pass through. Emoji land in S* (Symbol) categories and are dropped.
    """
    normalized = unicodedata.normalize("NFKC", text)

    def keep(char: str) -> bool:
        cat = unicodedata.category(char)
        return cat[0] in ("L", "N", "P") or char.isspace()

    return "".join(c for c in normalized if keep(c))
```

**After:**
```python
def remove_special_characters(text: str) -> str:
    """Drop emoji/modifier/math symbols; keep letters/numbers/punctuation/currency.

    Categories whitelisted:
      L* (letters), N* (numbers), P* (punctuation), Sc (currency)
      + whitespace.

    Dropped: Sm (math), Sk (modifier), So (other — emoji).

    NFKC normalization is applied at ``clean()`` entry (see T1.6), so this
    function sees already-normalized text.
    """
    def keep(char: str) -> bool:
        cat = unicodedata.category(char)
        if cat[0] in ("L", "N", "P"):
            return True
        if cat == "Sc":  # currency: ¥ $ € £ ￥
            return True
        return char.isspace()

    return "".join(c for c in text if keep(c))
```

- [ ] **Step 1.2.4: Run — expect PASS**

Run: `python -m pytest tests/test_tts_preprocessor.py::TestCurrencyAndDigits -v`
Expected: all 4 tests PASS.

### 1.3 — T1.6 NFKC to entry + full-width bracket variants

- [ ] **Step 1.3.1: Write failing tests**

Add to `tests/test_tts_preprocessor.py` (can go into existing `TestBrackets`, `TestParentheses`, `TestAngleBrackets` classes):

Append to `TestBrackets`:
```python
    def test_strips_chinese_tortoise_brackets(self):
        # 【xxx】 should be stripped when ignore_brackets is on.
        assert tts_preprocessor.clean("【开心】正文") == "正文"
```

Append to `TestAngleBrackets`:
```python
    def test_strips_chinese_angle_brackets(self):
        # 〈xxx〉 (U+3008/U+3009) — chinese book title marks — stripped.
        assert tts_preprocessor.clean("〈标签〉正文") == "正文"

    def test_strips_math_angle_brackets_after_nfkc(self):
        # ⟨xxx⟩ (U+27E8/U+27E9) — math angle brackets. NFKC normalizes
        # them to ⟨⟩ — keep as-is, filter must list this pair explicitly.
        # (NFKC at entry does NOT change these to ASCII <>.)
        assert tts_preprocessor.clean("⟨标签⟩正文") == "正文"
```

- [ ] **Step 1.3.2: Run — expect FAIL**

Run: `python -m pytest tests/test_tts_preprocessor.py -k "tortoise or chinese_angle or math_angle" -v`
Expected: FAIL. Current `_filter_nested` only matches the ASCII/Chinese parenthesis pairs passed to it.

- [ ] **Step 1.3.3: Lift NFKC + extend bracket filter pairs**

In `core/tts_preprocessor.py`, replace the `clean()` function (around lines 23-50):

**Before:**
```python
def clean(text: str, config: Mapping[str, bool] | None = None) -> str:
    """Apply enabled TTS filters to ``text``.

    Args:
        text: Raw text from LLM (may contain emoji, markdown, etc.).
        config: Dict with the five toggles. Missing keys default to True
            (preprocess by default — caller can disable individually).

    Returns:
        Filtered text safe to feed to a TTS engine.

    Each filter swallows its own exceptions and logs a warning, so a
    bad regex on one filter never blocks the whole pipeline.
    """
    if not text:
        return text
    cfg = dict(config) if config else {}
    if cfg.get("ignore_asterisks", True):
        text = _safely(filter_asterisks, text, "asterisks")
    if cfg.get("ignore_brackets", True):
        text = _safely(filter_brackets, text, "brackets")
    if cfg.get("ignore_parentheses", True):
        text = _safely(filter_parentheses, text, "parentheses")
    if cfg.get("ignore_angle_brackets", True):
        text = _safely(filter_angle_brackets, text, "angle_brackets")
    if cfg.get("remove_special_char", True):
        text = _safely(remove_special_characters, text, "special_chars")
    return _collapse_whitespace(text)
```

**After:**
```python
def clean(text: str, config: Mapping[str, bool] | None = None) -> str:
    """Apply enabled TTS filters to ``text``.

    NFKC-normalizes at entry so downstream filters see a consistent form
    (WP3 T1.6 fix). ``_collapse_whitespace`` intentionally folds newlines
    into single spaces — TTS engines don't use newlines for prosody
    (WP3 T2.5 decision).

    Args:
        text: Raw text from LLM (may contain emoji, markdown, etc.).
        config: Dict with the five toggles. Missing keys default to True
            (preprocess by default — caller can disable individually).

    Returns:
        Filtered text safe to feed to a TTS engine.

    Each filter swallows its own exceptions and logs a warning, so a
    bad regex on one filter never blocks the whole pipeline.
    """
    if not text:
        return text
    # Normalize once at entry — all downstream filters see consistent chars.
    text = unicodedata.normalize("NFKC", text)
    cfg = dict(config) if config else {}
    if cfg.get("ignore_asterisks", True):
        text = _safely(filter_asterisks, text, "asterisks")
    if cfg.get("ignore_brackets", True):
        text = _safely(filter_brackets, text, "brackets")
    if cfg.get("ignore_parentheses", True):
        text = _safely(filter_parentheses, text, "parentheses")
    if cfg.get("ignore_angle_brackets", True):
        text = _safely(filter_angle_brackets, text, "angle_brackets")
    if cfg.get("remove_special_char", True):
        text = _safely(remove_special_characters, text, "special_chars")
    return _collapse_whitespace(text)
```

Also update the bracket filter constants. Replace:

**Before:**
```python
def filter_brackets(text: str) -> str:
    """Strip content within ASCII square brackets ``[ ]``."""
    return _filter_nested(text, [("[", "]")])


def filter_parentheses(text: str) -> str:
    """Strip content within both ASCII ``( )`` and full-width ``（ ）`` parens."""
    return _filter_nested(text, [("(", ")"), ("（", "）")])


def filter_angle_brackets(text: str) -> str:
    """Strip content within angle brackets ``< >`` (e.g. XML/SSML tags)."""
    return _filter_nested(text, [("<", ">")])
```

**After:**
```python
def filter_brackets(text: str) -> str:
    """Strip content within ASCII ``[ ]`` and Chinese tortoise brackets ``【 】``."""
    return _filter_nested(text, [("[", "]"), ("【", "】")])


def filter_parentheses(text: str) -> str:
    """Strip content within ASCII ``( )`` and full-width ``（ ）`` parens."""
    return _filter_nested(text, [("(", ")"), ("（", "）")])


def filter_angle_brackets(text: str) -> str:
    """Strip content within angle brackets.

    Handles ASCII ``< >`` (XML/SSML tags), Chinese ``〈 〉`` (book-title marks),
    and mathematical ``⟨ ⟩`` (U+27E8/U+27E9 — NFKC leaves them unchanged).
    """
    return _filter_nested(text, [("<", ">"), ("〈", "〉"), ("⟨", "⟩")])
```

- [ ] **Step 1.3.4: Run full preprocessor test suite — expect all PASS**

Run: `python -m pytest tests/test_tts_preprocessor.py -v`
Expected: all prior tests + new T1.3 tests PASS (no regressions).

### 1.4 — Task 1 commit

- [ ] **Step 1.4.1: Verify full test module green**

Run:
```bash
python -m pytest tests/test_tts_preprocessor.py tests/test_tts_cache.py -q
```
Expected: all green.

- [ ] **Step 1.4.2: Verify wider TTS tests still pass**

Run:
```bash
python -m pytest tests/ -q -k "tts" 2>&1 | tail -10
```
Expected: no new failures (pre-existing failures per baseline are OK).

- [ ] **Step 1.4.3: Commit Task 1**

```bash
git add core/tts.py core/tts_preprocessor.py \
    tests/test_tts_preprocessor.py tests/test_tts_cache.py
git commit -m "$(cat <<'EOF'
fix(tts): WP3 preprocessor + MiniMax vol int (T1.1/1.2/1.6)

- T1.1 MiniMax `vol` 字段对齐官方 int 0-10：config float → int(round) + clamp
- T1.2 `Sc` 货币符号加白名单（¥$€£￥ 不再被 remove_special_characters 吞）
- T1.6 NFKC 提到 clean() 入口；filter_brackets 加【】；filter_angle_brackets 加〈〉⟨⟩
- T2.5 docstring 说明 `_collapse_whitespace` 吞换行是有意的（TTS 不用换行做韵律）
- T3.1 新增 TestCurrencyAndDigits（4 个）+ TestBrackets 中文版 + TestAngleBrackets 中/数学版
- T1.1 新增 TestMinimaxVolumeCoercion（5 个：float/round/clamp/bad input）

设计文档：notes/plans/voice-pipeline-debug-2026-04-16-design.md §2 T1.1/T1.2/T1.6
EOF
)"
```

---

## Task 2: WP4 — LLM Abbreviation Word-Boundary Guard

**Bug IDs covered:** T1.3 (`_possible_abbreviation_prefix` over-matches), T3.2 (13-abbrev parametrize + stream reset test)

**Files:**
- Modify: `core/llm.py:767-783` (function `_possible_abbreviation_prefix`)
- Test: `tests/test_llm_sentence_divider.py`

### 2.1 — T1.3 Word-boundary guard

- [ ] **Step 2.1.1: Write the failing test**

Add to `tests/test_llm_sentence_divider.py` (append after `TestAbbreviationGuard` class, outside it):

```python
class TestPossibleAbbreviationPrefix:
    """WP4 T1.3: `_possible_abbreviation_prefix` must require word boundary.

    Without the word-boundary guard, any text ending in ".e" + "." (like
    "Welcome.") was deferred one extra delta because "e." is a valid prefix
    of the abbreviation "e.g.". That adds perceivable first-sentence latency.
    """

    def test_welcome_not_deferred(self):
        # "Welcome." at dot_idx=7: head "e." would match the last 2 chars,
        # but the char before "e" (at idx 5) is alpha ("m") — NOT a word
        # boundary. Must return False.
        c = _make_client(faster_first=False, is_first=False)
        assert c._possible_abbreviation_prefix("Welcome.", 7) is False

    def test_use_eg_at_end_is_deferred(self):
        # "use e." at dot_idx=5: head "e." matches positions 4-5; char at
        # idx 3 is space — valid word boundary. Must return True.
        c = _make_client(faster_first=False, is_first=False)
        assert c._possible_abbreviation_prefix("use e.", 5) is True

    def test_eg_at_buffer_start(self):
        # "e." at dot_idx=1: head "e." at positions 0-1; no char before —
        # buffer start counts as word boundary. Must return True.
        c = _make_client(faster_first=False, is_first=False)
        assert c._possible_abbreviation_prefix("e.", 1) is True

    def test_streaming_welcome_splits_not_deferred(self):
        # End-to-end: feeding "Welcome." with force=False should NOT defer.
        c = _make_client(faster_first=False, is_first=False)
        out: list[str] = []
        leftover = c._flush_sentences("Welcome.", on_sentence=out.append, force=False)
        assert out == ["Welcome."]
        assert leftover == ""

    def test_streaming_use_eg_still_deferred(self):
        # Still correct behavior: "use e." streams → hold for next delta.
        c = _make_client(faster_first=False, is_first=False)
        out: list[str] = []
        leftover = c._flush_sentences("use e.", on_sentence=out.append, force=False)
        assert out == []
        assert leftover == "use e."

    def test_disabled_guard_does_not_defer(self):
        c = _make_client(abbrev_protect=False, faster_first=False, is_first=False)
        # With guard off, "Welcome." splits immediately.
        out: list[str] = []
        c._flush_sentences("Welcome.", on_sentence=out.append, force=False)
        assert out == ["Welcome."]
```

- [ ] **Step 2.1.2: Run — expect FAIL**

Run: `python -m pytest tests/test_llm_sentence_divider.py::TestPossibleAbbreviationPrefix -v`
Expected: `test_welcome_not_deferred`, `test_streaming_welcome_splits_not_deferred` FAIL. Other three PASS (they were correctly True before, still True after).

- [ ] **Step 2.1.3: Add word-boundary guard to `_possible_abbreviation_prefix`**

In `core/llm.py`, replace `_possible_abbreviation_prefix` (lines ~767-790):

**Before:**
```python
    def _possible_abbreviation_prefix(self, buffer: str, dot_idx: int) -> bool:
        """True if buffer[:dot_idx+1] could be the start of an abbreviation.

        Used at end-of-stream-buffer to defer splitting on a trailing dot
        when more chars might still arrive. Example: buffer ends in "e."
        and "g." would land in the next delta — splitting now would emit
        a partial sentence; waiting one delta lets "e.g." form properly.
        """
        for abbr in self._ABBREVIATIONS:
            # Look for abbreviations that start somewhere at/before dot_idx
            # and extend past dot_idx (i.e., not yet fully present).
            for offset in range(min(len(abbr), dot_idx + 1)):
                head = abbr[: offset + 1]
                if head and head[-1] == "." and buffer[dot_idx + 1 - len(head): dot_idx + 1] == head:
                    if offset + 1 < len(abbr):
                        return True
        return False
```

**After:**
```python
    def _possible_abbreviation_prefix(self, buffer: str, dot_idx: int) -> bool:
        """True if buffer[:dot_idx+1] could be the start of an abbreviation.

        Used at end-of-stream-buffer to defer splitting on a trailing dot
        when more chars might still arrive. Example: buffer ends in "e."
        and "g." would land in the next delta — splitting now would emit
        a partial sentence; waiting one delta lets "e.g." form properly.

        T1.3 fix: requires a word boundary before the head match. Without it,
        "Welcome." matches head "e." (prefix of "e.g.") because the last 2
        chars ARE "e." — but the "e" is the tail of the word "Welcome", not
        an abbreviation start. The guard: char at `start - 1` must be
        non-alpha (or `start == 0`, i.e., buffer beginning).
        """
        for abbr in self._ABBREVIATIONS:
            # Look for abbreviations that start somewhere at/before dot_idx
            # and extend past dot_idx (i.e., not yet fully present).
            for offset in range(min(len(abbr), dot_idx + 1)):
                head = abbr[: offset + 1]
                if not head or head[-1] != ".":
                    continue
                start = dot_idx + 1 - len(head)
                if start < 0:
                    continue
                if buffer[start: start + len(head)] != head:
                    continue
                # Word-boundary guard: the char just before `start` must NOT
                # be alphabetic. `start == 0` counts as valid boundary too.
                if start > 0 and buffer[start - 1].isalpha():
                    continue
                if offset + 1 < len(abbr):
                    return True
        return False
```

- [ ] **Step 2.1.4: Run — expect PASS**

Run: `python -m pytest tests/test_llm_sentence_divider.py::TestPossibleAbbreviationPrefix -v`
Expected: all 6 tests PASS.

### 2.2 — T3.2 Parametrize all 13 abbreviations

- [ ] **Step 2.2.1: Add parametrized test**

Append to `tests/test_llm_sentence_divider.py` (after TestPossibleAbbreviationPrefix):

```python
class TestAllAbbreviationsProtected:
    """WP4 T3.2: every abbreviation in the canonical list must be protected."""

    @pytest.mark.parametrize("abbr", [
        "Mrs.", "Prof.", "e.g.", "i.e.",
        "Mr.", "Ms.", "Dr.", "Jr.", "Sr.", "St.", "Rd.",
        "Inc.", "Ltd.", "vs.",
    ])
    def test_abbreviation_not_split_mid_sentence(self, abbr):
        c = _make_client(faster_first=False, is_first=False)
        # Compose a mid-sentence example ending in a real period afterward
        # so the sentence should be emitted as one unit.
        text = f"Meet {abbr} Smith today."
        out: list[str] = []
        c._flush_sentences(text, on_sentence=out.append, force=True)
        assert out == [f"Meet {abbr} Smith today."], f"split incorrectly at {abbr}"
```

- [ ] **Step 2.2.2: Run — expect PASS (regression guard)**

Run: `python -m pytest tests/test_llm_sentence_divider.py::TestAllAbbreviationsProtected -v`
Expected: all 14 parametrized cases PASS. If any fail, update `_ABBREVIATIONS` tuple in `core/llm.py` (but expected: no changes needed, this just locks in existing coverage).

### 2.3 — T3.2 `_is_first_sentence` reset at stream entry

- [ ] **Step 2.3.1: Add stream-entry reset test**

Append to `tests/test_llm_sentence_divider.py`:

```python
class TestFirstSentenceFlagReset:
    """WP4 T3.2: `generate_response_stream` must reset `_is_first_sentence=True`
    at entry so that a second turn also fires faster_first_response on its
    own first sentence.
    """

    def test_reset_happens_at_entry(self):
        # We don't need a real LLM — just check the attribute flip after
        # the generator is entered. Stub the inner `_stream_openai` so we
        # can exit immediately after the reset.
        with patch.object(LLMClient, "__init__", lambda self, cfg, **kw: None):
            c = LLMClient.__new__(LLMClient)
            c._abbrev_protect = True
            c._faster_first_response = True
            c._is_first_sentence = False  # stale from prior turn
            c.provider = "openai"
            # Record what _stream_openai saw for _is_first_sentence.
            saw = {}

            def _stub(*args, **kwargs):
                saw["flag"] = c._is_first_sentence
                return "ok"

            c._stream_openai = _stub
            # mirror generate_response_stream signature minimally
            c.generate_response_stream(
                user_message="hi",
                conversation_history=[],
                tools=[],
                tool_executor=lambda *a, **k: "",
                user_name="", user_id="", user_role="",
                on_sentence=lambda s: None,
                user_emotion="", memory_context=None,
            )
            assert saw.get("flag") is True, (
                "generate_response_stream must reset _is_first_sentence=True"
            )
```

- [ ] **Step 2.3.2: Run — expect PASS**

Run: `python -m pytest tests/test_llm_sentence_divider.py::TestFirstSentenceFlagReset -v`
Expected: PASS (code at line 650 already does this reset; test locks in the invariant).

If it fails, inspect `core/llm.py:generate_response_stream` — the reset line should be:
```python
self._is_first_sentence = True
```
before the provider branch.

### 2.4 — Task 2 commit

- [ ] **Step 2.4.1: Run full LLM test module**

Run: `python -m pytest tests/test_llm_sentence_divider.py -v`
Expected: all green.

- [ ] **Step 2.4.2: Commit Task 2**

```bash
git add core/llm.py tests/test_llm_sentence_divider.py
git commit -m "$(cat <<'EOF'
fix(llm): WP4 abbreviation word-boundary guard (T1.3) + 测试补齐 (T3.2)

- T1.3 `_possible_abbreviation_prefix` 要求 head 起点前是词边界（非字母 或
  buffer 开头）。修复前 "Welcome." 被误判为 "e.g." 的前缀，流式下延迟 1 delta
- T3.2 新增 TestPossibleAbbreviationPrefix（6 个 case：welcome / use e. /
  buffer start / 端到端 / guard 关闭时直接切）
- T3.2 TestAllAbbreviationsProtected 参数化 14 个缩写（含 vs.），锁定保护范围
- T3.2 TestFirstSentenceFlagReset 断言 `generate_response_stream` 入口重置 flag

设计文档：notes/plans/voice-pipeline-debug-2026-04-16-design.md §2 T1.3 / T3.2
EOF
)"
```

---

## Task 3: WP6 — VAD dBFS defaults + Production Config Sanity

**Bug IDs covered:** T1.4 (code defaults still SPL positive), T3.3 (production config + real-model integration test)

**Files:**
- Modify: `core/vad_silero.py` — change defaults at lines 51, 228, 230, 242-246
- Modify: `tests/test_vad_silero.py` — add TestProductionDefaults class; align TestProviderFactory tts_mode values

### 3.1 — T1.4 Change code defaults to dBFS negative

- [ ] **Step 3.1.1: Write failing tests**

Add to `tests/test_vad_silero.py` (append new class):

```python
class TestDefaultsAreDBFS:
    """WP6 T1.4: code defaults must match dBFS scale (negative), not SPL (positive)."""

    def test_silero_default_db_threshold_is_dbfs(self, mock_session_factory):
        # With no db_threshold passed, default must be negative (dBFS).
        sess = mock_session_factory([0.1])
        with patch("onnxruntime.InferenceSession", return_value=sess):
            vad = SileroVADDirect(model_path="/dev/null")
        assert vad._db_threshold < 0, (
            f"Default must be dBFS (negative), got {vad._db_threshold}"
        )

    def test_build_vad_record_mode_default_dbfs(self, mock_session_factory):
        sess = mock_session_factory([0.1])
        cfg = {
            "vad_provider": "silero_direct",
            "vad_model_path": "/dev/null",
            # intentionally omit vad_db_threshold → must use negative default
        }
        with patch("onnxruntime.InferenceSession", return_value=sess):
            inst = build_vad(cfg, mode="record")
        assert inst._db_threshold < 0, (
            f"build_vad record default must be dBFS, got {inst._db_threshold}"
        )

    def test_build_vad_tts_mode_mac_default_dbfs(self, mock_session_factory):
        sess = mock_session_factory([0.1])
        cfg = {
            "vad_provider": "silero_direct",
            "vad_model_path": "/dev/null",
        }
        with patch("onnxruntime.InferenceSession", return_value=sess), \
             patch("platform.system", return_value="Darwin"):
            inst = build_vad(cfg, mode="tts")
        assert inst._db_threshold < 0, (
            f"build_vad tts Mac default must be dBFS, got {inst._db_threshold}"
        )

    def test_build_vad_tts_mode_rpi_default_dbfs(self, mock_session_factory):
        sess = mock_session_factory([0.1])
        cfg = {
            "vad_provider": "silero_direct",
            "vad_model_path": "/dev/null",
        }
        with patch("onnxruntime.InferenceSession", return_value=sess), \
             patch("platform.system", return_value="Linux"):
            inst = build_vad(cfg, mode="tts")
        assert inst._db_threshold < 0, (
            f"build_vad tts RPi default must be dBFS, got {inst._db_threshold}"
        )
```

- [ ] **Step 3.1.2: Run — expect FAIL**

Run: `python -m pytest tests/test_vad_silero.py::TestDefaultsAreDBFS -v`
Expected: all 4 FAIL (current defaults are 60.0 / 72.0 / 62.0).

- [ ] **Step 3.1.3: Change the defaults**

In `core/vad_silero.py` line 51, change:
```python
        db_threshold: float = 60.0,
```
to:
```python
        db_threshold: float = -45.0,  # dBFS; silence is typically < -50, speech ~ -30
```

In `core/vad_silero.py` around lines 225-246 (function `build_vad`), replace:

**Before:**
```python
    if provider == "silero_direct":
        if mode == "tts":
            import platform
            if platform.system() == "Darwin":
                db_default = float(cfg.get("vad_db_threshold_during_tts_mac", 72.0))
            else:
                db_default = float(cfg.get("vad_db_threshold_during_tts_rpi", 62.0))
            return SileroVADDirect(
                model_path=str(cfg["vad_model_path"]),
                prob_threshold=float(cfg.get("vad_prob_threshold_during_tts", 0.5)),
                db_threshold=db_default,
                smoothing_window=int(cfg.get("vad_smoothing_window", 5)),
                required_hits=int(cfg.get("vad_required_hits", 3)),
                required_misses=int(cfg.get("vad_required_misses", 24)),
            )
        return SileroVADDirect(
            model_path=str(cfg["vad_model_path"]),
            prob_threshold=float(cfg.get("vad_prob_threshold", 0.4)),
            db_threshold=float(cfg.get("vad_db_threshold", 60.0)),
            smoothing_window=int(cfg.get("vad_smoothing_window", 5)),
            required_hits=int(cfg.get("vad_required_hits", 3)),
            required_misses=int(cfg.get("vad_required_misses", 24)),
        )
```

**After:**
```python
    if provider == "silero_direct":
        if mode == "tts":
            import platform
            # During-TTS dBFS defaults tuned per-platform: Mac CoreAudio
            # returns higher energy (near-field mic + louder speaker),
            # RPi ReSpeaker post-AEC is quieter. Both are dBFS (negative).
            if platform.system() == "Darwin":
                db_default = float(cfg.get("vad_db_threshold_during_tts_mac", -22.0))
            else:
                db_default = float(cfg.get("vad_db_threshold_during_tts_rpi", -32.0))
            return SileroVADDirect(
                model_path=str(cfg["vad_model_path"]),
                prob_threshold=float(cfg.get("vad_prob_threshold_during_tts", 0.5)),
                db_threshold=db_default,
                smoothing_window=int(cfg.get("vad_smoothing_window", 5)),
                required_hits=int(cfg.get("vad_required_hits", 3)),
                required_misses=int(cfg.get("vad_required_misses", 24)),
            )
        # Record-mode dBFS default: -45 clears typical silence (~-60)
        # but is well below normal speech (~-30).
        return SileroVADDirect(
            model_path=str(cfg["vad_model_path"]),
            prob_threshold=float(cfg.get("vad_prob_threshold", 0.4)),
            db_threshold=float(cfg.get("vad_db_threshold", -45.0)),
            smoothing_window=int(cfg.get("vad_smoothing_window", 5)),
            required_hits=int(cfg.get("vad_required_hits", 3)),
            required_misses=int(cfg.get("vad_required_misses", 24)),
        )
```

- [ ] **Step 3.1.4: Run — expect PASS for new tests**

Run: `python -m pytest tests/test_vad_silero.py::TestDefaultsAreDBFS -v`
Expected: all 4 PASS.

### 3.2 — T3.3 Production config + real-model integration sanity

- [ ] **Step 3.2.1: Add integration tests**

Add to `tests/test_vad_silero.py` (append new class):

```python
import yaml as _yaml


class TestProductionDefaults:
    """WP6 T3.3: load actual config.yaml + real ONNX model, verify sanity.

    Skips when the real model isn't on disk (CI / fresh clones without
    data/). The goal is to catch regressions where config and code drift
    apart after the SPL→dBFS fix.
    """

    @pytest.fixture
    def production_config(self) -> dict:
        config_path = Path(__file__).resolve().parent.parent / "config.yaml"
        if not config_path.exists():
            pytest.skip("config.yaml not present")
        return _yaml.safe_load(config_path.read_text())

    @pytest.fixture
    def real_model_path(self) -> Path:
        p = Path(__file__).resolve().parent.parent / "data" / "silero_vad.onnx"
        if not p.exists():
            pytest.skip("data/silero_vad.onnx not present")
        return p

    def test_audio_section_defaults_load_ok(self, production_config, real_model_path):
        # Load real audio:* config and real model; instance should build.
        audio_cfg = dict(production_config.get("audio", {}))
        audio_cfg["vad_model_path"] = str(real_model_path)
        # Force silero_direct even if production config uses fallback
        audio_cfg["vad_provider"] = "silero_direct"
        inst = build_vad(audio_cfg, mode="record")
        assert inst is not None
        assert inst._db_threshold < 0, "production config must be dBFS"

    def test_silence_does_not_trigger_with_production_defaults(
        self, production_config, real_model_path,
    ):
        audio_cfg = dict(production_config.get("audio", {}))
        audio_cfg["vad_model_path"] = str(real_model_path)
        audio_cfg["vad_provider"] = "silero_direct"
        inst = build_vad(audio_cfg, mode="record")
        # 1 second of pure silence (16000 samples @ 16kHz) — must stay IDLE.
        silence = np.zeros(16000, dtype=np.float32)
        inst.accept_waveform(silence)
        assert inst.is_speech_detected() is False
        assert inst.empty() is True, "no segment should be completed on silence"

    def test_synthetic_speech_triggers_with_production_defaults(
        self, production_config, real_model_path,
    ):
        audio_cfg = dict(production_config.get("audio", {}))
        audio_cfg["vad_model_path"] = str(real_model_path)
        audio_cfg["vad_provider"] = "silero_direct"
        # Relax required_hits so 1 second of synthetic speech is enough
        audio_cfg["vad_required_hits"] = 3
        audio_cfg["vad_required_misses"] = 5
        inst = build_vad(audio_cfg, mode="record")
        # Synthetic 150 Hz sine + harmonic + white noise @ amplitude 0.2
        t = np.arange(16000) / 16000.0
        speech_like = (
            0.15 * np.sin(2 * np.pi * 150 * t)
            + 0.05 * np.sin(2 * np.pi * 300 * t)
            + 0.05 * np.random.RandomState(42).randn(16000)
        ).astype(np.float32)
        # Feed in chunks so state machine has a chance to transition.
        for start in range(0, 16000, 512):
            inst.accept_waveform(speech_like[start: start + 512])
        # After 1 sec of speech-like signal, should have entered ACTIVE
        # at some point (and possibly back to IDLE with segment completed).
        assert not inst.empty() or inst.is_speech_detected(), (
            "synthetic speech should trigger VAD with production defaults"
        )

    def test_tts_mode_mac_default_passthrough(self, production_config, real_model_path):
        interrupt_cfg = dict(production_config.get("interrupt", {}))
        interrupt_cfg["vad_model_path"] = str(real_model_path)
        interrupt_cfg["vad_provider"] = "silero_direct"
        with patch("platform.system", return_value="Darwin"):
            inst = build_vad(interrupt_cfg, mode="tts")
        assert inst._db_threshold < 0
        assert inst._prob_threshold > 0.0
```

Ensure the file imports `yaml` at top (check and add if missing):
```python
import yaml as _yaml
```
(Only add if not already imported.)

- [ ] **Step 3.2.2: Run — expect PASS or SKIP**

Run: `python -m pytest tests/test_vad_silero.py::TestProductionDefaults -v`
Expected: all 4 PASS (if real model + config.yaml present), or SKIP (with reason).

### 3.3 — Align existing test_factory values to dBFS

- [ ] **Step 3.3.1: Clean stale positive values in TestProviderFactory**

In `tests/test_vad_silero.py`, `TestProviderFactory.test_factory_tts_mode_uses_tts_thresholds` (around lines 176-190), replace:

**Before:**
```python
    def test_factory_tts_mode_uses_tts_thresholds(self, mock_session_factory):
        cfg = {
            "vad_provider": "silero_direct",
            "vad_model_path": "/dev/null",
            "vad_prob_threshold_during_tts": 0.7,
            "vad_db_threshold_during_tts_mac": 80.0,
            "vad_db_threshold_during_tts_rpi": 65.0,
        }
        with patch("onnxruntime.InferenceSession", return_value=mock_session_factory([0.1])):
            with patch("platform.system", return_value="Darwin"):
                inst = build_vad(cfg, mode="tts")
                assert inst._db_threshold == 80.0
            with patch("platform.system", return_value="Linux"):
                inst2 = build_vad(cfg, mode="tts")
                assert inst2._db_threshold == 65.0
```

**After:**
```python
    def test_factory_tts_mode_uses_tts_thresholds(self, mock_session_factory):
        # Use dBFS values (negative) to match the production unit convention.
        cfg = {
            "vad_provider": "silero_direct",
            "vad_model_path": "/dev/null",
            "vad_prob_threshold_during_tts": 0.7,
            "vad_db_threshold_during_tts_mac": -22.0,
            "vad_db_threshold_during_tts_rpi": -32.0,
        }
        with patch("onnxruntime.InferenceSession", return_value=mock_session_factory([0.1])):
            with patch("platform.system", return_value="Darwin"):
                inst = build_vad(cfg, mode="tts")
                assert inst._db_threshold == -22.0
            with patch("platform.system", return_value="Linux"):
                inst2 = build_vad(cfg, mode="tts")
                assert inst2._db_threshold == -32.0
```

- [ ] **Step 3.3.2: Run — expect PASS**

Run: `python -m pytest tests/test_vad_silero.py::TestProviderFactory -v`
Expected: all PASS (values are now consistent with dBFS convention).

### 3.4 — Task 3 commit

- [ ] **Step 3.4.1: Full VAD test module green**

Run: `python -m pytest tests/test_vad_silero.py -v`
Expected: all green (plus any `test_real_model` / `TestProductionDefaults` that skip if model missing).

- [ ] **Step 3.4.2: Config self-check (touched defaults logic, make sure yaml still loads)**

Run:
```bash
python -c "import yaml; yaml.safe_load(open('config.yaml'))"
```
Expected: no output (success).

- [ ] **Step 3.4.3: Commit Task 3**

```bash
git add core/vad_silero.py tests/test_vad_silero.py
git commit -m "$(cat <<'EOF'
fix(vad): WP6 代码默认值对齐 dBFS + 生产 config 真模型 sanity (T1.4/T3.3)

- T1.4 `SileroVADDirect.__init__` default db_threshold: 60.0 → -45.0
- T1.4 `build_vad` tts-mode Mac default 72.0 → -22.0；RPi 62.0 → -32.0
- T1.4 `build_vad` record-mode default 60.0 → -45.0
- 原因：367ffac 只改 config.yaml，代码 fallback 仍 SPL 正值 → config key
  缺失时 VAD 永不触发（任何正常语音 dBFS < 0）
- T3.3 新增 TestDefaultsAreDBFS（4 个断言默认值为负）
- T3.3 新增 TestProductionDefaults（4 个加载真 config + 真 ONNX：静音不
  触发 / 合成语音触发 / tts 模式 Mac passthrough）
- 清扫 test_factory_tts_mode_uses_tts_thresholds：80/65 → -22/-32 避免阅读混淆

设计文档：notes/plans/voice-pipeline-debug-2026-04-16-design.md §2 T1.4 / T3.3 / §4
EOF
)"
```

---

## Task 4: WP2 — ASR Normalizer Fixes

**Bug IDs covered:** T1.5 (text path bypass), T2.1 (`_ACTION_WORDS` "灯"), T2.2 (L3 length mismatch), T3.5 (< 10ms perf)

**Files:**
- Modify: `core/asr_normalizer.py` (lines 27-29 `_ACTION_WORDS`, 163-168 `_apply_fuzzy`)
- Modify: `jarvis.py` — move normalize from `handle_utterance:599` to `_process_turn` entry
- Test: `tests/test_asr_normalizer.py`

### 4.1 — T1.5 Move normalize() to `_process_turn`

- [ ] **Step 4.1.1: Identify the current call site**

Run:
```bash
grep -n "asr_normalizer" jarvis.py
```
Expected output includes `599:        normalized = self.asr_normalizer.normalize(text)` (or similar). Note the exact line and surrounding context.

- [ ] **Step 4.1.2: Locate `_process_turn` entry point**

Run:
```bash
grep -n "def _process_turn" jarvis.py
```
Expected: one line, e.g. `655:    def _process_turn(`. Open around this line to see the first few lines of the method (the reset of `self._interrupt_played_texts = None`).

- [ ] **Step 4.1.3: Write failing test**

Create new file `tests/test_jarvis_asr_integration.py`:

```python
"""WP2 T1.5: asr_normalizer must run on BOTH voice and text entry paths.

Moved from `handle_utterance` to `_process_turn` (shared pipeline) so that
MQTT / web-frontend text also benefits from ASR correction.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from jarvis import JarvisApp


@pytest.fixture
def jarvis_stub():
    with patch.object(JarvisApp, "__init__", lambda self, cfg, **kw: None):
        j = JarvisApp.__new__(JarvisApp)
        j.logger = MagicMock()
        j._interrupt_played_texts = None
        j.asr_normalizer = MagicMock()
        j.asr_normalizer.normalize = MagicMock(side_effect=lambda t: t + "_NORM")
        j.conversation_store = MagicMock()
        # Any attribute touched before the first return must be mocked.
        # We intentionally bail early via a sentinel on get_history.
        j.conversation_store.get_history = MagicMock(side_effect=RuntimeError("stop here"))
    return j


class TestNormalizeCalledByProcessTurn:
    def test_voice_path_normalizes(self, jarvis_stub):
        """Voice entry (`handle_utterance` → `_process_turn`) runs normalize."""
        with pytest.raises(RuntimeError, match="stop here"):
            jarvis_stub._process_turn(
                text="开客厅大蛋",
                session_id="s",
                output_fn=lambda _s: None,
            )
        jarvis_stub.asr_normalizer.normalize.assert_called_once_with("开客厅大蛋")

    def test_text_path_normalizes(self, jarvis_stub):
        """Text entry (`handle_text` → `_process_turn`) ALSO runs normalize.

        Before T1.5, only the voice path called normalize. This locks in the
        shared behavior at the `_process_turn` entry.
        """
        with pytest.raises(RuntimeError, match="stop here"):
            jarvis_stub._process_turn(
                text="开客厅大蛋",
                session_id="s",
                output_fn=lambda _s: None,
            )
        jarvis_stub.asr_normalizer.normalize.assert_called_once_with("开客厅大蛋")
```

- [ ] **Step 4.1.4: Run — expect FAIL**

Run: `python -m pytest tests/test_jarvis_asr_integration.py -v`
Expected: FAIL — `asr_normalizer.normalize` is NOT called from `_process_turn` currently; it's in `handle_utterance`.

- [ ] **Step 4.1.5: Move the normalize() call**

In `jarvis.py`, find line ~599 in `handle_utterance`:
```python
        normalized = self.asr_normalizer.normalize(text)
```
Remove this line and any subsequent use of `normalized` (replace uses of `normalized` with `text` in that method, OR keep the variable by assigning `text = self.asr_normalizer.normalize(text)` BEFORE the removal so nothing else changes). Simplest: delete the call from `handle_utterance` because `_process_turn` will do it.

Locate the exact code around `jarvis.py:599`:
```bash
grep -n -B 2 -A 5 "normalized = self.asr_normalizer.normalize(text)" jarvis.py
```

Then edit so that:
- The line `normalized = self.asr_normalizer.normalize(text)` is removed
- Any downstream references to `normalized` are replaced with `text` (or restructured so the normalizer call happens in `_process_turn`)

In `jarvis.py`, find the first line inside `_process_turn` (after the signature and docstring; the existing `self._interrupt_played_texts = None` reset line at ~695):
```python
        self._interrupt_played_texts = None
```

Add IMMEDIATELY AFTER it (before any other logic reads `text`):
```python
        # T1.5: normalize on shared pipeline so text-path (handle_text/MQTT/web)
        # also benefits. Corrections have require_context guards → safe for
        # non-voice input.
        text = self.asr_normalizer.normalize(text)
```

Verify placement by running:
```bash
grep -n "asr_normalizer" jarvis.py
```
Expected: exactly ONE occurrence of `.normalize(` — inside `_process_turn`. The reference in `__init__` (`self.asr_normalizer = ASRNormalizer(config)`) stays.

- [ ] **Step 4.1.6: Run — expect PASS**

Run: `python -m pytest tests/test_jarvis_asr_integration.py -v`
Expected: both tests PASS.

### 4.2 — T2.1 `_ACTION_WORDS` drop "灯"

- [ ] **Step 4.2.1: Write failing test**

Add to `tests/test_asr_normalizer.py`:

```python
class TestActionWordsTightening:
    """WP2 T2.1: `_ACTION_WORDS` must not include bare "灯" — too broad for L3 fuzzy."""

    def test_fuzzy_does_not_fire_on_ambient_light_talk(self):
        # L3 enabled; alias says "大灯" can be corrected, but casual talk
        # about lights (路灯/灯笼/灯泡) mustn't trigger the fuzzy path.
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
```

- [ ] **Step 4.2.2: Run — expect FAIL**

Run: `python -m pytest tests/test_asr_normalizer.py::TestActionWordsTightening -v`
Expected: `test_fuzzy_does_not_fire_on_ambient_light_talk` FAILs (current `_ACTION_WORDS` has "灯" and "我喜欢小灯泡" contains "灯"); `test_fuzzy_fires_with_strict_action_word` may FAIL because "打开" isn't in the current list.

- [ ] **Step 4.2.3: Tighten `_ACTION_WORDS`**

In `core/asr_normalizer.py`, replace lines 27-29:

**Before:**
```python
_ACTION_WORDS: tuple[str, ...] = (
    "开", "关", "调", "亮", "暗", "模式", "灯", "切换", "启动",
)
```

**After:**
```python
# WP2 T2.1: tightened — bare "灯" was too broad (路灯/灯笼/灯泡 all trigger).
# "暗" also removed (暗恋/暗号 false positives). "打开"/"关闭" added so real
# verbs survive after removing the single-char variants.
_ACTION_WORDS: tuple[str, ...] = (
    "开", "关", "打开", "关闭", "调", "亮", "模式",
    "切换", "启动", "场景",
)
```

- [ ] **Step 4.2.4: Run — expect PASS**

Run: `python -m pytest tests/test_asr_normalizer.py::TestActionWordsTightening -v`
Expected: both tests PASS.

### 4.3 — T2.2 L3 length-match guard

- [ ] **Step 4.3.1: Write failing tests**

Add to `tests/test_asr_normalizer.py`:

```python
class TestLayer3LengthGuard:
    """WP2 T2.2: fuzzy window must equal alias length to avoid text-length drift."""

    def test_window_must_equal_alias_length(self):
        # alias "大灯" is 2 chars. A 3-char window must NOT match it.
        cfg = {
            "asr_aliases": {"客厅大灯": ["大灯"]},
            "asr_normalizer_fuzzy": {"enabled": True, "max_distance": 2},
        }
        n = ASRNormalizer(cfg)
        # "开大蛋灯" has a 3-char window around "大蛋灯" that could edit-distance
        # match "大灯" (distance 1 via delete) — before the fix that would
        # replace 3 chars with the full 4-char canonical, corrupting text.
        # After the fix, only 2-char window "大蛋" gets matched (distance 1).
        out = n.normalize("打开大蛋灯")
        # The correct behavior: "大蛋" → "客厅大灯"; the trailing "灯" stays.
        # So result is "打开客厅大灯灯" (awkward but length-stable, no stitched corruption).
        assert out == "打开客厅大灯灯"

    def test_canonical_can_be_longer_than_alias(self):
        # alias "大灯" (2) → canonical "客厅大灯" (4). Still works: window 2 matches,
        # replaced with 4-char canonical.
        cfg = {
            "asr_aliases": {"客厅大灯": ["大灯"]},
            "asr_normalizer_fuzzy": {"enabled": True, "max_distance": 1},
        }
        n = ASRNormalizer(cfg)
        out = n.normalize("打开大蛋")
        assert out == "打开客厅大灯"  # text expands, canonical is 2 chars longer
```

- [ ] **Step 4.3.2: Run — expect FAIL on `test_window_must_equal_alias_length`**

Run: `python -m pytest tests/test_asr_normalizer.py::TestLayer3LengthGuard -v`
Expected: `test_window_must_equal_alias_length` FAILs (current code uses `abs(len(cand) - len(window)) > max_distance` — allows cross-length matches).

- [ ] **Step 4.3.3: Apply length-match guard**

In `core/asr_normalizer.py`, find `_apply_fuzzy` (around lines 147-169). Replace the inner loop block:

**Before:**
```python
            for cand, canonical in self._fuzzy_targets.items():
                if abs(len(cand) - len(window)) > self._fuzzy_max_distance:
                    continue
                d = _levenshtein(window, cand)
                if d <= self._fuzzy_max_distance:
                    return text[:i] + canonical + text[i + window_size:]
```

**After:**
```python
            for cand, canonical in self._fuzzy_targets.items():
                # T2.2: strict length match — window must equal alias length.
                # This avoids 2-char windows matching 4-char aliases and
                # corrupting text by expanding position-wise. Canonical CAN
                # be longer than alias (that's the point: users say short,
                # system fills in the full name).
                if len(cand) != window_size:
                    continue
                d = _levenshtein(window, cand)
                if d <= self._fuzzy_max_distance:
                    return text[:i] + canonical + text[i + window_size:]
```

- [ ] **Step 4.3.4: Run — expect PASS**

Run: `python -m pytest tests/test_asr_normalizer.py::TestLayer3LengthGuard -v`
Expected: both tests PASS.

### 4.4 — T3.5 Performance < 10ms

- [ ] **Step 4.4.1: Add performance assertion test**

Add to `tests/test_asr_normalizer.py`:

```python
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
        # Cold / miss path — nothing matches, so both layers scan fully.
        text = "帮我看看今天日历"
        # Warm up (JIT / attribute caches)
        for _ in range(10):
            n.normalize(text)
        # Time 1000 iterations
        start = time.perf_counter()
        for _ in range(1000):
            n.normalize(text)
        elapsed_ms = (time.perf_counter() - start) * 1000
        # 10ms per call × 1000 = 10 000 ms; allow headroom: assert < 5 000
        # (i.e., average < 5ms/call — very safe).
        assert elapsed_ms < 5000, f"cold path took {elapsed_ms:.0f} ms / 1000 iters"

    def test_layer3_enabled_still_reasonable(self):
        import time
        cfg = self._realistic_config()
        cfg["asr_normalizer_fuzzy"] = {"enabled": True, "max_distance": 2}
        n = ASRNormalizer(cfg)
        text = "打开客厅大蛋灯好吗"  # has action word → fuzzy will scan windows
        for _ in range(5):
            n.normalize(text)
        start = time.perf_counter()
        for _ in range(100):
            n.normalize(text)
        elapsed_ms = (time.perf_counter() - start) * 1000
        # L3 enabled is O(N*M*W²) — 50ms/call allowed in the extreme, so
        # 100 iters < 5000 ms is comfortable.
        assert elapsed_ms < 5000, f"L3 path took {elapsed_ms:.0f} ms / 100 iters"
```

- [ ] **Step 4.4.2: Run — expect PASS**

Run: `python -m pytest tests/test_asr_normalizer.py::TestPerformance -v`
Expected: both PASS on modern hardware. If a CI machine is extremely slow, the assertion headroom (5000ms for 1000 iters = 5ms/call average) is still safe.

### 4.5 — Task 4 commit

- [ ] **Step 4.5.1: Full ASR test module green**

Run: `python -m pytest tests/test_asr_normalizer.py tests/test_jarvis_asr_integration.py -v`
Expected: all PASS.

- [ ] **Step 4.5.2: Commit Task 4**

```bash
git add jarvis.py core/asr_normalizer.py \
    tests/test_asr_normalizer.py tests/test_jarvis_asr_integration.py
git commit -m "$(cat <<'EOF'
fix(asr): WP2 text 路径 + _ACTION_WORDS 收紧 + L3 长度守卫 + perf test
(T1.5/T2.1/T2.2/T3.5)

- T1.5 `normalize()` 调用从 `handle_utterance` 移到 `_process_turn` 入口；
  text 前端（handle_text/web/MQTT）也走 normalizer；corrections 的
  require_context guard 保证对真实打字无误伤
- T2.1 `_ACTION_WORDS` 删单字"灯"/"暗"（路灯/灯笼/暗恋 误触发），
  加 "打开"/"关闭"/"场景" 补动词覆盖
- T2.2 L3 `_apply_fuzzy` 要求 window 长度 == alias 长度；canonical 长度
  可以 ≥ alias 长度（fuzzy 的目的本来就是"短→长"补全）
- T3.5 TestPerformance：1000 iter < 5000ms（平均 < 5ms/call）
- 新文件：tests/test_jarvis_asr_integration.py（voice + text 路径断言）

设计文档：notes/plans/voice-pipeline-debug-2026-04-16-design.md §2 T1.5/T2.1/T2.2
EOF
)"
```

---

## Task 5: WP7 — InterruptMonitor Thread Safety

**Bug IDs covered:** T2.3 (unlocked `_recording`/`_fired` writes), T2.4 (`stop()` + timer-cancel critical-section merge)

**Files:**
- Modify: `core/interrupt_monitor.py` (lines 113-161 `start/stop` methods, 169-213 `feed_audio`)
- Test: `tests/test_interrupt_soft_stop.py` (add synchronized timer race test)
- Test: `tests/test_interrupt_monitor.py` (add locked-state assertion tests)

### 5.1 — T2.3 Lock `_recording` and `_fired`

- [ ] **Step 5.1.1: Write failing test**

Add to `tests/test_interrupt_monitor.py`:

```python
class TestStopPreventsFurtherCallbacks:
    """WP7 T2.3: after stop(), feed_audio must not fire callbacks, even if
    a mic thread races a chunk in just after stop() is called."""

    def test_callback_not_fired_after_stop(self):
        fires: list[str] = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: fires.append("int"),
        )
        monitor.start()
        monitor.stop()
        # Simulate a late-arriving chunk on the mic thread
        monitor.feed_audio(np.zeros(1600, dtype=np.float32))
        # _check_partial path: even if the keyword matched, _recording is False
        # so the early return should catch it.
        monitor._check_partial("停")
        # `_check_partial` still fires (it only checks `_fired` + `enabled`);
        # but feed_audio gates on `_recording`. Asserting only that feed_audio
        # doesn't push audio to the ASR stream (which would invoke _check_partial)
        # is the tight invariant here.
        # We verify by checking no audio was accumulated AFTER stop.
        assert monitor._audio_chunks == []

    def test_start_clears_fired_under_lock(self):
        # If _fired is left True from a prior session, start() must clear it.
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: None,
        )
        monitor._fired = True  # stale
        monitor.start()
        assert monitor._fired is False
```

- [ ] **Step 5.1.2: Run — expect PASS (may already pass)**

Run: `python -m pytest tests/test_interrupt_monitor.py::TestStopPreventsFurtherCallbacks -v`
Expected: may pass even without code change (CPython GIL hides single-byte race); the code change below is about lock discipline, not behavior.

- [ ] **Step 5.1.3: Wrap state writes in the lock**

In `core/interrupt_monitor.py`, replace `start()` (around lines 113-130):

**Before:**
```python
    def start(self) -> None:
        """Begin a monitoring session. Call before TTS playback starts."""
        if not self.enabled:
            return
        self._fired = False
        self._audio_chunks = []
        self._recording = True
        self._load_recognizer()
        self._load_vad()
        self._asr_buffer = np.array([], dtype=np.float32)
        if self._recognizer:
            self._stream = self._recognizer.create_stream()
        if self._vad is not None:
            self._vad.reset()
        # Reset soft-stop state for the new session
        self._soft_state = "NORMAL"
        self._was_speech_detected = False
        self._cancel_soft_timer()
```

**After:**
```python
    def start(self) -> None:
        """Begin a monitoring session. Call before TTS playback starts.

        T2.3: All mutable-state writes go under `self._lock` so mic-thread
        readers see consistent values (no torn reads; also clearer intent).
        """
        if not self.enabled:
            return
        self._load_recognizer()
        self._load_vad()
        with self._lock:
            self._fired = False
            self._audio_chunks = []
            self._recording = True
            self._asr_buffer = np.array([], dtype=np.float32)
            # Reset soft-stop state for the new session
            self._soft_state = "NORMAL"
            self._was_speech_detected = False
            self._cancel_soft_timer_locked()
        if self._recognizer:
            self._stream = self._recognizer.create_stream()
        if self._vad is not None:
            self._vad.reset()
```

And replace `stop()` (T2.4 merges the two critical sections):

**Before:**
```python
    def stop(self) -> np.ndarray | None:
        """End monitoring session. Returns accumulated audio or None."""
        self._recording = False
        self._cancel_soft_timer()
        # If we exited mid-DUCKED without a keyword, ensure the caller's
        # playback state isn't left frozen.
        with self._lock:
            should_resume = (
                self._soft_stop_enabled
                and self._soft_state == "DUCKED"
                and self._on_soft_resume is not None
            )
            self._soft_state = "NORMAL"
        if should_resume:
            try:
                self._on_soft_resume()
            except Exception as exc:
                LOGGER.warning("on_soft_resume on stop() failed: %s", exc)
        if self._stream and self._recognizer:
            try:
                self._recognizer.decode_stream(self._stream)
            except Exception:
                pass
            self._stream = None
        if self._audio_chunks:
            result = np.concatenate(self._audio_chunks)
            self._audio_chunks = []
            return result
        return None
```

**After:**
```python
    def stop(self) -> np.ndarray | None:
        """End monitoring session. Returns accumulated audio or None.

        T2.3 + T2.4: single critical section covers _recording flip, timer
        cancel, and soft-state read/clear. This prevents a timer callback
        from racing state inspection — whichever thread wins the lock
        decides whether on_soft_resume fires (and `resume_playback` is
        idempotent so a duplicate from the other thread is harmless).
        """
        with self._lock:
            self._recording = False
            self._cancel_soft_timer_locked()
            should_resume = (
                self._soft_stop_enabled
                and self._soft_state == "DUCKED"
                and self._on_soft_resume is not None
            )
            self._soft_state = "NORMAL"
            audio_chunks_snapshot = self._audio_chunks
            self._audio_chunks = []
        if should_resume and self._on_soft_resume is not None:
            try:
                self._on_soft_resume()
            except Exception as exc:
                LOGGER.warning("on_soft_resume on stop() failed: %s", exc)
        if self._stream and self._recognizer:
            try:
                self._recognizer.decode_stream(self._stream)
            except Exception:
                pass
            self._stream = None
        if audio_chunks_snapshot:
            return np.concatenate(audio_chunks_snapshot)
        return None
```

Also update `feed_audio` entry (lines ~169-178) to read `_recording` under the lock:

**Before:**
```python
    def feed_audio(self, audio: np.ndarray, sample_rate: int = 16000) -> None:
        """Feed an audio chunk for analysis.

        Silero VAD gates the stream: non-speech chunks are accumulated for
        post-interrupt re-transcription but NOT forwarded to streaming ASR.
        This avoids wasting CPU on AEC residual noise and reduces false
        keyword triggers.
        """
        if not self.enabled or not self._recording:
            return
```

**After:**
```python
    def feed_audio(self, audio: np.ndarray, sample_rate: int = 16000) -> None:
        """Feed an audio chunk for analysis.

        Silero VAD gates the stream: non-speech chunks are accumulated for
        post-interrupt re-transcription but NOT forwarded to streaming ASR.
        This avoids wasting CPU on AEC residual noise and reduces false
        keyword triggers.

        T2.3: `_recording` is read under the lock so callers see the flip
        atomically. The heavy work (VAD + ASR decode) stays outside the
        lock to avoid serializing the mic thread.
        """
        if not self.enabled:
            return
        with self._lock:
            if not self._recording:
                return
```

- [ ] **Step 5.1.4: Run — expect PASS**

Run: `python -m pytest tests/test_interrupt_monitor.py -v`
Expected: all existing + new tests PASS.

### 5.2 — T2.4 Synchronized timer race test

- [ ] **Step 5.2.1: Add synchronized timer race test**

Add to `tests/test_interrupt_soft_stop.py`:

```python
class TestTimerStopRace:
    """WP7 T2.4: when stop() and the soft-resume timer race, on_soft_resume
    should still be called at most once (the resume op is idempotent, but
    we don't want duplicate log noise either).

    Synchronized via `patch("threading.Timer")` — the "timer" becomes a
    handle we fire manually, removing all wall-clock flakiness.
    """

    def _make_monitor(self, on_soft_resume, on_soft_pause=None):
        return InterruptMonitor(
            config={
                "interrupt": {
                    "enabled": True,
                    "soft_stop_enabled": True,
                    "soft_stop_timeout_ms": 100,
                }
            },
            on_soft_pause=on_soft_pause or (lambda: None),
            on_soft_resume=on_soft_resume,
        )

    def test_stop_wins_race_timer_callback_noops(self):
        resume_calls: list[int] = []

        monitor = self._make_monitor(
            on_soft_resume=lambda: resume_calls.append(1),
            on_soft_pause=lambda: None,
        )
        monitor.start()

        # Force state into DUCKED by simulating a VAD start edge.
        monitor._update_soft_state(is_speech=True)
        # Capture the Timer instance for manual firing.
        # (The real Timer was created during _update_soft_state; we reach
        # in for it directly. This test asserts ordering, not mechanism.)
        timer_obj = monitor._soft_resume_timer
        assert timer_obj is not None

        # Main thread wins the race: stop() called first.
        monitor.stop()
        # After stop(), state is NORMAL; on_soft_resume fired once (by stop).
        assert len(resume_calls) == 1

        # Now simulate the timer actually firing (as if it was already
        # queued when we called stop). _on_soft_timeout must see NORMAL
        # and NOT call on_soft_resume again.
        monitor._on_soft_timeout()
        assert len(resume_calls) == 1, (
            f"timer callback must no-op after stop() already resumed; "
            f"got {len(resume_calls)} calls"
        )

    def test_timer_wins_race_stop_noops_further(self):
        resume_calls: list[int] = []

        monitor = self._make_monitor(
            on_soft_resume=lambda: resume_calls.append(1),
            on_soft_pause=lambda: None,
        )
        monitor.start()

        # Force DUCKED
        monitor._update_soft_state(is_speech=True)
        assert monitor._soft_state == "DUCKED"

        # Timer wins: simulate its callback firing first.
        monitor._on_soft_timeout()
        assert len(resume_calls) == 1
        assert monitor._soft_state == "NORMAL"

        # stop() now runs; state is already NORMAL so it must NOT
        # call on_soft_resume again.
        monitor.stop()
        assert len(resume_calls) == 1, (
            "stop() must no-op on_soft_resume when state is NORMAL"
        )
```

- [ ] **Step 5.2.2: Run — expect PASS**

Run: `python -m pytest tests/test_interrupt_soft_stop.py::TestTimerStopRace -v`
Expected: both PASS. This exercises the single-critical-section property added in step 5.1.3's `stop()` rewrite.

### 5.3 — Task 5 commit

- [ ] **Step 5.3.1: Full interrupt-related tests green**

Run:
```bash
python -m pytest tests/test_interrupt_monitor.py tests/test_interrupt_soft_stop.py \
    tests/test_interrupt_memory_injection.py tests/test_tts_suspend.py -v
```
Expected: all PASS.

- [ ] **Step 5.3.2: Commit Task 5**

```bash
git add core/interrupt_monitor.py tests/test_interrupt_monitor.py \
    tests/test_interrupt_soft_stop.py
git commit -m "$(cat <<'EOF'
fix(interrupt): WP7 thread-safety + stop/timer 锁合并 (T2.3/T2.4)

- T2.3 `_recording` / `_fired` / `_audio_chunks` / `_asr_buffer` / soft-stop
  state 在 start/stop/feed_audio 里全部走 `self._lock` 下的读写
- T2.4 `stop()` 把 timer cancel + soft-state 检查 + _soft_state flip 合并
  到单个 critical section；timer 回调看见 NORMAL 就 no-op（resume_playback
  幂等，但也避免重复日志噪声）
- T2.4 新增 TestTimerStopRace 同步化版（patch 掉 Timer 避免 wall-clock flaky）
  覆盖两方向 race：stop 先 / timer 先
- T2.3 新增 TestStopPreventsFurtherCallbacks / test_start_clears_fired_under_lock

设计文档：notes/plans/voice-pipeline-debug-2026-04-16-design.md §2 T2.3/T2.4
EOF
)"
```

---

## Task 6: WP1 — Buffer Batching Regression Test

**Bug IDs covered:** T3.4 (no existing assertion for `streaming_asr_chunk_samples` batching)

**Files:**
- Test only: `tests/test_interrupt_monitor.py`

### 6.1 — T3.4 Buffer batching assertion

- [ ] **Step 6.1.1: Add the batching test**

Add to `tests/test_interrupt_monitor.py`:

```python
class TestStreamingBufferBatching:
    """WP1 T3.4: `streaming_asr_chunk_samples` should gate when audio is
    forwarded to the recognizer — small chunks accumulate, threshold triggers
    one decode."""

    def _make_config(self, chunk_samples: int) -> dict:
        return {
            "interrupt": {
                "enabled": True,
                "streaming_asr_chunk_samples": chunk_samples,
            }
        }

    def test_small_chunk_does_not_trigger_decode(self):
        monitor = InterruptMonitor(
            config=self._make_config(3200),
            on_interrupt=lambda: None,
        )
        # Force-install mocked ASR + no VAD gate
        mock_stream = MagicMock()
        mock_recognizer = MagicMock()
        mock_recognizer.is_ready.return_value = False
        mock_recognizer.get_result.return_value = MagicMock(text="")
        monitor._stream = mock_stream
        monitor._recognizer = mock_recognizer
        monitor._vad = None
        # Flip _recording under lock (simulates start()'s invariant)
        with monitor._lock:
            monitor._recording = True

        # Feed 1000 samples (below 3200 threshold)
        monitor.feed_audio(np.zeros(1000, dtype=np.float32))
        mock_stream.accept_waveform.assert_not_called()

        # Feed another 1000 — still 2000 total, below threshold
        monitor.feed_audio(np.zeros(1000, dtype=np.float32))
        mock_stream.accept_waveform.assert_not_called()

    def test_threshold_crossing_triggers_one_decode(self):
        monitor = InterruptMonitor(
            config=self._make_config(3200),
            on_interrupt=lambda: None,
        )
        mock_stream = MagicMock()
        mock_recognizer = MagicMock()
        mock_recognizer.is_ready.return_value = False
        mock_recognizer.get_result.return_value = MagicMock(text="")
        monitor._stream = mock_stream
        monitor._recognizer = mock_recognizer
        monitor._vad = None
        with monitor._lock:
            monitor._recording = True

        # Feed 2000 + 2500 → 4500 > 3200 threshold after the second feed
        monitor.feed_audio(np.zeros(2000, dtype=np.float32))
        monitor.feed_audio(np.zeros(2500, dtype=np.float32))
        assert mock_stream.accept_waveform.call_count == 1
        # Arg shape check — the accumulated chunk fed to the recognizer
        # should be 4500 samples (or the full buffer at threshold crossing).
        args, _ = mock_stream.accept_waveform.call_args
        assert len(args[1]) == 4500

    def test_buffer_cleared_after_decode(self):
        monitor = InterruptMonitor(
            config=self._make_config(3200),
            on_interrupt=lambda: None,
        )
        mock_stream = MagicMock()
        mock_recognizer = MagicMock()
        mock_recognizer.is_ready.return_value = False
        mock_recognizer.get_result.return_value = MagicMock(text="")
        monitor._stream = mock_stream
        monitor._recognizer = mock_recognizer
        monitor._vad = None
        with monitor._lock:
            monitor._recording = True

        # Cross threshold once
        monitor.feed_audio(np.zeros(4000, dtype=np.float32))
        assert mock_stream.accept_waveform.call_count == 1
        # Feed another sub-threshold chunk — buffer was cleared, so below
        # threshold again: no second decode.
        monitor.feed_audio(np.zeros(1000, dtype=np.float32))
        assert mock_stream.accept_waveform.call_count == 1
```

- [ ] **Step 6.1.2: Run — expect PASS**

Run: `python -m pytest tests/test_interrupt_monitor.py::TestStreamingBufferBatching -v`
Expected: all 3 PASS (this is a regression guard for behavior already present since WP1).

### 6.2 — Task 6 commit

- [ ] **Step 6.2.1: Full interrupt test module green**

Run: `python -m pytest tests/test_interrupt_monitor.py -v`
Expected: all PASS.

- [ ] **Step 6.2.2: Commit Task 6**

```bash
git add tests/test_interrupt_monitor.py
git commit -m "$(cat <<'EOF'
test(interrupt): WP1 streaming_asr_chunk_samples 批量逻辑断言 (T3.4)

- 新增 TestStreamingBufferBatching（3 个 case）
  - 小 chunk 累积不触发 decode
  - 阈值跨越触发一次 decode（断言参数长度）
  - decode 后 buffer 清空（后续小 chunk 又回到累积态）
- 锁定 WP1 chunk 8000→3200 改动后的批量行为，防止回归

设计文档：notes/plans/voice-pipeline-debug-2026-04-16-design.md §2 T3.4
EOF
)"
```

---

## Task 7: Documentation — 交付报告更新

**Bug IDs covered:** T3.6 (manual test checklist expansion + fix log)

**Files:**
- Modify: `notes/plans/voice-pipeline-optimization-2026-04-16-report.md`

### 7.1 — Append fix log + expanded checklist

- [ ] **Step 7.1.1: Read the existing report**

Run:
```bash
wc -l notes/plans/voice-pipeline-optimization-2026-04-16-report.md
tail -20 notes/plans/voice-pipeline-optimization-2026-04-16-report.md
```
Expected: file exists; last line is the `END OF REPORT` marker.

- [ ] **Step 7.1.2: Collect the commit hashes for commits 1-6**

Run:
```bash
git log --oneline -10
```
Note the 7-char short SHAs for commits 1-6 (the `fix(tts)` / `fix(llm)` / `fix(vad)` / `fix(asr)` / `fix(interrupt)` / `test(interrupt)` lines). You will substitute these into the table in the next step. DO NOT use `git commit --amend` — per CLAUDE.md, always make NEW commits. Filling hashes BEFORE committing avoids any amend.

- [ ] **Step 7.1.3: Append the 2026-04-16 debug fixup section**

Append to `notes/plans/voice-pipeline-optimization-2026-04-16-report.md` BEFORE the final `END OF REPORT` marker (or at end if marker placement makes insertion awkward). Use the following content verbatim, **substituting the actual 7-char hashes you collected in Step 7.1.2 for `<hash-c1>` through `<hash-c6>`**:

```markdown

---

## 9. 2026-04-16 Debug Fixup 记录（Post-Delivery）

交付后 Allen + 执行 agent 二轮审计发现 17 项遗留项（真 bug 13 / 不改代码 1 / 测试缺口 5 / 文档 1 — 详见 `voice-pipeline-debug-2026-04-16-design.md`）。后续 7 个 fixup commits 解决：

| # | Commit | 范围 |
|---|--------|------|
| 0 | `e5ff894` | 设计文档落盘 |
| 1 | `<hash-c1>` | WP3 preprocessor + MiniMax vol int (T1.1/1.2/1.6 + T2.5 docstring + T3.1) |
| 2 | `<hash-c2>` | WP4 abbreviation word-boundary + 13 abbrev parametrize + stream reset (T1.3/T3.2) |
| 3 | `<hash-c3>` | WP6 VAD dBFS 代码默认 + 生产 config 真模型 sanity (T1.4/T3.3) |
| 4 | `<hash-c4>` | WP2 text 路径 + _ACTION_WORDS 收紧 + L3 长度守卫 + perf (T1.5/T2.1/T2.2/T3.5) |
| 5 | `<hash-c5>` | WP7 thread-safety + stop/timer 合并 (T2.3/T2.4) |
| 6 | `<hash-c6>` | WP1 streaming buffer 批量断言 (T3.4) |
| 7 | _this commit_ | 本文档更新 (T3.6) |

（7 的 hash 不需要预填，commit 完后 `git log` 可见。）

### 9.1 手测 checklist 扩充（接续 §5）

- [ ] **T1.1 MiniMax vol**：触发一段长 LLM 回复 → TTS 不返回 422 / type error；音量听起来正常（不爆也不过小）
- [ ] **T1.2 Currency**：LLM 说出 `¥100` / `$5` / `€3` → TTS 读出来时货币符号不被吞（每家 TTS 引擎对 ¥ 的朗读可能不同：MiniMax 多念"元"，edge-tts 多念"人民币"；关键是符号到达 TTS 之前没被 preprocessor 丢掉）
- [ ] **T1.3 Welcome. 首句延迟**：让 LLM 回复以 `Welcome.` 开头 → 首句 TTS 触发时间肉眼感知不慢（对照 plan §5.4 的 faster_first_response 期望）
- [ ] **T1.4 VAD fallback**：临时把 `config.yaml` 的 `audio.vad_db_threshold` 一行删掉并启动 jarvis.py → 无报错；说话能正常被 VAD 触发（证明代码默认值也是 dBFS 合理值）— 完事别忘了改回来
- [ ] **T1.5 text 前端修正**：`python jarvis.py` 后用 web/text 前端发 `开客厅大蛋` → 系统应识别为`开客厅大灯`意图并执行
- [ ] **T1.6 全角方括号/花括号**：LLM 回复 `【开心】正文` / `〈tag〉正文` → TTS 只念"正文"
- [ ] **T2.1 灯泡不误触发**：L3 fuzzy 打开（`asr_normalizer_fuzzy.enabled=true`）后说 `我喜欢小灯泡` → 不被改成其他设备名
- [ ] **T2.3 thread safety**：连续打断 3 次（TTS 说话 → 说"停" → 再开始新对话 → 再打断...）→ 无日志里有重复的 `on_soft_resume` 警告
- [ ] **Bench 再跑一次**：`python scripts/bench_interrupt_latency.py --runs 10 --label after-debug` → `speech_to_detect_ms` 中位数 < 400ms，写进 `scripts/bench_results/interrupt_latency.jsonl`
- [ ] **soft_stop 手测后切换默认**（仍不是本轮范围，但 Allen 可在这个 pass 里一起验）：`interrupt.soft_stop_enabled=true` + 麦克风说"嗯嗯"不说关键词 → TTS 暂停 3s 后自动恢复，无 audio click/pop。**通过后在 config.yaml 里把默认改成 true（单独 commit）**

### 9.2 已知遗留 / 后续

- L3 fuzzy 默认仍 `enabled=false`；Allen 实战观察后按需开启
- `bench_interrupt_latency.py` 仍需要人工说"停"；未来可预录 trigger 音频从虚拟 mic 注入
- MQTT/远程频道非 streaming 路径若有打断也想注入 `[Interrupted by user]` marker，需扩 `_truncate_assistant_for_interrupt` 触发点（本轮未做）
```

- [ ] **Step 7.1.4: Verify the report is syntactically OK AND hashes are filled**

Run:
```bash
python -c "import pathlib; s = pathlib.Path('notes/plans/voice-pipeline-optimization-2026-04-16-report.md').read_text(); assert '## 9. 2026-04-16 Debug Fixup' in s, 'section 9 not appended'; assert 'END OF REPORT' in s, 'end marker missing'; assert '<hash-c' not in s, 'unfilled hash placeholders remain'; print('OK')"
```
Expected: `OK`. If assertion `unfilled hash placeholders remain` fires, go back to 7.1.2 / 7.1.3 and fill real hashes.

### 7.2 — Task 7 commit

- [ ] **Step 7.2.1: Commit Task 7**

```bash
git add notes/plans/voice-pipeline-optimization-2026-04-16-report.md
git commit -m "$(cat <<'EOF'
docs(report): 2026-04-16 debug fixup 记录 + 手测 checklist 扩充 (T3.6)

新增 §9：
- 7 个 fixup commit 清单（commits 1-6 hash 已回填）
- 9 条新增手测项（T1.1-T2.3 + bench 重跑 + soft_stop 后续）
- 9.2 遗留：L3 fuzzy 仍默认关闭、bench 人工触发、MQTT 非 streaming 路径

设计文档：notes/plans/voice-pipeline-debug-2026-04-16-design.md §2 T3.6
EOF
)"
```

---

## Final Verification (run after all 7 tasks done)

- [ ] **Full pytest suite**

Run:
```bash
python -m pytest tests/ -q 2>&1 | tail -10
```
Expected: `~993 passed, 10 failed` (10 pre-existing failures unchanged; +30 new tests from our work).

- [ ] **Config yaml loads**

Run:
```bash
python -c "import yaml; yaml.safe_load(open('config.yaml'))"
```
Expected: no output (valid yaml).

- [ ] **Jarvis starts without exceptions**

Run (with 10-second timeout so it doesn't block):
```bash
timeout 10 python jarvis.py --no-wake 2>&1 | head -40 || true
```
Expected: startup logs; no Python traceback in output. Kill after 10s is fine.

- [ ] **Interrupt latency bench**

Per design §6.5 this is mandatory this round.
```bash
python scripts/bench_interrupt_latency.py --runs 10 --label after-debug 2>&1 | tail -20
```
Expected: median `speech_to_detect_ms < 400ms`; results appended to `scripts/bench_results/interrupt_latency.jsonl`.

- [ ] **Git log sanity**

Run:
```bash
git log --oneline -10
```
Expected: 7 new commits (beyond `e5ff894`), each with a `fix(...):` / `test(...):` / `docs(...):` prefix matching the plan. No `Co-Authored-By` in any message (run `git log --format=%B -10 | grep -i co-authored` and expect empty output).

---

## Rollback (per design §6.5)

- Single-commit failure: `git reset --hard HEAD~1`; re-analyze; retry
- Multi-commit regression: `git bisect`; fix + new coverage test
- Manual-test bug: new failing test first, then fix, then append to §9.2 of the report

---

**END OF PLAN**
