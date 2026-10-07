"""Runtime switches shared by the Inherent HTTP surface and the voice code (ADR-0015).

In-memory booleans the desktop surface flips over ``POST /inherent/controls``.
``mic_muted`` stops new wake arms (Jarvis stops taking commands; barge-in
during its own speech stays). ``speech_muted`` is the TTS player's output
gain: synthesis, timing, phases and events run exactly as unmuted, only the
speaker is silent, so unmuting mid-sentence resumes audibly. ``conversation``
(ADR 0041) is the surface's wave mode: capture listens without a wake word,
and speech over Jarvis lowers her until its words decide (ADR 0100). Not
persisted: a daemon restart comes back unmuted and out of conversation, and
the surface re-syncs on connect. ``quiet`` (ADR 0153) is the one exception:
the runtime loads it at boot and saves every change through ``on_quiet``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class VoiceControls:
    """The switches; plain attribute reads are atomic under the GIL."""

    mic_muted: bool = False
    speech_muted: bool = False
    conversation: bool = False
    # ADR 0153: ``off``, ``quiet``, ``no-pop`` or ``dnd`` (``jarvis.state.quiet_mode.LEVELS``).
    quiet: str = "off"
    # ``(level) -> None``, bound by the runtime to the saved file and the controls push.
    on_quiet: Callable[[str], None] | None = None
    # ``() -> None``, bound by the runtime; called when conversation goes from on to off (ADR 0174).
    on_conversation_end: Callable[[], None] | None = None
    # ``(muted) -> None``, bound by the runtime to the TTS player's gain.
    on_speech_muted: Callable[[bool], None] | None = None
    # ``(muted) -> None``, bound by the runtime to the GPT-Live sender gate.
    on_mic_muted: Callable[[bool], None] | None = None

    def mic_is_muted(self) -> bool:
        """Reader the wake owners hold instead of the object itself."""
        return self.mic_muted

    def conversation_is_on(self) -> bool:
        """Reader the duplex voice session holds instead of the object itself."""
        return self.conversation

    def update(
        self,
        *,
        mic_muted: bool | None = None,
        speech_muted: bool | None = None,
        conversation: bool | None = None,
        quiet: str | None = None,
    ) -> dict[str, bool | str]:
        """Apply the given switches (``None`` leaves one unchanged) and return the state."""
        if mic_muted is not None and mic_muted != self.mic_muted:
            self.mic_muted = mic_muted
            if self.on_mic_muted is not None:
                self.on_mic_muted(mic_muted)
        if speech_muted is not None and speech_muted != self.speech_muted:
            self.speech_muted = speech_muted
            if self.on_speech_muted is not None:
                self.on_speech_muted(speech_muted)
        if conversation is not None:
            ended = self.conversation and not conversation
            self.conversation = conversation
            if ended and self.on_conversation_end is not None:
                self.on_conversation_end()
        if quiet is not None and quiet != self.quiet:
            self.quiet = quiet
            if self.on_quiet is not None:
                self.on_quiet(quiet)
        return {
            "mic_muted": self.mic_muted,
            "speech_muted": self.speech_muted,
            "conversation": self.conversation,
            "quiet": self.quiet,
        }


__all__ = ["VoiceControls"]
