"""A system prompt in two parts: the stable prefix and what varies by route or turn."""

from __future__ import annotations

from typing import Self


class SplitSystem(str):  # noqa: SLOT000 — a str subclass cannot have non-empty slots
    """The whole prompt as a string, with the part after ``stable`` that varies per turn.

    Every client that wants a plain string reads it as one; Anthropic's request puts a cache
    breakpoint after ``stable`` so core memory is a cached prefix of its own.
    """

    stable: str
    variable: str

    def __new__(cls, stable: str, variable: str = "") -> Self:  # noqa: D102
        self = super().__new__(cls, f"{stable}\n\n{variable}" if variable else stable)
        self.stable = stable
        self.variable = variable
        return self
