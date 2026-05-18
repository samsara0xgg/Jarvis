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

import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

Provider = Literal["openai", "anthropic"]


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
    """

    text: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    raw: Mapping[str, Any]


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
        self._last_metadata["preset"] = self._active_preset
        self._last_metadata["model"] = self._model

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

    # ---- OpenAI backend -----------------------------------------------

    def _get_openai_client(self) -> Any:  # noqa: ANN401
        if self._openai_client is not None:
            return self._openai_client
        from openai import OpenAI  # noqa: PLC0415 — lazy SDK init (see module docstring)

        if not self._api_key:
            msg = "OpenAI API key is unset; check api_key_env in the active preset"
            raise MissingAPIKeyError(msg)
        # base_url=None is the SDK's "use the default OpenAI host" sentinel.
        self._openai_client = OpenAI(
            api_key=self._api_key,
            base_url=self._base_url or None,
        )
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
        )

    def _chat_stream_openai(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
    ) -> Iterator[ChatStreamChunk]:
        """Stream OpenAI chat completions. Unused Day-1; preserved skeleton."""
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

        self._last_metadata = _empty_metadata()
        self._last_metadata["provider"] = "openai"
        self._last_metadata["streaming"] = True
        self._last_metadata["preset"] = self._active_preset
        self._last_metadata["model"] = self._model

        finish_reason: str | None = None
        response = client.chat.completions.create(**kwargs)
        for chunk in response:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = choices[0].delta
            text = getattr(delta, "content", None) if delta is not None else None
            fr = getattr(choices[0], "finish_reason", None)
            if fr:
                finish_reason = fr
            if text:
                yield ChatStreamChunk(text=text, is_final=False, finish_reason=None)
        self._last_finish_reason = finish_reason
        yield ChatStreamChunk(text=None, is_final=True, finish_reason=finish_reason)

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
        response = client.messages.create(**kwargs)

        usage = getattr(response, "usage", None)
        self._last_metadata["provider"] = "anthropic"
        self._last_metadata["response_id"] = getattr(response, "id", None)
        self._last_metadata["streaming"] = False
        self._last_finish_reason = getattr(response, "stop_reason", None)
        self._last_input_tokens = getattr(usage, "input_tokens", None) if usage else None
        self._last_output_tokens = getattr(usage, "output_tokens", None) if usage else None

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
        )

    def _chat_stream_anthropic(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
    ) -> Iterator[ChatStreamChunk]:
        """Stream Anthropic messages. Unused Day-1; preserved skeleton."""
        client = self._get_anthropic_client()

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        self._last_metadata = _empty_metadata()
        self._last_metadata["provider"] = "anthropic"
        self._last_metadata["streaming"] = True
        self._last_metadata["preset"] = self._active_preset
        self._last_metadata["model"] = self._model

        finish_reason: str | None = None
        with client.messages.stream(**kwargs) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    text = getattr(delta, "text", None) if delta is not None else None
                    if text:
                        yield ChatStreamChunk(text=text, is_final=False, finish_reason=None)
                elif etype == "message_delta":
                    delta = getattr(event, "delta", None)
                    if delta is not None:
                        fr = getattr(delta, "stop_reason", None)
                        if fr:
                            finish_reason = fr
        self._last_finish_reason = finish_reason
        yield ChatStreamChunk(text=None, is_final=True, finish_reason=finish_reason)


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
    "load_llm_config",
]
