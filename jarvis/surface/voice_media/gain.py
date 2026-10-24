"""The media owner's output gain: the speech mute, a barge-in yield, and the fades around a cut."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jarvis.surface.voice_tts import AudioStreamPlayer

# A barge-in yield lowers or restores the speech within one spoken syllable.
YIELD_RAMP_MS = 30.0
# A stop or a barge-in hold first fades her out on the real waveform: the cut's
# own declick (ADR-0006 D11) starts from the last sample played, and from
# mid-word that truncation was a pop even at the yield's 0.2 (live test
# 2026-09-29). The wait is bounded for a player whose callback is not running.
CUT_FADE_MS = 20.0
_CUT_FADE_WAIT_S = 0.1


class OutputGain:
    """The only writer of the player's gain; callable from any thread (ADR 0383).

    The player's gain is the mute's (ADR-0015 D2) times a barge-in yield's, so a yield never
    lifts a mute. A fade to silence leaves both where they are, and ``restore`` gives the gain
    back to them.
    """

    def __init__(self, player: AudioStreamPlayer) -> None:
        """Drive ``player``; the mute and the yield start at full gain."""
        self._player = player
        self._lock = threading.Lock()
        self._mute = 1.0
        self._yield = 1.0

    def set_mute(self, gain: float, ramp_ms: float) -> None:
        """Ramp to ``gain`` times the yield: 0.0 is the speech mute, 1.0 restores."""
        with self._lock:
            self._mute = gain
            self._player.set_gain(gain * self._yield, ramp_ms)

    def set_yield(self, gain: float) -> None:
        """Lower the speech while Allen may be taking the turn; 1.0 gives it back."""
        with self._lock:
            self._yield = gain
            self._player.set_gain(self._mute * gain, YIELD_RAMP_MS)

    def begin_fade(self) -> None:
        """Start the ramp to silence on the real waveform, without waiting for it."""
        with self._lock:
            self._player.set_gain(0.0, CUT_FADE_MS)

    async def fade_out(self) -> None:
        """Ramp her to silence before the player cuts or holds her, bounded for a dead callback."""
        self.begin_fade()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CUT_FADE_WAIT_S
        while loop.time() < deadline:
            if self._player.current_gain() == 0.0:
                return
            await asyncio.sleep(0.005)

    def restore(self) -> None:
        """Give the gain back to the mute and the yield after a fade."""
        with self._lock:
            self._player.set_gain(self._mute * self._yield, YIELD_RAMP_MS)
