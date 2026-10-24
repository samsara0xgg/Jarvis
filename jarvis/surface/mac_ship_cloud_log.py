"""Cloud sessions' transcripts, taken from the private relay repository and shipped (ADR 0387).

A cloud VM's watcher (``scripts/cloud_log.py``) pushes each batch of pieces, every one encrypted
with ``age`` to this Mac's key, as a branch ``s/<session id>/<epoch ms>`` holding
``<account>/<path in the config directory>/<piece>.age``. Only when the key file exists, a pass
lists the repository's branches, fetches the oldest ``BRANCHES_PER_PASS`` into a bare repository
under the runtime root, decrypts every blob and ships it as
``<account>/cloud-<session id>/<path>/<piece>`` like the Mac's own pieces, and deletes the branch
once the brain has kept every piece of it. A branch that cannot be read (a blob that does not
decrypt, or a path or piece the brain would refuse) stays in the repository and is not tried
again while the key file stays the same; a refusal from the brain stops the pass and the
branch is tried next time. The repository gets an empty ``main`` first, so a relay branch is
never its default branch, which GitHub would refuse to delete.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from jarvis.state import code_log
from jarvis.surface.mac_ship_core import SET_META, Report, ShipFailed, Sink, State

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

BRANCHES_PER_PASS: Final = 40
BRANCH: Final = re.compile(r"refs/heads/s/([A-Za-z0-9_]{1,80})/(\d{1,15})")
MAIN: Final = "refs/heads/main"
UNREADABLE: Final = "cloud-log-unreadable:"
"""``meta`` key prefix of a branch left in the repository: its commit and the key file's mtime."""
_GIT_S: Final = 300
_AGE_S: Final = 60
_SEARCH: Final = "/opt/homebrew/bin:/usr/local/bin"
_ENV: Final = {
    "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "jarvis", "GIT_AUTHOR_EMAIL": "jarvis@localhost",
    "GIT_COMMITTER_NAME": "jarvis", "GIT_COMMITTER_EMAIL": "jarvis@localhost",
}


@dataclass(frozen=True)
class CloudLogConfig:
    """The relay repository, the Mac's key, and where its local copy lives."""

    url: str
    identity: Path
    git_dir: Path
    age: str = "age"


class _Unreadable(Exception):  # noqa: N818 - a verdict on a branch, not a fault
    """A branch whose blobs cannot all be read as pieces."""


def relay(config: CloudLogConfig, state: State, sink: Sink, report: Report) -> None:
    """One pass over the relay repository's oldest branches.

    Raises:
        ShipFailed: git could not reach the repository, or the brain did not keep a piece.
    """
    if not config.identity.is_file():
        return
    age = shutil.which(config.age, path=f"{os.environ.get('PATH', '')}:{_SEARCH}")
    if age is None:
        msg = "age is not installed"
        raise ShipFailed(msg)
    key = str(config.identity.stat().st_mtime_ns)
    git = _Git(config.git_dir)
    heads = git.heads(config.url)
    if MAIN not in heads:
        git.push(config.url, f"{git.empty_commit()}:{MAIN}")
    waiting = sorted(
        (int(found[2]), ref, found[1], sha) for ref, sha in heads.items()
        if (found := BRANCH.fullmatch(ref)) and state.meta(UNREADABLE + ref) != f"{sha} {key}"
    )[:BRANCHES_PER_PASS]
    if not waiting:
        return
    git.fetch(config.url, [ref for _, ref, _, _ in waiting])
    for _, ref, session, sha in waiting:
        try:
            pieces = _read(git, sha, session, config.identity, age)
        except _Unreadable as exc:
            LOGGER.warning("mac-ship: cloud branch %s left in place: %s", ref, exc)
            report.skipped["claude-code-cloud/unreadable"] += 1
            state.apply([(SET_META, (UNREADABLE + ref, f"{sha} {key}"))])
            continue
        for relpath, data, raw_len in pieces:
            if sink.send_file(code_log.SOURCE, relpath, data) != hashlib.sha256(data).hexdigest():
                msg = "the brain's digest of a cloud transcript piece does not match"
                raise ShipFailed(msg)
            report.files += 1
            report.file_bytes += len(data)
            report.file_original_bytes += raw_len
        git.push(config.url, f":{ref}")
        git.forget(ref)
    git.prune()


def _read(
    git: _Git, sha: str, session: str, identity: Path, age: str,
) -> list[tuple[str, bytes, int]]:
    """``(relpath on the brain, gzip piece, raw length)`` of every blob of one branch."""
    pieces = []
    for blob, path in git.blobs(sha):
        account, _, inner = path.partition("/")
        if not path.endswith(".age") or not inner:
            msg = "a blob is not <account>/<path>/<piece>.age"
            raise _Unreadable(msg)
        relpath = f"{account}/cloud-{session}/{inner.removesuffix('.age')}"
        if (error := code_log.relpath_error(relpath)) is not None:
            raise _Unreadable(error)
        sealed = git.cat(blob)
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, the Mac's own age and key
                [age, "--decrypt", "--identity", str(identity)], input=sealed,
                capture_output=True, timeout=_AGE_S, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            msg = f"age did not run ({type(exc).__name__})"
            raise ShipFailed(msg) from None
        if done.returncode != 0:
            msg = "a blob does not decrypt with this Mac's key"
            raise _Unreadable(msg)
        try:
            raw = code_log.raw_of(relpath, done.stdout)
        except ValueError as exc:
            raise _Unreadable(str(exc)) from None
        pieces.append((relpath, done.stdout, len(raw)))
    return pieces


class _Git:
    """The bare repository under the runtime root that branches are fetched into."""

    def __init__(self, git_dir: Path) -> None:
        self._dir = git_dir
        if not (git_dir / "HEAD").is_file():
            git_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._run("init", "--bare", "--quiet")

    def _run(self, *args: str, stdin: bytes = b"") -> bytes:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv on the shipper's own repository
                ["/usr/bin/git", "--git-dir", str(self._dir), *args], input=stdin,
                capture_output=True, timeout=_GIT_S, check=False, env={**os.environ, **_ENV},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            msg = f"git {args[0]} did not run ({type(exc).__name__})"
            raise ShipFailed(msg) from None
        if done.returncode != 0:
            msg = f"git {args[0]} failed ({done.returncode})"
            raise ShipFailed(msg)
        return done.stdout

    def heads(self, url: str) -> dict[str, str]:
        """Every branch of the repository at ``url`` and its commit."""
        listed = self._run("ls-remote", "--heads", url).decode()
        return {ref: sha for sha, ref in (line.split("\t") for line in listed.splitlines())}

    def empty_commit(self) -> str:
        """A commit of the empty tree, for a repository that has no ``main`` yet."""
        tree = self._run("mktree").decode().strip()
        return self._run("commit-tree", tree, "-m", "relay").decode().strip()

    def push(self, url: str, refspec: str) -> None:
        """Push one refspec (``:<ref>`` deletes the branch)."""
        self._run("push", "--quiet", url, refspec)

    def fetch(self, url: str, refs: list[str]) -> None:
        """Fetch ``refs`` with their objects."""
        self._run("fetch", "--quiet", "--no-tags", url, *(f"+{ref}:{ref}" for ref in refs))

    def forget(self, ref: str) -> None:
        """Drop the local copy of a branch that has been relayed."""
        self._run("update-ref", "-d", ref)

    def prune(self) -> None:
        """Drop the objects no branch still needs, so the local copy does not grow."""
        self._run("gc", "--quiet", "--prune=now")

    def blobs(self, sha: str) -> list[tuple[str, str]]:
        """``(blob, path)`` of every file in commit ``sha``."""
        listed = self._run("ls-tree", "-r", "-z", "--full-tree", sha).decode()
        found = []
        for entry in filter(None, listed.split("\0")):
            meta, path = entry.split("\t", 1)
            _mode, kind, blob = meta.split(" ")
            if kind != "blob":
                msg = "the branch holds something that is not a file"
                raise _Unreadable(msg)
            found.append((blob, path))
        return found

    def cat(self, blob: str) -> bytes:
        """The bytes of one blob."""
        return self._run("cat-file", "blob", blob)
