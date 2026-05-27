# Pre-emit Gate Optimal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore Pre-emit Gate scope to spec §3.4.12 v0 — gate only consequential claims — so conversational turns with no tracked task subject (`"介绍一下你自己"`, `"现在几点"`) are not falsely downgraded to the canonical `找不到对应的 task (未验证 / unverified)` hard refusal.

**Architecture:** Single-cut fix at the L3 decision layer. Widen `pre_emit_gate`'s signature to accept `str | None`, add an explicit `None` pass-through branch that returns a routine/sentence `ResponsePlan` unchanged, and drop the `"unknown_subject"` magic-string fallback in `_finalize_response`. The fix is type-driven: the signature change forces every call site to acknowledge the no-subject case, which is the latent root cause. No new modules, no new dependencies, no spec changes, no prompt changes.

**Tech Stack:** Python 3.12, pytest, mypy strict, ruff, lint-imports. Same toolchain as the rest of jarvis L3.

**Reference docs:** Design — `docs/superpowers/specs/2026-05-27-pre-emit-gate-optimal-design.md`. Spec — `docs/spec.html` §3.4.12 (primary, v0 scope), §3.4.4 (LLMSituationPacket `active_task?` optional), §3.4.13 + §3.6.6 (ResponsePlan / routine streaming), §13.1 (Pre-emit Gate three checks), §13 I10 (Claim ≤ Evidence).

---

## Conventions used in this plan

- **Worktree path:** all paths below are relative to `/Users/alllllenshi/Projects/jarvis/.claude/worktrees/claude-adr0001/` unless stated. Run all commands from there.
- **Four gates (run after every task that modifies code):**
  ```
  .venv/bin/ruff check jarvis tests
  .venv/bin/mypy jarvis
  .venv/bin/lint-imports
  .venv/bin/pytest -x
  ```
  The plan calls these out at the end of each task; if a task adds a new test that should temporarily fail, run that single test in isolation (`.venv/bin/pytest <path> -v`) instead of `pytest -x`.
- **TDD discipline:** each task writes a failing test FIRST, runs it to see it fail, writes minimal impl, runs it to see it pass, then commits. Never commit red.
- **Commit messages:** follow `type(scope): subject` per recent history. No `Co-Authored-By` per project CLAUDE.md. Body uses the 5-part template from `docs/git-guide.md` § 2 (one-line opener · file bullets · `Tier 1:` line · `Legacy-bypass:` / `Legacy consulted:` only when applicable).
- **Layer rules:** all edits stay in L3 (`jarvis/decision/*`). `gates.py` keeps stdlib + `jarvis.shared` + `jarvis.state` imports only; `__init__.py` is the existing cross-cutting decision module. No new layer crossings — `lint-imports` continues to enforce.

---

## File Map

| File | Role | Change kind |
|---|---|---|
| `jarvis/decision/gates.py` (lines 346-424) | Pre-emit Gate impl | Modify — widen `active_subject_ref: str → str \| None`, add `None` pass-through branch at function head, update docstring |
| `jarvis/decision/__init__.py` (lines 1843-1860) | `_finalize_response` retry chain | Modify — drop the `"unknown_subject"` magic-string fallback; let `active_subject: str \| None` flow through to the gate |
| `tests/unit/test_pre_emit_gate.py` (append at file end) | Pre-emit Gate unit tests | Add 1 new test case for `None` pass-through |
| `tests/integration/test_conversational_turn_no_gate_downgrade.py` | E2E regression | New file (~140 lines incl. stubs) — drives `decide()` end-to-end against an empty ledger with a draft containing "完成"; asserts pass-through + audit-trail invariants |

**Files inspected but not modified:**

- `jarvis/decision/__init__.py:714-716` — F1 short-circuit's `_hard_refusal_plan(active_subject="unknown_subject", ...)`. This is a DIFFERENT use of the string (a label on the F1 hard-refusal path, not the gate fallback). Stays untouched.
- `jarvis/decision/resolver.py:241` — resolver's `match_basis="unknown_subject"` (multi-match fail-fast tag). Orthogonal.
- `tests/canary/test_gate_contracts.py:127-136` — AST inspector for `pre_emit_gate`'s MUST-check primitives. Continues to pass because the str-branch body still contains `"allow_completion_language"`, `"force_limitation_language"`, `claim`, `evidence`.
- `tests/canary/test_finalize_emits_pre_emit_gate.py` — AST inspector for `gate.evaluated(pre_emit)` emission. Continues to pass: `_emit_pre_emit_gate_event` is still called.
- `tests/unit/test_pre_emit_forced_template.py::test_finalize_response_wires_scrub_and_hard_refusal` — AST check that `_finalize_response` still calls `_scrub_completion_keywords` and `_hard_refusal_plan`. Both calls remain in the retry chain (only entered when a real subject is in scope) — guard continues to pass.
- `tests/unit/test_pre_emit_forced_template.py::test_finalize_response_promotes_silent_log_on_hard_refusal` — AST check for `hard_refusal_used` / `silent_log` / `queue_review` tokens. All remain.
- `tests/unit/test_pre_emit_forced_template.py::_GATE_TO_SCRUB_COVERAGE` — drift guard for keyword/scrub parity. Untouched (we add no keywords).

---

# Phase 1 — Gate signature widening

One task. Pure-function change; mypy will be the primary check.

## Task 1: Widen `pre_emit_gate` signature and add `None` pass-through branch

**Files:**
- Modify: `jarvis/decision/gates.py:346-424` (`pre_emit_gate` signature + docstring + early return)
- Test:   `tests/unit/test_pre_emit_gate.py` (append one new test after `test_pre_emit_detects_done_keyword_case_insensitive`)

**Spec basis:** §3.4.12 v0 ("只 gate consequential claims"); §3.4.4 (LLMSituationPacket `active_task?` is optional, so `None` is a legitimate first-class input, not an error case); §13.1 (three Pre-emit Gate checks all vacuously satisfied when there is no subject to claim against — see design doc §3.5 for the per-check mapping).

- [ ] **Step 1: Write the failing test.**

Append at the end of `tests/unit/test_pre_emit_gate.py` (after the existing `test_pre_emit_detects_done_keyword_case_insensitive` function):

```python


# --- None subject pass-through (§3.4.12 v0: only gate consequential claims) ---


def test_pre_emit_passes_through_when_no_active_subject():
    """``active_subject_ref=None`` → pass through unchanged.

    Per spec §3.4.12 v0 the Pre-emit Gate only gates consequential
    claims (task status, agent completion, test result, device result,
    memory write, current mutable state). When the caller signals no
    subject (None), there is no consequential claim being made about
    anyone — the gate has nothing to enforce. The draft is shipped
    verbatim with ``permission=allow_completion_language`` and
    ``downgrade_required=False``. §13.1 three checks are vacuously
    satisfied (no claim, routine output_form, no agent self-report).

    Mirrors the §3.4.4 LLMSituationPacket schema where ``active_task?``
    is optional — None at the gate boundary is the structural reflection
    of an empty packet, not a failure mode.
    """
    projection = _make_projection()  # empty
    draft = "我可以帮你完成各种任务。"  # contains "完成" — would trip the str branch
    plan = pre_emit_gate(draft, projection, active_subject_ref=None)

    assert plan.text == draft, "draft must be shipped verbatim"
    assert plan.permission == "allow_completion_language"
    assert plan.downgrade_required is False
    assert plan.active_claim_levels == ()
    assert plan.output_risk_class == "routine"
    assert plan.required_gate_mode == "sentence"
    assert plan.response_hash == hashlib.sha256(draft.encode("utf-8")).hexdigest()
```

- [ ] **Step 2: Run the test to confirm RED.**

```
.venv/bin/pytest tests/unit/test_pre_emit_gate.py::test_pre_emit_passes_through_when_no_active_subject -v
```

Expected: FAIL. Mypy strict would also reject `active_subject_ref=None` because the current signature is `str` — but pytest itself catches the runtime mismatch first (the `claim_evidence.strongest_level_for(None)` call would raise or `claim_evidence.claims_for(None)` would return `()`, falling through to `force_limitation_language` with a wrong permission verdict).

If pytest passes (i.e. the impl already accepts None), STOP and inspect — the gate's current implementation may have shifted under your feet; re-read `jarvis/decision/gates.py:346-424` before continuing.

- [ ] **Step 3: Widen the signature and add the `None` branch.**

In `jarvis/decision/gates.py`, change the signature at line 349 from:

```python
def pre_emit_gate(
    draft_text: str,
    claim_evidence: ClaimEvidenceProjection,
    active_subject_ref: str,
) -> ResponsePlan:
```

to:

```python
def pre_emit_gate(
    draft_text: str,
    claim_evidence: ClaimEvidenceProjection,
    active_subject_ref: str | None,
) -> ResponsePlan:
```

Then, at the top of the function body (immediately after the docstring closes, before `strongest = claim_evidence.strongest_level_for(active_subject_ref)`), insert:

```python
    # Spec §3.4.12 v0: only gate consequential claims. When the caller
    # has no subject in scope, no claim is being made about any tracked
    # entity — the gate has nothing to enforce. Pass the draft through
    # unchanged with a routine ResponsePlan (matches §3.4.13 / §3.6.6
    # routine = sentence-boundary streaming). §13.1 three checks are
    # vacuously satisfied: (a) claim ≤ evidence trivially holds (no
    # claim), (b) output_form is routine, (c) no agent report is being
    # interpreted (Result Interpreter §3.4.11 is upstream).
    if active_subject_ref is None:
        return ResponsePlan(
            text=draft_text,
            permission="allow_completion_language",
            downgrade_required=False,
            active_claim_levels=(),
            response_hash=_response_hash(draft_text),
            output_risk_class="routine",
            required_gate_mode="sentence",
        )
```

Also extend the docstring's `Args:` line for `active_subject_ref` (current text reads "Subject the response is 'about' — Day-1 this is the active ``task_id``."). Replace that line with:

```
        active_subject_ref: Subject the response is "about" — Day-1
            this is the active ``task_id``. ``None`` signals no subject
            is in scope (spec §3.4.4 LLMSituationPacket admits this as
            ``active_task?`` optional); the gate then short-circuits to
            a routine pass-through per §3.4.12 v0.
```

- [ ] **Step 4: Run the new test to confirm GREEN, then run all four gates.**

```
.venv/bin/pytest tests/unit/test_pre_emit_gate.py::test_pre_emit_passes_through_when_no_active_subject -v
```

Expected: PASS.

Then run the full gate suite:

```
.venv/bin/ruff check jarvis tests
.venv/bin/mypy jarvis
.venv/bin/lint-imports
.venv/bin/pytest -x
```

Expected: all green. The existing 8 unit tests in `test_pre_emit_gate.py` continue to pass because they all call `pre_emit_gate(..., active_subject_ref="task_X")` — the `str` branch is byte-identical to the pre-change body.

If mypy complains about any call site, that call site was relying on `active_subject_ref` being `str` — at this phase only the 3 sites inside `_finalize_response` (lines 1867 / 1900 / 1919) pass the variable directly, and the variable is still typed `str` there. No mypy break expected. If mypy DOES break, fix it; do not commit until green.

- [ ] **Step 5: Commit.**

```
git add jarvis/decision/gates.py tests/unit/test_pre_emit_gate.py
git commit -m "$(cat <<'EOF'
feat(decision): pre_emit_gate accepts active_subject_ref=None pass-through

Spec §3.4.12 v0 step 1 of 3 toward restoring "only gate consequential claims" scope. Widens the gate's signature so callers can express "no subject in scope" at the type level (mirrors §3.4.4 LLMSituationPacket active_task? optionality) instead of coercing into the gate's failure path with a "unknown_subject" magic string.

- jarvis/decision/gates.py — widen active_subject_ref to str | None; add early-return None branch returning routine/sentence pass-through ResponsePlan; docstring extended with §3.4.4 / §3.4.12 v0 references.
- tests/unit/test_pre_emit_gate.py — new case test_pre_emit_passes_through_when_no_active_subject covers None + draft containing "完成" keyword + empty projection, asserting allow + downgrade_required=False + active_claim_levels=() + routine/sentence shape.

Tier 1: lint-imports KEPT · ruff clean · mypy strict clean · 9/9 pre_emit_gate tests pass · wall <fill in actual>s (< 30s budget).
EOF
)"
```

(Fill in the actual wall-clock time from the last `pytest -x` run before committing.)

---

# Phase 2 — Caller fix with end-to-end regression test

One task. Combines the integration regression test (written first, fails immediately) and the `_finalize_response` fallback drop (the minimal impl that flips it green) into one TDD cycle. This commits green, honouring "never commit red".

## Task 2: Drop `"unknown_subject"` fallback and add integration regression test

**Files:**
- Create: `tests/integration/test_conversational_turn_no_gate_downgrade.py` (new file ~150 lines incl. stubs)
- Modify: `jarvis/decision/__init__.py:1843-1860` (drop the `"unknown_subject"` fallback; let `active_subject: str | None` flow through to the gate)

**Spec basis:** §3.4.12 v0 (caller stops coercing the empty case into the gate's failure path); §3.4.4 (`active_task?` optionality flows honestly through L3). Design doc §7 acceptance criteria 1, 2, 6 verified by the integration test.

- [ ] **Step 1: Write the failing integration test.**

Create `tests/integration/test_conversational_turn_no_gate_downgrade.py` with the following content. The stubs mirror `tests/unit/test_decision_utterance_received.py` patterns — same `_StubRuntimePaths` / `_StubLLMClient` shape — but parametrized so the LLM draft can include the "完成" keyword.

```python
"""E2E regression: conversational turn must not be force-downgraded.

Per design doc 2026-05-27-pre-emit-gate-optimal-design.md §7 acceptance
criteria:

  1. Conversational pass-through — a turn with no active task subject
     surfaces the LLM's own draft text (not the canonical "找不到对应的
     task" hard-refusal template).
  2. Gate event audit — exactly one ``gate.evaluated`` event with
     ``attempt=0``, ``permission=allow_completion_language``,
     ``downgrade_required=False``, ``active_claim_levels=[]``.
  6. No new LLM calls — ``cost.recorded`` count for the turn drops to
     one (the original draft generation); the attempt-1 retry and
     attempt-2 template branch must not fire.

Spec basis: §3.4.12 v0 (only gate consequential claims), §3.4.4
(LLMSituationPacket ``active_task?`` is optional, so empty-task turns
are first-class).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import (
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
    decide,
)
from jarvis.decision.llm import ChatResult, LLMClient
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@dataclass(frozen=True)
class _StubRuntimePaths:
    event_log: Path
    artifacts_root: Path

    def artifact_dir_for_run(self, run_id: str) -> Path:
        out = self.artifacts_root / run_id
        out.mkdir(parents=True, exist_ok=True)
        return out


class _StubLLMClient:
    """Returns a parametrized draft. One chat per turn; never raises.

    The draft intentionally carries the ``完成`` completion keyword to
    prove that the gate is no longer downgrading on keyword presence
    alone when no subject is in scope.
    """

    def __init__(self, draft_text: str) -> None:
        self._draft_text = draft_text
        self.chat_calls = 0
        self.model = "stub-model"

    @property
    def last_input_tokens(self) -> int | None:
        return 0

    @property
    def last_output_tokens(self) -> int | None:
        return 0

    @property
    def last_finish_reason(self) -> str | None:
        return "stop"

    @contextmanager
    def fresh_context(self) -> Iterator[_StubLLMClient]:
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],  # noqa: ARG002
        system: str,  # noqa: ARG002
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002
        tool_choice: str | None = "auto",  # noqa: ARG002
    ) -> ChatResult:
        self.chat_calls += 1
        return ChatResult(
            text=self._draft_text,
            tool_calls=(),
            finish_reason="stop",
            input_tokens=0,
            output_tokens=0,
            raw={},
            model_used="stub-model",
            tokens_in=0,
            tokens_out=0,
        )


def _build_ctx(
    tmp_path: Path,
    *,
    draft_text: str,
) -> tuple[DecideContext, sqlite3.Connection, _StubLLMClient]:
    conn = open_event_log(tmp_path / "events.db")
    paths = _StubRuntimePaths(
        event_log=tmp_path / "events.db",
        artifacts_root=tmp_path / "artifacts",
    )
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    registry = build_default_registry()
    lifecycle = ActionLifecycle()
    llm = _StubLLMClient(draft_text=draft_text)
    ctx = DecideContext(
        conn=conn,
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", lifecycle),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt",
    )
    return ctx, conn, llm


def _gate_evaluated_payloads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM events WHERE type = 'gate.evaluated' ORDER BY ts_epoch_ms"
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _count_rows(conn: sqlite3.Connection, event_type: str) -> int:
    cursor = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = ?", (event_type,),
    )
    return int(cursor.fetchone()[0])


def test_conversational_turn_passes_through_without_downgrade(
    tmp_path: Path,
) -> None:
    """`介绍一下你自己` against empty ledger → LLM draft surfaces unmodified."""
    # The draft contains the "完成" keyword. Pre-change this triggers
    # force_limitation → retry → template → hard_refusal. Post-change
    # the None-subject pass-through ships it verbatim.
    draft = "我是 Jarvis，我可以帮你完成各种任务。"
    ctx, conn, llm = _build_ctx(tmp_path, draft_text=draft)
    try:
        trigger = emit_event(
            conn,
            type="surface.user_intent",
            payload={
                "transcript": "介绍一下你自己",
                "turn_id": "T_conv_001",
            },
            correlation={"turn_id": "T_conv_001"},
        )

        result = decide(trigger, ctx)

        # Acceptance criterion 1: the LLM draft surfaces verbatim.
        assert result.response_plan is not None
        assert result.response_plan.text == draft, (
            "Conversational turn was downgraded — pre-emit gate fired despite no "
            f"subject. Got: {result.response_plan.text!r}"
        )
        assert "找不到对应的 task" not in result.response_plan.text, (
            "Conversational turn surfaced the F1 / hard-refusal canonical text — "
            "indicates the gate retry chain ran to exhaustion."
        )
        assert result.response_plan.permission == "allow_completion_language"
        assert result.response_plan.downgrade_required is False

        # Acceptance criterion 2: exactly one gate.evaluated, attempt 0,
        # allow_completion_language, downgrade_required=False.
        gates = _gate_evaluated_payloads(conn)
        assert len(gates) == 1, (
            f"Expected exactly 1 gate.evaluated event, got {len(gates)}: "
            f"the retry chain (attempts 1 and 2) must not fire when no "
            f"subject is in scope. Payloads: {gates!r}"
        )
        verdict = gates[0]
        assert verdict["gate"] == "pre_emit"
        assert verdict["attempt"] == 0
        assert verdict["permission"] == "allow_completion_language"
        assert verdict["downgrade_required"] is False

        # Acceptance criterion 6: exactly one cost.recorded — the original
        # draft generation. No retry round-trip.
        cost_count = _count_rows(conn, "cost.recorded")
        assert cost_count == 1, (
            f"Expected exactly 1 cost.recorded event, got {cost_count}: "
            f"the attempt-1 LLM retry must not fire on the no-subject path."
        )
        assert llm.chat_calls == 1, (
            f"LLM stub recorded {llm.chat_calls} chat calls; expected 1."
        )
    finally:
        conn.close()
```

- [ ] **Step 2: Run the test to confirm RED.**

```
.venv/bin/pytest tests/integration/test_conversational_turn_no_gate_downgrade.py -v
```

Expected: FAIL. Specific failure mode: `result.response_plan.text` will equal the `_FORCED_LIMITATION_TEMPLATE`-wrapped scrubbed draft or the `_hard_refusal_plan` text (`找不到对应的 task ...`), not the raw `draft`. The first `assert result.response_plan.text == draft` line will trip. The gate event count will be 3 (attempts 0, 1, 2) not 1; the cost.recorded count will be 2 (initial + retry) not 1.

If the test passes at this step, STOP and inspect — Task 1 may have inadvertently changed `_finalize_response` behavior, or the worktree has drift. The whole point of this step is to confirm the bug is reproduced before fixing it.

- [ ] **Step 3: Apply the caller fix.**

In `jarvis/decision/__init__.py`, locate the block at lines 1843-1860 inside `_finalize_response`. Current text:

```python
    hard_refusal_used = False
    active_subject = scratch.active_subject_ref
    if active_subject is None and packet.open_tasks:
        active_subject = packet.open_tasks[0].task_id
    if active_subject is None:
        # No scratch.active_subject_ref AND no open tasks — the gate
        # will see an empty claim set and force_limitation_language
        # by construction. Demoted to debug: the hard-refusal text now
        # carries a user-facing "找不到对应的 task" branch (F1), so the
        # failure mode reaches the operator via the surface rather than
        # via stderr noise.
        LOGGER.debug(
            "_finalize_response: no active_subject_ref and no open tasks; "
            "falling back to 'unknown_subject' (turn_id=%r). The Pre-emit "
            "Gate will force limitation framing.",
            scratch.turn_id,
        )
        active_subject = "unknown_subject"
```

Replace with:

```python
    hard_refusal_used = False
    active_subject: str | None = scratch.active_subject_ref
    if active_subject is None and packet.open_tasks:
        active_subject = packet.open_tasks[0].task_id
    if active_subject is None:
        # Spec §3.4.12 v0 + §3.4.4 LLMSituationPacket: no subject in
        # scope is a first-class case, not a failure mode. Pass None to
        # the gate; it short-circuits to a routine pass-through (see
        # pre_emit_gate's None branch). The retry chain (attempts 1
        # and 2) and _hard_refusal_plan remain reachable only when a
        # real subject is in scope and downgrade_required fires.
        LOGGER.debug(
            "_finalize_response: no active_subject_ref and no open tasks "
            "(turn_id=%r); passing None to pre_emit_gate for §3.4.12 v0 "
            "pass-through (no consequential claim to gate).",
            scratch.turn_id,
        )
```

Note: the `active_subject = "unknown_subject"` assignment line is REMOVED. The variable now stays `None` and is passed to the three `pre_emit_gate(...)` call sites at lines 1867 / 1900 / 1919 (formerly accepted `str`, now `str | None` after Task 1).

- [ ] **Step 4: Run the integration test to confirm GREEN, then the full four-gate suite.**

```
.venv/bin/pytest tests/integration/test_conversational_turn_no_gate_downgrade.py -v
```

Expected: PASS. All three assertion groups (draft surfaces verbatim · single `gate.evaluated` with `attempt=0` + `allow_completion_language` + `downgrade_required=False` · single `cost.recorded`) hold. These correspond directly to design doc §7 acceptance criteria 1, 2, and 6.

Then run the supporting gates plus the full suite to catch any regression:

```
.venv/bin/ruff check jarvis tests
.venv/bin/mypy jarvis
.venv/bin/lint-imports
.venv/bin/pytest -x
```

Expected: all green. Notes on what could regress and why each should hold:

- **mypy** — `active_subject: str | None` resolves cleanly; the three `pre_emit_gate(...)` calls at lines 1867 / 1900 / 1919 pass `active_subject` and the gate (post-Task 1) accepts `str | None`. ✓
- **AST canary tests** (`test_finalize_response_wires_scrub_and_hard_refusal`, `test_finalize_response_promotes_silent_log_on_hard_refusal`) — both walk `_finalize_response`'s AST for specific token presence. `_scrub_completion_keywords`, `_hard_refusal_plan`, `hard_refusal_used`, `silent_log`, `queue_review` all remain (still reachable in the str-subject retry branch). ✓
- **`pre_emit_gate` AST inspector** (`test_pre_emit_gate_contains_must_check_primitives`) — checks for `claim` / `evidence` / `permission` / `allow_completion_language` / `force_limitation_language` tokens. All present in the str branch; `allow_completion_language` also appears in the new None branch. ✓
- **F1 short-circuit tests** (`tests/unit/test_pre_emit_forced_template.py::_no_task_to_refer_to` group; `tests/unit/test_entity_resolved_outcomes.py`) — orthogonal: F1 fires inside `_run_tool_use_loop` BEFORE `_finalize_response` is reached, on the demonstrative-task-reference path. The separate use of `"unknown_subject"` at `jarvis/decision/__init__.py:716` is the F1 hard-refusal label and stays. ✓ (Acceptance criterion 4.)
- **Original 8 cases in `test_pre_emit_gate.py`** — all pass `active_subject_ref="task_X"` (`str`); the str branch is byte-identical to the pre-change body. ✓ (Acceptance criterion 3.)

If `pytest -x` reds, stop and inspect the first failing test. Do NOT delete or skip a failing test — diagnose. The expected delta is "0 fail → 0 fail" for the existing suite plus "1 fail → 1 pass" for the new integration test.

- [ ] **Step 5: Commit.**

```
git add tests/integration/test_conversational_turn_no_gate_downgrade.py jarvis/decision/__init__.py
git commit -m "$(cat <<'EOF'
fix(decision): _finalize_response passes None to pre_emit_gate when no subject

Spec §3.4.12 v0 step 2 of 2 — drops the "unknown_subject" magic-string fallback so conversational turns ("介绍一下你自己", "现在几点", "你能做什么") flow through to the gate's None pass-through (added in the previous commit) instead of being coerced into the force_limitation → retry → template → hard_refusal chain. Combined with the integration regression test, this restores the §3.4.12 v0 "只 gate consequential claims" scope end-to-end.

- jarvis/decision/__init__.py — _finalize_response no longer coerces missing active_subject to "unknown_subject"; active_subject type widened to str | None and flows through to pre_emit_gate; LOGGER.debug message updated to describe the §3.4.12 v0 pass-through path. F1 short-circuit's separate use of "unknown_subject" at line 716 (a _hard_refusal_plan label, not the gate fallback) is untouched.
- tests/integration/test_conversational_turn_no_gate_downgrade.py — new file. Drives decide() with stub LLM returning "我是 Jarvis，我可以帮你完成各种任务。"; asserts design doc §7 acceptance criteria 1 (draft surfaces verbatim · 找不到对应的 task absent · permission=allow_completion_language · downgrade_required=False), 2 (exactly 1 gate.evaluated with attempt=0), 6 (exactly 1 cost.recorded · llm.chat_calls == 1).

Tier 1: lint-imports KEPT · ruff clean · mypy strict clean · N/N unit + integration tests pass (incl. the new conversational pass-through regression) · wall <fill in>s (< 30s budget).
EOF
)"
```

(Substitute the actual wall-clock time from the last `pytest -x` run and the actual test count `N/N` before committing — the project commit template requires both in the Tier 1 line. If the wall-clock exceeds 30s, investigate before committing.)

---

# After both tasks complete

Design doc §7 has 6 acceptance criteria; Tasks 1 and 2 cover all 6 directly:

| Criterion | Covered by |
|---|---|
| 1. Conversational pass-through (LLM draft surfaces, no hard-refusal template) | Task 2 Step 4 — integration test first three assertions |
| 2. Single `gate.evaluated` (attempt=0, allow, downgrade_required=False, levels=[]) | Task 2 Step 4 — integration test `_gate_evaluated_payloads` block |
| 3. No regression on tracked-task turns (existing 8 unit cases) | Task 1 Step 4 + Task 2 Step 4 — `pytest -x` green |
| 4. No regression on F1 short-circuit | Task 2 Step 4 — `pytest -x` green (F1 path is orthogonal; `_finalize_response:716` `"unknown_subject"` label stays) |
| 5. Four gates green | Task 1 Step 4 + Task 2 Step 4 — explicit four-gate runs |
| 6. No new LLM calls (`cost.recorded` count drops from up to 3 to 1) | Task 2 Step 4 — integration test `cost_count` + `llm.chat_calls` assertions |

**Optional live smoke (requires real LLM credentials, costs one round-trip):**

```
.venv/bin/jarvis --runtime-root /tmp/jarvis-preempt-fix submit "介绍一下你自己"
```

Expected: a coherent Chinese self-introduction in stdout, NOT `找不到对应的 task (未验证 / unverified)`. If the response naturally contains the keyword `完成` (likely — "可以帮你完成 X" is the default phrasing), the pass-through path was exercised end-to-end against a real model. Skip if no credentials are configured.

**Memory follow-up (not a code change, end-of-cycle bookkeeping):**

After both commits land green, append a memory entry recording that ADR-0002 v3.1's Pre-emit Gate drift item is resolved, so the next session can surface "ready to flip ADR-0002 Status from Proposed → Approved" as an option (Allen's call, not the engineer's).

---

# Out of scope (do NOT do as part of this plan)

These belong to separate designs / plans:

- Upstream Pre-action Gate / Post-action Gate enforcement of "LLM claims a real-world fact without any action event" (design doc §5.1).
- v1 structured claim plan (spec §3.4.12 v1).
- Expanding `_COMPLETION_KEYWORDS` or `_COMPLETION_SCRUB_PATTERNS`.
- Changes to `attention_policy`, `_hard_refusal_plan`, the forced-limitation template, or the F1 short-circuit.
- Voice / ADR-0005 surface changes.
- ADR-0002 v3.1 Status flip (this plan unblocks it; the flip is Allen's call).

If during execution you discover an opportunity that fits one of these categories, capture it as a follow-up note — do NOT bundle it into the same PR.
