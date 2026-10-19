"""What his phone records through the day, kept as the raw archive (ADR 0238).

A paired phone records while he has the switch on and sends each piece of audio here once it is
closed. One directory holds the pieces and keeps them for good: no age sweep, not in the export
zip, not in "clear recordings"; erasing all data removes them only while the directory sits under
the runtime root. A piece is filed by the device that sent it and the UTC moment it started, as
``audio/<device>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSS.fffZ>_<duration>ms_<id>.<ext>``, so the archive
reads without an index. The container is read from the first bytes, never from what the sender
claims; the codec inside it is the phone's choice.

A piece is written whole or not at all (a temporary file, fsynced, then renamed), so a stored
name always holds complete bytes and the phone may delete its copy once the answer's SHA-256
matches its own. Sending the same piece again is answered from what is stored.

Layer rules: stdlib only (L2).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

LIFELOG_DIRNAME: Final = "lifelog"
"""Under the runtime root unless ``lifelog.dir`` names another place."""
AUDIO_DIRNAME: Final = "audio"

MAX_PIECE_BYTES: Final = 32 * 1024 * 1024
"""Ten minutes of stereo at 256 kbps is 19 MB; a phone sends pieces of about ten minutes."""
MAX_DURATION_MS: Final = 60 * 60 * 1000
MIN_STARTED_MS: Final = 1_577_836_800_000  # 2020-01-01T00:00:00Z
MAX_STARTED_MS: Final = 4_102_444_800_000  # 2100-01-01T00:00:00Z
DEFAULT_MIN_FREE_BYTES: Final = 5 * 1024 * 1024 * 1024
"""What a piece may not take the disk below, so the brain's own databases never run it dry."""

COVERAGE_FRESH_MS: Final = 15 * 60 * 1000
"""A terminal whose newest archived piece ended this recently is recording now (ADR 0292):
a piece is cut every ten minutes and sent when it closes, so a recording terminal's newest
kept piece is never much older than that."""

PIECE_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DEVICE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PIECE_FILE: Final = re.compile(r"(\d{8}T\d{6}\.\d{3})Z_(\d+)ms_")


class PieceRefused(ValueError):  # noqa: N818 — a refusal with a status, not a fault
    """A piece the archive will not take; ``status`` is the HTTP code that fits."""

    def __init__(self, status: int, message: str) -> None:
        """Keep the code beside the sentence that says why."""
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class StoredPiece:
    """One piece as the archive holds it; ``created`` is false when it was already there."""

    id: str
    sha256: str
    size: int
    created: bool


def container_ext(data: bytes) -> str | None:
    """The file extension of the audio container ``data`` opens with, or ``None``."""
    if data[:4] == b"caff":
        return "caf"
    if data[:4] == b"OggS":
        return "ogg"
    if data[4:8] == b"ftyp":
        return "m4a"
    return None


def piece_name(started_ms: int, duration_ms: int, piece_id: str, ext: str) -> str:
    """The file name a piece is kept under: its UTC start, its length and its id."""
    start = datetime.fromtimestamp(started_ms / 1000, tz=UTC)
    stamp = start.strftime("%Y%m%dT%H%M%S") + f".{started_ms % 1000:03d}Z"
    return f"{stamp}_{duration_ms}ms_{piece_id}.{ext}"


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_dirs(directory: Path) -> None:
    """``directory`` and its missing parents, each made 0700."""
    missing = [p for p in (directory, *directory.parents) if not p.exists()]
    for path in reversed(missing):
        path.mkdir(mode=0o700, exist_ok=True)


class Lifelog:
    """The archive of his recorded days; one per brain."""

    def __init__(
        self,
        directory: Path,
        *,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        """``free_bytes`` reads the free space of the disk a path is on (tests replace it)."""
        self.directory = directory
        self.min_free_bytes = min_free_bytes
        self._free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)

    def save_audio(
        self, device: str, piece_id: str, started_ms: int, duration_ms: int, data: bytes,
    ) -> StoredPiece:
        """Keep one piece of ``device``'s audio, or say why not with a :class:`PieceRefused`."""
        ext = _checked(device, piece_id, started_ms, duration_ms, data)
        digest = hashlib.sha256(data).hexdigest()
        day = datetime.fromtimestamp(started_ms / 1000, tz=UTC).strftime("%Y-%m-%d")
        folder = self.directory / AUDIO_DIRNAME / device / day
        name = piece_name(started_ms, duration_ms, piece_id, ext)
        _make_dirs(folder)
        kept = [p for p in folder.glob(f"*_{piece_id}.*") if not p.name.startswith(".")]
        if kept:
            if kept[0].name != name:
                raise PieceRefused(409, "this id is already kept with another start or length")
            if hashlib.sha256(kept[0].read_bytes()).hexdigest() != digest:
                raise PieceRefused(409, "this id is already kept with other bytes")
            return StoredPiece(piece_id, digest, len(data), created=False)
        if self._free_bytes(folder) - len(data) < self.min_free_bytes:
            raise PieceRefused(507, "the disk is too full to keep more recordings")
        partial = folder / f".{name}.{uuid.uuid4().hex}.part"
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            partial.replace(folder / name)
        finally:
            partial.unlink(missing_ok=True)
        _fsync_dir(folder)
        return StoredPiece(piece_id, digest, len(data), created=True)

    def newest_end_ms(self, device: str) -> int | None:
        """When the newest piece kept for ``device`` ended (UTC ms), read from the file names."""
        if not _DEVICE.fullmatch(device):
            return None
        folder = self.directory / AUDIO_DIRNAME / device
        try:
            days = sorted((p for p in folder.iterdir() if p.is_dir()), reverse=True)
        except OSError:
            return None
        for day in days:  # the newest day can hold only temporary files
            ends = [e for p in day.iterdir() if (e := _end_ms(p.name)) is not None]
            if ends:
                return max(ends)
        return None

    def coverage(
        self, terminals: Iterable[str], home: Mapping[str, float] | None, now_ms: int,
    ) -> dict[str, Any]:
        """Facts the phone decides by (ADR 0292): a terminal recording now, and where home is.

        A terminal counts as recording when it is connected and its newest kept piece ended
        within :data:`COVERAGE_FRESH_MS`. ``terminal`` and ``newest_piece_end_ms`` are those of
        the connected terminal with the newest piece, recording or not; both are ``None`` when
        no connected terminal has a piece. ``home`` is passed on as it is (``None`` if unknown).
        """
        newest: tuple[int, str] | None = None
        for name in terminals:
            end = self.newest_end_ms(name)
            if end is not None and (newest is None or end > newest[0]):
                newest = (end, name)
        return {
            "now_ms": now_ms,
            "mac_recording": newest is not None and now_ms - newest[0] <= COVERAGE_FRESH_MS,
            "terminal": None if newest is None else newest[1],
            "newest_piece_end_ms": None if newest is None else newest[0],
            "home": None if home is None else dict(home),
        }


def _end_ms(name: str) -> int | None:
    """The UTC millisecond a piece's file name says it ended, or ``None`` for any other name."""
    found = _PIECE_FILE.match(name)
    if found is None:
        return None
    start = datetime.strptime(found[1], "%Y%m%dT%H%M%S.%f").replace(tzinfo=UTC)
    return round(start.timestamp() * 1000) + int(found[2])


def _checked(device: str, piece_id: str, started_ms: int, duration_ms: int, data: bytes) -> str:
    """The container extension of a piece the archive takes, or the :class:`PieceRefused`."""
    if not _DEVICE.fullmatch(device):
        raise PieceRefused(400, "the device name cannot name a folder")
    if not PIECE_ID.fullmatch(piece_id):
        raise PieceRefused(422, "the id is 1 to 64 of A-Z a-z 0-9 _ -")
    if not MIN_STARTED_MS <= started_ms < MAX_STARTED_MS:
        raise PieceRefused(422, "started_ms is not a moment between 2020 and 2100")
    if not 0 < duration_ms <= MAX_DURATION_MS:
        raise PieceRefused(422, "duration_ms is not between 1 ms and an hour")
    if not data:
        raise PieceRefused(400, "the piece is empty")
    if len(data) > MAX_PIECE_BYTES:
        raise PieceRefused(413, "the piece is larger than 32 MiB")
    ext = container_ext(data)
    if ext is None:
        raise PieceRefused(415, "send audio in a CAF, Ogg or MPEG-4 container")
    return ext
