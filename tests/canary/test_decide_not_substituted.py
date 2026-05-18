"""H9 — ``jarvis.decision.decide`` is the original, unwrapped function.

Per ADR 0001 § Acceptance criterion H9:

> Runtime check: after fixture setup, assert ``jarvis.decision.decide``
> is the original function (not patched / wrapped / replaced).

Two structural facts are checked:

1. ``decide.__module__ == "jarvis.decision"`` — the module attribute
   recorded at definition time is the canonical L3 package.
2. ``getattr(decide, "__wrapped__", None) is None`` — no
   :func:`functools.wraps` chain (which is what
   :func:`unittest.mock.patch` / decorator-based instrumentation
   typically introduces).
"""

from __future__ import annotations

import jarvis.decision as decision_module
from jarvis.decision import decide


def test_decide_module_attribute_is_jarvis_decision() -> None:
    """``decide.__module__`` points at the L3 package."""
    assert decide.__module__ == "jarvis.decision", (
        f"decide.__module__ = {decide.__module__!r}; expected 'jarvis.decision'"
    )


def test_decide_is_not_wrapped() -> None:
    """No ``__wrapped__`` chain — decide is the bare function."""
    wrapped = getattr(decide, "__wrapped__", None)
    assert wrapped is None, (
        f"decide.__wrapped__ is set ({wrapped!r}); the function has been wrapped "
        "(unittest.mock.patch or functools.wraps decorator)"
    )


def test_decide_is_the_function_on_the_module() -> None:
    """``jarvis.decision.decide`` is the same object the import returns."""
    assert decision_module.decide is decide, (
        "jarvis.decision.decide was rebound on the module — looks like a "
        "patch / substitution slipped through"
    )
