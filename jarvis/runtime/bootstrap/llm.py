"""The model side of the boot: the words client, the think mode, the conversation setting."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.think_mode import ThinkMode, ThinkModeConfigError, load_think_mode
from jarvis.runtime.config import RuntimeBootstrapError, RuntimeConfig
from jarvis.shared.realtime import new_response_id

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.decision.llm import LLMClient
    from jarvis.runtime.settings import Settings


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
