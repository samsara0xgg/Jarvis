"""L3 Intent Router — Tier 0 regex whitelist + Tier 2 LLM bridge.

Per ADR 0001 § Stub strategy L3 Intent Router rows.

- **Tier 0** is table-driven (spec §17): :func:`tier_0_match` runs the
  closed regex whitelist loaded from ``config/tier0_patterns.yaml``
  (see :mod:`jarvis.decision.tier0`) against the user's transcript. No
  table (the Day-1 state) means no shortcuts — every turn falls
  through to Tier 2.
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

from jarvis.decision.tier0 import match_tier0

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jarvis.decision.packet import SituationPacket
    from jarvis.decision.tier0 import Tier0Hit, Tier0Table


# --- Tier 0 (regex whitelist, spec §17) -------------------------------------


def tier_0_match(
    packet: SituationPacket,
    table: Tier0Table | None = None,
) -> Tier0Hit | None:
    """Spec §17 Tier 0: deterministic whitelist match on the transcript.

    Returns None when no table is loaded (Day-1 scaffold behavior),
    when the trigger is not a user utterance, or on a whitelist miss —
    the caller falls through to the Tier 2 LLM loop.
    """
    if not table:
        return None
    trigger = packet.trigger_event
    if trigger.type not in ("surface.user_intent", "utterance.received"):
        return None
    transcript = trigger.payload.get("transcript", "")
    if not isinstance(transcript, str):
        return None
    return match_tier0(transcript, table)


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
    - For a re-entry (``action.result_observed``):
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
    # ADR-0005 §5.1: voice/PTT path emits ``utterance.received`` carrying the
    # same ``transcript`` payload contract as ``surface.user_intent``; both
    # must reach the LLM as the user's actual words, not the system-trigger
    # fallback (which made wake turns answer "this is a system reentry, no
    # new task" instead of replying to what the user said).
    if trigger.type in ("surface.user_intent", "utterance.received"):
        transcript = trigger.payload.get("transcript", "")
        return [{"role": "user", "content": str(transcript)}]

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
        A list of tool dicts: each schema gains the required
        :data:`LEAD_IN_ARGUMENT` (ADR 0043), which L3 removes before the
        tool sees its arguments.
    """
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": _with_lead_in(tool["input_schema"]),
        }
        for tool in tools
    ]


LEAD_IN_ARGUMENT = "lead_in"
_LEAD_IN_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "动手前先念给用户的一句很短的话，说你这就去做什么，比如“我查一下维多利亚明天的天气”；"  # noqa: RUF001 — Chinese punctuation is intentional.
        "不说结果。用用户这一轮说话的语言。"
    ),
}


def _with_lead_in(schema: Mapping[str, Any]) -> dict[str, Any]:
    """``schema`` plus the required lead-in string every tool call carries."""
    required = [name for name in schema.get("required", ()) if name != LEAD_IN_ARGUMENT]
    return {
        **schema,
        "properties": {**schema.get("properties", {}), LEAD_IN_ARGUMENT: _LEAD_IN_PROPERTY},
        "required": [LEAD_IN_ARGUMENT, *required],
    }


__all__ = [
    "LEAD_IN_ARGUMENT",
    "build_llm_messages",
    "tier_0_match",
    "tool_definitions_for_llm",
]
