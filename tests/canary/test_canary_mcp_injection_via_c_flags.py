"""Canary — MCP injection is via ``-c`` spawn flags, not runtime RPC.

Per ADR-0002 Step 7 Tier-1 canary list (lines 1666-1676):

    ``test_canary_mcp_injection_via_c_flags`` — AST scan asserts that
    ``jarvis/execution/codex_action.py`` injects ``submit_report`` via
    the ``-c mcp_servers.jarvis-tools.*`` spawn-flag mechanism (a
    ``"mcp_servers.jarvis-tools.command"`` string literal must appear
    in the ``-c`` flag list passed to ``CodexAppServerClient``) AND that
    no string ``"mcp/register_tool"`` (or equivalent runtime-RPC
    registration method) appears **anywhere under**
    ``jarvis/execution/`` or ``jarvis/decision/``. This canary is the
    architectural guard that the injection mechanism is config-flag,
    not runtime RPC (Codex's app-server protocol surface does not
    expose runtime tool registration).

Two parts:

* **Part A — anti-pattern absent.** Walk every ``.py`` under
  ``jarvis/execution/`` and ``jarvis/decision/``; reject any string
  literal containing ``mcp/register_tool``.
* **Part B — injection path present.** At least one file under
  ``jarvis/execution/`` must contain the literal
  ``"mcp_servers.jarvis-tools.command"`` — the ``-c`` flag key that
  points Codex at the Step-6 stdio MCP subprocess.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from tests.canary._helpers import parse, relative_to_repo, repo_root

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# The anti-pattern: a runtime JSON-RPC method that would register tools
# dynamically post-spawn. Codex 0.125+'s app-server protocol surface is
# closed (initialize / thread/start / turn/start / turn/interrupt only) —
# no such method exists. If a string literal containing this token
# appears anywhere in jarvis L3/L4, somebody is reaching for an API
# Codex does not expose.
_ANTI_PATTERN: str = "mcp/register_tool"

# The injection witness: the `-c` flag key that wires `submit_report`
# into Codex at spawn time. ADR-0002 § Codex contract.
_INJECTION_WITNESS: str = "mcp_servers.jarvis-tools.command"


def _iter_scoped_py_files() -> Iterator[Path]:
    """Yield every ``.py`` under ``jarvis/execution/`` or ``jarvis/decision/``."""
    root = repo_root() / "jarvis"
    for layer in ("execution", "decision"):
        layer_dir = root / layer
        if not layer_dir.is_dir():
            continue
        for path in sorted(layer_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def _iter_execution_py_files() -> Iterator[Path]:
    """Yield every ``.py`` under ``jarvis/execution/`` only."""
    layer_dir = repo_root() / "jarvis" / "execution"
    for path in sorted(layer_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _string_literals(path: Path) -> list[str]:
    """Return every ``str`` :class:`ast.Constant.value` in ``path``."""
    return [
        node.value
        for node in ast.walk(parse(path))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_canary_no_runtime_tool_registration() -> None:
    """Part A — ``"mcp/register_tool"`` appears nowhere in L3/L4.

    Codex's app-server has no such JSON-RPC method. If this canary
    trips, somebody is reaching for an API the protocol does not expose;
    fix by routing through the ``-c mcp_servers.<server>.*`` spawn-flag
    pattern instead (the Step-6 + Step-7 mechanism).
    """
    violations: list[str] = [
        f"{relative_to_repo(path)}: string literal contains {_ANTI_PATTERN!r}"
        for path in _iter_scoped_py_files()
        if any(_ANTI_PATTERN in value for value in _string_literals(path))
    ]

    assert not violations, (
        "MCP injection must be via -c spawn flags (ADR-0002 § Codex "
        "contract), NOT via a runtime tool-registration JSON-RPC method. "
        f"The token {_ANTI_PATTERN!r} appeared in:\n  "
        + "\n  ".join(violations)
    )


def test_canary_injection_witness_present_in_execution() -> None:
    """Part B — at least one file in ``jarvis/execution/`` carries the witness.

    The witness is the ``mcp_servers.jarvis-tools.command`` string literal
    that the Step-7 driver passes through ``CodexAppServerClient``'s
    ``extra_args``. Its presence is the static proof that the injection
    path is wired at spawn time.
    """
    witnesses: list[str] = [
        relative_to_repo(path)
        for path in _iter_execution_py_files()
        if any(_INJECTION_WITNESS in value for value in _string_literals(path))
    ]

    assert witnesses, (
        "jarvis/execution/ must contain at least one file whose string "
        f"literals include {_INJECTION_WITNESS!r} — that token is the "
        "static proof that submit_report is injected via the -c spawn-flag "
        "mechanism (ADR-0002 § Codex contract Step 7). No file matched."
    )
