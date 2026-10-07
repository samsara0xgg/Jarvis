"""ADR 0174: the conversation's way to her voice volume and speed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state.voice_settings import (
    MAX_PERCENT,
    MAX_SPEED,
    MIN_PERCENT,
    MIN_SPEED,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.execution.tools import ToolContext
    from jarvis.state.voice_settings import VoiceSettings

CONFIRM_ABOVE_PERCENT: Final[int] = 200
# The model chooses only a direction and a size; these numbers are the program's. Volume
# steps multiply, so each is the same loudness change at any level: about 3 dB and 6 dB.
_VOLUME_STEPS: Final[dict[str, float]] = {
    "louder_a_bit": 1.4, "louder_a_lot": 2.0, "quieter_a_bit": 0.7, "quieter_a_lot": 0.5,
}
_SPEED_STEPS: Final[dict[str, float]] = {
    "faster_a_bit": 0.2, "faster_a_lot": 0.4, "slower_a_bit": -0.2, "slower_a_lot": -0.4,
}
_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "volume": {"type": "string", "enum": list(_VOLUME_STEPS)},
        "speed": {"type": "string", "enum": list(_SPEED_STEPS)},
        "reset": {
            "type": "boolean",
            "description": "Back to the factory voice (normal volume and speed) and forget"
            " any remembered default.",
        },
        "remember": {
            "type": "boolean",
            "description": "Keep the current volume and speed as the default from now on,"
            " also after a restart.",
        },
        "confirmed": {
            "type": "boolean",
            "description": "True only when the user has just agreed to go above 200% volume.",
        },
    },
    "required": [],
}
_DESCRIPTION: Final = (
    "Change the assistant's own voice: its speaking volume and speed. Use only when the user"
    " asks about the assistant's own voice: louder, quieter, faster, slower, back to normal,"
    " or keep it like this (remember). 'What? What did you say', 'say that again' and 'I can't"
    " make that out' mean say it again, not louder. Music, a video or the computer's system"
    " volume are not this tool. Choose a_lot only when the user says it strongly or asks again"
    " right after a change. The new voice applies from your next spoken answer, which is your"
    " reply to this request; the result carries the new values and flags (at_limit,"
    " needs_confirmation, remembered, reset): say it in your own words. When"
    " needs_confirmation is true nothing was changed for volume: tell the user the volume"
    " would be that high and ask first; call again with confirmed true only after a yes."
    " Without remember, the change ends with the conversation."
)


def _pick(args: Mapping[str, Any], key: str, table: Mapping[str, Any]) -> Any:  # noqa: ANN401 - int or float step
    """The program's step for the model's enum word; unknown is the model's error."""
    word = args.get(key)
    if word is None or word == "":
        return None
    if word not in table:
        msg = f"{key} must be one of: {', '.join(table)}"
        raise ToolError(msg, code="invalid_argument")
    return table[word]


def build_voice_tool(voice: VoiceSettings | None) -> tuple[Tool, ...]:
    """``set_voice`` bound to the shared settings; none when unwired."""
    if voice is None:
        return ()

    def set_voice(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        volume_step = _pick(args, "volume", _VOLUME_STEPS)
        speed_step = _pick(args, "speed", _SPEED_STEPS)
        reset = args.get("reset") is True
        confirmed = args.get("confirmed") is True
        if reset:
            voice.reset()
        percent, speed = voice.snapshot()
        before = (percent, speed)
        at_limit = False
        needs_confirmation = False
        would_be: int | None = None
        if volume_step is not None:
            stepped = round(percent * volume_step)
            target = min(MAX_PERCENT, max(MIN_PERCENT, stepped))
            at_limit = target != stepped or target == percent
            if target > CONFIRM_ABOVE_PERCENT and not confirmed and target > percent:
                needs_confirmation, would_be = True, target
            else:
                percent = target
        if speed_step is not None:
            target_speed = round(min(MAX_SPEED, max(MIN_SPEED, speed + speed_step)), 2)
            at_limit = at_limit or target_speed != round(speed + speed_step, 2) or (
                target_speed == speed
            )
            speed = target_speed
        voice.set_current(percent, speed)
        remembered = args.get("remember") is True
        if remembered:
            voice.remember()
        result: dict[str, Any] = {
            "volume_percent": percent,
            "speed": speed,
            "changed": [
                name for name, now, was in (
                    ("volume", percent, before[0]), ("speed", speed, before[1]),
                ) if now != was
            ],
            "at_limit": at_limit,
            "needs_confirmation": needs_confirmation,
            "remembered": remembered,
            "reset": reset,
        }
        if would_be is not None:
            result["would_be_volume_percent"] = would_be
        return result

    return (
        Tool(
            name="set_voice",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=set_voice,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
        ),
    )


__all__ = ["CONFIRM_ABOVE_PERCENT", "build_voice_tool"]
