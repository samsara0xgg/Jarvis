"""L3 Intent Router — Tier 0 scaffold + Tier 2 LLM bridge.

Per ADR 0001 § Stub strategy L3 Intent Router rows.

Day-1:

- **Tier 0** is an empty scaffold: :func:`tier_0_match` always returns
  ``None``. No deterministic regex shortcuts ship Day-1 per Allen's
  directive. The function exists so Stage 2's first deterministic
  pattern has a place to slot in without restructuring.
- **Tier 2** is a real LLM call. :func:`build_llm_messages` turns the
  current packet into an OpenAI/Anthropic-compatible message list; the
  composition root constructs the system prompt + tool list and feeds
  them into ``LLMClient.chat``.

This module imports :mod:`jarvis.decision.llm` (the LLM client), so it
is the boundary where the resolver's ``no-llm-in-resolver`` rule starts
to matter — keeping Tier 0 / Tier 2 in this file (separate from
``resolver.py``) makes canary H10's AST scan trivial.

Layer rules: stdlib + ``jarvis.shared`` + ``jarvis.decision.llm``. No
imports of sibling layers (``jarvis.execution`` / ``jarvis.surface`` /
``jarvis.deployment``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jarvis.decision.packet import SituationPacket


# --- Tier 0 (empty scaffold) ------------------------------------------------


def tier_0_match(packet: SituationPacket) -> None:
    """Return ``None`` always — Day-1 has no deterministic shortcuts.

    The signature is shaped like the eventual Stage 2 surface
    (``packet -> ToolCall | None``) but Day-1 deliberately ships
    nothing. Allen confirmed: no regex patterns Day-1.

    Args:
        packet: SituationPacket. Unused Day-1 but documented so the
            Stage 2 first-pattern PR has a place to read.

    Returns:
        Always ``None``.
    """
    del packet  # Stage 2 wiring placeholder — preserved as the matched arg name.


# --- Tier 2 LLM message bridge ---------------------------------------------


def build_llm_messages(
    packet: SituationPacket,
    *,
    utterance: str | None = None,
) -> list[dict[str, Any]]:
    """Build the ``messages`` argument for ``LLMClient.chat``.

    Day-1 messages list is minimal:

    - For a ``surface.user_intent`` trigger: one ``{"role":"user",
      "content": <utterance>}`` message.
    - For a re-entry (``worker.reported`` / ``action.result_observed``):
      one ``{"role":"user", "content": <summary>}`` describing the
      latest result so the LLM can plan next steps.

    The system prompt is supplied separately to ``LLMClient.chat`` as
    the ``system`` argument and is NOT included here.

    Args:
        packet: Current SituationPacket.
        utterance: Explicit user utterance text. When None and the
            trigger is ``surface.user_intent``, the function reads
            ``packet.trigger_event.payload["transcript"]``.

    Returns:
        A list of message dicts shaped for the LLM client.
    """
    if utterance is not None:
        return [{"role": "user", "content": utterance}]

    trigger = packet.trigger_event
    if trigger.type == "surface.user_intent":
        transcript = trigger.payload.get("transcript", "")
        return [{"role": "user", "content": str(transcript)}]

    if trigger.type == "worker.reported":
        summary = trigger.payload.get("summary", "worker reported a result")
        run_id = trigger.payload.get("run_id", "unknown")
        return [
            {
                "role": "user",
                "content": (
                    f"[system trigger] worker.reported for run_id={run_id}: {summary}. "
                    f"Proceed with verification per Allen's request "
                    f"('审核了再告诉我')."
                ),
            }
        ]

    if trigger.type == "action.result_observed":
        semantics = trigger.payload.get("semantics", "unknown")
        output = trigger.payload.get("tool_output", "")
        return [
            {
                "role": "user",
                "content": (
                    f"[system trigger] tool result observed (semantics={semantics}): "
                    f"{output}. Compose a final response for Allen, honoring the "
                    f"Pre-emit Gate."
                ),
            }
        ]

    return [
        {
            "role": "user",
            "content": (
                f"[system trigger] event type {trigger.type!r} re-entered L3 — "
                f"plan next step."
            ),
        }
    ]


def tool_definitions_for_llm(
    tools: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Convert ToolDefinition records into the Anthropic-style tool list.

    ``LLMClient.chat`` accepts the Anthropic ``input_schema`` shape and
    translates internally for OpenAI. The composition root passes
    ``tool_registry.for_caller(JARVIS_LLM)`` through this helper to
    avoid leaking the L4 ``ToolDefinition`` dataclass into L3.

    Args:
        tools: Iterable of dict-like objects with ``name`` /
            ``description`` / ``input_schema`` keys.

    Returns:
        A list of tool dicts. Day-1 is a near-passthrough.
    """
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": dict(tool["input_schema"]),
        }
        for tool in tools
    ]


__all__ = [
    "build_llm_messages",
    "tier_0_match",
    "tool_definitions_for_llm",
]
