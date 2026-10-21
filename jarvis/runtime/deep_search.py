"""The deep backend of `web_search`: one nested request to Anthropic's own web search.

`jarvis.execution.tools.DeepSearch` is the L4 seam; this composition-root module
is its implementation, built the way `screen_look`'s vision client is: a DEDICATED,
request-scoped `LLMClient` on one preset, with its own bounded timeout and retries
and its own cost row, so a deep search never shares a provider identity with the
decision loop's client.

Anthropic runs the searching (`web_search_20260209`, up to `max_uses` searches
inside one request), so the answer comes back as a transcript: the model's notes
between searches, the searches, then its brief. Only the brief is the result.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.execution.tools import DeepFindings, DeepSearch
from jarvis.shared.pricing import load_pricing_table
from jarvis.shared.realtime import new_response_id
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.decision.llm import ChatResult

LOGGER = logging.getLogger("jarvis.runtime")

DEFAULT_DEEP_PRESET: Final[str] = "sonnet"
DEFAULT_DEEP_MAX_USES: Final[int] = 15

_DEEP_CALL_TIMEOUT_S: Final[float] = 120.0
"""A deep search measured 27 to 39 s (live check, 2026-10-17); this is the ceiling
for a slow one, not the expectation."""

_DEEP_CALL_MAX_RETRIES: Final[int] = 1

_WEB_SEARCH_TOOL_TYPE: Final[str] = "web_search_20260209"

_SOURCES_MAX: Final[int] = 10

_ERROR_EXCERPT_MAX_CHARS: Final[int] = 300

_SECRET_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{32,}")

_NOT_ANSWER_BLOCKS: Final = frozenset({"text", "thinking", "redacted_thinking"})
"""Block types that do not end the model's search work: any other block (a search,
its results, a code execution and its result) is followed by more notes or the brief."""

_INCOMPLETE_STOPS: Final = frozenset({"pause_turn", "max_tokens"})

_RESEARCH_SYSTEM: Final[str] = (
    "Today is {today}. Research the question on the web. Search widely in parallel, open "
    "several independent sources, cross-check facts, prices and dates, and note where "
    "sources disagree. Then write the findings as a compact plain-text brief for an "
    "assistant who will answer him: the answer first, then the key facts with numbers and "
    "dates, then disagreements or caveats. State findings; do not address him or recommend "
    "in your own voice. No preamble, no markdown headings. Write the findings in the "
    "language of the question. End with a line reading only 'Sources:', then the URL of "
    "each page the brief rests on, one per line."
)

_SOURCES_LINE: Final = re.compile(
    r"^[ \t*#]*(?:sources|来源)[ \t*]*[:\uff1a][ \t*]*$", re.IGNORECASE | re.MULTILINE,
)
"""The line that closes the brief and opens its own list of the pages it rests on."""

_URL: Final = re.compile(r"https?://[^\s<>()\[\]\u3000-\u303f\uff00-\uffef]+")
"""A URL stops at CJK punctuation, which a Chinese brief puts right after it."""


class DeepSearchError(RuntimeError):
    """A deep search that gave no usable brief; the message is already redacted."""


def _redact_error(exc: Exception) -> str:
    """`"{type}: {bounded excerpt}"` with any long token-like run scrubbed.

    A provider error can echo request headers; a key is one unbroken run of this
    alphabet, so the pattern removes it without trusting the error's shape.
    """
    text = _SECRET_RUN_RE.sub("[redacted]", str(exc))
    if len(text) > _ERROR_EXCERPT_MAX_CHARS:
        text = text[:_ERROR_EXCERPT_MAX_CHARS] + "…[truncated]"
    return f"{type(exc).__name__}: {text}"


def _blocks(raw: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = raw.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, Mapping)]


def _final_text_blocks(blocks: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The text blocks after the last search/tool block.

    Text written before or between searches is the model's scratch notes; one once
    reached a spoken answer ("r is a string; fetch directly."). Only the brief,
    written after the last tool block, is the result.
    """
    last_tool = max(
        (i for i, block in enumerate(blocks) if block.get("type") not in _NOT_ANSWER_BLOCKS),
        default=-1,
    )
    return [b for b in blocks[last_tool + 1:] if b.get("type") == "text"]


def _sources(
    final_text: list[Mapping[str, Any]], listed: str,
) -> tuple[tuple[str, str], ...]:
    """Cited pages first, then the pages the brief lists; one row per url.

    Never the pages the searches merely returned: in the live check those were mostly
    pages the brief did not use (old release notes for "the latest Rust").
    """
    found: dict[str, str] = {}

    def add(url: object, title: object) -> None:
        if isinstance(url, str) and url and url not in found:
            found[url] = title if isinstance(title, str) else ""

    for block in final_text:
        citations = block.get("citations")
        for citation in citations if isinstance(citations, list) else ():
            if isinstance(citation, Mapping):
                add(citation.get("url"), citation.get("title"))
    for url in _URL.findall(listed):
        add(url.rstrip(".,;:"), "")
    return tuple((title or url, url) for url, title in found.items())[:_SOURCES_MAX]


def _split_sources(text: str) -> tuple[str, str]:
    """The brief and the text of its closing source list (empty when it wrote none)."""
    lines = list(_SOURCES_LINE.finditer(text))
    if not lines:
        return text, ""
    last = lines[-1]
    return text[: last.start()].rstrip(), text[last.end():]


def _search_count(raw: Mapping[str, Any], blocks: list[Mapping[str, Any]]) -> int:
    usage = raw.get("usage")
    server = usage.get("server_tool_use") if isinstance(usage, Mapping) else None
    counted = server.get("web_search_requests") if isinstance(server, Mapping) else None
    if isinstance(counted, int) and not isinstance(counted, bool):
        return counted
    return sum(
        1 for b in blocks if b.get("type") == "server_tool_use" and b.get("name") == "web_search"
    )


def findings_from(result: ChatResult) -> DeepFindings:
    """Read a finished deep request: the brief, its sources, the search count.

    Raises `DeepSearchError` when the model wrote no brief after its last search.
    """
    raw = result.raw
    blocks = _blocks(raw)
    final_text = _final_text_blocks(blocks)
    written = "".join(str(block.get("text") or "") for block in final_text).strip()
    summary, listed = _split_sources(written)
    if not summary:
        msg = "the deep search ended without a written brief"
        raise DeepSearchError(msg)
    stop = result.finish_reason or raw.get("stop_reason")
    return DeepFindings(
        summary=summary,
        sources=_sources(final_text, listed),
        searches=_search_count(raw, blocks),
        complete=stop not in _INCOMPLETE_STOPS,
    )


class _RequestScopedDeepSearch:
    """Mint isolated provider and accounting state in the calling worker."""

    def __init__(
        self,
        sessions: LLMSessionFactory,
        *,
        max_uses: int,
        event_log_path: Path | None,
        pricing_path: Path | None,
    ) -> None:
        self._factory = sessions
        self._snapshot = sessions.snapshot()
        self._max_uses = max_uses
        self._event_log_path = event_log_path
        self._pricing = {} if pricing_path is None else load_pricing_table(pricing_path)

    def research(self, query: str) -> DeepFindings:
        """One bounded request with its own SQLite connection."""
        client = self._factory.create(self._snapshot, response_id=new_response_id())
        request: dict[str, Any] = {
            "messages": [{"role": "user", "content": query}],
            "system": _RESEARCH_SYSTEM.format(today=datetime.now().astimezone().date().isoformat()),
            "tools": [
                {"type": _WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": self._max_uses},
            ],
        }
        try:
            with contextlib.ExitStack() as stack:
                if self._event_log_path is None:
                    result = client.chat(**request)
                else:
                    conn = stack.enter_context(
                        contextlib.closing(open_runtime_event_log(self._event_log_path)),
                    )
                    result = CostRecorder(conn, pricing_table=self._pricing).chat(
                        client, **request, kind="deep_search", turn_id=None,
                    )
        except Exception as exc:  # noqa: BLE001 — re-raised redacted; see `_redact_error`
            raise DeepSearchError(_redact_error(exc)) from None
        return findings_from(result)


def _anthropic_host(preset: Mapping[str, Any]) -> bool:
    """Only Anthropic's own host runs its web search; same test as `native_tool_search`."""
    if str(preset.get("provider", "")).strip().lower() != "anthropic":
        return False
    return "api.anthropic.com" in str(preset.get("base_url") or "api.anthropic.com")


def _web_block(config: Mapping[str, Any]) -> Mapping[str, Any]:
    tools = config.get("tools")
    web = tools.get("web") if isinstance(tools, Mapping) else None
    return web if isinstance(web, Mapping) else {}


def build_deep_search(
    config: Mapping[str, Any],
    *,
    event_log_path: Path | None = None,
    pricing_path: Path | None = None,
    account_cost: bool = False,
    sessions: LLMSessionFactory | None = None,
) -> DeepSearch | None:
    """Build the `web_search` deep seam from `tools.web.deep_preset` / `deep_max_uses`.

    `None` when that preset is absent, malformed, or not Anthropic on Anthropic's
    host: `web_search` then answers a deep request with a wider quick search. A
    missing deep search is one optional capability off, so it never fails boot.

    `sessions` mints each request's client: omitted, the real factory over the deep
    preset; a test hands the scripted model's.
    """
    web = _web_block(config)
    raw_preset = web.get("deep_preset")
    preset_name = raw_preset.strip() if isinstance(raw_preset, str) and raw_preset.strip() else (
        DEFAULT_DEEP_PRESET
    )
    raw_uses = web.get("deep_max_uses")
    max_uses = (
        raw_uses
        if isinstance(raw_uses, int) and not isinstance(raw_uses, bool) and raw_uses > 0
        else DEFAULT_DEEP_MAX_USES
    )
    llm_block = config.get("llm")
    presets = llm_block.get("presets") if isinstance(llm_block, Mapping) else None
    preset = presets.get(preset_name) if isinstance(presets, Mapping) else None
    if not isinstance(preset, Mapping) or not _anthropic_host(preset):
        return None
    llm_config: dict[str, Any] = {
        "provider": "anthropic",
        "presets": {preset_name: dict(preset)},
        "default_preset": preset_name,
        "timeout_s": _DEEP_CALL_TIMEOUT_S,
        "max_retries": _DEEP_CALL_MAX_RETRIES,
    }
    try:
        LLMClient(llm_config)
    except (ValueError, TypeError) as exc:
        LOGGER.warning(
            "web_search deep: llm.presets.%s is malformed (%s: %s); deep falls back to quick",
            preset_name, type(exc).__name__, exc,
        )
        return None
    if account_cost and event_log_path is None:
        message = "deep search accounting requires an Event Log path"
        raise ValueError(message)
    return _RequestScopedDeepSearch(
        LLMSessionFactory(llm_config) if sessions is None else sessions,
        max_uses=max_uses,
        event_log_path=event_log_path if account_cost else None,
        pricing_path=pricing_path,
    )
