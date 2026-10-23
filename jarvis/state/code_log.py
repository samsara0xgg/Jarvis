"""Every Claude Code transcript, kept raw on the brain as gzip pieces (ADR 0384).

A piece is the bytes ``[start, end)`` of one transcript file, gzip-compressed, kept under
``<account>/<origin>/<path in the config directory>/<start>-<end>-<sha>.gz`` where ``<sha>`` is
the first 16 hex of the SHA-256 of the raw bytes and ``<origin>`` is ``mac`` or
``cloud-<session id>``. A ``.jsonl`` piece ends at a line end. The name is the content: the same
piece sent again is the same file, and a file written anew starts again at 0 under new names, so
nothing is ever overwritten.

This module holds the rule both sides share: what a piece may be called and hold, how a terminal
cuts pending bytes into pieces, and how a reader merges a file's pieces back into lines.

Layer rules: stdlib only (L2).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import zlib
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

SOURCE: Final = "claude-code-log"
"""The imports source the pieces arrive under (``PUT /inherent/imports/claude-code-log/files/..``)."""
TARGET_PIECE_BYTES: Final = 8 * 1024 * 1024
"""A terminal cuts a piece at the last line end within this many raw bytes."""
MAX_PIECE_RAW: Final = 48 * 1024 * 1024
"""No piece holds more raw bytes; a single line longer than this cannot be sent."""
MAX_PIECE_GZ: Final = 64 * 1024 * 1024
"""What the route reads of one piece's body."""
SHA_CHARS: Final = 16
MAX_PATH_PARTS: Final = 8

_ACCOUNT: Final = re.compile(r"[a-z0-9]{1,16}")
_ORIGIN: Final = re.compile(r"mac|cloud-[A-Za-z0-9_]{1,80}")
_PART: Final = re.compile(r"[A-Za-z0-9._-]{1,255}")
_PIECE: Final = re.compile(rf"(\d{{1,15}})-(\d{{1,15}})-([0-9a-f]{{{SHA_CHARS}}})\.gz")


def short_sha(raw: bytes) -> str:
    """The digest a piece's name carries."""
    return hashlib.sha256(raw).hexdigest()[:SHA_CHARS]


def piece_name(start: int, raw: bytes) -> str:
    """The file name of the piece holding ``raw`` from byte ``start`` of its file."""
    return f"{start}-{start + len(raw)}-{short_sha(raw)}.gz"


def compress(raw: bytes) -> bytes:
    """``raw`` as one gzip member with no timestamp, so the same bytes compress the same."""
    return gzip.compress(raw, compresslevel=6, mtime=0)


def file_parts_ok(parts: tuple[str, ...] | list[str]) -> bool:
    """Whether ``parts`` (a path inside a config directory) can name a folder in the archive."""
    return 0 < len(parts) <= MAX_PATH_PARTS and all(
        _PART.fullmatch(p) and p not in {".", ".."} for p in parts
    )


def relpath_error(relpath: str) -> str | None:
    """Why ``relpath`` cannot name a piece, or ``None``."""
    parts = relpath.split("/")
    if len(parts) < 4:  # noqa: PLR2004 - account, origin, at least one path part, the piece
        return "the path is not <account>/<origin>/<path>/<piece>"
    account, origin, *inner, name = parts
    if not _ACCOUNT.fullmatch(account):
        return "the account is 1 to 16 of a-z 0-9"
    if not _ORIGIN.fullmatch(origin):
        return "the origin is mac or cloud-<session id>"
    if not file_parts_ok(inner):
        return "the file's path has parts outside A-Za-z0-9._- or too many of them"
    found = _PIECE.fullmatch(name)
    if found is None:
        return "the piece is not named <start>-<end>-<sha16>.gz"
    if int(found[2]) <= int(found[1]):
        return "the piece ends before it starts"
    return None


def raw_of(relpath: str, data: bytes) -> bytes:
    """The raw bytes of a piece whose name :func:`relpath_error` accepted, checked against it.

    Raises:
        ValueError: ``data`` is not one gzip member of at most :data:`MAX_PIECE_RAW` bytes whose
            length and digest are the ones its name says, or a ``.jsonl`` piece does not end
            at a line end. The message names the fault, never the content.
    """
    *_, file_name, name = relpath.split("/")
    found = _PIECE.fullmatch(name)
    if found is None:
        msg = "the piece is not named <start>-<end>-<sha16>.gz"
        raise ValueError(msg)
    inflate = zlib.decompressobj(zlib.MAX_WBITS | 16)
    try:
        raw = inflate.decompress(data, MAX_PIECE_RAW + 1)
    except zlib.error:
        msg = "the piece is not gzip"
        raise ValueError(msg) from None
    if len(raw) > MAX_PIECE_RAW or inflate.unconsumed_tail:
        msg = "the piece holds more than 48 MiB"
        raise ValueError(msg)
    if not inflate.eof or inflate.unused_data:
        msg = "the piece is not one whole gzip member"
        raise ValueError(msg)
    if len(raw) != int(found[2]) - int(found[1]):
        msg = "the piece's length is not the one its name says"
        raise ValueError(msg)
    if short_sha(raw) != found[3]:
        msg = "the piece's digest is not the one its name says"
        raise ValueError(msg)
    if file_name.endswith(".jsonl") and not raw.endswith(b"\n"):
        msg = "a .jsonl piece ends in the middle of a line"
        raise ValueError(msg)
    return raw


def line_cut(pending: bytes) -> int | None:
    """How many bytes of ``pending`` (a ``.jsonl`` file's unsent bytes) the next piece takes.

    The piece ends at the last line end within :data:`TARGET_PIECE_BYTES`; a first line longer
    than that is taken whole up to :data:`MAX_PIECE_RAW`. ``None`` when no whole line is pending
    yet (a line still being written waits for its end), and ``-1`` when ``pending`` holds
    :data:`MAX_PIECE_RAW` bytes of a single line, longer than any piece may be.
    """
    end = pending.rfind(b"\n", 0, TARGET_PIECE_BYTES)
    if end >= 0:
        return end + 1
    first = pending.find(b"\n", 0, MAX_PIECE_RAW)
    if first >= 0:
        return first + 1
    return -1 if len(pending) >= MAX_PIECE_RAW else None


def _key(line: bytes) -> str:
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        record = None
    if isinstance(record, dict) and isinstance(record.get("uuid"), str):
        return f"uuid:{record['uuid']}"
    return "line:" + hashlib.sha256(line).hexdigest()


def assemble(folder: Path) -> bytes:
    """One transcript file read back from its pieces in ``folder``, lines merged (ADR 0384).

    Pieces are read in start order; a line with a ``uuid`` is kept once per uuid and any other
    line once per exact bytes, so a file shipped twice (a cloud VM written anew) reads once.
    """
    pieces: list[tuple[int, int, str, Path]] = []
    for path in folder.iterdir():
        found = _PIECE.fullmatch(path.name)
        if found is not None and path.is_file():
            pieces.append((int(found[1]), int(found[2]), found[3], path))
    seen: set[str] = set()
    out: list[bytes] = []
    for *_, path in sorted(pieces):
        for line in gzip.decompress(path.read_bytes()).splitlines(keepends=True):
            key = _key(line)
            if key not in seen:
                seen.add(key)
                out.append(line)
    return b"".join(out)
