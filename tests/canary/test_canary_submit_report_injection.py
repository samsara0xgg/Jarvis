"""Canary — ``codex_action.py`` injects ``submit_report`` via ``-c`` flags.

Per ADR-0002 Step 7 Tier-1 canary list (lines 1658-1665):

    ``test_canary_submit_report_injection`` — AST scan:
    ``jarvis/execution/codex_action.py`` builds an ``extra_args`` list
    containing string literals ``"mcp_servers.jarvis-tools.command"``,
    ``"mcp_servers.jarvis-tools.args"``,
    ``"mcp_servers.jarvis-tools.startup_timeout_sec"``,
    ``"mcp_servers.jarvis-tools.tool_timeout_sec"`` (the ``-c`` flag
    injection vector); the ``take_notification`` loop captures
    ``item/tool_call`` events with ``tool_name == "submit_report"``. The
    canary lives at AST level — it must match before any live spawn.

Two parts:

* **Part A — the four ``mcp_servers.jarvis-tools.*`` keys.** Walk every
  :class:`ast.Constant` (covers bare strings AND the constant slices of
  f-strings) in ``codex_action.py`` and assert each key appears as a
  substring of at least one literal.
* **Part B — submit_report capture.** Assert both ``"submit_report"`` and
  ``"item/tool_call"`` appear in string literals so the post-turn guard
  is statically wired (the take_notification branch references them).
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, repo_root

# The four `mcp_servers.jarvis-tools.*` keys spec'd by ADR-0002 § Codex
# contract. Each must appear inside at least one string literal in
# `codex_action.py`. We tolerate them appearing inside f-string prefixes
# (which the AST encodes as ast.Constant nodes within JoinedStr) by
# scanning every Constant — bare or formatted.
_REQUIRED_MCP_KEYS: tuple[str, ...] = (
    "mcp_servers.jarvis-tools.command",
    "mcp_servers.jarvis-tools.args",
    "mcp_servers.jarvis-tools.startup_timeout_sec",
    "mcp_servers.jarvis-tools.tool_timeout_sec",
)

# Submit-report capture markers. ``submit_report`` is the tool name the
# driver matches on; ``item/tool_call`` is the JSON-RPC method whose
# params carry the captured payload.
_REQUIRED_CAPTURE_TOKENS: tuple[str, ...] = (
    "submit_report",
    "item/tool_call",
)


def _collect_string_literals(module: ast.Module) -> list[str]:
    """Return every ``str`` :class:`ast.Constant.value` in ``module``.

    Covers bare string literals AND f-string constant fragments (which
    appear in the AST as ``ast.Constant`` children of ``ast.JoinedStr``).
    Non-string constants (numbers / None / bytes) are skipped.
    """
    return [
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_canary_eight_c_flags_present() -> None:
    """Part A — the four ``mcp_servers.jarvis-tools.*`` keys appear literally.

    Each key must appear as a substring of at least one string literal in
    ``codex_action.py``. The Step-7 canary is the only static proof that
    the ``-c`` flag injection mechanism is wired statically (not built at
    runtime from some config file the user might mutate).
    """
    path = repo_root() / "jarvis" / "execution" / "codex_action.py"
    literals = _collect_string_literals(parse(path))
    haystack = "\n".join(literals)

    missing = [key for key in _REQUIRED_MCP_KEYS if key not in haystack]

    assert not missing, (
        "codex_action.py must inject the submit_report MCP server via the "
        "four `-c mcp_servers.jarvis-tools.*` flags (ADR-0002 § Codex "
        "contract); the following keys are missing from string literals "
        "in this file:\n  " + "\n  ".join(missing)
    )


def test_canary_submit_report_capture_wired() -> None:
    """Part B — ``submit_report`` AND ``item/tool_call`` both appear literally.

    The post-turn guard in ``run_codex_action`` matches on these two
    strings: ``method == "item/tool_call"`` AND ``tool_name ==
    "submit_report"``. Both must appear in source as literals so the
    capture path is statically discoverable.
    """
    path = repo_root() / "jarvis" / "execution" / "codex_action.py"
    literals = _collect_string_literals(parse(path))
    haystack = "\n".join(literals)

    missing = [token for token in _REQUIRED_CAPTURE_TOKENS if token not in haystack]

    assert not missing, (
        "codex_action.py must reference both 'submit_report' and "
        "'item/tool_call' as string literals so the post-turn capture "
        "guard (ADR-0002 § submit_report tool injection) is statically "
        "wired; missing literals:\n  " + "\n  ".join(missing)
    )
