"""Git diff capture + dirty-tree auto-stash for Codex `spawn_worker`.

Day-2 Codex writes directly into the user's working tree (ADR-0002 D5).
If Allen has uncommitted edits at spawn-time, ``git diff`` would conflate
those edits with Codex's changes — evidence pollution. This module is the
deterministic auto-stash + diff-capture surface that keeps the verify
path's exit code a true postcondition signal.

Three phases:

1. **Pre-spawn** — ``isolate_pretask_changes(cwd, run_id=...)`` issues
   ``git -C cwd stash push -u -m "jarvis-pre-codex-<run_id>"`` when the
   tree has uncommitted (tracked or untracked) changes; returns
   ``"stash@{0}"`` or ``None`` for clean trees.

2. **Post-spawn** — ``capture_diff(cwd)`` runs plain ``git -C cwd diff``
   (no ``--cached``, no ``--check``, no three-dot range) to materialize
   Codex's working-tree edits as a unified diff string.
   ``write_diff_artifact`` persists it under
   ``<artifact_dir>/run_<run_id>/diff.txt`` with a minimal sidecar
   ``diff.json`` placeholder; Step 10 (spawn_worker rewrite) will own
   the canonical run-record JSON.

3. **Post-verify** — ``restore_pretask_changes(cwd, stash_ref,
   artifact_dir=..., run_id=...)`` pops the stash. The ordering contract
   (CRITICAL, per ADR-0002 § Dirty-tree policy, lines 663-713) is that
   this MUST be called by the runtime composition AFTER
   ``verify_diff_handler`` returns; it must NEVER be called inside
   ``spawn_worker_handler``. Calling early would re-introduce Allen's
   pre-task work on top of Codex's edits mid-verify, polluting the
   ``verify_command`` exit code. The static guarantee lands in Step
   10/17 via the ``test_canary_stash_pop_after_verify`` AST scan.

   On ``git stash pop`` conflict the stash is surfaced as a
   ``conflict.patch`` artifact, the conflicted tree is reset to the
   post-Codex state (``git reset --hard HEAD``), and the helper returns
   a :class:`StashConflictArtifact`. Allen reconciles manually —
   per the spec, **never silently overwrite Allen's work**.

References:
    - ADR-0002 § Dirty-tree policy (lines 663-713) — full contract.
    - ADR-0002 D5 — Codex writes directly into target repo's working tree.
    - ADR-0002 F3 / § What is explicitly discarded (lines 437-443) —
      ``git apply --check`` is NOT in the verify path; Codex already
      applied the patch in-place.

Layer boundary (`.importlinter` + canary H13): stdlib only. No imports
from sibling layers.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_GIT_TIMEOUT_SECONDS = 60


class StashError(RuntimeError):
    """Raised when a ``git stash``/``git status`` invocation fails."""


class DiffError(RuntimeError):
    """Raised when ``git diff`` cannot be captured."""


@dataclass(frozen=True)
class StashConflictArtifact:
    """Returned by ``restore_pretask_changes`` when ``git stash pop`` conflicted.

    Attributes:
        patch_path: Absolute path to the preserved stash patch
            (``<artifact_dir>/run_<run_id>/conflict.patch``). Contains
            the output of ``git stash show -p <stash_ref>`` so Allen can
            re-apply manually with ``git apply``.
        reason: Short machine-readable tag (e.g. ``"stash_pop_conflict"``)
            for the worker.artifact_observed event emitted by the
            runtime composition in Step 17.
    """

    patch_path: Path
    reason: str


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git -C cwd <args>`` with consistent flags.

    Captures stdout+stderr as text, ``check=False`` (callers inspect
    ``returncode`` themselves so they can attach stderr to raised
    errors), 60 s timeout.
    """
    return subprocess.run(  # noqa: S603 — argv list, no shell, fixed "git" exe.
        ["git", "-C", str(cwd), *args],  # noqa: S607 — `git` resolved via PATH is intentional.
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def isolate_pretask_changes(cwd: Path, *, run_id: str) -> str | None:
    """Stash any uncommitted changes; return the stash ref or ``None`` when clean.

    Detection: ``git -C cwd status --porcelain=v1`` empty → clean →
    return ``None``. Otherwise: ``git -C cwd stash push -u -m
    "jarvis-pre-codex-<run_id>"``. The ``-u`` flag also stashes
    untracked files so Codex sees a fully clean tree.

    The returned ref is always ``"stash@{0}"`` by convention because
    we immediately pop it in ``restore_pretask_changes``; if a caller
    needs a durable handle they can read ``git stash list``.

    Args:
        cwd: Target repo working directory.
        run_id: Run identifier embedded in the stash message for
            postmortem clarity (e.g. ``"R7"``).

    Returns:
        ``"stash@{0}"`` when something was stashed, else ``None``.

    Raises:
        StashError: if ``git status`` or ``git stash push`` fails.
    """
    status = _git("status", "--porcelain=v1", cwd=cwd)
    if status.returncode != 0:
        msg = f"git status failed (rc={status.returncode}): {status.stderr.strip()}"
        raise StashError(msg)
    if status.stdout.strip() == "":
        return None

    label = f"jarvis-pre-codex-{run_id}"
    push = _git("stash", "push", "-u", "-m", label, cwd=cwd)
    if push.returncode != 0:
        msg = f"git stash push failed (rc={push.returncode}): {push.stderr.strip()}"
        raise StashError(msg)
    return "stash@{0}"


def capture_diff(cwd: Path) -> str:
    """Capture ``git -C cwd diff`` as a single utf-8 string.

    NO ``--cached``, NO ``--check``, NO three-dot range — plain
    working-tree diff. Day-2 verifies Codex's in-place edits; cached/
    index isn't relevant (Codex doesn't ``git add``).

    Args:
        cwd: Target repo working directory.

    Returns:
        Unified diff as utf-8 text. Empty string when there are no
        unstaged changes (a clean tree post-Codex is a valid signal
        the worker did nothing observable; downstream evaluators
        treat empty diffs as the genuine outcome).

    Raises:
        DiffError: if ``git diff`` fails (e.g. ``cwd`` is not a repo).
    """
    proc = _git("diff", cwd=cwd)
    if proc.returncode != 0:
        msg = f"git diff failed (rc={proc.returncode}): {proc.stderr.strip()}"
        raise DiffError(msg)
    return proc.stdout


def write_diff_artifact(
    diff_text: str,
    *,
    artifact_dir: Path,
    run_id: str,
) -> Path:
    """Persist ``diff_text`` under ``<artifact_dir>/run_<run_id>/diff.txt``.

    Creates the run dir if absent. Also writes a sidecar ``diff.json``
    with a minimal placeholder shape — Step 10 (spawn_worker rewrite)
    will overwrite this with the canonical run record once it owns the
    artifact contract.

    Args:
        diff_text: Output of :func:`capture_diff`.
        artifact_dir: Root artifact directory (typically the
            ``RuntimePaths.artifacts`` value supplied by L6 deployment).
        run_id: Run identifier (e.g. ``"R7"``).

    Returns:
        The absolute path of the written ``diff.txt``.
    """
    run_dir = artifact_dir / f"run_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    diff_path = run_dir / "diff.txt"
    diff_path.write_text(diff_text, encoding="utf-8")

    sidecar: dict[str, object] = {
        "status": "captured",
        "run_id": run_id,
        "task_id_unknown": True,  # Step 10 plumbs the real task_id.
        "summary": "codex diff capture",
        "diff_path": str(diff_path),
        "exit_reason": "ok",
    }
    (run_dir / "diff.json").write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return diff_path


def restore_pretask_changes(
    cwd: Path,
    stash_ref: str | None,
    *,
    artifact_dir: Path,
    run_id: str,
) -> StashConflictArtifact | None:
    """Pop the stash; on conflict, surface a patch artifact.

    Day-2 ordering contract (CRITICAL, per ADR-0002 § Dirty-tree
    policy, lines 663-713): this MUST be called by the runtime
    composition AFTER ``verify_diff_handler(...)`` returns. NEVER call
    inside ``spawn_worker_handler``. Calling early would re-introduce
    Allen's pre-task work on top of Codex's edits mid-verify,
    polluting the ``verify_command`` exit code. The static guarantee
    lands in Step 10/17 via the ``test_canary_stash_pop_after_verify``
    AST scan.

    Steps:

    1. If ``stash_ref`` is ``None`` (clean tree at spawn-time), return
       ``None`` immediately — no-op.
    2. ``git -C cwd stash pop <stash_ref>``.
    3. If pop returned non-zero AND the output indicates a conflict
       (``"CONFLICT"`` or ``"merge conflict"`` substring,
       case-insensitive):

       a. Read the stash patch via
          ``git -C cwd stash show -p <stash_ref>``.
       b. Write to ``<artifact_dir>/run_<run_id>/conflict.patch``.
       c. Best-effort ``git -C cwd stash drop <stash_ref>`` so the
          orphan stash doesn't linger in ``git stash list``.
       d. ``git -C cwd reset --hard HEAD`` — Codex's changes are
          authoritative; Allen reconciles the stash manually from
          the artifact. Per ADR-0002 lines 696-705, **never silently
          overwrite Allen's work** — the stash patch is the
          recoverable handle.
       e. Return :class:`StashConflictArtifact`.

    4. If pop returned non-zero for a non-conflict reason, raise
       :class:`StashError` with stderr attached.
    5. If pop succeeded cleanly, return ``None``.

    Args:
        cwd: Target repo working directory.
        stash_ref: Output of :func:`isolate_pretask_changes`
            (``"stash@{0}"`` or ``None``).
        artifact_dir: Root artifact directory; the conflict patch
            lands at ``<artifact_dir>/run_<run_id>/conflict.patch``.
        run_id: Run identifier (must match the one used by
            :func:`write_diff_artifact`).

    Returns:
        :class:`StashConflictArtifact` when a conflict was preserved;
        ``None`` on clean pop or when there was nothing to pop.

    Raises:
        StashError: when a non-conflict git failure occurs (e.g. the
            stash ref disappeared between push and pop).
    """
    if stash_ref is None:
        return None

    pop = _git("stash", "pop", stash_ref, cwd=cwd)
    if pop.returncode == 0:
        return None

    combined = f"{pop.stdout}\n{pop.stderr}".lower()
    if "conflict" not in combined:
        msg = (
            f"git stash pop failed non-conflict (rc={pop.returncode}): "
            f"{pop.stderr.strip()}"
        )
        raise StashError(msg)

    # Conflict path: preserve the stash as an artifact, then reset the
    # tree to the post-Codex state.
    show = _git("stash", "show", "-p", stash_ref, cwd=cwd)
    if show.returncode != 0:
        msg = (
            f"git stash show -p failed (rc={show.returncode}): "
            f"{show.stderr.strip()}"
        )
        raise StashError(msg)

    run_dir = artifact_dir / f"run_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    patch_path = run_dir / "conflict.patch"
    patch_path.write_text(show.stdout, encoding="utf-8")

    # Best-effort drop — if it fails, we still have the artifact;
    # surfacing a secondary StashError would mask the primary
    # conflict signal Allen needs to see.
    _git("stash", "drop", stash_ref, cwd=cwd)

    # Reset the conflicted working tree to the post-Codex HEAD.
    reset = _git("reset", "--hard", "HEAD", cwd=cwd)
    if reset.returncode != 0:
        msg = (
            f"git reset --hard HEAD failed after stash conflict "
            f"(rc={reset.returncode}): {reset.stderr.strip()}"
        )
        raise StashError(msg)

    return StashConflictArtifact(
        patch_path=patch_path,
        reason="stash_pop_conflict",
    )
