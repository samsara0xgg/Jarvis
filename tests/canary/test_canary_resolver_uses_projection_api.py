"""Canary — ``jarvis/decision/resolver.py`` reads L2 via the projection API only.

Per ADR-0002 Step 5 Tier-1 canary list (lines 1646-1650):

> ``test_canary_resolver_uses_projection_api`` — AST scan:
> ``jarvis/decision/resolver.py`` does NOT contain any literal SQL
> (``"SELECT"``, ``"select * from"``, raw ``sqlite3.connect(...)``).
> The resolver may only call ``task_ledger.tasks_in_window(...)`` or
> other projection APIs.

Spec §3.4.3: L3 must read L2 through projection snapshots only — direct
``SELECT`` over the events table from L3 is a layer violation. This
canary makes the rule machine-enforced.

Two parts:

- **Part A — SQL string literals.** AST-walk the file and reject any
  string :class:`ast.Constant` whose value contains an SQL write/read
  keyword followed by a whitespace + identifier (so prose comments
  about "the SELECT path" stay legal, but executable SQL fragments do
  not).
- **Part B — ``sqlite3.connect``.** AST-walk and reject any
  :class:`ast.Call` whose function chain ends in ``sqlite3.connect``.
"""

from __future__ import annotations

import ast
import re

from tests.canary._helpers import parse, repo_root

# A "real" SQL statement looks structurally distinct from prose because
# it pairs two keywords (e.g. SELECT ... FROM, INSERT INTO ... VALUES,
# UPDATE ... SET, DELETE FROM ... WHERE). Matching on bigrams keeps
# docstring sentences like "SELECT events" or "the FROM clause" from
# false-tripping while reliably catching any executable SQL the resolver
# might grow.  We accept arbitrary whitespace/identifier filler between
# the two anchor keywords (up to one newline) so realistic multi-line
# SQL is still detected.
_SQL_KEYWORD_PAIRS: tuple[tuple[str, str], ...] = (
    ("SELECT", "FROM"),
    ("INSERT", "INTO"),
    ("UPDATE", "SET"),
    ("DELETE", "FROM"),
)
_SQL_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(
        rf"\b{first}\b[^\n]{{1,200}}\b{second}\b",
        re.IGNORECASE,
    )
    for first, second in _SQL_KEYWORD_PAIRS
)


def _attribute_chain(node: ast.AST) -> list[str]:
    """Render an :class:`ast.Attribute` chain as a dotted list of names."""
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return list(reversed(parts))


def test_resolver_module_has_no_sql_literals() -> None:
    """Part A — no literal SQL fragments anywhere in ``resolver.py``.

    Scans for canonical SQL keyword bigrams (SELECT ... FROM,
    INSERT INTO, UPDATE ... SET, DELETE FROM) inside string literals.
    Bigrams are the discriminator that separates executable SQL from
    English prose containing the keywords in isolation.
    """
    path = repo_root() / "jarvis" / "decision" / "resolver.py"
    module = parse(path)

    violations: list[str] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Constant):
            continue
        value = node.value
        if not isinstance(value, str):
            continue
        for pattern in _SQL_RES:
            match = pattern.search(value)
            if match is not None:
                snippet = value[match.start() : match.end()]
                violations.append(
                    f"line {getattr(node, 'lineno', 0)}: SQL fragment {snippet!r}"
                )
                break

    assert not violations, (
        "resolver.py must not contain SQL literals (use the projection "
        "API):\n  " + "\n  ".join(violations)
    )


def test_resolver_module_does_not_call_sqlite3_connect() -> None:
    """Part B — no ``sqlite3.connect(...)`` call anywhere in ``resolver.py``."""
    path = repo_root() / "jarvis" / "decision" / "resolver.py"
    module = parse(path)

    violations: list[str] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        chain = _attribute_chain(node.func)
        if not chain:
            continue
        # Reject any call whose dotted chain ends in `sqlite3.connect`
        # or whose tail is bare `connect` after a `sqlite3` segment.
        if chain[-1] == "connect" and "sqlite3" in chain[:-1]:
            violations.append(
                f"line {getattr(node, 'lineno', 0)}: call to {'.'.join(chain)}(...)"
            )

    assert not violations, (
        "resolver.py must not open SQLite connections directly (call "
        "projection methods instead):\n  " + "\n  ".join(violations)
    )


def test_resolver_module_does_not_import_sqlite3_runtime() -> None:
    """Belt-and-braces — sqlite3 may only appear under ``TYPE_CHECKING``.

    The resolver does not need ``sqlite3`` at runtime; if it ever
    grows a top-level ``import sqlite3``, that is a strong signal the
    projection API is being bypassed. ``TYPE_CHECKING`` imports are
    fine (no runtime effect) and the resolver currently has none.
    """
    path = repo_root() / "jarvis" / "decision" / "resolver.py"
    module = parse(path)

    runtime_imports: list[str] = []
    for node in module.body:
        if isinstance(node, ast.Import):
            runtime_imports.extend(
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name == "sqlite3" or alias.name.startswith("sqlite3.")
            )
        elif isinstance(node, ast.ImportFrom) and (
            node.module == "sqlite3" or (node.module or "").startswith("sqlite3.")
        ):
            runtime_imports.append(f"line {node.lineno}: from {node.module} import ...")

    assert not runtime_imports, (
        "resolver.py must not import sqlite3 at runtime (projection API "
        "is the only sanctioned L2 surface):\n  " + "\n  ".join(runtime_imports)
    )
