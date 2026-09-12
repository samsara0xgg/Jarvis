"""Runtime mute switches shared by the Inherent HTTP surface and the voice code (ADR-0015).

Two in-memory booleans the desktop surface flips over ``POST /inherent/controls``
and the voice owners read on their own threads: ``mic_muted`` stops new wake
arms (Jarvis stops taking commands; barge-in during its own speech stays),
``speech_muted`` makes ``_tts_watcher`` treat every new response turn as
silent (text still streams, nothing is synthesized). Not persisted: a daemon
restart comes back unmuted, and the surface re-syncs on connect.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VoiceControls:
    """The two switches; plain attribute reads are atomic under the GIL."""

    mic_muted: bool = False
    speech_muted: bool = False

    def mic_is_muted(self) -> bool:
        """Reader the wake owners hold instead of the object itself."""
        return self.mic_muted

    def speech_is_muted(self) -> bool:
        """Reader the TTS watcher holds instead of the object itself."""
        return self.speech_muted

    def update(
        self,
        *,
        mic_muted: bool | None = None,
        speech_muted: bool | None = None,
    ) -> dict[str, bool]:
        """Apply the given switches (``None`` leaves one unchanged) and return the state."""
        if mic_muted is not None:
            self.mic_muted = mic_muted
        if speech_muted is not None:
            self.speech_muted = speech_muted
        return {"mic_muted": self.mic_muted, "speech_muted": self.speech_muted}


__all__ = ["VoiceControls"]
