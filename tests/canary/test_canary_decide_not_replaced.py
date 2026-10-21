"""No test replaces ``decide``; the model is the only thing a test stands a fake in for.

ADR 0326: a hermetic test that runs the decision layer puts ``tests/support/model.py``'s
``ScriptedModel`` where the model client stands, through ``DecideContext.llm_client`` or the
runtime's ``llm_client`` and ``llm_session_factory``, and the real ``decide()`` answers. A stub
in place of ``decide`` (a function that returns a ready plan) skips the prompt, the tool loop,
the gates and the cost rows, so a test that runs on it says nothing about them.

The check reads every ``.py`` under ``tests/`` and flags the ways a test puts something else in
the place of ``decide``:

- ``monkeypatch.setattr(module, "decide", stub)``, the dotted form
  ``monkeypatch.setattr("jarvis.runtime.decide", stub)`` and the builtin ``setattr``;
- ``monkeypatch.setitem(vars(module), "decide", stub)``;
- ``module.decide = stub`` and ``vars(module)["decide"] = stub``;
- ``mock.patch("jarvis.runtime.decide")``, ``patch.object(module, "decide")`` and
  ``patch.multiple(module, decide=stub)``.

A local named ``decide``, a parameter of that name, and a call to ``decide(...)`` are not
replacements and are not flagged.
"""

from __future__ import annotations

import ast

import pytest

from tests.canary._helpers import parse, relative_to_repo, repo_root

_NAME = "decide"


def _is_the_name(node: ast.expr | None) -> bool:
    """A string constant that is ``decide``, or a dotted path that ends on it."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.rsplit(".", 1)[-1] == _NAME
    )


def _callee(func: ast.expr) -> str:
    """The last name of what a call calls: ``setattr`` for ``monkeypatch.setattr``."""
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _replacing_call(node: ast.Call) -> bool:
    callee, args = _callee(node.func), node.args
    keywords = {keyword.arg: keyword.value for keyword in node.keywords}
    if callee in {"setattr", "setitem", "object"}:
        # the name is the second argument: setattr, setitem and patch.object all take it there
        named = args[1] if len(args) > 1 else keywords.get("name") or keywords.get("key")
        dotted = args[0] if args else keywords.get("target")  # setattr("pkg.module.decide", stub)
        return _is_the_name(named) or (callee == "setattr" and _is_the_name(dotted))
    if callee == "patch":
        return _is_the_name(args[0] if args else keywords.get("target"))
    if callee == "multiple":
        return _NAME in keywords
    return False


def _replacing_assignment(target: ast.expr) -> bool:
    if isinstance(target, ast.Attribute):
        return target.attr == _NAME
    if isinstance(target, ast.Subscript):
        return isinstance(target.slice, ast.Constant) and target.slice.value == _NAME
    return False


def _replacement_lines(node: ast.AST) -> list[int]:
    """``[line]`` when ``node`` puts something else in the place of ``decide``, else ``[]``."""
    if isinstance(node, ast.Call):
        replaced = _replacing_call(node)
    elif isinstance(node, ast.Assign):
        replaced = any(map(_replacing_assignment, node.targets))
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        replaced = _replacing_assignment(node.target)
    else:
        return []
    return [node.lineno] if replaced else []


def replacements(tree: ast.AST) -> list[int]:
    """The line of every place ``tree`` puts something else in the place of ``decide``."""
    return sorted(line for node in ast.walk(tree) for line in _replacement_lines(node))


_REPLACING = [
    'monkeypatch.setattr(runtime_module, "decide", stub)',
    'monkeypatch.setattr("jarvis.runtime.decide", stub)',
    'monkeypatch.setattr(target="jarvis.runtime.decide", value=stub)',
    'monkeypatch.setattr(obj=runtime_module, name="decide", value=stub, raising=False)',
    'setattr(runtime_module, "decide", stub)',
    'monkeypatch.setitem(vars(runtime_module), "decide", stub)',
    "runtime_module.decide = stub",
    "jarvis.runtime.decide = stub",
    'vars(runtime_module)["decide"] = stub',
    "runtime_module.decide: Callable[..., object] = stub",
    'patch("jarvis.runtime.decide")',
    'mock.patch("jarvis.runtime.decide", stub)',
    'patch.object(runtime_module, "decide", stub)',
    "patch.multiple(runtime_module, decide=stub)",
    "def test_x(monkeypatch):\n    monkeypatch.setattr(runtime_module, 'decide', stub)",
    "def script(monkeypatch, behavior):\n    monkeypatch.setattr(m, 'decide', behavior)",
]
_NOT_REPLACING = [
    "result = decide(trigger, ctx)",
    "decide = something_local",
    "def decide(*args): ...",
    "def test_x(decide): ...",
    'monkeypatch.setattr(runtime_module, "render_response", stub)',
    'monkeypatch.setattr(runtime_module, "decided", stub)',
    'monkeypatch.setattr("jarvis.runtime.decide_loop", stub)',
    'monkeypatch.setattr(runtime_module, "time", clock)',
    "runtime_module.render_response = stub",
    'patch("jarvis.runtime.render_response")',
    'patch.object(runtime_module, "render_response", stub)',
    "patch.multiple(runtime_module, render_response=stub)",
    'ScriptedModel(Reply("decide"))',
    "plan = decide.parse_items(text)",
]


@pytest.mark.parametrize("source", _REPLACING)
def test_every_way_of_replacing_decide_is_found(source: str) -> None:
    """A probe that replaces ``decide`` is reported, so the scan of ``tests/`` would fail on it."""
    assert replacements(ast.parse(source)), source


@pytest.mark.parametrize("source", _NOT_REPLACING)
def test_what_only_uses_the_name_is_not_found(source: str) -> None:
    """Calling ``decide``, a local of that name and other patches are left alone."""
    assert replacements(ast.parse(source)) == [], source


def test_no_test_replaces_decide() -> None:
    """Every ``.py`` under ``tests/``: no stub stands in the place of ``decide``."""
    found = [
        f"{relative_to_repo(path)}:{line}"
        for path in sorted((repo_root() / "tests").rglob("*.py"))
        if "__pycache__" not in path.parts
        for line in replacements(parse(path))
    ]
    assert not found, (
        "a test replaces decide() (ADR 0326). Put tests/support/model.py's ScriptedModel where the "
        "model client stands and let the real decide() answer:\n  " + "\n  ".join(found)
    )
