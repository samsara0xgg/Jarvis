"""L4 — ``tool_search``: deferred tools reach the model only when searched (ADR 0034).

Codex's tool discovery on a Chat Completions wire: every deferred tool's
metadata (name, description, input property names) is one BM25 document;
a search returns the best names under
``loaded_tools``, plus every read-only tool of the best match's server
(ADR 0056), and the decision loop adds those tools to the model's tool list
for the rest of the turn. The search never runs a found tool.

A deferred tool that a tool on the menu has taken over (``Tool.supersedes``, ADR 0242) is found
but not loaded: the result says which tool to call instead. So is one the query names outright.
A built-in tool kept off the menu (ADR 0246) is deferred the same way and listed by name in the
tool's description.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolContext, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

TOOL_SEARCH_NAME: Final = "tool_search"
DEFAULT_LIMIT: Final[int] = 8
"""Codex ``TOOL_SEARCH_DEFAULT_LIMIT``."""

_K1: Final[float] = 1.2
_B: Final[float] = 0.75
_TOKEN: Final = re.compile(r"[a-z0-9]+|[一-鿿]")
_NAME_PART: Final = re.compile(r"[^a-z0-9]+")

_DESCRIPTION: Final = (
    "# Tool discovery\n\n"
    "Searches over deferred tool metadata with BM25 and exposes matching tools for the "
    "next model call.\n\n"
    "You have access to tools from the following sources:\n{sources}\n{loaded}"
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


_MCP_PREFIX: Final = "mcp__"


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


def _no_replacements() -> dict[str, tuple[str, str]]:
    return {}


def _mentions(query: str, swapped: Mapping[str, tuple[str, str]]) -> list[str]:
    """The superseded names a query spells out whole (``search_records``, ``search records``).

    A tool that is not in the search index (a built-in kept registered but deferred) is found
    only this way.
    """
    flat = f"_{_NAME_PART.sub('_', query.lower()).strip('_')}_"
    return [old for old in swapped if f"_{old.rsplit('__', 1)[-1].lower()}_" in flat]


def build_tool_search(
    tools: Sequence[Tool],
    sources: Mapping[str, str],
    replaced: Callable[[], Mapping[str, tuple[str, str]]] = _no_replacements,
) -> tuple[Tool, ...]:
    """The ``tool_search`` tool over the deferred ``tools``; none when nothing is deferred.

    Only deferred tools are indexed and returned. A non-deferred tool of a listed
    source is already on the model's menu, so the description names it; without
    that the model searches for tools it can already call (ADR 0169).

    ``sources`` maps a source (an MCP server) to the description the model
    reads in the tool's own description, as Codex lists its connectors. ``replaced`` is read at
    each search (``ToolRegistry.superseded``): ``{deferred name: (tool on the menu, how to call
    it)}``; a hit on one of those names is not loaded and the result says to call the other.
    """
    deferred = [t for t in tools if t.deferred]
    if not deferred:
        return ()
    index = _Bm25([_search_text(t) for t in deferred])
    names = [t.name for t in deferred]
    own = sorted(n for n in names if not n.startswith(_MCP_PREFIX))
    listed = sorted({_source(n) for n in names if n.startswith(_MCP_PREFIX)})
    # A menu tool that took over some of a source's tools is named on that source's line, or
    # the model searches the source for what it can already call (exam 2026-10-12: gmail).
    taken: dict[str, list[str]] = {}
    for old, (_, how) in replaced().items():
        if old in names:
            taken.setdefault(_source(old), []).append(how)
    lines = [
        f"- {s}: {sources.get(s, s)}"
        + (f" Already in your tools for it: {', '.join(taken[s])}." if s in taken else "")
        for s in listed
    ]
    if own:
        lines.append(f"- Jarvis's own tools, not listed upfront: {', '.join(own)}")
    source_lines = "\n".join(lines)
    loaded = sorted(t.name for t in tools if not t.deferred and _source(t.name) in listed)
    loaded_line = (
        f"Already available, call these directly and never search for them: {', '.join(loaded)}.\n"
        if loaded
        else ""
    )

    def handle(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            msg = (
                "tool_search: query is empty; pass English keywords for the tool you need, "
                'e.g. "plugin"'
            )
            raise ToolError(msg, code="invalid_arguments")
        try:
            limit = int(args.get("limit") or DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            msg = f"tool_search: limit must be a whole number from 1 (default {DEFAULT_LIMIT})"
            raise ToolError(msg, code="invalid_arguments")
        found = [names[i] for i in index.top(query, limit)]
        swapped = replaced()
        notes = [
            f"{name.rsplit('__', 1)[-1]} is replaced by {swapped[name][0]}, which is already in "
            f"your tools: call {swapped[name][1]}."
            for name in dict.fromkeys([*found, *_mentions(query, swapped)])
            if name in swapped
        ]
        hits = [name for name in found if name not in swapped]
        # ADR 0056: the best match's server brings all of its read-only tools. A search
        # there is often useless without the read that follows (gmail_search lists ids,
        # gmail_get reads one), and read-only tools run unasked, so loading them risks nothing.
        best = _source(hits[0]) if hits else None
        reads = [
            name
            for name, tool in zip(names, deferred, strict=True)
            if tool.read_only and _source(name) == best and name not in hits
            and name not in swapped
        ]
        return {"loaded_tools": hits + reads, **({"note": " ".join(notes)} if notes else {})}

    return (
        Tool(
            name=TOOL_SEARCH_NAME,
            description=_DESCRIPTION.format(sources=source_lines, loaded=loaded_line),
            input_schema=_SCHEMA,
            handler=handle,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
        ),
    )
