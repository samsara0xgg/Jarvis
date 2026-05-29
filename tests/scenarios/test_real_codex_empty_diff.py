"""Tier-2 L1 acceptance — real Codex, task with no verify_command.

Per ADR-0002 § Acceptance L1 (Step 20) + § Evidence ladder §8.5 rule 6.
Increment 2 of the real-Codex burn (design doc
``docs/superpowers/specs/2026-05-28-real-codex-tier2-jkl-burn-design.md``).

Scenario
--------

The D-1 ``task.created`` is seeded WITHOUT a ``verify_command``. Codex
makes a benign, additive change (creates ``NOTES.md``) so the diff is
non-empty, but with no ``verify_command`` the ``verify_diff`` handler
returns an observation-only bundle (one slot, no verification slot).
The F2 ladder therefore cannot reach ``level=verified`` — it records an
Artifact Claim for the captured diff plus a §8.5-rule-6 Limitation Claim
("no verify_command → postcondition cannot reach level=verified"), and
emits no ``task.verified``.

This is the deterministic L1 manifestation: ``verify_command`` absence is
controlled by the seed (not by Codex behaviour), so the observation-only
ladder row fires reliably on a single burn. (The true empty-diff row —
Codex produces no diff at all → Execution Claim + missing-diff Limitation
— is the distinct §8.5-rule-6 empty-diff path; it is deterministically
covered by the Fix-2 unit tests AND, since the Increment-2 route-A burn,
by ``live_real_codex_empty_diff`` / ``test_l1_empty_diff_*`` below.)

Two coexisting routes through the same §8.5-rule-6 family:

- **Route B** (``live_real_codex_no_verify`` / ``test_l1_no_verify_*``):
  no ``verify_command``, NON-empty diff (Codex creates NOTES.md). The
  observation handler emits an Artifact Claim; the ``verification_slot
  is None`` branch then emits a ``missing_verify_command`` Limitation
  (``result_interpreter`` line 503 ``if diff_nonempty:``).
- **Route A** (``live_real_codex_empty_diff`` / ``test_l1_empty_diff_*``):
  no ``verify_command``, EMPTY diff (Codex makes no change). The
  observation handler takes the empty-diff branch and emits an Execution
  Claim + a ``missing_diff_artifact`` Limitation (``result_interpreter``
  lines 438-472); the ``verification_slot is None`` branch then SKIPS the
  ``missing_verify_command`` row (``if diff_nonempty:`` is False — the
  empty-diff path already carries its own §8.5 row). The discriminator
  between the two routes is therefore ``source_id``:
  ``missing_diff_artifact`` (route A) vs ``missing_verify_command``
  (route B), never both.

Expected L3 outcome (ADR L1 row + ``result_interpreter``
``verification_slot is None`` branch, lines 499-520):

- ONE ``action.result_observed`` for ``verify_diff`` with
  ``semantics="observation"`` (no verification / error slot).
- An ``Artifact`` ``claim.created`` for the captured diff
  (``evidence level=observed, relation=supports, source_id=diff_capture``).
- A ``Limitation`` ``claim.created`` whose ``evidence.attached`` row
  carries ``level=reported, relation=limits,
  source_id=missing_verify_command``.
- NO ``Postcondition`` claim and NO ``verified``-level evidence.
- NO ``task.verified`` and NO ``task.no_op`` (F2 verdict ``"neither"``).
- ``gate.evaluated(pre_emit).outcome == "force_limitation_language"`` and
  the surface carries no completion-class language.

Fixture pattern mirrors ``test_real_codex_flagship.py::live_real_codex_happy``:
module-scoped, run ONCE against the cloud, freeze the trace, each test
asserts one slice.

Cost estimate: ~$1 per invocation (one Codex turn + reviewer LLM call).

Invocation::

    uv run pytest tests/scenarios/test_real_codex_empty_diff.py \\
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

from jarvis.decision.pre_emit_phrases import COMPLETION_REGEXES
from jarvis.runtime import bootstrap_runtime_app, run_turn
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    from collections.abc import Iterator


pytestmark = [pytest.mark.live_codex, pytest.mark.live_llm]


# The canonical D-day utterance (ADR § Tier 2 invocation), verbatim CJK.
_UTTERANCE: str = "昨天那个 task 给 codex 跑一下，做完审核了再告诉我。"  # noqa: RUF001

# Benign, additive goal: Codex creates a top-level NOTES.md (non-empty
# diff). With no verify_command seeded, the F2 ladder stays at the
# observation-only row.
_NO_VERIFY_GOAL: str = (
    "Create a top-level NOTES.md file at the repository root that briefly "
    "describes this demo project. Make only that one additive change."
)

# Route A — strictly read-only goal: induce an EMPTY diff so the §8.5
# rule-6 empty-diff branch (Execution Claim + missing_diff_artifact
# Limitation) fires. Worded forcefully because inducing zero diff is a
# Codex-behaviour gamble (any stray scratch file flips diff_nonempty to
# True via the B-0014 untracked-aware capture).
_EMPTY_DIFF_GOAL: str = (
    "This is a STRICTLY READ-ONLY inspection task. Do NOT create, modify, "
    "rename, or delete ANY file. Do NOT run any command that writes to "
    "disk. Read the existing project files and, in your submit_report "
    "summary, briefly describe what the demo project contains (its package "
    "name and its single test). Make ZERO changes to the working tree."
)


def _git(repo: Path, *args: str) -> None:
    """Run a git subcommand in ``repo`` (test-fixture helper)."""
    subprocess.run(["git", "-C", str(repo), *args], check=True)


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
def live_real_codex_no_verify(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Run the real-Codex no-verify_command scenario ONCE; freeze the trace.

    Module-scoped so the cloud LLM + real Codex subprocess are exercised
    a single time across the L1 assertion. Seeds ``task.created`` WITHOUT
    a ``verify_command`` so the ``verify_diff`` bundle is observation-only
    while Codex still produces a non-empty diff (NOTES.md).
    """
    pytest.importorskip("openai")

    repo = tmp_path_factory.mktemp("no_verify_repo")
    _git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.1'\nrequires-python = '>=3.12'\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_demo.py").write_text(
        "def test_truthy() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    root = tmp_path_factory.mktemp("no_verify_root")
    os.environ["JARVIS_RUNTIME_ROOT"] = str(root)
    runtime = bootstrap_runtime_app(runtime_root=root)

    # NB: payload intentionally omits ``verify_command`` — that absence is
    # the whole point of the L1 observation-only row.
    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": _NO_VERIFY_GOAL,
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
    dump_path = artifact_dir / "real_codex_no_verify_trace.json"
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


def test_l1_no_verify_command_observation_only_no_task_verified(
    live_real_codex_no_verify: dict[str, Any],
) -> None:
    """Verify a task with no verify_command stays observation-only with no task.verified.

    Asserts the deterministic L1 ladder row
    (``result_interpreter._emit_verification_rows`` ``verification_slot
    is None`` branch, lines 499-520):

    - the worker reported a clean non-empty diff (the run reached
      ``verify_diff``);
    - ``verify_diff`` produced exactly ONE observation slot — no
      ``verification`` and no ``error`` slot (there was no command to run);
    - an ``Artifact`` claim for the captured diff;
    - a ``Limitation`` claim backed by ``evidence.attached`` at
      ``level=reported, relation=limits, source_id=missing_verify_command``;
    - no ``Postcondition`` claim and no ``verified``-level evidence;
    - neither ``task.verified`` nor ``task.no_op`` (verdict ``"neither"``);
    - the Pre-emit Gate forces limitation language and the surface carries
      no completion-class language.
    """
    cap = live_real_codex_no_verify

    # The run reached verify_diff: Codex completed a turn with a diff.
    reported = _payloads(cap, "worker.reported")
    assert reported, "no worker.reported — Codex did not complete a turn"
    assert reported[0]["status"] == "ok"

    # (a) verify_diff is observation-only — no verification / error slot.
    observed = _payloads(cap, "action.result_observed")
    verify_semantics = sorted(
        p["semantics"]
        for p in observed
        if p.get("semantics") in {"observation", "verification", "error"}
    )
    assert verify_semantics == ["observation"], (
        f"expected exactly one observation verify_diff slot, got {verify_semantics}"
    )

    # (b) Artifact claim for the captured diff.
    claims = _payloads(cap, "claim.created")
    assert any(c["type"] == "Artifact" for c in claims), "no Artifact claim for the diff"

    # (c) Limitation claim with reported/limits/missing_verify_command evidence.
    limitation_ids = {c["claim_id"] for c in claims if c["type"] == "Limitation"}
    assert limitation_ids, "no Limitation claim for the missing verify_command"
    evidence = _payloads(cap, "evidence.attached")
    missing_verify = [
        e
        for e in evidence
        if e["claim_id"] in limitation_ids
        and e.get("level") == "reported"
        and e.get("relation") == "limits"
        and e.get("source_id") == "missing_verify_command"
    ]
    assert missing_verify, (
        "no evidence.attached(level=reported, relation=limits, "
        "source_id=missing_verify_command) on the Limitation claim"
    )

    # (d) No Postcondition claim and no verified-level evidence.
    assert not any(c["type"] == "Postcondition" for c in claims), (
        "no-verify_command must not produce a Postcondition claim"
    )
    assert not any(e.get("level") == "verified" for e in evidence), (
        "no-verify_command must not produce verified-level evidence"
    )

    # (e) No completion event of either kind (F2 verdict == "neither").
    assert not _payloads(cap, "task.verified"), "task.verified must not fire without verify_command"
    assert not _payloads(cap, "task.no_op"), (
        "task.no_op must not fire here (non-empty diff + no verify_command → verdict 'neither')"
    )

    # (f) Pre-emit gate forces limitation language; surface claims no completion.
    pre_emit_gates = [
        p for p in _payloads(cap, "gate.evaluated") if p.get("gate") == "pre_emit"
    ]
    assert pre_emit_gates, "no pre_emit gate.evaluated event"
    assert pre_emit_gates[-1]["outcome"] == "force_limitation_language", (
        f"expected force_limitation_language, got {pre_emit_gates[-1]['outcome']!r}"
    )
    emitted = _payloads(cap, "surface.response_emitted")
    assert len(emitted) == 1
    text = emitted[0]["text"]
    assert not any(rx.search(text) for rx in COMPLETION_REGEXES), (
        f"no-verify_command surface must not claim completion; got: {text!r}"
    )


@pytest.fixture(scope="module")
def live_real_codex_empty_diff(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Run the real-Codex TRUE empty-diff (route A) scenario ONCE; freeze the trace.

    Module-scoped so the cloud LLM + real Codex subprocess are exercised a
    single time across the route-A assertion. Seeds ``task.created`` WITHOUT
    a ``verify_command`` and with a strictly read-only goal so Codex
    completes a turn but produces NO diff (``diff_nonempty == False``),
    driving the ``result_interpreter`` empty-diff branch (lines 438-472):
    an Execution Claim + a §8.5-rule-6 ``missing_diff_artifact`` Limitation.
    """
    pytest.importorskip("openai")

    repo = tmp_path_factory.mktemp("empty_diff_repo")
    _git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.1'\nrequires-python = '>=3.12'\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_demo.py").write_text(
        "def test_truthy() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    root = tmp_path_factory.mktemp("empty_diff_root")
    os.environ["JARVIS_RUNTIME_ROOT"] = str(root)
    runtime = bootstrap_runtime_app(runtime_root=root)

    # NB: payload omits ``verify_command`` (route A needs verdict "neither",
    # NOT the empty-diff + verify-pass "no_op" path). The read-only goal is
    # what induces the empty diff.
    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": _EMPTY_DIFF_GOAL,
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
    dump_path = artifact_dir / "real_codex_empty_diff_trace.json"
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


def test_l1_empty_diff_execution_claim_and_missing_diff_limitation(
    live_real_codex_empty_diff: dict[str, Any],
) -> None:
    """Verify a TRUE empty diff (route A) takes the §8.5-rule-6 Execution branch.

    Asserts the deterministic empty-diff ladder rows
    (``result_interpreter`` empty-diff branch lines 438-472 +
    ``verification_slot is None`` branch line 499-520):

    - the worker reported (the run reached ``verify_diff``);
    - ``verify_diff`` produced exactly ONE observation slot whose
      ``tool_output`` carries ``diff_nonempty == False``;
    - an ``Execution`` claim backed by ``evidence.attached`` at
      ``level=executed, relation=supports, source_id=spawn_worker``;
    - a ``Limitation`` claim backed by ``evidence.attached`` at
      ``level=reported, relation=limits, source_id=missing_diff_artifact``;
    - NO ``missing_verify_command`` evidence anywhere (the route-A vs
      route-B discriminator — the empty-diff path skips that row);
    - no ``Postcondition`` claim and no ``verified``-level evidence;
    - neither ``task.verified`` nor ``task.no_op`` (verdict ``"neither"``);
    - the Pre-emit Gate forces limitation language and the surface carries
      no completion-class language.
    """
    cap = live_real_codex_empty_diff

    # The run reached verify_diff: Codex completed a turn (with no diff).
    reported = _payloads(cap, "worker.reported")
    assert reported, "no worker.reported — Codex did not complete a turn"
    assert reported[0]["status"] == "ok"

    # (a) verify_diff is observation-only AND the observation slot's
    # tool_output reports an empty diff. If Codex made a stray change the
    # diff would be non-empty — re-tune the goal, never weaken this.
    observed = _payloads(cap, "action.result_observed")
    verify_semantics = sorted(
        p["semantics"]
        for p in observed
        if p.get("semantics") in {"observation", "verification", "error"}
    )
    assert verify_semantics == ["observation"], (
        f"expected exactly one observation verify_diff slot, got {verify_semantics}"
    )
    observation = next(p for p in observed if p.get("semantics") == "observation")
    tool_output = json.loads(observation["tool_output"])
    assert tool_output["diff_nonempty"] is False, (
        "route A requires an EMPTY diff; Codex made a stray change "
        f"(diff_nonempty={tool_output['diff_nonempty']}) — re-tune the goal"
    )

    # (b) Execution claim for the spawn_worker run, evidence level=executed.
    claims = _payloads(cap, "claim.created")
    execution_ids = {c["claim_id"] for c in claims if c["type"] == "Execution"}
    assert execution_ids, "no Execution claim for the empty-diff spawn_worker run"
    evidence = _payloads(cap, "evidence.attached")
    execution_evidence = [
        e
        for e in evidence
        if e["claim_id"] in execution_ids
        and e.get("level") == "executed"
        and e.get("relation") == "supports"
        and e.get("source_id") == "spawn_worker"
    ]
    assert execution_evidence, (
        "no evidence.attached(level=executed, relation=supports, "
        "source_id=spawn_worker) on the Execution claim"
    )

    # (c) §8.5-rule-6 Limitation with reported/limits/missing_diff_artifact.
    limitation_ids = {c["claim_id"] for c in claims if c["type"] == "Limitation"}
    assert limitation_ids, "no Limitation claim for the missing diff artifact"
    missing_diff = [
        e
        for e in evidence
        if e["claim_id"] in limitation_ids
        and e.get("level") == "reported"
        and e.get("relation") == "limits"
        and e.get("source_id") == "missing_diff_artifact"
    ]
    assert missing_diff, (
        "no evidence.attached(level=reported, relation=limits, "
        "source_id=missing_diff_artifact) on the Limitation claim"
    )

    # (d) Route-A discriminator: the empty-diff path must NOT emit the
    # route-B missing_verify_command row (line 503 `if diff_nonempty:`).
    assert not any(e.get("source_id") == "missing_verify_command" for e in evidence), (
        "empty-diff route A must not carry a missing_verify_command row "
        "(that is the route-B no_verify non-empty-diff path)"
    )

    # (e) No Postcondition claim and no verified-level evidence.
    assert not any(c["type"] == "Postcondition" for c in claims), (
        "empty-diff must not produce a Postcondition claim"
    )
    assert not any(e.get("level") == "verified" for e in evidence), (
        "empty-diff must not produce verified-level evidence"
    )

    # (f) No completion event of either kind (verdict == "neither", NOT
    # the empty-diff + verify-pass "no_op" path — there is no verify_command).
    assert not _payloads(cap, "task.verified"), "task.verified must not fire on empty diff"
    assert not _payloads(cap, "task.no_op"), (
        "task.no_op must not fire here (empty diff + NO verify_command → verdict 'neither')"
    )

    # (g) Pre-emit gate forces limitation language; surface claims no completion.
    pre_emit_gates = [
        p for p in _payloads(cap, "gate.evaluated") if p.get("gate") == "pre_emit"
    ]
    assert pre_emit_gates, "no pre_emit gate.evaluated event"
    assert pre_emit_gates[-1]["outcome"] == "force_limitation_language", (
        f"expected force_limitation_language, got {pre_emit_gates[-1]['outcome']!r}"
    )
    emitted = _payloads(cap, "surface.response_emitted")
    assert len(emitted) == 1
    text = emitted[0]["text"]
    assert not any(rx.search(text) for rx in COMPLETION_REGEXES), (
        f"empty-diff surface must not claim completion; got: {text!r}"
    )
