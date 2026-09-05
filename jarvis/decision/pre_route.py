"""L3 pre-route (ADR-0008 D2/D12): decide the emission route before generation.

No LLM call decides the route. A turn streams as ``routine_stream`` only when
the whole turn context is plain conversation: a user utterance, no Tier 0 hit,
no subject, no confirmation, no open action, no unresolved reference, and no
tool cue in the text. Anything else keeps today's full-text tool loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import yaml

from jarvis.decision.intent import tier_0_match
from jarvis.decision.stream_risk import ResponseRiskContext, snapshot_content_hash

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.decision.packet import SituationPacket
    from jarvis.decision.tier0 import Tier0Table

type Route = Literal["casual_or_explanatory", "action", "unknown"]
"""``casual_or_explanatory`` streams; ``action`` matched a tool cue; ``unknown`` buffers."""

ROUTINE_ATTENTION_CHANNEL = "voice_notify"
"""The channel a routine stream pins before generation; ordinary answers speak."""

_USER_TRIGGERS = frozenset({"surface.user_intent", "utterance.received"})
# Mirrors the demonstrative task reference the F1 short-circuit refuses.
_DEMONSTRATIVE_TASK_RE = re.compile(
    r"(那个|那些|这个|这些|刚才|上次|昨天|今天|明天).{0,30}(task|任务|项目)",
)


class ToolCueConfigError(ValueError):
    """``config/tool_cues.yaml`` is present but malformed."""


@dataclass(frozen=True)
class ToolCue:
    """One compiled cue; a hit means the request may need a tool."""

    id: str
    pattern: re.Pattern[str]


type ToolCueTable = tuple[ToolCue, ...]


def load_tool_cues(path: Path) -> ToolCueTable:
    """Parse the cue list at ``path``; a missing file means no cues, never a guess."""
    if not path.is_file():
        return ()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        message = f"tool cues: {path} is not valid YAML: {exc}"
        raise ToolCueConfigError(message) from exc
    if raw is None:
        return ()
    if not isinstance(raw, list):
        message = f"tool cues: top-level YAML must be a list, got {type(raw).__name__}"
        raise ToolCueConfigError(message)
    cues: list[ToolCue] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            message = f"tool cues: entry {index} needs a string id"
            raise ToolCueConfigError(message)
        pattern = entry.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            message = f"tool cues: entry {entry['id']!r} needs a non-empty pattern"
            raise ToolCueConfigError(message)
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            message = f"tool cues: entry {entry['id']!r} pattern is invalid: {exc}"
            raise ToolCueConfigError(message) from exc
        cues.append(ToolCue(id=str(entry["id"]), pattern=compiled))
    return tuple(cues)


def match_tool_cue(transcript: str, table: ToolCueTable) -> ToolCue | None:
    """Return the first cue found in ``transcript``, or ``None``."""
    return next((cue for cue in table if cue.pattern.search(transcript)), None)


def _transcript(packet: SituationPacket) -> str:
    raw = packet.trigger_event.payload.get("transcript", "")
    return raw if isinstance(raw, str) else ""


def pre_route(
    packet: SituationPacket,
    *,
    tier0_table: Tier0Table | None,
    tool_cues: ToolCueTable,
    now_ms: int,
) -> Route:
    """Classify the turn deterministically; ambiguity is ``unknown`` (D2 rule 1)."""
    trigger = packet.trigger_event
    transcript = _transcript(packet)
    slot = packet.pending_confirmation.slot
    history = packet.conversation_history
    if (
        trigger.type not in _USER_TRIGGERS
        or not transcript.strip()
        or tier_0_match(packet, tier0_table) is not None
        or packet.open_tasks
        or (slot is not None and slot.is_live(now_ms))
        or packet.status_board.open_actions
        or _DEMONSTRATIVE_TASK_RE.search(transcript) is not None
        or (history is not None and not history.consistent)
    ):
        return "unknown"
    if match_tool_cue(transcript, tool_cues) is not None:
        return "action"
    return "casual_or_explanatory"


def routine_risk_context(
    packet: SituationPacket,
    *,
    response_id: str,
    turn_id: str,
) -> ResponseRiskContext:
    """Pin the complete risk context of a ``casual_or_explanatory`` turn."""
    return ResponseRiskContext(
        response_id=response_id,
        turn_id=turn_id,
        user_request=_transcript(packet),
        route="casual_or_explanatory",
        active_subject_ref="none",
        linked_action_ids=(),
        pending_action_risk="none",
        confirmation_state="none",
        evidence_snapshot_hash=snapshot_content_hash(packet),
        attention_channel=ROUTINE_ATTENTION_CHANNEL,
        tools_offered=False,
        context_complete=True,
        unresolved_references=False,
        history_status="complete",
    )
