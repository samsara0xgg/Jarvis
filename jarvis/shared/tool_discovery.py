"""The tool-discovery text both searches share (ADR 0034, ADR 0287).

Jarvis's own ``tool_search`` (L4) carries the list of what a search can find in its description.
With Anthropic's own tool search (L3, ADR 0287) the same list goes in the system prompt, where
the description's lines about how Jarvis's tool works would name a tool that is not on the menu.
"""

from __future__ import annotations

from typing import Final

OWN_SEARCH_LEAD: Final = (
    "Searches over deferred tool metadata with BM25 and exposes matching tools for the "
    "next model call.\n\n"
)
OWN_SEARCH_ASK: Final = "you should use this tool (`tool_search`) to search for the required tools"
_NATIVE_ASK: Final = "you should search for the required tools with the tool search tool"


def native_catalog(own_description: str) -> str:
    """``tool_search``'s description, said of Anthropic's tool search instead."""
    return own_description.replace(OWN_SEARCH_LEAD, "").replace(OWN_SEARCH_ASK, _NATIVE_ASK)
