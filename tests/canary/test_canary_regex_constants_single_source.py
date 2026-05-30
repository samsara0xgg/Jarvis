"""Canary: limitation/completion regex literals live in ONE place.

Per ADR-0002 § Canonical limitation phrasing (lines 344-372) +
build-order Step 13. The canonical limitation/completion regex set
lives in :mod:`jarvis.decision.pre_emit_phrases`. No other file may
inline a ``re.compile(...)`` / ``re.match(...)`` / ``re.search(...)``
call whose first string-literal argument contains one of the canonical
pattern source fragments.

Three divergent regex sets across docs is what created the F4/K5/L3
drift in the first place — this canary makes the rule machine-checked
rather than just-documented.

Scope: AST-scans every ``.py`` file under ``tests/`` (the most
likely drift surface — fixture authors copy-paste regex literals from
docs). Production code under ``jarvis/`` is governed by import-graph
review; only the single source file owns the constants. The single
source file itself (``jarvis/decision/pre_emit_phrases.py``) is the
allowed home.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis.decision.pre_emit_phrases import COMPLETION_REGEXES, LIMITATION_REGEXES

if TYPE_CHECKING:
    from collections.abc import Iterator

# Canonical pattern source-text fragments. Any test-side
# ``re.compile(...)`` (or ``re.search`` / ``re.match``) whose first
# string-literal arg contains one of these substrings is treated as a
# divergent re-declaration and fails the canary.
_CANONICAL_FRAGMENTS: tuple[str, ...] = tuple(
    pat.pattern for pat in (*LIMITATION_REGEXES, *COMPLETION_REGEXES)
)

# Repo root = three parents up from this file:
# tests/canary/test_canary_regex_constants_single_source.py
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_TESTS_ROOT: Path = _REPO_ROOT / "tests"
_SINGLE_SOURCE: Path = _REPO_ROOT / "jarvis" / "decision" / "pre_emit_phrases.py"

# Regex-API call names this canary inspects. Only ``re.<name>(...)``
# calls are flagged; bare ``compile`` etc. without the ``re.`` prefix
# are out of scope (they're not the typical drift surface).
_REGEX_API_NAMES: frozenset[str] = frozenset({"compile", "match", "search", "fullmatch"})


def _iter_test_py_files() -> Iterator[Path]:
    """Yield every ``.py`` file under ``tests/`` (recursively)."""
    yield from _TESTS_ROOT.rglob("*.py")


def _first_string_arg(call: ast.Call) -> str | None:
    """Return the literal string value of ``call``'s first arg, if any.

    Returns ``None`` when the first arg is not a bare string literal
    (e.g. it's a Name, Attribute, f-string, or the call has no args) —
    those cases can't be statically resolved to a regex fragment, so
    the canary skips them.
    """
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _is_re_dot_call(call: ast.Call) -> bool:
    """Return True iff ``call`` is of the form ``re.<name>(...)``."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _REGEX_API_NAMES
        and isinstance(func.value, ast.Name)
        and func.value.id == "re"
    )


def _violations_in_file(path: Path) -> list[tuple[int, str, str]]:
    """Return (lineno, api_name, regex_text) tuples for each canary hit.

    Empty list = clean. A non-empty return value means the file
    contains one or more ``re.<api>(<literal>, ...)`` calls whose
    literal contains a canonical fragment.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    hits: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_re_dot_call(node):
            continue
        regex_text = _first_string_arg(node)
        if regex_text is None:
            continue
        for fragment in _CANONICAL_FRAGMENTS:
            if fragment in regex_text:
                # ast.Attribute carries the ``.attr`` of the API name.
                assert isinstance(node.func, ast.Attribute)
                hits.append((node.lineno, node.func.attr, regex_text))
                break
    return hits


def test_canary_regex_constants_single_source() -> None:
    """No test file may inline a canonical limitation/completion regex.

    Imports of :data:`jarvis.decision.pre_emit_phrases.LIMITATION_REGEXES`
    / ``COMPLETION_REGEXES`` are the only sanctioned consumption path.
    Adding a new test that needs to assert limitation/completion phrasing?
    Import the constants. The canary itself is exempt — it consumes the
    canonical fragments via ``.pattern`` introspection on the imported
    Patterns, not via inline ``re.<api>`` literals.
    """
    self_path = Path(__file__).resolve()
    offenders: dict[str, list[tuple[int, str, str]]] = {}
    for path in _iter_test_py_files():
        resolved = path.resolve()
        if resolved == self_path:
            # The canary's own file is the exception — by construction
            # it doesn't inline literals (it reflects on the constants).
            continue
        hits = _violations_in_file(resolved)
        if hits:
            offenders[str(resolved.relative_to(_REPO_ROOT))] = hits

    assert not offenders, (
        "Inline limitation/completion regex literal(s) found outside "
        f"{_SINGLE_SOURCE.relative_to(_REPO_ROOT)}. Replace with an import "
        "from `jarvis.decision.pre_emit_phrases` (LIMITATION_REGEXES / "
        f"COMPLETION_REGEXES). Offenders: {offenders!r}"
    )


def test_canary_single_source_file_exists() -> None:
    """The single source of truth file MUST exist on disk.

    Guards against a refactor accidentally deleting the module the rest
    of the codebase imports from; a missing file would make every
    canonical-fragment consumer ImportError before this canary runs,
    but spelling it out keeps the failure mode explicit.
    """
    assert _SINGLE_SOURCE.is_file(), (
        f"single source of truth missing: {_SINGLE_SOURCE} — Step 13 of "
        "ADR-0002 requires this file"
    )
