"""Assert the mechanical half of the tool standard (docs/spec.html#tools, ADR 0317).

Builds the menu this checkout offers the decision model, as ``scripts.tool_exam`` does, and
checks every built-in tool on it: a name that is lowercase words joined by underscores, a
description of at most 600 characters written in English that calls him "he", and a
description on every parameter. Plugin tools (``mcp__``) are their servers' and are not checked.

It also fails a tool module that reads the wall clock itself (ADR 0322): a tool takes the time of
day from the clock in its context, so that the runtime's clock and the exam's frozen one reach it.
The modules that wait on real time are exempt (:data:`REAL_TIME`), and so is ``time.monotonic``,
which the dispatcher and ``web_fetch`` use for their call deadlines.

Tools that predate the standard are listed in ``PRE_STANDARD`` with the rules they still break.
They are reported, not failed, and each one leaves the list when it is rebuilt to the standard.
What fails is a rule a tool breaks that its entry does not name, and an entry that no longer
matches a tool or names a rule the tool now keeps, so the list can only shrink.

    .venv/bin/python -m scripts.check_tools
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from jarvis.shared import CallerPrincipal
from scripts.tool_exam import _exam_registry

if TYPE_CHECKING:
    from collections.abc import Mapping

REPO = Path(__file__).resolve().parent.parent
DESCRIPTION_CAP = 600
NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
CJK_RE = re.compile(r"[　-鿿＀-￯]")
THE_USER_RE = re.compile(r"\bthe user\b", re.IGNORECASE)

REAL_TIME: frozenset[str] = frozenset({"workers.py", "codex_app_server.py", "mcp_oauth.py"})
"""The tool modules whose clock is real time by nature: subprocess deadlines, token expiry."""
_WALL_READS: frozenset[tuple[str, str]] = frozenset({
    ("time", "time"), ("time", "sleep"), ("time", "localtime"), ("time", "gmtime"),
    ("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"), ("date", "today"),
})

# Every built-in tool that broke a rule when the standard was set (2026-10-17), with the rules
# it breaks. Remove a rule when the tool keeps it, and the entry when it keeps them all.
PRE_STANDARD: dict[str, frozenset[str]] = {
    name: frozenset(rules.split())
    for name, rules in {
        "archive": "long",
        "ask_user": "long the-user",
        "disable_plugin": "the-user undescribed",
        "end_night_run": "the-user",
        "get_briefing": "undescribed",
        "job_ledger": "the-user",
        "list_plugins": "the-user",
        "look_at_picture": "the-user",
        "open_path": "the-user",
        "open_plugin": "long the-user undescribed",
        "open_url": "the-user",
        "query_activity": "long undescribed",
        "read_activity": "undescribed",
        "read_records": "undescribed",
        "recall": "long the-user undescribed",
        "refresh_work_state": "long the-user undescribed",
        "remove_plugin": "the-user undescribed",
        "save_briefing": "undescribed",
        "save_knowledge": "the-user undescribed",
        "screen_look": "the-user",
        "search_knowledge": "undescribed",
        "search_notes": "the-user",
        "search_records": "long the-user undescribed",
        "set_voice": "long the-user undescribed",
        "show_on_dashboard": "long undescribed",
        "start_night_run": "the-user",
    }.items()
}


def _undescribed(schema: Mapping[str, Any], path: str = "") -> list[str]:
    """Parameters, nested ones included, that carry no description."""
    missing: list[str] = []
    for name, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        if not prop.get("description"):
            missing.append(path + name)
        if prop.get("type") == "object":
            missing += _undescribed(prop, f"{path}{name}.")
        items = prop.get("items")
        if isinstance(items, dict) and items.get("type") == "object":
            missing += _undescribed(items, f"{path}{name}[].")
    return missing


def broken_rules(name: str, description: str, schema: Mapping[str, Any]) -> dict[str, str]:
    """Each rule this tool breaks -> what breaks it."""
    broken: dict[str, str] = {}
    if not NAME_RE.fullmatch(name):
        broken["name"] = name
    if len(description) > DESCRIPTION_CAP:
        broken["long"] = f"{len(description)} > {DESCRIPTION_CAP} characters"
    if CJK_RE.search(description):
        broken["not-english"] = "Chinese text in the description"
    if THE_USER_RE.search(description):
        broken["the-user"] = 'says "the user", not "he"'
    missing = _undescribed(schema)
    if missing:
        broken["undescribed"] = ", ".join(missing)
    return broken


def wall_clock_reads() -> tuple[int, list[str]]:
    """``(modules scanned, each wall-clock read)`` in the tool modules outside :data:`REAL_TIME`."""
    scanned, found = 0, []
    for path in sorted((REPO / "jarvis" / "execution").glob("*.py")):
        if path.name in REAL_TIME:
            continue
        scanned += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Attribute):
                continue
            base = node.value
            owner = base.id if isinstance(base, ast.Name) else (
                base.attr if isinstance(base, ast.Attribute) else ""
            )
            if (owner, node.attr) in _WALL_READS:
                found.append(
                    f"{path.relative_to(REPO)}:{node.lineno}: reads {owner}.{node.attr}; "
                    "take the time from ctx.clock",
                )
    return scanned, found


def checked_tools() -> dict[str, dict[str, str]]:
    """Every built-in tool the decision model is offered -> the rules it breaks."""
    config = yaml.safe_load((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        no_plugins = Path(tmp) / "mcp-tools.json"
        no_plugins.write_text(json.dumps({}), encoding="utf-8")
        registry = _exam_registry(config, no_plugins)
    return {
        tool.name: broken_rules(tool.name, tool.description, tool.input_schema)
        for tool in registry.for_caller(CallerPrincipal.JARVIS_LLM)
        if not tool.name.startswith("mcp__")
    }


def failures(tools: Mapping[str, Mapping[str, str]]) -> list[str]:
    """What fails the gate: a rule broken outside ``PRE_STANDARD``, or a stale entry."""
    out: list[str] = []
    for name, broken in sorted(tools.items()):
        allowed = PRE_STANDARD.get(name, frozenset())
        out += [f"{name}: {rule} ({why})" for rule, why in broken.items() if rule not in allowed]
        out += [
            f"{name}: keeps {rule} now; remove it from PRE_STANDARD"
            for rule in sorted(allowed - broken.keys())
        ]
    out += [f"{name}: no such tool; remove it from PRE_STANDARD" for name in PRE_STANDARD
            if name not in tools]
    return out


def main() -> int:
    """Print the counts, then each failure; exit nonzero on any."""
    tools = checked_tools()
    legacy = sum(1 for name in tools if name in PRE_STANDARD)
    sys.stdout.write(f"tools checked: {len(tools)}   pre-standard: {legacy}\n")
    found = failures(tools)
    scanned, reads = wall_clock_reads()
    sys.stdout.write(f"tool modules scanned for wall-clock reads: {scanned}   reads: {len(reads)}\n")  # noqa: E501
    found += reads
    for line in found:
        sys.stdout.write(f"FAIL {line}\n")
    if not found:
        sys.stdout.write("OK\n")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
