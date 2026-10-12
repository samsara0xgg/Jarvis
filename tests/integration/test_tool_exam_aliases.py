"""``tool_exam score`` reads the tool names in a case's expectation through ``tool-aliases.json``.

No provider is called: runs are written as the exam would have, and ``judge`` and ``cmd_score``
read them. The cases stay as they were logged; the alias file says which tool replaced which.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING, Any

import pytest

from scripts import tool_exam

if TYPE_CHECKING:
    from pathlib import Path

ALIASES = {
    "mail_inbox": "mail",
    "mcp__gmail__gmail_search": "mail",
    "mcp__gmail__gmail_get": "mail",
}
REQUEST: dict[str, Any] = {"messages": [{"role": "user", "content": "x"}]}


def _line(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"calls": [{"name": name, "args": args or {}}], "text": "", "error": None}


@pytest.mark.parametrize(
    ("expect", "call", "aliases", "verdict"),
    [
        # An ok name that was merged is satisfied by the new tool; unaliased, it is a miss.
        ({"ok": ["mail_inbox"]}, ("mail", {}), ALIASES, (True, "pass")),
        ({"ok": ["mail_inbox"]}, ("mail", {}), None, (False, "FAIL(mail)")),
        ({"ok": ["mcp__gmail__gmail_search", "text"]}, ("mail", {"query": "x"}), ALIASES,
         (True, "pass")),
        # A bad name that was merged fails the new tool.
        ({"ok": ["text"], "bad": ["mcp__gmail__gmail_get"]}, ("mail", {"read": "m1"}), ALIASES,
         (False, "FAIL(mail)")),
        ({"ok": ["text"], "bad": ["mcp__gmail__gmail_get"]}, ("mail", {"read": "m1"}), None,
         (False, "FAIL(mail)")),
        # A name the file does not mention is read as it is.
        ({"ok": ["weather"]}, ("weather", {}), ALIASES, (True, "pass")),
        ({"ok": ["weather"]}, ("mail", {}), ALIASES, (False, "FAIL(mail)")),
        # An args_must_not rule moves to the new name and trips on the arguments it names.
        ({"ok": ["mail_inbox"], "args_must_not": {"mail_inbox": {"query": "*"}}},
         ("mail", {"query": "x"}), ALIASES, (False, "FAIL(mail~args)")),
        ({"ok": ["mail_inbox"], "args_must_not": {"mail_inbox": {"query": "*"}}},
         ("mail", {}), ALIASES, (True, "pass")),
    ],
)
def test_judge_reads_expected_names_through_the_aliases(
    expect: dict[str, Any], call: tuple[str, dict[str, Any]],
    aliases: dict[str, str] | None, verdict: tuple[bool, str],
) -> None:
    """ok, bad and args_must_not are compared after the old names are mapped to the new."""
    assert tool_exam.judge(expect, _line(*call), REQUEST, aliases) == verdict


def test_two_rules_for_old_names_that_merged_both_apply() -> None:
    """Old tools folded into one keep each of their args_must_not rules."""
    expect = {"ok": ["mail"], "args_must_not": {
        "mcp__gmail__gmail_get": {"format": "full"}, "mcp__gmail__gmail_search": ["spam"],
    }}
    assert tool_exam.judge(expect, _line("mail", {"format": "full"}), REQUEST, ALIASES) == (
        False, "FAIL(mail~args)",
    )
    assert tool_exam.judge(expect, _line("mail", {"query": "spam"}), REQUEST, ALIASES) == (
        False, "FAIL(mail~args)",
    )
    assert tool_exam.judge(expect, _line("mail", {"query": "co-op"}), REQUEST, ALIASES) == (
        True, "pass",
    )


def test_load_aliases_reads_the_file_beside_the_cases_and_none_is_empty(tmp_path: Path) -> None:
    """The file is optional."""
    cases = tmp_path / "cases.json"
    assert tool_exam.load_aliases(cases) == {}
    (tmp_path / "tool-aliases.json").write_text(json.dumps(ALIASES), encoding="utf-8")
    assert tool_exam.load_aliases(cases) == ALIASES


def test_score_judges_an_unedited_case_with_the_alias_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: a case that expects mail_inbox passes a run that called mail."""
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{
        "id": "inbox", "said": "anything for me?", "request": REQUEST, "tags": [],
        "expect": {"first": {"ok": ["mail_inbox"], "bad": ["tool_search"]}},
    }]), encoding="utf-8")
    results = tmp_path / "results.jsonl"
    run = {
        "kind": "run", "case": "inbox", "preset": "p", "run": 0, "model": "m",
        "first_token_s": 0.5, "total_s": 1.0, "text": "", "finish_reason": "stop",
        "calls": [{"name": "mail", "args": {}}], "usage": None, "error": None,
    }
    results.write_text(json.dumps(run) + "\n", encoding="utf-8")

    def score() -> str:
        tool_exam.cmd_score(argparse.Namespace(results=results, cases=cases))
        return capsys.readouterr().out

    assert "inbox  'anything for me?'  p[FAIL]: FAIL(mail)" in score()
    (tmp_path / "tool-aliases.json").write_text(json.dumps(ALIASES), encoding="utf-8")
    assert "inbox  'anything for me?'  p[PASS]: pass" in score()
