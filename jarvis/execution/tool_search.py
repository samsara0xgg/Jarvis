"""L4 — ``tool_search``: deferred tools reach the model only when searched (ADR 0034).

Codex's tool discovery on a Chat Completions wire: every deferred tool's
metadata (name, description, input property names) is one BM25 document;
a search returns the best names under
``loaded_tools``, and the decision loop adds those tools to the model's
tool list for the rest of the turn. The search never runs a found tool.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolContext, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

TOOL_SEARCH_NAME: Final = "tool_search"
DEFAULT_LIMIT: Final[int] = 8
"""Codex ``TOOL_SEARCH_DEFAULT_LIMIT``."""

_K1: Final[float] = 1.2
_B: Final[float] = 0.75
_TOKEN: Final = re.compile(r"[a-z0-9]+|[一-鿿]")

_DESCRIPTION: Final = (
    "# Tool discovery\n\n"
    "Searches over deferred tool metadata with BM25 and exposes matching tools for the "
    "next model call.\n\n"
    "You have access to tools from the following sources:\n{sources}\n"
    "Some of the tools may not have been provided to you upfront, and you should use this "
    "tool (`tool_search`) to search for the required tools. Tool metadata is English, so "
    "search with English keywords."
)

_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query for deferred tools."},
        "limit": {
            "type": "number",
            "description": f"Maximum number of tools to return. Defaults to {DEFAULT_LIMIT}.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


# The bm25 crate Codex uses drops English stop words; this is the part of its list queries hit.
_STOP_WORDS: Final = frozenset(
    {
        "a", "am", "an", "and", "any", "are", "as", "at", "be", "by", "can", "do", "for",
        "from", "get", "i", "in", "is", "it", "me", "my", "of", "on", "or", "our", "some",
        "that", "the", "this", "to", "up", "us", "we", "what", "which", "who", "whom",
        "with", "you", "your",
    }
)  # fmt: skip


def _tokens(text: str) -> list[str]:
    # ponytail: plural-s strip stands in for Codex's English stemmer; swap in one if misses show.
    return [
        t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t  # noqa: PLR2004
        for t in _TOKEN.findall(text.lower())
        if t not in _STOP_WORDS
    ]


def _source(tool_name: str) -> str:
    """The server of ``mcp__<server>__<tool>``, else the name itself."""
    parts = tool_name.split("__")
    return parts[1] if len(parts) > 2 and parts[0] == "mcp" else tool_name  # noqa: PLR2004


def _search_text(tool: Tool) -> str:
    properties = tool.input_schema.get("properties") or {}
    raw_name = tool.name.split("__")[-1].replace("_", " ").replace("-", " ")
    # Codex joins the flat name, the callable name and the raw tool name, so a
    # tool's own name words weigh three times against its description. A
    # server-wide description is left out: shared by every tool of the server,
    # it would zero the weight of the words it contains.
    return " ".join(
        (
            tool.name.replace("_", " ").replace("-", " "),
            raw_name,
            raw_name,
            tool.description,
            " ".join(sorted(properties)),
        )
    )


class _Bm25:
    """Okapi BM25 over one fixed document set."""

    def __init__(self, documents: Sequence[str]) -> None:
        self._docs = [Counter(_tokens(doc)) for doc in documents]
        self._lengths = [sum(doc.values()) for doc in self._docs]
        self._avg = (sum(self._lengths) / len(self._lengths)) if self._docs else 0.0
        frequency: Counter[str] = Counter()
        for doc in self._docs:
            frequency.update(doc.keys())
        n = len(self._docs)
        self._idf = {t: math.log((n - f + 0.5) / (f + 0.5) + 1) for t, f in frequency.items()}

    def top(self, query: str, limit: int) -> list[int]:
        terms = set(_tokens(query))
        scored = []
        for index, (doc, length) in enumerate(zip(self._docs, self._lengths, strict=True)):
            score = 0.0
            for term in terms & doc.keys():
                tf = doc[term]
                norm = tf + _K1 * (1 - _B + _B * length / self._avg)
                score += self._idf[term] * tf * (_K1 + 1) / norm
            if score > 0:
                scored.append((score, index))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [index for _, index in scored[:limit]]


def build_tool_search(deferred: Sequence[Tool], sources: Mapping[str, str]) -> tuple[Tool, ...]:
    """The ``tool_search`` tool over ``deferred``; none when nothing is deferred.

    ``sources`` maps a source (an MCP server) to the description the model
    reads in the tool's own description, as Codex lists its connectors.
    """
    if not deferred:
        return ()
    index = _Bm25([_search_text(t) for t in deferred])
    names = [t.name for t in deferred]
    listed = sorted({_source(n) for n in names})
    source_lines = "\n".join(f"- {s}: {sources.get(s, s)}" for s in listed)

    def handle(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            msg = "query must not be empty"
            raise ToolError(msg, code="invalid_arguments")
        limit = int(args.get("limit") or DEFAULT_LIMIT)
        if limit <= 0:
            msg = "limit must be greater than zero"
            raise ToolError(msg, code="invalid_arguments")
        return {"loaded_tools": [names[i] for i in index.top(query, limit)]}

    return (
        Tool(
            name=TOOL_SEARCH_NAME,
            description=_DESCRIPTION.format(sources=source_lines),
            input_schema=_SCHEMA,
            handler=handle,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
        ),
    )
