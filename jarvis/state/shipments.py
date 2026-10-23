"""What the Mac terminal copies into the brain on a schedule (ADR 0294, extending ADR 0259).

Two intakes, both append-only and both answered only after the bytes are safe:

- :meth:`Shipments.receive_entries`: one JSONL body of dated entries for a source in
  :data:`~jarvis.state.imports.SHIPPED_SOURCES`. The body is kept raw whole-or-nothing
  (``imports/<source>/<sha256[:16]>-shipment.jsonl``) and its lines are read into
  ``import_entries`` of ``imports/shipped.db`` (never memory.db) in one transaction; a bad line
  refuses the whole body, never half of it.
  Sending the same bytes again adds nothing and is answered ``already_held``.
- :meth:`Shipments.receive_file`: one binary file (a TimeSink screenshot) under
  ``imports/<source>/files/<relative path>``, written whole or not at all, answered with its
  SHA-256 so the Mac keeps its cursor until the digest matches. Another file source is
  ``claude-code-daily`` (ADR 0303): a Markdown day report (``<date>.md``, at most 256 KiB) or
  the day's kept dialogue (``dialogue/<date>.md``, at most 8 MiB), which may be sent again with
  other bytes (the newest stands) and is read by ``ClaudeDailyIntake``. The third is
  ``claude-code-log`` (ADR 0384): a gzip piece of a Claude Code transcript, named by its own
  content (:mod:`jarvis.state.code_log`), checked against its name and kept for good.

Both stop at the disk floor, like the lifelog archive (ADR 0238), so the brain's databases never
run out of room; the refusal is 507 and the Mac keeps what it holds and tries later.

Layer rules: stdlib and L2 only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import code_log, imports
from jarvis.state.import_parsers import Parsed
from jarvis.state.imports import SHIPPED_SOURCES, Counts, keep_raw
from jarvis.state.lifelog import DEFAULT_MIN_FREE_BYTES

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

MAX_SHIPMENT_BYTES: Final = 16 * 1024 * 1024
MAX_FILE_BYTES: Final = 16 * 1024 * 1024
FILES_DIRNAME: Final = "files"
CODE_DAY_SOURCE: Final = "claude-code-daily"
"""The Mac's day report of his Claude Code work (ADR 0303), shipped as ``<date>.md`` files."""
FILE_SOURCES: Final = ("timesink", CODE_DAY_SOURCE, code_log.SOURCE)
MAX_REPORT_BYTES: Final = 256 * 1024
MAX_DIALOGUE_BYTES: Final = 8 * 1024 * 1024
DIALOGUE_DIRNAME: Final = "dialogue"
_DAY_FILE: Final = r"\d{4}-\d{2}-\d{2}\.md"
SHIPMENT_NAME: Final = "shipment.jsonl"
_MAX_KEY: Final = 1024
_MAX_TEXT: Final = 4 * 1024 * 1024
_MAX_AT_MS: Final = 4_102_444_800_000  # 2100-01-01T00:00:00Z
_PART: Final = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
RELPATH: Final = re.compile(rf"{_PART}(?:/{_PART}){{0,3}}")
_IMAGE_EXT: Final = frozenset({"jpg", "jpeg", "png", "heic", "webp"})


class ShipRefused(ValueError):  # noqa: N818 - a refusal with a status, not a fault
    """A shipment the brain will not take; ``status`` is the HTTP code that fits."""

    def __init__(self, status: int, message: str) -> None:
        """Keep the code beside the sentence that says why."""
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Received:
    """What a body of entries did; never any content."""

    sha256: str
    size: int
    added: int
    held: int
    conflicting: int
    already_held: bool


@dataclass(frozen=True)
class StoredFile:
    """One file as the archive holds it; ``created`` is false when it was already there."""

    sha256: str
    size: int
    created: bool


def _picture(data: bytes) -> bool:
    """Whether ``data`` opens as a JPEG, PNG, HEIC or WebP file."""
    return (
        data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
        or data[4:8] == b"ftyp" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


def _parsed(source: str, number: int, line: bytes) -> Parsed:
    """One shipped line as an entry, or the refusal that names the line (never its values)."""
    where = f"line {number}"
    try:
        raw = json.loads(line)
    except (ValueError, RecursionError):
        raise ShipRefused(422, f"{where} is not JSON") from None
    if not isinstance(raw, dict):
        raise ShipRefused(422, f"{where} is not an object")
    key, kind = raw.get("key"), raw.get("kind")
    text, zone = raw.get("text", ""), raw.get("zone", "UTC")
    at_ms, fields = raw.get("at_ms"), raw.get("fields")
    if not (isinstance(key, str) and 0 < len(key) <= _MAX_KEY):
        raise ShipRefused(422, f"{where} has no usable key")
    if not (isinstance(kind, str) and 0 < len(kind) <= 64):  # noqa: PLR2004
        raise ShipRefused(422, f"{where} has no usable kind")
    if not (isinstance(text, str) and len(text) <= _MAX_TEXT):
        raise ShipRefused(422, f"{where} has no usable text")
    if not (isinstance(zone, str) and 0 < len(zone) <= 64):  # noqa: PLR2004
        raise ShipRefused(422, f"{where} has no usable zone")
    if not (isinstance(at_ms, int) and not isinstance(at_ms, bool) and 0 <= at_ms < _MAX_AT_MS):
        raise ShipRefused(422, f"{where} has no usable at_ms")
    if not isinstance(fields, dict):
        raise ShipRefused(422, f"{where} has no fields object")
    return Parsed(source, key, f"line:{number}", at_ms, zone, kind, text, fields)


def _fsync_write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


class Shipments:
    """The brain's intake for what a terminal ships; one per brain."""

    def __init__(
        self,
        directory: Path,
        *,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        """``directory`` is the imports archive; ``free_bytes`` reads a disk's free space."""
        self.directory = directory
        self.min_free_bytes = min_free_bytes
        self._free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)

    def _room(self, folder: Path, size: int) -> None:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._free_bytes(folder) - size < self.min_free_bytes:
            raise ShipRefused(507, "the disk is too full to keep more")

    def receive_entries(self, source: str, body: bytes) -> Received:
        """Keep ``body`` raw and read its lines into ``import_entries`` of ``shipped.db``.

        Raises:
            ShipRefused: the source is unknown, the body is empty, over the cap, or has a line
                that is not an entry, or the disk is below its floor. Nothing is kept then.
        """
        if source not in SHIPPED_SOURCES:
            raise ShipRefused(404, f"{source!r} is not a source a terminal ships")
        if not body:
            raise ShipRefused(400, "the shipment is empty")
        if len(body) > MAX_SHIPMENT_BYTES:
            raise ShipRefused(413, "the shipment is larger than 16 MiB")
        parsed = [
            _parsed(source, number, line)
            for number, line in enumerate(body.split(b"\n"), start=1) if line.strip()
        ]
        if not parsed:
            raise ShipRefused(400, "the shipment has no lines")
        digest = hashlib.sha256(body).hexdigest()
        folder = self.directory / source
        if not any(folder.glob(f"{digest[:16]}-*")):
            self._room(folder, len(body))
        _, created = keep_raw(self.directory, source, SHIPMENT_NAME, body)
        counts = Counts()
        with contextlib.closing(imports.open_shipped(self.directory)) as conn:
            imports.add_entries(conn, digest, parsed, counts)
            conn.commit()
        return Received(
            digest, len(body), counts.added, counts.held, counts.conflicting,
            already_held=not created and counts.added == 0,
        )

    def receive_file(self, source: str, relpath: str, data: bytes) -> StoredFile:
        """Keep ``data`` at ``<source>/files/<relpath>``, or say why not.

        Raises:
            ShipRefused: the source takes no files, the path cannot name a file here, the bytes
                are not a picture (a UTF-8 Markdown report for ``claude-code-daily``, a piece
                that matches its name for ``claude-code-log``), they are over the cap, a
                picture's path holds other bytes, or the disk is below its floor.
        """
        if source not in FILE_SOURCES:
            raise ShipRefused(404, f"{source!r} ships no files")
        if source == CODE_DAY_SOURCE:
            return self._receive_report(relpath, data)
        if source == code_log.SOURCE:
            return self._receive_code_log(relpath, data)
        if not RELPATH.fullmatch(relpath) or relpath.rsplit(".", 1)[-1].lower() not in _IMAGE_EXT:
            raise ShipRefused(422, "the path is not a relative picture path")
        if not data:
            raise ShipRefused(400, "the file is empty")
        if len(data) > MAX_FILE_BYTES:
            raise ShipRefused(413, "the file is larger than 16 MiB")
        if not _picture(data):
            raise ShipRefused(415, "send a JPEG, PNG, HEIC or WebP picture")
        digest = hashlib.sha256(data).hexdigest()
        target = self.directory / source / FILES_DIRNAME / relpath
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ShipRefused(409, "this path is already kept with other bytes")
            return StoredFile(digest, len(data), created=False)
        self._room(target.parent, len(data))
        partial = target.parent / f".{target.name}.{uuid.uuid4().hex}.part"
        try:
            _fsync_write(partial, data)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        return StoredFile(digest, len(data), created=True)

    def _receive_report(self, relpath: str, data: bytes) -> StoredFile:
        """Keep a day report at ``claude-code-daily/files/<name>``; other bytes replace the old."""
        if (refusal := _report_error(relpath, data)) is not None:
            raise refusal
        digest = hashlib.sha256(data).hexdigest()
        target = self.directory / CODE_DAY_SOURCE / FILES_DIRNAME / relpath
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            return StoredFile(digest, len(data), created=False)
        self._room(target.parent, len(data))
        partial = target.parent / f".{target.name}.{uuid.uuid4().hex}.part"
        try:
            _fsync_write(partial, data)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        return StoredFile(digest, len(data), created=True)

    def _receive_code_log(self, relpath: str, data: bytes) -> StoredFile:
        """Keep a transcript piece at ``claude-code-log/files/<relpath>`` (ADR 0384).

        The name carries the piece's length and digest, so a path already kept holds these
        same raw bytes and is answered as held, with the digest of what was sent.
        """
        if (error := code_log.relpath_error(relpath)) is not None:
            raise ShipRefused(422, error)
        if not data:
            raise ShipRefused(400, "the piece is empty")
        if len(data) > code_log.MAX_PIECE_GZ:
            raise ShipRefused(413, "the piece is larger than 64 MiB")
        try:
            code_log.raw_of(relpath, data)
        except ValueError as exc:
            raise ShipRefused(422, str(exc)) from None
        digest = hashlib.sha256(data).hexdigest()
        target = self.directory / code_log.SOURCE / FILES_DIRNAME / relpath
        if target.exists():
            return StoredFile(digest, len(data), created=False)
        self._room(target.parent, len(data))
        partial = target.parent / f".{target.name}.{uuid.uuid4().hex}.part"
        try:
            _fsync_write(partial, data)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        return StoredFile(digest, len(data), created=True)


def _report_error(relpath: str, data: bytes) -> ShipRefused | None:
    """Why ``data`` is not a day report to keep at ``relpath`` (ADR 0303), or ``None``."""
    kept_dialogue = relpath.startswith(f"{DIALOGUE_DIRNAME}/")
    if not re.fullmatch(rf"(?:{DIALOGUE_DIRNAME}/)?{_DAY_FILE}", relpath):
        return ShipRefused(422, "the path is not <YYYY-MM-DD>.md or dialogue/<YYYY-MM-DD>.md")
    if not data:
        return ShipRefused(400, "the file is empty")
    if len(data) > (MAX_DIALOGUE_BYTES if kept_dialogue else MAX_REPORT_BYTES):
        return ShipRefused(413, "the file is larger than its cap (256 KiB; 8 MiB for dialogue)")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return ShipRefused(415, "the report is not UTF-8 text")
    return None


def answer(received: Received) -> dict[str, Any]:
    """The JSON a terminal is answered with for a body of entries."""
    return {
        "sha256": received.sha256, "bytes": received.size, "added": received.added,
        "held": received.held, "conflicting": received.conflicting,
        "already_held": received.already_held,
    }
