"""Runtime-bound guard for the instant a new action is accepted."""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_ADMISSION: ContextVar[Callable[[], AbstractContextManager[None]]] = ContextVar(
    "action_admission", default=nullcontext,
)


@contextmanager
def bind_action_admission(
    guard: Callable[[], AbstractContextManager[None]],
) -> Iterator[None]:
    """Bind a response's admission fence during its decision invocation."""
    token = _ADMISSION.set(guard)
    try:
        yield
    finally:
        _ADMISSION.reset(token)


def action_admission_guard() -> AbstractContextManager[None]:
    """Acquire the current response fence, or preserve legacy admission."""
    return _ADMISSION.get()()
