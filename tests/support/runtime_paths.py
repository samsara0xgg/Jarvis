"""The two paths of a runtime that a decision turn reads, for tests that build a DecideContext."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class StubRuntimePaths:
    """``event_log`` and ``artifacts_root``, which is all ``RuntimePathsLike`` a turn here needs."""

    event_log: Path
    artifacts_root: Path
