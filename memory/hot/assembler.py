"""Prompt Assembler — builds the 4-block system prompt for every cloud LLM turn.

The Assembler is the hot-path (ms level) read over the MemoryStore. It walks
Mastra OM's observation corpus plus the user's profile and assembles a
:class:`PromptContext` that knows how to serialize for both Anthropic
(``list[dict]`` with ``cache_control``) and OpenAI-compatible (single string,
relying on prefix stability + sticky routing for cache hits) backends.

Block layout (plan/2026-04-21-memory-v2-finish.md "Target Prompt Structure"):

    Block 1 · identity          — cache=True   personality + output_rules
    Block 2 · core_profile      — cache=True   ``[关于用户]`` summary
    Block 3 · observations      — cache=True   full non-superseded OM dump
    Block 4 · situation         — cache=False  time / emotion / user-status

Block 4 is deliberately not cached so mid-conversation changes (emotion flip,
situation escalation, voiceprint swap) never invalidate the upstream three
blocks. Anthropic allows at most 4 ``cache_control`` breakpoints; the layout
here uses 3, leaving 1 slack for ``tools``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class _StoreLike(Protocol):
    """Minimal contract the Assembler needs from the MemoryStore."""

    def get_profile(self, user_id: str) -> dict[str, Any] | None: ...
    def get_all_observations(self) -> list[dict[str, Any]]: ...


_PROFILE_HEADER = "[关于用户]"

_OBSERVATIONS_PREAMBLE = (
    "The following observations are your memory of past conversations "
    "with the user. Newer observations supersede older ones. "
    "Reference specific details when relevant."
)


@dataclass
class PromptBlock:
    """A single system-prompt section.

    Attributes:
        content: Raw text for the block. Must already include any wrapping
            tags the LLM expects (``<personality>``, ``<situation>``, ...).
        cache: When True, Anthropic serialization attaches
            ``cache_control={"type": "ephemeral"}``. OpenAI-compatible
            serialization ignores this flag and relies on prefix stability.
        name: Debug / routing tag (e.g. ``"identity"``, ``"observations"``).
    """

    content: str
    cache: bool = False
    name: str = ""


@dataclass
class PromptContext:
    """Structured output of an Assembler run.

    Carries the system blocks, the messages array, and the explicit list of
    observation ids that landed in Block 3. Downstream trace v3 logging reads
    ``injected_observation_ids`` to record exactly which OM rows the LLM was
    given this turn (distinct from hallucinated ``<cited_obs>`` mentions).
    """

    blocks: list[PromptBlock]
    messages: list[dict]
    injected_observation_ids: list[int] = field(default_factory=list)

    def to_anthropic_system(self) -> list[dict]:
        """Serialize blocks into Anthropic's ``system=list[dict]`` shape.

        Each block becomes ``{"type": "text", "text": ..., [cache_control: ...]}``
        preserving the ``cache`` flag on each block.
        """
        out: list[dict] = []
        for b in self.blocks:
            entry: dict[str, Any] = {"type": "text", "text": b.content}
            if b.cache:
                entry["cache_control"] = {"type": "ephemeral"}
            out.append(entry)
        return out

    def to_openai_system_str(self) -> str:
        """Serialize blocks into a single system string (OpenAI / xAI).

        OpenAI-compatible backends have no explicit per-block cache control;
        they cache via prefix stability. Joining with a blank line keeps the
        boundary stable for the prefix-hash algorithm.
        """
        return "\n\n".join(b.content for b in self.blocks)


class Assembler:
    """Stateless assembler — one instance wraps the store + profile renderer.

    Stateless means every :meth:`assemble` call returns an independent
    :class:`PromptContext`; the Assembler keeps no per-turn scratch so two
    concurrent turns (e.g. voice + web) cannot see each other's data.
    """

    def __init__(
        self,
        store: _StoreLike,
        profile_fn: Callable[[dict | None], str],
    ) -> None:
        self._store = store
        self._profile_fn = profile_fn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def assemble(
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
        """Build a fresh :class:`PromptContext` for the current turn.

        Args:
            text: Current user utterance (appended as the last message).
            user_id: Authenticated user id for profile / observation lookup.
            history: Prior conversation turns; copied into ``messages`` so
                mutations on the returned context cannot corrupt the caller.
            user_name: Speaker name (voiceprint); ``None`` → guest branch.
            user_role: Authenticated role; passed through to block builders.
            user_emotion: SenseVoice emotion tag, empty string omits it.
            situation: One of ``normal`` / ``urgent`` / ``error`` / ``rapid``.

        Returns:
            :class:`PromptContext` with 3–4 blocks (Block 2 is skipped when
            no profile is available) plus a full ``messages`` list.
        """
        block1 = self._block1_identity(user_role)
        block2 = self._block2_profile(user_id)
        block3, injected_ids = self._block3_observations()
        block4 = self._block4_situation(
            user_name=user_name,
            user_role=user_role,
            user_emotion=user_emotion,
            situation=situation,
        )

        blocks = [b for b in (block1, block2, block3, block4) if b is not None]
        messages = list(history) + [{"role": "user", "content": text}]
        return PromptContext(
            blocks=blocks,
            messages=messages,
            injected_observation_ids=injected_ids,
        )

    # ------------------------------------------------------------------
    # Block builders
    # ------------------------------------------------------------------

    def _block1_identity(self, user_role: str) -> PromptBlock:
        # Local import avoids a cycle: core.personality → memory.hot
        # would reintroduce the prompt-mangling loop v1 had.
        from core.personality import build_identity_block

        return PromptBlock(
            content=build_identity_block(user_role=user_role),
            cache=True,
            name="identity",
        )

    def _block2_profile(self, user_id: str) -> PromptBlock | None:
        profile = self._store.get_profile(user_id)
        text = self._profile_fn(profile)
        if not text:
            return None
        return PromptBlock(
            content=f"{_PROFILE_HEADER}\n{text}",
            cache=True,
            name="profile",
        )

    def _block3_observations(self) -> tuple[PromptBlock, list[int]]:
        observations = self._store.get_all_observations()
        lines: list[str] = []
        ids: list[int] = []
        for o in observations:
            content = o.get("content")
            oid = o.get("id")
            if not content or oid is None:
                continue
            lines.append(f"{oid}. {content}")
            ids.append(oid)
        obs_body = "\n".join(lines)
        block_text = (
            f"{_OBSERVATIONS_PREAMBLE}\n\n"
            f"<observations>\n{obs_body}\n</observations>"
        )
        return (
            PromptBlock(content=block_text, cache=True, name="observations"),
            ids,
        )

    def _block4_situation(
        self,
        *,
        user_name: str | None,
        user_role: str,
        user_emotion: str,
        situation: str,
    ) -> PromptBlock:
        from core.personality import build_situation_block

        return PromptBlock(
            content=build_situation_block(
                user_name=user_name,
                user_role=user_role,
                user_emotion=user_emotion,
                situation=situation,
            ),
            cache=False,
            name="situation",
        )
