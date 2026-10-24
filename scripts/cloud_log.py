#!/usr/bin/env python3
"""Ship this cloud VM's Claude Code transcripts to the private relay repository (ADR 0387).

    python3 cloud_log.py install --account a --recipient age1... [--url URL]
    python3 cloud_log.py start
    python3 cloud_log.py watch

``install`` (the environment's setup script runs it, as root) copies this file to
``~/.jarvis-cloud-log/cloud_log.py``, writes ``config.json`` beside it and makes each start of
Claude Code start the watcher: a line in ``~/.bashrc`` and a SessionStart hook in
``~/.claude/settings.json``, each added once. ``start`` prints nothing and returns at once,
leaving one detached watcher per VM (a lock decides which). The watcher reads ``projects/`` and
``history.jsonl`` of ``~/.claude``, cuts each file's new bytes into pieces by the rule the brain
checks (``jarvis/state/code_log.py``; repeated here because this file runs alone, and a test
holds the two equal), encrypts each with ``age`` to the Mac's key and pushes a batch as a new
branch ``s/<session id>/<epoch ms>`` once the files have been quiet ``QUIET_MS``, or every
``EVERY_MS`` while they grow. Its cursors move only after the push. Standard library only.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import stat as stat_mode
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO, Final

DEFAULT_URL: Final = "https://github.com/samsara0xgg/jarvis-cloud-log.git"
HOME_DIR: Final = ".jarvis-cloud-log"
MARK: Final = "# jarvis-cloud-log"
QUIET_MS: Final = 45 * 1000
"""A batch goes once every file with new lines has been quiet this long..."""
EVERY_MS: Final = 10 * 60 * 1000
"""...or when this long has passed since the last one."""
FILE_QUIET_MS: Final = 60 * 1000
"""A file that is not ``.jsonl`` ships whole once it has not changed for this long."""
BATCH_BYTES: Final = 32 * 1024 * 1024
TICK_S: Final = 15
RETRY_S: Final = 60
EDGE_BYTES: Final = 4096
TARGET_PIECE_BYTES: Final = 8 * 1024 * 1024
MAX_PIECE_RAW: Final = 48 * 1024 * 1024
SHA_CHARS: Final = 16
MAX_PATH_PARTS: Final = 8
_PART: Final = re.compile(r"[A-Za-z0-9._-]{1,255}")
_ACCOUNT: Final = re.compile(r"[a-z0-9]{1,16}")
_SESSION: Final = re.compile(r"[A-Za-z0-9_]{1,80}")
_RECIPIENT: Final = re.compile(r"age1[0-9a-z]{20,100}")
_GIT_S: Final = 300
_ENV: Final = {
    "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "jarvis", "GIT_AUTHOR_EMAIL": "jarvis@localhost",
    "GIT_COMMITTER_NAME": "jarvis", "GIT_COMMITTER_EMAIL": "jarvis@localhost",
}


# --- the piece rule, the same as jarvis/state/code_log.py --------------------------------------


def short_sha(raw: bytes) -> str:
    """The digest a piece's name carries."""
    return hashlib.sha256(raw).hexdigest()[:SHA_CHARS]


def piece_name(start: int, raw: bytes) -> str:
    """The file name of the piece holding ``raw`` from byte ``start`` of its file."""
    return f"{start}-{start + len(raw)}-{short_sha(raw)}.gz"


def compress(raw: bytes) -> bytes:
    """``raw`` as one gzip member with no timestamp."""
    return gzip.compress(raw, compresslevel=6, mtime=0)


def file_parts_ok(parts: list[str]) -> bool:
    """Whether ``parts`` (a path inside the config directory) can name a folder in the archive."""
    return 0 < len(parts) <= MAX_PATH_PARTS and all(
        _PART.fullmatch(p) and p not in {".", ".."} for p in parts
    )


def line_cut(pending: bytes) -> int | None:
    """How many bytes of a ``.jsonl`` file's unsent bytes the next piece takes (see code_log)."""
    end = pending.rfind(b"\n", 0, TARGET_PIECE_BYTES)
    if end >= 0:
        return end + 1
    first = pending.find(b"\n", 0, MAX_PIECE_RAW)
    if first >= 0:
        return first + 1
    return -1 if len(pending) >= MAX_PIECE_RAW else None


def _line_end(log: BinaryIO, start: int) -> int | None:
    log.seek(start)
    while chunk := log.read(TARGET_PIECE_BYTES):
        found = chunk.find(b"\n")
        if found >= 0:
            return start + found + 1
        start += len(chunk)
    return None


def next_piece(log: BinaryIO, offset: int, *, jsonl: bool) -> bytes | int | None:
    """The raw bytes of the next piece from ``offset``, ``None`` when there is none yet, or an int.

    A positive int is how many bytes can never be one piece and are passed over (a line over
    48 MiB, so the lines after it still ship); ``-1`` is a whole other file over 48 MiB.
    """
    log.seek(offset)
    if not jsonl:
        raw = log.read(MAX_PIECE_RAW + 1)
        if len(raw) > MAX_PIECE_RAW:
            return -1
        return raw or None
    raw = log.read(TARGET_PIECE_BYTES)
    cut = line_cut(raw)
    if cut is None and len(raw) == TARGET_PIECE_BYTES:
        log.seek(offset)
        raw = log.read(MAX_PIECE_RAW)
        cut = line_cut(raw)
    if cut == -1:
        end = _line_end(log, offset + MAX_PIECE_RAW)
        return None if end is None else end - offset
    return None if cut is None else raw[:cut]


def _edge(log: BinaryIO, start: int, end: int) -> str:
    log.seek(start)
    raw = log.read(max(0, end - start))
    return f"{len(raw)}:{short_sha(raw)}"


def _edges(log: BinaryIO, offset: int) -> list[str]:
    return [_edge(log, 0, min(EDGE_BYTES, offset)), _edge(log, max(0, offset - EDGE_BYTES), offset)]


def _still(log: BinaryIO, size: int, offset: int, head: str, tail: str) -> bool:
    head_len, tail_len = int(head.split(":", 1)[0]), int(tail.split(":", 1)[0])
    return (
        size >= offset and _edge(log, 0, head_len) == head
        and _edge(log, offset - tail_len, offset) == tail
    )


# --- the watcher -------------------------------------------------------------------------------


def _git(repo: Path, *args: str, stdin: bytes = b"", env: dict[str, str] | None = None) -> bytes:
    done = subprocess.run(  # noqa: S603 - fixed argv on the watcher's own repository
        ["git", "-C", str(repo), *args], input=stdin, capture_output=True,  # noqa: S607 - the VM's git
        timeout=_GIT_S, check=False, env={**os.environ, **_ENV, **(env or {})},
    )
    if done.returncode != 0:
        said = done.stderr.decode(errors="replace")[-300:]
        msg = f"git {args[0]} failed ({done.returncode}): {said}"
        raise RuntimeError(msg)
    return done.stdout


class Watcher:
    """One VM's transcripts and what of them has been pushed."""

    def __init__(self, config: dict[str, Any], session: str, home: Path) -> None:
        """``config`` is what ``install`` wrote; ``home`` holds the state and the repository."""
        self.account = str(config["account"])
        self.recipient = str(config["recipient"])
        self.url = str(config.get("url") or DEFAULT_URL)
        self.claude_dir = Path(str(config.get("claude_dir") or Path.home() / ".claude"))
        self.age = str(config.get("age") or "age")
        self.session = session
        self.home = home
        self.repo = home / "repo"
        self.state_path = home / "state.json"
        try:
            self.state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            self.state = {"files": {}, "last_sent_ms": 0}
        self._main = False

    def _files(self) -> list[tuple[str, Path, os.stat_result]]:
        found = []
        starts = [(self.claude_dir / "projects", "projects"),
                  (self.claude_dir / "history.jsonl", "history.jsonl")]
        for start, name in starts:
            try:
                top = start.lstat()
            except OSError:
                continue
            if stat_mode.S_ISREG(top.st_mode):
                found.append((name, start, top))
                continue
            if not stat_mode.S_ISDIR(top.st_mode):
                continue
            for folder, dirs, files in os.walk(start):
                dirs.sort()
                for file in sorted(files):
                    path = Path(folder) / file
                    try:
                        stat = path.lstat()
                    except OSError:
                        continue
                    rel = f"{name}/{path.relative_to(start).as_posix()}"
                    if stat_mode.S_ISREG(stat.st_mode) and file_parts_ok(rel.split("/")):
                        found.append((rel, path, stat))
        return found

    def once(self, now_ms: int) -> int:
        """Push one batch if one is due; the number of pieces pushed."""
        held = self.state["files"]
        changed = [
            (stat.st_mtime_ns, rel, path, stat) for rel, path, stat in self._files()
            if held.get(str(path), [None, None])[:2] != [stat.st_size, stat.st_mtime_ns]
        ]
        if not changed:
            return 0
        latest = max(mtime for mtime, *_ in changed) // 1_000_000
        if now_ms - latest < QUIET_MS and now_ms - int(self.state["last_sent_ms"]) < EVERY_MS:
            return 0
        pieces: list[tuple[str, bytes]] = []
        after: dict[str, list[Any]] = {}
        size = 0
        for _, rel, path, stat in sorted(changed):
            if size >= BATCH_BYTES:
                break
            size += self._cut(rel, path, stat, now_ms, pieces, after, BATCH_BYTES - size)
        if pieces:
            self._push(pieces, now_ms)
            self.state["last_sent_ms"] = now_ms
        held.update(after)
        self._save()
        return len(pieces)

    def _cut(  # noqa: PLR0913 - one file, the clock, and the batch it adds to
        self, rel: str, path: Path, stat: os.stat_result, now_ms: int,
        pieces: list[tuple[str, bytes]], after: dict[str, list[Any]], room: int,
    ) -> int:
        """Add ``path``'s pending pieces to the batch; their encrypted bytes."""
        jsonl = path.name.endswith(".jsonl")
        if not jsonl and now_ms - stat.st_mtime_ns // 1_000_000 < FILE_QUIET_MS:
            return 0
        row = self.state["files"].get(str(path))
        added = 0
        with path.open("rb") as log:
            offset = row[2] if row and _still(log, stat.st_size, row[2], row[3], row[4]) else 0
            while added < room:
                piece = next_piece(log, offset, jsonl=jsonl)
                if piece is None or (isinstance(piece, int) and piece < 0):
                    break
                if isinstance(piece, int):
                    offset += piece
                    continue
                sealed = subprocess.run(  # noqa: S603 - fixed argv, the VM's age
                    [self.age, "--encrypt", "--recipient", self.recipient],
                    input=compress(piece), capture_output=True, timeout=_GIT_S, check=True,
                ).stdout
                pieces.append((f"{self.account}/{rel}/{piece_name(offset, piece)}.age", sealed))
                offset += len(piece)
                added += len(sealed)
                if not jsonl:
                    break
            else:
                after[str(path)] = [-1, -1, offset, *_edges(log, offset)]
                return added
            after[str(path)] = [stat.st_size, stat.st_mtime_ns, offset, *_edges(log, offset)]
        return added

    def _push(self, pieces: list[tuple[str, bytes]], now_ms: int) -> None:
        if not (self.repo / ".git").is_dir():
            self.repo.mkdir(parents=True, exist_ok=True)
            _git(self.repo, "init", "--quiet")
        if not self._main:
            if not _git(self.repo, "ls-remote", "--heads", self.url, "main").strip():
                tree = _git(self.repo, "mktree").decode().strip()
                commit = _git(self.repo, "commit-tree", tree, "-m", "relay").decode().strip()
                try:
                    _git(self.repo, "push", "--quiet", self.url, f"{commit}:refs/heads/main")
                except RuntimeError:
                    if not _git(self.repo, "ls-remote", "--heads", self.url, "main").strip():
                        raise
            self._main = True
        index = self.home / "relay-index"
        index.unlink(missing_ok=True)
        lines = []
        for name, sealed in pieces:
            blob = _git(self.repo, "hash-object", "-w", "--stdin", stdin=sealed).decode().strip()
            lines.append(f"100644 {blob}\t{name}\n")
        env = {"GIT_INDEX_FILE": str(index)}
        _git(self.repo, "update-index", "--add", "--index-info", stdin="".join(lines).encode(),
             env=env)
        tree = _git(self.repo, "write-tree", env=env).decode().strip()
        commit = _git(self.repo, "commit-tree", tree, "-m", f"{self.session} {len(pieces)}",
                      env=env).decode().strip()
        _git(self.repo, "push", "--quiet", self.url,
             f"{commit}:refs/heads/s/{self.session}/{now_ms}")
        index.unlink(missing_ok=True)

    def _save(self) -> None:
        part = self.state_path.with_suffix(".part")
        part.write_text(json.dumps(self.state))
        part.replace(self.state_path)


# --- install, start, watch ---------------------------------------------------------------------


def install(home: Path, account: str, recipient: str, url: str) -> None:
    """Copy this file to ``home``, write its config, and hook it into each Claude Code start."""
    if not _ACCOUNT.fullmatch(account):
        msg = "the account is 1 to 16 of a-z 0-9"
        raise SystemExit(msg)
    if not _RECIPIENT.fullmatch(recipient):
        msg = "the recipient is an age public key (age1...)"
        raise SystemExit(msg)
    base = home / HOME_DIR
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    script = base / "cloud_log.py"
    if Path(__file__).resolve() != script.resolve():
        shutil.copyfile(__file__, script)
    config = {"account": account, "recipient": recipient, "url": url,
              "claude_dir": str(home / ".claude")}
    (base / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    command = f"python3 {script} start"
    bashrc = home / ".bashrc"
    text = bashrc.read_text() if bashrc.exists() else ""
    if MARK not in text:  # first, ahead of the usual "not interactive: return"
        bashrc.write_text(f"{{ {command}; }} >/dev/null 2>&1 || true  {MARK}\n{text}")
    settings_path = home / ".claude" / "settings.json"
    try:
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except ValueError:
        return  # not ours to repair; the ~/.bashrc line starts the watcher
    starts = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    if not any(command in json.dumps(entry) for entry in starts):
        starts.append({"hooks": [{"type": "command", "command": command}]})
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def _lock(base: Path) -> Any:  # noqa: ANN401 - an open file holding the lock, or None
    handle = (base / "watch.lock").open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def start(home: Path) -> None:
    """Start a detached watcher unless this is not a cloud session or one is running."""
    base = home / HOME_DIR
    if os.environ.get("CLAUDE_CODE_REMOTE") != "true" or not (base / "config.json").is_file():
        return
    held = _lock(base)
    if held is None:
        return
    held.close()
    with (base / "watch.log").open("ab") as log:
        subprocess.Popen(  # noqa: S603 - this file, detached
            [sys.executable, str(Path(__file__).resolve()), "watch"], stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True, close_fds=True,
        )


def watch(home: Path) -> None:
    """Push batches until the VM goes; one watcher per VM."""
    base = home / HOME_DIR
    held = _lock(base)
    session = os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID", "")
    if held is None:
        return
    if not _SESSION.fullmatch(session):
        print(f"{time.strftime('%FT%T')} no usable CLAUDE_CODE_REMOTE_SESSION_ID", flush=True)  # noqa: T201 - its log
        return
    watcher = Watcher(json.loads((base / "config.json").read_text()), session, base)
    while True:
        try:
            watcher.once(time.time_ns() // 1_000_000)
            time.sleep(TICK_S)
        except Exception as exc:  # noqa: BLE001 - the watcher outlives any one failed push
            print(f"{time.strftime('%FT%T')} push failed: {exc}", flush=True)  # noqa: T201 - its log
            time.sleep(RETRY_S)


def main() -> None:
    """The three commands."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install")
    setup.add_argument("--account", required=True)
    setup.add_argument("--recipient", required=True)
    setup.add_argument("--url", default=DEFAULT_URL)
    commands.add_parser("start")
    commands.add_parser("watch")
    args = parser.parse_args()
    home = Path.home()
    if args.command == "install":
        install(home, args.account, args.recipient, args.url)
    elif args.command == "start":
        start(home)
    else:
        watch(home)


if __name__ == "__main__":
    main()
