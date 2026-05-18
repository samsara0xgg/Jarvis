# Day-1 Build Progress

ADR: `docs/adr/0001-mac-only-flagship-scenario.md`.

Per ADR § Process discipline, each step records: Files, Legacy consulted,
Legacy-bypassed, Tier 1, Notes, Next.

---

## Step 0 — Reference scan (Done in main worktree)

- Files: `docs/adr/0001-legacy-scan.md` (copied verbatim into worktree).
- Legacy consulted: full repo scan recorded in legacy-scan.md.
- Legacy-bypassed: none yet.
- Tier 1: n/a (no code yet).
- Notes: ADR + legacy-scan both copied into worktree for in-tree
  reference. Build branch `worktree-claude-adr0001`.
- Next: Step 1.

---

## Step 1 — pyproject.toml ratchet + deps

- Files: `pyproject.toml` (ratchet), `.importlinter` (fix
  `containers = jarvis` × `jarvis.shared` double-prefix bug),
  `tests/__init__.py` (added to satisfy ruff INP001).
- Legacy consulted: none.
- Legacy-bypassed: none.
- Tier 1:
  - T1.A `lint-imports`: 1 contract kept, 0 broken.
  - T1.B `ruff check .`: All checks passed.
  - T1.C `mypy .` (strict mode in config): no issues in 12 source files.
  - T1.D, T1.E: no unit tests yet.
- Notes: Created worktree-local `.venv` (uv) so parallel agents don't
  fight over the shared `mise` venv at the main project root. Restored
  main `.venv` editable install to the main worktree after stamping
  this worktree. New runtime deps: `openai`, `anthropic`, `PyYAML`;
  dev adds `types-PyYAML` for mypy `--strict`. Added `live_llm` pytest
  marker (Tier 2 gate).
- Next: Step 2 (L1 constitution + shared types) — delegate to subagent.

---

## Step 2 — L1 Constitution + shared types

- Files:
  - `jarvis/constitution/__init__.py` (189 LOC) — `JARVIS_IDENTITY` Final
    string, `PRINCIPLES` MappingProxyType of C1..C6 frozen
    `ConstitutionalPrinciple` dataclasses (canonical Chinese text from
    spec §3.2.1), `NON_GOALS` tuple of 6 (spec §3.2.2), `AUTONOMY_LEVELS`
    tuple `("L0","L1","L2","L3","L4")`, `AUTONOMY_AXES` MappingProxyType
    of 5 frozen `AutonomyAxis` dataclasses (spec §3.2.6).
  - `jarvis/shared/__init__.py` (252 LOC) — `Event`, `Claim`, `Evidence`,
    `ActionRequest` frozen dataclasses; `PromptContext(system: str)`
    frozen dataclass; `AuthorizationLease` TypedDict; `CallerPrincipal`
    enum with 6 members per spec §3.5.2; `EvidenceLevel` / `ClaimType` /
    `RiskLevel` Literal aliases.
  - `tests/unit/__init__.py` (empty marker).
  - `tests/unit/test_constitution.py` (92 LOC, 9 tests).
  - `tests/unit/test_shared_types.py` (140 LOC, 8 tests).
  - `pyproject.toml` — added `[tool.ruff.lint.per-file-ignores]` for
    `jarvis/constitution/__init__.py` (RUF001: canonical CJK
    punctuation in spec-copied text) and `tests/**/*.py` (S101, ANN201,
    PLR2004, S108: pytest idioms). Each entry has a justification
    comment per canary H6 spirit.
- Legacy consulted:
  - `jarvis-legacy/tools_v2/registry.py` — caller_scope is `set[str]`
    with string labels (`"llm"`, `"regex_router"`). Day-1 swaps to a
    typed `CallerPrincipal` enum with spec §3.5.2 canonical names; this
    is the intended adaptation per ADR § Module map.
  - `jarvis-legacy/docs/product/jarvis-product-constitution.html` —
    located but not consulted; spec.html §3.2 is the canonical source
    and was used directly.
  - No legacy `core/identity.py` or `core/constitution.py` exists.
- Legacy-bypassed:
  - `Legacy-bypass: jarvis-legacy/core/personality.py — Xiaoyue persona
    explicitly discarded per ADR § Identity; identity label lives in
    L1 Constitution as the frozen string "Jarvis".`
- Tier 1:
  - T1.A `lint-imports`: 1 contract kept, 0 broken. 10 files analyzed.
  - T1.B `ruff check .`: All checks passed.
  - T1.C `mypy .` (strict): Success: no issues found in 15 source files.
  - T1.D `pytest tests/unit/ -x`: 17 passed in 0.02s.
- Notes:
  - L1 Constitution carries canonical Chinese text verbatim from
    spec §3.2.1 / §3.2.2 / §3.2.6. Added a per-file `RUF001` ignore for
    `jarvis/constitution/__init__.py` with same-line comment in
    `pyproject.toml`. Per-file-ignores are a separate table from the
    top-level `ignore` list — canary H6 (planned Step 11) checks the
    top-level list; this new section follows the same justification
    discipline.
  - `Mapping` is imported inside `if TYPE_CHECKING:` blocks so ruff
    `TC003` is satisfied; `from __future__ import annotations` makes
    all annotations strings, so runtime evaluation is not needed.
  - Spec ambiguity fixed: spec §3.2.6 lists 5 axes (Proactivity /
    Confirmation / Verification / Execution / Memory write) but doesn't
    name a discrete level vocabulary. The ADR says "C1–C6 + 5 autonomy
    axes" without specifying levels. Picked the L0..L4 ladder per spec
    §3.4.10 risk wording (matches ActionRequest.risk_level shared
    vocabulary) so EffectivePolicy ceiling comparisons stay in one
    namespace. Exposed as `AUTONOMY_LEVELS`.
  - `PromptContext` carries only `system: str` per ADR Q3 option (a) —
    legacy `core/llm.py` had a richer `prompt_context` argument tied to
    personality + hot-memory assembler; Day-1 deliberately replaces
    with a single string and lets the dataclass grow additively later.
  - `ActionRequest` carries only the Pre-action Gate inputs plus
    correlation keys (action_id, tool_name, target_entity_ref,
    caller_principal, risk_level, arguments, authorization_lease,
    run_id, turn_id). Spec §3.4.8 lists more fields
    (`result_expected_by`, `max_duration`, `timeout_policy`,
    `retry_policy`, `expected_postcondition`); deferred to Step 6 when
    L4 lifecycle / scheduler actually consume them.
- Next: Step 3 (L6 deployment with `JARVIS_RUNTIME_ROOT`).

---

## Step 3 — L6 Deployment (`JARVIS_RUNTIME_ROOT` bootstrap)

- Files:
  - `jarvis/deployment/__init__.py` (121 LOC) — `RuntimePaths` frozen
    dataclass (`root`, `event_log`, `artifacts_root`, `registry` +
    `artifact_dir_for_run(run_id)` method) and `bootstrap_runtime(root)`
    function. Stdlib only (`os`, `pathlib`, `dataclasses`); no imports
    from any `jarvis.*` sibling. Single canonical home for the
    `"~/.jarvis"` literal lives in module constant
    `_DEFAULT_RUNTIME_ROOT_LITERAL` — canary H8 (Step 11) will scan
    `jarvis/` for that string outside this module.
  - `tests/unit/test_deployment.py` (160 LOC, 14 tests) — covers
    explicit-arg precedence, env-var override, default expansion via
    HOME redirect (no real `~/.jarvis/` touched), empty-env fallback,
    path shape, `mkdir` for `root` + `artifacts_root` (but not
    `mac_events.db` or `registry.json`), idempotency (twice),
    frozen-dataclass enforcement, `artifact_dir_for_run` lazy-create,
    per-run isolation, absolute paths, no env mutation, and direct
    `RuntimePaths` construction.
- Legacy consulted:
  - `jarvis-legacy/core/scheduler.py:26-27` — the
    `Path(...).parent.mkdir(parents=True, exist_ok=True)` pattern for
    SQLite db paths. Same idiom reused; only one line of borrowed
    shape.
- Legacy-bypassed:
  - `Legacy-bypass: jarvis-legacy/core/scheduler.py — hardcodes
    "data/scheduler.db" / "data/memory/jarvis_memory.db" relative paths
    inline; Day-1 routes all runtime state through
    JARVIS_RUNTIME_ROOT-derived RuntimePaths per ADR § Configurable
    paths. Skeleton not reusable.`
  - `Legacy-bypass: jarvis-legacy/core/_paths.py — a denylist/allowlist
    sandbox helper for user-file tools; not a runtime-root bootstrap.
    Reusable in Stage 2 when real file tools land, not Day-1.`
- Tier 1 (available subset T1.A–T1.D before Step 11):
  - T1.A `lint-imports`: 1 contract kept, 0 broken. 10 files analyzed.
  - T1.B `ruff check .`: All checks passed.
  - T1.C `mypy .` (strict): Success: no issues found in 16 source
    files.
  - T1.D `pytest tests/unit/ -x`: 31 passed in 0.04s
    (constitution 9 + deployment 14 + shared_types 8).
- Notes:
  - **Env-var resolution.** Order is (1) explicit `root=` argument
    (used by tests / runtime composition root override), (2)
    `JARVIS_RUNTIME_ROOT` env var if set AND non-empty (empty string
    falls through to default — guards against shells that leak
    `JARVIS_RUNTIME_ROOT=`), (3) built-in default `~/.jarvis` expanded
    via `Path.expanduser()`. All resolved paths are absolute
    (`Path.resolve()`).
  - **`artifact_dir_for_run` is lazy-create.** Per ADR Step-3 Q4
    ("creates lazily and returns, or returns the Path without creating
    — pick one and document"), the chosen behavior is lazy: the
    `${artifacts_root}/run_<run_id>/` directory is created on each
    call with `exist_ok=True`. This means L4 `spawn_worker` (Step 6)
    can call `paths.artifact_dir_for_run(R1).joinpath("diff.json")`
    and write directly without re-mkdir'ing. Tested idempotent via
    drop-a-file-then-recall.
  - **Bootstrap creates `root` and `artifacts_root` only.**
    `mac_events.db` and `registry.json` are owned by L2 at write
    time; L6 only declares where they live. Avoids overlap with
    Step 4 SQLite open / Step 4 (optional) file-backed registry.
  - **No `jarvis.state` import.** Confirmed by grep over the module:
    only `os`, `pathlib.Path`, `dataclasses.dataclass`. Canary H13
    (Step 11) will enforce this via AST scan; Day-1's stricter L6
    rule (ADR § Six-layer boundary contract) is satisfied by
    construction.
  - **No `os.environ[...] =` writes.** L6 only reads
    `JARVIS_RUNTIME_ROOT`. Asserted by
    `test_bootstrap_does_not_mutate_env`.
  - **Single home for `~/.jarvis`.** `grep -rn '~/.jarvis' jarvis/`
    shows the literal only inside `jarvis/deployment/__init__.py`
    (constant `_DEFAULT_RUNTIME_ROOT_LITERAL` + 3 docstring
    references; canary H8 will be string-literal-only in the AST scan
    sense). Other layers go through `RuntimePaths`.
- Next: Step 4 (L2 `event_log.py` — SQLite append-only + EventTypeRegistry).

---

## Step 4 — L2 Event Log + EventTypeRegistry

- Files:
  - `jarvis/state/event_log.py` (605 LOC) — single `events` SQLite table
    with append-only triggers (`events_no_update` /
    `events_no_delete`, both `BEFORE` triggers calling
    `RAISE(ABORT, ...)`), three indexes on `(type)` /
    `(source_event_id)` / `(ts_epoch_ms)`, in-module
    `EventTypeRegistry` covering the 19 Day-1 event types
    (`MappingProxyType` over frozen `EventTypeSchema` dataclasses,
    `get` / `requires` / `optional` / `iter_types` API),
    `emit_event(conn, *, type, payload, source_event_id?,
    correlation?, ts_epoch_ms?, schema_version?, event_uid?) -> Event`
    keyword-only API with validation order
    (registry → schema_version → required-payload → dangling source
    FK), four typed exceptions (`UnregisteredEventTypeError`,
    `SchemaVersionMismatchError`, `MissingPayloadFieldError`,
    `DanglingSourceEventError`) under one base `EventLogError`,
    `open_event_log(path)` idempotent connect, `iter_events(conn)` /
    `get_event(conn, uid)` readers. Stdlib only (`sqlite3`, `json`,
    `time`, `uuid`, `types`, `dataclasses`, `typing`, `pathlib`) plus
    `jarvis.shared.Event`.
  - `tests/unit/test_event_log.py` (414 LOC, 22 tests) — registry
    coverage (19 canonical types, `gate.evaluated` schema verbatim,
    `get` returns None for unknown, `requires` / `optional`
    round-trip), `emit_event` happy paths (round-trip via
    `iter_events`, uid uniqueness + 32-char hex shape, `get_event`
    round-trip + None on miss, deterministic ordering with
    source-chain integrity, optional-payload-absent acceptance,
    nested-payload JSON survival), validation rejections (unregistered
    type, missing required field, schema_version mismatch + matching
    accepted, dangling source_event_id + valid accepted), append-only
    trigger enforcement (UPDATE raises, DELETE raises), idempotent
    `open_event_log` (two opens preserve data + triggers still fire +
    `sqlite_master` shows each trigger exactly once), shared.Event
    isinstance check.
- Legacy consulted:
  - `jarvis-legacy/core/mcp_server.py:26-100` — read-only sqlite3
    connection helper + Row factory. Pattern noted but not reused
    Day-1: legacy uses `sqlite3.Row` factory and JSON-column
    deserialization helper; Day-1 sticks with positional tuple unpack
    in `_row_to_event` because the column set is fixed and small
    (8 columns), so Row → dict overhead isn't justified Day-1.
  - `jarvis-legacy/jarvis/` and `jarvis-legacy/core/` — searched for
    `CREATE TABLE events`, `event_log`, `emit_event`; no L2 event log
    found. ADR Step 4 reference sources note this: legacy was
    memory-based, no clean L2 spine exists.
- Legacy-bypassed:
  - `Legacy-bypass: jarvis-legacy/core/mcp_server.py — RO sqlite
    helper for MCP exposure of memory observations; not a write-side
    L2 event log. Day-1 writes its own connect + schema + triggers
    inline because mcp_server only opens RO and never installs
    schema/triggers.`
- Tier 1 (available subset T1.A–T1.D before Step 11):
  - T1.A `lint-imports`: 1 contract kept, 0 broken (11 files
    analyzed). State sibling `jarvis.state.event_log` only imports
    `jarvis.shared` from within `jarvis.*`; no cross-sibling links.
  - T1.B `ruff check .`: All checks passed (stdlib-only +
    `from __future__ import annotations`; messages assigned to local
    `msg` per `EM102`; `emit_event` carries `# noqa: PLR0913` with
    spec-§5.1 justification on the line; bare SQL literals avoid the
    S608 false positive).
  - T1.C `mypy .` (strict): Success: no issues found in 18 source
    files.
  - T1.D `pytest tests/unit/ -x`: 53 passed in 0.08s
    (constitution 9 + deployment 14 + event_log 22 +
    shared_types 8). Tests use `tmp_path` exclusively — `~/.jarvis`
    is never touched.
- Notes:
  - **Append-only enforcement is trigger-level**, not advisory. Two
    `BEFORE` triggers on `events` (`events_no_update` /
    `events_no_delete`) call `RAISE(ABORT, '<message>')` so any
    raw-SQL `UPDATE events SET ...` / `DELETE FROM events WHERE ...`
    surfaces as `sqlite3.IntegrityError` to the Python caller,
    regardless of how the connection is opened. Acceptance criterion
    A6 (Tier 2) literally attempts both inside the test — satisfied
    by construction here. Triggers are created with
    `IF NOT EXISTS` so the idempotency invariant on
    `open_event_log` holds.
  - **Connection / commit lifecycle.** Default `isolation_level`
    (deferred transactions). `emit_event` commits per successful
    INSERT — one event per transaction. Caller does not need to call
    `conn.commit()`. The schema-install path in `open_event_log` also
    commits before returning. This was a binary choice (autocommit
    via `isolation_level=None` vs explicit commit-per-write); explicit
    commit was picked because it lets validation errors short-circuit
    cleanly without partial state, and it makes the "one event = one
    durable row" boundary explicit. Documented in the `emit_event`
    docstring.
  - **`PRAGMA foreign_keys = ON` intentionally NOT set.** The
    `source_event_id` FK is `TEXT` pointing at `events.event_uid` (a
    UNIQUE column rather than a primary key). Application-side
    validation in `_validate_source_event_id` does a SELECT-LIMIT-1
    pre-check before INSERT — this also gives us the typed
    `DanglingSourceEventError` for caller match instead of an opaque
    SQLite integrity message. The append-only triggers run
    independently of any FK pragma.
  - **uuid generation.** `uuid.uuid4().hex` Day-1 (32-char lowercase
    hex). Python 3.12 stdlib has no UUIDv7 — the
    `jarvis.shared.Event` docstring already says "UUIDv7-ish hex
    string" so the contract surface is unchanged. Stage 2 can swap to
    UUIDv7 (or `uuid6`-package) without touching the table schema —
    `event_uid` is `TEXT NOT NULL UNIQUE`, format is opaque to SQLite.
  - **Payload JSON serialization.** `json.dumps(dict(payload),
    sort_keys=True, separators=(",", ":"))` — coercion to plain
    `dict` first means non-dict `Mapping` subclasses
    (`MappingProxyType`, custom subclasses) round-trip without
    `TypeError: Object of type ... is not JSON serializable`. Tests
    cover nested list / dict payloads (entity.resolved candidates
    list) end-to-end.
  - **Single canonical "events" literal site.** The table name
    string `"events"` appears only in this module — schema DDL,
    trigger DDL, both read queries, the INSERT statement, and one
    docstring reference. Step 11 canary H1 will whitelist
    `jarvis/state/event_log.py` as the only INSERT site (grep over
    `INSERT INTO events`).
  - **EventTypeRegistry is in-module.** Spec §5.4 calls for a single
    source of truth; ADR § Configurable paths says
    `${JARVIS_RUNTIME_ROOT}/registry.json` is OPTIONAL Day-1 (L6 places
    the path; L2 doesn't have to populate it). Day-1 ships the
    registry as a frozen `MappingProxyType` over
    `EventTypeSchema` dataclasses so tests are self-contained and
    canary H2 (AST scan for `emit_event("<type>", ...)` literals
    referencing unregistered types) sees a deterministic constant.
  - **`gate.evaluated` schema is copied verbatim from ADR
    § Day-1 EventTypeRegistry extensions** — required
    `(gate, outcome, reasons)`, optional
    `(action_id, response_hash, claim_levels, check_results)`,
    `owner_layer="L3"`, `schema_version=1`. Test asserts this
    explicitly.
  - **19 event types registered** (one more than the 16 ADR canonical
    list — three terminal lifecycle states `action.failed` /
    `action.timeout_assumed` / `action.cancelled` are explicitly
    enumerated in the ADR Step 4 spec block but not on the happy-path
    trace; they're registered Day-1 so the negative-path test
    (Step 13) and the lifecycle gate (Step 6) can emit them without
    a registry edit).
  - **Layer boundary.** Imports: `json`, `sqlite3`, `time`, `uuid`,
    `dataclasses`, `types`, `typing`, `pathlib` (TYPE_CHECKING only),
    `collections.abc` (TYPE_CHECKING only), `jarvis.shared.Event`.
    No imports from `jarvis.constitution`, `jarvis.decision`,
    `jarvis.execution`, `jarvis.surface`, `jarvis.deployment`,
    `jarvis.runtime`, `jarvis.cli`. `lint-imports` confirms.
- Next: Step 5 (L2 `projections.py` — Task Ledger + Recent Trace +
  Claim/Evidence pure fold).

---

## Step 5 — L2 Projections (Task Ledger + Recent Trace + Claim/Evidence)

- Files:
  - `jarvis/state/projections.py` (638 LOC) — three pure-fold
    projections plus a `ProjectionSet` bundle. All public dataclasses
    `frozen=True`: `TaskLedgerRecord`, `TaskLedger`, `TaskLedgerSnapshot`,
    `RecentTrace`, `ClaimEvidenceProjection`, `ProjectionSet`. Public
    `TaskStatus` Literal of 6 values (`open`, `reported_complete`,
    `verified_complete`, `failed`, `cancelled`, `unverifiable`) — Day-1
    fold rules produce the first three. `derive_status(task_id)` is the
    single status-derivation function (no `status` column; H11 will AST
    scan this file). `rebuild_projections(conn)` reads via
    `iter_events(conn)` (no SQL writes) and returns a deep-equal
    `ProjectionSet` on repeat calls. `make_snapshot(conn)` is the
    L3-facing alias (Day-1 identical). Stdlib only (`collections.deque`,
    `dataclasses`, `typing`) plus `jarvis.shared` (`Claim`, `ClaimType`,
    `Evidence`, `EvidenceLevel`) and `jarvis.state.event_log`
    (`iter_events`).
  - `tests/unit/test_projections.py` (701 LOC, 28 tests). Seeds events
    through `emit_event` against `tmp_path` SQLite — never touches
    `~/.jarvis`.
- Legacy consulted:
  - `jarvis-legacy/memory/hot/conversation.py` — sliding-window store,
    not a projection over events. Pattern inspected; nothing reusable
    for fold-from-events semantics.
  - `jarvis-legacy/core/tool_result.py` — vocabulary will be borrowed
    in Step 9 (Result Interpreter); the projection module only consumes
    the resulting `claim.created` / `evidence.attached` events.
- Legacy-bypassed:
  - `Legacy-bypass: jarvis-legacy/memory/hot/conversation.py — Day-1
    Recent Trace is a pure event-log fold (ring buffer over Event
    sequence), not a sliding window over Allen-Jarvis conversation
    turns. Legacy class is a different abstraction (chat-history
    centric, not event-centric); not reusable without distorting the
    spec §6 contract.`
- Tier 1 (available subset T1.A–T1.E before Step 11):
  - T1.A `lint-imports`: 1 contract kept, 0 broken. 12 files analyzed.
    `jarvis.state.projections` imports `jarvis.shared` (Claim,
    ClaimType, Evidence, EvidenceLevel) and `jarvis.state.event_log`
    (iter_events); both within-layer or downward, no cross-sibling.
  - T1.B `ruff check .`: All checks passed.
  - T1.C `mypy .` (strict): Success: no issues found in 20 source
    files.
  - T1.D `pytest tests/unit/ -x`: 81 passed in 0.21s
    (constitution 9 + deployment 14 + event_log 22 + projections 28 +
    shared_types 8).
  - T1.E elapsed wall-clock for `tests/unit/`: 0.17s (well under 30s
    bound).
- Notes:
  - **Status is computed, not stored.** `TaskLedgerRecord` has no
    `status` / `derived_status` field. The single source of truth is
    `_derive_status(records_by_task_id, claim_evidence, task_id)`, a
    module-level helper consumed by both `TaskLedger.derive_status` and
    `TaskLedgerSnapshot.derive_status`. Both call sites produce
    identical results (asserted by
    `test_snapshot_derive_status_matches_ledger`). Step 11 canary H11
    will AST-scan this file for `task["status"] = ...` /
    `task.status = ...` / SQL `UPDATE ... SET status = ...` outside
    `derive_status()` — by construction there are zero such
    assignments (status is a function return value, not a stored
    attribute).
  - **Status derivation rules** (ADR § Acceptance D + F):
    - `verified_complete` requires BOTH a `task.verified` event for
      this `task_id` AND a Postcondition Claim with
      `evidence.level in {verified, accepted}` whose `subject_ref`
      equals the `task_id`. Either alone is insufficient (catches
      regressions in either direction —
      `test_derive_status_not_verified_without_postcondition_evidence`
      and `test_derive_status_not_verified_without_task_verified_event`).
    - `reported_complete` requires at least one `worker.reported`
      under any run of the task (via `run.started`-bridged
      `run_id → task_id` correlation), AND the verified condition
      above is NOT met.
    - `open` otherwise (including unknown `task_id` —
      `test_derive_status_unknown_task_id_is_open`).
  - **worker.reported correlation challenge.** The
    `worker.reported` event payload carries `(run_id, action_id,
    status)` but NOT `task_id`. The fold bridges through
    `run.started(run_id, task_id)` events to build a
    `run_id → task_id` map, then re-keys subsequent
    `worker.reported` events into the right Task Ledger row. Tests
    seed both events; the
    `test_derive_status_reported_complete_after_worker_report` case
    is the canonical fixture for this code path. If a
    `worker.reported` arrives with no matching prior `run.started`,
    the fold silently skips it (ADR § Acceptance D path requires
    `run.started` first; spec §7.5 names Run as the canonical
    correlation unit).
  - **subject_ref ↔ task_id correlation.** Per canonical-trace
    evt 20 (`claim.created(type=Postcondition, subject_ref=task_X)`),
    a Postcondition Claim's `subject_ref` IS the `task_id` of the task
    being verified. The fold therefore uses raw equality
    (`claim.subject_ref == task_id`) — no aliasing, no entity-registry
    indirection Day-1. Stage 2 multi-entity scenarios will route
    through the Entity Registry projection (deferred per ADR §
    Stub strategy L2 row).
  - **Evidence ladder.** Frozen module-private `_EVIDENCE_LADDER` and
    `_EVIDENCE_RANK` per spec §8.4 (`reported < observed < executed <
    verified < accepted`). `strongest_level_for(subject_ref)` walks
    every claim for the subject, every evidence on each claim, and
    returns `max(...)` keyed by ladder rank. `None` when no evidence
    exists. Used by Pre-emit Gate (Step 9) per ADR § Gate contracts.
  - **Recent Trace is oldest-first.** `RecentTrace.iter()` yields
    events in append order (the oldest within the ring-buffer window
    first). Implementation uses `collections.deque(maxlen=...)` for
    O(n) folds; the deque does not escape — the result is frozen as a
    tuple on construction. Default `max_size=200`; `max_size=0` yields
    an empty trace; negative `max_size` raises `ValueError`.
  - **Frozen dataclasses with mutable contents.** `TaskLedger` /
    `TaskLedgerSnapshot` / `ClaimEvidenceProjection` carry `dict` /
    `tuple` fields. Frozen-dataclass equality is structural, so two
    rebuilds with the same input dict contents compare equal —
    `test_rebuild_projections_is_idempotent` asserts deep equality
    across two consecutive rebuilds. Step 11 canary H11 / H1 do not
    require these projections to be deeply immutable; the contract is
    "no SQL writes, status is derived", both met by construction.
  - **No L3/L4/L5/L6 imports.** `grep -rn 'import' jarvis/state/projections.py`
    shows: `collections.deque`, `dataclasses`, `typing.{TYPE_CHECKING,
    Final, Literal, cast}`, `jarvis.shared.{Claim, ClaimType, Evidence,
    EvidenceLevel}`, `jarvis.state.event_log.iter_events`, plus
    TYPE_CHECKING-only `sqlite3`, `collections.abc`, and
    `jarvis.shared.Event`. `lint-imports` confirms boundary contract
    KEPT.
  - **Single L3 surface.** `make_snapshot(conn)` is the L3-facing
    name (per Step 5 spec). `rebuild_projections(conn)` is the
    Day-1 internal alias; Day-1 they are byte-identical. Stage 2 may
    introduce snapshot-vs-rebuild differentiation (e.g., high-water-mark
    caching per spec §3.3.6) without touching the L3 call sites.
- Next: Step 6 (L4 `tools.py` — registry + 8-state lifecycle +
  spawn_worker / verify_diff stubs).
