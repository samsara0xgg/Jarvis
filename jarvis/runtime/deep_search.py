"""The deep backend of `web_search`: nested requests to Anthropic's own web search (ADR 0289, 0359).

`jarvis.execution.web_search_tool.DeepSearch` is the L4 seam; this composition-root module
is its implementation, built the way `screen_look`'s vision client is: a DEDICATED,
request-scoped `LLMClient` on one preset, with no retry and its own cost rows, so a deep
search never shares a provider identity with the decision loop's client.

Anthropic runs the searching (`web_search_20260209`, up to `max_uses` searches inside one
request), so the answer comes back as a transcript: the model's notes between searches, the
searches with their results, then its brief. The server pauses a long turn (`pause_turn`); the
request is sent again with its own blocks to go on. The whole search has one budget of a minute,
streamed, so when it runs out the blocks received so far are read: without a brief the result is
the pages the searches found.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import urlsplit

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_message import MessageReader
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.execution.web_search_tool import DeepFindings, DeepSearch, Source
from jarvis.runtime.search_location import HomePlace, user_location
from jarvis.shared import lang
from jarvis.shared.pricing import load_pricing_table
from jarvis.shared.realtime import new_response_id
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from jarvis.decision.llm import LLMClient as _Client
    from jarvis.shared.clock import Clock
    from jarvis.shared.here import Coarse
    from jarvis.shared.realtime import LLMRequestOutcome

LOGGER = logging.getLogger("jarvis.runtime")

DEFAULT_DEEP_PRESET: Final[str] = "sonnet"
DEFAULT_DEEP_MAX_USES: Final[int] = 15

_BUDGET_S: Final[float] = 60.0
"""The whole deep search, every request of it, from its start (ADR 0359). A deep search measured
27 to 39 s (live check, 2026-10-17); past a minute he has waited twice what she promised."""

_DEEP_CALL_MAX_RETRIES: Final[int] = 0
"""No retry: a retry could double the wait to end in the same error."""

_WEB_SEARCH_TOOL_TYPE: Final[str] = "web_search_20260209"

_SOURCES_MAX: Final[int] = 10

_ERROR_EXCERPT_MAX_CHARS: Final[int] = 300

_SECRET_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{32,}")

_AGE_FORMATS: Final = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y")
"""How a search result writes the age of its page when it names a day."""
_AGE_CHARS: Final[int] = 30

_NOT_ANSWER_BLOCKS: Final = frozenset({"text", "thinking", "redacted_thinking"})
"""Block types that do not end the model's search work: any other block (a search,
its results, a code execution and its result) is followed by more notes or the brief."""

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
    """A deep search that gave no usable brief or page; the message is already redacted."""


def _redact_error(exc: BaseException) -> str:
    """`"{type}: {bounded excerpt}"` with any long token-like run scrubbed.

    A provider error can echo request headers; a key is one unbroken run of this
    alphabet, so the pattern removes it without trusting the error's shape.
    """
    text = _SECRET_RUN_RE.sub("[redacted]", str(exc))
    if len(text) > _ERROR_EXCERPT_MAX_CHARS:
        text = text[:_ERROR_EXCERPT_MAX_CHARS] + "…[truncated]"
    return f"{type(exc).__name__}: {text}"


def _final_text_blocks(blocks: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The text blocks after the last search/tool block.

    Text written before or between searches is the model's scratch notes; one once
    reached a spoken answer ("r is a string; fetch directly."). Only the brief, written
    after the last tool block, is the result.
    """
    last_tool = max(
        (i for i, block in enumerate(blocks) if block.get("type") not in _NOT_ANSWER_BLOCKS),
        default=-1,
    )
    return [b for b in blocks[last_tool + 1:] if b.get("type") == "text"]


def _split_sources(text: str) -> tuple[str, str]:
    """The brief and the text of its closing source list (empty when it wrote none)."""
    lines = list(_SOURCES_LINE.finditer(text))
    if not lines:
        return text, ""
    last = lines[-1]
    return text[: last.start()].rstrip(), text[last.end():]


def _site(url: str) -> str:
    """The page's host without ``www.``; empty when ``url`` has none."""
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _address(url: str) -> str:
    """``url`` as one page's identity: no scheme, ``www.``, trailing slash or fragment."""
    parts = urlsplit(url)
    query = f"?{parts.query}" if parts.query else ""
    return f"{_site(url)}{parts.path.rstrip('/')}{query}"


def _page_date(age: object) -> str | None:
    """A search result's page age as a day (``YYYY-MM-DD``), as it wrote it when it names none."""
    if not isinstance(age, str) or not age.strip():
        return None
    text = age.strip()
    for form in _AGE_FORMATS:
        with contextlib.suppress(ValueError):
            return datetime.strptime(text, form).date().isoformat()  # noqa: DTZ007 — a day, no zone
    return text[:_AGE_CHARS]


def _pages(blocks: Sequence[Mapping[str, Any]]) -> tuple[Source, ...]:
    """Every page the searches returned, in the order found, once each, with the age it gave."""
    found: dict[str, Source] = {}
    for block in blocks:
        results = block.get("content") if block.get("type") == "web_search_tool_result" else None
        for one in results if isinstance(results, list) else ():
            if not isinstance(one, Mapping) or one.get("type") != "web_search_result":
                continue
            url = one.get("url")
            if not isinstance(url, str) or not (site := _site(url)) or url in found:
                continue
            title = one.get("title")
            found[url] = Source(
                title.strip() if isinstance(title, str) else "", site,
                _page_date(one.get("page_age")), url,
            )
    return tuple(found.values())


def _sources(
    final_text: Sequence[Mapping[str, Any]], listed: str, pages: Sequence[Source],
) -> tuple[Source, ...]:
    """Cited pages first, then the pages the brief lists; one row per page, dated by its search.

    Never the pages the searches merely returned: in the live check those were mostly
    pages the brief did not use (old release notes for "the latest Rust"). A page's date is its
    search result's age; a page the searches did not return has none.
    """
    seen = {_address(page.url): page for page in pages}
    found: dict[str, Source] = {}

    def add(url: object, title: object) -> None:
        if not isinstance(url, str) or not (site := _site(url)) or _address(url) in found:
            return
        page = seen.get(_address(url))
        named = title.strip() if isinstance(title, str) else ""
        found[_address(url)] = Source(
            named or (page.title if page else ""), site, page.date if page else None, url,
        )

    for block in final_text:
        citations = block.get("citations")
        for citation in citations if isinstance(citations, list) else ():
            if isinstance(citation, Mapping):
                add(citation.get("url"), citation.get("title"))
    for url in _URL.findall(listed):
        add(url.rstrip(".,;:"), "")
    return tuple(found.values())[:_SOURCES_MAX]


def _search_count(readers: Sequence[MessageReader]) -> int:
    """The searches the requests ran: the provider's own count, else the search blocks counted."""
    total = 0
    for reader in readers:
        counted = reader.search_requests
        total += counted if counted is not None else sum(
            1 for block in reader.blocks
            if block.get("type") == "server_tool_use" and block.get("name") == "web_search"
        )
    return total


def findings_from(
    readers: Sequence[MessageReader], *, stopped: Literal["minute", "failed"] | None,
) -> DeepFindings:
    """Read the requests of one deep search: the brief, its sources, the pages found.

    ``stopped`` says the search did not finish: no brief is taken then, only the pages. A
    search that finished with no text after its last search has no brief either, and is read
    as one that failed.
    """
    blocks = [block for reader in readers for block in reader.blocks]
    pages, searches = _pages(blocks), _search_count(readers)
    if stopped is None:
        final_text = _final_text_blocks(blocks)
        written = "".join(str(block.get("text") or "") for block in final_text).strip()
        brief, listed = _split_sources(written)
        if brief:
            length = readers[-1].stop_reason == "max_tokens"
            return DeepFindings(
                brief, _sources(final_text, listed, pages), pages, searches,
                "length" if length else None,
            )
        stopped = "failed"
    return DeepFindings("", (), pages, searches, stopped)


class _Reading:
    """The requests of one deep search, read on a thread of their own.

    The caller keeps the clock and the deadline: it waits on :attr:`done`, and past the deadline
    calls :meth:`stop`, which freezes what was read and cancels the request, so a server that has
    gone quiet cannot hold the call. :meth:`readers` is one :class:`MessageReader` per request.
    """

    def __init__(  # noqa: PLR0913 — one search's request, its clock and its deadline
        self, client: _Client, query: str, system: str, tools: list[dict[str, Any]],
        clock: Clock, deadline: float,
    ) -> None:
        self._client, self._query, self._system, self._tools = client, query, system, tools
        self._clock, self._deadline = clock, deadline
        self._readers: list[MessageReader] = []
        self.error: Exception | None = None
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._frozen = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Begin reading; the thread is the caller's to abandon, not to join."""
        threading.Thread(target=self._run, name="deep-search", daemon=True).start()

    def stop(self) -> None:
        """Keep what has been read and cancel the request wherever it is waiting."""
        with self._lock:
            self._frozen = True
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)

    def readers(self) -> list[MessageReader]:
        """The requests so far, in order; none is added once the search is stopped."""
        with self._lock:
            return list(self._readers)

    def _run(self) -> None:
        try:
            asyncio.run(self._read())
        except asyncio.CancelledError:
            pass  # stopped at the deadline: the caller has what was read
        except Exception as exc:  # noqa: BLE001 — kept for the caller, who words it
            self.error = exc
        finally:
            self.done.set()

    def _begin(self, reader: MessageReader) -> bool:
        """Count ``reader``'s request unless the search was stopped meanwhile; whether it did."""
        with self._lock:
            if self._frozen:
                return False
            self._readers.append(reader)
            return True

    def _take(self, reader: MessageReader, event: Mapping[str, Any]) -> bool:
        """Give ``reader`` the event unless the search was stopped meanwhile; whether it did."""
        with self._lock:
            if self._frozen:
                return False
            reader.feed(event)
            return True

    async def _read(self) -> None:
        with self._lock:
            if self._frozen:
                return
            self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
        paused: list[dict[str, Any]] = []
        while True:
            stream = self._client.message_events(
                messages=[{"role": "user", "content": self._query}], system=self._system,
                tools=self._tools, paused=paused,
            )
            reader = MessageReader(stream.request_id)
            if not self._begin(reader):
                return
            async for event in stream.events:
                if not self._take(reader, event):
                    return
            if reader.stop_reason != "pause_turn" or self._clock.monotonic() >= self._deadline:
                return
            paused = [*paused, *reader.blocks]  # sent back as they came, with no user message


class _RequestScopedDeepSearch:
    """Mint isolated provider and accounting state in the calling worker."""

    def __init__(
        self,
        sessions: LLMSessionFactory,
        *,
        max_uses: int,
        event_log_path: Path | None,
        pricing_path: Path | None,
        home: HomePlace | None,
    ) -> None:
        self._factory = sessions
        self._snapshot = sessions.snapshot()
        self._max_uses = max_uses
        self._event_log_path = event_log_path
        self._pricing = {} if pricing_path is None else load_pricing_table(pricing_path)
        self._home = home

    def research(self, query: str, *, where: Coarse | None, clock: Clock) -> DeepFindings:
        """Search within one minute of ``clock``; the brief and its sources, or the pages found."""
        deadline = clock.monotonic() + _BUDGET_S
        today = clock.now().date()
        tool: dict[str, Any] = {
            "type": _WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": self._max_uses,
        }
        if location := user_location(where, self._home):
            tool["user_location"] = location
        client = self._factory.create(self._snapshot, response_id=new_response_id())
        reading = _Reading(
            client, query,
            _RESEARCH_SYSTEM.format(today=f"{lang.weekday(today, 'en')}, {today.isoformat()}"),
            [tool], clock, deadline,
        )
        reading.start()
        finished = clock.wait(reading.done, max(0.0, deadline - clock.monotonic()))
        if not finished:
            reading.stop()
        readers, error = reading.readers(), reading.error if finished else None
        last = readers[-1] if readers else None
        # A message that closed just as the minute ended is a whole brief; any other is not.
        whole = last is not None and last.ended and last.stop_reason not in (None, "pause_turn")
        self._account(client, readers, whole=whole, error=error)
        stopped: Literal["minute", "failed"] | None = None
        if not whole:
            late = not finished or (last is not None and last.stop_reason == "pause_turn")
            stopped = "minute" if late else "failed"
        found = findings_from(readers, stopped=stopped)
        if not found.brief and not found.pages and stopped != "minute":
            msg = "the deep search ended without a written brief"
            raise DeepSearchError(msg if error is None else _redact_error(error)) from None
        return found

    def _account(
        self, client: _Client, readers: Sequence[MessageReader], *,
        whole: bool, error: Exception | None,
    ) -> None:
        """One cost row per request: done, or cancelled at the deadline, or failed."""
        if self._event_log_path is None or not readers:
            return
        with contextlib.closing(open_runtime_event_log(self._event_log_path)) as conn:
            recorder = CostRecorder(conn, pricing_table=self._pricing)
            for one in readers[:-1]:
                recorder.record_stream(
                    one.disposition("completed"), provider=client.provider, model=client.model,
                    kind="deep_search", turn_id=None,
                )
            last: LLMRequestOutcome = (
                "completed" if whole else "error" if error is not None else "cancelled"
            )
            recorder.record_stream(
                readers[-1].disposition(last, None if error is None else type(error).__name__),
                provider=client.provider, model=client.model, kind="deep_search", turn_id=None,
            )


def _anthropic_host(preset: Mapping[str, Any]) -> bool:
    """Only Anthropic's own host runs its web search; same test as `native_tool_search`."""
    if str(preset.get("provider", "")).strip().lower() != "anthropic":
        return False
    return "api.anthropic.com" in str(preset.get("base_url") or "api.anthropic.com")


def _web_block(config: Mapping[str, Any]) -> Mapping[str, Any]:
    tools = config.get("tools")
    web = tools.get("web") if isinstance(tools, Mapping) else None
    return web if isinstance(web, Mapping) else {}


def build_deep_search(  # noqa: PLR0913 — the config, the accounting and the home
    config: Mapping[str, Any],
    *,
    event_log_path: Path | None = None,
    pricing_path: Path | None = None,
    account_cost: bool = False,
    sessions: LLMSessionFactory | None = None,
    home: HomePlace | None = None,
) -> DeepSearch | None:
    """Build the `web_search` deep seam from `tools.web.deep_preset` / `deep_max_uses`.

    `None` when that preset is absent, malformed, or not Anthropic on Anthropic's
    host: `web_search` then answers a deep request with a wider quick search. A
    missing deep search is one optional capability off, so it never fails boot.

    `sessions` mints each request's client: omitted, the real factory over the deep
    preset; a test hands the scripted model's. `home` is where he is when no report of his
    position can say (ADR 0359).
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
        "timeout_s": _BUDGET_S,
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
        home=home,
    )
