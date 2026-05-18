"""H4 — ``decision.llm.LLMClient`` is the original class, not a Mock.

Per ADR 0001 § Acceptance criterion H4:

> Runtime check: after fixture setup, assert ``decision.llm.LLMClient``
> is the original class, not a ``Mock`` / ``MagicMock`` / subclass
> override.

We check three structural facts without importing ``unittest.mock``:

1. :class:`LLMClient` is a ``type`` (a class object).
2. ``LLMClient.__module__ == "jarvis.decision.llm"``.
3. ``LLMClient.__class__.__name__`` is NOT ``Mock`` / ``MagicMock``
   (so a metaclass-Mocked replacement is caught).
"""

from __future__ import annotations

import jarvis.decision.llm as llm_module
from jarvis.decision.llm import LLMClient


def test_llm_client_is_real_class_not_mock() -> None:
    """The published ``LLMClient`` is the genuine class object."""
    # 1. It is a class.
    assert isinstance(LLMClient, type), (
        f"jarvis.decision.llm.LLMClient is not a class — got {type(LLMClient).__name__!r}"
    )

    # 2. Its declared module is the real L3 module.
    assert LLMClient.__module__ == "jarvis.decision.llm", (
        f"LLMClient.__module__ = {LLMClient.__module__!r}; expected "
        f"'jarvis.decision.llm'"
    )

    # 3. Its metaclass isn't a Mock/MagicMock variant.
    metaclass_name = type(LLMClient).__name__
    assert metaclass_name not in {"Mock", "MagicMock", "NonCallableMock"}, (
        f"LLMClient appears to be a mock — metaclass is {metaclass_name!r}"
    )


def test_llm_client_attribute_on_module_matches_published_class() -> None:
    """``jarvis.decision.llm.LLMClient`` attribute is the genuine class."""
    assert llm_module.LLMClient is LLMClient, (
        "jarvis.decision.llm.LLMClient was rebound on the module — looks like a "
        "patch / substitution slipped through"
    )
    assert llm_module.LLMClient.__module__ == "jarvis.decision.llm"
