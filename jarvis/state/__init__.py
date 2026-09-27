"""L2 — State Object."""

from __future__ import annotations


class NewerDataError(RuntimeError):
    """A database was written by a newer Jarvis than this one (ADR 0068).

    Opening it anyway would stamp it back down to this build's version and
    let old code write rows in a format it does not know, so it is refused.
    """

    def __init__(self, name: str, found: int, known: int) -> None:
        """Name the file, the version it carries and the newest this build reads."""
        super().__init__(
            f"{name} was written by a newer Jarvis (data format {found}; this Jarvis "
            f"reads up to {known}). Update Jarvis to open it."
        )
