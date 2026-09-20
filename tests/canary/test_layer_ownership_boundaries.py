"""H13 — layer ownership boundaries (stronger than ``.importlinter``).

Per ADR 0001 § Acceptance criterion H13:

> AST scan: ``jarvis/runtime`` is the only composition root that imports
> multiple layer siblings; ``jarvis/deployment`` does not import
> ``jarvis.state``; ``jarvis/surface`` does not import
> ``jarvis.decision`` or ``jarvis.execution``; ``jarvis/execution``
> does not import ``jarvis.decision`` or ``jarvis.surface``. This
> catches ownership violations that a pure DAG import check may still
> allow.

Implementation: walk every ``.py`` file under each layer's package
directory; collect every ``Import`` / ``ImportFrom`` targeting another
``jarvis.*`` module; apply the four ownership rules. Day-1 reuse of
``jarvis.state`` from ``jarvis.deployment`` is explicitly forbidden by
``deployment``'s own module docstring; H13 enforces it.

Note: import-linter's DAG-style layer rule permits ``jarvis.deployment``
to import ``jarvis.shared`` / ``jarvis.constitution`` (they sit below
deployment). H13 is stricter: it also forbids the sibling cross-import
edges mentioned above.

Day-2 ADR-0002 § Sleep/wake protocol (Step 16) introduces a narrow,
file-scoped exception: ``jarvis/deployment/sleep_wake.py`` may import
``jarvis.state.event_log`` to emit ``mac.sleeping`` / ``mac.awake`` /
``action.timeout_assumed`` per spec §3.7.8. The rest of
``jarvis/deployment/`` still respects the Day-1 ban.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import iter_jarvis_py_files, parse, relative_to_repo, repo_root

# Layer name => forbidden top-level ``jarvis.<X>`` imports.
_FORBIDDEN_BY_LAYER: dict[str, frozenset[str]] = {
    "deployment": frozenset({"jarvis.state"}),
    "surface": frozenset({"jarvis.decision", "jarvis.execution"}),
    "execution": frozenset({"jarvis.decision", "jarvis.surface"}),
}

# Narrow per-file exceptions to the deployment-imports-jarvis.state ban.
# Day-2 ADR-0002 Step 16: sleep_wake.py must emit mac.* +
# action.timeout_assumed events directly so the spec §3.7.8 fail-closed
# reconciliation path stays self-contained inside L6. The rest of
# jarvis/deployment/ still respects the Day-1 stricter rule.
_PER_FILE_EXCEPTIONS: dict[tuple[str, str], frozenset[str]] = {
    ("deployment", "jarvis/deployment/sleep_wake.py"): frozenset({"jarvis.state"}),
}


def _layer_of(rel: str) -> str | None:
    """Return ``deployment`` / ``surface`` / ``execution`` / ``runtime`` / etc."""
    parts = rel.split("/")
    if len(parts) < 2 or parts[0] != "jarvis":
        return None
    return parts[1]


def _collect_imports(module: ast.Module) -> list[tuple[int, str]]:
    """Return ``[(line, dotted_module_name), ...]`` for every jarvis.* import."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            out.extend(
                (node.lineno, alias.name)
                for alias in node.names
                if alias.name.startswith("jarvis")
            )
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module
            if module_name is None:
                continue
            if module_name.startswith("jarvis"):
                out.append((node.lineno, module_name))
    return out


def _top_level_layer(module_name: str) -> str:
    """``jarvis.surface.cli`` -> ``jarvis.surface``; ``jarvis.shared`` -> ``jarvis.shared``."""
    parts = module_name.split(".")
    if len(parts) >= 2:
        return ".".join(parts[:2])
    return module_name


def test_h13_layer_ownership_boundaries() -> None:
    """Each layer obeys the H13 ownership rules."""
    violations: list[str] = []

    for path in iter_jarvis_py_files():
        rel = relative_to_repo(path)
        layer = _layer_of(rel)
        if layer is None or layer not in _FORBIDDEN_BY_LAYER:
            continue
        forbidden = _FORBIDDEN_BY_LAYER[layer]
        # Apply narrow per-file exception (e.g. sleep_wake.py imports
        # jarvis.state.event_log per ADR-0002 § Sleep/wake protocol).
        exception = _PER_FILE_EXCEPTIONS.get((layer, rel), frozenset())
        effective_forbidden = forbidden - exception
        module = parse(path)
        for lineno, imp in _collect_imports(module):
            target_top = _top_level_layer(imp)
            if target_top in effective_forbidden:
                violations.append(
                    f"{rel}:{lineno}: jarvis.{layer} imports {imp!r}; "
                    f"forbidden by H13 ({sorted(forbidden)!r})"
                )

    assert not violations, (
        "H13 — layer ownership violations:\n  " + "\n  ".join(violations)
    )


def test_h13_runtime_is_unique_cross_sibling_importer() -> None:
    """Only ``jarvis.runtime.*`` imports more than one middle-layer sibling.

    Middle layers: ``decision`` / ``execution`` / ``surface`` /
    ``deployment``. For every ``.py`` outside ``jarvis/runtime/``, count
    how many distinct middle-layer siblings it imports; fail if more
    than one. Imports of ``jarvis.shared`` / ``jarvis.constitution`` /
    ``jarvis.state`` / ``jarvis.cli`` do not count as middle-layer
    siblings.
    """
    middle_layers: frozenset[str] = frozenset(
        {"jarvis.decision", "jarvis.execution", "jarvis.surface", "jarvis.deployment"}
    )

    repo = repo_root()
    runtime_dir = (repo / "jarvis" / "runtime").resolve()
    # ``jarvis.cli`` legitimately re-exports from runtime; it does not
    # import any middle-layer sibling directly per the layer DAG, but
    # if it ever does the rule still applies. The H13 contract names
    # only runtime as the composition root; we do NOT whitelist cli.

    violations: list[str] = []
    for path in iter_jarvis_py_files():
        resolved = path.resolve()
        if resolved.is_relative_to(runtime_dir):
            continue
        rel = relative_to_repo(path)
        module = parse(path)
        seen_siblings: set[str] = set()
        for _lineno, imp in _collect_imports(module):
            target_top = _top_level_layer(imp)
            if target_top not in middle_layers:
                continue
            # Self-imports inside a layer's own package don't count
            # as cross-sibling — ``jarvis.decision.gates`` importing
            # ``jarvis.decision.policy`` is fine.
            layer = _layer_of(rel)
            own_top = f"jarvis.{layer}" if layer else None
            if target_top == own_top:
                continue
            seen_siblings.add(target_top)
        if len(seen_siblings) > 1:
            violations.append(
                f"{rel}: imports {sorted(seen_siblings)!r} (multiple middle-layer "
                "siblings) — only jarvis.runtime.* may compose siblings"
            )

    assert not violations, (
        "H13 — composition root exclusivity violations:\n  " + "\n  ".join(violations)
    )
