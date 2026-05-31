"""Unit tests for :mod:`jarvis.execution.diff_capture`.

Uses real ``git`` invocations against tmp_path-backed repos rather than
mocks — auto-stash and stash-pop conflict semantics are git-internal,
so any mock would diverge from production behaviour. The git-fixture
cost is ~1-2 s per test on a cold machine; well within the Tier-1
budget given there are only a handful of tests.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import TYPE_CHECKING

import pytest

from jarvis.execution.diff_capture import (
    DiffError,
    StashConflictArtifact,
    capture_diff,
    isolate_pretask_changes,
    restore_pretask_changes,
    write_diff_artifact,
)

if TYPE_CHECKING:
    from pathlib import Path

_GIT_USER_FLAGS = ["-c", "user.email=t@t", "-c", "user.name=t"]
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a real git command in ``repo`` with deterministic user flags."""
    return subprocess.run(  # noqa: S603 — fixed git argv, no shell, test fixture.
        ["git", "-C", str(repo), *_GIT_USER_FLAGS, *args],  # noqa: S607 — git on PATH by design.
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(repo: Path) -> None:
    """Create a fresh git repo with one empty commit so HEAD exists."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 — fixed git argv, no shell, test fixture.
        ["git", "init", "-q", "-b", "main", str(repo)],  # noqa: S607 — git on PATH by design.
        check=True,
        capture_output=True,
    )
    _git(repo, "commit", "--allow-empty", "-q", "-m", "init")


# ---- isolate_pretask_changes -------------------------------------------------


def test_clean_tree_isolate_returns_none(tmp_path: Path) -> None:
    """A spotless tree must produce no stash (clean-spawn fast path)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert isolate_pretask_changes(repo, run_id="R1") is None


def test_dirty_tree_isolate_stashes_tracked(tmp_path: Path) -> None:
    """Modified tracked file → stash@{0} returned and tree becomes clean."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "b")
    (repo / "a.txt").write_text("dirty\n")

    ref = isolate_pretask_changes(repo, run_id="R2")

    assert ref is not None
    assert _SHA_RE.match(ref), f"expected 40-char hex SHA, got {ref!r}"
    porcelain = subprocess.run(  # noqa: S603 — fixed git argv, no shell, test fixture.
        ["git", "-C", str(repo), "status", "--porcelain=v1"],  # noqa: S607 — git on PATH by design.
        capture_output=True,
        text=True,
        check=True,
    )
    assert porcelain.stdout.strip() == ""


def test_dirty_tree_isolate_stashes_untracked(tmp_path: Path) -> None:
    """`-u` flag must capture untracked files so Codex sees a clean tree."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "new.tmp").write_text("scratch\n")

    ref = isolate_pretask_changes(repo, run_id="R3")

    assert ref is not None
    assert _SHA_RE.match(ref), f"expected 40-char hex SHA, got {ref!r}"
    assert not (repo / "new.tmp").exists()


# ---- restore_pretask_changes -------------------------------------------------


def test_restore_none_is_noop(tmp_path: Path) -> None:
    """stash_ref=None → no-op, return None (clean-tree spawn case)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert (
        restore_pretask_changes(
            repo,
            None,
            artifact_dir=tmp_path / "art",
            run_id="R4",
        )
        is None
    )


def test_dirty_tree_clean_pop_restores(tmp_path: Path) -> None:
    """Codex no-op + pop → Allen's stashed work is reapplied verbatim."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("hello\n")  # untracked file

    ref = isolate_pretask_changes(repo, run_id="R5")
    assert ref is not None
    assert _SHA_RE.match(ref), f"expected 40-char hex SHA, got {ref!r}"
    # Codex no-op: tree stays untouched. Pop should restore "a.txt".
    art = restore_pretask_changes(
        repo,
        ref,
        artifact_dir=tmp_path / "art",
        run_id="R5",
    )
    assert art is None
    assert (repo / "a.txt").read_text() == "hello\n"


def test_dirty_tree_conflict_pop_writes_artifact(tmp_path: Path) -> None:
    """Conflicting Codex edit → conflict.patch artifact + post-Codex tree."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "b1")

    # Allen's pre-task edit.
    (repo / "a.txt").write_text("allen edit\n")

    ref = isolate_pretask_changes(repo, run_id="R6")
    assert ref is not None
    assert _SHA_RE.match(ref), f"expected 40-char hex SHA, got {ref!r}"

    # Codex simulates a conflicting edit, then commits it.
    (repo / "a.txt").write_text("codex edit\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "codex")

    art = restore_pretask_changes(
        repo,
        ref,
        artifact_dir=tmp_path / "art",
        run_id="R6",
    )

    assert isinstance(art, StashConflictArtifact)
    assert art.reason == "stash_pop_conflict"
    assert art.patch_path.is_file()
    assert art.patch_path.parent.name == "run_R6"
    # The preserved patch carries Allen's edit so he can re-apply.
    assert "allen edit" in art.patch_path.read_text()
    # Working tree is reset to post-Codex (codex edit), not the conflicted blob.
    assert (repo / "a.txt").read_text() == "codex edit\n"
    # And the orphan stash was dropped.
    listing = subprocess.run(  # noqa: S603 — fixed git argv, no shell, test fixture.
        ["git", "-C", str(repo), "stash", "list"],  # noqa: S607 — git on PATH by design.
        capture_output=True,
        text=True,
        check=True,
    )
    assert listing.stdout.strip() == ""


def test_dirty_tree_uncommitted_codex_edit_writes_artifact(tmp_path: Path) -> None:
    """Uncommitted Codex edit → 'would be overwritten' abort → artifact, edit kept.

    The realistic Codex lifecycle edits the working tree but does NOT
    commit before restore. ``git stash apply`` then aborts pre-merge
    with "would be overwritten by merge" (rc=1, no CONFLICT marker).
    This must surface a :class:`StashConflictArtifact` — same as the
    committed-conflict case — but must NOT ``reset --hard``, which would
    destroy Codex's uncommitted work.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "b1")

    # Allen's pre-task edit.
    (repo / "a.txt").write_text("allen edit\n")

    ref = isolate_pretask_changes(repo, run_id="R7")
    assert ref is not None
    assert _SHA_RE.match(ref), f"expected 40-char hex SHA, got {ref!r}"

    # Codex edits the same file but does NOT commit — the realistic case.
    (repo / "a.txt").write_text("codex edit\n")

    art = restore_pretask_changes(
        repo,
        ref,
        artifact_dir=tmp_path / "art",
        run_id="R7",
    )

    assert isinstance(art, StashConflictArtifact)
    assert art.reason == "stash_pop_conflict"
    assert art.patch_path.is_file()
    assert art.patch_path.parent.name == "run_R7"
    # The preserved patch carries Allen's edit so he can re-apply.
    assert "allen edit" in art.patch_path.read_text()
    # Codex's uncommitted edit MUST survive — no reset --hard wiped it.
    assert (repo / "a.txt").read_text() == "codex edit\n"
    # And the orphan stash was dropped.
    listing = subprocess.run(  # noqa: S603 — fixed git argv, no shell, test fixture.
        ["git", "-C", str(repo), "stash", "list"],  # noqa: S607 — git on PATH by design.
        capture_output=True,
        text=True,
        check=True,
    )
    assert listing.stdout.strip() == ""


# ---- capture_diff -----------------------------------------------------------


def test_capture_diff_returns_unified_diff(tmp_path: Path) -> None:
    """Tracked-file edit surfaces as a unified diff with +/- lines."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "x.py").write_text("print('hi')\n")
    _git(repo, "add", "x.py")
    _git(repo, "commit", "-q", "-m", "x")
    (repo / "x.py").write_text("print('hello world')\n")

    diff = capture_diff(repo)

    assert "diff --git" in diff
    assert "+print('hello world')" in diff
    assert "-print('hi')" in diff


def test_capture_diff_empty_repo_is_empty_string(tmp_path: Path) -> None:
    """An empty post-init repo (no diff) returns an empty string, not an error."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert capture_diff(repo) == ""


def test_capture_diff_non_repo_raises(tmp_path: Path) -> None:
    """A plain dir (not a git repo) surfaces as DiffError."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(DiffError):
        capture_diff(not_a_repo)


# ---- write_diff_artifact -----------------------------------------------------


def test_write_diff_artifact_creates_run_dir_and_sidecar(tmp_path: Path) -> None:
    """diff.txt + diff.json placeholder land under run_<R>/."""
    art = tmp_path / "art"
    path = write_diff_artifact("diff content\n", artifact_dir=art, run_id="R7")

    assert path.is_file()
    assert path.name == "diff.txt"
    assert path.parent.name == "run_R7"
    assert path.read_text() == "diff content\n"

    sidecar = path.parent / "diff.json"
    assert sidecar.is_file()
    body = sidecar.read_text()
    assert '"run_id": "R7"' in body
    assert '"task_id_unknown": true' in body
    assert '"diff_path"' in body


def test_write_diff_artifact_empty_diff_is_persisted(tmp_path: Path) -> None:
    """An empty diff is a valid signal (Codex no-op); must persist verbatim."""
    art = tmp_path / "art"
    path = write_diff_artifact("", artifact_dir=art, run_id="R8")
    assert path.read_text() == ""


# ---- B-0011 SHA-as-handle regression ----------------------------------------


def test_isolate_returns_sha_not_stack_ref(tmp_path: Path) -> None:
    """B-0011 regression: isolate returns SHA, not 'stash@{0}'."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("hello\n")

    ref = isolate_pretask_changes(repo, run_id="R-sha")

    assert ref is not None
    assert ref != "stash@{0}"
    assert _SHA_RE.match(ref), f"expected 40-char hex SHA, got {ref!r}"
    rev = subprocess.run(  # noqa: S603 — fixed git argv, no shell, test fixture.
        ["git", "-C", str(repo), "rev-parse", "stash@{0}"],  # noqa: S607 — git on PATH by design.
        capture_output=True,
        text=True,
        check=True,
    )
    assert rev.stdout.strip() == ref


def test_restore_finds_stash_by_sha_after_user_stash_pushed(tmp_path: Path) -> None:
    """B-0011 regression: user pushing a concurrent stash does not corrupt restore."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "b")
    (repo / "a.txt").write_text("jarvis pretask edit\n")

    ref = isolate_pretask_changes(repo, run_id="R-race")
    assert ref is not None
    assert _SHA_RE.match(ref), f"expected 40-char hex SHA, got {ref!r}"

    # User pushes a separate stash (new untracked file).
    (repo / "user_wip.txt").write_text("user concurrent work\n")
    _git(repo, "stash", "push", "-u", "-m", "user-wip")

    listing_before = subprocess.run(  # noqa: S603 — fixed git argv, no shell, test fixture.
        ["git", "-C", str(repo), "stash", "list"],  # noqa: S607 — git on PATH by design.
        capture_output=True,
        text=True,
        check=True,
    )
    lines_before = listing_before.stdout.strip().splitlines()
    assert len(lines_before) == 2
    assert "user-wip" in lines_before[0]
    assert "jarvis-pre-codex-R-race" in lines_before[1]

    art = restore_pretask_changes(
        repo,
        ref,
        artifact_dir=tmp_path / "art",
        run_id="R-race",
    )

    assert art is None
    assert (repo / "a.txt").read_text() == "jarvis pretask edit\n"
    listing_after = subprocess.run(  # noqa: S603 — fixed git argv, no shell, test fixture.
        ["git", "-C", str(repo), "stash", "list"],  # noqa: S607 — git on PATH by design.
        capture_output=True,
        text=True,
        check=True,
    )
    lines_after = listing_after.stdout.strip().splitlines()
    assert len(lines_after) == 1
    assert "user-wip" in lines_after[0]
    assert "jarvis-pre-codex-R-race" not in listing_after.stdout
    assert not (repo / "user_wip.txt").exists()


def test_restore_handles_sha_not_found(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """User manually dropped jarvis stash -> warning + no-op, no raise."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("hello\n")

    ref = isolate_pretask_changes(repo, run_id="R-gone")
    assert ref is not None

    _git(repo, "stash", "drop", "stash@{0}")

    with caplog.at_level(logging.WARNING, logger="jarvis.execution.diff_capture"):
        art = restore_pretask_changes(
            repo,
            ref,
            artifact_dir=tmp_path / "art",
            run_id="R-gone",
        )

    assert art is None
    assert any(ref in rec.getMessage() for rec in caplog.records), (
        f"expected warning mentioning SHA {ref}, got "
        f"{[r.getMessage() for r in caplog.records]}"
    )
