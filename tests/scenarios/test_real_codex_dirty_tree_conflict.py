"""Tier-2 J13 acceptance — dirty tree + a goal that DEMANDS a commit.

Per ADR-0002 § Dirty-tree policy (lines 663-713) + J13. Companion to
``test_real_codex_dirty_tree_overwrite.py``. This burn was designed to
drive ``restore_pretask_changes``'s ``is_merge_conflict`` branch (Codex
COMMITS its conflicting edit → ``git stash apply`` writes CONFLICT
markers → patch preserved → ``reset --hard``). The 2026-07-06 burn
falsified that plan and pinned something better:

**A real Codex cannot move HEAD.** The shipped workspace-write sandbox
denies writes to ``.git/`` (Codex's own submit_report: "could not stage
or commit because writes to .git/index.lock are denied by the
sandbox"). Even with a goal that explicitly demands ``git commit`` and
a fixture repo carrying full local git identity, the turn ends with
HEAD unmoved and Codex's edit UNCOMMITTED — i.e. the stash restore
takes the overwrite-abort shape, never the merge-conflict shape. The
``is_merge_conflict`` branch is therefore live-unreachable under the
shipped sandbox; it stays deterministically proven against real git by
``tests/unit/test_diff_capture.py`` (committed-conflict case).

Scenario
--------

Allen has an UNCOMMITTED pre-task edit to a tracked file (``README.md``)
when the turn starts. ``spawn_worker_handler`` stashes it via
``isolate_pretask_changes``, then real Codex — told to edit that same
file AND commit — edits it but is DENIED the commit by the sandbox. The
post-verify ``_pop_pending_stashes`` finalizer hits the "would be
overwritten" pre-merge abort:

- the stash patch is preserved as ``conflict.patch`` (Allen's edit is
  the recoverable handle, never silently merged);
- ONE ``worker.artifact_observed`` (``kind="stash_conflict"``) points at
  the patch, plus a ``Limitation`` ``claim.created`` +
  ``evidence.attached(relation=limits, level=reported,
  source_id=git_stash_pop, reason=stash_pop_conflict)`` pair;
- the tree is NOT reset (Codex's edit is uncommitted; a reset would
  destroy it) — and, decisively, ``git rev-list --count HEAD`` is still
  1: the sandbox held the commit line.

The fixture sets repo-local git identity (and disables commit signing)
so the ONLY thing standing between Codex and a commit is the sandbox —
pinning the denial as a live security invariant rather than an
environment accident.

Cost estimate: ~$1 per invocation (one Codex turn + reviewer LLM call).

Invocation::

    uv run pytest tests/scenarios/test_real_codex_dirty_tree_conflict.py \\
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

# Codex's line — the goal pins the exact text so the final-tree
# assertion can distinguish Codex's committed edit from the base.
_CODEX_LINE: str = "Updated by Codex during the demo task."

# Goal: force Codex to edit the SAME tracked file Allen dirtied AND
# demand a commit. The commit is expected to be DENIED by the sandbox
# (.git writes blocked) — the demand is what makes the denial a pinned
# invariant instead of an untested assumption.
_CONFLICT_GOAL: str = (
    "Edit the existing top-level README.md file: append exactly one new line "
    f"at the very end reading '{_CODEX_LINE}'. Then commit that change with "
    "git (stage README.md and run git commit -m 'codex: update readme'). "
    "Change ONLY README.md and make exactly ONE commit — do NOT create, "
    "rename, or delete any other file."
)


def _git(repo: Path, *args: str) -> None:
    """Run a git subcommand in ``repo`` (test-fixture helper)."""
    subprocess.run(["git", "-C", str(repo), *args], check=True)


def _git_stdout(repo: Path, *args: str) -> str:
    """Return stdout of a git subcommand in ``repo``."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
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
def live_real_codex_stash_conflict(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Run the real-Codex dirty-tree merge-conflict scenario ONCE; freeze the trace.

    Module-scoped so the cloud LLM + real Codex subprocess are exercised a
    single time across the J13 assertions. The repo carries a committed
    ``README.md`` that Allen then dirties (uncommitted) before the turn;
    the goal steers Codex to edit that same file AND commit it, so the
    post-verify stash restore hits a true 3-way merge conflict.
    """
    pytest.importorskip("openai")

    repo = tmp_path_factory.mktemp("stash_conflict_repo")
    _git(repo, "init", "-q")
    # Repo-local identity + no signing so Codex's own `git commit` cannot
    # fail on machine-level config (Allen's global gitconfig may enable
    # gpgsign; the demo repo must be self-sufficient).
    _git(repo, "config", "user.email", "codex@demo.local")
    _git(repo, "config", "user.name", "Codex Demo")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.1'\nrequires-python = '>=3.12'\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(_README_BASE, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")

    # Allen's UNCOMMITTED pre-task edit to the tracked README.md. Present
    # in the working tree when spawn_worker's isolate_pretask_changes runs.
    (repo / "README.md").write_text(
        _README_BASE + _ALLEN_MARKER + "\n",
        encoding="utf-8",
    )

    root = tmp_path_factory.mktemp("stash_conflict_root")
    os.environ["JARVIS_RUNTIME_ROOT"] = str(root)
    runtime = bootstrap_runtime_app(runtime_root=root)

    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": _CONFLICT_GOAL,
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
    dump_path = artifact_dir / "real_codex_dirty_tree_conflict_trace.json"
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


def test_j13_conflict_isolate_stashed_allen_pretask_edit(
    live_real_codex_stash_conflict: dict[str, Any],
) -> None:
    """spawn_worker stashed Allen's dirty tree → worker.reported carries a stash_ref.

    Identical isolation contract to the overwrite burn: the tree was
    dirty at spawn-time, so ``isolate_pretask_changes`` returned a
    40-char stash SHA that travelled onto the report payload.
    """
    cap = live_real_codex_stash_conflict

    reported = _payloads(cap, "worker.reported")
    assert reported, "no worker.reported — Codex did not complete a turn"
    stash_ref = reported[0].get("stash_ref")
    assert isinstance(stash_ref, str), (
        f"expected a stash SHA string on worker.reported (dirty tree was "
        f"isolated), got {stash_ref!r}"
    )
    assert len(stash_ref) == 40, f"expected a 40-char stash SHA, got {stash_ref!r}"


def test_j13_conflict_surfaces_conflict_patch_artifact(
    live_real_codex_stash_conflict: dict[str, Any],
) -> None:
    """The merge conflict surfaced as worker.artifact_observed(kind=stash_conflict).

    Exactly one ``stash_conflict`` artifact row points at a
    ``conflict.patch`` that preserves Allen's pre-task edit — proving the
    stash was not lost when Codex's committed edit won the tree.
    """
    cap = live_real_codex_stash_conflict

    artifacts = [
        p for p in _payloads(cap, "worker.artifact_observed") if p.get("kind") == "stash_conflict"
    ]
    assert len(artifacts) == 1, (
        f"expected exactly one stash_conflict artifact (did Codex edit "
        f"README.md AND commit it so the stash apply merge-conflicted?), "
        f"got {len(artifacts)}"
    )

    patch_path = Path(artifacts[0]["artifact_path"])
    assert patch_path.is_file(), f"conflict patch not written at {patch_path}"
    assert patch_path.name == "conflict.patch"
    assert _ALLEN_MARKER in patch_path.read_text(encoding="utf-8"), (
        "conflict.patch must preserve Allen's pre-task edit for manual reconciliation"
    )


def test_j13_conflict_emits_limitation_claim(
    live_real_codex_stash_conflict: dict[str, Any],
) -> None:
    """The conflict produced a Limitation claim + git_stash_pop evidence.

    ADR-0002 § Dirty-tree policy: the surface must be able to tell Allen
    his work was preserved, not silently overwritten. Same contract as
    the overwrite burn: a ``Limitation`` ``claim.created`` whose
    ``evidence.attached`` row is ``relation=limits, level=reported,
    source_id=git_stash_pop, reason=stash_pop_conflict``.
    """
    cap = live_real_codex_stash_conflict

    claims = _payloads(cap, "claim.created")
    limitation_ids = {c["claim_id"] for c in claims if c["type"] == "Limitation"}
    assert limitation_ids, "no Limitation claim for the stash merge conflict"

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


def test_j13_sandbox_denies_codex_commit_head_unmoved(
    live_real_codex_stash_conflict: dict[str, Any],
) -> None:
    """The sandbox held the commit line: HEAD never moved, Codex's edit stays uncommitted.

    The goal explicitly demanded ``git commit`` and the repo carries full
    local git identity, so the ONLY veto left is the workspace-write
    sandbox's ``.git/`` write denial. ``rev-list --count HEAD == 1`` pins
    that denial live — which is also exactly WHY the stash restore can
    only ever take the overwrite-abort shape (tree dirty, no reset) and
    the ``is_merge_conflict`` branch stays a unit-proven defense
    (``tests/unit/test_diff_capture.py`` committed-conflict case).
    """
    cap = live_real_codex_stash_conflict
    repo: Path = cap["repo"]

    # The live security invariant: a sandboxed Codex cannot move HEAD.
    commit_count = int(_git_stdout(repo, "rev-list", "--count", "HEAD").strip())
    assert commit_count == 1, (
        f"expected HEAD to stay at the fixture's init commit (sandbox denies "
        f".git writes), got {commit_count} commits — the sandbox let a real "
        "Codex commit through; re-verify the -c sandbox flags before trusting "
        "the dirty-tree policy"
    )

    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert _ALLEN_MARKER not in readme, (
        "Allen's pre-task edit must NOT be in the working tree — it is "
        "isolated into conflict.patch, not silently merged onto Codex's edit"
    )
    assert readme != _README_BASE, (
        "README.md was reset to its committed base — Codex's uncommitted "
        "edit was destroyed (the overwrite-abort shape must NOT reset)"
    )

    porcelain = _git_stdout(repo, "status", "--porcelain")
    assert "README.md" in porcelain, (
        "README.md should show as uncommitted-modified (Codex edited it but "
        f"the sandbox denied the commit); git status:\n{porcelain}"
    )
