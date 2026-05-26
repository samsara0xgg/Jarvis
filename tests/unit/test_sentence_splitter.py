"""Unit tests for ``jarvis.surface.sentence_splitter``.

Covers the boundary detection contract for ADR-0003 Step 2 chunked emission:

- ASCII + CJK terminators + newline as boundaries
- decimal-point guard (``3.14`` stays one sentence)
- abbreviation guard for the legacy 14-entry abbreviation list
- multi-dot abbreviations (``e.g.``, ``i.e.``) — every internal dot protected
- empty / whitespace-only inputs return ``[]``
- per-sentence leading/trailing whitespace stripping
- multi-newline paragraph breaks collapse empty intermediates
"""

from __future__ import annotations

import pytest

from jarvis.surface.sentence_splitter import split_into_sentences


def test_empty_string_returns_empty_list():
    """``""`` has no content → ``[]``."""
    assert split_into_sentences("") == []


def test_whitespace_only_returns_empty_list():
    """``"   "`` strips to empty → ``[]``."""
    assert split_into_sentences("   ") == []


def test_whitespace_with_newline_returns_empty_list():
    """Whitespace with embedded newline strips to empty and returns ``[]``."""
    assert split_into_sentences("   \n  ") == []


def test_no_terminator_returns_single_sentence():
    """Text with no boundary punctuation is returned as one stripped sentence."""
    assert split_into_sentences("hello world") == ["hello world"]


def test_no_terminator_strips_outer_whitespace():
    """Single-sentence return path strips leading/trailing whitespace."""
    assert split_into_sentences("  hello world  ") == ["hello world"]


def test_pure_ascii_period_exclamation_question():
    """``.``, ``!``, ``?`` each end a sentence."""
    result = split_into_sentences("Hi. Hello! How?")
    assert result == ["Hi.", "Hello!", "How?"]


def test_pure_cjk_punctuation():
    """``。``, ``！``, ``？`` each end a sentence."""
    result = split_into_sentences("你好。世界!再见？")
    # Use explicit CJK literal in the assertion:
    assert result == ["你好。", "世界!", "再见？"]


def test_pure_cjk_full_terminators():
    """All three CJK terminators end sentences."""
    assert split_into_sentences("你好。世界！再见？") == ["你好。", "世界！", "再见？"]


def test_mixed_cjk_and_ascii_in_one_input():
    """CJK and ASCII terminators coexist in a single input."""
    result = split_into_sentences("Hello. 你好。Bye!")
    assert result == ["Hello.", "你好。", "Bye!"]


def test_newline_is_a_boundary_and_is_consumed():
    """A newline character ends a sentence and is not retained in the output text."""
    assert split_into_sentences("line one\nline two") == ["line one", "line two"]


def test_paragraph_break_drops_empty_intermediate():
    """Double newline produces an empty intermediate that is dropped."""
    assert split_into_sentences("para one.\n\npara two.") == ["para one.", "para two."]


def test_leading_trailing_whitespace_stripped_per_sentence():
    """Each emitted sentence is individually stripped of outer whitespace."""
    assert split_into_sentences("  Hi.  Hello.  ") == ["Hi.", "Hello."]


def test_decimal_guard_pi():
    """``3.14`` between digits is NOT a split point."""
    assert split_into_sentences("Pi is 3.14 approx.") == ["Pi is 3.14 approx."]


def test_decimal_guard_multiple_decimals():
    """Multiple decimal numbers all stay intact."""
    result = split_into_sentences("Use 1.5 or 2.7 here.")
    assert result == ["Use 1.5 or 2.7 here."]


def test_decimal_guard_then_terminator():
    """Decimal in mid-sentence followed by a real terminator splits correctly."""
    result = split_into_sentences("Pi is 3.14. Ok?")
    assert result == ["Pi is 3.14.", "Ok?"]


@pytest.mark.parametrize(
    ("abbr", "text"),
    [
        ("Mrs.", "Mrs. Smith arrived."),
        ("Prof.", "Prof. Smith arrived."),
        ("Mr.", "Mr. Smith arrived."),
        ("Ms.", "Ms. Smith arrived."),
        ("Dr.", "Dr. Smith arrived."),
        ("Jr.", "John Jr. arrived."),
        ("Sr.", "John Sr. arrived."),
        ("St.", "Main St. is here."),
        ("Rd.", "Long Rd. is here."),
        ("Inc.", "Acme Inc. wins."),
        ("Ltd.", "Acme Ltd. wins."),
        ("vs.", "Cats vs. dogs win."),
    ],
)
def test_single_dot_abbreviation_guard(abbr: str, text: str):
    """Internal ``.`` in a known single-dot abbreviation does not split."""
    # The whole input should remain one sentence (only the trailing dot terminates).
    result = split_into_sentences(text)
    assert result == [text], f"Abbreviation {abbr!r} was not protected in {text!r}"


@pytest.mark.parametrize(
    ("abbr", "text"),
    [
        ("e.g.", "See e.g. this example."),
        ("i.e.", "Namely i.e. this one."),
    ],
)
def test_multi_dot_abbreviation_guard(abbr: str, text: str):
    """Every internal dot of multi-dot abbreviations (``e.g.``, ``i.e.``) is protected."""
    result = split_into_sentences(text)
    assert result == [text], f"Multi-dot abbreviation {abbr!r} was not protected in {text!r}"


def test_abbreviation_then_separate_sentence():
    """An abbreviation followed by a real sentence terminator splits at the terminator."""
    result = split_into_sentences("Dr. Smith arrived. He is on time.")
    assert result == ["Dr. Smith arrived.", "He is on time."]


def test_abbreviation_case_sensitive():
    """Abbreviation matching is case-sensitive (legacy parity).

    Lowercase ``dr.`` is NOT in the protected list, so a ``.`` after ``dr``
    terminates a sentence.
    """
    # "dr." is not in the abbreviation list; the dot ends a sentence.
    result = split_into_sentences("dr. smith arrived.")
    assert result == ["dr.", "smith arrived."]


def test_consecutive_terminators_collapse_empty_segments():
    """Two terminators in a row produce one sentence + an empty piece (dropped)."""
    # The empty segment between "!" and "?" strips to "" and is dropped.
    assert split_into_sentences("Hi!?") == ["Hi!", "?"]


def test_trailing_terminator_no_remainder():
    """Text ending in a terminator yields no extra empty remainder."""
    assert split_into_sentences("Done.") == ["Done."]


def test_no_remainder_after_final_terminator():
    """A terminator at the very end of input does not produce a trailing empty sentence."""
    assert split_into_sentences("First. Second.") == ["First.", "Second."]


def test_remainder_without_terminator_is_emitted():
    """A trailing fragment with no terminator is still emitted as a final sentence."""
    assert split_into_sentences("First. and more") == ["First.", "and more"]
