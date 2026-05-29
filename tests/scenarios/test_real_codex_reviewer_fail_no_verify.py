"""Tier-2 L5 acceptance — reviewer verdict=fail with no verify_command.

Per ADR-0002 § Acceptance L5 (Step 20) + spec §8.5 rule 1 / §13.2 I10.
Increment 2 of the real-Codex burn (design doc
``docs/superpowers/specs/2026-05-28-real-codex-tier2-jkl-burn-design.md``).

Scenario
--------

The D-1 ``task.created`` is seeded WITHOUT a ``verify_command`` and with
an ambitious, multi-requirement goal whose success can NOT be confirmed
from a static diff (concurrency-correctness + "all tests pass / 100%
coverage" claims). Codex produces a non-empty diff (real code), but the
L3 reviewer — instructed to return ``"fail"`` when uncertain (no
hedging) — cannot confirm the strong guarantees from the diff alone and
returns ``verdict="fail"``.

This proves the reviewer-advisory contract: with no ``verify_command`` the
F2 verdict is ``"neither"`` (no ``task.verified``) REGARDLESS of the
reviewer, and the reviewer's "fail" verdict is recorded only as a
``reported``-grade ``refutes`` evidence row — it can never raise a claim
to ``verified`` (spec §13.2 I10) and never produces ``task.verified``
(reviewer is advisory, no veto — the dual of the happy-path finding).

Expected L5 outcome (ADR L5 row + ``result_interpreter`` reviewer-rows,
lines 575-619):

- ONE ``action.result_observed`` for ``verify_diff`` with
  ``semantics="observation"`` (no verification / error slot).
- An ``Artifact`` claim for the captured diff.
- A §8.5-rule-6 ``Limitation`` claim
  (``evidence level=reported, relation=limits,
  source_id=missing_verify_command``) for the absent verify_command.
- A reviewer ``evidence.attached`` row (attached to the Artifact claim)
  at ``level=reported, relation=refutes, source_type=llm,
  source_id=reviewer`` — the reviewer's "fail" verdict, Report-grade.
- NO ``Postcondition`` claim and NO ``verified``-level evidence.
- NO ``task.verified`` and NO ``task.no_op`` (verdict ``"neither"``).
- ``gate.evaluated(pre_emit).outcome == "force_limitation_language"``;
  surface carries no completion-class language.

Note: the reviewer verdict is a live-LLM judgment and therefore not
strictly deterministic. The goal is tuned to elicit ``"fail"`` (ambitious
+ unverifiable-from-diff, leaning on the reviewer's uncertain→fail rule);
every assertion that does NOT depend on the verdict is the binding
deterministic contract.

Fixture pattern mirrors ``test_real_codex_flagship.py::live_real_codex_happy``:
module-scoped, run ONCE against the cloud, freeze the trace.

Cost estimate: ~$1.50 per invocation (one Codex turn + reviewer LLM call).

Invocation::

    uv run pytest tests/scenarios/test_real_codex_reviewer_fail_no_verify.py \\
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

# Ambitious, multi-requirement goal whose success CANNOT be confirmed from
# a static diff (concurrency correctness + "all tests pass / 100% coverage"
# claims) and there is no verify_command to settle it. Codex still produces
# a real non-empty diff; the reviewer, told to return "fail" when uncertain,
# refutes it.
_REVIEWER_FAIL_GOAL: str = (
    "Implement a complete, production-ready, thread-safe in-memory key-value "
    "store in a new file kvstore.py: get / set / delete / keys, full type "
    "hints, a thorough docstring, and lock-based thread-safety that is "
    "provably correct under 64 concurrent threads. Also add a complete pytest "
    "suite in tests/test_kvstore.py that achieves 100% line coverage with "
    "every test passing, including concurrency stress tests. The 100% "
    "coverage and all-tests-pass guarantees are mandatory."
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
def live_real_codex_reviewer_fail(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Run the real-Codex reviewer-fail / no-verify scenario ONCE; freeze the trace.

    Module-scoped so the cloud LLM + real Codex subprocess are exercised a
    single time. Seeds ``task.created`` WITHOUT a ``verify_command`` and
    with an ambitious goal that the diff cannot be confirmed to satisfy, so
    the reviewer returns ``verdict="fail"`` while the F2 verdict stays
    ``"neither"`` (no ``task.verified``).
    """
    pytest.importorskip("openai")

    repo = tmp_path_factory.mktemp("reviewer_fail_repo")
    _git(repo, "init", "-q")
    (repo / "README.md").write_text(
        "# Demo\n\nA throw-away repo for the L5 reviewer-fail acceptance.\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    root = tmp_path_factory.mktemp("reviewer_fail_root")
    os.environ["JARVIS_RUNTIME_ROOT"] = str(root)
    runtime = bootstrap_runtime_app(runtime_root=root)

    # NB: payload omits ``verify_command`` — the reviewer is the only
    # judgment source, and with no verify_command the verdict stays
    # "neither" regardless of what the reviewer says.
    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": _REVIEWER_FAIL_GOAL,
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
    dump_path = artifact_dir / "real_codex_reviewer_fail_trace.json"
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


def test_l5_reviewer_fail_no_verify_command_emits_refutes_no_task_verified(
    live_real_codex_reviewer_fail: dict[str, Any],
) -> None:
    """Verify reviewer=fail + no verify_command yields a refutes evidence row and no task.verified.

    Deterministic contract (independent of the reviewer verdict):

    - the worker reported a clean non-empty diff (the run reached
      ``verify_diff``);
    - ``verify_diff`` produced exactly one ``observation`` slot (no
      verification / error slot — there was no command to run);
    - an ``Artifact`` claim plus a §8.5-rule-6 ``Limitation`` claim with
      ``evidence(level=reported, relation=limits,
      source_id=missing_verify_command)``;
    - no ``Postcondition`` claim and no ``verified``-level evidence;
    - neither ``task.verified`` nor ``task.no_op`` (verdict ``"neither"``);
    - the Pre-emit Gate forces limitation language and the surface carries
      no completion-class language;
    - the reviewer ran (a ``cost.recorded(kind="reviewer")`` row) and is
      recorded as a ``reported``-grade evidence row (``source_id=reviewer,
      source_type=llm``) — it never reaches ``verified`` (spec §13.2 I10).

    Reviewer-verdict-specific (goal tuned to elicit ``"fail"``): that
    reviewer evidence row carries ``relation="refutes"``.
    """
    cap = live_real_codex_reviewer_fail

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

    claims = _payloads(cap, "claim.created")
    evidence = _payloads(cap, "evidence.attached")

    # (b) Artifact claim + §8.5-rule-6 missing-verify Limitation.
    assert any(c["type"] == "Artifact" for c in claims), "no Artifact claim for the diff"
    limitation_ids = {c["claim_id"] for c in claims if c["type"] == "Limitation"}
    assert limitation_ids, "no Limitation claim for the missing verify_command"
    assert any(
        e["claim_id"] in limitation_ids
        and e.get("level") == "reported"
        and e.get("relation") == "limits"
        and e.get("source_id") == "missing_verify_command"
        for e in evidence
    ), "no missing_verify_command Limitation evidence row"

    # (c) No Postcondition claim and no verified-level evidence.
    assert not any(c["type"] == "Postcondition" for c in claims), (
        "reviewer-fail/no-verify must not produce a Postcondition claim"
    )
    assert not any(e.get("level") == "verified" for e in evidence), (
        "reviewer-fail/no-verify must not produce verified-level evidence"
    )

    # (d) No completion event of either kind (F2 verdict == "neither").
    assert not _payloads(cap, "task.verified"), "task.verified must not fire (no verify_command)"
    assert not _payloads(cap, "task.no_op"), "task.no_op must not fire (verdict 'neither')"

    # (e) The reviewer ran and is recorded as advisory reported-grade evidence.
    reviewer_costs = [c for c in _payloads(cap, "cost.recorded") if c.get("kind") == "reviewer"]
    assert reviewer_costs, "reviewer LLM never ran (no cost.recorded kind=reviewer)"
    reviewer_evidence = [
        e
        for e in evidence
        if e.get("source_id") == "reviewer" and e.get("source_type") == "llm"
    ]
    assert reviewer_evidence, "no reviewer evidence row"
    assert all(e.get("level") == "reported" for e in reviewer_evidence), (
        "reviewer evidence must stay at level=reported (spec §13.2 I10)"
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
        f"reviewer-fail surface must not claim completion; got: {text!r}"
    )

    # (g) Reviewer-verdict-specific: the goal is tuned to elicit verdict=fail,
    # which is recorded as a refutes evidence row.
    assert any(e.get("relation") == "refutes" for e in reviewer_evidence), (
        "expected a refutes reviewer evidence row (verdict=fail); reviewer "
        f"relations seen: {[e.get('relation') for e in reviewer_evidence]}"
    )
