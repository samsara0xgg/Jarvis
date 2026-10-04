"""What his voice carried that the words did not: tone and wordless laughs (ADR 0152).

SenseVoice hears every committed clip whole and labels its emotion and audio events; a
lone 哈哈 is dropped as a listening sound and never becomes text. This keeps those labels
in memory (never audio) until the next turn's state block reads them once through
:meth:`VoiceCues.line`.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Final

# SenseVoice emotion and event labels worth telling her. Only HAPPY: in the 2026-10-03
# replay of 725 clips it sat on his laughing lines, while 6 of 10 ANGRY were plain lines
# ("确认。") and SAD fired once on a garbled clip. BGM (11% of clean speech), Speech,
# NEUTRAL and EMO_UNKNOWN say nothing.
_TONES: Final = {"HAPPY": "happy"}
_SOUNDS: Final = {
    "Laughter": "a laugh", "Cough": "a cough", "Sneeze": "a sneeze", "Cry": "crying",
    "Breath": "a breath", "Applause": "applause",
}
_LAUGH_WORDS: Final = re.compile(r"[哈呵嘿嘻]{2,}")
_STALE_S: Final = 120.0  # a cue older than this describes a different moment


class VoiceCues:
    """The newest clip's tone and sounds, and wordless laughs dropped since her last turn."""

    def __init__(self) -> None:
        """Start with nothing heard."""
        self._lock = threading.Lock()
        self._clip: list[str] = []
        self._laughing = False
        self._laughs = [0, 0]  # [while she was silent, while she was speaking]
        self._at = 0.0

    def heard(self, text: str, emotion: str | None, event: str | None) -> None:
        """Note a clip's labels right after its final ASR, replacing the clip before it."""
        clip = [f"he sounded {_TONES[emotion]}"] if emotion in _TONES else []
        if event in _SOUNDS:
            clip.append(f"{_SOUNDS[event]} in this clip")
        with self._lock:
            self._clip = clip
            self._laughing = event == "Laughter" or _LAUGH_WORDS.fullmatch(
                re.sub(r"[\W_]+", "", text),
            ) is not None
            self._at = time.monotonic()

    def dropped(self, *, over_her: bool) -> None:
        """The clip just heard was no turn; a laugh in it is kept, its other cues are not."""
        with self._lock:
            self._laughs[over_her] += self._laughing
            self._clip = []
            self._laughing = False
            self._at = time.monotonic()

    def line(self) -> str | None:
        """One labelled line for the next turn, or None; reading clears it."""
        with self._lock:
            parts, laughs = self._clip, self._laughs
            self._clip, self._laughs = [], [0, 0]
            if time.monotonic() - self._at > _STALE_S:
                return None
        whens = ("since your last turn", "while you were speaking")
        for count, when in zip(laughs, whens, strict=True):
            if count:
                parts.append(f"he laughed without words {when}")
        head = "Voice cues (background, rarely worth mentioning; from audio, may be wrong): "
        return f"{head}{'; '.join(parts)}." if parts else None
