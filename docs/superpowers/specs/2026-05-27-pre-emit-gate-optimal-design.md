# Pre-emit Gate Optimal — Design

**Date:** 2026-05-27
**Branch:** `worktree-claude-adr0001`
**Spec basis (primary):** `docs/spec.html` §3.4.12 (Pre-emit Gate 的可行边界 — v0 scope rule), §3.4.4 (LLMSituationPacket — `active_task?` optional)
**Spec basis (corroborative):** §3.4.13 + §3.6.6 (ResponsePlan + routine streaming), §13.1 (Pre-emit Gate's three checks), §13 I10 (Claim ≤ Evidence)
**Companion ADR:** ADR-0002 v3.1 (Pre-emit Gate is a core mechanism of the §3.5 verification ladder)
**Status:** Draft — awaiting Allen review before handoff to writing-plans

---

## 1. Problem

When Allen says something conversational to Jarvis — "介绍一下你自己", "现在几点", "你能做什么" — Jarvis's actual reply is replaced by the boilerplate `"找不到对应的 task（未验证 / unverified）"`. The user hears a nonsense answer that has no relationship to the question they asked.

This is not a model hallucination; it is the Pre-emit Gate over-triggering.

### Where it happens

Reproducible whenever a turn ends with `active_subject_ref == "unknown_subject"` (no tracked task) AND the LLM's draft response happens to contain one of `完成 / 已完成 / done / verified` (without a preceding `未 / 没 / 不`).

"我可以帮你**完成**各种任务" is a natural self-introduction reply and trips the gate every time.

Other queries (e.g. "现在几点") only escape because the LLM happens to phrase its reply with "无法验证" — limitation language that does not match the four keywords. The escape is accidental.

### Why it happens

`jarvis/decision/gates.py:346` decides `permission` solely by:

```
permission = "allow_completion_language"  if strongest_evidence ∈ {verified, accepted}
                                          AND a Postcondition Claim exists for active_subject_ref
permission = "force_limitation_language"  otherwise
```

For any utterance that is not about a tracked task, `active_subject_ref` is `"unknown_subject"` (`jarvis/decision/__init__.py:1860`), `strongest_evidence` is `None`, the Postcondition test fails — so `force_limitation_language` is unconditional. Combined with keyword detection on the draft, `downgrade_required` triggers, sending the turn through retry (`_finalize_response` attempts 1, 2) and finally `_hard_refusal_plan`.

The retry chain is correctly implemented per ADR-0002 §3.5.8, but it is being entered for the wrong reason: there is no claim about a tracked subject in the first place.

### Why it is a spec violation

`docs/spec.html` §3.4.12 v0 specifies the gate scope explicitly:

> 只 gate consequential claims：task status、agent completion、test result、device result、memory write、current mutable state。

A self-introduction containing the verb "完成" is none of those. Invariant I10 (§13) restricts to "语音、document、task status、memory write" — capability statements in conversational replies are not in that list.

Independently, §3.4.4 (LLMSituationPacket) marks `active_task?` as optional — the spec explicitly admits the case where no task is in scope. The implementation invented the magic string `"unknown_subject"` to coerce that legitimate empty case into the gate's failure path, which is exactly backwards.

The implementation gates strictly more than the spec authorises.

---

## 2. Root cause in three lines

1. `pre_emit_gate` was written for the case where `active_subject_ref` is always a real task.
2. `_finalize_response` invented `"unknown_subject"` as a fallback when no task is in scope.
3. The gate's binary logic ("verified evidence or refuse") treats the missing-subject case as the worst case (refuse) rather than the empty case (nothing to gate).

---

## 3. Proposed change

### 3.1 Behavior

Replace the magic-string `"unknown_subject"` fallback with an honest `None`. When the gate sees no subject, it passes the draft through unchanged with `permission="allow_completion_language"`, `downgrade_required=False`, `output_risk_class="routine"`, `required_gate_mode="sentence"`.

| Situation | Old behavior | New behavior |
|---|---|---|
| Task in scope, verified Postcondition evidence | Allow completion language | Allow completion language (unchanged) |
| Task in scope, no verified evidence | Force limitation; retry; template; hard refusal | Force limitation; retry; template; hard refusal (unchanged) |
| No task in scope ("介绍你自己", "现在几点", "你能做什么") | Force limitation; retry; template; hard refusal | Pass through. Draft is shipped verbatim. |
| No task in scope, LLM does claim a real-world fact ("我已经把灯关了") | Force limitation; retry; template; hard refusal | Pass through. (See §5 — this is an upstream gap, not the Pre-emit Gate's responsibility.) |

### 3.2 Signature change

```python
def pre_emit_gate(
    draft_text: str,
    claim_evidence: ClaimEvidenceProjection,
    active_subject_ref: str | None,  # was: str
) -> ResponsePlan:
    if active_subject_ref is None:
        # No subject = no consequential claim being made about anyone.
        # Per spec §3.4.12 v0, gate only consequential claims.
        return ResponsePlan(
            text=draft_text,
            permission="allow_completion_language",
            downgrade_required=False,
            active_claim_levels=(),
            response_hash=_response_hash(draft_text),
            output_risk_class="routine",
            required_gate_mode="sentence",
        )
    # ... existing body unchanged for str case
```

### 3.3 Caller change

`_finalize_response` in `jarvis/decision/__init__.py:1843-1860` currently:

```python
active_subject = scratch.active_subject_ref
if active_subject is None and packet.open_tasks:
    active_subject = packet.open_tasks[0].task_id
if active_subject is None:
    LOGGER.debug(...)
    active_subject = "unknown_subject"
```

After:

```python
active_subject: str | None = scratch.active_subject_ref
if active_subject is None and packet.open_tasks:
    active_subject = packet.open_tasks[0].task_id
# If still None, pass None to the gate — it will pass-through cleanly.
```

The three `pre_emit_gate(...)` call sites at lines 1867 / 1900 / 1919 accept the `str | None` value as-is.

### 3.4 What stays unchanged

- ADR-0002 §3.5.8 verification ladder (claim emission, evidence accumulation, Postcondition path)
- `_FORCED_LIMITATION_TEMPLATE`, `_scrub_completion_keywords`, `_hard_refusal_plan`
- `attention_policy` and the `silent_log` / `queue_review` / `voice_notify` channels
- The four-keyword detector `_COMPLETION_KEYWORDS` and its scrub mirror
- F1 short-circuit in `decide()` (entity.resolved hard-refusal path for empty-ledger demonstrative references)
- `gate.evaluated` event emission for attempt 0; attempts 1 and 2 only fire when a subject exists and triggers downgrade

### 3.5 §13.1 three-checks preservation

Spec §13.1 specifies the Pre-emit Gate's three checks: (a) `claim ≤ evidence`, (b) `output_form` check, (c) agent self-report cannot upgrade to verified. The None branch:

- (a) **vacuously satisfied** — no claim is being made about any subject, so the inequality holds trivially.
- (b) **preserved** — the branch returns `output_risk_class="routine"`, `required_gate_mode="sentence"`, matching §3.6.6 row 1 ("routine: sentence-boundary streaming, 低延迟优先，风险低"). Surface still receives a ResponsePlan and gates accordingly.
- (c) **vacuously satisfied** — no agent report is being interpreted; this check operates on Result Interpreter output (§3.4.11), which is upstream and unaffected.

---

## 4. Why this is the optimal fix

| Criterion | This design |
|---|---|
| Spec alignment | Restores §3.4.12 v0 literal: gate only consequential claims |
| Code surface | One function signature widened (`str` → `str | None`), one branch added in `pre_emit_gate`, three lines removed in `_finalize_response`. No new modules, no new dependencies. |
| New mechanisms introduced | Zero |
| LLM calls added per turn | Zero (eliminates the unnecessary retry chain on conversational turns) |
| Risk of regressing the real protection | Zero when a task is in scope. The `str` branch of `pre_emit_gate` is byte-identical to the current implementation. |
| Test surface | `tests/unit/test_pre_emit_gate.py` adds one case (None → allow); existing 8 cases unchanged. `_finalize_response` integration tests gain one case for the conversational pass-through path. |
| ADR-0002 v3.1 unblocking | This is the last drift item between impl and the §3.4.12 v0 spec that ADR-0002 references. Closes the gap before Status flip. |
| Allen's "never patch prompts" rule | Honoured — fix lives in the gate, not in the prompt. |

### Alternatives rejected

**Add an upstream intent classifier in `decide()`** that detects conversational queries and bypasses the gate. Rejected: new mechanism, possible extra LLM call, more surface area, spec §3.4.12 v0 does not require classification. Closer to the v1 ("structured claim plan") vision but premature for v0.

**Use NLP to check whether the draft's "完成" actually refers to a tracked subject.** Rejected: spec §3.4.12 explicitly says "不做 full natural-language NLP parser". Brittle.

**Leave the implementation alone; require Allen to phrase introductions without "完成".** Rejected: the bug is in the implementation, not the user.

---

## 5. Risks and edge cases

### 5.1 Upstream gap (acknowledged, not addressed here)

If the LLM produces "我已经把灯关了" while `active_subject_ref` is `None`, the gate now passes it through. The current implementation also fails to gate this correctly — it would catch the keyword but apply the wrong remediation (force limitation language without a corresponding tracked subject).

The correct enforcement point is upstream, by spec:

- §13.1 Pre-action Gate (fires before `action.pre_emit`) — checks "是否绑定 active task". An LLM claiming "灯关了" without dispatching an ActionRequest never reaches this gate, so the LLM cannot create a real-world effect; the claim is at most fictitious.
- §3.5.7 `post_action_check` — when an action IS dispatched, the tool's declared `result_semantics` and `post_action_check` recipe normalise the result into the correct evidence level (ack / observed / verified). The subject is then in scope; the Pre-emit Gate's str branch catches the unsupported claim.

A separate design (likely a v0.1 / v1 follow-up) should cover "LLM claims a real-world fact without any action event at all". The spec's structural answer is "no action event ⇒ no state changed ⇒ the claim is detectably false against the State Object" — verifying this at gate time would require draft-vs-state cross-checking that v0 explicitly defers (§3.4.12 v1 "structured claim plan"). Out of scope here.

### 5.2 What if the LLM volunteers "task X is done" with no task in scope

If `active_subject_ref` is `None` but the draft mentions a task id, the gate as designed passes it through. This is a degenerate case — the LLM either hallucinated the task id (caught by entity resolver / F1 short-circuit upstream) or is referencing a task that should have been put into scope (caught by Pre-action Gate). The Pre-emit Gate is not the right layer to detect this.

### 5.3 `gate.evaluated` event semantics

The `pre_emit_gate` short-circuit branch still produces a `ResponsePlan`, and `_finalize_response` still emits `gate.evaluated(attempt=0)` with that plan. `active_claim_levels=()` makes the audit explicit: "evaluated, nothing to claim against". The retry chain naturally does not fire because `downgrade_required=False`.

---

## 6. Scope

### In scope

- Modify `pre_emit_gate` signature: `active_subject_ref: str | None`.
- Add the `None` branch returning a pass-through `ResponsePlan`.
- Remove the `"unknown_subject"` fallback in `_finalize_response`.
- Unit test for the `None` pass-through path.
- Integration test: a conversational turn ("介绍一下你自己" or equivalent) lands with the LLM's draft, not the hard-refusal template, and emits exactly one `gate.evaluated`.
- Update `LOGGER.debug` message in `_finalize_response` to reflect that None is now a clean pass-through, not a "failure mode reaching the operator via the surface".

### Out of scope (separate designs)

- Upstream enforcement of consequential claims without action events (§5.1).
- v1 structured claim plan (LLM emits claims + required evidence + allowed phrasing, runtime approves).
- Adding more completion keywords or expanding the scrub set.
- Changes to `attention_policy`, `_hard_refusal_plan`, or the retry chain.
- Any changes to voice surface (ADR-0005), runtime, or execution layers.
- ADR-0002 v3.1 Status flip — this design unblocks it; the flip is Allen's call.

### Will not change

- Any spec text in `docs/spec.html`.
- Any ADR file in `docs/adr/`.
- The four-keyword detector regex set.
- The forced-limitation template text.
- The hard-refusal plan text or its bilingual rendering.

---

## 7. Acceptance criteria

1. **Conversational pass-through** — A live turn with input `"介绍一下你自己"` against an empty ledger produces an LLM-generated reply (not the hard-refusal template) in the `surface.response_emitted` payload.
2. **Gate event audit** — That same turn emits exactly one `gate.evaluated` event with `attempt=0`, `permission="allow_completion_language"`, `downgrade_required=False`, `active_claim_levels=[]`.
3. **No regression on tracked-task turns** — Existing unit tests in `tests/unit/test_pre_emit_gate.py` continue to pass with no modification to the eight existing call sites (they all pass `str` arguments).
4. **No regression on F1 short-circuit** — The empty-ledger demonstrative-reference path ("昨天那个 task 给 Codex 跑一下" against empty ledger) still hard-refuses via `_no_task_to_refer_to`, not via the gate retry chain.
5. **Four gates green** — `ruff check`, `mypy`, `lint-imports`, `pytest -x` all pass after the change.
6. **No new LLM calls** — The conversational pass-through path does not trigger the attempt-1 retry or the attempt-2 template branch; `cost.recorded` count for a conversational turn drops to one (the original draft generation), down from up to three.

---

## 8. Implementation surface (for writing-plans)

**Modify (2 production files):**

- `jarvis/decision/gates.py:346-424` — widen signature `active_subject_ref: str | None`, add the None pass-through branch, update docstring.
- `jarvis/decision/__init__.py:1843-1860` — drop `"unknown_subject"` fallback, pass `None` through to the three `pre_emit_gate(...)` call sites at lines 1867 / 1900 / 1919.

**Add (2 tests):**

- `tests/unit/test_pre_emit_gate.py` — case: `pre_emit_gate("我可以帮你完成各种任务", projection, active_subject_ref=None)` returns `permission="allow_completion_language"`, `downgrade_required=False`, `active_claim_levels=()`.
- `tests/integration/test_conversational_turn_no_gate_downgrade.py` (new file) — end-to-end live-run-style: submit `"介绍一下你自己"` to an empty-ledger daemon, assert `surface.response_emitted.text` is the LLM draft (not the hard-refusal template) and that exactly one `gate.evaluated` event exists with `attempt=0`.

**Inspect only (verify no regression, no code change expected):**

- `jarvis/decision/__init__.py:1948-1958` — `attention_policy` `hard_refusal_used` promotion still fires only when the retry chain ran to exhaustion, which now requires a real subject. Conversational turns route through normal `attention_policy` rules and should land on the surface as appropriate. Covered by the new integration test.
- `tests/canary/test_gate_contracts.py:127-136` — AST inspection. Passes the function existence and the primitive-string checks; widening the signature does not affect.
- `tests/unit/test_pre_emit_forced_template.py::_GATE_TO_SCRUB_COVERAGE` — drift guard for keyword/scrub parity. Untouched (we add no keywords).

---

## 9. Open questions for Allen

None at the design level. All decisions are pinned to spec §3.4.12 v0 literal text. The signature choice (`str | None` vs default-value sentinel) is the only stylistic call; `str | None` chosen because it forces every call site to acknowledge the case at the type system level, which is what made the original bug latent.

---

## 10. References

**Spec (primary):**
- §3.4.12 — Pre-emit Gate 的可行边界 (v0 scope rule, the literal authority for this fix)
- §3.4.4 — LLMSituationPacket schema with `active_task?` optional

**Spec (corroborative):**
- §3.4.13 — ResponsePlan (`output_risk_class`, `required_gate_mode`)
- §3.6.6 — Streaming TTS and ResponsePlan (routine = sentence-boundary streaming)
- §3.4.11 — Result Interpreter (`result_semantics` table — orthogonal but adjacent)
- §3.5.7 — `post_action_check` (the structural enforcement point for §5.1 gap)
- §13.1 — Pre-emit Gate three checks; §13 I10 — Claim ≤ Evidence

**ADR and prior work:**
- `docs/adr/0002-real-codex-flagship-scenario.md` §3.5.8 — verification ladder
- `jarvis/decision/gates.py:312-424` — current Pre-emit Gate implementation
- `jarvis/decision/__init__.py:1820-1965` — `_finalize_response` retry chain
- Prior memory: S2840 / S2841 / S2842 — root cause traced, two fix options proposed but not applied
- Related working fix: F1 short-circuit (B-NEW-4, commits `8305c41` + `f11713c`) — same "if nothing to gate, don't engage" principle applied at the entity resolver layer
