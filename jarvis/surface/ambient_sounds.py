"""L5 ambient sounds: what non-speech sounds happened around Allen, as one state-block line.

ADR 0151.  ``native/ambient_sounds`` runs Apple SoundAnalysis over the 16 kHz mono frames the
voice session already captures and prints the labels it hears.  This module feeds that helper,
filters its labels down to a short list Allen's own tests showed to be real, and renders the
recent ones for ``Runtime.live_context``.  Only label names and times are kept; audio is
analysed and dropped, never stored.

Layer rules: stdlib and ``jarvis.surface.voice_native_out`` only.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from jarvis.surface.voice_native_out import _HELPER_DIR as _VOICE_OUT_DIR
from jarvis.surface.voice_native_out import ensure_helper_binary

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

_HELPER_DIR: Final = _VOICE_OUT_DIR.parent / "ambient_sounds"
_RATE: Final = 16_000
# name -> (sure, maybe): a family's best confidence in one window is a fact at ``sure`` or above.
# ``maybe``, when set, makes it an uncertain event at or above that in two consecutive windows.
# Allen's live test (2026-10-03): the real events scored 0.92-0.99 and the false ones (door, bird,
# drawer) 0.31-0.77, so everything wants 0.9; his real laugh scored 0.43-0.59 over three windows
# and no false laughter appeared. A name not listed gets ``DEFAULT_BAR``.
DEFAULT_BAR: Final[tuple[float, float | None]] = (0.9, None)
THRESHOLDS: Final[dict[str, tuple[float, float | None]]] = {"laughter": (0.9, 0.4)}

_FAMILIES: Final[dict[str, tuple[str, ...]]] = {
    "laughter": ("laughter", "giggling", "chuckle_chortle", "belly_laugh", "snicker"),
    "cough": ("cough",),
    "sneeze": ("sneeze",),
    "sigh": ("sigh",),
    "nose_blowing": ("nose_blowing",),
    "snoring": ("snoring",),
    "crying": ("crying_sobbing",),
    "shouting": ("screaming", "shout", "yell"),
    "bird": ("bird", "bird_chirp_tweet", "bird_vocalization"),
    "dog": ("dog", "dog_bark", "dog_bow_wow"),
    "cat": ("cat", "cat_meow"),
    "rain": ("rain",),
    "thunderstorm": ("thunderstorm",),
    "knock": ("knock",),
    "doorbell": ("door_bell",),
    "door": ("door", "door_slam"),
    "phone": ("telephone", "ringtone", "telephone_bell_ringing"),
    "alarm": ("smoke_detector", "alarm_clock"),
    "siren": ("siren", "police_siren", "ambulance_siren", "fire_engine_siren"),
    "music": (
        "music", "synthesizer", "piano", "electric_piano", "guitar", "acoustic_guitar",
        "electric_guitar", "drum_kit", "violin_fiddle", "saxophone", "organ",
    ),
    "typing": ("typing", "typing_computer_keyboard"),
}
_NAME_OF: Final = {label: name for name, labels in _FAMILIES.items() for label in labels}
# How each name reads in the line: (words, True if a repeat count is worth showing).
_PHRASES: Final[dict[str, tuple[str, bool]]] = {
    "laughter": ("laughed", True),
    "cough": ("coughed", True),
    "sneeze": ("sneezed", True),
    "sigh": ("sighed", True),
    "nose_blowing": ("blew his nose", True),
    "snoring": ("snoring", False),
    "crying": ("crying", False),
    "shouting": ("shouting", True),
    "bird": ("birds", False),
    "dog": ("a dog", False),
    "cat": ("a cat", False),
    "rain": ("rain", False),
    "thunderstorm": ("thunderstorm", False),
    "knock": ("knocking", True),
    "doorbell": ("doorbell", True),
    "door": ("a door", True),
    "phone": ("a phone ringing", True),
    "alarm": ("an alarm", False),
    "siren": ("a siren", False),
    "music": ("music on", False),
    "typing": ("typing", False),
}

_HOP_S: Final = 2.0  # results come 1.5 s apart (3 s windows, 0.5 overlap) plus slack
_WINDOW_S: Final = 3.0  # the classifier's window; a result covers the 3 s before its ``t``
_TAIL_S: Final = 0.5  # her voice is still decaying in the room this long after she stops
_DEBOUNCE_S: Final = 10.0
_SPAN_S: Final = 600.0
_LINE_CHARS: Final = 200
_QUEUE_FRAMES: Final = 64  # ~2 s of 512-sample frames; a full queue drops the newest
_MAX_RESTARTS: Final = 5
_RESTART_BACKOFF_S: Final = 5.0


@dataclass
class _Event:
    t: float  # monotonic seconds, last time heard
    name: str
    maybe: float  # 0 for a fact, else the best confidence of an uncertain event


class AmbientSounds:
    """The helper's lifecycle, the playback and threshold filter, and the ``live_context`` line."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, *, log_path: Path | None = None,
    ) -> None:
        """Idle until :meth:`start`; ``clock`` times events (monotonic seconds).

        ``log_path`` gets one JSON line per classifier window: its labels, whether her voice
        was in it, and what was kept. Labels only, never audio.
        """
        self._clock = clock
        self._log_path = log_path
        self._lock = threading.Lock()
        # Samples sent to the helper so far: its clock, which a result's ``t`` is read on.
        self._fed = 0
        self._busy: deque[list[int]] = deque(maxlen=64)  # [start, end] samples while she spoke
        self._events: deque[_Event] = deque(maxlen=512)
        self._maybe_prev: dict[str, tuple[float, float]] = {}  # name -> (window end, confidence)
        self._queue: queue.Queue[bytes | None] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        """Build and run the helper in the background; frames are dropped until it is up."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="ambient-sounds", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """End the helper and wait briefly for the supervisor."""
        self._stop.set()
        proc = self._proc
        if proc is not None:
            proc.terminate()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)

    def feed(self, pcm16: bytes, *, speaking: bool) -> None:
        """Hand one 16 kHz mono int16 frame to the helper; never blocks, drops when it lags."""
        q = self._queue
        if q is None:
            return
        try:
            q.put_nowait(pcm16)
        except queue.Full:
            return
        self.track(len(pcm16) // 2, speaking=speaking)

    def track(self, samples: int, *, speaking: bool) -> None:
        """Count samples sent, and remember the span in which she was speaking."""
        with self._lock:
            start = self._fed
            self._fed += samples
            if not speaking:
                return
            if self._busy and self._busy[-1][1] >= start:
                self._busy[-1][1] = self._fed
            else:
                self._busy.append([start, self._fed])

    def ingest(self, raw: bytes | str) -> None:
        """Take one helper line: keep the whitelisted labels that clear their bar and her voice."""
        try:
            result = json.loads(raw)
            end_s = float(result["t"])
            heard = [(str(label), float(conf)) for label, conf in result["labels"]]
        except (ValueError, KeyError, TypeError):
            LOGGER.debug("ambient sounds: unreadable helper line %r", raw)
            return
        best: dict[str, float] = {}
        for label, conf in heard:
            name = _NAME_OF.get(label)
            if name is not None:
                best[name] = max(best.get(name, 0.0), conf)
        window_end = int(end_s * _RATE)
        window_start = window_end - int(_WINDOW_S * _RATE)
        kept: list[str] = []
        with self._lock:
            tail = int(_TAIL_S * _RATE)
            her_voice = any(a < window_end and b + tail > window_start for a, b in self._busy)
            now = self._clock()
            for name, conf in best.items() if not her_voice else ():
                sure, maybe = THRESHOLDS.get(name, DEFAULT_BAR)
                if conf >= sure:
                    self._record_locked(name, now, 0.0)
                    kept.append(name)
                elif maybe is not None and conf >= maybe:
                    prev = self._maybe_prev.get(name)
                    if prev is not None and 0 < end_s - prev[0] <= _HOP_S:
                        self._record_locked(name, now, max(conf, prev[1]))
                        kept.append(f"maybe {name}")
                if maybe is not None and conf >= maybe:
                    self._maybe_prev[name] = (end_s, conf)
        self._log(heard, her_voice=her_voice, kept=kept)

    def _log(self, heard: list[tuple[str, float]], *, her_voice: bool, kept: list[str]) -> None:
        if self._log_path is None or not heard:
            return
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "labels": [[label, round(conf, 3)] for label, conf in heard],
            "her_voice": her_voice,
            "kept": kept,
        }
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            LOGGER.debug("ambient sounds: could not write %s", self._log_path, exc_info=True)

    def _record_locked(self, name: str, now: float, maybe: float) -> None:
        for event in reversed(self._events):
            if event.name == name and bool(event.maybe) == bool(maybe):
                if now - event.t < _DEBOUNCE_S:
                    event.t = now  # same sound still going: one event, kept current
                    event.maybe = max(event.maybe, maybe)
                    return
                break
        self._events.append(_Event(now, name, maybe))

    def line(self) -> str | None:
        """The state-block line for the sounds of the last 10 minutes, every turn, or None.

        Every turn, not only what is new: asked what he heard, she answers from it instead of
        reaching for a tool (a 2026-10-03 replay: 6 of 10 tool calls without it, 2 with it).
        """
        now = self._clock()
        with self._lock:
            since = now - _SPAN_S
            fresh = [e for e in self._events if e.t > since]
            if not fresh:
                return None
        sure: dict[str, int] = {}
        maybe: dict[str, tuple[int, float]] = {}
        for e in fresh:
            if e.maybe:
                n, best = maybe.get(e.name, (0, 0.0))
                maybe[e.name] = (n + 1, max(best, e.maybe))
            else:
                sure[e.name] = sure.get(e.name, 0) + 1
        facts = [
            f"{words} repeatedly" if counted and sure[name] > 1 else words
            for name, (words, counted) in _PHRASES.items()
            if name in sure
        ]
        guesses = [
            f"maybe {_PHRASES[name][0]} "
            f"(~{round(best * 100)}%, this detector under-scores his laugh)"
            for name, (_, best) in maybe.items()
            if name not in sure
        ]
        minutes = max(1, math.ceil((now - since) / 60))
        head = (
            "Sounds around him (background, rarely worth mentioning; "
            f"last {minutes} min, from audio): "
        )
        tail = "; ".join(guesses) + "."
        room = _LINE_CHARS - len(head) - (len(tail) if guesses else 1)
        kept: list[str] = []
        for fact in facts:  # whole phrases only, in order; the guess is kept over the facts
            if sum(len(k) + 2 for k in kept) + len(fact) > room - (2 if guesses else 0):
                break
            kept.append(fact)
        body = ", ".join(kept)
        if guesses:
            return head + (body + "; " if body else "") + tail
        return head + body + "."

    def _supervise(self) -> None:
        restarts = 0
        try:
            binary = ensure_helper_binary(_HELPER_DIR, "jarvis-ambient-sounds")
        except Exception:
            LOGGER.exception("ambient sounds: helper build failed; off for this run")
            return
        while not self._stop.is_set():
            try:
                self._run_once(binary)
            except Exception:
                LOGGER.exception("ambient sounds: helper run failed")
            if self._stop.is_set() or restarts >= _MAX_RESTARTS:
                break
            restarts += 1
            LOGGER.warning("ambient sounds: helper exited, restart %d/%d", restarts, _MAX_RESTARTS)
            self._stop.wait(_RESTART_BACKOFF_S * restarts)
        if not self._stop.is_set():
            LOGGER.warning("ambient sounds: helper kept dying; off for this run")

    def _run_once(self, binary: object) -> None:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, repo-owned binary
            [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        q: queue.Queue[bytes | None] = queue.Queue(maxsize=_QUEUE_FRAMES)
        with self._lock:
            self._fed = 0
            self._busy.clear()
        self._proc = proc
        writer = threading.Thread(
            target=self._write_loop, args=(proc, q), name="ambient-sounds-in", daemon=True,
        )
        writer.start()
        self._queue = q
        try:
            assert proc.stdout is not None  # noqa: S101 - PIPE was requested above
            for raw in proc.stdout:
                self.ingest(raw)
        finally:
            self._queue = None
            proc.terminate()
            while True:  # make room for the writer's stop sentinel
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            q.put(None)
            writer.join(timeout=2.0)
            err = proc.stderr.read() if proc.stderr is not None else b""
            proc.wait(timeout=2.0)
            self._proc = None
            if err:
                LOGGER.warning("ambient sounds helper: %s", err.decode(errors="replace")[-400:])

    @staticmethod
    def _write_loop(proc: subprocess.Popen[bytes], q: queue.Queue[bytes | None]) -> None:
        assert proc.stdin is not None  # noqa: S101 - PIPE was requested
        while (data := q.get()) is not None:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError):
                return
