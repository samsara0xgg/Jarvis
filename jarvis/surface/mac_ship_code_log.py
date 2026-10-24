"""Every Claude Code transcript file of both accounts, shipped raw as pieces (ADR 0387).

A pass walks ``projects/`` and ``history.jsonl`` of each account's config directory (links are
not followed, nor any ``tmp/`` folder) and, until each is done, the snapshot folders kept on
2026-10-23. A ``.jsonl`` file ships its new whole lines, cut by
:func:`jarvis.state.code_log.line_cut`; any other file ships whole once it has been quiet
``QUIET_MS``. A piece goes to
``claude-code-log/files/<account>/mac/<path in the config directory>/<piece>`` and the file's
cursor moves only after the brain's digest matches; a file written anew (shorter than its cursor,
or its first or last shipped bytes changed) starts again at 0. The files whose stat differs from
their cursor go oldest first until the pass has shipped ``max_bytes`` or ``max_pieces``.

A snapshot ``.jsonl`` whose live twin holds the same first and last bytes is the twin's: held once
the twin has shipped past its end, waited for until then. Every other snapshot file ships itself,
under the path its twin would have. A snapshot folder whose files are all settled is marked in
``meta`` and not walked again.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_mode
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final

from jarvis.state import code_log
from jarvis.surface.mac_ship_core import SET_CODE_LOG, SET_META, Report, ShipFailed, Sink, State

if TYPE_CHECKING:
    from collections.abc import Iterator

QUIET_MS: Final = 60 * 1000
"""A file that is not ``.jsonl`` ships once it has not changed for this long."""
EDGE_BYTES: Final = 4096
"""How much of a file's start, and of what comes before its cursor, proves it was not rewritten."""
PROJECTS: Final = "projects"
HISTORY: Final = "history.jsonl"
SCRATCH: Final = "tmp"
"""A folder by this name is not entered: a background job's scratch copies of repositories and
caches, not records."""
KEPT: Final = "code-log-kept:"
"""``meta`` key prefix of a snapshot folder that is done."""

Row = tuple[int, int, int, str, str]
"""A file's cursor: ``(size, mtime_ns, offset, head, tail)``; size -1 while it is part shipped."""


@dataclass(frozen=True)
class Tree:
    """One folder of transcript files.

    ``under`` is the folder's path in the config directory: empty for an account's config
    directory itself, of which only ``projects/`` and ``history.jsonl`` are walked, or the
    folder a snapshot is a copy of (``projects``, ``jobs-archive``).
    """

    account: str
    directory: Path
    under: str = ""


@dataclass(frozen=True)
class CodeLogConfig:
    """The live config directories, the snapshot folders, and what one pass may ship."""

    live: tuple[Tree, ...]
    keep: tuple[Tree, ...] = ()
    max_bytes: int = 256 * 1024 * 1024
    max_pieces: int = 2000


@dataclass
class _Budget:
    bytes_left: int
    pieces_left: int

    def spent(self) -> bool:
        return self.bytes_left <= 0 or self.pieces_left <= 0


@dataclass(order=True)
class _Job:
    mtime_ns: int
    path: Path
    account: str = field(compare=False)
    rel: str = field(compare=False)
    stat: os.stat_result = field(compare=False)


def ship(config: CodeLogConfig, state: State, sink: Sink, report: Report, now_ms: int) -> None:
    """One pass over every transcript file not yet shipped as it now stands.

    Raises:
        ShipFailed: the brain did not keep a piece; that file's cursor stays where it was.
    """
    rows: dict[str, Row] = {
        str(found[0]): found[1:] for found in state.conn.execute(
            "SELECT path, size, mtime_ns, offset, head, tail FROM code_log",
        )
    }
    jobs: list[_Job] = []
    for tree in config.live:
        for rel, path, stat in _walk(tree, report):
            if _changed(rows.get(str(path)), stat):
                jobs.append(_Job(stat.st_mtime_ns, path, tree.account, rel, stat))
    live = {tree.account: tree.directory for tree in config.live}
    for tree in config.keep:
        key = KEPT + str(tree.directory)
        if state.meta(key) is None and tree.directory.is_dir():
            jobs.extend(_keep_jobs(tree, live.get(tree.account), rows, state, report, key, now_ms))
    budget = _Budget(config.max_bytes, config.max_pieces)
    for job in sorted(jobs):
        if budget.spent():
            break
        _ship_file(job, rows.get(str(job.path)), state, sink, report, budget, now_ms)


def _keep_jobs(  # noqa: PLR0913 - one snapshot folder, its twin folder and the pass's state
    tree: Tree, twin_dir: Path | None, rows: dict[str, Row], state: State, report: Report,
    key: str, now_ms: int,
) -> list[_Job]:
    """The snapshot files that ship themselves; the folder is marked done when none is left."""
    jobs = []
    settled = True
    for rel, path, stat in _walk(tree, report):
        twin = None if twin_dir is None else _twin(twin_dir, rel)
        verdict = _snapshot(path, stat, rows, twin)
        if verdict == "ship":
            jobs.append(_Job(stat.st_mtime_ns, path, tree.account, rel, stat))
        settled = settled and verdict == "settled"
    if settled:
        state.apply([(SET_META, (key, str(now_ms)))])
    return jobs


def _walk(tree: Tree, report: Report) -> Iterator[tuple[str, Path, os.stat_result]]:
    """``(path in the config directory, file, its lstat)`` of each regular file of ``tree``."""
    starts = (
        [(tree.directory, tree.under)] if tree.under
        else [(tree.directory / PROJECTS, PROJECTS), (tree.directory / HISTORY, HISTORY)]
    )
    for start, name in starts:
        try:
            top = start.lstat()
        except OSError:
            continue
        if stat_mode.S_ISREG(top.st_mode):
            yield name, start, top
            continue
        if not stat_mode.S_ISDIR(top.st_mode):
            continue
        for folder, dirs, files in os.walk(start):  # a link to a folder is listed, not entered
            dirs[:] = sorted(d for d in dirs if d != SCRATCH)
            for file in sorted(files):
                path = Path(folder) / file
                try:
                    stat = path.lstat()
                except OSError:
                    continue
                if not stat_mode.S_ISREG(stat.st_mode):
                    continue
                rel = f"{name}/{path.relative_to(start).as_posix()}"
                if not code_log.file_parts_ok(rel.split("/")):
                    report.skipped["claude-code-log/name"] += 1
                    continue
                yield rel, path, stat


def _changed(row: Row | None, stat: os.stat_result) -> bool:
    return row is None or row[0] != stat.st_size or row[1] != stat.st_mtime_ns


def _twin(config_dir: Path, rel: str) -> Path | None:
    """The live file a snapshot file at ``rel`` is a copy of, when the live walk covers it."""
    return config_dir / rel if rel == HISTORY or rel.startswith(f"{PROJECTS}/") else None


def _edge(log: BinaryIO, start: int, end: int) -> str:
    """``<length>:<sha16>`` of bytes ``[start, end)``, as far as the file reaches."""
    log.seek(start)
    raw = log.read(max(0, end - start))
    return f"{len(raw)}:{code_log.short_sha(raw)}"


def _edges(log: BinaryIO, offset: int) -> tuple[str, str]:
    """The head and the tail a cursor at ``offset`` keeps."""
    return _edge(log, 0, min(EDGE_BYTES, offset)), _edge(log, max(0, offset - EDGE_BYTES), offset)


def _still(log: BinaryIO, size: int, row: Row) -> bool:
    """Whether the file still holds what its cursor shipped: not shorter, same head and tail."""
    offset, head, tail = row[2], row[3], row[4]
    head_len, tail_len = int(head.split(":", 1)[0]), int(tail.split(":", 1)[0])
    return (
        size >= offset and _edge(log, 0, head_len) == head
        and _edge(log, offset - tail_len, offset) == tail
    )


def _snapshot(  # noqa: PLR0911 - one answer per way a copy can stand
    path: Path, stat: os.stat_result, rows: dict[str, Row], twin: Path | None,
) -> str:
    """``settled``, ``wait`` (its live twin will cover it) or ``ship`` for one snapshot file."""
    own = rows.get(str(path))
    if stat.st_size == 0 or (own is not None and not _changed(own, stat)):
        return "settled"
    if twin is None or not path.name.endswith(".jsonl"):
        return "ship"
    try:
        twin_stat = twin.lstat()
        if not stat_mode.S_ISREG(twin_stat.st_mode) or twin_stat.st_size < stat.st_size:
            return "ship"
        with path.open("rb") as copy, twin.open("rb") as original:
            size = stat.st_size
            if _edges(copy, size) != _edges(original, size):
                return "ship"
    except OSError:
        return "ship"
    held = rows.get(str(twin))
    if held is not None and (held[2] >= stat.st_size or not _changed(held, twin_stat)):
        return "settled"  # shipped past it, or as far as the twin's whole lines go
    return "wait"


def _line_end(log: BinaryIO, start: int) -> int | None:
    """Just past the first line end at or after ``start``; ``None`` while that line is unended."""
    log.seek(start)
    while chunk := log.read(code_log.TARGET_PIECE_BYTES):
        found = chunk.find(b"\n")
        if found >= 0:
            return start + found + 1
        start += len(chunk)
    return None


def _next_piece(log: BinaryIO, offset: int, *, jsonl: bool) -> bytes | int | None:
    """The raw bytes of the next piece from ``offset``, ``None`` when there is none yet, or an int.

    A positive int is how many bytes can never be one piece and are passed over (a line over
    48 MiB, so the lines after it still ship); ``-1`` is a whole other file over 48 MiB.
    """
    log.seek(offset)
    if not jsonl:
        raw = log.read(code_log.MAX_PIECE_RAW + 1)
        if len(raw) > code_log.MAX_PIECE_RAW:
            return -1
        return raw or None
    raw = log.read(code_log.TARGET_PIECE_BYTES)
    cut = code_log.line_cut(raw)
    if cut is None and len(raw) == code_log.TARGET_PIECE_BYTES:
        log.seek(offset)
        raw = log.read(code_log.MAX_PIECE_RAW)
        cut = code_log.line_cut(raw)
    if cut == -1:
        end = _line_end(log, offset + code_log.MAX_PIECE_RAW)
        return None if end is None else end - offset
    return None if cut is None else raw[:cut]


def _ship_file(  # noqa: PLR0913 - one file: the job, its cursor, the pass's state, sink, report, budget and clock
    job: _Job, row: Row | None, state: State, sink: Sink, report: Report, budget: _Budget,
    now_ms: int,
) -> None:
    """Ship the pieces ``job``'s file has pending, moving its cursor after each one is kept."""
    jsonl = job.path.name.endswith(".jsonl")
    if not jsonl and now_ms - job.stat.st_mtime_ns // 1_000_000 < QUIET_MS:
        return
    path = str(job.path)
    try:
        log = job.path.open("rb")
    except OSError:
        return
    with log:
        offset = row[2] if row is not None and _still(log, job.stat.st_size, row) else 0
        while True:
            if budget.spent():
                return  # the cursor's size stays -1, so the next pass carries on here
            piece = _next_piece(log, offset, jsonl=jsonl)
            if piece is None:
                break
            if isinstance(piece, int):
                report.skipped["claude-code-log/too-large"] += 1
                if piece < 0:
                    break
                offset += piece
                state.apply([(SET_CODE_LOG, (path, -1, -1, offset, *_edges(log, offset)))])
                continue
            data = code_log.compress(piece)
            relpath = f"{job.account}/mac/{job.rel}/{code_log.piece_name(offset, piece)}"
            if sink.send_file(code_log.SOURCE, relpath, data) != hashlib.sha256(data).hexdigest():
                msg = "the brain's digest of a transcript piece does not match"
                raise ShipFailed(msg)
            offset += len(piece)
            state.apply([(SET_CODE_LOG, (path, -1, -1, offset, *_edges(log, offset)))])
            budget.bytes_left -= len(piece)
            budget.pieces_left -= 1
            report.files += 1
            report.file_bytes += len(data)
            report.file_original_bytes += len(piece)
            if not jsonl:
                break
        state.apply([(SET_CODE_LOG, (
            path, job.stat.st_size, job.stat.st_mtime_ns, offset, *_edges(log, offset),
        ))])
