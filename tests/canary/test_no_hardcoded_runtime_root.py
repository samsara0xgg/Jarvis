"""H8 — no literal ``"~/.jarvis"`` string outside ``jarvis/deployment/``.

Per ADR 0001 § Acceptance criterion H8:

> AST scan for literal ``"~/.jarvis"`` strings outside
> ``jarvis/deployment/``.

The deployment module is the single canonical home for the default
runtime root literal (per ADR § Configurable paths). Every other
module must obtain the path via :func:`jarvis.deployment.bootstrap_runtime`
or :class:`jarvis.deployment.RuntimePaths`.

Implementation: walk all ``.py`` under ``jarvis/`` with ``ast.parse``;
for each :class:`ast.Constant` whose value contains ``"~/.jarvis"``,
fail unless the source file is under ``jarvis/deployment/``.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import (
    iter_jarvis_py_files,
    parse,
    relative_to_repo,
    repo_root,
)

_TARGET_LITERAL = "~/.jarvis"


def test_no_hardcoded_runtime_root_outside_deployment() -> None:
    """Only ``jarvis/deployment/*.py`` may carry the ``~/.jarvis`` literal."""
    deployment_dir = (repo_root() / "jarvis" / "deployment").resolve()
    violations: list[str] = []

    for path in iter_jarvis_py_files():
        resolved = path.resolve()
        if resolved.is_relative_to(deployment_dir):
            continue
        rel = relative_to_repo(path)
        module = parse(path)
        for node in ast.walk(module):
            if not isinstance(node, ast.Constant):
                continue
            value = node.value
            if not isinstance(value, str):
                continue
            if _TARGET_LITERAL in value:
                violations.append(
                    f"{rel}:{node.lineno}: literal "
                    f"{_TARGET_LITERAL!r} found outside jarvis/deployment/ "
                    "(use bootstrap_runtime / RuntimePaths)"
                )

    assert not violations, (
        "H8 — hardcoded runtime root literal outside jarvis/deployment/:\n  "
        + "\n  ".join(violations)
    )
