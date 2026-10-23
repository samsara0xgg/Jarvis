"""One Anthropic message read from its raw stream, block by block (ADR 0359).

The typed stream (``llm_stream``) turns text and tool calls into events and refuses what else a
response holds. A request that runs Anthropic's own web search returns more: the search calls,
their results with each page's age, and text that cites them. A deep search needs all of it, to
read the pages it found when a deadline cuts it short and to send a paused turn back as it came,
so :class:`MessageReader` keeps every block as the API sent it, readable at any moment. Usage and
the stop reason are the typed stream's own, read by its own rules.

Layer rules: stdlib and ``jarvis.decision``.
"""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.llm_stream import StreamDisposition, StreamNormalizer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from jarvis.shared.realtime import LLMRequestOutcome

_MESSAGE_EVENTS: Final = frozenset({"message_start", "message_delta", "message_stop"})
"""The events that say who answered, how much it cost and why it stopped; the typed stream's
normalizer reads these and no block."""


@dataclass(frozen=True)
class MessageStream:
    """One request's raw stream events and the identity it was given before it went out."""

    request_id: str
    events: AsyncIterator[Mapping[str, Any]]


class MessageReader:
    """The blocks of one streamed message so far, and how it stopped.

    Fed one raw event at a time on one thread and read on another: every method takes the lock.
    A block is a dict as the API would send it back: a text block carries its text and citations
    so far, a thinking block its signature, a tool call its parsed input once it is complete.
    """

    def __init__(self, request_id: str) -> None:
        """Start an empty message for the request that carries ``request_id``."""
        self.request_id = request_id
        self._lock = threading.Lock()
        self._blocks: dict[int, dict[str, Any]] = {}
        self._input: dict[int, str] = {}
        self._stopped: set[int] = set()
        self._usage = StreamNormalizer("anthropic", request_id)
        self._searches: int | None = None
        self._ended = False

    def feed(self, event: Mapping[str, Any]) -> None:
        """Take one raw stream event; an event this reader does not know is left alone."""
        with self._lock:
            kind = event.get("type")
            if kind in _MESSAGE_EVENTS:
                self._usage.feed(event)
                self._message(event)
            elif kind == "content_block_start":
                self._start(event)
            elif kind == "content_block_delta":
                self._delta(event)
            elif kind == "content_block_stop":
                self._stop(event)

    def _message(self, event: Mapping[str, Any]) -> None:
        if event["type"] == "message_stop":
            self._ended = True
            return
        usage = event.get("usage") or (event.get("message") or {}).get("usage")
        server = usage.get("server_tool_use") if isinstance(usage, dict) else None
        counted = server.get("web_search_requests") if isinstance(server, dict) else None
        if isinstance(counted, int) and not isinstance(counted, bool):
            self._searches = counted

    def _start(self, event: Mapping[str, Any]) -> None:
        index, block = int(event["index"]), copy.deepcopy(dict(event["content_block"]))
        match block.get("type"):
            case "text":
                block["text"] = block.get("text") or ""
            case "thinking":
                block["thinking"] = block.get("thinking") or ""
                block["signature"] = block.get("signature") or ""
            case "tool_use" | "server_tool_use":
                block["input"] = block.get("input") or {}
                self._input[index] = ""
        self._blocks[index] = block

    def _delta(self, event: Mapping[str, Any]) -> None:
        index, delta = int(event["index"]), event.get("delta") or {}
        block = self._blocks.get(index)
        if block is None:
            return
        match delta.get("type"):
            case "text_delta":
                block["text"] += delta.get("text") or ""
            case "citations_delta":
                block.setdefault("citations", []).append(copy.deepcopy(delta["citation"]))
            case "input_json_delta":
                self._input[index] = self._input.get(index, "") + (delta.get("partial_json") or "")
            case "thinking_delta":
                block["thinking"] += delta.get("thinking") or ""
            case "signature_delta":
                block["signature"] += delta.get("signature") or ""

    def _stop(self, event: Mapping[str, Any]) -> None:
        index = int(event["index"])
        self._stopped.add(index)
        if (parsed := self._parsed(index)) is not None:
            self._blocks[index]["input"] = parsed

    def _parsed(self, index: int) -> dict[str, Any] | None:
        """A tool call's input as far as it has streamed, if that is a whole JSON object."""
        try:
            parsed = json.loads(self._input.get(index) or "")
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @property
    def blocks(self) -> list[dict[str, Any]]:
        """Every block so far, in the order the API gave them, as copies the caller may keep."""
        with self._lock:
            found = copy.deepcopy([self._blocks[i] for i in sorted(self._blocks)])
            for block, index in zip(found, sorted(self._blocks), strict=True):
                if index not in self._stopped and (parsed := self._parsed(index)) is not None:
                    block["input"] = parsed
            return found

    @property
    def stop_reason(self) -> str | None:
        """Why the message ended (``end_turn``, ``pause_turn``, ``max_tokens``), else ``None``."""
        with self._lock:
            return self._usage.finish_reason

    @property
    def ended(self) -> bool:
        """Whether the message's closing event arrived."""
        with self._lock:
            return self._ended

    @property
    def search_requests(self) -> int | None:
        """How many searches the provider says it ran, if it said."""
        with self._lock:
            return self._searches

    def disposition(
        self, outcome: LLMRequestOutcome, error_code: str | None = None,
    ) -> StreamDisposition:
        """The accounting outcome of this request as far as it got: what it used, how it ended."""
        with self._lock:
            return StreamDisposition(
                llm_request_id=self.request_id,
                provider_response_id=self._usage.response_id,
                outcome=outcome,
                usage=self._usage.usage(completed=outcome == "completed"),
                finish_reason=self._usage.finish_reason,
                error_code=error_code,
            )


__all__ = ["MessageReader", "MessageStream"]
