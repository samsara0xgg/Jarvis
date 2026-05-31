"""Git diff capture + dirty-tree auto-stash for Codex `spawn_worker`.

Day-2 Codex writes directly into the user's working tree (ADR-0002 D5).
If Allen has uncommitted edits at spawn-time, ``git diff`` would conflate
those edits with Codex's changes — evidence pollution. This module is the
deterministic auto-stash + diff-capture surface that keeps the verify
path's exit code a true postcondition signal.

Three phases:

1. **Pre-spawn** — ``isolate_pretask_changes(cwd, run_id=...)`` issues
   ``git -C cwd stash push -u -m "jarvis-pre-codex-<run_id>"`` when the
   tree has uncommitted (tracked or untracked) changes; returns the
   40-char commit SHA of the new stash entry (resolved via
   ``git rev-parse stash@{0}``) or ``None`` for clean trees. The SHA
   is used (not the stack ref ``stash@{0}``) so a concurrent user
   ``git stash push`` between isolate and restore cannot corrupt the
   restore — see B-0011 / ADR-0002 § Dirty-tree policy.

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
import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_GIT_TIMEOUT_SECONDS = 60
_LOGGER = logging.getLogger(__name__)

# ``git stash list --format=%H %gd`` emits two whitespace-separated tokens
# per entry: commit SHA and stack ref. Anything else is a parse anomaly.
_STASH_LIST_LINE_FIELDS = 2


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


def _find_stash_by_sha(cwd: Path, sha: str) -> str | None:
    """Return ``stash@{N}`` for the entry whose commit SHA equals ``sha``, else None.

    The stack-ref form ``stash@{N}`` is positional: a concurrent user
    ``git stash push`` shifts every existing entry up by one slot. The
    commit SHA, by contrast, is immutable per stash entry — we use it
    as the durable handle and resolve back to the live stack ref at
    restore time via ``git stash list --format=%H %gd``.
    """
    listing = _git("stash", "list", "--format=%H %gd", cwd=cwd)
    if listing.returncode != 0:
        msg = (
            f"git stash list failed (rc={listing.returncode}): "
            f"{listing.stderr.strip()}"
        )
        raise StashError(msg)
    for line in listing.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == _STASH_LIST_LINE_FIELDS and parts[0] == sha:
            return parts[1]
    return None


def isolate_pretask_changes(cwd: Path, *, run_id: str) -> str | None:
    """Stash any uncommitted changes; return the stash SHA or ``None`` when clean.

    Detection: ``git -C cwd status --porcelain=v1`` empty → clean →
    return ``None``. Otherwise: ``git -C cwd stash push -u -m
    "jarvis-pre-codex-<run_id>"``. The ``-u`` flag also stashes
    untracked files so Codex sees a fully clean tree.

    The returned handle is the 40-char commit SHA of the just-created
    stash entry, obtained via ``git rev-parse stash@{0}``. We use SHA
    (not the stack ref ``stash@{0}``) because B-0011 showed the stack
    ref is fragile: any concurrent user ``git stash push`` between
    isolate and restore shifts our entry from ``stash@{0}`` to
    ``stash@{1}``, and a naive ``git stash pop stash@{0}`` would then
    apply (and drop) the user's stash, silently corrupting their work.
    The SHA is immutable per entry, so ``restore_pretask_changes``
    resolves it back to the live stack position via
    :func:`_find_stash_by_sha`.

    Args:
        cwd: Target repo working directory.
        run_id: Run identifier embedded in the stash message for
            postmortem clarity (e.g. ``"R7"``).

    Returns:
        40-char hex commit SHA of the new stash entry when something
        was stashed, else ``None``.

    Raises:
        StashError: if ``git status``, ``git stash push``, or
            ``git rev-parse stash@{0}`` fails.
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

    rev = _git("rev-parse", "stash@{0}", cwd=cwd)
    if rev.returncode != 0:
        msg = (
            f"git rev-parse stash@{{0}} failed (rc={rev.returncode}): "
            f"{rev.stderr.strip()}"
        )
        raise StashError(msg)
    return rev.stdout.strip()


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
    """Restore the jarvis pre-task stash by SHA; on conflict, surface a patch artifact.

    Day-2 ordering contract (CRITICAL, per ADR-0002 § Dirty-tree
    policy, lines 663-713): this MUST be called by the runtime
    composition AFTER ``verify_diff_handler(...)`` returns. NEVER call
    inside ``spawn_worker_handler``. Calling early would re-introduce
    Allen's pre-task work on top of Codex's edits mid-verify,
    polluting the ``verify_command`` exit code. The static guarantee
    lands in Step 10/17 via the ``test_canary_stash_pop_after_verify``
    AST scan.

    Restore uses ``apply + drop`` (not ``pop``) so that an apply-time
    conflict leaves the entry in the stash list — letting us extract
    the patch via ``git stash show -p`` and explicitly drop it. With
    plain ``pop``, a conflict consumes the entry, so the patch is only
    available before the conflict materializes.

    Steps:

    1. If ``stash_ref`` is ``None`` (clean tree at spawn-time), return
       ``None`` immediately — no-op.
    2. Resolve ``stash_ref`` (40-char SHA) back to the current stack
       position via :func:`_find_stash_by_sha`. If not found (e.g.
       Allen manually ``git stash drop``-ed it), log a WARNING and
       return ``None`` — the stash is gone, restore is a no-op.
    3. ``git -C cwd stash apply <resolved>``.
    4. If apply returned non-zero AND the output indicates a conflict,
       preserve the stash as an artifact. Two shapes qualify:

       - **Merge conflict** (``"conflict"`` substring): Codex committed
         its edit, so apply wrote conflict markers into the tree.
       - **Overwrite abort** (``"would be overwritten"`` substring):
         Codex left an *uncommitted* edit, so apply aborted pre-merge
         and the tree is untouched.

       Either way:

       a. Read the stash patch via
          ``git -C cwd stash show -p <resolved>`` (works even though
          apply failed).
       b. Write to ``<artifact_dir>/run_<run_id>/conflict.patch``.
       c. Best-effort ``git -C cwd stash drop <resolved>`` so the
          orphan stash doesn't linger in ``git stash list``.
       d. For the **merge-conflict** shape only, ``git -C cwd reset
          --hard HEAD`` drops the markers — Codex's committed changes
          are authoritative. The **overwrite-abort** shape is NOT
          reset: the tree already holds Codex's uncommitted edits and a
          reset would destroy them. Per ADR-0002 lines 696-705, **never
          silently overwrite Allen's work** — the stash patch is the
          recoverable handle.
       e. Return :class:`StashConflictArtifact`.

    5. If apply returned non-zero for any other reason, raise
       :class:`StashError` with stderr attached.
    6. If apply succeeded cleanly, ``git stash drop <resolved>`` —
       apply leaves the entry in place, so we must drop it
       explicitly. A drop failure raises :class:`StashError`.

    Args:
        cwd: Target repo working directory.
        stash_ref: 40-char SHA returned by
            :func:`isolate_pretask_changes`, or ``None``.
        artifact_dir: Root artifact directory; the conflict patch
            lands at ``<artifact_dir>/run_<run_id>/conflict.patch``.
        run_id: Run identifier (must match the one used by
            :func:`write_diff_artifact`).

    Returns:
        :class:`StashConflictArtifact` when a conflict was preserved;
        ``None`` on clean apply, missing stash, or when there was
        nothing to restore.

    Raises:
        StashError: when ``git stash list`` fails, a non-conflict
            ``git stash apply`` failure occurs, ``git stash show -p``
            fails on the conflict path, ``git reset --hard`` fails,
            or the clean-apply ``git stash drop`` fails.
    """
    if stash_ref is None:
        return None

    resolved = _find_stash_by_sha(cwd, stash_ref)
    if resolved is None:
        _LOGGER.warning(
            "jarvis pre-task stash %s not found in git stash list; "
            "restore is a no-op (was it manually dropped?)",
            stash_ref,
        )
        return None

    apply = _git("stash", "apply", resolved, cwd=cwd)
    if apply.returncode == 0:
        drop = _git("stash", "drop", resolved, cwd=cwd)
        if drop.returncode != 0:
            msg = (
                f"git stash drop failed after clean apply "
                f"(rc={drop.returncode}): {drop.stderr.strip()}"
            )
            raise StashError(msg)
        return None

    combined = f"{apply.stdout}\n{apply.stderr}".lower()
    # Two distinct conflict shapes reach this point:
    #  - merge conflict: Codex committed its edit, so apply attempted a
    #    3-way merge and wrote CONFLICT markers into the tree.
    #  - overwrite abort: Codex left an *uncommitted* edit, so apply
    #    aborted pre-merge ("would be overwritten by merge") and the
    #    tree is untouched — it still holds Codex's edits.
    is_merge_conflict = "conflict" in combined
    is_overwrite_abort = "would be overwritten" in combined
    if not (is_merge_conflict or is_overwrite_abort):
        msg = (
            f"git stash apply failed non-conflict (rc={apply.returncode}): "
            f"{apply.stderr.strip()}"
        )
        raise StashError(msg)

    # Conflict path: preserve the stash as a patch artifact. show -p
    # reads the stash diff even though apply failed, so the user's work
    # is recoverable in either shape.
    show = _git("stash", "show", "-p", resolved, cwd=cwd)
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
    _git("stash", "drop", resolved, cwd=cwd)

    # Restore the tree to the post-Codex state. Only the merge-conflict
    # shape wrote markers into the tree, so only it needs a reset. The
    # overwrite-abort tree already holds Codex's uncommitted edits — a
    # reset --hard there would destroy Codex's work, which the
    # Codex-authoritative policy must never do.
    if is_merge_conflict and not is_overwrite_abort:
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
