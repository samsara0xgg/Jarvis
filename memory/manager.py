"""MemoryManager — v3 memory interface for Jarvis.

Public surface:
  - build_prompt_context(text, user_id, history, ...) → PromptContext for the
    current turn (Assembler-driven 4-block layout, hot path).
  - get_last_prompt_context() → most recently assembled PromptContext, used by
    the runtime to read injected_observation_ids for trace v3.
  - write_observation(turn_data, source_turn_id) → cold-path Observer write
    into the observations table.

The v1 LLM-extraction / cosine-dedup / maintenance pipeline (save / query /
maintain / _rebuild_profile / etc.) was removed. Profile updates are now
manual; future plan: aggregate from high-priority observations.
"""

from __future__ import annotations

import logging

from memory.cold.observer import Observer
from memory.core.store import MemoryStore
from memory.hot.assembler import Assembler, PromptContext

LOGGER = logging.getLogger(__name__)


class MemoryManager:
    """Manages Jarvis's long-term memory — assemble prompt context and store observations.

    Args:
        config: Parsed application configuration.
    """

    def __init__(self, config: dict) -> None:
        mem_config = config.get("memory", {})
        db_path = mem_config.get("db_path", "data/memory/jarvis_memory.db")
        self.store = MemoryStore(db_path)
        self.logger = LOGGER

        self._observer = Observer(config)
        self._assembler = Assembler(self.store, self._profile_to_text)
        self._last_ctx: PromptContext | None = None

    # ------------------------------------------------------------------
    # Hot path — prompt context assembly
    # ------------------------------------------------------------------

    def build_prompt_context(
        self,
        text: str,
        user_id: str,
        history: list[dict],
        *,
        user_name: str | None = None,
        user_role: str = "guest",
        user_emotion: str = "",
        situation: str = "normal",
    ) -> PromptContext:
        """Build a :class:`PromptContext` for the current turn via the Assembler.

        Stores the result so :meth:`get_last_prompt_context` can return it to
        callers that need the injected observation ids (trace v3) without
        reaching into private state.
        """
        ctx = self._assembler.assemble(
            text=text,
            user_id=user_id,
            history=history,
            user_name=user_name,
            user_role=user_role,
            user_emotion=user_emotion,
            situation=situation,
        )
        self._last_ctx = ctx
        return ctx

    def get_last_prompt_context(self) -> PromptContext | None:
        """Return the most recent :class:`PromptContext` assembled, or None."""
        return self._last_ctx

    # ------------------------------------------------------------------
    # Cold path — observation write
    # ------------------------------------------------------------------

    def write_observation(
        self,
        turn_data: dict,
        source_turn_id: int | None = None,
    ) -> int:
        """Extract and store observations from a conversation turn.

        Args:
            turn_data: Dict with user_text, assistant_text, tool_calls, user_emotion.
            source_turn_id: The trace table row ID for this turn.

        Returns:
            Number of observations stored.
        """
        observations = self._observer.extract(turn_data)
        if not observations:
            return 0

        markdown = self._observer.format_markdown(observations)
        chunk_id = self.store.get_next_chunk_id()
        self.store.add_observation(
            chunk_id=chunk_id,
            content=markdown,
            source_turn_id=source_turn_id,
        )

        self.logger.info(
            "Stored %d observations (chunk %d) from turn %s",
            len(observations), chunk_id, source_turn_id,
        )
        return len(observations)

    # ------------------------------------------------------------------
    # Profile rendering — used by Assembler Block 2
    # ------------------------------------------------------------------

    def _profile_to_text(self, profile: dict | None) -> str:
        """Convert profile JSON to concise natural language.

        Returns an empty string when ``profile`` is ``None`` so the Assembler
        Block 2 branch can simply test the returned text for truthiness.
        """
        if not profile:
            return ""

        parts: list[str] = []

        identity = profile.get("identity", {})
        if identity:
            id_parts = []
            if identity.get("name"):
                id_parts.append(identity["name"])
            if identity.get("occupation"):
                id_parts.append(identity["occupation"])
            if identity.get("location"):
                id_parts.append(f"住{identity['location']}")
            if identity.get("traits"):
                id_parts.extend(identity["traits"])
            if id_parts:
                parts.append("，".join(id_parts) + "。")

        prefs = profile.get("preferences", {})
        if prefs.get("likes"):
            parts.append("喜欢：" + "、".join(prefs["likes"]) + "。")
        if prefs.get("dislikes"):
            parts.append("不喜欢：" + "、".join(prefs["dislikes"]) + "。")

        relationships = profile.get("relationships", {})
        if relationships:
            r_parts = [f"{k}：{v}" for k, v in relationships.items()]
            parts.append("关系：" + "；".join(r_parts) + "。")

        routines = profile.get("routines", {})
        if routines:
            r_parts = [f"{k}：{v}" for k, v in routines.items()]
            parts.append("习惯：" + "；".join(r_parts) + "。")

        status = profile.get("status")
        if status:
            parts.append(f"近况：{status}")

        return "\n".join(parts)
