# Wave 4–5 takeover repair

Goal: close the audited authorization, cancellation, ownership, continuation,
and accounting correctness gaps while preserving the existing realtime design.

Done criteria and evidence:
- R1: two real decisions on one confirmation yield one canonical acceptance,
  one authorization/outbox claim, and at most one L4 handler acceptance;
  replay and crash ambiguity fail closed. Unsafe pump configurations stay off.
- R2–R3: cancellation winning admission prevents late tool/provider work;
  a 50 ms SQLite cancel budget includes connection and admission-lock waits.
- R4–R7: short tools do not retain fictitious debt; mutating siblings serialize;
  restart restores unresolved original-mode debt; shutdown rejects queued work;
  failed process close never proves physical quiescence.
- R8–R10: one terminal has one semantic continuation; concurrent vision calls
  have isolated clients and legal accounting connections; worker cost facts
  precede their triggers and concurrent L3 accounting cannot double charge.
- R11: both flat provider configs preserve legacy API-key fallback.
- Real SQLite connections, barriers, registry/runtime/pump, process failure
  injection, full non-live suite, ruff, strict mypy, import contracts, and diff
  checks provide falsifiable evidence. An isolated daemon/cloud/L5 burn records
  exact clean revision, config and module provenance, with text, audio, callback,
  and physical evidence distinguished.

Constraints: start at audited 6ed7280 (Wave 3 ancestor 7b69a53); isolated worktree;
default flags off; no Python unit tests, push, main/Claude-worktree mutation,
production DB access, user-data deletion, or main-volume changes. Use apply_patch
for edits. Keep verified Git checkpoints and concise external state.

Non-goals: Wave 6, general event-driven continuation redesign, streaming permit
and truthful-progress features, voice/AEC/Inherent UX completion. Record remaining
architecture boundaries explicitly. Controller independently reviews this module.
After this repair passes that review, stop: no next Wave, feature task, or automatic
continuation is authorized. Findings within this repair may be fixed in this task.
