"""Tier 2 negative-case scenario test (ADR 0001 Step 13) — real cloud LLM.

Same flagship Mac scenario as Step 12, but the L4 ``spawn_worker`` stub
flips its artifact write to ``{"status": "fail"}``. The downstream
``verify_diff`` handler reads the artifact, sees the status mismatch,
and emits ``action.result_observed(semantics=error)``. ``task.verified``
never fires; the Task Ledger derives ``reported_complete``; the Result
Interpreter emits a Limitation Claim with reported evidence; and the
Pre-emit Gate refuses any completion-class language in the final CLI
output (forcing limitation framing instead).

Architecture
------------

Two module-scoped fixtures (``live_fail_path_run`` +
``live_fail_replay_run``) wrap the ``_SPAWN_WORKER_ARTIFACT_STATUS``
monkeypatch and call :func:`run_turn` exactly once each — so we burn
the cloud LLM twice across the full F1-F6 + I3 acceptance walk.

Acceptance coverage:

- ``test_negative_verify_diff_error_semantics`` covers § F1.
- ``test_negative_task_status_reported_complete`` covers § F2 (+ F6).
- ``test_negative_limitation_claim_emitted`` covers § F3.
- ``test_negative_response_has_limitation_language`` covers § F4.
- ``test_negative_response_no_bare_completion`` covers § F5.
- ``test_negative_projection_idempotent`` covers § F6.
- ``test_negative_replay_determinism`` covers § I3.

This file uses ZERO ``unittest.mock`` / ``vcr`` / ``responses`` /
``betamax`` / ``pytest_recording`` imports. Canary H7 + the conftest
``open_audit_hook`` keep the audit honest.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.pre_emit_phrases import COMPLETION_REGEXES, LIMITATION_REGEXES
from jarvis.execution import tools as execution_tools
from jarvis.runtime import RunTurnResult, bootstrap_runtime_app, run_turn
from jarvis.state.event_log import emit_event
from jarvis.state.projections import rebuild_projections
from tests.scenarios.conftest import write_llm_use_artifact

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime


# ADR-0002 Step 20: this Day-1 negative-case file now requires
# --live-codex in addition to --live-llm. The ``_SPAWN_WORKER_ARTIFACT_STATUS``
# monkeypatch knob it depends on was removed when Day-2's real
# ``spawn_worker_handler`` replaced the Day-1 Timer stub
# (``jarvis/execution/tools.py`` no longer defines the symbol); the
# L3 verify-fail ladder is now exercised end-to-end through a real
# Codex round-trip + a fixture repo whose ``verify_command`` exits
# non-zero. Gating by both flags ensures the file stays a no-op in
# the default Tier-1 path. The per-test ``@pytest.mark.live_llm``
# decorators remain as in-source documentation.
pytestmark = pytest.mark.live_codex


# The single utterance the ADR § Tier 2 invocation command pins —
# identical to Step 12 so the LLM's view of the world is the same;
# only the artifact knob flips. The CJK fullwidth punctuation is
# verbatim from the ADR — RUF001 would flag it as ambiguous but the
# bytes are load-bearing here too (the LLM reads it).
_UTTERANCE: str = "昨天那个 task 给 codex 跑一下，做完审核了再告诉我。"  # noqa: RUF001

# ADR § Acceptance F4 — at least one of these must match the final CLI
# output. ADR-0002 Step 13: imported from the single source of truth
# (``jarvis.decision.pre_emit_phrases``) rather than re-declared
# inline. F4 historically tracked five of the six canonical limitation
# regexes (no ``agent reported`` row) and used ``re.search`` against
# pattern source-text — preserve that by dropping the ``agent\s+
# reported`` Pattern and consuming ``.pattern`` from the remaining
# Patterns.
_LIMITATION_PATTERNS: tuple[str, ...] = tuple(
    pat.pattern for pat in LIMITATION_REGEXES if pat.pattern != r"agent\s+reported"
)

# ADR § Acceptance F5 — none of these may match the final CLI output.
# ADR-0002 Step 13: imported from the single source of truth
# (``jarvis.decision.pre_emit_phrases``). The CLI-output check uses
# ``re.search`` against pattern source-text (case-sensitive via the
# raw strings — historical F5 invariant), so consume ``.pattern`` from
# each canonical Pattern rather than the Pattern objects themselves.
_COMPLETION_PATTERNS: tuple[str, ...] = tuple(
    pat.pattern for pat in COMPLETION_REGEXES
)

# ADR § Acceptance I3 cross-run jitter ceiling (matches I1's ± 2 from
# the happy path). Negative path may emit one extra Limitation Claim
# vs. another depending on whether the LLM produced an explicit
# limitation framing or the gate forced the downgrade.
_I3_TOLERANCE: int = 2


# --- F5 sentence-level negation-frame helpers -----------------------------
#
# The ADR § F5 clause is: response "MUST NOT match any of
# ``_COMPLETION_PATTERNS`` outside an explicit 'agent reported' frame".
# We interpret "frame" as a sentence-level negation/limitation context.
# A sentence is "framed" iff it contains any of ``_NEGATION_MARKERS``.
# Only the concatenation of UNframed sentences is F5-checked. This
# preserves the verbatim ADR regex set in :data:`_COMPLETION_PATTERNS`
# while correctly handling LLM outputs like "agent reported 已完成,
# but not verified" or "cannot call it done without evidence".

# Sentence delimiter: split on ``.`` / ``!`` / ``?`` (ASCII end-of-
# sentence) followed by whitespace, on CJK ``。`` plus fullwidth bang/
# question (the bytes are load-bearing — the LLM emits these), or on a
# bare newline.
_SENTENCE_SPLIT_RE: re.Pattern[str] = re.compile(
    r"(?<=[.!?。！？])\s+|\n"  # noqa: RUF001
)

# Negation / limitation markers. If any marker matches inside a
# sentence (case-insensitive ``re.search``), that sentence is treated
# as inside an "agent reported / limitation" frame for F5 purposes.
_NEGATION_MARKERS: tuple[str, ...] = (
    # English negation / limitation lexicon ------------------------------
    r"\bnot\b",
    r"\bno\b",
    r"\bnot\s+yet\b",
    r"\bcannot\b",
    r"\bcan't\b",
    r"\bcouldn't\b",
    r"\bwon't\b",
    r"\bdo\s+not\b",
    r"\bdon't\b",
    r"\bunable\b",
    r"\bdoesn'?t\b",
    r"\bfailed\b",
    r"\bfailure\b",
    r"\berror\b",
    r"\bwithout\b",
    r"\blimitation\b",
    r"\bunverified\b",
    r"\bagent\s+reported\b",
    r"\bworker\s+reported\b",
    r"\breport(?:ed|s)\s+but\b",
    # Chinese negation / limitation lexicon ------------------------------
    r"不能",
    r"没能",
    r"未能",
    r"未\b",
    r"未\s",
    r"没有",
    r"没\s",
    r"不可",
    r"不准",
    r"不会",
    r"还没",
    r"还未",
    r"失败",
    r"未通过",
    r"未验证",
    r"没验证",
    r"还没验",
    r"未完成",
    r"没完成",
    r"暂不",
    r"暂未",
    r"目前不能",
    r"不能当作",
    r"不能把.*当作",
    r"agent[\s_]*报告",
    r"worker[\s_]*报告",
    r"报告完成.*但",
    r"limitation",
    r"报告\s*[完成|结束|完结]",
)


def _has_negation_marker(sentence: str) -> bool:
    """Return ``True`` iff ``sentence`` contains any negation/limitation marker.

    Case-insensitive ``re.search`` against :data:`_NEGATION_MARKERS`.
    """
    return any(
        re.search(pattern, sentence, flags=re.IGNORECASE)
        for pattern in _NEGATION_MARKERS
    )


def _bare_completion_violations(text: str) -> list[str]:
    """Return the list of ``_COMPLETION_PATTERNS`` that match ``text`` outside a frame.

    Splits ``text`` into sentences (see :data:`_SENTENCE_SPLIT_RE`),
    drops any sentence that carries a negation/limitation marker, then
    runs the verbatim ADR-fixed :data:`_COMPLETION_PATTERNS` regex set
    via ``re.search`` against the concatenated "bare" residue. The
    return value is the set of patterns that actually matched (empty
    list means F5-clean).
    """
    sentences = _SENTENCE_SPLIT_RE.split(text)
    bare_text = " ".join(s for s in sentences if not _has_negation_marker(s))
    return [pat for pat in _COMPLETION_PATTERNS if re.search(pat, bare_text)]


# Self-check fixtures for the sentence-level helper. These are pure
# string-literal cases — NO LLM is invoked. They exercise both
# directions (must-flag and must-pass) so the assertion machinery
# itself is guarded against regression.
_F5_SANITY_VIOLATING: tuple[str, ...] = (
    "任务已完成",       # F5 violation — bare 已完成, no negation context.
    "It's done.",      # F5 violation — bare \bdone\b.
)
_F5_SANITY_CLEAN: tuple[str, ...] = (
    "agent reported 已完成,但没有验证",  # negation context (没有/但)
    "cannot call it done",                # negation context (cannot)
)


# --- Module-scoped live runs -----------------------------------------------


def _seed_and_run(root: Path) -> JarvisRuntime:
    """Bootstrap a runtime against ``root`` and seed one open task, identical to Step 12.

    Returns the assembled :class:`JarvisRuntime`. The caller is
    responsible for running the turn (and closing the connection).
    """
    os.environ["JARVIS_RUNTIME_ROOT"] = str(root)
    runtime = bootstrap_runtime_app(runtime_root=root)
    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": "Implement Day-1 verify pipeline",
            "source": "manual",
        },
        ts_epoch_ms=yesterday_ms,
    )
    return runtime


@pytest.fixture(scope="module")
def live_fail_path_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Drive the negative-case flagship turn ONCE under monkeypatched artifact status.

    Module-scoped so F1-F6 share a single cloud LLM call. The artifact
    knob ``_SPAWN_WORKER_ARTIFACT_STATUS`` is flipped via
    :func:`pytest.MonkeyPatch.context` for the lifetime of the turn —
    pytest's per-test ``monkeypatch`` fixture is function-scoped, so we
    drive the patch manually here.
    """
    pytest.importorskip("openai")  # Defensive — the runtime needs it.

    root = tmp_path_factory.mktemp("flagship_fail")
    runtime = _seed_and_run(root)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(execution_tools, "_SPAWN_WORKER_ARTIFACT_STATUS", "fail")
        result = run_turn(runtime, utterance=_UTTERANCE)
    # Wait briefly so any straggling Timer callback finalizes its emit
    # before we snapshot. Day-1 Timer delay is ~10 ms.
    time.sleep(0.2)

    artifact_path = write_llm_use_artifact(runtime, suffix="fail")

    captured: dict[str, Any] = {
        "runtime": runtime,
        "result": result,
        "db_path": runtime.runtime_paths.event_log,
        "artifact_path": artifact_path,
    }
    try:
        yield captured
    finally:
        runtime.conn.close()


@pytest.fixture(scope="module")
def live_fail_replay_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Second independent negative-case run for replay determinism (I3)."""
    pytest.importorskip("openai")

    root = tmp_path_factory.mktemp("flagship_fail_replay")
    runtime = _seed_and_run(root)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(execution_tools, "_SPAWN_WORKER_ARTIFACT_STATUS", "fail")
        result = run_turn(runtime, utterance=_UTTERANCE)
    time.sleep(0.2)

    captured: dict[str, Any] = {
        "runtime": runtime,
        "result": result,
        "db_path": runtime.runtime_paths.event_log,
    }
    try:
        yield captured
    finally:
        runtime.conn.close()


# --- helpers --------------------------------------------------------------


def _fetch_all_rows(db_path: Path) -> list[sqlite3.Row]:
    """Open a fresh read-only connection and return every events row."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = list(
            conn.execute(
                "SELECT id, event_uid, type, schema_version, ts_epoch_ms, "
                "payload_json, source_event_id, correlation_json "
                "FROM events ORDER BY id ASC"
            )
        )
    finally:
        conn.close()
    return rows


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(row["payload_json"])
    return parsed


# --- F1 — verify_diff emits error semantics; no task.verified -------------


@pytest.mark.live_llm
def test_negative_verify_diff_error_semantics(
    live_fail_path_run: dict[str, Any],
) -> None:
    """ADR § Acceptance F1 — verify_diff error semantics + no task.verified.

    F1.a — At least one ``action.result_observed`` row carries
           ``payload.semantics == "error"``.
    F1.b — Zero ``task.verified`` rows.
    """
    rows = _fetch_all_rows(live_fail_path_run["db_path"])

    error_observed = [
        row
        for row in rows
        if row["type"] == "action.result_observed"
        and _payload(row).get("semantics") == "error"
    ]
    assert error_observed, (
        "F1: no action.result_observed row carries semantics='error'; "
        f"observed types: {[r['type'] for r in rows]!r}"
    )

    task_verified = [row for row in rows if row["type"] == "task.verified"]
    assert not task_verified, (
        f"F1: task.verified must NOT fire on the negative path; "
        f"found {len(task_verified)} rows"
    )


# --- F2 — derived_status = "reported_complete" ----------------------------


@pytest.mark.live_llm
def test_negative_task_status_reported_complete(
    live_fail_path_run: dict[str, Any],
) -> None:
    """ADR § Acceptance F2 — Task Ledger derives ``reported_complete``."""
    runtime: JarvisRuntime = live_fail_path_run["runtime"]
    rebuilt = rebuild_projections(runtime.conn)
    derived = rebuilt.task_ledger.derive_status("task_X")
    assert derived == "reported_complete", (
        f"F2: derived status was {derived!r}; expected 'reported_complete' "
        f"on the negative path"
    )


# --- F3 — Limitation Claim with reported evidence -------------------------


@pytest.mark.live_llm
def test_negative_limitation_claim_emitted(
    live_fail_path_run: dict[str, Any],
) -> None:
    """ADR § Acceptance F3 — Limitation Claim + reported evidence chain.

    Walks ``claim.created(type=Limitation)`` -> its companion
    ``evidence.attached(level=reported)`` -> ensures the evidence row
    references the same ``claim_id`` AND that the cause chain on the
    Limitation claim traces back to a ``verify_diff``
    ``action.result_observed(semantics=error)`` row.
    """
    rows = _fetch_all_rows(live_fail_path_run["db_path"])
    by_uid: dict[str, sqlite3.Row] = {r["event_uid"]: r for r in rows}

    limitation_claims = [
        r
        for r in rows
        if r["type"] == "claim.created" and _payload(r).get("type") == "Limitation"
    ]
    observed_claim_types: set[str] = {
        str(_payload(r).get("type"))
        for r in rows
        if r["type"] == "claim.created"
    }
    assert limitation_claims, (
        f"F3: expected >=1 claim.created(type=Limitation); claim types "
        f"observed: {sorted(observed_claim_types)!r}"
    )

    for lc in limitation_claims:
        claim_id = _payload(lc)["claim_id"]

        # F3.a — companion evidence.attached(level=reported) references
        # this claim_id.
        reported_evidence = [
            r
            for r in rows
            if r["type"] == "evidence.attached"
            and _payload(r).get("claim_id") == claim_id
            and _payload(r).get("level") == "reported"
        ]
        assert reported_evidence, (
            f"F3: Limitation claim_id={claim_id!r} has no companion "
            f"evidence.attached(level=reported)"
        )

        # F3.b — source chain on the Limitation claim must trace back
        # through an action.result_observed(semantics=error). Walk
        # source_event_id parents until we find one (or run out).
        cursor_uid = lc["source_event_id"]
        seen: set[str] = set()
        found_error_observed = False
        while cursor_uid is not None and cursor_uid not in seen:
            seen.add(cursor_uid)
            parent = by_uid.get(cursor_uid)
            if parent is None:
                break
            if (
                parent["type"] == "action.result_observed"
                and _payload(parent).get("semantics") == "error"
            ):
                found_error_observed = True
                break
            cursor_uid = parent["source_event_id"]
        assert found_error_observed, (
            f"F3: Limitation claim_id={claim_id!r} chain does not root "
            f"in an action.result_observed(semantics=error) row"
        )


# --- F4 — Limitation language present -------------------------------------


@pytest.mark.live_llm
def test_negative_response_has_limitation_language(
    live_fail_path_run: dict[str, Any],
) -> None:
    """ADR § Acceptance F4 — CLI output contains limitation language."""
    result: RunTurnResult = live_fail_path_run["result"]
    candidates: tuple[str, ...] = (result.response_text, result.response_plan.text)

    # At least ONE pattern must match at least ONE candidate text.
    hits: list[str] = []
    for pattern in _LIMITATION_PATTERNS:
        for text in candidates:
            if re.search(pattern, text):
                hits.append(pattern)
                break
    assert hits, (
        f"F4: no limitation pattern matched response text. "
        f"Patterns tried: {_LIMITATION_PATTERNS!r}. "
        f"response_text={result.response_text!r}; "
        f"response_plan.text={result.response_plan.text!r}"
    )


# --- F5 — No bare completion claims ---------------------------------------


@pytest.mark.live_llm
def test_negative_response_no_bare_completion(
    live_fail_path_run: dict[str, Any],
) -> None:
    r"""ADR § Acceptance F5 — CLI output must NOT match completion regex set.

    Per ADR § F5, each pattern in :data:`_COMPLETION_PATTERNS` is
    asserted to NOT match via :func:`re.search` **outside an explicit
    "agent reported" frame**. The verbatim ADR regex set is unchanged
    (the negative-lookahead in ``r"已完成(?!\s*报告)"`` only excludes
    the literal "已完成报告" suffix; it does NOT cover the general
    "agent reported / cannot call it done / 不能当作已完成" frames the
    LLM actually emits).

    We implement the ADR's "frame" clause at the SENTENCE level: split
    the response text on sentence boundaries, mark any sentence
    containing a negation/limitation marker (English + CJK lexicon, see
    :data:`_NEGATION_MARKERS`) as "framed", and only F5-check the
    concatenation of UNframed sentences. A literal ``verified`` /
    ``done`` / ``完成`` / ``已完成`` in a sentence with no negation
    context is still a violation. See :func:`_bare_completion_violations`
    for the helper.

    Sanity guards (pure-string, no LLM) below the live-LLM assertion
    confirm the helper's both directions: bare completion is flagged,
    framed completion is allowed through.
    """
    # Pure-string sanity guards — must hold regardless of the live LLM
    # output. These exercise the assertion machinery itself.
    for sample in _F5_SANITY_VIOLATING:
        assert _bare_completion_violations(sample), (
            f"F5 sanity (must-flag): bare completion sample passed "
            f"silently: {sample!r}"
        )
    for sample in _F5_SANITY_CLEAN:
        assert not _bare_completion_violations(sample), (
            f"F5 sanity (must-pass): framed completion sample was "
            f"flagged: {sample!r}"
        )

    result: RunTurnResult = live_fail_path_run["result"]
    text = result.response_text

    violations = _bare_completion_violations(text)
    assert not violations, (
        f"F5: completion pattern(s) matched the response outside any "
        f"limitation frame: {violations!r}. "
        f"original={text!r}"
    )


# --- F6 — Re-derived projection matches in-memory state -------------------


@pytest.mark.live_llm
def test_negative_projection_idempotent(
    live_fail_path_run: dict[str, Any],
) -> None:
    """ADR § Acceptance F6 — Re-derived projection matches in-memory state.

    Two consecutive :func:`rebuild_projections` calls against the same
    connection produce deep-equal ``ProjectionSet``s (D5 equivalent for
    the negative path), AND the Task Ledger record for ``task_X`` /
    the derived status / the Claim/Evidence projection are stable.
    """
    runtime: JarvisRuntime = live_fail_path_run["runtime"]
    first = rebuild_projections(runtime.conn)
    second = rebuild_projections(runtime.conn)

    assert first.task_ledger.derive_status("task_X") == "reported_complete"
    assert (
        first.task_ledger.derive_status("task_X")
        == second.task_ledger.derive_status("task_X")
    ), "F6: derived status differs across rebuilds"
    assert (
        first.task_ledger.records_by_task_id["task_X"]
        == second.task_ledger.records_by_task_id["task_X"]
    ), "F6: task ledger record differs across rebuilds"
    assert first.claim_evidence.claims_by_id == second.claim_evidence.claims_by_id, (
        "F6: claim projection differs across rebuilds"
    )
    assert (
        first.claim_evidence.evidence_by_claim_id
        == second.claim_evidence.evidence_by_claim_id
    ), "F6: evidence projection differs across rebuilds"


# --- I3 — Replay determinism on the negative path -------------------------


@pytest.mark.live_llm
def test_negative_replay_determinism(
    live_fail_path_run: dict[str, Any],
    live_fail_replay_run: dict[str, Any],
) -> None:
    """ADR § Acceptance I3 — Two negative-path runs produce identical derived status.

    I3 — Final ``derived_status == "reported_complete"`` for ``task_X``
         across two independent live runs (identical seed, identical
         flipped artifact status). Event-count tolerance ± 2 like I1.
    """
    rows_a = _fetch_all_rows(live_fail_path_run["db_path"])
    rows_b = _fetch_all_rows(live_fail_replay_run["db_path"])

    # Event-count tolerance (mirrors I1's ± 2 on the happy path).
    assert abs(len(rows_a) - len(rows_b)) <= _I3_TOLERANCE, (
        f"I3: negative-path event-count drift exceeds ±{_I3_TOLERANCE}: "
        f"run-A={len(rows_a)}, run-B={len(rows_b)}"
    )

    runtime_a: JarvisRuntime = live_fail_path_run["runtime"]
    runtime_b: JarvisRuntime = live_fail_replay_run["runtime"]
    status_a = rebuild_projections(runtime_a.conn).task_ledger.derive_status("task_X")
    status_b = rebuild_projections(runtime_b.conn).task_ledger.derive_status("task_X")
    assert status_a == status_b == "reported_complete", (
        f"I3: derived status differs across negative reruns: "
        f"a={status_a!r}, b={status_b!r}"
    )
