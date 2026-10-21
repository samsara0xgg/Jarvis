"""The model side of the boot: the client, the session factory, the prompt, the response runs."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.response_run import ResponseRunRegistry
from jarvis.decision.think_mode import ThinkMode, ThinkModeConfigError, load_think_mode
from jarvis.runtime.config import RuntimeBootstrapError, RuntimeConfig
from jarvis.runtime.settings import REPLY_LINES
from jarvis.shared.realtime import new_response_id
from jarvis.state.committed_event_bus import CommittedEventBus

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.runtime.settings import Settings
    from jarvis.shared.realtime import Wave4ResponseFlags

LOGGER = logging.getLogger(__name__)


def _confirmation_words_client(config: RuntimeConfig) -> Callable[[], LLMClient] | None:
    """ADR 0321: the client that reads his words against a waiting card, one per reading.

    It runs on ``confirmation.words_preset`` with ``confirmation.words_timeout_s``: one attempt,
    no retry, so a slow provider leaves the card waiting for its button. A missing preset reads
    nothing: such words run as an ordinary turn.
    """
    name = config.confirmation.words_preset
    llm_block = config.raw.get("llm")
    presets = llm_block.get("presets") if isinstance(llm_block, Mapping) else None
    preset = presets.get(name) if isinstance(presets, Mapping) else None
    if not isinstance(preset, Mapping):
        LOGGER.warning("confirmation: llm.presets.%s missing; no words read against a card", name)
        return None
    factory = LLMSessionFactory(
        {
            "provider": "openai",
            "presets": {name: dict(preset)},
            "default_preset": name,
            "timeout_s": config.confirmation.words_timeout_s,
            "max_retries": 0,
        },
    )
    snapshot = factory.snapshot()
    return lambda: factory.create(snapshot, response_id=new_response_id())


def _think_mode(llm_config: Mapping[str, Any], config_path: Path) -> ThinkMode | None:
    """``llm.think`` (ADR 0108); a broken block stops boot like a broken tool-cue table."""
    try:
        return load_think_mode(llm_config)
    except ThinkModeConfigError as exc:
        msg = f"runtime: {config_path} {exc}"
        raise RuntimeBootstrapError(msg) from exc


def _follow_conversation_setting(settings: Settings | None, factory: LLMSessionFactory) -> None:
    """ADR 0297: the page's conversation model and effort reach the next turn at once."""
    if settings is None:
        return
    settings.on_conversation = factory.choose
    if chosen := settings.conversation_saved():
        factory.choose(*chosen)


@dataclass(frozen=True)
class Llm:
    """The model side of the runtime: how a turn reaches the model and what it starts from.

    Holds no connection: the SDK clients are built lazily, so a failure after this stage leaves
    nothing to undo.
    """

    client: LLMClient
    think_mode: ThinkMode | None
    session_factory: LLMSessionFactory
    response_flags: Wave4ResponseFlags
    response_runs: ResponseRunRegistry
    committed_event_bus: CommittedEventBus | None
    system_prompt: str


def build_llm(config: RuntimeConfig) -> Llm:
    """The L3 client over ``llm:``, the flag graph's consumers and the rendered system prompt.

    Lazy SDK construction: no network call until a turn invokes ``chat``. Plugin skills are
    sampled per turn, so connections need no daemon restart.
    """
    llm_config = config.raw.get("llm")
    if not isinstance(llm_config, Mapping):
        msg = f"runtime: {config.config_path} has no 'llm' section"
        raise RuntimeBootstrapError(msg)
    client = LLMClient(llm_config)
    think_mode = _think_mode(llm_config, config.config_path)
    # ADR-0008 Step 2 (Wave 4A). The whole flag graph was resolved once, when the config was
    # parsed; every downstream site reads the validated result. Every runtime gets the session
    # factory and the cancel registry (ADR 0332).
    response_flags = config.realtime.response.flags
    # The bus is gated on `realtime.enabled` rather than a Wave-4A switch so
    # Waves 4B/4C reuse this same line. It is inert while nobody subscribes.
    committed_event_bus = CommittedEventBus() if config.realtime.enabled else None
    session_factory = LLMSessionFactory(llm_config)
    response_runs = ResponseRunRegistry()
    system_prompt = config.prompt_path.read_text(encoding="utf-8").replace(
        "{assistant}", config.assistant_name
    )
    if config.reply_language in REPLY_LINES:
        reply_line = REPLY_LINES[config.reply_language]
        system_prompt = f"{system_prompt.rstrip()}\n\n{reply_line}\n"
    return Llm(
        client=client,
        think_mode=think_mode,
        session_factory=session_factory,
        response_flags=response_flags,
        response_runs=response_runs,
        committed_event_bus=committed_event_bus,
        system_prompt=system_prompt,
    )
