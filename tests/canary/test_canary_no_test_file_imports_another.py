"""No test file imports from another test file.

A helper that two tests share lives in ``tests/support/``, under a public name,
and both import it from there. A test file that is also a library makes every
rename in it a hunt through its importers, and it runs its own tests again in
each importer's collection when it re-exports a fixture.

The check reads every ``.py`` under ``tests/`` and flags an import whose module
is a ``test_*`` file: ``from tests.integration.test_x import y``,
``from tests.integration import test_x``, ``import tests.integration.test_x``
and the relative forms. ``tests/canary/_helpers.py`` and the shared fakes in
``tests/integration/`` that are not test files (``weatherkit_world.py``, the
``mcp_*_server.py`` stand-ins) are not test files and stay importable.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, relative_to_repo, repo_root


def _is_test_module(name: str) -> bool:
    return name.rsplit(".", 1)[-1].startswith("test_")


def _imported_test_modules(tree: ast.Module, importer_module: str) -> set[str]:
    """The ``test_*`` modules ``tree`` imports, as dotted names."""
    package = importer_module.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if _is_test_module(a.name))
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - node.level + 1]
                base = ".".join([*parts, *([base] if base else [])])
            if _is_test_module(base):
                found.add(base)
            found.update(f"{base}.{a.name}" for a in node.names if _is_test_module(a.name))
    return found


def _violations() -> set[tuple[str, str]]:
    """(importing file, imported test file), each by file name without ``.py``."""
    found: set[tuple[str, str]] = set()
    for path in sorted((repo_root() / "tests").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = relative_to_repo(path).removesuffix(".py").replace("/", ".")
        found.update(
            (path.stem, target.rsplit(".", 1)[-1])
            for target in _imported_test_modules(parse(path), module)
        )
    return found


def test_no_test_file_imports_another_test_file() -> None:
    """Shared helpers come from ``tests/support/``, never from a ``test_*`` file."""
    found = sorted(_violations())
    assert not found, (
        "move the helper to tests/support/ and import it from there:\n"
        + "\n".join(f"  {importer} imports {module}" for importer, module in found)
    )
