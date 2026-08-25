"""Repo observer v0 — read-only local git perception (ADR-0009 D5).

An L5 input adapter under the ``observer`` principal (spec §2.1 puts the
"Mac observer" on the INPUTS/Surface row). One poll per repo produces a
*total* snapshot; the module emits ``repo.state_observed`` only when a
field changed (spec §3.6.1 emit-on-change ladder), plus one
``project.commit_seen`` per newly-seen first-parent commit.

Three pins carry the whole design:

1. **``GIT_OPTIONAL_LOCKS=0`` on every invocation.** Plain ``git
   status`` opportunistically writes ``.git/index.lock``; when a poll
   fires while Allen is mid ``git commit``, his commit dies with
   "Unable to create index.lock". The spec row this section cites
   ("read-only observation" / 不写外部世界) forbids exactly that. Every
   subprocess in this module goes through :func:`_run_git`, the only
   place the argv and the environment are built — there is no
   "this one is just a rev-parse" exception.
2. **The change-baseline is recovered from the event log**, not from
   process memory: :func:`recover_baselines` folds the latest
   ``repo.state_observed`` per repo. :class:`RepoObserver` keeps an
   in-memory copy as a *cache*, never as the source of truth. The first
   poll after a restart therefore emits exactly the delta accumulated
   while the daemon was down — no spurious restart emissions, and no
   permanent hole in ``project.commit_seen`` across the overnight
   window this ADR exists for.
3. **Neither event type is a decision trigger** (spec §3.4.1 / §3.2.5
   安静优先); observations fold silently. Pinned by
   ``tests/canary/test_canary_observer_never_triggers_decide.py``.

Failure modes F5 (subprocess hang / non-zero exit) and F6 (repo path
deleted or moved) collapse to one behavior: log once, skip this repo for
this cycle, emit nothing, and leave the baseline untouched so the next
successful poll still reports the whole delta.

Two contract edges the ADR leaves implicit, resolved here and documented
rather than discovered later:

- A repo with **no baseline at all** (first ever observation) emits its
  ``repo.state_observed`` and *no* ``project.commit_seen``: with no
  previous baseline no commit is "newly seen since" one. The repo enters
  observation at its current HEAD.
- A baseline sha that no longer resolves (rebase, ``gc``, force-move)
  makes ``old..HEAD`` unwalkable. That skips the commit walk only —
  ``repo.state_observed`` still lands and the baseline still advances,
  because the F5/F6 "skip the repo" rule must not turn a rewritten
  history into a repo that never reports again.

Wiring into ``serve_inherent`` and the ``observer.repos`` config are
Step 10; this module owns the snapshot / baseline / emit halves only.
:meth:`RepoObserver.collect` is the ``asyncio.to_thread`` half (pure
subprocess work, touches no database), :meth:`RepoObserver.emit` the
loop-thread half (single-writer over ``runtime.conn`` preserved).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from jarvis.shared import Event

LOGGER = logging.getLogger("jarvis.surface.repo_observer")

# Spec §5.1 principal, realized as a required payload field because the
# L2 schema has no actor column (ADR-0009 V6).
OBSERVER_ACTOR: Final[str] = "observer"

# Spec §3.3.9 bounded payloads — subject strings are capped, no diffs
# inline, no artifacts in v0.
MAX_SUBJECT_CHARS: Final[int] = 200

# ADR-0009 D5 burst cap: at most this many ``project.commit_seen`` per
# poll, with ``truncated``/``skipped_count`` on the final one.
DEFAULT_BURST_CAP: Final[int] = 20

# F5: a wedged `git` must not stall the observer task.
DEFAULT_GIT_TIMEOUT_S: Final[float] = 5.0

# THE pin (ADR-0009 D5). Applied in :func:`_run_git` to every single
# invocation; nothing in this module shells out to git any other way.
_GIT_ENV_OVERRIDES: Final[dict[str, str]] = {"GIT_OPTIONAL_LOCKS": "0"}

# ASCII unit separator: `git --format` field delimiter. Split with a
# bounded maxsplit so a subject containing the byte cannot shift fields.
_FIELD_SEP: Final[str] = "\x1f"

# One row per repo is all the baseline fold needs; the type is bound as a
# parameter so no SQL string interpolation happens here at all.
_SELECT_REPO_STATE_SQL: Final[str] = (
    "SELECT payload_json FROM events WHERE type = ? ORDER BY id ASC"
)


@dataclass(frozen=True)
class RepoSnapshot:
    """One repo's observable git state. Every field is always present.

    ``branch`` is the literal string ``"HEAD"`` on a detached checkout —
    a valid value, not an error, so the payload contract stays total
    (ADR-0009 D5). ``head_sha`` doubles as the last commit's sha, since
    HEAD *is* the last commit. ``last_commit_subject`` is already capped
    at :data:`MAX_SUBJECT_CHARS`.
    """

    repo_path: str
    branch: str
    head_sha: str
    dirty_file_count: int
    last_commit_subject: str


@dataclass(frozen=True)
class CommitRecord:
    """One first-parent commit newly seen since the baseline."""

    commit_sha: str
    subject: str
    committed_at_ms: int


@dataclass(frozen=True)
class RepoPoll:
    """Everything one repo's subprocess work produced — no DB touched.

    This is what crosses the ``asyncio.to_thread`` boundary in Step 10:
    :meth:`RepoObserver.collect` builds it off the loop, and
    :meth:`RepoObserver.emit` turns it into events on the loop thread.
    ``commits`` is ordered oldest-first; ``skipped_count`` is how many
    older commits the burst cap dropped.
    """

    snapshot: RepoSnapshot
    commits: tuple[CommitRecord, ...]
    skipped_count: int
    observed_at_ms: int


def _now_epoch_ms() -> int:
    """Return wall-clock epoch milliseconds (payload timestamps only)."""
    return int(time.time() * 1000)


def _cap_subject(text: str) -> str:
    """Truncate ``text`` to the spec §3.3.9 bounded-payload cap."""
    return text[:MAX_SUBJECT_CHARS]


def _run_git(repo_path: str, args: Sequence[str], *, timeout_s: float) -> str | None:
    """Run one local-only ``git`` read; return stdout, or None on failure.

    The single choke point for ADR-0009 D5: ``GIT_OPTIONAL_LOCKS=0`` is
    injected here, so no caller can accidentally issue a lock-taking
    invocation. Nothing here fetches, touches the network, or writes.

    Any failure — timeout (F5), non-zero exit, missing ``git``, or a
    repo path that no longer exists (F6) — logs once and returns None,
    which every caller turns into "skip this repo this cycle".

    Args:
        repo_path: Repo working tree, passed via ``git -C``.
        args: The git subcommand and its flags.
        timeout_s: Hard subprocess timeout (F5).

    Returns:
        Captured stdout on exit 0, else None.
    """
    env = {**os.environ, **_GIT_ENV_OVERRIDES}
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv; `args` comes from module-local literals only.
            ["git", "-C", repo_path, *args],  # noqa: S607 — `git` off PATH is the contract (same as tests/scenarios).
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning(
            "repo_observer: git %s failed for %s (%s); skipping this cycle",
            " ".join(args),
            repo_path,
            exc,
        )
        return None
    if completed.returncode != 0:
        LOGGER.warning(
            "repo_observer: git %s exited %d for %s (%s); skipping this cycle",
            " ".join(args),
            completed.returncode,
            repo_path,
            completed.stderr.strip()[:MAX_SUBJECT_CHARS],
        )
        return None
    return completed.stdout


def snapshot_repo(
    repo_path: str,
    *,
    git_timeout_s: float = DEFAULT_GIT_TIMEOUT_S,
) -> RepoSnapshot | None:
    """Return the total snapshot for ``repo_path``, or None to skip it.

    Three local reads: ``rev-parse --abbrev-ref HEAD`` (branch, or the
    literal ``"HEAD"`` when detached), ``status --porcelain`` (dirty
    file count = line count), and ``log -1`` (head sha + subject). Any
    of them failing means F5/F6 — None, and the caller emits nothing.
    """
    branch_out = _run_git(repo_path, ("rev-parse", "--abbrev-ref", "HEAD"), timeout_s=git_timeout_s)
    if branch_out is None:
        return None
    status_out = _run_git(repo_path, ("status", "--porcelain"), timeout_s=git_timeout_s)
    if status_out is None:
        return None
    head_out = _run_git(
        repo_path,
        ("log", "-1", f"--format=%H{_FIELD_SEP}%s"),
        timeout_s=git_timeout_s,
    )
    if head_out is None:
        return None

    head_sha, _, subject = head_out.strip("\n").partition(_FIELD_SEP)
    return RepoSnapshot(
        repo_path=repo_path,
        branch=branch_out.strip(),
        head_sha=head_sha.strip(),
        dirty_file_count=sum(1 for line in status_out.splitlines() if line.strip()),
        last_commit_subject=_cap_subject(subject),
    )


def _parse_commit_line(line: str) -> CommitRecord | None:
    """Parse one ``%H<sep>%ct<sep>%s`` row; None when it is malformed."""
    sha, _, rest = line.partition(_FIELD_SEP)
    committed_at, _, subject = rest.partition(_FIELD_SEP)
    if not sha or not committed_at.isdigit():
        return None
    return CommitRecord(
        commit_sha=sha,
        subject=_cap_subject(subject),
        committed_at_ms=int(committed_at) * 1000,
    )


def commits_since(
    repo_path: str,
    baseline_head: str | None,
    head_sha: str,
    *,
    burst_cap: int = DEFAULT_BURST_CAP,
    git_timeout_s: float = DEFAULT_GIT_TIMEOUT_S,
) -> tuple[tuple[CommitRecord, ...], int]:
    """Walk ``baseline_head..head_sha`` first-parent, burst-capped.

    Returns the newest ``burst_cap`` commits **oldest-first** (so the
    final event of a burst is HEAD's own commit) together with how many
    older ones were dropped. ADR-0010 reads a non-zero drop count as
    "this window is gapped, not contiguous".

    Returns ``((), 0)`` — commit walk skipped, ``repo.state_observed``
    unaffected — when there is no baseline yet, when HEAD did not move,
    or when the range is unwalkable because the baseline sha no longer
    resolves (rebase / gc / force-move).
    """
    if baseline_head is None or baseline_head == head_sha:
        return ((), 0)

    rev_range = f"{baseline_head}..{head_sha}"
    count_out = _run_git(
        repo_path,
        ("rev-list", "--first-parent", "--count", rev_range),
        timeout_s=git_timeout_s,
    )
    if count_out is None:
        LOGGER.warning(
            "repo_observer: %s is unwalkable in %s (rewritten history?); "
            "emitting repo state only",
            rev_range,
            repo_path,
        )
        return ((), 0)
    stripped = count_out.strip()
    if not stripped.isdigit():
        return ((), 0)
    total = int(stripped)
    if total == 0:
        return ((), 0)

    log_out = _run_git(
        repo_path,
        (
            "log",
            "--first-parent",
            f"--max-count={burst_cap}",
            f"--format=%H{_FIELD_SEP}%ct{_FIELD_SEP}%s",
            rev_range,
        ),
        timeout_s=git_timeout_s,
    )
    if log_out is None:
        return ((), 0)

    parsed = [
        record
        for record in (_parse_commit_line(line) for line in log_out.splitlines() if line)
        if record is not None
    ]
    parsed.reverse()  # git logs newest-first; emit in commit order.
    return tuple(parsed), max(total - len(parsed), 0)


def collect_repo_poll(
    repo_path: str,
    baseline: RepoSnapshot | None,
    *,
    burst_cap: int = DEFAULT_BURST_CAP,
    git_timeout_s: float = DEFAULT_GIT_TIMEOUT_S,
) -> RepoPoll | None:
    """Do one repo's whole subprocess half; None means F5/F6 skip.

    Pure with respect to process state: no database handle, no shared
    mutation. That is what makes it safe to hand to
    ``asyncio.to_thread`` while the loop thread keeps the single-writer
    connection to itself.
    """
    snapshot = snapshot_repo(repo_path, git_timeout_s=git_timeout_s)
    if snapshot is None:
        return None
    commits, skipped_count = commits_since(
        repo_path,
        None if baseline is None else baseline.head_sha,
        snapshot.head_sha,
        burst_cap=burst_cap,
        git_timeout_s=git_timeout_s,
    )
    return RepoPoll(
        snapshot=snapshot,
        commits=commits,
        skipped_count=skipped_count,
        observed_at_ms=_now_epoch_ms(),
    )


def _snapshot_from_payload(payload: dict[str, Any]) -> RepoSnapshot | None:
    """Rebuild a baseline snapshot from a stored ``repo.state_observed``."""
    try:
        return RepoSnapshot(
            repo_path=str(payload["repo_path"]),
            branch=str(payload["branch"]),
            head_sha=str(payload["head_sha"]),
            dirty_file_count=int(payload["dirty_file_count"]),
            last_commit_subject=str(payload["last_commit_subject"]),
        )
    except (KeyError, TypeError, ValueError):
        LOGGER.warning("repo_observer: unreadable repo.state_observed payload; ignoring row")
        return None


def recover_baselines(event_log: sqlite3.Connection) -> dict[str, RepoSnapshot]:
    """Fold the latest ``repo.state_observed`` per repo out of the log.

    This — not any in-memory buffer — is the change-baseline (ADR-0009
    D5). Rows arrive in append order, so a plain dict overwrite leaves
    the newest observation per ``repo_path``. Typed by event type rather
    than scanning the whole log.
    """
    baselines: dict[str, RepoSnapshot] = {}
    cursor = event_log.execute(_SELECT_REPO_STATE_SQL, ("repo.state_observed",))
    for (payload_json,) in cursor:
        snapshot = _snapshot_from_payload(json.loads(payload_json))
        if snapshot is not None:
            baselines[snapshot.repo_path] = snapshot
    return baselines


class RepoObserver:
    """Emit-on-change repo perception over a log-recovered baseline.

    Split deliberately into a thread half and a loop half:
    :meth:`collect` runs the ``git`` subprocesses and touches no
    database; :meth:`emit` writes events and advances the cached
    baseline, and must run on the connection's owning thread.
    """

    def __init__(
        self,
        event_log: sqlite3.Connection,
        repo_paths: Iterable[str | Path],
        *,
        burst_cap: int = DEFAULT_BURST_CAP,
        git_timeout_s: float = DEFAULT_GIT_TIMEOUT_S,
    ) -> None:
        """Bind the observer to a log connection and the watched repos."""
        self._event_log = event_log
        self.repo_paths: tuple[str, ...] = tuple(str(path) for path in repo_paths)
        self._burst_cap = burst_cap
        self._git_timeout_s = git_timeout_s
        self._baselines: dict[str, RepoSnapshot] = {}

    def recover_baselines(self) -> dict[str, RepoSnapshot]:
        """Seed the baseline cache from the event log; call at startup.

        Until this runs the cache is empty, which would make the first
        poll of every repo look like a first-ever observation.
        """
        self._baselines = recover_baselines(self._event_log)
        return dict(self._baselines)

    def baseline_for(self, repo_path: str) -> RepoSnapshot | None:
        """Return the cached baseline for ``repo_path``, if any."""
        return self._baselines.get(repo_path)

    def collect(self, repo_path: str) -> RepoPoll | None:
        """Run one repo's subprocess half (``asyncio.to_thread`` target)."""
        return collect_repo_poll(
            repo_path,
            self._baselines.get(repo_path),
            burst_cap=self._burst_cap,
            git_timeout_s=self._git_timeout_s,
        )

    def emit(self, poll: RepoPoll) -> list[Event]:
        """Append the poll's events on the loop thread; advance baseline.

        Emits one ``project.commit_seen`` per newly-seen commit (oldest
        first, ``truncated``/``skipped_count`` riding the final one when
        the burst cap bit), then ``repo.state_observed`` — but only if a
        snapshot field actually changed (spec §3.6.1). An unchanged poll
        emits nothing at all.
        """
        snapshot = poll.snapshot
        baseline = self._baselines.get(snapshot.repo_path)
        if baseline == snapshot and not poll.commits:
            return []

        emitted: list[Event] = []
        last_index = len(poll.commits) - 1
        for index, commit in enumerate(poll.commits):
            payload: dict[str, Any] = {
                "repo_path": snapshot.repo_path,
                "commit_sha": commit.commit_sha,
                "subject": commit.subject,
                "committed_at_ms": commit.committed_at_ms,
                "actor": OBSERVER_ACTOR,
            }
            if poll.skipped_count > 0 and index == last_index:
                payload["truncated"] = True
                payload["skipped_count"] = poll.skipped_count
            emitted.append(
                emit_event(self._event_log, type="project.commit_seen", payload=payload)
            )

        if baseline != snapshot:
            emitted.append(
                emit_event(
                    self._event_log,
                    type="repo.state_observed",
                    payload={
                        "repo_path": snapshot.repo_path,
                        "branch": snapshot.branch,
                        "head_sha": snapshot.head_sha,
                        "dirty_file_count": snapshot.dirty_file_count,
                        "last_commit_subject": snapshot.last_commit_subject,
                        "observed_at_ms": poll.observed_at_ms,
                        "actor": OBSERVER_ACTOR,
                    },
                )
            )

        self._baselines[snapshot.repo_path] = snapshot
        return emitted

    def poll_once(self) -> list[Event]:
        """Collect + emit every watched repo, synchronously, in order.

        The single-threaded composition of the two halves. Step 10's
        task splits them across ``asyncio.to_thread`` instead; the
        emit-on-change semantics are identical either way.
        """
        emitted: list[Event] = []
        for repo_path in self.repo_paths:
            poll = self.collect(repo_path)
            if poll is None:
                continue
            emitted.extend(self.emit(poll))
        return emitted


__all__ = [
    "DEFAULT_BURST_CAP",
    "DEFAULT_GIT_TIMEOUT_S",
    "MAX_SUBJECT_CHARS",
    "OBSERVER_ACTOR",
    "CommitRecord",
    "RepoObserver",
    "RepoPoll",
    "RepoSnapshot",
    "collect_repo_poll",
    "commits_since",
    "recover_baselines",
    "snapshot_repo",
]
