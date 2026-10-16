"""L2 voiceprints: one profile of the owner's voice per terminal microphone (ADR 0286).

A terminal sends each utterance with a 192-number speaker embedding, the speech seconds, the
name of the microphone that heard it and how long that stream has been open. The brain scores it
against the profile of that (device, microphone), learns from the owner's own turns, and turns
away a long utterance that scores far below it. Scores, thresholds and profiles live here and
nowhere else; nothing in this module reaches a model.

A profile is the L2-normalised, weighted mean of the newest rows of a (device, microphone) whose
weights total :data:`WINDOW_WEIGHT`: a learned row weighs 1, a seed row (the offline study's
centroid, imported once) its stated count. Rows are append-only in ``memory.db`` (``voiceprints``)
and the profiles are cached here, refreshed when a row is added to one. A process that only
seeds or reads the table (``python -m jarvis voiceprint``) does not refresh a running brain's
cache: the brain reads the new rows at its next restart, or at the next row it adds there.

Errors: ``ValueError`` is a refused seed.
"""

from __future__ import annotations

import logging
import math
import threading
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, NamedTuple, TypeGuard

import numpy as np

from jarvis.state.memory_db import iso_seconds, local_now, open_memory_db

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

LOGGER = logging.getLogger("jarvis.state.voiceprint")

DIMENSIONS: Final = 192
MIN_SECONDS: Final = 2.0
"""Shorter speech neither turns anything away nor teaches a profile: half his fragments score
below the bar."""
MIN_MIC_AGE_S: Final = 180.0
"""The first minutes after a microphone opens score wrongly (the reSpeaker at power-on)."""
TURN_AWAY_BELOW: Final = 0.30
LEARN_AT: Final = 0.50
"""A turn this close to its own ready profile joins it."""
BOOTSTRAP_AT: Final = 0.35
"""A turn this close to his best other profile starts a microphone that has none ready."""
WINDOW_WEIGHT: Final = 300.0
READY_ROWS: Final = 30
READY_DAYS: Final = 2
_MAX_MIC_CHARS: Final = 200


def _number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _unit(vector: np.ndarray) -> np.ndarray | None:
    norm = float(np.linalg.norm(vector))
    return None if not math.isfinite(norm) or norm == 0.0 else (vector / norm).astype(np.float32)


class Voiceprint(NamedTuple):
    """What a terminal sent with one utterance; ``embedding`` is a unit vector."""

    embedding: np.ndarray
    seconds: float
    mic: str
    mic_age_s: float

    @classmethod
    def parse(cls, raw: object) -> Voiceprint | None:
        """The checked ``voiceprint`` object of a frame; ``None`` when absent or malformed."""
        if not isinstance(raw, dict):
            return None
        vector, seconds, mic, age = (
            raw.get("embedding"), raw.get("seconds"), raw.get("mic"), raw.get("mic_age_s"),
        )
        if (
            not isinstance(vector, list) or len(vector) != DIMENSIONS
            or not isinstance(mic, str) or not 0 < len(mic) <= _MAX_MIC_CHARS
            or not _number(seconds) or not _number(age)
            or not all(_number(one) for one in vector)
        ):
            return None
        unit = _unit(np.asarray(vector, dtype=np.float32))
        return None if unit is None else cls(unit, float(seconds), mic, float(age))

    @property
    def settled(self) -> bool:
        """Long enough, on a microphone open long enough, to be judged or learned from."""
        return self.seconds >= MIN_SECONDS and self.mic_age_s >= MIN_MIC_AGE_S


@dataclass(frozen=True)
class Scored:
    """One utterance's score and what it means; ``score`` is ``None`` with nothing to compare."""

    device: str
    voiceprint: Voiceprint
    score: float | None
    own: bool
    """The score is against this microphone's own ready profile, not another one's."""
    turn_away: bool

    def payload(self) -> dict[str, Any] | None:
        """What the turn's ``utterance.received`` keeps: never the embedding."""
        if self.score is None:
            return None
        return {
            "score": round(self.score, 3), "mic": self.voiceprint.mic,
            "seconds": round(self.voiceprint.seconds, 2),
        }


class Status(NamedTuple):
    """One (device, microphone) as ``voiceprint status`` prints it."""

    device: str
    mic: str
    rows: int
    days: int
    ready: bool
    last_added: str


@dataclass(frozen=True)
class _Profile:
    vector: np.ndarray
    ready: bool


def _ready(rows: int, days: int, *, seeded: bool) -> bool:
    return seeded or (rows >= READY_ROWS and days >= READY_DAYS)


class VoiceprintBook:
    """The owner's profiles, per (device, microphone), over ``memory.db``.

    ``gate()`` says whether the setting lets an utterance be turned away; off, everything is
    still scored, logged and learned. Safe to call from any thread.
    """

    def __init__(self, db_path: Path, *, gate: Callable[[], bool] = lambda: True) -> None:
        """Keep profiles in the ``voiceprints`` table of the memory database at ``db_path``."""
        self._path = db_path
        self._gate = gate
        self._lock = threading.Lock()
        self._profiles: dict[tuple[str, str], _Profile] | None = None

    def score(self, device: str, voiceprint: Voiceprint) -> Scored:
        """Score ``voiceprint`` against its microphone's profile, else against his best other.

        Turned away only when the gate is on, the speech is long enough, its microphone has been
        open long enough, that microphone's profile is ready and the score is under
        :data:`TURN_AWAY_BELOW`.
        """
        key = (device, voiceprint.mic)
        with self._lock:
            profiles = self._loaded()
            own = profiles.get(key)
            if own is not None and own.ready:
                best: float | None = float(np.dot(voiceprint.embedding, own.vector))
                is_own = True
            else:
                others = [
                    float(np.dot(voiceprint.embedding, one.vector))
                    for other, one in profiles.items() if one.ready and other != key
                ]
                best, is_own = (max(others) if others else None), False
        turn_away = (
            is_own and best is not None and best < TURN_AWAY_BELOW and voiceprint.settled
            and self._gate()
        )
        return Scored(device, voiceprint, best, is_own, turn_away)

    def finish(self, scored: Scored, verdict: str, turn_id: str) -> None:
        """Log the scored utterance with its verdict; a turn that is his own joins the profile.

        The turn id keeps a resent utterance from being learned twice.
        """
        voiceprint, score = scored.voiceprint, scored.score
        LOGGER.info(
            "voiceprint device=%s mic=%r seconds=%.1f age=%.0fs score=%s against=%s verdict=%s",
            scored.device, voiceprint.mic, voiceprint.seconds, voiceprint.mic_age_s,
            "none" if score is None else f"{score:.3f}",
            "none" if score is None else "own" if scored.own else "other", verdict,
        )
        if (
            verdict == "turn" and score is not None and voiceprint.settled
            and score >= (LEARN_AT if scored.own else BOOTSTRAP_AT)
        ):
            self._add(scored.device, voiceprint, weight=1.0, seed=False, turn_id=turn_id)

    def seed(self, device: str, mic: str, count: int, centroid: Sequence[object]) -> None:
        """Import the offline study's centroid of ``count`` turns as one ready seed row.

        Raises:
            ValueError: ``count`` is below 1, or ``centroid`` is not 192 finite numbers.
        """
        unit = _checked_seed(count, centroid)
        self._add(
            device, Voiceprint(unit, 0.0, mic, 0.0), weight=float(min(count, WINDOW_WEIGHT)),
            seed=True, turn_id=None,
        )

    def status(self) -> list[Status]:
        """Every (device, microphone) with rows; no embeddings."""
        with closing(open_memory_db(self._path)) as conn:
            rows = conn.execute(
                "SELECT device, mic, COUNT(*), COUNT(DISTINCT substr(ts, 1, 10)), MAX(seed), "
                "MAX(ts) FROM voiceprints GROUP BY device, mic ORDER BY device, mic",
            ).fetchall()
        return [
            Status(device, mic, count, days, _ready(count, days, seeded=bool(seeded)), last)
            for device, mic, count, days, seeded, last in rows
        ]

    # -- rows and the cache ---------------------------------------------------------------

    def _loaded(self) -> dict[tuple[str, str], _Profile]:
        """Every profile, read once; call with the lock held."""
        if self._profiles is None:
            profiles: dict[tuple[str, str], _Profile] = {}
            with closing(open_memory_db(self._path)) as conn:
                keys = conn.execute("SELECT DISTINCT device, mic FROM voiceprints").fetchall()
                for device, mic in keys:
                    if (profile := _profile(conn, device, mic)) is not None:
                        profiles[device, mic] = profile
            self._profiles = profiles
        return self._profiles

    def _add(
        self, device: str, voiceprint: Voiceprint, *, weight: float, seed: bool,
        turn_id: str | None,
    ) -> None:
        with self._lock:
            profiles = self._loaded()
            with closing(open_memory_db(self._path)) as conn, conn:
                conn.execute(
                    "INSERT OR IGNORE INTO voiceprints "
                    "(device, mic, ts, seconds, weight, seed, turn_id, vec) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        device, voiceprint.mic, iso_seconds(local_now()), voiceprint.seconds,
                        weight, int(seed), turn_id, voiceprint.embedding.tobytes(),
                    ),
                )
            with closing(open_memory_db(self._path)) as conn:
                profile = _profile(conn, device, voiceprint.mic)
            if profile is not None:
                profiles[device, voiceprint.mic] = profile


def _checked_seed(count: int, centroid: Sequence[object]) -> np.ndarray:
    if count < 1 or len(centroid) != DIMENSIONS or not all(_number(one) for one in centroid):
        msg = f"a seed is a count of at least 1 and {DIMENSIONS} numbers"
        raise ValueError(msg)
    unit = _unit(np.asarray(centroid, dtype=np.float32))
    if unit is None:
        msg = "a seed's centroid is not all zero"
        raise ValueError(msg)
    return unit


def seed_study(
    book: VoiceprintBook, device: str, study: object, names: Mapping[str, str],
) -> list[tuple[str, int]]:
    """Seed ``device``'s microphones from the offline study's file; ``(mic, count)`` of each.

    ``study`` is ``{"mics": {"<key>": {"n": int, "centroid": [192 numbers]}}}`` and ``names``
    maps each key to the full name of the microphone (what a terminal sends as ``mic``). Every
    key must be named and every name used, and nothing is written unless all are good.

    Raises:
        ValueError: the file has another shape, a key is unnamed or a name unused, or a
            centroid is refused.
    """
    mics = study.get("mics") if isinstance(study, dict) else None
    if not isinstance(mics, dict) or not mics:
        msg = 'the file is {"mics": {"<key>": {"n": int, "centroid": [192 numbers]}}}'
        raise ValueError(msg)
    if set(mics) != set(names):
        msg = f"the file has mics {sorted(mics)} but --mic names {sorted(names)}"
        raise ValueError(msg)
    seeds: list[tuple[str, int, Sequence[object]]] = []
    for key, entry in mics.items():
        count = entry.get("n") if isinstance(entry, dict) else None
        centroid = entry.get("centroid") if isinstance(entry, dict) else None
        if not isinstance(count, int) or isinstance(count, bool) or not isinstance(centroid, list):
            msg = f'mic {key!r} needs "n" (a whole number) and "centroid" (a list)'
            raise ValueError(msg)  # noqa: TRY004 - a refused file is a ValueError
        seeds.append((names[key], count, centroid))
    for _mic, count, centroid in seeds:  # checked whole first: a bad one writes nothing
        _checked_seed(count, centroid)
    for mic, count, centroid in seeds:
        book.seed(device, mic, count, centroid)
    return [(mic, count) for mic, count, _ in seeds]


def _profile(conn: sqlite3.Connection, device: str, mic: str) -> _Profile | None:
    """The profile of one (device, microphone) from its newest rows; ``None`` with no rows."""
    rows = conn.execute(
        "SELECT weight, vec FROM voiceprints WHERE device = ? AND mic = ? "
        "ORDER BY id DESC LIMIT ?",
        (device, mic, int(WINDOW_WEIGHT)),
    ).fetchall()
    total = np.zeros(DIMENSIONS, dtype=np.float32)
    room = WINDOW_WEIGHT
    for weight, blob in rows:
        take = min(float(weight), room)
        total += np.float32(take) * np.frombuffer(blob, dtype=np.float32)
        room -= take
        if room <= 0:
            break
    unit = _unit(total)
    if unit is None:
        return None
    count, days, seeded = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT substr(ts, 1, 10)), MAX(seed) FROM voiceprints "
        "WHERE device = ? AND mic = ?",
        (device, mic),
    ).fetchone()
    return _Profile(unit, _ready(count, days, seeded=bool(seeded)))
