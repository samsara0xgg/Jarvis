"""Unit tests for the Tier 0 pattern table (spec §17 closed whitelist)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from jarvis.decision.tier0 import (
    Tier0ConfigError,
    Tier0Hit,
    load_tier0_table,
    match_tier0,
    render_tier0_response,
    validate_tier0_table,
)

if TYPE_CHECKING:
    from pathlib import Path

_VALID_YAML = """\
- id: time_now
  pattern: "^现在几点了?[?？。.!！]?$"
  tool: get_current_time
  template: "现在是{spoken_time}。"
- id: capture_demo
  pattern: "^加个待办[:：]\\\\s*(.+)$"
  tool: create_task
  args: { goal: "$1" }
  template: "记下了：{goal}。"
"""  # noqa: RUF001 — fullwidth CJK punctuation is the routing data under test.


def _write(tmp_path: Path, content: str) -> Path:
    """Write ``content`` to a tier0 pattern file inside ``tmp_path``."""
    p = tmp_path / "tier0_patterns.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_load_missing_file_returns_empty_table(tmp_path: Path) -> None:
    """Absent pattern file → Tier 0 disabled, not an error."""
    assert load_tier0_table(tmp_path / "nope.yaml") == ()


def test_load_valid_file_two_patterns(tmp_path: Path) -> None:
    """A well-formed file loads in file order with args preserved."""
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    assert [p.pattern_id for p in table] == ["time_now", "capture_demo"]
    assert table[0].tool_name == "get_current_time"
    assert table[1].arg_template == {"goal": "$1"}


def test_load_rejects_malformed_yaml_naming_the_file(tmp_path: Path) -> None:
    """A YAML *syntax* error surfaces as Tier0ConfigError naming the file.

    The header comment in ``config/tier0_patterns.yaml`` invites hand
    edits, so an unbalanced quote is the likeliest operator mistake.
    PyYAML's own mark says ``<unicode string>`` because the loader reads
    the text before parsing, so the path has to come from us or the
    operator is told nothing about *which* file to fix.
    """
    bad = '- id: x\n  pattern: "^x$\n  tool: get_current_time\n  template: "t"\n'
    with pytest.raises(Tier0ConfigError, match=r"tier0_patterns\.yaml"):
        load_tier0_table(_write(tmp_path, bad))


def test_load_rejects_unanchored_pattern(tmp_path: Path) -> None:
    """A pattern without ``^...$`` fails fast naming the entry (spec §17)."""
    bad = '- id: x\n  pattern: "现在几点"\n  tool: get_current_time\n  template: "t"\n'
    with pytest.raises(Tier0ConfigError, match=r"x.*anchored"):
        load_tier0_table(_write(tmp_path, bad))


def test_load_rejects_bad_regex(tmp_path: Path) -> None:
    """A regex that does not compile fails fast naming the entry."""
    bad = '- id: x\n  pattern: "^([$"\n  tool: get_current_time\n  template: "t"\n'
    with pytest.raises(Tier0ConfigError, match="x"):
        load_tier0_table(_write(tmp_path, bad))


def test_load_rejects_duplicate_id(tmp_path: Path) -> None:
    """Two entries sharing an id is a config error, not a silent override."""
    bad = (
        '- id: x\n  pattern: "^a$"\n  tool: t1\n  template: "t"\n'
        '- id: x\n  pattern: "^b$"\n  tool: t1\n  template: "t"\n'
    )
    with pytest.raises(Tier0ConfigError, match="duplicate"):
        load_tier0_table(_write(tmp_path, bad))


def test_load_rejects_missing_keys(tmp_path: Path) -> None:
    """An entry missing required keys fails fast naming the entry."""
    bad = '- id: x\n  pattern: "^a$"\n'
    with pytest.raises(Tier0ConfigError, match="x"):
        load_tier0_table(_write(tmp_path, bad))


def test_load_rejects_non_list_document(tmp_path: Path) -> None:
    """Top-level YAML that is not a list is a config error."""
    with pytest.raises(Tier0ConfigError, match="list"):
        load_tier0_table(_write(tmp_path, "key: value\n"))


def test_load_rejects_out_of_range_group_ref(tmp_path: Path) -> None:
    """An ``args`` ref to a group the regex lacks fails at load, not mid-turn.

    ``$0`` (whole match) is rejected too: valid refs are ``$1..$N`` only.
    """
    too_high = (
        '- id: x\n  pattern: "^a(.+)$"\n  tool: t1\n  args: { goal: "$5" }\n  template: "t"\n'
    )
    with pytest.raises(Tier0ConfigError, match=r"x.*group"):
        load_tier0_table(_write(tmp_path, too_high))
    whole_match = (
        '- id: x\n  pattern: "^a(.+)$"\n  tool: t1\n  args: { goal: "$0" }\n  template: "t"\n'
    )
    with pytest.raises(Tier0ConfigError, match=r"x.*group"):
        load_tier0_table(_write(tmp_path, whole_match))


def test_validate_rejects_tool_not_in_allowed_set(tmp_path: Path) -> None:
    """A pattern targeting a tool regex_router may not call fails validation."""
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    with pytest.raises(Tier0ConfigError, match=r"capture_demo.*create_task"):
        validate_tier0_table(
            table,
            allowed_tool_names=frozenset({"get_current_time"}),
            async_tool_names=frozenset(),
        )


def test_validate_rejects_async_tool(tmp_path: Path) -> None:
    """Tier 0 dispatches sync tools only; an async target fails validation."""
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    with pytest.raises(Tier0ConfigError, match="async"):
        validate_tier0_table(
            table,
            allowed_tool_names=frozenset({"get_current_time", "create_task"}),
            async_tool_names=frozenset({"create_task"}),
        )


def test_validate_passes_clean_table(tmp_path: Path) -> None:
    """A table whose tools are all allowed and sync validates silently."""
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    validate_tier0_table(
        table,
        allowed_tool_names=frozenset({"get_current_time", "create_task"}),
        async_tool_names=frozenset(),
    )


def test_match_hits_full_sentence_only(tmp_path: Path) -> None:
    """Anchoring means only whole utterances hit; any extra text misses."""
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    assert match_tier0("现在几点", table) is not None
    assert match_tier0("现在几点了？", table) is not None  # noqa: RUF001 — fullwidth question mark is the CJK utterance under test.
    assert match_tier0("  现在几点  ", table) is not None  # strip
    assert match_tier0("请问现在几点", table) is None  # prefix breaks anchor
    assert match_tier0("现在几点了顺便查天气", table) is None
    assert match_tier0("昨天那个 task 给 Codex 跑一下", table) is None


def test_match_extracts_capture_group_args(tmp_path: Path) -> None:
    """``$N`` arg templates resolve to the matching capture group."""
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    hit = match_tier0("加个待办：买牛奶", table)  # noqa: RUF001 — fullwidth colon is the CJK utterance under test.
    assert hit is not None
    assert hit.tool_name == "create_task"
    assert hit.tool_args == {"goal": "买牛奶"}


def test_render_fills_payload_and_args(tmp_path: Path) -> None:
    """The response template is filled from the tool payload scalars."""
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    hit = match_tier0("现在几点", table)
    assert hit is not None
    text = render_tier0_response(hit, {"spoken_time": "下午3点42分"})
    assert text == "现在是下午3点42分。"


def test_render_missing_variable_falls_back(tmp_path: Path) -> None:
    """A template variable no source provides degrades, never crashes."""
    table = load_tier0_table(_write(tmp_path, _VALID_YAML))
    hit = match_tier0("现在几点", table)
    assert hit is not None
    text = render_tier0_response(hit, {"unrelated": "x"})
    assert "模板变量缺失" in text
    assert "完成" not in text  # must stay Pre-emit-Gate safe


def test_render_malformed_format_spec_falls_back(tmp_path: Path) -> None:
    """A bad format spec degrades to the fallback instead of raising ValueError."""
    src = (
        '- id: bad_tpl\n  pattern: "^现在几点$"\n  tool: get_current_time\n'
        '  template: "现在是{spoken_time:!}。"\n'
    )
    table = load_tier0_table(_write(tmp_path, src))
    hit = match_tier0("现在几点", table)
    assert hit is not None
    text = render_tier0_response(hit, {"spoken_time": "下午3点42分"})
    assert "模板变量缺失" in text
    assert "完成" not in text  # must stay Pre-emit-Gate safe


@pytest.mark.parametrize(
    "template",
    [
        "现在是{spoken_time.hour}点。",  # AttributeError — attribute access on a stringified value
        "{spoken_time[label]}",  # TypeError — string indices must be integers
        "{spoken_time[9]}",  # IndexError — string index out of range
        "现在是{spoken_time:!}。",  # ValueError — unknown format code
        "现在是{spoken_time",  # ValueError — unmatched brace
        "{0}",  # IndexError — positional ref, no positional args
        "{spoken_time!z}",  # ValueError — unknown conversion specifier
        "{missing_var}",  # KeyError — no such variable
    ],
)
def test_render_never_raises_on_authored_template(template: str) -> None:
    """No config-authored template may crash the turn; each degrades to the fallback.

    Every payload value is stringified before formatting, so the reachable
    ``str.format`` failure family is exactly KeyError / IndexError /
    ValueError / AttributeError / TypeError. This table pins all five.
    """
    hit = Tier0Hit(
        pattern_id="tpl",
        tool_name="get_current_time",
        tool_args={},
        response_template=template,
    )
    text = render_tier0_response(hit, {"spoken_time": "下午3点42分"})
    assert "模板变量缺失" in text
    assert "完成" not in text  # must stay Pre-emit-Gate safe
