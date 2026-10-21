"""Canary — the runtime asks L2 named questions and writes no SQL over the event log (ADR 0349).

``import-linter`` reads imports, not strings, so a query written in ``jarvis/runtime`` over the
log's columns or payload keys breaks silently when L2 renames one. This reads the string
literals of every ``jarvis/runtime/**/*.py`` and counts the files that hold SQL: a SELECT, INSERT,
UPDATE or DELETE statement, or a ``json_extract`` call. Docstrings and comments are prose and are
not read; a string built from literals joined by ``+``, or an f-string, is read whole.

The count only goes down. ``_SQL_FILES`` is the number of files still holding SQL; the commit
that moves a file's last query into ``jarvis/state`` lowers it, and no commit raises it, since a
new question belongs in ``jarvis/state`` as a function that says what it answers. No file is
named here (ADR 0325: a canary's rule sits with the thing, not in a list of file names): a
failure prints the files that hold SQL now.

Canary house style: stdlib only, no import of the code under test.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

from tests.canary._helpers import relative_to_repo, repo_root

# SQL keywords are matched in capitals, the way every statement in this repository is written, so
# prose such as "select a device from the list" is not taken for a query; the function is not.
_SQL: Final = re.compile(
    r"\bSELECT\b.+?\bFROM\b"
    r"|\bINSERT\s+(?:OR\s+\w+\s+)?INTO\b"
    r"|\bUPDATE\s+\w+\s+SET\b"
    r"|\bDELETE\s+FROM\b"
    r"|(?i:\bjson_extract\s*\()",
    re.DOTALL,
)

# Files under jarvis/runtime that still hold SQL. Lower it in the commit that moves a file's
# last query into jarvis/state; it never goes up.
_SQL_FILES: Final = 8


def _text(node: ast.expr) -> str | None:
    """The text of a string literal, an f-string or a ``+`` of those; ``None`` for anything else.

    An f-string's expressions read as ``{}``, so the SQL around them stays in one piece.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(part.value) if isinstance(part, ast.Constant) else "{}" for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        return None if left is None or right is None else left + right
    return None


def _docstrings(tree: ast.Module) -> set[int]:
    """The ids of the string nodes that are docstrings."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def sql_lines(src: str) -> list[int]:
    """Lines of the string literals in Python source that hold SQL; docstrings are skipped."""
    tree = ast.parse(src)
    skipped = _docstrings(tree)
    parts: set[int] = set()  # pieces of a literal already read whole
    found: list[int] = []
    for node in ast.walk(tree):
        if id(node) in skipped or id(node) in parts:
            continue
        if not isinstance(node, ast.Constant | ast.JoinedStr | ast.BinOp):
            continue
        text = _text(node)
        if text is None:
            continue
        parts.update(id(inner) for inner in ast.walk(node) if inner is not node)
        if _SQL.search(text):
            found.append(node.lineno)
    return sorted(found)


def test_scanner_reads_sql_literals() -> None:
    """It finds SQL in plain, joined, concatenated and f-string literals, and skips prose."""
    src = (
        '"""SELECT x FROM y: a docstring says it."""\n'
        "# SELECT a FROM b in a comment\n"
        "a = 'SELECT 1 FROM events WHERE id = ?'\n"
        "b = ('SELECT a, '\n"
        "     'b FROM events')\n"
        "c = 'SELECT a ' + 'FROM events'\n"
        "d = f'SELECT {cols} FROM events'\n"
        "e = \"json_extract(payload_json, '$.turn_id')\"\n"
        "f = 'INSERT INTO events (a) VALUES (?)'\n"
        "g = 'UPDATE events SET a = 1'\n"
        "h = 'DELETE FROM events'\n"
        "i = 'Please select a device from the list' + ''\n"
        "j = 'update the cursor set by the caller'\n"
        "def k() -> None:\n"
        '    """DELETE FROM docstring."""\n'
        "    return None\n"
    )
    assert sql_lines(src) == [3, 4, 6, 7, 8, 9, 10, 11]


def _holders() -> dict[str, list[int]]:
    held: dict[str, list[int]] = {}
    for path in sorted((repo_root() / "jarvis" / "runtime").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        lines = sql_lines(path.read_text(encoding="utf-8"))
        if lines:
            held[relative_to_repo(path)] = lines
    return held


def test_runtime_files_holding_sql_only_go_down() -> None:
    """No more runtime files hold SQL than before; when one stops, the count is lowered."""
    held = _holders()
    listing = "\n".join(f"{rel}:{lines[0]}" for rel, lines in held.items())
    assert len(held) <= _SQL_FILES, (
        f"{len(held) - _SQL_FILES} more file(s) under jarvis/runtime hold SQL than before. The"
        " runtime asks L2 named questions (ADR 0349): put the query in jarvis/state as a"
        f" function that says what it answers and call it. Files holding SQL now:\n{listing}"
    )
    assert len(held) == _SQL_FILES, (
        f"{_SQL_FILES - len(held)} file(s) under jarvis/runtime no longer hold SQL: lower"
        f" _SQL_FILES to {len(held)} in {Path(__file__).name}."
    )
