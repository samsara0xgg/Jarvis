"""ADR 0276: one timing fact per answered turn, measured on the brain's own monotonic clock.

The brain keeps a :class:`TurnTimer` for each turn it drives (the runtime binds it for the
turn's thread) and the code that already knows each moment marks it: the context blocks, the
model requests (sent, first stream event, first thinking, first text, usage), the first chunk
that left her and the holds it waited behind. ``drive_turn`` writes the result as one
``turn.timing`` event when the answer is out. Nothing here changes what is said or when; every
mark is a clock read and an assignment, and a turn nobody timed (a caller outside the runtime)
marks into nothing.
"""

from __future__ import annotations

import contextlib
import contextvars
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

from jarvis.decision.llm_stream import (
    LLMStreamHandle,
    LLMTextDelta,
    LLMToolCallCompleted,
    LLMUsageCompleted,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_NS_PER_MS: Final = 1_000_000
_MIN_HOLD_NS: Final = _NS_PER_MS
"""A wait under 1 ms is a check that found nothing to wait for, not a hold."""


def _ms(since_ns: int, at_ns: int | None) -> float | None:
    return None if at_ns is None else round((at_ns - since_ns) / _NS_PER_MS, 1)


@dataclass
class RequestTiming:
    """One model request: when it was made, when each kind of output first came back."""

    kind: str
    streamed: bool = True
    provider: str | None = None
    model: str | None = None
    preset: str | None = None
    opened_ns: int = field(default_factory=time.monotonic_ns)
    sent_ns: int | None = None
    first_event_ns: int | None = None
    first_thinking_ns: int | None = None
    first_text_ns: int | None = None
    block_closed_ns: int | None = None
    done_ns: int | None = None
    thinking_bytes: int | None = None
    tool_call: bool = False
    llm_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    service_tier: str | None = None

    def seen(self, event: object) -> None:
        """Take one typed event the consumer read: usage, a first text, a tool call."""
        if isinstance(event, LLMTextDelta):
            if event.text and self.first_text_ns is None:
                self.first_text_ns = time.monotonic_ns()
        elif isinstance(event, LLMUsageCompleted):
            self.llm_request_id = event.llm_request_id
            self.input_tokens = event.input_tokens
            self.output_tokens = event.output_tokens
            self.cache_read_tokens = event.cache_read_tokens
            self.cache_write_tokens = event.cache_write_tokens
            self.service_tier = event.service_tier
        elif isinstance(event, LLMToolCallCompleted):
            self.tool_call = True

    def block_closed(self) -> None:
        """The reply's claims block has closed (ADR 0320): speech may start from here."""
        if self.block_closed_ns is None:
            self.block_closed_ns = time.monotonic_ns()

    def done(self, handle: object) -> None:
        """The stream is closed: take what the stream itself saw, which is nearer the wire."""
        self.done_ns = time.monotonic_ns()
        if isinstance(handle, LLMStreamHandle):
            seen = handle.timing
            self.sent_ns = seen.sent_ns
            self.first_event_ns = seen.first_event_ns
            self.first_thinking_ns = seen.first_thinking_ns
            self.first_text_ns = seen.first_text_ns or self.first_text_ns
            self.thinking_bytes = seen.thinking_bytes

    def chat_done(self, result: Any) -> None:  # noqa: ANN401 - a ChatResult, read by attribute
        """A request that returns whole: its text is there only when it is done."""
        self.streamed = False
        self.done_ns = time.monotonic_ns()
        self.sent_ns = self.opened_ns
        if getattr(result, "text", None):
            self.first_text_ns = self.done_ns
        self.tool_call = bool(getattr(result, "tool_calls", ()))
        self.llm_request_id = getattr(result, "llm_request_id", None) or None
        self.input_tokens = getattr(result, "input_tokens", None)
        self.output_tokens = getattr(result, "output_tokens", None)
        self.cache_read_tokens = getattr(result, "cache_read_in", None)
        self.cache_write_tokens = getattr(result, "cache_write_in", None)
        self.service_tier = getattr(result, "service_tier", None)

    def payload(self, t0: int) -> dict[str, Any]:
        """The request as the event carries it, every time in ms from the turn's start."""
        return {
            "kind": self.kind,
            "streamed": self.streamed,
            "model": self.model,
            "preset": self.preset,
            "llm_request_id": self.llm_request_id,
            "opened_ms": _ms(t0, self.opened_ns),
            "sent_ms": _ms(t0, self.sent_ns),
            "first_event_ms": _ms(t0, self.first_event_ns),
            "first_thinking_ms": _ms(t0, self.first_thinking_ns),
            "first_text_ms": _ms(t0, self.first_text_ns),
            "block_ms": _ms(self.opened_ns, self.block_closed_ns),
            "done_ms": _ms(t0, self.done_ns),
            "tool_call": self.tool_call,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "thinking_bytes": self.thinking_bytes,
            "service_tier": self.service_tier,
        }


class TurnTimer:
    """The clock of one turn: ``t0`` is the moment the brain began driving it."""

    def __init__(self, *, turn_id: str, channel: str | None, trigger: str, queue_ms: int) -> None:
        """Start at once; ``queue_ms`` is how long the intent sat in the log before this."""
        self.t0 = time.monotonic_ns()
        self.turn_id = turn_id
        self.channel = channel
        self.trigger = trigger
        self.queue_ms = queue_ms
        self.device: str | None = None
        self.marks: dict[str, int] = {}
        self.parts: dict[str, int] = {}
        self.holds: dict[str, int] = {}
        self.requests: list[RequestTiming] = []

    def mark(self, name: str) -> None:
        """Remember the first time ``name`` happened."""
        self.marks.setdefault(name, time.monotonic_ns())

    def hold(self, name: str, since_ns: int) -> None:
        """A wait of ``name`` from ``since_ns`` has ended; it counts until a chunk is out."""
        waited = time.monotonic_ns() - since_ns
        if "first_chunk" not in self.marks and waited >= _MIN_HOLD_NS:
            self.holds[name] = self.holds.get(name, 0) + waited

    @contextlib.contextmanager
    def part(self, name: str) -> Iterator[None]:
        """Add the time spent inside to context part ``name``, until the first request opens."""
        start = time.monotonic_ns()
        try:
            yield
        finally:
            if not self.requests:
                self.parts[name] = self.parts.get(name, 0) + time.monotonic_ns() - start

    def timed[F: Callable[..., Any]](self, name: str, fn: F | None) -> F | None:
        """``fn`` as it was, but every call is counted in context part ``name``."""
        if fn is None:
            return None

        def run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - fn's own signature
            with self.part(name):
                return fn(*args, **kwargs)

        return cast("F", run)

    def request(self, kind: str, client: object) -> RequestTiming:
        """Open the record of a request about to be made on ``client``."""
        # A run's client is pinned to a preset snapshot; its ``active_preset`` is a placeholder.
        pinned = getattr(client, "preset_snapshot", None)
        record = RequestTiming(
            kind=kind,
            provider=getattr(client, "provider", None),
            model=getattr(client, "model", None),
            preset=getattr(pinned, "preset_name", None) or getattr(client, "active_preset", None),
        )
        self.requests.append(record)
        return record

    def payload(self) -> dict[str, Any]:
        """The turn's ``turn.timing`` event payload, every time in ms from ``t0``."""
        decisions = [r for r in self.requests if r.kind == "decision"]
        answering = next((r for r in decisions if r.first_text_ns is not None), None)
        tool_first = None if answering is None else any(
            r.tool_call for r in decisions[: decisions.index(answering)]
        )
        asked = decisions[0] if decisions else None
        held = {name: round(ns / _NS_PER_MS, 1) for name, ns in self.holds.items()}
        return {
            "turn_id": self.turn_id,
            "channel": self.channel,
            "device": self.device,
            "trigger": self.trigger,
            "provider": asked.provider if asked else None,
            "model": asked.model if asked else None,
            "preset": asked.preset if asked else None,
            "queue_ms": self.queue_ms,
            "decide_start_ms": _ms(self.t0, self.marks.get("decide_start")),
            "context_ms": _ms(self.t0, self.requests[0].opened_ns) if self.requests else None,
            "context_parts": {name: round(ns / _NS_PER_MS, 1) for name, ns in self.parts.items()},
            "first_text_ms": _ms(self.t0, answering.first_text_ns) if answering else None,
            "first_chunk_ms": _ms(self.t0, self.marks.get("first_chunk")),
            "done_ms": _ms(self.t0, self.marks.get("rendered")),
            "tool_round_before_text": tool_first,
            "holds": held,
            "held_by": max(held, key=lambda name: held[name]) if held else None,
            "requests": [r.payload(self.t0) for r in self.requests],
        }


_CURRENT: contextvars.ContextVar[TurnTimer | None] = contextvars.ContextVar(
    "jarvis_turn_timer", default=None,
)


def bind(timer: TurnTimer) -> contextvars.Token[TurnTimer | None]:
    """Make ``timer`` the one the code on this thread marks, until :func:`release`."""
    return _CURRENT.set(timer)


def release(token: contextvars.Token[TurnTimer | None]) -> None:
    """Undo a :func:`bind`."""
    _CURRENT.reset(token)


def mark(name: str) -> None:
    """Mark ``name`` on the bound timer, if there is one."""
    if (timer := _CURRENT.get()) is not None:
        timer.mark(name)


def hold(name: str, since_ns: int) -> None:
    """Count a wait of ``name`` that began at ``since_ns``, if a timer is bound."""
    if (timer := _CURRENT.get()) is not None:
        timer.hold(name, since_ns)


def part(name: str) -> contextlib.AbstractContextManager[None]:
    """Count the time inside as context part ``name``, if a timer is bound."""
    timer = _CURRENT.get()
    return contextlib.nullcontext() if timer is None else timer.part(name)


def request(kind: str, client: object) -> RequestTiming:
    """The record of a request about to be made; kept on the bound timer if there is one."""
    timer = _CURRENT.get()
    return timer.request(kind, client) if timer is not None else RequestTiming(kind=kind)
