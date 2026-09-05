"""Provider-neutral asynchronous stream protocol and cancellation ownership.

No tool executes here. Completed proposals exist only after protocol EOF and
validation of every tool block; L3 still owns authorization and dispatch.
"""

# ruff: noqa: EM101 — exception arguments are stable protocol codes, not prose.
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from jarvis.shared.realtime import LLMRequestOutcome, LLMUsageStatus


_MAX_TOOLS = 32
_MAX_ARGUMENT_BYTES = 65_536
_MAX_CALL_ID = 512
_MAX_TOOL_NAME = 256
_MAX_BLOCKS = 128


@dataclass(frozen=True, kw_only=True)
class _Event:
    llm_request_id: str
    provider_response_id: str | None


@dataclass(frozen=True, kw_only=True)
class LLMTextDelta(_Event):
    """Untrusted model text, never an emission permit."""

    text: str
    content_block_index: int
    kind: Literal["text_delta"] = "text_delta"


@dataclass(frozen=True, kw_only=True)
class LLMToolCallStarted(_Event):
    """First observed tool delta; immediately freezes routine text emission."""

    call_index: int
    call_id: str | None
    name: str | None
    kind: Literal["tool_started"] = "tool_started"


@dataclass(frozen=True, kw_only=True)
class LLMToolArgumentsDelta(_Event):
    """Partial tool JSON; informational only."""

    call_index: int
    call_id: str | None
    arguments_delta: str
    kind: Literal["tool_arguments_delta"] = "tool_arguments_delta"


@dataclass(frozen=True, kw_only=True)
class LLMToolCallCompleted(_Event):
    """An assembled, syntactically valid proposal awaiting the L3 gate."""

    call_index: int
    call_id: str
    name: str
    arguments_json: str
    kind: Literal["tool_completed"] = "tool_completed"


@dataclass(frozen=True, kw_only=True)
class LLMUsageCompleted(_Event):
    """Exactly one explicit usage summary, including unavailable usage."""

    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    usage_status: LLMUsageStatus
    kind: Literal["usage_completed"] = "usage_completed"


@dataclass(frozen=True, kw_only=True)
class LLMResponseCompleted(_Event):
    """Provider generation completion, not gate approval or physical delivery."""

    finish_reason: str
    kind: Literal["response_completed"] = "response_completed"


@dataclass(frozen=True, kw_only=True)
class LLMResponseFailed(_Event):
    """Transport/protocol failure with no executable proposal."""

    error_code: str
    retryable: bool
    kind: Literal["response_failed"] = "response_failed"


type LLMStreamEvent = (
    LLMTextDelta | LLMToolCallStarted | LLMToolArgumentsDelta | LLMToolCallCompleted
    | LLMUsageCompleted | LLMResponseCompleted | LLMResponseFailed
)


@dataclass(frozen=True)
class StreamDisposition:
    """One immutable accounting outcome, also returned by explicit cancellation."""

    llm_request_id: str
    provider_response_id: str | None
    outcome: LLMRequestOutcome
    usage: LLMUsageCompleted
    finish_reason: str | None
    error_code: str | None


class StreamProtocolError(ValueError):
    """Malformed/incomplete provider data must not become an action."""


def _protocol_error(code: str) -> StreamProtocolError:
    return StreamProtocolError(code)


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _protocol_error("expected_object")
    return value


def _index(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _protocol_error("invalid_block_index")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise _protocol_error("invalid_text_delta")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _protocol_error("duplicate_argument_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise _protocol_error("nonfinite_argument_number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _protocol_error("nonfinite_argument_number")
    return parsed


@dataclass
class _ToolBlock:
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    argument_bytes: int = 0
    stopped: bool = False
    initial_object: dict[str, Any] | None = None


class StreamNormalizer:
    """Bounded provider protocol state; consumes usage through actual EOF."""

    def __init__(self, provider: Literal["openai", "anthropic"], request_id: str) -> None:
        """Bind one provider/request without any network or mutable client state."""
        self.provider = provider
        self.request_id = request_id
        self.response_id: str | None = None
        self.finish_reason: str | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cache_read_tokens: int | None = None
        self.cache_write_tokens: int | None = None
        self._tools: dict[int, _ToolBlock] = {}
        self._blocks: dict[int, str] = {}
        self._message_started = False
        self._message_stopped = False
        self._final_usage_seen = False

    def _identity(self) -> dict[str, Any]:
        return {"llm_request_id": self.request_id, "provider_response_id": self.response_id}

    def _response_id(self, value: object) -> None:
        if value is None:
            return
        candidate = _text(value)
        if not candidate or (self.response_id is not None and candidate != self.response_id):
            raise _protocol_error("response_identity_changed")
        self.response_id = candidate

    def _usage(self, value: object, names: Mapping[str, str]) -> None:
        if value is None:
            return
        usage = _object(value)
        for provider_name, field_name in names.items():
            number = usage.get(provider_name)
            if number is not None:
                setattr(self, field_name, _index(number))

    def _tool(self, index: int) -> _ToolBlock:
        if index not in self._tools:
            if len(self._tools) >= _MAX_TOOLS:
                raise _protocol_error("too_many_tool_calls")
            self._tools[index] = _ToolBlock()
        return self._tools[index]

    def _arguments(self, index: int, fragment: object) -> LLMToolArgumentsDelta:
        tool = self._tool(index)
        if tool.stopped:
            raise _protocol_error("tool_delta_after_stop")
        if tool.initial_object:
            raise _protocol_error("arguments_after_initial_object")
        text = _text(fragment)
        tool.argument_bytes += len(text.encode("utf-8"))
        if tool.argument_bytes > _MAX_ARGUMENT_BYTES:
            raise _protocol_error("tool_arguments_too_large")
        tool.arguments += text
        return LLMToolArgumentsDelta(
            **self._identity(), call_index=index, call_id=tool.call_id or None,
            arguments_delta=text,
        )

    def feed(self, raw: Mapping[str, Any]) -> list[LLMStreamEvent]:
        """Normalize one SDK event; this never produces a completed proposal."""
        if self.provider == "openai":
            return self._openai(raw)
        return self._anthropic(raw)

    def _openai_final_usage(self, usage: object) -> None:
        if self.finish_reason and isinstance(usage, dict) and all(
            usage.get(key) is not None for key in ("prompt_tokens", "completion_tokens")
        ):
            self._final_usage_seen = True

    def _openai(self, raw: Mapping[str, Any]) -> list[LLMStreamEvent]:  # noqa: C901, PLR0912
        self._response_id(raw.get("id"))
        self._usage(raw.get("usage"), {
            "prompt_tokens": "input_tokens", "completion_tokens": "output_tokens",
        })
        usage = raw.get("usage")
        if isinstance(usage, dict):
            self._usage(usage.get("prompt_tokens_details"), {"cached_tokens": "cache_read_tokens"})
        choices = raw.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise _protocol_error("unsupported_choice_count")
        if not choices:
            self._openai_final_usage(usage)
            return []
        choice = _object(choices[0])
        if _index(choice.get("index", 0)) != 0:
            raise _protocol_error("unsupported_choice_index")
        delta = _object(choice.get("delta") or {})
        events: list[LLMStreamEvent] = []
        if self.finish_reason and any(delta.get(key) for key in ("content", "tool_calls")):
            raise _protocol_error("delta_after_finish")
        if delta.get("refusal"):
            raise _protocol_error("provider_refusal")
        calls = delta.get("tool_calls") or []
        if not isinstance(calls, list):
            raise _protocol_error("invalid_tool_calls")
        for value in calls:
            call = _object(value)
            if call.get("type", "function") != "function":
                raise _protocol_error("unsupported_tool_type")
            index = _index(call.get("index"))
            first = index not in self._tools
            tool = self._tool(index)
            function = _object(call.get("function") or {})
            tool.call_id += _text(call.get("id") or "")
            tool.name += _text(function.get("name") or "")
            if len(tool.call_id) > _MAX_CALL_ID or len(tool.name) > _MAX_TOOL_NAME:
                raise _protocol_error("tool_identity_too_large")
            if first:
                events.append(LLMToolCallStarted(
                    **self._identity(), call_index=index, call_id=tool.call_id or None,
                    name=tool.name or None,
                ))
            if "arguments" in function:
                events.append(self._arguments(index, function["arguments"]))
        # A single chunk can contain text and a tool proposal. Freeze routine
        # emission before its text becomes visible to the segment consumer.
        if delta.get("content"):
            events.append(LLMTextDelta(
                **self._identity(), text=_text(delta["content"]), content_block_index=0,
            ))
        reason = choice.get("finish_reason")
        if reason:
            if self.finish_reason is not None:
                raise _protocol_error("duplicate_finish")
            self.finish_reason = _text(reason)
        self._openai_final_usage(usage)
        return events

    def _anthropic(self, raw: Mapping[str, Any]) -> list[LLMStreamEvent]:  # noqa: C901, PLR0911, PLR0912, PLR0915
        kind = _text(raw.get("type"))
        if kind == "ping":
            return []
        if self._message_stopped:
            raise _protocol_error("event_after_message_stop")
        if kind == "message_start":
            if self._message_started:
                raise _protocol_error("duplicate_message_start")
            self._message_started = True
            message = _object(raw.get("message"))
            self._response_id(message.get("id"))
            self._usage(message.get("usage"), {
                "input_tokens": "input_tokens", "output_tokens": "output_tokens",
                "cache_read_input_tokens": "cache_read_tokens",
                "cache_creation_input_tokens": "cache_write_tokens",
            })
            return []
        if not self._message_started:
            raise _protocol_error("missing_message_start")
        if self.finish_reason and kind.startswith("content_block_"):
            raise _protocol_error("delta_after_finish")
        if kind == "content_block_start":
            index = _index(raw.get("index"))
            if index in self._blocks:
                raise _protocol_error("duplicate_content_block")
            if len(self._blocks) >= _MAX_BLOCKS:
                raise _protocol_error("too_many_content_blocks")
            block = _object(raw.get("content_block"))
            block_type = _text(block.get("type"))
            self._blocks[index] = block_type
            if block_type == "tool_use":
                tool = self._tool(index)
                tool.call_id = _text(block.get("id"))
                tool.name = _text(block.get("name"))
                if len(tool.call_id) > _MAX_CALL_ID or len(tool.name) > _MAX_TOOL_NAME:
                    raise _protocol_error("tool_identity_too_large")
                initial = _object(block.get("input"))
                if len(json.dumps(initial).encode("utf-8")) > _MAX_ARGUMENT_BYTES:
                    raise _protocol_error("tool_arguments_too_large")
                tool.initial_object = initial
                return [LLMToolCallStarted(
                    **self._identity(), call_index=index, call_id=tool.call_id, name=tool.name,
                )]
            if block_type == "text":
                text = _text(block.get("text", ""))
                return [LLMTextDelta(
                    **self._identity(), text=text, content_block_index=index,
                )] if text else []
            if block_type not in {"thinking", "redacted_thinking"}:
                raise _protocol_error("unsupported_content_block")
            return []
        if kind == "content_block_delta":
            index = _index(raw.get("index"))
            active_block = self._blocks.get(index)
            delta = _object(raw.get("delta"))
            if active_block == "text" and delta.get("type") == "text_delta":
                return [LLMTextDelta(
                    **self._identity(), text=_text(delta.get("text")), content_block_index=index,
                )]
            if active_block == "tool_use" and delta.get("type") == "input_json_delta":
                return [self._arguments(index, delta.get("partial_json"))]
            if active_block == "thinking" and delta.get("type") in {
                "thinking_delta", "signature_delta",
            }:
                return []
            raise _protocol_error("invalid_content_delta")
        if kind == "content_block_stop":
            index = _index(raw.get("index"))
            if index not in self._blocks or self._blocks[index] == "stopped":
                raise _protocol_error("invalid_block_stop")
            self._blocks[index] = "stopped"
            if index in self._tools:
                self._tools[index].stopped = True
            return []
        if kind == "message_delta":
            delta = _object(raw.get("delta"))
            reason = delta.get("stop_reason")
            if reason:
                if self.finish_reason is not None:
                    raise _protocol_error("duplicate_finish")
                if any(value != "stopped" for value in self._blocks.values()):
                    raise _protocol_error("finish_before_block_stop")
                self.finish_reason = _text(reason)
            usage = raw.get("usage")
            self._usage(usage, {"output_tokens": "output_tokens"})
            if isinstance(usage, dict) and usage.get("output_tokens") is not None:
                self._final_usage_seen = True
            return []
        if kind == "message_stop":
            self._message_stopped = True
            return []
        raise _protocol_error("unsupported_provider_event")

    def complete(self) -> list[LLMToolCallCompleted]:
        """Validate the complete response and every proposal before exposing any."""
        if not self.finish_reason:
            raise _protocol_error("missing_finish_reason")
        if self.provider == "anthropic" and (
            not self._message_stopped or any(value != "stopped" for value in self._blocks.values())
        ):
            raise _protocol_error("incomplete_message")
        if self._tools and self.finish_reason not in {"tool_calls", "tool_use"}:
            raise _protocol_error("tool_finish_mismatch")
        results: list[LLMToolCallCompleted] = []
        call_ids: set[str] = set()
        for index, tool in sorted(self._tools.items()):
            if not tool.call_id or not tool.name or tool.call_id in call_ids:
                raise _protocol_error("invalid_tool_identity")
            call_ids.add(tool.call_id)
            arguments = tool.arguments
            if not arguments:
                if tool.initial_object is None:
                    raise _protocol_error("missing_tool_arguments")
                arguments = json.dumps(tool.initial_object)
            parsed = json.loads(
                arguments, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
            _object(parsed)
            results.append(LLMToolCallCompleted(
                **self._identity(), call_index=index, call_id=tool.call_id,
                name=tool.name, arguments_json=arguments,
            ))
        return results

    def usage(self, *, completed: bool) -> LLMUsageCompleted:
        """Expose known counts without turning interrupted requests into free calls."""
        known = self.input_tokens is not None or self.output_tokens is not None
        both = self.input_tokens is not None and self.output_tokens is not None
        status: LLMUsageStatus = (
            "provider_final" if completed and both and self._final_usage_seen
            else "partial" if known else "unavailable"
        )
        return LLMUsageCompleted(
            **self._identity(), input_tokens=self.input_tokens, output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens, cache_write_tokens=self.cache_write_tokens,
            usage_status=status,
        )


class LLMStreamHandle:
    """Single-reader async stream; cancellation closes in-flight network reads.

    All methods run on the owning event loop. A cancelled stream emits no more
    events; cancel() returns its immutable disposition and L3 records it through
    the same mandatory settlement callback as normal completion or failure.
    """

    def __init__(
        self, *, normalizer: StreamNormalizer, source: AsyncIterator[Mapping[str, Any]],
        on_settled: Callable[[StreamDisposition], object],
    ) -> None:
        """Bind a lazy provider source and its L3 accounting owner before I/O."""
        self._normalizer = normalizer
        self._source = source
        self._on_settled = on_settled
        self._claimed = False
        self._cancel_reason: str | None = None
        self._read: asyncio.Task[Mapping[str, Any]] | None = None
        self._close_lock = asyncio.Lock()
        self._source_closed = False
        self._result: StreamDisposition | None = None
        self._accounted = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def result(self) -> StreamDisposition | None:
        """Return the chosen disposition, without implying accounting succeeded."""
        return self._result

    def events(self) -> AsyncIterator[LLMStreamEvent]:
        """Claim the one reader; a second subscription cannot replay model output."""
        if self._claimed:
            raise _protocol_error("stream_already_claimed")
        self._claimed = True
        return self._events()

    def _settle(self, outcome: LLMRequestOutcome, error: str | None = None) -> StreamDisposition:
        if self._result is None:
            self._result = StreamDisposition(
                llm_request_id=self._normalizer.request_id,
                provider_response_id=self._normalizer.response_id,
                outcome=outcome, usage=self._normalizer.usage(completed=outcome == "completed"),
                finish_reason=self._normalizer.finish_reason, error_code=error,
            )
        if not self._accounted:
            # A failed commit may be retried, but never with a different
            # disposition. L2 deduplicates ambiguous commit/replay by request ID.
            self._on_settled(self._result)
            self._accounted = True
        return self._result

    async def _close_source(self) -> None:
        async with self._close_lock:
            if not self._source_closed:
                close = getattr(self._source, "aclose", None)
                if callable(close):
                    await close()
                self._source_closed = True

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise _protocol_error("foreign_stream_event_loop")
        self._loop = loop

    def _cancelled(self) -> bool:
        return self._cancel_reason is not None

    async def _read_next(self) -> Mapping[str, Any]:
        return await anext(self._source)

    async def cancel(self, reason: str) -> StreamDisposition:
        """Stop remote reads and future events, then commit one cancellation outcome."""
        self._bind_loop()
        self._cancel_reason = reason or "cancelled"
        if self._read is not None:
            self._read.cancel()
            await asyncio.gather(self._read, return_exceptions=True)
        try:
            await self._close_source()
        finally:
            result = self._settle("cancelled", self._cancel_reason)
        return result

    async def aclose(self) -> None:
        """Release abandoned streams; preserve an already chosen terminal outcome."""
        await self.cancel("consumer_closed")

    async def _events(self) -> AsyncIterator[LLMStreamEvent]:  # noqa: C901, PLR0912 — single-reader protocol lifecycle
        self._bind_loop()
        outcome: LLMRequestOutcome = "cancelled"
        error: str | None = None
        proposals: list[LLMToolCallCompleted] = []
        try:
            while not self._cancelled():
                self._read = asyncio.create_task(self._read_next())
                try:
                    raw = await self._read
                except StopAsyncIteration:
                    proposals = self._normalizer.complete()
                    outcome = "completed"
                    break
                for event in self._normalizer.feed(raw):
                    if self._cancelled():
                        return
                    yield event
        except asyncio.CancelledError:
            if not self._cancelled():
                error = "consumer_cancelled"
                raise
        except Exception as exc:  # noqa: BLE001 — convert provider/protocol faults to typed failure.
            outcome = "error"
            error = str(exc) if isinstance(exc, StreamProtocolError) else type(exc).__name__
        finally:
            try:
                await self._close_source()
            except Exception as exc:  # noqa: BLE001 — cleanup failure cannot suppress accounting.
                if outcome == "completed":
                    outcome, error = "error", type(exc).__name__
            finally:
                result = self._settle(outcome, self._cancel_reason or error)
        if self._cancelled():
            return
        if result.outcome == "completed":
            for proposal in proposals:
                if self._cancelled():
                    return
                yield proposal
        if self._cancelled():
            return
        yield result.usage
        if self._cancelled():
            return
        if result.outcome == "completed":
            yield LLMResponseCompleted(
                llm_request_id=result.llm_request_id,
                provider_response_id=result.provider_response_id,
                finish_reason=result.finish_reason or "unknown",
            )
        else:
            yield LLMResponseFailed(
                llm_request_id=result.llm_request_id,
                provider_response_id=result.provider_response_id,
                error_code=result.error_code or "provider_error", retryable=False,
            )
