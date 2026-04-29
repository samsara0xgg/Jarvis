"""v4 SCHEMA — 2-branch discriminated anyOf with label_kind tag.

Branches:
  - ToolIntent  (label_kind="tool"):   intent in 9 actionable, defer=null
  - DeferIntent (label_kind="defer"):  intent=null, defer in 5 reasons, no tools/spans

v3 -> v4 changes:
  - Drop greeting branch (greeting -> defer)
  - Drop simple_weather intent (open slot, defer to L2)
  - Drop reasoning_chain / ambiguity_signals / slot_alternatives fields
  - Defer reasons 7 -> 5: merge context_continuation + memory_dependent + implicit_temporal
    into needs_history; rename ambiguous_slot -> ambiguous (covers free_text_payload too)
  - Tool branch intent enum: 9 (was 10)
  - Top-level fields: 8 (was 11)
"""
from __future__ import annotations

# Tool names (10) - drops get_weather from v3
TOOL_NAMES = [
    "control_device", "get_current_time", "get_date",
    "cc_slash", "cc_interrupt", "cc_show", "cc_tell",
    "list_query", "obsidian_add_to_inbox", "type_to_focused",
]

# Intent values for tool branch (9) - drops greeting and simple_weather
TOOL_INTENTS = [
    "control_device", "get_current_time", "get_date",
    "cc_slash", "cc_interrupt", "cc_message",
    "note_capture", "text_input", "list_query",
]

ACTION_VALUES = [
    "turn_on", "turn_off", "toggle", "set", "adjust", "query", "exec", "chat",
]

# Defer reasons (5) - merged history class, simplified ambiguous
DEFER_VALUES = [
    "out_of_scope",   # 9 intents don't fit (knowledge / news / subjective / capability meta)
    "needs_history",  # context / memory / implicit_temporal merged
    "multi_intent",   # parallel multi-action
    "tool_chaining",  # serial tool dependency
    "ambiguous",      # slot missing / intent unclear / non-strict-trigger payload
]

SLOT_VALUES = [
    "device", "value", "attribute", "date",
    "slash_command", "slash_arg", "list_target", "content",
]


def _span_item():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "slot": {"type": "string", "enum": SLOT_VALUES},
            "text": {"type": "string"},
            "normalized": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "number"},
                    {"type": "boolean"},
                    {"type": "null"},
                ]
            },
            "normalized_enum": {
                "anyOf": [{"type": "string"}, {"type": "null"}]
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["slot", "text", "normalized", "normalized_enum", "confidence"],
    }


def _tool_call_item():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "enum": TOOL_NAMES},
            "args_json": {"type": "string"},
        },
        "required": ["name", "args_json"],
    }


def _alt_tool_item():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tool": {
                "anyOf": [
                    {"type": "string", "enum": TOOL_NAMES},
                    {"type": "null"},
                ]
            },
            "prob": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["tool", "prob"],
    }


# v4 dropped: reasoning_chain, ambiguity_signals, slot_alternatives
_COMMON_REQUIRED = [
    "label_kind", "intent", "action", "tool_calls", "spans",
    "defer_reason", "alternative_tools", "response_text",
]


_TOOL_BRANCH = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "label_kind": {"type": "string", "enum": ["tool"]},
        "intent": {"type": "string", "enum": TOOL_INTENTS},
        "action": {"type": "string", "enum": ACTION_VALUES},
        "tool_calls": {"type": "array", "items": _tool_call_item()},
        "spans": {"type": "array", "items": _span_item()},
        "defer_reason": {"type": "null"},
        "alternative_tools": {"type": "array", "items": _alt_tool_item()},
        "response_text": {"type": "string"},
    },
    "required": _COMMON_REQUIRED,
}

_DEFER_BRANCH = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "label_kind": {"type": "string", "enum": ["defer"]},
        "intent": {"type": "null"},
        "action": {"type": "string", "enum": ["chat"]},
        "tool_calls": {"type": "array", "maxItems": 0, "items": _tool_call_item()},
        "spans": {"type": "array", "maxItems": 0, "items": _span_item()},
        "defer_reason": {"type": "string", "enum": DEFER_VALUES},
        "alternative_tools": {"type": "array", "items": _alt_tool_item()},
        "response_text": {"type": "string"},
    },
    "required": _COMMON_REQUIRED,
}

# Strict mode requires root type=object. Wrap the 2-branch anyOf inside a
# single `label` property. Output shape: {"label": {...branch fields}}.
SCHEMA = {
    "name": "teacher_router_label_v4",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["label"],
        "properties": {
            "label": {
                "anyOf": [_TOOL_BRANCH, _DEFER_BRANCH]
            }
        },
    },
}


def schema_strip_unsupported() -> dict:
    """Return a copy of SCHEMA with `minimum`/`maximum`/`maxItems` removed.

    Use as fallback if strict mode rejects those keywords.
    """
    import copy
    s = copy.deepcopy(SCHEMA)

    def strip(node):
        if isinstance(node, dict):
            for k in ("minimum", "maximum", "maxItems"):
                node.pop(k, None)
            for v in node.values():
                strip(v)
        elif isinstance(node, list):
            for v in node:
                strip(v)

    strip(s)
    return s
