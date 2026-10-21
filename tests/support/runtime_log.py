"""A runtime root with a real event log and a memory.db, for tests that read both."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.deployment import bootstrap_runtime
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from pathlib import Path


class RuntimeLog:
    """``paths`` of the bootstrapped root, ``conn`` on its event log, ``memory`` the memory.db."""

    def __init__(self, root: Path) -> None:
        """Bootstrap ``root`` and open its event log; create an empty memory.db beside it."""
        self.paths = bootstrap_runtime(root)
        self.conn = open_event_log(self.paths.event_log)
        self.memory = root / "memory.db"
        open_memory_db(self.memory).close()
