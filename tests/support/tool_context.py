"""A ToolContext for calling a tool's handler directly: the test's clock, and a Here if needed."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from jarvis.execution.tools import ToolContext
from tests.support.clock import FakeClock

if TYPE_CHECKING:
    import sqlite3

    from jarvis.execution.tools import RuntimePathsLike
    from jarvis.shared.clock import Clock
    from jarvis.shared.here import Here


def tool_context(
    conn: sqlite3.Connection | None = None, action_id: str = "A1", *,
    clock: Clock | None = None, here: Here | None = None, runtime_paths: Any = None,  # noqa: ANN401
) -> ToolContext:
    """What the dispatcher would hand a handler, with ``clock`` (default a fresh FakeClock)."""
    return ToolContext(
        cast("sqlite3.Connection", conn),
        cast("RuntimePathsLike", runtime_paths or Path("/nonexistent")),
        action_id, clock or FakeClock(), here=here,
    )
