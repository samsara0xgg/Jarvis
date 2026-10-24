"""ADR 0360, 0363: a long text in parts cut between paragraphs, or read where a question is."""

from __future__ import annotations

from jarvis.execution.paragraphs import PART_CHARS, parts, pick, split


def _paragraphs(count: int, size: int) -> str:
    return _text(*(f"Paragraph {n}. " + "word " * (size // 5) for n in range(count)))


def _text(*paragraphs: str) -> str:
    """The paragraphs with a blank line between them."""
    return "\n\n".join(paragraphs)


def test_a_text_is_cut_into_parts_between_paragraphs_never_inside_one() -> None:
    """Twelve thousand characters make four parts; each holds whole paragraphs, in order."""
    text = _paragraphs(24, 500)
    pieces = parts(text)
    assert len(text) > 12_000
    assert len(pieces) == 4
    assert all(len(one) <= PART_CHARS for one in pieces)
    assert "\n\n".join(pieces) == "\n\n".join(split(text))
    assert all(one.startswith("Paragraph ") for one in pieces)


def test_a_paragraph_longer_than_a_part_is_cut_at_its_lines_then_at_a_space() -> None:
    """The cut falls on a line break when there is one, else on a space, never mid-word."""
    lined = "\n".join(f"line {n} " + "x" * 90 for n in range(60))
    for piece in split(lined, 1000):
        assert len(piece) <= 1000
        assert piece.splitlines()[-1].startswith("line ")
    spaced = ("alpha " * 700).strip()
    pieces = split(spaced, 1000)
    assert len(pieces) == 5
    assert all(len(one) <= 1000 and set(one.split()) == {"alpha"} for one in pieces)


def test_a_run_of_cjk_characters_with_no_space_is_cut_at_the_limit() -> None:
    """Chinese has no spaces to cut at: the run is cut at the limit and nothing is lost."""
    run = "中" * 2500
    pieces = split(run, 1000)
    assert [len(one) for one in pieces] == [1000, 1000, 500]
    assert "".join(pieces) == run


def test_an_empty_text_has_no_parts() -> None:
    """Nothing in, nothing out: no empty part."""
    assert parts("  \n\n ") == []
    assert pick("", "anything").text == ""


def test_picking_keeps_the_opening_and_the_paragraph_that_matches_in_text_order() -> None:
    """The opening, then what matches best; the rest is counted as left out."""
    filler = ["Nothing relevant here, only a long ramble about the weather today."] * 5
    text = _text(
        "Opening: a note about the March statement.", *filler,
        "The minimum payment due is $35.00 by November 3.", *filler,
        "Closing remarks and a signature.",
    )
    picked = pick(text, "minimum payment due date", limit=300)
    assert picked.text.split("\n\n") == [
        "Opening: a note about the March statement.",
        "The minimum payment due is $35.00 by November 3.",
    ]
    assert picked.left_out == 11


def test_a_rare_word_counts_for_more_than_a_common_one() -> None:
    """Of two paragraphs that fit, the one with the rare word is picked first."""
    text = _text(
        "Opening line.",
        "the the the common words the the plan plan plan",
        "the plan and the invoice",
        "unrelated words entirely here",
    )
    picked = pick(text, "plan invoice", limit=60)
    assert "invoice" in picked.text
    assert picked.left_out == 2


def test_picking_reads_chinese_and_ignores_accents_and_case() -> None:
    """Chinese matches by pairs of characters, Latin words without accents or case."""
    text = _text(
        "开头一段",
        "这是一段无关的话",
        "应付金额为一百元 请在十月前付款",
        "Café menu for Friday",
    )
    assert "应付金额" in pick(text, "应付金额是多少", limit=80).text
    assert "Café menu" in pick(text, "CAFE MENU", limit=80).text


def test_a_text_that_matches_nothing_gives_only_its_opening() -> None:
    """No match: the opening paragraph, and every other paragraph counted as left out."""
    picked = pick("First.\n\nSecond.\n\nThird.", "zebra", limit=100)
    assert (picked.text, picked.left_out) == ("First.", 2)
