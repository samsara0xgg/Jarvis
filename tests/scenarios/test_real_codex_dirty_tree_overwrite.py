"""Tier-2 J13 acceptance — real Codex, dirty-tree stash-overwrite conflict.

Per ADR-0002 § Dirty-tree policy (lines 663-713) + J13. The live
counterpart of the deterministic
``test_diff_capture.py::test_dirty_tree_uncommitted_codex_edit_writes_artifact``
unit test: the unit test proves the git mechanics against real git; this
burn proves the realistic Codex lifecycle (edits a file but never
commits) actually drives that path end-to-end through the runtime.

Scenario
--------

Allen has an UNCOMMITTED pre-task edit to a tracked file (``README.md``)
when the turn starts. ``spawn_worker_handler`` stashes it via
``isolate_pretask_changes`` (the tree reverts to HEAD), then real Codex
edits the SAME file and exits WITHOUT committing. The post-verify
``_pop_pending_stashes`` finalizer calls ``restore_pretask_changes``,
whose ``git stash apply`` aborts pre-merge with "would be overwritten by
merge" (rc=1, no CONFLICT marker) because Codex's uncommitted edit
occupies the file. Per commit ``070b48b`` this is classified as a
conflict shape: the stash patch is preserved as ``conflict.patch``, the
stash is dropped, and — critically — the tree is NOT reset (a
``reset --hard`` would destroy Codex's uncommitted work).

The runtime routes the returned ``StashConflictArtifact`` through
``emit_stash_conflict_surfacing`` (L3), producing:

- ONE ``worker.artifact_observed`` (``kind="stash_conflict"``) pointing at
  the preserved patch, correlated to the spawn_worker ``action_id``;
- a ``Limitation`` ``claim.created`` + ``evidence.attached(relation=limits,
  level=reported, source_id=git_stash_pop)`` pair so the surface can tell
  Allen his pre-task work was preserved, not silently overwritten.

Codex-behaviour note: the conflict only materialises if Codex edits the
SAME tracked file Allen dirtied, so the goal forcefully targets
``README.md`` and forbids committing. If Codex edited a different file
there would be no overwrite conflict (the stash would re-apply cleanly)
and the ``worker.artifact_observed(kind=stash_conflict)`` assertion would
fail with a clear message. The final-state assertion additionally
requires the surviving edit to be UNCOMMITTED (``git status`` dirty),
pinning the burn to commit-2's new "would be overwritten" path rather
than the pre-existing committed-conflict path.

Cost estimate: ~$1 per invocation (one Codex turn + reviewer LLM call).

Invocation::

    uv run pytest tests/scenarios/test_real_codex_dirty_tree_overwrite.py \\
        --live-codex --live-llm
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import bootstrap_runtime_app, run_turn
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    from collections.abc import Iterator


pytestmark = [pytest.mark.live_codex, pytest.mark.live_llm]


# The canonical D-day utterance (ADR § Tier 2 invocation), verbatim CJK.
_UTTERANCE: str = "昨天那个 task 给 codex 跑一下，做完审核了再告诉我。"  # noqa: RUF001

# README.md content committed at init — the base both Allen and Codex
# diverge from.
_README_BASE: str = "# Demo\n\nA tiny demo project.\n"

# Allen's pre-task marker — written UNCOMMITTED to README.md before the
# turn. It must end up ONLY inside conflict.patch, never in the tree.
_ALLEN_MARKER: str = "ALLEN-PRETASK-EDIT"

# Goal: force Codex to edit the SAME tracked file Allen dirtied (so the
# stash restore hits the "would be overwritten" abort) and to NOT commit
# (so the surviving edit stays uncommitted — commit-2's path).
_DIRTY_TREE_GOAL: str = (
    "Edit the existing top-level README.md file: append exactly one new line "
    "at the very end reading 'Updated by Codex during the demo task.'. Change "
    "ONLY README.md — do NOT create, rename, or delete any other file, and do "
    "NOT run git commit or git add."
)


def _git(repo: Path, *args: str) -> None:
    """Run a git subcommand in ``repo`` (test-fixture helper)."""
    subprocess.run(["git", "-C", str(repo), *args], check=True)


def _git_porcelain(repo: Path) -> str:
    """Return ``git status --porcelain`` output for ``repo``."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _load_trace(db_path: Path) -> list[dict[str, Any]]:
    """Return every ``events`` row as a dict (rowid-ordered, payload parsed)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM events ORDER BY rowid").fetchall()
    finally:
        conn.close()
    trace: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        payload_json = record.get("payload_json")
        if isinstance(payload_json, str):
            record["payload"] = json.loads(payload_json)
        trace.append(record)
    return trace


def _payloads(capture: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    """All payloads of ``event_type`` in the captured trace (in order)."""
    return [event["payload"] for event in capture["trace"] if event["type"] == event_type]


@pytest.fixture(scope="module")
def live_real_codex_dirty_tree(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Run the real-Codex dirty-tree stash-overwrite scenario ONCE; freeze the trace.

    Module-scoped so the cloud LLM + real Codex subprocess are exercised a
    single time across the J13 assertions. The repo carries a committed
    ``README.md`` that Allen then dirties (uncommitted) before the turn,
    and the goal steers Codex to edit that same file without committing.
    """
    pytest.importorskip("openai")

    repo = tmp_path_factory.mktemp("dirty_tree_repo")
    _git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.1'\nrequires-python = '>=3.12'\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(_README_BASE, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    # Allen's UNCOMMITTED pre-task edit to the tracked README.md. Present
    # in the working tree when spawn_worker's isolate_pretask_changes runs.
    (repo / "README.md").write_text(
        _README_BASE + _ALLEN_MARKER + "\n",
        encoding="utf-8",
    )

    root = tmp_path_factory.mktemp("dirty_tree_root")
    os.environ["JARVIS_RUNTIME_ROOT"] = str(root)
    runtime = bootstrap_runtime_app(runtime_root=root)

    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": _DIRTY_TREE_GOAL,
            "source": "manual",
            "repo_path": str(repo),
        },
        ts_epoch_ms=yesterday_ms,
    )

    result = run_turn(runtime, utterance=_UTTERANCE)
    time.sleep(0.3)  # let any straggling emit finalize before the snapshot

    db_path = runtime.runtime_paths.event_log
    trace = _load_trace(db_path)

    artifact_dir = Path(__file__).resolve().parent.parent / "_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    dump_path = artifact_dir / "real_codex_dirty_tree_overwrite_trace.json"
    dump_path.write_text(
        json.dumps(trace, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    captured: dict[str, Any] = {
        "runtime": runtime,
        "result": result,
        "db_path": db_path,
        "repo": repo,
        "trace": trace,
        "dump_path": dump_path,
    }
    try:
        yield captured
    finally:
        runtime.conn.close()


def test_j13_isolate_stashed_allen_pretask_edit(
    live_real_codex_dirty_tree: dict[str, Any],
) -> None:
    """spawn_worker stashed Allen's dirty tree → worker.reported carries a stash_ref.

    The run reached Codex (``worker.reported`` exists) and, because the
    tree was dirty at spawn-time, ``isolate_pretask_changes`` returned a
    40-char stash SHA that travelled onto the report payload.
    """
    cap = live_real_codex_dirty_tree

    reported = _payloads(cap, "worker.reported")
    assert reported, "no worker.reported — Codex did not complete a turn"
    stash_ref = reported[0].get("stash_ref")
    assert isinstance(stash_ref, str), (
        f"expected a stash SHA string on worker.reported (dirty tree was "
        f"isolated), got {stash_ref!r}"
    )
    assert len(stash_ref) == 40, f"expected a 40-char stash SHA, got {stash_ref!r}"


def test_j13_stash_overwrite_surfaces_conflict_artifact(
    live_real_codex_dirty_tree: dict[str, Any],
) -> None:
    """The overwrite conflict surfaced as worker.artifact_observed(kind=stash_conflict).

    Exactly one ``stash_conflict`` artifact row points at a
    ``conflict.patch`` that preserves Allen's pre-task edit — proving the
    stash was not lost and the conflict was made visible (not a silent
    file write).
    """
    cap = live_real_codex_dirty_tree

    artifacts = [
        p for p in _payloads(cap, "worker.artifact_observed") if p.get("kind") == "stash_conflict"
    ]
    assert len(artifacts) == 1, (
        f"expected exactly one stash_conflict artifact (did Codex edit "
        f"README.md so the stash restore conflicted?), got {len(artifacts)}"
    )

    patch_path = Path(artifacts[0]["artifact_path"])
    assert patch_path.is_file(), f"conflict patch not written at {patch_path}"
    assert patch_path.name == "conflict.patch"
    assert _ALLEN_MARKER in patch_path.read_text(encoding="utf-8"), (
        "conflict.patch must preserve Allen's pre-task edit for manual reconciliation"
    )


def test_j13_stash_overwrite_emits_limitation_claim(
    live_real_codex_dirty_tree: dict[str, Any],
) -> None:
    """The conflict produced a Limitation claim + git_stash_pop evidence.

    ADR-0002 § Dirty-tree policy: the surface must be able to tell Allen
    his work was preserved, not silently overwritten. That is carried by a
    ``Limitation`` ``claim.created`` whose ``evidence.attached`` row is
    ``relation=limits, level=reported, source_id=git_stash_pop``.
    """
    cap = live_real_codex_dirty_tree

    claims = _payloads(cap, "claim.created")
    limitation_ids = {c["claim_id"] for c in claims if c["type"] == "Limitation"}
    assert limitation_ids, "no Limitation claim for the stash-pop conflict"

    evidence = _payloads(cap, "evidence.attached")
    stash_pop = [
        e
        for e in evidence
        if e["claim_id"] in limitation_ids
        and e.get("relation") == "limits"
        and e.get("level") == "reported"
        and e.get("source_id") == "git_stash_pop"
        and e.get("reason") == "stash_pop_conflict"
    ]
    assert stash_pop, (
        "no evidence.attached(relation=limits, level=reported, "
        "source_id=git_stash_pop, reason=stash_pop_conflict) on the Limitation claim"
    )


def test_j13_codex_uncommitted_edit_survived_no_reset(
    live_real_codex_dirty_tree: dict[str, Any],
) -> None:
    """Codex's uncommitted edit survived — the overwrite path did NOT reset the tree.

    The decisive proof of commit-2's fix: the old code reset --hard HEAD
    on every conflict, which would have wiped Codex's UNCOMMITTED edit back
    to ``_README_BASE``. The new code skips reset for the overwrite-abort
    shape, so the tree still holds Codex's change AND that change is still
    uncommitted (``git status`` dirty). Allen's marker lives only in the
    preserved patch, never in the working tree.
    """
    cap = live_real_codex_dirty_tree
    repo: Path = cap["repo"]

    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert _ALLEN_MARKER not in readme, (
        "Allen's pre-task edit must NOT be in the working tree — it is "
        "isolated into conflict.patch, not silently merged onto Codex's edit"
    )
    assert readme != _README_BASE, (
        "README.md was reset to its committed base — Codex's uncommitted "
        "edit was destroyed (the buggy reset --hard path)"
    )

    porcelain = _git_porcelain(repo)
    assert "README.md" in porcelain, (
        "README.md should show as uncommitted-modified (Codex edited but did "
        f"not commit, and no reset ran); git status:\n{porcelain}"
    )
