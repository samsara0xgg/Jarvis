"""Canary — ``codex_action.py`` ships a ``codex --version`` preflight gate.

Per ADR-0002 Step 7 + Step 19 Tier-1 canary list (lines 1623-1625):

    ``test_canary_codex_version_preflight`` — module import raises if
    ``codex --version`` < 0.125.0.

Wording note: the canary's intent is to enforce the **architectural rule**
that the preflight mechanism exists and is wired to the documented floor;
importing :mod:`jarvis.execution.codex_action` is itself safe (the gate
fires at handler-registration / spawn time, not on import — per Step 7's
``ensure_codex_version_supported`` comment). Functional behavior of the
gate (mocked subprocess returning a low version raises, etc.) lives in
``tests/unit/test_codex_action.py``; this canary is the AST-level proof
that the gate **exists** and is **pinned at the spec'd floor**.

Four assertions, all stdlib-only AST scans of
``jarvis/execution/codex_action.py``:

* **A.** A callable named ``ensure_codex_version_supported`` is defined
  at module scope (``def`` or ``async def``).
* **B.** An exception class named ``CodexVersionTooLow`` *or*
  ``CodexVersionTooLowError`` is defined at module scope.
* **C.** The callable's body raises one of the two exception class names
  somewhere inside it (via ``raise CodexVersionTooLow(...)`` or
  ``raise CodexVersionTooLowError(...)``).
* **D.** The minimum-version literal ``(0, 125, 0)`` appears as a tuple
  literal in the module — confirming the floor is pinned at 0.125.0 per
  ADR description. (A bare ``"0.125.0"`` string literal also satisfies
  the canary, as the ADR description leaves room for either spelling.)

The canary is the only static proof Allen has that the preflight exists
+ is pinned; without it, a refactor could silently delete or downgrade
the gate and tests would only catch it when a real Codex spawn at the
old version returned a confusing error somewhere downstream.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, repo_root

_CODEX_ACTION_RELPATH: str = "jarvis/execution/codex_action.py"

_GATE_FUNCTION_NAME: str = "ensure_codex_version_supported"

# Either spelling is accepted per the ADR description ("``CodexVersionTooLow``
# (or ``CodexVersionTooLowError``)"). The Step-7 module ships both as
# aliases — the canary accepts either as the canonical name.
_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {"CodexVersionTooLow", "CodexVersionTooLowError"},
)

# Minimum version pinned by ADR-0002 Step 7 / Step 19 canary description.
# The module may spell this as a tuple ``(0, 125, 0)`` or as a string
# ``"0.125.0"`` — both satisfy the canary.
_MIN_VERSION_TUPLE: tuple[int, int, int] = (0, 125, 0)
_MIN_VERSION_STRING: str = "0.125.0"


def _find_function_def(
    module: ast.Module, *, name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the top-level ``def <name>`` (or ``async def``) node, or ``None``."""
    for node in module.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    return None


def _find_class_def(module: ast.Module, *, name: str) -> ast.ClassDef | None:
    """Return the top-level ``class <name>`` node, or ``None``."""
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _raise_targets_in(node: ast.AST) -> list[str]:
    """Return the bare names of every ``raise <Name>(...)`` under ``node``.

    Walks the subtree for ``ast.Raise`` nodes whose exc is a ``Call``
    with an ``ast.Name`` func (the common shape) OR a bare ``ast.Name``
    exc (``raise SomeError`` without args). Attribute-style raises
    (``raise mod.SomeError(...)``) return the ``.attr`` suffix so the
    canary still matches if a future refactor namespaces the exception.
    """
    names: list[str] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Raise) or sub.exc is None:
            continue
        exc = sub.exc
        # `raise Name(args)` shape — the exc is a Call.
        if isinstance(exc, ast.Call):
            target = exc.func
            if isinstance(target, ast.Name):
                names.append(target.id)
            elif isinstance(target, ast.Attribute):
                names.append(target.attr)
        # `raise Name` shape — bare exception reference.
        elif isinstance(exc, ast.Name):
            names.append(exc.id)
        elif isinstance(exc, ast.Attribute):
            names.append(exc.attr)
    return names


def _module_has_min_version_tuple(module: ast.Module) -> bool:
    """Return True iff ``(0, 125, 0)`` appears as an ast.Tuple of int constants.

    Matches any tuple literal whose three elements are ``ast.Constant``
    integers equal to ``0, 125, 0`` in order. We don't restrict to a
    specific assignment target because the gate may store the floor in
    ``_MIN_VERSION`` (the Step-7 spelling), ``MIN_VERSION``, or inline
    inside a comparison — all are equivalent for the canary's purpose.
    """
    target = _MIN_VERSION_TUPLE
    for node in ast.walk(module):
        if not isinstance(node, ast.Tuple) or len(node.elts) != len(target):
            continue
        values: list[int] = []
        for elt in node.elts:
            if not (isinstance(elt, ast.Constant) and isinstance(elt.value, int)):
                values = []
                break
            values.append(elt.value)
        if values and tuple(values) == target:
            return True
    return False


def _module_has_min_version_string(module: ast.Module) -> bool:
    """Return True iff the literal ``"0.125.0"`` appears as a string constant."""
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _MIN_VERSION_STRING in node.value
        for node in ast.walk(module)
    )


def test_canary_codex_version_gate_callable_defined() -> None:
    """A — ``ensure_codex_version_supported`` MUST be a top-level callable.

    The Step-7 module exposes this helper for ``spawn_worker_handler`` to
    call at handler-registration time per the docstring of the helper
    itself. A missing or renamed callable means the gate is bypassed —
    which is exactly the architectural regression this canary guards.
    """
    module = parse(repo_root() / _CODEX_ACTION_RELPATH)
    func = _find_function_def(module, name=_GATE_FUNCTION_NAME)
    assert func is not None, (
        f"{_CODEX_ACTION_RELPATH}: top-level callable "
        f"`{_GATE_FUNCTION_NAME}` not found. ADR-0002 Step 7 requires the "
        "module to expose this preflight helper; spawn_worker_handler "
        "calls it at handler registration before any real Codex spawn."
    )


def test_canary_codex_version_exception_class_defined() -> None:
    """B — exception class ``CodexVersionTooLow[Error]`` MUST be defined.

    Either spelling (with or without the ``Error`` suffix) is accepted —
    the Step-7 module ships both as aliases so callers can use either.
    """
    module = parse(repo_root() / _CODEX_ACTION_RELPATH)
    found = [name for name in _EXCEPTION_NAMES if _find_class_def(module, name=name) is not None]
    # Alias assignments (``CodexVersionTooLow = CodexVersionTooLowError``)
    # also count — walk the module body for assignments whose target name
    # matches and whose value is a Name referring to a defined class.
    aliased: list[str] = []
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        aliased.extend(
            tgt.id
            for tgt in node.targets
            if isinstance(tgt, ast.Name) and tgt.id in _EXCEPTION_NAMES
        )
    visible = set(found) | set(aliased)
    assert visible, (
        f"{_CODEX_ACTION_RELPATH}: no exception class named "
        f"{sorted(_EXCEPTION_NAMES)!r} found (neither as ClassDef nor as "
        "module-level alias). ADR-0002 Step 7 requires the preflight to "
        "raise a structured `CodexVersionTooLow`/`CodexVersionTooLowError` "
        "so spawn_worker_handler can surface the upgrade hint as a "
        "Limitation Claim rather than a bare RuntimeError."
    )


def test_canary_codex_version_gate_body_raises_exception() -> None:
    """C — gate body MUST raise one of the version-too-low exception classes.

    A gate that defines the exception but never raises it would be a
    silent regression — the canary asserts the raise is wired inside the
    callable's body, not just defined somewhere in the module.
    """
    module = parse(repo_root() / _CODEX_ACTION_RELPATH)
    func = _find_function_def(module, name=_GATE_FUNCTION_NAME)
    assert func is not None, (
        f"{_CODEX_ACTION_RELPATH}: top-level callable "
        f"`{_GATE_FUNCTION_NAME}` not found (see "
        "`test_canary_codex_version_gate_callable_defined`)."
    )
    raise_names = _raise_targets_in(func)
    matched = [name for name in raise_names if name in _EXCEPTION_NAMES]
    assert matched, (
        f"{_CODEX_ACTION_RELPATH}: `{_GATE_FUNCTION_NAME}` does not raise "
        f"any of {sorted(_EXCEPTION_NAMES)!r}. The gate body must raise "
        "the structured exception when the parsed version is below the "
        f"floor; current raise targets in body: {raise_names!r}."
    )


def test_canary_codex_version_floor_pinned_at_0_125_0() -> None:
    """D — the floor literal ``(0, 125, 0)`` (or ``"0.125.0"``) MUST appear.

    Either spelling satisfies the canary. The Step-7 module pins the
    floor as a tuple constant ``_MIN_VERSION = (0, 125, 0)``; future
    refactors that move the floor into a config string ``"0.125.0"`` are
    still acceptable. What the canary refuses to allow is a silent
    downgrade where neither literal appears (i.e. the gate parses some
    other version number not documented in the ADR).
    """
    module = parse(repo_root() / _CODEX_ACTION_RELPATH)
    has_tuple = _module_has_min_version_tuple(module)
    has_string = _module_has_min_version_string(module)
    assert has_tuple or has_string, (
        f"{_CODEX_ACTION_RELPATH}: minimum-version floor not pinned at "
        f"{_MIN_VERSION_TUPLE!r} (tuple) or {_MIN_VERSION_STRING!r} "
        "(string). ADR-0002 Step 7 + Step 19 canary description require "
        "the preflight to reject `codex --version` < 0.125.0; the floor "
        "must appear as a literal in source so a refactor cannot silently "
        "downgrade it."
    )
