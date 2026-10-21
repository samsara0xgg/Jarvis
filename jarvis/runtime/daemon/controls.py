"""The mute switches and the quiet level the surfaces flip, and what a change of one tells them."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jarvis.state import quiet_mode
from jarvis.surface import voice_controls

if TYPE_CHECKING:
    from jarvis.runtime import JarvisRuntime
    from jarvis.surface.inherent_output import InherentBroadcaster

LOGGER = logging.getLogger(__name__)


def build_controls(
    runtime: JarvisRuntime, broadcaster: InherentBroadcaster,
) -> voice_controls.VoiceControls:
    """The two mute switches the desktop surface flips over ``POST /inherent/controls``.

    ADR-0015: the mic on the input session, speech as the player's gain; the callbacks that
    act on them are set where the voice they act on is built. ADR 0153: the quiet level is the
    one switch kept across restarts. ADR 0174: a volume or speed said in a conversation ends
    with it.
    """
    quiet_path = runtime.runtime_paths.root / "quiet-mode.json"
    controls = voice_controls.VoiceControls(quiet=quiet_mode.load(quiet_path))

    def keep_quiet(level: str) -> None:
        quiet_mode.save(quiet_path, level)
        LOGGER.info("controls: quiet=%s", level)
        broadcaster.broadcast_op_sync("controls", **controls.update())

    controls.on_quiet = keep_quiet
    if runtime.voice_settings is not None:
        controls.on_conversation_end = runtime.voice_settings.end_conversation
    return controls


def set_quiet(controls: voice_controls.VoiceControls, level: str) -> None:
    """Set the quiet level; ``controls.on_quiet`` keeps it and tells the surfaces."""
    controls.update(quiet=level)


def set_conversation(
    controls: voice_controls.VoiceControls,
    broadcaster: InherentBroadcaster,
    on: bool,  # noqa: FBT001 - session callback shape
    reason: str,
) -> None:
    """ADR 0102: voice flips conversation mode; the surface hears it as a controls push."""
    state = controls.update(conversation=on)
    LOGGER.info("controls: conversation=%s (%s)", on, reason)
    broadcaster.broadcast_op_sync("controls", **state, reason=reason)
