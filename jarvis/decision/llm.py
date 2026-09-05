"""L3 multi-provider LLM client (one round trip per call).

Adapted from legacy ``jarvis-legacy/core/llm.py`` (1650 LOC) per ADR
§ Reference sources. Day-1 keeps the provider switch (``openai`` /
``anthropic``), presets + :meth:`LLMClient.switch_model`, the metadata
accessors, and a preserved :meth:`LLMClient.chat_stream` skeleton.

Day-1 deviations from legacy:

* ``system: str`` replaces ``prompt_context`` (ADR Q3 option (a)).
* ``tracker: object | None = None`` — every tracker call gated.
* :meth:`chat` is ONE provider request, not a 10-iteration tool-use
  loop. Step 9's ``decide()`` orchestrates loops so the Pre-action
  Gate can inspect each tool call before execution.
* No personality / hot-memory / token-budget math / xAI cache
  headers / multimodal staging / retries. Stage 2 reintroduces.
* SDK construction is lazy (first :meth:`chat` call). Unit tests can
  construct an :class:`LLMClient` without touching the network.

Layer rules: stdlib + ``openai`` + ``anthropic`` + ``yaml``. No
imports from any ``jarvis.*`` sibling layer (siblings of L3 are
forbidden by ``.importlinter``).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import yaml

from jarvis.decision.llm_stream import LLMStreamHandle, StreamNormalizer
from jarvis.shared.realtime import LLMUsageStatus
from jarvis.shared.realtime_trace import record_realtime_trace

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
    from pathlib import Path

    from jarvis.decision.llm_stream import StreamDisposition

LOGGER = logging.getLogger(__name__)

Provider = Literal["openai", "anthropic"]
UsageStatus = LLMUsageStatus


def _new_llm_request_id() -> str:
    """Mint one request identity before provider I/O starts."""
    return "LLM" + uuid.uuid4().hex


def _usage_status(input_tokens: int | None, output_tokens: int | None) -> UsageStatus:
    if input_tokens is not None and output_tokens is not None:
        return "provider_final"
    if input_tokens is not None or output_tokens is not None:
        return "partial"
    return "unavailable"


def _iter_and_close(stream: Iterable[Any]) -> Iterator[Any]:
    """Consume a provider stream and close it on EOF, error, or cancellation."""
    try:
        yield from stream
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()


# --- Typed exceptions -------------------------------------------------------


class MissingLLMSectionError(KeyError):
    """Raised when :func:`load_llm_config` finds no ``llm:`` block."""


class UnknownPresetError(KeyError):
    """Raised by :meth:`LLMClient.switch_model` for unknown preset names."""


class MissingAPIKeyError(RuntimeError):
    """Raised when the configured ``api_key_env`` variable is unset."""


# --- Public data shapes -----------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One tool-use request emitted by the LLM.

    Attributes:
        call_id: Provider-supplied tool_call id; must be echoed in the
            tool-result message so the LLM matches results to requests.
        name: Tool name (matches a ``ToolDefinition.name``).
        arguments_json: Raw JSON string from the LLM. The caller
            (Step 9 ``decide()``) parses it; this client does not.
    """

    call_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class ChatResult:
    """Outcome of one :meth:`LLMClient.chat` call.

    ``text`` is non-None when the LLM produced text; ``tool_calls`` is
    non-empty when the LLM wants tools dispatched. Both can coexist.

    Attributes:
        text: Final assistant text, or ``None`` for pure tool-calls.
        tool_calls: Frozen tuple of :class:`ToolCall`.
        finish_reason: OpenAI ``"stop"``/``"tool_calls"``/``"length"``
            or Anthropic ``"end_turn"``/``"tool_use"``/``"max_tokens"``.
        input_tokens: Prompt tokens, or ``None``.
        output_tokens: Completion tokens, or ``None``.
        raw: Read-only provider raw response (for audit / debug).
        model_used: Concrete model id this turn ran on (e.g.
            ``"gpt-5.5"``). Mirrors ``LLMClient.model`` at call time so
            ``cost.recorded`` can attribute the spend per spec §5.4.1
            (ADR-0002 Step 3 § cost.recorded plumbing). Day-2 additive
            field; defaults to ``""`` so existing tests that hand-build
            a :class:`ChatResult` keep compiling.
        tokens_in: Non-None alias for :attr:`input_tokens` defaulted
            to ``0`` for cost arithmetic. The ``input_tokens | None``
            field stays for backwards compatibility; ``tokens_in`` is
            the field the L3 cost emitter reads. Equal to
            ``input_tokens or 0`` when populated by the SDK.
        tokens_out: Non-None alias for :attr:`output_tokens`, same
            rationale.
        cache_read_in: Prompt tokens served from the provider prompt
            cache (billed at the cache_read rate). Defaults to ``0``
            when the provider response omits the field.
        cache_write_in: Prompt tokens written into the provider prompt
            cache this turn (billed at the cache_write rate). Defaults
            to ``0`` when the provider response omits the field.
    """

    text: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    raw: Mapping[str, Any]
    model_used: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_in: int = 0
    cache_write_in: int = 0
    llm_request_id: str = field(default_factory=_new_llm_request_id)
    provider_response_id: str | None = None
    usage_status: UsageStatus = "unavailable"


@dataclass(frozen=True)
class ChatStreamChunk:
    """One chunk yielded by :meth:`LLMClient.chat_stream` (unused Day-1).

    Attributes:
        text: Text delta, or ``None``.
        is_final: ``True`` for the terminal chunk only.
        finish_reason: Set on the final chunk; ``None`` otherwise.
    """

    text: str | None
    is_final: bool
    finish_reason: str | None
    llm_request_id: str = ""
    provider_response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    usage_status: UsageStatus = "unavailable"


# --- Config loader ----------------------------------------------------------


def load_llm_config(path: Path) -> Mapping[str, Any]:
    """Load the ``llm:`` block from a YAML config file.

    Args:
        path: Path to a ``config/jarvis.yaml``-shaped file.

    Returns:
        The mapping under the ``"llm"`` key.

    Raises:
        MissingLLMSectionError: When ``llm:`` is absent or non-mapping.
    """
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    llm = raw.get("llm") if isinstance(raw, dict) else None
    if not isinstance(llm, dict):
        msg = f"config at {path} has no 'llm' section"
        raise MissingLLMSectionError(msg)
    return llm


# --- LLMClient --------------------------------------------------------------


def _empty_metadata() -> dict[str, Any]:
    """Return a fresh empty metadata dict (shape matches trace columns)."""
    return {
        "provider": None,
        "response_id": None,
        "preset": None,
        "model": None,
        "streaming": False,
        "llm_request_id": None,
        "usage_status": "unavailable",
        "cache_read_tokens": None,
        "cache_write_tokens": None,
    }


class LLMClient:
    """Multi-provider LLM client driving one chat turn at a time.

    Provider is selected by ``config["provider"]`` (``"openai"`` or
    ``"anthropic"``). Both backends share the same :meth:`chat`
    signature returning a frozen :class:`ChatResult`.

    Args:
        config: The ``llm:`` block from ``config/jarvis.yaml``.
        tracker: Optional health tracker. When non-None, the client
            calls ``tracker.record_success(component)`` /
            ``tracker.record_failure(component)``. Day-1 passes ``None``.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        tracker: object | None = None,
    ) -> None:
        """Construct the client from a parsed ``llm:`` config block."""
        # Defensive copy: caller-side mutations must not bleed in.
        cfg: dict[str, Any] = dict(config)

        provider_raw = str(cfg.get("provider", "openai")).strip().lower()
        if provider_raw not in ("openai", "anthropic"):
            msg = f"unsupported provider {provider_raw!r}; expected 'openai' or 'anthropic'"
            raise ValueError(msg)
        self._provider: Provider = provider_raw  # type: ignore[assignment]

        self._presets: dict[str, dict[str, Any]] = {
            name: dict(spec) for name, spec in (cfg.get("presets") or {}).items()
        }
        self._active_preset: str | None = None
        self._tracker = tracker

        # Provider-agnostic defaults; preset application overwrites.
        self._model: str = str(cfg.get("model", ""))
        self._base_url: str = ""
        self._max_tokens: int = int(cfg.get("max_tokens", 0) or 0)
        self._api_key: str | None = None

        # Transport-level knobs (MUST-FIX 2, ADR-0011 §12): opt-in, flat
        # top-level config only — no preset ever overrides these, so a
        # caller that wants a bounded client (e.g. `screen_look`'s vision
        # preset, via `jarvis.runtime._build_vision_client`) passes them
        # in its config dict, and every other `LLMClient` (the decision
        # loop's own) keeps the SDK's own default (unset -> not passed).
        raw_timeout = cfg.get("timeout_s")
        self._timeout_s: float | None = (
            float(raw_timeout)
            if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool)
            else None
        )
        raw_max_retries = cfg.get("max_retries")
        self._max_retries: int | None = (
            int(raw_max_retries)
            if isinstance(raw_max_retries, int) and not isinstance(raw_max_retries, bool)
            else None
        )

        # Last-call metadata. Reset on every chat() entry.
        self._last_metadata: dict[str, Any] = _empty_metadata()
        self._last_finish_reason: str | None = None
        self._last_input_tokens: int | None = None
        self._last_output_tokens: int | None = None

        # Lazy SDK handles, instantiated on first chat() / chat_stream().
        self._openai_client: Any | None = None
        self._anthropic_client: Any | None = None

        default_preset = cfg.get("default_preset")
        if default_preset and default_preset in self._presets:
            self._apply_preset(str(default_preset))
        elif self._provider == "openai":
            # Flat-config fallback (rare Day-1: jarvis.yaml ships presets).
            self._base_url = str(cfg.get("base_url", "") or "")
            api_key_env = cfg.get("api_key_env")
            if api_key_env:
                self._api_key = os.environ.get(str(api_key_env))
            else:
                self._api_key = os.environ.get("OPENAI_API_KEY")
        else:  # anthropic flat-config fallback
            self._base_url = str(cfg.get("base_url", "") or "")
            api_key_env = cfg.get("api_key_env")
            if api_key_env:
                self._api_key = os.environ.get(str(api_key_env))
            else:
                self._api_key = os.environ.get("ANTHROPIC_API_KEY")

    # ---- read-only public accessors (ADR § Acceptance G1) -------------

    @property
    def provider(self) -> Provider:
        """Active provider: ``"openai"`` or ``"anthropic"``."""
        return self._provider

    @property
    def model(self) -> str:
        """Active model id (e.g. ``"gpt-5.5"``)."""
        return self._model

    @property
    def base_url(self) -> str:
        """Active provider base URL. May be empty for native Anthropic."""
        return self._base_url

    @property
    def max_tokens(self) -> int:
        """Maximum output tokens for the next call."""
        return self._max_tokens

    @property
    def active_preset(self) -> str | None:
        """Name of the currently-active preset, or ``None``."""
        return self._active_preset

    @property
    def last_input_tokens(self) -> int | None:
        """Prompt tokens consumed by the most recent :meth:`chat` call."""
        return self._last_input_tokens

    @property
    def last_output_tokens(self) -> int | None:
        """Completion tokens generated by the most recent :meth:`chat` call."""
        return self._last_output_tokens

    @property
    def last_finish_reason(self) -> str | None:
        """Finish reason from the most recent :meth:`chat` call."""
        return self._last_finish_reason

    @property
    def last_llm_request_id(self) -> str | None:
        """Stable identity minted before the most recent provider request."""
        value = self._last_metadata.get("llm_request_id")
        return value if isinstance(value, str) else None

    @property
    def last_usage_status(self) -> UsageStatus:
        """Protocol-derived usage completeness for the latest request."""
        value = self._last_metadata.get("usage_status")
        if value in ("provider_final", "partial", "unavailable"):
            return value  # type: ignore[no-any-return]
        return "unavailable"

    @property
    def last_cache_read_tokens(self) -> int | None:
        """Provider-reported cache-read tokens for the latest request."""
        value = self._last_metadata.get("cache_read_tokens")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def last_cache_write_tokens(self) -> int | None:
        """Provider-reported cache-write tokens for the latest request."""
        value = self._last_metadata.get("cache_write_tokens")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def last_metadata(self) -> Mapping[str, Any]:
        """Read-only metadata for the most recent :meth:`chat` call.

        Keys: ``provider`` / ``response_id`` / ``preset`` / ``model`` /
        ``streaming``. All None / False before any call.
        """
        return dict(self._last_metadata)

    # ---- preset / model switching -------------------------------------

    def get_presets(self) -> Mapping[str, Mapping[str, Any]]:
        """Return a read-only view of registered model presets."""
        return {name: dict(spec) for name, spec in self._presets.items()}

    def switch_model(self, preset_name: str) -> str:
        """Apply a named preset; return the new ``model`` id.

        Raises:
            UnknownPresetError: When ``preset_name`` is not registered.
        """
        if preset_name not in self._presets:
            msg = f"unknown preset {preset_name!r}; available: {sorted(self._presets)}"
            raise UnknownPresetError(msg)
        self._apply_preset(preset_name)
        return self._model

    def _apply_preset(self, name: str) -> None:
        """Adopt a preset; reset lazy SDK handles so next chat() rebuilds them."""
        preset = self._presets[name]

        new_provider = preset.get("provider")
        if new_provider:
            provider_norm = str(new_provider).strip().lower()
            if provider_norm not in ("openai", "anthropic"):
                msg = f"unsupported provider {provider_norm!r} in preset {name!r}"
                raise ValueError(msg)
            self._provider = provider_norm  # type: ignore[assignment]

        self._model = str(preset.get("model", self._model))
        self._base_url = str(preset.get("base_url", "") or "")
        if "max_tokens" in preset:
            self._max_tokens = int(preset["max_tokens"])

        api_key_env = preset.get("api_key_env")
        if api_key_env:
            self._api_key = os.environ.get(str(api_key_env))
        # else: leave previously-resolved key in place (preset switching
        # within the same provider/account should not blow away the key
        # if the new preset doesn't name one explicitly).

        self._openai_client = None
        self._anthropic_client = None
        self._active_preset = name
        LOGGER.info(
            "Applied preset %r: provider=%s model=%s base_url=%s",
            name,
            self._provider,
            self._model,
            self._base_url,
        )

    # ---- chat (Day-1: one round trip, no tool loop) -------------------

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> ChatResult:
        """Send one chat turn and return the LLM's text or tool calls.

        Args:
            messages: Conversation history (``role`` / ``content`` plus
                optional tool fields). Callers own history because the
                Pre-action Gate must see each tool call before execution.
            system: Rendered system prompt string (per ADR Q3 (a)).
            tools: Tool definitions in Anthropic shape (``input_schema``
                key). Translated to OpenAI when provider is ``openai``.
            tool_choice: OpenAI tool_choice hint. Anthropic ignores.

        Returns:
            A frozen :class:`ChatResult`.
        """
        # Reset per-call metadata so stale values don't bleed across turns.
        self._last_metadata = _empty_metadata()
        self._last_finish_reason = None
        self._last_input_tokens = None
        self._last_output_tokens = None
        self._last_metadata["provider"] = self._provider
        self._last_metadata["preset"] = self._active_preset
        self._last_metadata["model"] = self._model
        self._last_metadata["llm_request_id"] = _new_llm_request_id()

        component = f"llm.{self._provider}"
        try:
            if self._provider == "openai":
                result = self._chat_openai(
                    messages=messages,
                    system=system,
                    tools=tools,
                    tool_choice=tool_choice,
                )
            else:
                result = self._chat_anthropic(
                    messages=messages,
                    system=system,
                    tools=tools,
                )
        except Exception:
            if self._tracker is not None:
                self._tracker.record_failure(component)  # type: ignore[attr-defined]
            raise
        if self._tracker is not None:
            self._tracker.record_success(component)  # type: ignore[attr-defined]
        return result

    def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[ChatStreamChunk]:
        """Stream a chat turn chunk-by-chunk. Unused Day-1.

        Yields :class:`ChatStreamChunk` instances; the final has
        ``is_final=True`` and a non-None ``finish_reason``.
        """
        if self._provider == "openai":
            return self._chat_stream_openai(messages=messages, system=system, tools=tools)
        return self._chat_stream_anthropic(messages=messages, system=system, tools=tools)

    def stream_events(
        self, *, messages: list[dict[str, Any]], system: str,
        tools: list[dict[str, Any]] | None = None,
        on_settled: Callable[[StreamDisposition], object],
    ) -> LLMStreamHandle:
        """Prepare an isolated typed stream; L3 must supply its cost settlement owner."""
        request_id = _new_llm_request_id()
        # Capture everything before returning the lazy source. Later preset or
        # caller-message mutations cannot redirect this request or its payload.
        options: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            options["base_url"] = self._base_url
        if self._timeout_s is not None:
            options["timeout"] = self._timeout_s
        if self._max_retries is not None:
            options["max_retries"] = self._max_retries
        body: dict[str, Any] = {"model": self._model, "stream": True}
        if self._provider == "openai":
            token_key = "max_completion_tokens" if self._model.startswith("gpt-5") else "max_tokens"
            body.update({
                token_key: self._max_tokens,
                "messages": [{"role": "system", "content": system}, *copy.deepcopy(messages)],
                "stream_options": {"include_usage": True},
            })
            if tools:
                body["tools"] = _tools_to_openai(copy.deepcopy(tools))
        else:
            body.update({
                "max_tokens": self._max_tokens, "system": system,
                "messages": copy.deepcopy(messages),
            })
            if tools:
                body["tools"] = copy.deepcopy(tools)
        return LLMStreamHandle(
            normalizer=StreamNormalizer(self._provider, request_id),
            source=self._typed_provider_events(self._provider, options, body),
            on_settled=on_settled,
        )

    @staticmethod
    async def _typed_provider_events(
        provider: Provider, options: dict[str, Any], body: dict[str, Any],
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Own and close one async SDK client/stream, including cancelled reads."""
        if not options.get("api_key"):
            message = "provider API key is unset; check the request preset"
            raise MissingAPIKeyError(message)
        record_realtime_trace(
            "llm_sdk_request_call_started_upper_bound", provider=provider,
            model=body["model"], streaming=True,
            measurement_semantics="immediately_before_sdk_call_not_network_send",
        )
        if provider == "openai":
            from openai import AsyncOpenAI  # noqa: PLC0415 — lazy provider construction

            async with AsyncOpenAI(**options) as client:
                response = await client.chat.completions.create(**body)
                async with response:
                    async for chunk in response:
                        yield chunk.model_dump(exclude_none=True)
        else:
            from anthropic import AsyncAnthropic  # noqa: PLC0415 — lazy provider construction

            async with AsyncAnthropic(**options) as anthropic_client:
                response = await anthropic_client.messages.create(**body)
                async with response:
                    async for chunk in response:
                        yield chunk.model_dump(exclude_none=True)

    # ---- fresh-context contextmanager (ADR-0002 Step 9) ---------------

    @contextmanager
    def fresh_context(self) -> Iterator[LLMClient]:
        """Yield a view of self that does NOT carry parent session history.

        Day-2 reviewer use case (ADR-0002 § Reviewer contract): when L3's
        Result Interpreter calls :func:`jarvis.decision.reviewer.review_diff`,
        the reviewer's chat call must NOT see the decision LLM's prior
        turns; the reviewer evaluates the diff against the goal in
        isolation (a Report-grade verdict per spec §8.5 rule 1).
        ``test_canary_reviewer_fresh_context`` enforces that every
        reviewer ``.chat(...)`` call site is wrapped in
        ``with llm_client.fresh_context() as fresh:``.
        Why this is currently a structural no-op:
        :meth:`chat` is fully stateless — the caller owns the ``messages``
        list, the system prompt is passed per-call, and ``LLMClient`` keeps
        zero conversation state across :meth:`chat` invocations (only
        per-call ``_last_*`` metadata is stored, and that is reset at the
        top of every :meth:`chat` call before any provider work). So a
        downstream chat inside this contextmanager already CANNOT see any
        earlier turn's content. The contextmanager is the architectural
        marker the canary enforces; should :meth:`chat` ever gain
        cross-call conversation state (e.g. cached message history),
        this is the single place to clear and restore it.
        Yields ``self`` directly: callers may invoke any method on the
        yielded client identically to the parent. The contract is
        "fresh", not "different object".
        """
        yield self

    # ---- OpenAI backend -----------------------------------------------

    def _get_openai_client(self) -> Any:  # noqa: ANN401
        if self._openai_client is not None:
            return self._openai_client
        from openai import OpenAI  # noqa: PLC0415 — lazy SDK init (see module docstring)

        if not self._api_key:
            msg = "OpenAI API key is unset; check api_key_env in the active preset"
            raise MissingAPIKeyError(msg)
        # base_url=None is the SDK's "use the default OpenAI host" sentinel.
        # `timeout`/`max_retries` are omitted entirely (SDK defaults apply)
        # unless the config set `timeout_s`/`max_retries` explicitly
        # (MUST-FIX 2, ADR-0011 §12) — today only the vision preset's
        # dedicated client does.
        client_kwargs: dict[str, Any] = {
            "api_key": self._api_key,
            "base_url": self._base_url or None,
        }
        if self._timeout_s is not None:
            client_kwargs["timeout"] = self._timeout_s
        if self._max_retries is not None:
            client_kwargs["max_retries"] = self._max_retries
        self._openai_client = OpenAI(**client_kwargs)
        return self._openai_client

    def _chat_openai(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
    ) -> ChatResult:
        client = self._get_openai_client()

        oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        oai_messages.extend(messages)

        openai_tools = _tools_to_openai(tools) if tools else None

        # gpt-5 family uses max_completion_tokens; older models use max_tokens.
        tok_key = "max_completion_tokens" if self._model.startswith("gpt-5") else "max_tokens"
        kwargs: dict[str, Any] = {
            "model": self._model,
            tok_key: self._max_tokens,
            "messages": oai_messages,
        }
        if openai_tools:
            kwargs["tools"] = openai_tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice

        LOGGER.info("Sending request to OpenAI (model=%s base=%s)", self._model, self._base_url)
        record_realtime_trace(
            "llm_sdk_request_call_started_upper_bound",
            provider="openai",
            model=self._model,
            streaming=False,
            measurement_semantics="immediately_before_sdk_call_not_network_send",
        )
        response = client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        assistant_msg = choice.message
        usage = getattr(response, "usage", None)

        self._last_metadata["provider"] = "openai"
        self._last_metadata["response_id"] = getattr(response, "id", None)
        self._last_metadata["streaming"] = False
        self._last_finish_reason = getattr(choice, "finish_reason", None)
        self._last_input_tokens = getattr(usage, "prompt_tokens", None) if usage else None
        self._last_output_tokens = getattr(usage, "completion_tokens", None) if usage else None
        self._last_metadata["usage_status"] = _usage_status(
            self._last_input_tokens,
            self._last_output_tokens,
        )

        # ADR-0002 Step 3: surface cache token counts on ChatResult so
        # the L3 cost emitter can bill at cache rates. OpenAI/OpenRouter
        # expose cached prompt tokens via usage.prompt_tokens_details
        # (.cached_tokens). chat completions does not expose a per-turn
        # cache-write count, so cache_write_in stays 0 here.
        cache_read_in = 0
        if usage is not None:
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cached = getattr(details, "cached_tokens", None)
                if cached is not None:
                    cache_read_in = int(cached)
        self._last_metadata["cache_read_tokens"] = cache_read_in if usage is not None else None
        self._last_metadata["cache_write_tokens"] = 0 if usage is not None else None

        text_part = (assistant_msg.content or "").strip() or None
        tool_calls: tuple[ToolCall, ...] = ()
        if assistant_msg.tool_calls:
            tool_calls = tuple(
                ToolCall(
                    call_id=tc.id,
                    name=tc.function.name,
                    arguments_json=tc.function.arguments or "{}",
                )
                for tc in assistant_msg.tool_calls
            )

        raw = _safe_model_dump(response)
        return ChatResult(
            text=text_part,
            tool_calls=tool_calls,
            finish_reason=self._last_finish_reason,
            input_tokens=self._last_input_tokens,
            output_tokens=self._last_output_tokens,
            raw=raw,
            model_used=self._model,
            tokens_in=self._last_input_tokens or 0,
            tokens_out=self._last_output_tokens or 0,
            cache_read_in=cache_read_in,
            cache_write_in=0,
            llm_request_id=str(self._last_metadata["llm_request_id"]),
            provider_response_id=getattr(response, "id", None),
            usage_status=self.last_usage_status,
        )

    def _chat_stream_openai(  # noqa: C901, PLR0915 - provider protocol normalization
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
    ) -> Iterator[ChatStreamChunk]:
        """Stream OpenAI chat completions. Unused Day-1; preserved skeleton."""
        self._last_metadata = _empty_metadata()
        self._last_metadata["provider"] = "openai"
        self._last_metadata["streaming"] = True
        self._last_metadata["preset"] = self._active_preset
        self._last_metadata["model"] = self._model
        self._last_metadata["llm_request_id"] = _new_llm_request_id()
        self._last_finish_reason = None
        self._last_input_tokens = None
        self._last_output_tokens = None

        client = self._get_openai_client()
        oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        oai_messages.extend(messages)
        openai_tools = _tools_to_openai(tools) if tools else None

        tok_key = "max_completion_tokens" if self._model.startswith("gpt-5") else "max_tokens"
        kwargs: dict[str, Any] = {
            "model": self._model,
            tok_key: self._max_tokens,
            "messages": oai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if openai_tools:
            kwargs["tools"] = openai_tools

        finish_reason: str | None = None
        record_realtime_trace(
            "llm_sdk_request_call_started_upper_bound",
            provider="openai",
            model=self._model,
            streaming=True,
            measurement_semantics="immediately_before_sdk_call_not_network_send",
        )
        response = client.chat.completions.create(**kwargs)
        first_text_delta = True
        for chunk in _iter_and_close(response):
            response_id = getattr(chunk, "id", None)
            if response_id:
                self._last_metadata["response_id"] = response_id
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                prompt_tokens = getattr(usage, "prompt_tokens", None)
                completion_tokens = getattr(usage, "completion_tokens", None)
                if prompt_tokens is not None:
                    self._last_input_tokens = int(prompt_tokens)
                if completion_tokens is not None:
                    self._last_output_tokens = int(completion_tokens)
                details = getattr(usage, "prompt_tokens_details", None)
                cached = getattr(details, "cached_tokens", None) if details is not None else None
                self._last_metadata["cache_read_tokens"] = int(cached) if cached is not None else 0
                self._last_metadata["cache_write_tokens"] = 0
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                # OpenAI-compatible providers put final usage on an
                # empty-choices terminal chunk.  Usage was consumed above;
                # only text/tool parsing is skipped here.
                continue
            delta = choices[0].delta
            text = getattr(delta, "content", None) if delta is not None else None
            fr = getattr(choices[0], "finish_reason", None)
            if fr:
                finish_reason = fr
            if text:
                if first_text_delta:
                    first_text_delta = False
                    record_realtime_trace(
                        "llm_provider_first_text_delta",
                        provider="openai",
                        model=self._model,
                        measurement_semantics="first_nonempty_sdk_stream_delta",
                    )
                yield ChatStreamChunk(
                    text=text,
                    is_final=False,
                    finish_reason=None,
                    llm_request_id=str(self._last_metadata["llm_request_id"]),
                    provider_response_id=(
                        str(self._last_metadata["response_id"])
                        if self._last_metadata["response_id"] is not None
                        else None
                    ),
                )
        self._last_finish_reason = finish_reason
        self._last_metadata["usage_status"] = _usage_status(
            self._last_input_tokens,
            self._last_output_tokens,
        )
        yield ChatStreamChunk(
            text=None,
            is_final=True,
            finish_reason=finish_reason,
            llm_request_id=str(self._last_metadata["llm_request_id"]),
            provider_response_id=(
                str(self._last_metadata["response_id"])
                if self._last_metadata["response_id"] is not None
                else None
            ),
            input_tokens=self._last_input_tokens,
            output_tokens=self._last_output_tokens,
            cache_read_tokens=self.last_cache_read_tokens,
            cache_write_tokens=self.last_cache_write_tokens,
            usage_status=self.last_usage_status,
        )

    # ---- Anthropic backend --------------------------------------------

    def _get_anthropic_client(self) -> Any:  # noqa: ANN401
        if self._anthropic_client is not None:
            return self._anthropic_client
        import anthropic  # noqa: PLC0415 — lazy SDK init (see module docstring)

        if not self._api_key:
            msg = "Anthropic API key is unset; check api_key_env in the active preset"
            raise MissingAPIKeyError(msg)
        self._anthropic_client = anthropic.Anthropic(api_key=self._api_key)
        return self._anthropic_client

    def _chat_anthropic(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
    ) -> ChatResult:
        client = self._get_anthropic_client()

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        LOGGER.info("Sending request to Anthropic (model=%s)", self._model)
        record_realtime_trace(
            "llm_sdk_request_call_started_upper_bound",
            provider="anthropic",
            model=self._model,
            streaming=False,
            measurement_semantics="immediately_before_sdk_call_not_network_send",
        )
        response = client.messages.create(**kwargs)

        usage = getattr(response, "usage", None)
        self._last_metadata["provider"] = "anthropic"
        self._last_metadata["response_id"] = getattr(response, "id", None)
        self._last_metadata["streaming"] = False
        self._last_finish_reason = getattr(response, "stop_reason", None)
        self._last_input_tokens = getattr(usage, "input_tokens", None) if usage else None
        self._last_output_tokens = getattr(usage, "output_tokens", None) if usage else None
        self._last_metadata["usage_status"] = _usage_status(
            self._last_input_tokens,
            self._last_output_tokens,
        )

        # ADR-0002 Step 3: Anthropic exposes both cache_read and
        # cache_creation token counts on usage. Surface both on
        # ChatResult so the L3 cost emitter bills at the correct rate.
        cache_read_in_anth = 0
        cache_write_in_anth = 0
        if usage is not None:
            cr = getattr(usage, "cache_read_input_tokens", None)
            if cr is not None:
                cache_read_in_anth = int(cr)
            cw = getattr(usage, "cache_creation_input_tokens", None)
            if cw is not None:
                cache_write_in_anth = int(cw)
        self._last_metadata["cache_read_tokens"] = cache_read_in_anth if usage is not None else None
        self._last_metadata["cache_write_tokens"] = (
            cache_write_in_anth if usage is not None else None
        )

        text_parts: list[str] = []
        tool_calls_list: list[ToolCall] = []
        for block in getattr(response, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif btype == "tool_use":
                # Anthropic returns parsed dict input; serialise back to JSON
                # so ToolCall.arguments_json's contract holds.
                raw_input = getattr(block, "input", None) or {}
                try:
                    arguments_json = json.dumps(raw_input, ensure_ascii=False)
                except (TypeError, ValueError):
                    arguments_json = "{}"
                tool_calls_list.append(
                    ToolCall(
                        call_id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        arguments_json=arguments_json,
                    )
                )

        text_joined = "".join(text_parts).strip() or None
        raw = _safe_model_dump(response)
        return ChatResult(
            text=text_joined,
            tool_calls=tuple(tool_calls_list),
            finish_reason=self._last_finish_reason,
            input_tokens=self._last_input_tokens,
            output_tokens=self._last_output_tokens,
            raw=raw,
            model_used=self._model,
            tokens_in=self._last_input_tokens or 0,
            tokens_out=self._last_output_tokens or 0,
            cache_read_in=cache_read_in_anth,
            cache_write_in=cache_write_in_anth,
            llm_request_id=str(self._last_metadata["llm_request_id"]),
            provider_response_id=getattr(response, "id", None),
            usage_status=self.last_usage_status,
        )

    def _chat_stream_anthropic(  # noqa: C901, PLR0912, PLR0915 - provider protocol normalization
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
    ) -> Iterator[ChatStreamChunk]:
        """Stream Anthropic messages. Unused Day-1; preserved skeleton."""
        self._last_metadata = _empty_metadata()
        self._last_metadata["provider"] = "anthropic"
        self._last_metadata["streaming"] = True
        self._last_metadata["preset"] = self._active_preset
        self._last_metadata["model"] = self._model
        self._last_metadata["llm_request_id"] = _new_llm_request_id()
        self._last_finish_reason = None
        self._last_input_tokens = None
        self._last_output_tokens = None

        client = self._get_anthropic_client()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        finish_reason: str | None = None
        first_text_delta = True
        record_realtime_trace(
            "llm_sdk_request_call_started_upper_bound",
            provider="anthropic",
            model=self._model,
            streaming=True,
            measurement_semantics="immediately_before_sdk_call_not_network_send",
        )
        with client.messages.stream(**kwargs) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "message_start":
                    message = getattr(event, "message", None)
                    response_id = getattr(message, "id", None)
                    if response_id:
                        self._last_metadata["response_id"] = response_id
                    usage = getattr(message, "usage", None)
                    if usage is not None:
                        input_tokens = getattr(usage, "input_tokens", None)
                        if input_tokens is not None:
                            self._last_input_tokens = int(input_tokens)
                        cache_read = getattr(usage, "cache_read_input_tokens", None)
                        cache_write = getattr(usage, "cache_creation_input_tokens", None)
                        self._last_metadata["cache_read_tokens"] = (
                            int(cache_read) if cache_read is not None else 0
                        )
                        self._last_metadata["cache_write_tokens"] = (
                            int(cache_write) if cache_write is not None else 0
                        )
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    text = getattr(delta, "text", None) if delta is not None else None
                    if text:
                        if first_text_delta:
                            first_text_delta = False
                            record_realtime_trace(
                                "llm_provider_first_text_delta",
                                provider="anthropic",
                                model=self._model,
                                measurement_semantics="first_nonempty_sdk_stream_delta",
                            )
                        yield ChatStreamChunk(
                            text=text,
                            is_final=False,
                            finish_reason=None,
                            llm_request_id=str(self._last_metadata["llm_request_id"]),
                            provider_response_id=(
                                str(self._last_metadata["response_id"])
                                if self._last_metadata["response_id"] is not None
                                else None
                            ),
                        )
                elif etype == "message_delta":
                    delta = getattr(event, "delta", None)
                    if delta is not None:
                        fr = getattr(delta, "stop_reason", None)
                        if fr:
                            finish_reason = fr
                    usage = getattr(event, "usage", None)
                    if usage is not None:
                        output_tokens = getattr(usage, "output_tokens", None)
                        if output_tokens is not None:
                            self._last_output_tokens = int(output_tokens)

            # Some Anthropic SDK versions expose the authoritative final
            # message only after protocol EOF.  Consume it while the stream
            # context is still alive so final usage cannot be dropped.
            get_final_message = getattr(stream, "get_final_message", None)
            if callable(get_final_message):
                final_message = get_final_message()
                response_id = getattr(final_message, "id", None)
                if response_id:
                    self._last_metadata["response_id"] = response_id
                final_reason = getattr(final_message, "stop_reason", None)
                if final_reason:
                    finish_reason = final_reason
                final_usage = getattr(final_message, "usage", None)
                if final_usage is not None:
                    input_tokens = getattr(final_usage, "input_tokens", None)
                    output_tokens = getattr(final_usage, "output_tokens", None)
                    if input_tokens is not None:
                        self._last_input_tokens = int(input_tokens)
                    if output_tokens is not None:
                        self._last_output_tokens = int(output_tokens)
                    cache_read = getattr(final_usage, "cache_read_input_tokens", None)
                    cache_write = getattr(final_usage, "cache_creation_input_tokens", None)
                    self._last_metadata["cache_read_tokens"] = (
                        int(cache_read) if cache_read is not None else 0
                    )
                    self._last_metadata["cache_write_tokens"] = (
                        int(cache_write) if cache_write is not None else 0
                    )
        self._last_finish_reason = finish_reason
        self._last_metadata["usage_status"] = _usage_status(
            self._last_input_tokens,
            self._last_output_tokens,
        )
        yield ChatStreamChunk(
            text=None,
            is_final=True,
            finish_reason=finish_reason,
            llm_request_id=str(self._last_metadata["llm_request_id"]),
            provider_response_id=(
                str(self._last_metadata["response_id"])
                if self._last_metadata["response_id"] is not None
                else None
            ),
            input_tokens=self._last_input_tokens,
            output_tokens=self._last_output_tokens,
            cache_read_tokens=self.last_cache_read_tokens,
            cache_write_tokens=self.last_cache_write_tokens,
            usage_status=self.last_usage_status,
        )


# --- private helpers --------------------------------------------------------


def _tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic-style tool definitions to OpenAI function format."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get(
                    "input_schema",
                    {"type": "object", "properties": {}},
                ),
            },
        }
        for tool in tools
    ]


def _safe_model_dump(obj: object) -> Mapping[str, Any]:
    """Coerce a provider SDK response into a plain dict (best-effort).

    Both ``openai`` and ``anthropic`` responses are pydantic models with
    ``model_dump()``; we fall back to ``vars()`` then ``{}``.
    """
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            return data
    try:
        return dict(vars(obj))
    except TypeError:
        return {}


__all__ = [
    "ChatResult",
    "ChatStreamChunk",
    "LLMClient",
    "MissingAPIKeyError",
    "MissingLLMSectionError",
    "Provider",
    "ToolCall",
    "UnknownPresetError",
    "UsageStatus",
    "load_llm_config",
]
