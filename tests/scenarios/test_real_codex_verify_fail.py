"""Tier-2 L3 acceptance — real Codex with a failing verify_command.

Per ADR-0002 § Acceptance L3 (Step 20) and § Negative-path appendix
(``verify_command fails``). Gated by ``--live-codex --live-llm``.
Increment 2 of the real-Codex burn (design doc
``docs/superpowers/specs/2026-05-28-real-codex-tier2-jkl-burn-design.md``).

Scenario
--------

The fixture repo carries a PERMANENTLY-RED test (``assert False``). The
D-1 ``task.created`` seeds ``repo_path`` + a ``verify_command`` whose
``pytest -x`` therefore exits non-zero no matter what Codex does. The
D-day goal is benign and additive — "create NOTES.md, do not touch
``tests/``" — so Codex produces a non-empty diff (B-0014 ensures the
untracked NOTES.md reaches the diff artifact) while the red test stays
red. This drives the L3 verify-fail row deterministically without
relying on Codex writing buggy code.

Expected L3 outcome (ADR L3 row + ``result_interpreter`` verify-fail
branch, lines 550-572):

- TWO ``action.result_observed`` for ``verify_diff`` — slot 1
  ``semantics="observation"`` (handler) + slot 2 ``semantics="error"``
  (L3); NO ``semantics="verification"`` slot.
- A ``Limitation`` ``claim.created`` whose ``evidence.attached`` row
  carries ``level=executed, relation=limits, source_id=verify_command``.
- NO ``Postcondition`` claim and NO ``verified``-level evidence.
- NO ``task.verified`` and NO ``task.no_op`` (the F2-ladder verdict is
  ``"neither"``).
- ``gate.evaluated(pre_emit).outcome == "force_limitation_language"``
  (no verified Postcondition for the subject) and the emitted surface
  text carries limitation framing — asserted against the canonical
  ``LIMITATION_REGEXES`` / ``COMPLETION_REGEXES`` constants imported
  from ``jarvis.decision.pre_emit_phrases`` (the canary forbids inline
  regex literals in tests).

Fixture pattern mirrors ``test_real_codex_flagship.py::live_real_codex_happy``:
module-scoped, run ONCE against the cloud, freeze the trace, each test
asserts one slice.

Cost estimate: ~$1 per invocation (one Codex turn + reviewer + Pre-emit
retry LLM calls).

Invocation::

    uv run pytest tests/scenarios/test_real_codex_verify_fail.py \\
        --live-codex --live-llm
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.pre_emit_phrases import COMPLETION_REGEXES, LIMITATION_REGEXES
from jarvis.runtime import bootstrap_runtime_app, run_turn
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    from collections.abc import Iterator


pytestmark = [pytest.mark.live_codex, pytest.mark.live_llm]


# The canonical D-day utterance (ADR § Tier 2 invocation), verbatim CJK.
_UTTERANCE: str = "昨天那个 task 给 codex 跑一下，做完审核了再告诉我。"  # noqa: RUF001

# Benign, additive goal: Codex creates a top-level NOTES.md (non-empty
# diff) and is explicitly forbidden from touching ``tests/`` so the
# permanently-red fixture test keeps ``verify_command`` exit code != 0.
_VERIFY_FAIL_GOAL: str = (
    "Create a top-level NOTES.md file at the repository root that briefly "
    "describes this demo project. Do NOT modify, fix, create, or delete any "
    "file under the tests/ directory — leave every existing test exactly as "
    "it is, including any test that currently fails. Your only change must "
    "be adding NOTES.md."
)

# Seeded test module: one passing test plus one intentional permanent
# failure. The red test guarantees ``pytest -x`` exits non-zero; the goal
# instructs Codex to leave it untouched.
_FIXTURE_TEST_MODULE: str = '''\
"""Demo tests for the L3 verify-fail acceptance fixture.

Contains one passing test and one PERMANENTLY-RED test. The red test is
an intentional fixture for the verify_command-fail path and must never
be repaired.
"""


def test_truthy() -> None:
    assert True


def test_known_failing_fixture_do_not_fix() -> None:
    """Intentional permanent failure — do not modify or repair."""
    assert False, "intentional failure: L3 verify_command-fail acceptance fixture"
'''


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
def live_real_codex_verify_fail(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Run the real-Codex verify-fail scenario ONCE; freeze the trace.

    Module-scoped so the cloud LLM + real Codex subprocess are exercised
    a single time across both L3 assertions. Seeds a repo whose
    ``verify_command`` (``pytest -x``) is permanently red, then gives
    Codex the benign ``NOTES.md`` goal so the diff is non-empty but the
    predicate still fails — driving the F2-ladder verify-fail row.
    """
    pytest.importorskip("openai")

    repo = tmp_path_factory.mktemp("verify_fail_repo")
    _git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.1'\nrequires-python = '>=3.12'\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_demo.py").write_text(_FIXTURE_TEST_MODULE, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    root = tmp_path_factory.mktemp("verify_fail_root")
    os.environ["JARVIS_RUNTIME_ROOT"] = str(root)
    runtime = bootstrap_runtime_app(runtime_root=root)

    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": _VERIFY_FAIL_GOAL,
            "source": "manual",
            "repo_path": str(repo),
            "verify_command": f"{sys.executable} -m pytest -x -q",
        },
        ts_epoch_ms=yesterday_ms,
    )

    result = run_turn(runtime, utterance=_UTTERANCE)
    time.sleep(0.3)  # let any straggling emit finalize before the snapshot

    db_path = runtime.runtime_paths.event_log
    trace = _load_trace(db_path)

    artifact_dir = Path(__file__).resolve().parent.parent / "_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    dump_path = artifact_dir / "real_codex_verify_fail_trace.json"
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


def test_l3_verify_fail_emits_limitation_at_executed(
    live_real_codex_verify_fail: dict[str, Any],
) -> None:
    """Verify the verify-fail path emits a Limitation at executed and no task.verified.

    Asserts the deterministic F2-ladder verify-fail row
    (``result_interpreter._emit_verification_rows``, lines 550-572):

    - the worker reported a clean diff (the run reached the verify slot);
    - ``verify_diff`` produced exactly an ``observation`` slot and an
      ``error`` slot (NO ``verification`` slot — the predicate failed);
    - a ``Limitation`` claim backed by an ``evidence.attached`` row at
      ``level=executed, relation=limits, source_id=verify_command``;
    - no ``Postcondition`` claim and no ``verified``-level evidence;
    - neither ``task.verified`` nor ``task.no_op`` (verdict ``"neither"``);
    - the emitted surface carries no completion-class language (the
      Pre-emit Gate's hard guarantee on the negative path).
    """
    cap = live_real_codex_verify_fail

    # The run reached the verify slot: Codex completed a turn with a diff.
    reported = _payloads(cap, "worker.reported")
    assert reported, "no worker.reported — Codex did not complete a turn"
    assert reported[0]["status"] == "ok"

    # (a) verify_diff produced observation + error slots, NOT verification.
    observed = _payloads(cap, "action.result_observed")
    verify_semantics = sorted(
        p["semantics"]
        for p in observed
        if p.get("semantics") in {"observation", "verification", "error"}
    )
    assert verify_semantics == ["error", "observation"], (
        f"expected exactly observation+error verify_diff slots, got {verify_semantics}"
    )
    error_slots = [p for p in observed if p.get("semantics") == "error"]
    assert error_slots[0].get("error"), "error slot missing verify_command exit-code marker"

    # (b) Limitation claim with executed/limits/verify_command evidence.
    claims = _payloads(cap, "claim.created")
    limitation_ids = {c["claim_id"] for c in claims if c["type"] == "Limitation"}
    assert limitation_ids, "no Limitation claim created on verify-fail"
    evidence = _payloads(cap, "evidence.attached")
    executed_limits = [
        e
        for e in evidence
        if e["claim_id"] in limitation_ids
        and e.get("level") == "executed"
        and e.get("relation") == "limits"
        and e.get("source_id") == "verify_command"
    ]
    assert executed_limits, (
        "no evidence.attached(level=executed, relation=limits, "
        "source_id=verify_command) on the Limitation claim"
    )

    # (c) No Postcondition claim and no verified-level evidence.
    assert not any(c["type"] == "Postcondition" for c in claims), (
        "verify-fail must not produce a Postcondition claim"
    )
    assert not any(e.get("level") == "verified" for e in evidence), (
        "verify-fail must not produce verified-level evidence"
    )

    # (d) No completion event of either kind (F2 verdict == "neither").
    assert not _payloads(cap, "task.verified"), "task.verified must not fire on verify-fail"
    assert not _payloads(cap, "task.no_op"), (
        "task.no_op must not fire on verify-fail (F2 verdict is 'neither', not 'no_op')"
    )

    # (e) Surface response carries no completion language.
    emitted = _payloads(cap, "surface.response_emitted")
    assert len(emitted) == 1
    text = emitted[0]["text"]
    assert not any(rx.search(text) for rx in COMPLETION_REGEXES), (
        f"verify-fail surface must not claim completion; got: {text!r}"
    )


def test_l3_pre_emit_gate_verdict_force_limitation_language(
    live_real_codex_verify_fail: dict[str, Any],
) -> None:
    """Verify the Pre-emit Gate forces limitation language on the verify-fail turn.

    Per ADR § Negative-path appendix + ``pre_emit_gate`` (gates.py
    415-418): with no ``verified``/``accepted`` evidence and no
    ``Postcondition`` claim for the active subject, the gate's
    ``permission`` is ``force_limitation_language`` — stamped onto the
    ``gate.evaluated(pre_emit)`` event's ``outcome`` field. Asserts the
    final pre_emit verdict and that the emitted surface text matches the
    canonical ``LIMITATION_REGEXES``.
    """
    cap = live_real_codex_verify_fail

    pre_emit_gates = [
        p for p in _payloads(cap, "gate.evaluated") if p.get("gate") == "pre_emit"
    ]
    assert pre_emit_gates, "no pre_emit gate.evaluated event"

    # The binding (final) pre_emit verdict forces limitation framing.
    assert pre_emit_gates[-1]["outcome"] == "force_limitation_language", (
        f"expected force_limitation_language, got {pre_emit_gates[-1]['outcome']!r}"
    )
    # The subject's active claim levels never reached 'verified'.
    assert "verified" not in pre_emit_gates[-1].get("claim_levels", []), (
        "verify-fail pre_emit gate saw verified evidence — unexpected"
    )

    # The emitted surface carries limitation framing (design L3 criterion).
    emitted = _payloads(cap, "surface.response_emitted")
    assert len(emitted) == 1
    text = emitted[0]["text"]
    assert any(rx.search(text) for rx in LIMITATION_REGEXES), (
        f"verify-fail surface must use limitation phrasing; got: {text!r}"
    )
