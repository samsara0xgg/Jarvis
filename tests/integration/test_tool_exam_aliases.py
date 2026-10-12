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


@pytest.mark.parametrize(
    ("menu", "kept"),
    [
        (None, ALIASES),  # no menu line before the run: every alias
        (["mail", "weather"], ALIASES),  # the replacement was offered
        (["mail_inbox", "weather"], {}),  # a run made before the merge: the old names stand
        ([], {}),
    ],
)
def test_an_alias_applies_only_when_its_replacement_was_on_the_menu(
    menu: list[str] | None, kept: dict[str, str],
) -> None:
    """The alias file is shared by branches; a menu without the new tool leaves old names alone."""
    assert tool_exam.usable_aliases(ALIASES, menu) == kept


def test_score_reads_each_run_through_the_aliases_of_the_menu_line_before_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """One file with two menus: a run before the merge keeps old names, one after maps them."""
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([
        {"id": "a", "said": "any mail?", "request": REQUEST, "tags": [],
         "expect": {"first": {"ok": ["mail_inbox"]}}},
        {"id": "b", "said": "any mail now?", "request": REQUEST, "tags": [],
         "expect": {"first": {"ok": ["mail_inbox"]}}},
    ]), encoding="utf-8")
    (tmp_path / "tool-aliases.json").write_text(json.dumps(ALIASES), encoding="utf-8")

    def run(case: str, name: str) -> dict[str, Any]:
        return {
            "kind": "run", "case": case, "preset": "p", "run": 0, "model": "m",
            "first_token_s": 0.5, "total_s": 1.0, "text": "", "finish_reason": "stop",
            "calls": [{"name": name, "args": {}}], "usage": None, "error": None,
        }

    lines = [
        {"kind": "menu", "tools": ["mail_inbox", "weather"], "chars": 1},
        run("a", "mail_inbox"),
        {"kind": "menu", "tools": ["mail", "weather"], "chars": 1},
        run("b", "mail"),
    ]
    results = tmp_path / "results.jsonl"
    results.write_text("".join(json.dumps(one) + "\n" for one in lines), encoding="utf-8")
    tool_exam.cmd_score(argparse.Namespace(results=results, cases=cases))
    out = capsys.readouterr().out
    assert "a  'any mail?'  p[PASS]: pass" in out  # old menu: mail_inbox still expected and called
    assert "b  'any mail now?'  p[PASS]: pass" in out  # new menu: mail_inbox read as mail
    lines[1] = run("a", "mail")  # the same call under the old menu is the wrong tool
    results.write_text("".join(json.dumps(one) + "\n" for one in lines), encoding="utf-8")
    tool_exam.cmd_score(argparse.Namespace(results=results, cases=cases))
    assert "a  'any mail?'  p[FAIL]: FAIL(mail)" in capsys.readouterr().out


def test_a_patch_rewrites_the_prompt_and_the_history_notes_alike() -> None:
    """A renamed tool is renamed in the system prompt and in the history notes that name it."""
    request = {"messages": [
        {"role": "system", "content": 'read it with gmail_get "now"'},
        {"role": "user", "content": "[Earlier days · use recall]\nhi"},
    ]}
    patches = [{"old": 'gmail_get "now"', "new": 'mail(read=) "now"'},
               {"old": "use recall]", "new": "use conversation]"}]
    assert tool_exam.patched(request, patches) == {"messages": [
        {"role": "system", "content": 'read it with mail(read=) "now"'},
        {"role": "user", "content": "[Earlier days · use conversation]\nhi"},
    ]}
