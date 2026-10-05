"""Streaming silence trimmer for one playback generation (ADR 0165).

Provider segments carry digital silence at both ends (MiniMax: median 182 ms
before the first word and 261 ms after the last). Played back to back that is
about 440 ms of dead air at every segment junction, and the first word of an
answer starts about 180 ms late. The trimmer runs on canonical float32-LE
PCM after the resampler and before the player ring. It only ever removes or
holds samples whose 5 ms RMS is below a threshold; a sample above it is passed
on the moment it is seen, in order, unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Clause marks, with the fullwidth comma, enumeration comma, semicolon and colon escaped.
_CLAUSE_ENDS = frozenset(",;:\uff0c\u3001\uff1b\uff1a")
# Closing quotes and brackets (curly and corner quotes, fullwidth parenthesis) may follow the mark.
_CLOSERS = frozenset("\"')]> \t\r\n\u201d\u2019\u300d\u300f\uff09\u3011\u300b")


@dataclass(frozen=True)
class SilenceTrimConfig:
    """Thresholds and lengths; the media config owns the defaults."""

    threshold_db: float
    window_ms: float
    preroll_ms: float
    fade_ms: float
    max_cut_ms: float
    clause_pause_ms: float
    sentence_pause_ms: float


def junction_pause_ms(text: str, config: SilenceTrimConfig) -> float:
    """Natural pause after a segment: by the punctuation that ended it."""
    stripped = text.rstrip()
    while stripped and stripped[-1] in _CLOSERS:
        stripped = stripped[:-1]
    if stripped and stripped[-1] in _CLAUSE_ENDS:
        return config.clause_pause_ms
    return config.sentence_pause_ms


def _empty() -> np.ndarray:
    return np.zeros(0, dtype=np.float32)


class SilenceTrimmer:
    """Stateful per-generation trimmer: ``begin_segment``, ``feed``..., ``end_segment``."""

    def __init__(self, *, sample_rate_hz: int, config: SilenceTrimConfig) -> None:
        """Derive every length in samples at the canonical rate."""
        self._config = config
        ms = sample_rate_hz / 1000
        self._window = max(1, round(config.window_ms * ms))
        self._threshold_energy = 10 ** (config.threshold_db / 10)
        self._preroll = max(self._window, round(config.preroll_ms * ms))
        self._fade = round(config.fade_ms * ms)
        self._max_cut = round(config.max_cut_ms * ms)
        self._ms = ms
        # What the next segment may keep of its leading silence: the pre-roll
        # at the start of a generation, the rest of the junction pause after.
        self._head_keep = self._preroll
        self._pause = 0
        self._lead = True
        self._lead_buf = _empty()
        self._hold = _empty()
        self._energy_tail = np.zeros(self._window - 1)
        self._fade_done = 0
        self.head_dropped = 0

    def begin_segment(self, text: str) -> None:
        """Start (or restart, after a segment that wrote nothing) one segment."""
        self._pause = round(junction_pause_ms(text, self._config) * self._ms)
        self._lead = True
        self._lead_buf = _empty()
        self._hold = _empty()
        self._energy_tail = np.zeros(self._window - 1)
        self._fade_done = 0
        self.head_dropped = 0

    def feed(self, pcm: bytes) -> bytes:
        """Return the samples that may be played now, in order, unchanged."""
        if not pcm:
            return b""
        samples = np.frombuffer(pcm, dtype="<f4")
        active = self._active(samples)
        out: list[np.ndarray] = []
        if self._lead:
            hits = np.flatnonzero(active)
            if hits.size == 0:
                self._lead_buf = np.concatenate([self._lead_buf, samples])
                out.append(self._spill_lead())
                return _join(out)
            first = int(hits[0])
            out.append(self._open_speech(np.concatenate([self._lead_buf, samples[:first]])))
            samples, active = samples[first:], active[first:]
        hits = np.flatnonzero(active)
        if hits.size == 0:
            self._hold = np.concatenate([self._hold, samples])
        else:
            last = int(hits[-1])
            out.append(self._hold)
            out.append(samples[: last + 1])
            self._hold = samples[last + 1 :]
        # A silence longer than the tail can ever be cut back to is not held.
        hold_max = max(0, self._pause - self._preroll) + self._max_cut
        if len(self._hold) > hold_max:
            out.append(self._hold[: len(self._hold) - hold_max])
            self._hold = self._hold[len(self._hold) - hold_max :]
        return _join(out)

    def end_segment(self) -> bytes:
        """Flush the segment: keep only the tail the junction pause allows."""
        if self._lead:
            kept = self._ramp_in(self._lead_buf)
            self._lead_buf = _empty()
            self._head_keep = max(self._preroll, self._pause - len(kept))
            return kept.tobytes()
        keep = min(len(self._hold), max(0, self._pause - self._preroll))
        kept = self._hold[:keep].copy()
        if keep < len(self._hold):
            fade = min(self._fade, keep)
            if fade:
                kept[keep - fade :] *= np.linspace(1.0, 0.0, fade, endpoint=False, dtype=np.float32)
        self._hold = _empty()
        self._head_keep = max(self._preroll, self._pause - keep)
        return kept.tobytes()

    def _active(self, samples: np.ndarray) -> np.ndarray:
        """True where the window of samples ending at this one is above the threshold."""
        energy = np.concatenate([self._energy_tail, samples.astype(np.float64) ** 2])
        csum = np.concatenate([[0.0], np.cumsum(energy)])
        window_mean = (csum[self._window :] - csum[: -self._window]) / self._window
        self._energy_tail = energy[len(energy) - (self._window - 1) :]
        return np.asarray(window_mean >= self._threshold_energy)

    @property
    def _lead_keep(self) -> int:
        # One window more than the pause: the onset of a word is below the
        # threshold until the window fills, and must not be cut with the silence.
        return self._head_keep + self._window

    def _drop_budget(self) -> int:
        return max(0, self._max_cut - self.head_dropped)

    def _spill_lead(self) -> np.ndarray:
        """Silence before any speech: keep the newest ``head_keep``, drop or release the rest."""
        overflow = len(self._lead_buf) - self._lead_keep
        if overflow <= 0:
            return _empty()
        drop = min(overflow, self._drop_budget())
        self.head_dropped += drop
        released = self._lead_buf[drop:overflow]
        self._lead_buf = self._lead_buf[overflow:]
        return self._ramp_in(released) if len(released) else released

    def _open_speech(self, silence: np.ndarray) -> np.ndarray:
        """Speech begins: emit the silence kept in front of it, cut back if long."""
        drop = min(max(0, len(silence) - self._lead_keep), self._drop_budget())
        self.head_dropped += drop
        self._lead = False
        self._lead_buf = _empty()
        return self._ramp_in(silence[drop:])

    def _ramp_in(self, silence: np.ndarray) -> np.ndarray:
        """Fade the first silent samples after a head cut; speech is never passed here."""
        if self.head_dropped == 0 or self._fade == 0 or len(silence) == 0:
            return silence
        n = min(self._fade - self._fade_done, len(silence))
        if n <= 0:
            return silence
        out = silence.copy()
        out[:n] *= np.arange(self._fade_done, self._fade_done + n, dtype=np.float32) / self._fade
        self._fade_done += n
        return out


def _join(parts: list[np.ndarray]) -> bytes:
    return np.concatenate(parts).astype("<f4", copy=False).tobytes() if parts else b""
