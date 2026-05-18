"""H6 — every ruff ignore entry has an adjacent justifying comment.

Per ADR 0001 § Acceptance criterion H6:

> Scans ``pyproject.toml`` for ruff ignore entries; every entry must
> have a one-line code comment on the same line or immediately above
> it. Unjustified additions = regression.

Two ignore blocks are scanned:

1. ``[tool.ruff.lint]`` ``ignore = [...]`` — top-level project-wide
   ignores.
2. ``[tool.ruff.lint.per-file-ignores]`` — every per-file mapping value
   is itself a list of rule strings; each rule must be justified.

A rule line is "justified" if EITHER

- The same line carries an inline comment (``"D100",  # ...``), OR
- The immediately preceding non-blank line is a ``#`` comment line.

Lines that are not rule entries (section headers, bracket-only lines,
blank lines, comment-only lines) are skipped.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tests.canary._helpers import repo_root

if TYPE_CHECKING:
    from collections.abc import Callable

_PYPROJECT_REL = "pyproject.toml"

# Match section headers we care about. Any other ``[section]`` line ends
# the previous scanning context.
_SECTION_HEADER_RE = re.compile(r"^\s*\[(?P<name>[\w\.\-]+)\]\s*$")

# Rule entry: a string-literal first thing on the line. May be followed
# by whitespace, a comma, and an optional ``#`` comment.
_RULE_LINE_RE = re.compile(
    r'^\s*(?P<quote>"|\')(?P<rule>[A-Z]+\d+|[A-Z]+\d*[a-z]?)(?P=quote)\s*,?\s*(?P<inline>#.*)?$'
)

# A per-file-ignores key/value line of the shape:
# "<glob>" = ["RULE1", "RULE2",  # justification]
# When the list closes on the same line we still want to validate every
# rule has a comment. This case is unusual (the project formats per-file
# ignores with one rule per line) but we cover it defensively.
_INLINE_LIST_RE = re.compile(r'^\s*"[^"]+"\s*=\s*\[(?P<body>.+)\]\s*$')


def _is_blank(line: str) -> bool:
    return line.strip() == ""


def _is_comment_line(line: str) -> bool:
    return line.lstrip().startswith("#")


def _previous_non_blank_is_comment(lines: list[str], idx: int) -> bool:
    """True iff the closest preceding non-blank line is a ``#`` comment."""
    for prev in range(idx - 1, -1, -1):
        if _is_blank(lines[prev]):
            continue
        return _is_comment_line(lines[prev])
    return False


def _scan_inline_list(body: str) -> list[str]:
    """Pull rule literals out of a same-line list body.

    A single-line list is justified iff every rule literal is followed
    by a same-list comment. Since we cannot reliably detect that from a
    single-line list, we treat any inline rules as UNJUSTIFIED unless
    the whole entry has a same-line trailing comment. We flag each rule
    so the diagnostic is precise.
    """
    return re.findall(r'"([A-Z]+\d+[a-z]?)"', body)


def _check_inline_list_line(
    line: str,
    lines: list[str],
    idx: int,
) -> list[str]:
    """Check a same-line list (e.g. ``"path" = ["A", "B"]``). Empty if OK."""
    inline_list = _INLINE_LIST_RE.match(line)
    if inline_list is None:
        return []
    rules = _scan_inline_list(inline_list.group("body"))
    inline_comment = "#" in line.split("]", 1)[1] if "]" in line else False
    above_comment = _previous_non_blank_is_comment(lines, idx)
    if not rules or inline_comment or above_comment:
        return []
    return [
        f"pyproject.toml:{idx + 1}: rule {rule!r} has no justification "
        "(neither inline comment nor a comment immediately above)"
        for rule in rules
    ]


def _check_rule_line(line: str, lines: list[str], idx: int) -> str | None:
    """Check one rule line (``"D100", # ...``). Return violation message or None."""
    rule_match = _RULE_LINE_RE.match(line)
    if rule_match is None:
        return None
    rule = rule_match.group("rule")
    inline = rule_match.group("inline")
    if inline is not None and inline.startswith("#"):
        return None
    if _previous_non_blank_is_comment(lines, idx):
        return None
    return (
        f"pyproject.toml:{idx + 1}: rule {rule!r} has no justifying comment "
        "(neither inline nor on the line above)"
    )


def _scan_block(
    lines: list[str],
    section_pred: Callable[[str], bool],
) -> list[str]:
    """Return justification violations under sections matching ``section_pred``.

    Walks line-by-line. Inside a matching section we look for bracketed
    lists (``ignore = [`` opens, ``]`` closes) or per-file inline lists.
    Rule entries on their own line are validated for justification.
    """
    violations: list[str] = []
    in_section = False
    in_list = False
    for idx, raw in enumerate(lines):
        line = raw.rstrip("\n")

        header = _SECTION_HEADER_RE.match(line)
        if header is not None:
            in_section = section_pred(header.group("name"))
            in_list = False
            continue
        if not in_section:
            continue

        # Track bracketed lists. We accept the simple ``ignore = [`` /
        # ``"path" = [`` openers and treat ``]`` as the closer.
        stripped = line.strip()
        if stripped.endswith("["):
            in_list = True
            continue
        if stripped.startswith("]") or stripped == "]":
            in_list = False
            continue

        # Same-line list — `"path" = ["A", "B"]`. Validate as one unit.
        inline_hits = _check_inline_list_line(line, lines, idx)
        if inline_hits:
            violations.extend(inline_hits)
            continue
        if _INLINE_LIST_RE.match(line) is not None:
            # Matched the inline-list shape but was justified; don't fall
            # through to per-line rule scan.
            continue

        # Only validate inside the bracketed list contexts we're tracking.
        if not in_list:
            continue

        rule_violation = _check_rule_line(line, lines, idx)
        if rule_violation is not None:
            violations.append(rule_violation)
    return violations


def test_pyproject_ruff_ignores_are_justified() -> None:
    """Every ruff ignore entry (top-level + per-file) carries a comment."""
    pyproject = repo_root() / _PYPROJECT_REL
    text = pyproject.read_text(encoding="utf-8")
    lines = text.splitlines()

    violations: list[str] = []
    # 1. [tool.ruff.lint] ignore = [...]
    violations.extend(
        _scan_block(lines, section_pred=lambda name: name == "tool.ruff.lint")
    )
    # 2. [tool.ruff.lint.per-file-ignores] — every per-file glob's list
    #    is itself a bracketed list whose rule entries need justification.
    violations.extend(
        _scan_block(
            lines, section_pred=lambda name: name == "tool.ruff.lint.per-file-ignores"
        )
    )

    assert not violations, (
        "H6 — unjustified ruff ignore entries:\n  " + "\n  ".join(violations)
    )
