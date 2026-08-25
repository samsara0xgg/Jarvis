"""Canary — the power-observer injection seam + the real observer's safety attrs.

Per ADR-0009 §7 Step 3: ``test_canary_power_observer_seam``.

Two things must survive every future refactor of
``jarvis/deployment/sleep_wake.py``:

1. **The stub-factory seam.** ``install_power_observer`` keeps its
   keyword-only ``observer_factory`` parameter and uses it INSTEAD of
   the real factory when one is injected. The K7 / K8 Tier-2 invariants
   and every unit test in ``tests/unit/test_sleep_wake.py`` ride on this
   seam; a refactor that calls ``_real_observer_factory()`` directly
   would drag IOKit into every one of them. The static counterpart of
   "an injected stub loads no IOKit" is that the frameworks are dlopened
   lazily: **no ``ctypes.CDLL(...)`` call may sit at module scope**, so
   merely importing the module (which the whole test suite does) never
   binds a framework.

2. **The real observer's two lifetime/teardown attributes.** D3 pins
   both, and both are the kind of thing a tidy-up refactor silently
   deletes:

   - the ``CFUNCTYPE`` wrapper handed to ``IORegisterForSystemPower`` is
     assigned to an instance attribute (ctypes does not own the
     trampoline — GC while registered = crash on the next notification);
   - a closed flag is set by ``shutdown()`` and consulted as the FIRST
     statement of the power-message callback, so a notification landing
     after teardown returns without touching the closed root port or a
     dead asyncio loop.

Pure AST, stdlib only — no import of the module under analysis, no mock.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from tests.canary._helpers import parse, repo_root

if TYPE_CHECKING:
    from collections.abc import Iterator

_SLEEP_WAKE_RELPATH: str = "jarvis/deployment/sleep_wake.py"
_INSTALL_FUNCTION_NAME: str = "install_power_observer"
_FACTORY_PARAM_NAME: str = "observer_factory"
_REAL_FACTORY_NAME: str = "_real_observer_factory"
_OBSERVER_CLASS_NAME: str = "_IOKitPowerObserver"
_CALLBACK_TYPE_NAME: str = "_CALLBACK_TYPE"
_CALLBACK_METHOD_NAME: str = "_on_power_message"
_SHUTDOWN_METHOD_NAME: str = "shutdown"
_CDLL_NAME: str = "CDLL"


# --- AST helpers -----------------------------------------------------------


def _call_name(call: ast.Call) -> str | None:
    """Return the dotted-name suffix of ``call.func`` (``a.b.c`` -> ``"c"``)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _find_function(scope: ast.Module | ast.ClassDef, *, name: str) -> ast.FunctionDef:
    """Return the ``def <name>`` directly inside ``scope``; raise if absent."""
    for node in scope.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    where = scope.name if isinstance(scope, ast.ClassDef) else _SLEEP_WAKE_RELPATH
    msg = f"{where} must define `def {name}(...)` — ADR-0009 D3."
    raise AssertionError(msg)


def _find_class(module: ast.Module, *, name: str) -> ast.ClassDef:
    """Return the top-level ``class <name>``; raise if absent."""
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    msg = (
        f"{_SLEEP_WAKE_RELPATH} must define `class {name}` — the real "
        "ctypes-IOKit observer pinned by ADR-0009 D3."
    )
    raise AssertionError(msg)


def _self_attr_assignments(func: ast.FunctionDef) -> Iterator[tuple[str, ast.expr]]:
    """Yield ``(attr_name, value)`` for every ``self.<attr> = <value>`` in ``func``."""
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                yield target.attr, node.value


def _first_statement(func: ast.FunctionDef) -> ast.stmt:
    """Return the first executable statement of ``func`` (docstring skipped)."""
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1]
    return body[0]


def _self_attr_reads(node: ast.AST) -> set[str]:
    """Return every ``self.<attr>`` name read anywhere under ``node``."""
    return {
        sub.attr
        for sub in ast.walk(node)
        if isinstance(sub, ast.Attribute)
        and isinstance(sub.value, ast.Name)
        and sub.value.id == "self"
    }


def _sleep_wake_module() -> ast.Module:
    """Parse ``jarvis/deployment/sleep_wake.py``."""
    return parse(repo_root() / _SLEEP_WAKE_RELPATH)


# --- 1. The stub-factory seam ---------------------------------------------


def test_canary_install_power_observer_keeps_the_factory_seam() -> None:
    """``install_power_observer`` keeps keyword-only ``observer_factory=None``."""
    install = _find_function(_sleep_wake_module(), name=_INSTALL_FUNCTION_NAME)
    kwonly = [arg.arg for arg in install.args.kwonlyargs]
    assert _FACTORY_PARAM_NAME in kwonly, (
        f"{_INSTALL_FUNCTION_NAME} must keep the keyword-only "
        f"`{_FACTORY_PARAM_NAME}` parameter (found kwonly args: {kwonly}). "
        "K7/K8 and every unit test inject a stub through it; removing it "
        "forces real IOKit into the whole suite."
    )
    index = kwonly.index(_FACTORY_PARAM_NAME)
    default = install.args.kw_defaults[index]
    default_is_none = isinstance(default, ast.Constant) and default.value is None
    assert default_is_none, (
        f"{_INSTALL_FUNCTION_NAME}: `{_FACTORY_PARAM_NAME}` must default to "
        "None so the real factory stays the fallback, not the requirement."
    )


def test_canary_install_power_observer_never_calls_the_real_factory() -> None:
    """The real factory is REFERENCED as a fallback value, never called directly.

    ``factory = observer_factory if observer_factory is not None else
    _real_observer_factory`` — a bare name, invoked only through
    ``factory()``. Any literal ``_real_observer_factory()`` inside
    ``install_power_observer`` means an injected stub no longer wins.
    """
    install = _find_function(_sleep_wake_module(), name=_INSTALL_FUNCTION_NAME)
    direct_calls = [
        node.lineno
        for node in ast.walk(install)
        if isinstance(node, ast.Call) and _call_name(node) == _REAL_FACTORY_NAME
    ]
    assert not direct_calls, (
        f"{_INSTALL_FUNCTION_NAME} calls {_REAL_FACTORY_NAME}() directly at "
        f"line(s) {direct_calls}. It must only be referenced as the default "
        f"value so `{_FACTORY_PARAM_NAME}=<stub>` takes precedence."
    )
    referenced = any(
        isinstance(node, ast.Name) and node.id == _REAL_FACTORY_NAME
        for node in ast.walk(install)
    )
    assert referenced, (
        f"{_INSTALL_FUNCTION_NAME} must still name {_REAL_FACTORY_NAME} as the "
        "no-injection fallback — the daemon has no stub to pass."
    )


def test_canary_no_framework_is_dlopened_at_module_scope() -> None:
    """Every ``ctypes.CDLL(...)`` sits inside a function, never at import time.

    Importing ``jarvis.deployment.sleep_wake`` happens in every unit
    test, on every platform. A module-scope ``CDLL`` would dlopen IOKit
    for all of them — including the stub-factory paths that must never
    bind a framework at all.
    """
    module = _sleep_wake_module()
    function_spans = [
        (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    module_scope_loads = [
        node.lineno
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and _call_name(node) == _CDLL_NAME
        and not any(start <= node.lineno <= end for start, end in function_spans)
    ]
    assert not module_scope_loads, (
        f"{_SLEEP_WAKE_RELPATH}: ctypes.CDLL(...) at module scope, line(s) "
        f"{module_scope_loads}. The IOKit / CoreFoundation load must stay "
        "lazy (inside the binding loader) so importing the module — which "
        "the entire test suite does — never binds a framework."
    )


# --- 2. The real observer's lifetime + teardown attributes ----------------


def test_canary_observer_holds_the_callback_wrapper_on_the_instance() -> None:
    """The ``CFUNCTYPE`` wrapper is assigned to a ``self.<attr>``, not a local.

    ctypes does not own the trampoline it builds for a Python callback.
    If the only reference is a local that dies when ``register()``
    returns, IOKit is left holding a dangling pointer and the next
    power notification crashes the runloop thread (ADR-0009 D3).
    """
    observer_class = _find_class(_sleep_wake_module(), name=_OBSERVER_CLASS_NAME)
    held_on_self = [
        attr
        for method in observer_class.body
        if isinstance(method, ast.FunctionDef)
        for attr, value in _self_attr_assignments(method)
        if isinstance(value, ast.Call) and _call_name(value) == _CALLBACK_TYPE_NAME
    ]
    assert held_on_self, (
        f"{_OBSERVER_CLASS_NAME}: no `self.<attr> = {_CALLBACK_TYPE_NAME}(...)` "
        "assignment found. The callback wrapper must be kept referenced on "
        "the instance for the observer's lifetime."
    )


def test_canary_shutdown_sets_a_closed_flag_the_callback_reads_first() -> None:
    """``shutdown()`` sets a flag that ``_on_power_message`` checks at entry.

    D3's teardown ordering is only safe because a late notification can
    see the flag: after ``IODeregisterForSystemPower`` the root port is
    closed and the asyncio loop may already be gone, so both the emit
    and the ack would be use-after-close.
    """
    observer_class = _find_class(_sleep_wake_module(), name=_OBSERVER_CLASS_NAME)
    shutdown = _find_function(observer_class, name=_SHUTDOWN_METHOD_NAME)
    flags = {
        attr
        for attr, value in _self_attr_assignments(shutdown)
        if isinstance(value, ast.Constant) and value.value is True
    }
    assert flags, (
        f"{_OBSERVER_CLASS_NAME}.{_SHUTDOWN_METHOD_NAME} must set a closed "
        "flag (`self.<attr> = True`) before unwinding the IOKit registration."
    )

    callback = _find_function(observer_class, name=_CALLBACK_METHOD_NAME)
    guard = _first_statement(callback)
    assert isinstance(guard, ast.If), (
        f"{_OBSERVER_CLASS_NAME}.{_CALLBACK_METHOD_NAME}: the first statement "
        "must be the closed-flag guard, so a notification arriving after "
        f"{_SHUTDOWN_METHOD_NAME}() returns before doing anything."
    )
    guarded_on = _self_attr_reads(guard.test) & flags
    assert guarded_on, (
        f"{_OBSERVER_CLASS_NAME}.{_CALLBACK_METHOD_NAME}: the entry guard must "
        f"test one of the closed flags {sorted(flags)} set by "
        f"{_SHUTDOWN_METHOD_NAME}()."
    )
    assert any(isinstance(stmt, ast.Return) for stmt in guard.body), (
        f"{_OBSERVER_CLASS_NAME}.{_CALLBACK_METHOD_NAME}: the closed-flag "
        "guard must RETURN — no callback, no IOAllowPowerChange."
    )
