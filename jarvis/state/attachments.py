"""What a phone attaches to a turn or shares from another app (ADR 0211).

One directory under the runtime root holds the files, kept like her other media: deleted by age
in the hourly sweep, in the export, cleared with the recordings. A file is named
``<id>__<name>.<ext>``; the id is 32 lowercase hex characters and is all a turn carries, so the
event log holds a reference and not the bytes. The kind is read from the first bytes, never from
the type the sender claims.

Layer rules: stdlib only (L2).
"""

from __future__ import annotations

import base64
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

ATTACHMENTS_DIRNAME: Final = "attachments"
"""Under ``artifacts/`` in the runtime root."""

MAX_IMAGE_BYTES: Final = 4 * 1024 * 1024
"""The cap a terminal's screenshot already has (ADR 0170)."""
MAX_TEXT_BYTES: Final = 256 * 1024
MAX_TEXT_TO_MODEL_CHARS: Final = 20_000
MAX_PER_TURN: Final = 4
MAX_IMAGE_BYTES_PER_TURN: Final = 8 * 1024 * 1024
MAX_STORE_BYTES: Final = 512 * 1024 * 1024
MAX_UPLOAD_BODY_BYTES: Final = MAX_IMAGE_BYTES + 64 * 1024
"""The largest multipart body worth reading: one file plus its framing."""

_ID: Final = re.compile(r"[0-9a-f]{32}")
_NAME_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._-]+")
_NAME_CHARS: Final = 60

Kind = Literal["image", "text"]

_IMAGE_TYPES: Final[tuple[tuple[bytes, str, str], ...]] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)
_HEIC_BRANDS: Final = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim"})


class AttachmentRefused(ValueError):  # noqa: N818 — a refusal with a status, not a fault
    """A file or a request the store will not take; ``status`` is the HTTP code that fits."""

    def __init__(self, status: int, message: str) -> None:
        """Keep the code beside the sentence that says why."""
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AttachmentRef:
    """One stored file as the routes and the record see it."""

    id: str
    kind: Kind
    name: str
    size: int
    mime: str


@dataclass(frozen=True)
class LoadedAttachment:
    """A stored file read for a model request: ``data`` for an image, ``text`` for a file."""

    ref: AttachmentRef
    data: bytes = b""
    text: str = ""

    def data_url(self) -> str:
        """The image as a ``data:`` URL, which is how a chat request carries it."""
        return f"data:{self.ref.mime};base64," + base64.b64encode(self.data).decode("ascii")


def valid_id(value: object) -> bool:
    """Whether ``value`` is the shape of an attachment id."""
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _image_kind(data: bytes) -> tuple[str, str] | None:
    for magic, mime, ext in _IMAGE_TYPES:
        if data.startswith(magic):
            return mime, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _text_of(data: bytes) -> str | None:
    """The file as text, or ``None`` when it is binary or not UTF-8."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(unicodedata.category(c) == "Cc" and c not in "\t\n\r\f" for c in text):
        return None
    return text


def _safe_stem(name: str) -> str:
    stem = name.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return _NAME_UNSAFE.sub("-", stem).strip("-.")[:_NAME_CHARS] or "file"


def _disk_name(attachment_id: str, stem: str, ext: str) -> str:
    return f"{attachment_id}__{stem}.{ext}"


def _ref_of(path: Path) -> AttachmentRef | None:
    attachment_id, separator, rest = path.name.partition("__")
    if not separator or not valid_id(attachment_id):
        return None
    stem, _, ext = rest.rpartition(".")
    for _magic, mime, known in _IMAGE_TYPES:
        if known == ext:
            return AttachmentRef(attachment_id, "image", f"{stem}.{ext}", path.stat().st_size, mime)
    if ext == "webp":
        return AttachmentRef(attachment_id, "image", f"{stem}.{ext}", path.stat().st_size,
                             "image/webp")
    return AttachmentRef(attachment_id, "text", f"{stem}.{ext}", path.stat().st_size, "text/plain")


class Attachments:
    """The directory of attached files; every method touches only that directory."""

    def __init__(self, directory: Path) -> None:
        """``directory`` need not exist yet; the first save makes it (0700)."""
        self.directory = directory

    def save(self, data: bytes, filename: str, *, images_ok: bool) -> AttachmentRef:
        """Store one uploaded file, or raise :class:`AttachmentRefused` saying why not.

        ``images_ok`` is whether the conversation model takes pictures; without it a picture is
        refused here, before it takes any space.
        """
        if not data:
            raise AttachmentRefused(400, "the file is empty")
        if data.startswith(b"%PDF-"):
            raise AttachmentRefused(
                415, "PDF is not supported: no PDF reader is installed; send the text or a picture",
            )
        if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
            raise AttachmentRefused(415, "HEIC is not supported; send the picture as JPEG or PNG")
        image = _image_kind(data)
        if image is not None:
            if not images_ok:
                raise AttachmentRefused(422, "the conversation model takes no pictures")
            if len(data) > MAX_IMAGE_BYTES:
                raise AttachmentRefused(
                    413, f"the picture is over {MAX_IMAGE_BYTES // (1024 * 1024)} MiB; shrink it",
                )
            mime, ext = image
            kind: Kind = "image"
        else:
            if _text_of(data) is None:
                raise AttachmentRefused(
                    415, "this file type is not supported; send a picture or a plain-text file",
                )
            if len(data) > MAX_TEXT_BYTES:
                raise AttachmentRefused(
                    413, f"the text file is over {MAX_TEXT_BYTES // 1024} KiB",
                )
            mime, ext, kind = "text/plain", "txt", "text"
        if self._used_bytes() + len(data) > MAX_STORE_BYTES:
            raise AttachmentRefused(507, "the attachment store is full; it empties with age")
        attachment_id = uuid.uuid4().hex
        stem = _safe_stem(filename)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.directory / _disk_name(attachment_id, stem, ext)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as sink:
            sink.write(data)
        return AttachmentRef(attachment_id, kind, f"{stem}.{ext}", len(data), mime)

    def _used_bytes(self) -> int:
        if not self.directory.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())

    def _path_of(self, attachment_id: str) -> Path | None:
        if not valid_id(attachment_id) or not self.directory.is_dir():
            return None
        return next(self.directory.glob(f"{attachment_id}__*"), None)

    def get(self, attachment_id: str) -> AttachmentRef | None:
        """The stored file's description, or ``None`` when it is gone or never was."""
        path = self._path_of(attachment_id)
        return None if path is None else _ref_of(path)

    def load(self, attachment_id: str) -> LoadedAttachment | None:
        """The file read for a request, or ``None`` when it is gone."""
        path = self._path_of(attachment_id)
        ref = None if path is None else _ref_of(path)
        if path is None or ref is None:
            return None
        data = path.read_bytes()
        if ref.kind == "image":
            return LoadedAttachment(ref, data=data)
        return LoadedAttachment(ref, text=data.decode("utf-8", errors="replace"))

    def load_many(self, ids: Iterable[str]) -> list[LoadedAttachment | None]:
        """One entry per id, in order; ``None`` for one that is gone."""
        return [self.load(attachment_id) for attachment_id in ids]

    def check_for_turn(self, ids: Sequence[str]) -> list[AttachmentRef]:
        """The refs of ``ids`` for one turn, or :class:`AttachmentRefused` for the turn's limits."""
        if len(ids) > MAX_PER_TURN:
            raise AttachmentRefused(422, f"at most {MAX_PER_TURN} attachments go with one turn")
        refs: list[AttachmentRef] = []
        for attachment_id in dict.fromkeys(ids):
            ref = self.get(attachment_id)
            if ref is None:
                raise AttachmentRefused(404, "an attachment is unknown or has expired")
            refs.append(ref)
        if sum(r.size for r in refs if r.kind == "image") > MAX_IMAGE_BYTES_PER_TURN:
            raise AttachmentRefused(
                422, f"pictures over {MAX_IMAGE_BYTES_PER_TURN // (1024 * 1024)} MiB in one turn",
            )
        return refs


def marker(refs: Iterable[AttachmentRef | None]) -> str:
    """The line history and the memory record keep in place of the file itself."""
    parts = [
        "an attachment that has expired" if ref is None else f"{ref.kind} {ref.name}"
        for ref in refs
    ]
    return f"[attached: {', '.join(parts)}]" if parts else ""
