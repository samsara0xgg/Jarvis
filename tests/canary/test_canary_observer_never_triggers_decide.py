"""Canary — repo observation never wakes ``decide()`` (ADR-0009 D5/§8).

D5's last bullet is a hard pin:

> **No decision triggers.** Neither type joins any trigger tuple;
> observations fold silently (§3.4.1; §3.2.5 安静优先).

That is what makes a 60-second poll affordable. The failure it guards
against is cheap to write by accident and expensive to notice: drop
``repo.state_observed`` into a watcher's type IN-list "so the daemon
notices commits" and every poll on a dirty working tree starts an LLM
turn — 1440 unbid turns a day, each one speaking, per repo.

Two static statements, both drift-proof by construction:

1. **No trigger tuple names either observation type.** Rather than
   hard-coding today's three tuples (``_RUNTIME_TRIGGER_TYPES``,
   ``_USER_INTENT_TRIGGER_TYPES``, ``_SYSTEM_TRIGGER_TYPES``), this
   scans every top-level ``*_TRIGGER_TYPES`` assignment under
   ``jarvis/``, so a *fourth* watcher added later is covered the day it
   lands. A guard asserts the scan found tuples at all, so a rename
   cannot make the canary pass vacuously.
2. **The observer emits only its two observation types.** Same
   invariant approached from the producer side: whatever the trigger
   tuples happen to contain, ``jarvis/surface/repo_observer.py`` must
   never call ``emit_event`` with a type that any watcher wakes on.

Implementation is the canary house style — stdlib ``ast`` only, no
``unittest.mock``, no monkeypatching of jarvis internals.
"""

from __future__ import annotations

import ast

import pytest

from tests.canary._helpers import iter_jarvis_py_files, parse, relative_to_repo, repo_root

# Observer modules and the observation types each may emit: the repo pair
# ADR-0009 §4 registers, and the usage observer ADR-0018 adds. Every type
# here is ``evidence_semantics=observation``; none is a trigger.
_OBSERVER_MODULES: dict[str, frozenset[str]] = {
    "jarvis/surface/repo_observer.py": frozenset({"repo.state_observed", "project.commit_seen"}),
    "jarvis/surface/usage_observer.py": frozenset({"usage.state_observed"}),
}

# ``tts.usage_observed`` is emitted by the media owner, not an observer
# module, but it is an observation too and must never wake a watcher.
_OBSERVER_EVENT_TYPES: frozenset[str] = frozenset({"tts.usage_observed"}).union(
    *_OBSERVER_MODULES.values()
)

_TRIGGER_TUPLE_SUFFIX = "_TRIGGER_TYPES"


def _assigned_names(node: ast.AST) -> list[str]:
    """Return the plain target names bound by one top-level assignment."""
    if isinstance(node, ast.AnnAssign):
        target = node.target
        return [target.id] if isinstance(target, ast.Name) else []
    if isinstance(node, ast.Assign):
        return [tgt.id for tgt in node.targets if isinstance(tgt, ast.Name)]
    return []


def _string_constants(value: ast.AST | None) -> list[str]:
    """Return the literal strings inside a tuple/list/set display."""
    if not isinstance(value, ast.Tuple | ast.List | ast.Set):
        return []
    return [
        elt.value
        for elt in value.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    ]


def _iter_trigger_tuples() -> list[tuple[str, str, list[str]]]:
    """Yield ``(rel_path, name, members)`` for every ``*_TRIGGER_TYPES``."""
    found: list[tuple[str, str, list[str]]] = []
    for path in iter_jarvis_py_files():
        rel = relative_to_repo(path)
        module = parse(path)
        for node in module.body:
            for name in _assigned_names(node):
                if not name.endswith(_TRIGGER_TUPLE_SUFFIX):
                    continue
                value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
                found.append((rel, name, _string_constants(value)))
    return found


def _emit_event_type_literals(module: ast.Module) -> list[tuple[int, str]]:
    """Return ``[(line, type_literal)]`` for every ``emit_event`` call."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if callee != "emit_event":
            continue
        for kw in node.keywords:
            if kw.arg == "type" and isinstance(kw.value, ast.Constant):
                value = kw.value.value
                if isinstance(value, str):
                    out.append((node.lineno, value))
    return out


def test_canary_observer_types_join_no_trigger_tuple() -> None:
    """No ``*_TRIGGER_TYPES`` tuple under ``jarvis/`` names an observer type."""
    tuples = _iter_trigger_tuples()
    assert tuples, (
        "ADR-0009 D5 canary found no *_TRIGGER_TYPES assignment under jarvis/ — "
        "the watcher trigger tuples were renamed or removed, so this canary "
        "would pass vacuously. Update the scan before shipping."
    )

    violations = [
        f"{rel}: {name} contains {sorted(set(members) & _OBSERVER_EVENT_TYPES)!r}"
        for rel, name, members in tuples
        if set(members) & _OBSERVER_EVENT_TYPES
    ]
    assert not violations, (
        "ADR-0009 D5: a repo-observation event type joined a decision trigger "
        "tuple:\n  " + "\n  ".join(violations) + "\n"
        "Observations fold silently (spec §3.4.1 / §3.2.5). A 60s poll that "
        "wakes decide() is ~1440 unbid LLM turns per repo per day."
    )


@pytest.mark.parametrize(("module_rel", "allowed"), sorted(_OBSERVER_MODULES.items()))
def test_canary_observer_emits_only_observation_types(
    module_rel: str, allowed: frozenset[str]
) -> None:
    """Each observer module emits only its registered types, none a watcher wakes on."""
    observer_path = repo_root() / module_rel
    assert observer_path.is_file(), (
        f"{module_rel} is missing — the observer module was moved or deleted; "
        "this canary has nothing left to pin."
    )

    emitted = _emit_event_type_literals(parse(observer_path))
    assert emitted, (
        f"{module_rel} makes no emit_event(type=<literal>) call. The "
        "observer either stopped emitting or moved to a computed type name, "
        "which this static canary cannot follow."
    )

    unexpected = [
        f"{module_rel}:{line}: emits {literal!r}"
        for line, literal in emitted
        if literal not in allowed
    ]
    assert not unexpected, (
        f"{module_rel} emits a type outside its registered observation set "
        f"{sorted(allowed)!r}:\n  " + "\n  ".join(unexpected)
    )

    trigger_types = {
        member for _rel, _name, members in _iter_trigger_tuples() for member in members
    }
    overlap = {literal for _line, literal in emitted} & trigger_types
    assert not overlap, (
        f"{module_rel} emits "
        f"{sorted(overlap)!r}, which some *_TRIGGER_TYPES tuple wakes on — "
        "every poll would start a turn."
    )
