"""One scripted model for tests that run the decision layer.

``ScriptedModel`` stands where ``LLMClient`` stands and answers from a script: text, tool calls,
usage, or a failure per request, in order. It records every request it is sent, so a test
asserts on what the model was shown. It replaces the per-file stubs that each carried their own
half of the client.

``DecisionClient`` is the part of the real client that ``decide()``, the turn timer, the cost
guard, the spoken route and the runtime's connection warm-up read. ``LLMClient`` is checked
against it below, and so is ``ScriptedModel``; if the real client changes shape, the type check
fails here, in one place, instead of a stub going quietly stale in some test file.

A runtime that opens a response run asks its ``LLMSessionFactory`` for the run's own request
client; ``ScriptedSessions`` is that factory with every run answered from the one script, so a
test drives ``drive_turn`` and the real ``decide()`` and still reads, in one place, what the
model was sent across all runs.

The streamed request is real from the provider's chunks onward: ``stream_events`` builds the
production ``LLMStreamHandle`` and ``StreamNormalizer`` over OpenAI chat-completion chunks made
from the script, so text deltas, tool proposals, usage and failure reach the consumer through
the code that handles a provider's stream. Only the network is missing.
"""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from jarvis.decision.llm import ChatResult, LLMClient, Provider, ToolCall
from jarvis.decision.llm_session import LLMPresetSnapshot, LLMRequestClient, LLMSessionFactory
from jarvis.decision.llm_stream import LLMStreamHandle, StreamNormalizer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from jarvis.decision.llm_stream import StreamDisposition


@runtime_checkable
class DecisionClient(Protocol):
    """What the decision layer reads of a model client, as ``LLMClient`` offers it."""

    @property
    def provider(self) -> Provider:
        """``openai`` or ``anthropic``."""
        ...

    @property
    def model(self) -> str:
        """The concrete model id of the active preset."""
        ...

    @property
    def active_preset(self) -> str | None:
        """The preset the client runs on, if it was built from one."""
        ...

    @property
    def native_tool_search(self) -> bool:
        """Whether the provider searches the tool list itself."""
        ...

    @property
    def last_input_tokens(self) -> int | None:
        """Prompt tokens of the most recent ``chat``."""
        ...

    @property
    def last_output_tokens(self) -> int | None:
        """Completion tokens of the most recent ``chat``."""
        ...

    def request_tier(self, service_tier: str | None) -> str | None:
        """The service tier a request to this client sends."""
        ...

    def warm_stream(self) -> None:
        """Open the connection a stream is about to need."""
        ...

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
        service_tier: str | None = None,
    ) -> ChatResult:
        """One round trip."""
        ...

    def stream_events(  # noqa: PLR0913 - the real client's keyword surface
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        on_settled: Callable[[StreamDisposition], object],
        responses: bool = False,
        max_output_tokens: int | None = None,
        text_format: Mapping[str, Any] | None = None,
        service_tier: str | None = None,
    ) -> LLMStreamHandle:
        """One typed stream."""
        ...


@dataclass(frozen=True)
class Call:
    """A tool call the model makes."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass(frozen=True)
class Usage:
    """The token counts a response reports."""

    input_tokens: int = 12
    output_tokens: int = 6
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class Reply:
    """One response: text, tool calls, or both. ``usage=None`` is a response that reports none."""

    text: str | None = None
    calls: tuple[Call, ...] = ()
    usage: Usage | None = field(default_factory=Usage)

    @property
    def finish_reason(self) -> str:
        """``tool_calls`` when the model calls tools, else ``stop`` (the OpenAI words)."""
        return "tool_calls" if self.calls else "stop"


def tool_call(
    name: str, arguments: Mapping[str, Any] | None = None, *, call_id: str | None = None
) -> Reply:
    """A reply that is one tool call and no words."""
    return Reply(calls=(Call(name, {} if arguments is None else arguments, call_id),))


@dataclass(frozen=True)
class Failure:
    """The request fails with ``error``: ``chat`` raises it, a stream breaks with it."""

    error: BaseException


@dataclass(frozen=True)
class Pause:
    """The request holds until the test lets it go, then gets ``then``.

    ``entered`` is set when the request arrives, so the test knows the model is being asked;
    ``released`` is what the test sets to let the answer go. A request that is not released
    within ``timeout_s`` fails with ``TimeoutError`` instead of hanging the suite.
    """

    entered: threading.Event
    released: threading.Event
    then: Reply | Failure | str = ""
    timeout_s: float = 5.0


type Step = Reply | Failure | Pause | str
"""One request's outcome; a bare string is a reply of that text."""


@dataclass(frozen=True)
class Request:
    """What the model was sent, copied at the moment it was sent."""

    kind: Literal["chat", "stream"]
    messages: list[dict[str, Any]]
    system: str
    tools: list[dict[str, Any]] | None
    tool_choice: str | None = None
    service_tier: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


class ScriptExhaustedError(AssertionError):
    """The model was asked more often than the test scripted."""


def _as_step(step: Step) -> Reply | Failure | Pause:
    return Reply(text=step) if isinstance(step, str) else step


class _Script:
    """The steps and the record of requests that a model and all its run clients share."""

    def __init__(self, steps: tuple[Step, ...], otherwise: Step | None) -> None:
        self.steps = [_as_step(step) for step in steps]
        self.otherwise = None if otherwise is None else _as_step(otherwise)
        self.requests: list[Request] = []
        self._lock = threading.Lock()

    def add(self, steps: tuple[Step, ...]) -> None:
        """Append ``steps`` to the script."""
        with self._lock:
            self.steps.extend(_as_step(step) for step in steps)

    def answer(self, request: Request) -> Reply | Failure:
        """Record ``request`` and return the step it gets, once any pause on it is released."""
        with self._lock:
            self.requests.append(request)
            asked, scripted = len(self.requests), len(self.steps)
            if asked <= scripted:
                step = self.steps[asked - 1]
            elif self.otherwise is not None:
                step = self.otherwise
            else:
                msg = (
                    f"the model was sent {request.kind} request {asked} "
                    f"but the script has {scripted} steps"
                )
                raise ScriptExhaustedError(msg)
        if isinstance(step, Pause):
            step.entered.set()
            if not step.released.wait(step.timeout_s):
                msg = f"request {asked} was held {step.timeout_s} s and never released"
                raise TimeoutError(msg)
            then = step.then
            return Reply(text=then) if isinstance(then, str) else then
        return step


class ScriptedModel:
    """A model client that answers each request with the next scripted step."""

    base_url = "http://scripted-model.invalid"
    active_preset: str | None = None

    def __init__(
        self,
        *steps: Step,
        otherwise: Step | None = None,
        provider: Provider = "openai",
        model: str = "scripted-model",
        native_tool_search: bool = False,
    ) -> None:
        """Answer request ``n`` with ``steps[n]``, and any request past them with ``otherwise``.

        Without ``otherwise`` a request past the end fails the test.
        """
        self.provider: Provider = provider
        self.model = model
        self.native_tool_search = native_tool_search
        self._script = _Script(steps, otherwise)
        self._last_usage: Usage | None = None
        self.uploads: list[tuple[str, int, str]] = []
        self.deleted_files: list[str] = []

    @property
    def requests(self) -> list[Request]:
        """Every request the model was sent, in order, across every client made from it."""
        return self._script.requests

    def script(self, *steps: Step) -> None:
        """Add ``steps`` after those the model has; a test can decide each answer as it goes."""
        self._script.add(steps)

    def as_llm_client(self) -> LLMClient:
        """This model where an ``LLMClient`` is typed: the one place the stand-in is declared."""
        return cast("LLMClient", self)

    def for_run(self, snapshot: LLMPresetSnapshot, response_id: str) -> ScriptedRun:
        """The request client of one response run: this script, pinned to the run's preset."""
        return ScriptedRun(self._script, snapshot, response_id, self.native_tool_search)

    @property
    def last_input_tokens(self) -> int | None:
        """Prompt tokens the last ``chat`` reported, or ``None`` if it reported none."""
        return self._last_usage.input_tokens if self._last_usage else None

    @property
    def last_output_tokens(self) -> int | None:
        """Completion tokens the last ``chat`` reported, or ``None`` if it reported none."""
        return self._last_usage.output_tokens if self._last_usage else None

    def request_tier(self, service_tier: str | None) -> str | None:  # noqa: ARG002
        """No tier is sent: only OpenAI's own host gets one, and this is not it."""
        return None

    def warm_stream(self) -> None:
        """A script has no connection to open."""

    def upload_file(self, name: str, data: bytes, mime: str) -> str:
        """Keep the upload as a Claude preset's Files API would; the id counts the uploads."""
        self.uploads.append((name, len(data), mime))
        return f"file_{len(self.uploads):03d}"

    def delete_file(self, file_id: str) -> None:
        """Keep the delete as a Claude preset's Files API would."""
        self.deleted_files.append(file_id)

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
        service_tier: str | None = None,
    ) -> ChatResult:
        """Answer one request with the next step."""
        step = self._script.answer(
            Request(
                "chat",
                copy.deepcopy(messages),
                system,
                copy.deepcopy(tools),
                tool_choice,
                service_tier,
            )
        )
        if isinstance(step, Failure):
            raise step.error
        usage = step.usage
        self._last_usage = usage
        return ChatResult(
            text=step.text,
            tool_calls=tuple(
                ToolCall(
                    call.call_id or f"call-{index}", call.name, json.dumps(dict(call.arguments))
                )
                for index, call in enumerate(step.calls)
            ),
            finish_reason=step.finish_reason,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            raw={},
            model_used=self.model,
            tokens_in=usage.input_tokens if usage else 0,
            tokens_out=usage.output_tokens if usage else 0,
            cache_read_in=usage.cache_read_tokens if usage else 0,
            cache_write_in=usage.cache_write_tokens if usage else 0,
            usage_status="provider_final" if usage else "unavailable",
        )

    def stream_events(  # noqa: PLR0913 - the real client's keyword surface
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        on_settled: Callable[[StreamDisposition], object],
        responses: bool = False,
        max_output_tokens: int | None = None,
        text_format: Mapping[str, Any] | None = None,
        service_tier: str | None = None,
    ) -> LLMStreamHandle:
        """Open a stream whose provider chunks come from the next step."""
        if self.provider != "openai" or responses:
            msg = "a scripted stream is OpenAI chat-completion chunks"
            raise NotImplementedError(msg)
        step = self._script.answer(
            Request(
                "stream",
                copy.deepcopy(messages),
                system,
                copy.deepcopy(tools),
                None,
                service_tier,
                {
                    "max_output_tokens": max_output_tokens,
                    "text_format": copy.deepcopy(text_format),
                },
            )
        )
        return LLMStreamHandle(
            normalizer=StreamNormalizer("openai", f"scripted-{len(self.requests)}"),
            source=_chunks(step),
            on_settled=on_settled,
        )


class ScriptedRun(ScriptedModel):
    """One response run's request client: the script of its model, pinned to a frozen preset.

    A run reads its own last usage, as the real per-run client does, so two runs overlapping on
    one script never see each other's token counts.
    """

    def __init__(
        self,
        script: _Script,
        snapshot: LLMPresetSnapshot,
        response_id: str,
        native_tool_search: bool,  # noqa: FBT001 - copied from the model this run belongs to
    ) -> None:
        """Pin the shared ``script`` to ``snapshot``'s provider and model."""
        super().__init__(
            provider=snapshot.provider,
            model=snapshot.model,
            native_tool_search=native_tool_search,
        )
        self._script = script
        self.active_preset = snapshot.preset_name
        self._preset_snapshot = snapshot
        self._response_id = response_id

    @property
    def preset_snapshot(self) -> LLMPresetSnapshot:
        """The frozen preset identity this run is pinned to."""
        return self._preset_snapshot

    @property
    def response_id(self) -> str:
        """The response run that owns this client."""
        return self._response_id


class ScriptedSessions(LLMSessionFactory):
    """The session factory of a runtime whose every response run is answered by one model.

    The preset logic is the real factory's, so a snapshot names the provider, model and effort a
    turn would use; only the client a run is handed is the scripted one.
    """

    def __init__(self, model: ScriptedModel, llm_config: Mapping[str, Any]) -> None:
        """Hand every run a client of ``model``; ``llm_config`` is the ``llm:`` block."""
        super().__init__(llm_config)
        self._model = model

    def create(
        self,
        preset_snapshot: LLMPresetSnapshot,
        *,
        response_id: str,
        tracker: object | None = None,  # noqa: ARG002 - the real factory's signature
    ) -> LLMRequestClient:
        """A request client for one run, from the shared script."""
        return cast("LLMRequestClient", self._model.for_run(preset_snapshot, response_id))


async def _chunks(step: Reply | Failure) -> AsyncIterator[Mapping[str, Any]]:
    """The OpenAI chat-completion chunks of one scripted response, or its failure."""
    if isinstance(step, Failure):
        raise step.error
    base: dict[str, Any] = {"id": "scripted-response", "object": "chat.completion.chunk"}

    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    if step.text:
        yield chunk({"content": step.text})
    for index, call in enumerate(step.calls):
        function = {"name": call.name, "arguments": json.dumps(dict(call.arguments))}
        yield chunk(
            {
                "tool_calls": [
                    {
                        "index": index,
                        "id": call.call_id or f"call-{index}",
                        "type": "function",
                        "function": function,
                    }
                ]
            }
        )
    yield chunk({}, step.finish_reason)
    if step.usage:
        yield {
            **base,
            "choices": [],
            "usage": {
                "prompt_tokens": step.usage.input_tokens,
                "completion_tokens": step.usage.output_tokens,
                "prompt_tokens_details": {"cached_tokens": step.usage.cache_read_tokens},
            },
        }


@runtime_checkable
class RunClient(DecisionClient, Protocol):
    """What a response run reads of its own request client, as ``LLMRequestClient`` offers it."""

    @property
    def preset_snapshot(self) -> LLMPresetSnapshot:
        """The frozen preset identity the run is pinned to."""
        ...

    @property
    def response_id(self) -> str:
        """The response run that owns the client."""
        ...


if TYPE_CHECKING:
    # The real client and the stand-in both have the shape the decision layer reads.
    def _real_client_is_a_decision_client(client: LLMClient) -> DecisionClient:
        return client

    def _scripted_model_is_a_decision_client(model: ScriptedModel) -> DecisionClient:
        return model

    # And the real per-run client and the scripted one both have the shape a run reads.
    def _real_run_client_is_a_run_client(client: LLMRequestClient) -> RunClient:
        return client

    def _scripted_run_is_a_run_client(run: ScriptedRun) -> RunClient:
        return run
