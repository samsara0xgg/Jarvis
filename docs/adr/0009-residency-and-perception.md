# ADR 0009 — Residency and Perception (launchd daemon · real sleep/wake · supervisor sweep · repo observer v0)

**Status:** Approved (2026-08-25; v2 — three-reviewer findings incorporated)
**Date:** 2026-08-11
**Supersedes:** the `_NSWorkspacePowerObserver` certified placeholder in `jarvis/deployment/sleep_wake.py:110-136` (reserved by ADR-0002 Step 16 as "wired in Step 17"); amends ADR-0003 D4's one-shot-CLI refusal contract (exit 2 → forward by default, `--no-forward` preserves the old behavior).
**Depends on:** ADR-0001 (Day-1 MVP), ADR-0002 (real-Codex flagship, Approved 2026-08-10 — incl. the Limitation-routing amendment and K7/K8 sleep/wake rows), ADR-0003 (Inherent text daemon: `serve_inherent`, daemon.lock, watcher/cursor pattern), ADR-0005 (voice surface — co-tenant of the serve process)
**Defers to future ADRs:** ADR-0010 (Drift Watch — reserved here; this ADR ships its food), scheduler / DeferredExecution (spec §3.7.9), FSEvents file watch · app sampler · screen observe, Inherent cockpit panel, full evidence-TTL ladder (spec §3.3.3)
**Number note:** 0004 (image staging), 0006 (streaming TTS / barge-in), 0007 (`surface.failed` + fallback chain), 0008 (real-time streaming) are pre-reserved by ADR-0003/0005 defer tables; this ADR takes the next free number and reserves 0010 for Drift Watch.

---

## 1. Context

### What exists today

- A real resident daemon exists: `python -m jarvis serve` → `bootstrap_runtime_app` + `serve_inherent` (uvicorn on 127.0.0.1:8006, `lifespan="off"` — the loop module owns all lifecycle), guarded by `~/.jarvis/daemon.lock` (`acquire_exclusive`: `fcntl.flock` + pid file + stale-pid recovery, `jarvis/deployment/process_lock.py:191-224`). **But nothing starts it.** No launchd plist, LaunchAgent, or supervisor exists anywhere in the repo; "安静常驻" is a manual terminal habit.
- The macOS power observer is a certified placeholder: `_NSWorkspacePowerObserver.register()` (`jarvis/deployment/sleep_wake.py:110-136`) imports `Foundation` only to prove PyObjC *could* work, stores callbacks, and never subscribes to anything ("wired in Step 17"). PyObjC is **not** in `pyproject.toml`, so even that import raises at register time. `install_power_observer` is called only on the fork-detach one-shot child (`cli/__init__.py:206-213, 303-308`) — never in `serve_inherent`. Real sleep/wake cannot reach `reconcile_after_wake` in production; K7/K8 green comes from injected `simulate_sleep`/`simulate_wake` stub factories.
- `reconcile_after_wake(event_log)` is done and idempotent (K8 live-green): it folds open actions and emits `worker.terminated_by_sleep` + `action.timeout_assumed` (`sleep_wake.py:252-307`); the Limitation claim derives **downstream**, when a decide() turn consumes the timeout event — which for orphans never happens today (see D4's turn-driving gap). Note the fold's precondition: `_in_progress_actions` (`sleep_wake.py:387-423`) requires `run.started` for its `run_id` and **skips actions that never reached it** — a blind spot D4 must remove for the sweep.
- No supervisor sweep exists. `result_expected_by` from the spec §3.4.8 ActionRequest skeleton is **not persisted anywhere** (`jarvis/shared/__init__.py:202-204` — "not yet wired"); the only timeout emitter is the Codex driver's own in-process 600s deadline (`execution/tools.py:135-137`, which names "the absent supervisor sweep" explicitly). Any action orphaned by a crash ghosts forever. `ActionLifecycle` is a per-process in-memory dict (`tools.py:312-325`) — a sweep must fold the event log, not query the FSM.
- Zero perception sources. The daemon reacts to exactly six trigger event types (`_RUNTIME_TRIGGER_TYPES` runtime/__init__.py:91-96 + `_USER_INTENT_TRIGGER_TYPES` inherent_loop.py:362-365); no git watcher, no Status Board projection, no `project.commit_seen` emitter. Context restore covers only tasks that went through jarvis itself.
- One-shot CLI: `python -m jarvis "<utterance>"` refuses with exit 2 while the daemon holds the lock ("jarvis: daemon running at pid {pid}; stop it or POST to http://127.0.0.1:8006/inherent/submit", `cli/__init__.py:456-464`). Once the daemon is always-resident, this refusal is permanent — the CLI dies as a surface unless it learns to forward.

### Why this ADR

Spec §1 opens with the identity sentence: "Jarvis 是 Allen 的私人 AI 运行时：**安静常驻**，持续维护 Allen 的工作、任务、agent、Mac 和房间状态". The north star (§1.4: "让你继续前进时几乎不需要重新装载上下文") requires jarvis to *see* work that happens outside its own turns. Today neither is true: the runtime is on-demand, and blind. This ADR makes the daemon a fact (launchd), makes sleep/wake real (the L6 "核心现实问题", §3.7.8), closes the ghost-action hole the spec marks high-ROI (§3.4.15 高 ROI list: "Action lifecycle + timeout"; mandate in §3.4.8), and lands the first perception source (repo observer → Status Board), which is the minimum for "接着昨天那个继续" to work on tasks jarvis did not itself run.

### Spec touchpoints

- §1 定位 / §3.2.5 user contract — "安静优先：默认不打扰；主动开口必须有 evidence 和 reason" → residency must be silent; new perception events must NOT trigger decision turns (§3.4.1 lists the trigger set; routine observations fold silently) and system-triggered turns must not speak (D4's channel-aware streaming).
- §16.1 deployment topology — "MAC NODE ｜ Mac, when awake, **launchd**" — launchd is the spec-named process manager; the Mac node is "when awake", not 24/7 (contrast: HOME NODE "RPi5, 24/7, systemd").
- §3.7.8 sleep/wake protocol — before-sleep is best-effort ("Sleep hook 不可靠"), wake side is mandatory: "Wake 后必须 emit mac.awake(slept_for_ms)，执行 mandatory reconciliation scan"; all dispatched/running actions spanning the window without terminal events must close via `worker.terminated_by_sleep` / `action.timeout_assumed` + Limitation Claim.
- §3.4.8 — "Scheduler / supervisor 必须扫 open actions，超过 result_expected_by 且无 terminal event 时 emit timeout/limitation."
- §2.1 / §3.6.1 — "Mac observer" is a canonical **L5 input adapter** (INPUTS row of the overview diagram); observation ingestion is the raw → adapter buffer → canonical event ladder, emitting "only when event-worthy".
- §3.3.1 — "所有持久变化都必须通过 event 进入系统：… git observation …. No state without event."
- §3.3.2 / §6 — Status Board projection ("Mac / home / action / tool / domain availability 的当前状态"), canonical fold sources `action.dispatched · action.result_observed · action.failed · domain_availability.changed · domain_projection.stale`; Drift Watch folds `project.commit_seen · task.* · observation tagged drift` (deferred to ADR-0010, but this ADR emits its food).
- §3.6.9 — surface freshness UX exemplar: "Git state: refreshed just now / stale since wake."
- §3.5.2 / §5.1 — perception runs read-only under the `observer` principal ("read-only observation；不写外部世界"); `actor` is an L5 provenance convention (see D5 — the L2 schema has no actor column; it rides in payload). Periodic system watchers match the `background_subscriber` profile.
- §5.4 — "未注册的 event type 不能进 emit"; new observation types must be registered with `evidence_semantics=observation`.
- §3.3.9 — "Event payload must be bounded"; no diffs inline; string fields capped (D5).

### Non-spec context

- `.importlinter` 6-layer contract plus the canary layer: `tests/canary/test_layer_ownership_boundaries.py` allows `deployment → jarvis.state.event_log` **only** for the file-scoped exception `jarvis/deployment/sleep_wake.py` (H13), and `tests/canary/test_no_hardcoded_runtime_root.py` (H8) forbids the literal `~/.jarvis` outside `jarvis/deployment/`. These two canaries dictate module placement (see §10 module map): the sweep lives inside `sleep_wake.py`; launchd paths/templates live in a new `jarvis/deployment/launchd.py`.
- `runtime.conn` is event-loop-thread-only; watcher tasks poll it there; thread-dispatched work opens fresh connections (precedent `_drive_turn_in_worker_thread`, inherent_loop.py:316-348).
- Doc drift to fix while here: `serve_inherent` docstring claims the lock file is `jarvis.lock`; the actual name is `daemon.lock`.

---

## 2. Scope

Four pillars, one ADR, because they are one dependency chain: launchd makes the process exist; sleep/wake + sweep make its lifecycle honest; the observer gives it eyes; the Status Board makes what it sees answerable. Splitting them would ship a daemon that lies about liveness or an observer with nothing to run it.

**In scope:** P1 launchd residency + CLI forward · P2 real sleep/wake in serve · P3 supervisor sweep + persisted deadlines + system-trigger turns · P4 repo observer v0 + Status Board projection + packet exposure.

**Out of scope** (§12 defer table): every other perception source, Drift Watch (ADR-0010), scheduler/reminders, cockpit rendering, RPi/home domain, full TTL ladder.

---

## 3. Decision

### D1. Residency: launchd LaunchAgent `com.allen.jarvis`, installed by the CLI

A user-domain LaunchAgent (gui/$UID), not a LaunchDaemon: the daemon needs Allen's GUI session (`say`, `osascript` banner, PortAudio wake device — the L5 set the Mac node owns, §3.7.2). Agent lifecycle is therefore **login-scoped** — with FileVault the daemon is down until Allen logs in, which is consistent with §16.1's "when awake"; `jarvis daemon install/status` detect a missing GUI session and say so instead of surfacing raw `launchctl` errno 5 (the SSH case). Plist pinned:

- `Label` com.allen.jarvis; `ProgramArguments` = [`<repo>/.venv/bin/python`, `-m`, `jarvis`, `serve`]; the install verb **validates** `[interpreter, "-c", "import jarvis"]` before writing the plist (mise/uv shims are not assumed on launchd's PATH; a pruned interpreter is caught at install and re-checked by `daemon status`). `WorkingDirectory` = repo root.
- `RunAtLoad` true; **`KeepAlive` = true** (respawn always; the off switch is `launchctl bootout` via `jarvis daemon uninstall`, which stops the job regardless of KeepAlive); `ThrottleInterval` 10. Rationale: uvicorn traps SIGTERM and exits 0, so a `KeepAlive={SuccessfulExit:false}` agent would stay down silently after any stray SIGTERM — the exact residency hole this ADR exists to close.
- `StandardOutPath`/`StandardErrorPath` → `~/.jarvis/logs/daemon.{out,err}.log` (dir created by install; path constants + plist template render live in `jarvis/deployment/launchd.py` — H8 keeps `~/.jarvis` literals inside deployment/). launchd never rotates these; `jarvis daemon status` warns above a size threshold and the DoD notes the optional `newsyslog.d` recipe.
- `EnvironmentVariables` = {`JARVIS_LAUNCHD_AGENT`: com.allen.jarvis} — the marker that lets the spawned daemon recognise itself (see the manual-serve bullet below). Amended after the M1 burn: without it the guard below fires on launchd's **own** child, which runs this exact `ProgramArguments`, and `KeepAlive` turns that refusal into a respawn loop every `ThrottleInterval` (observed: `state = spawn scheduled`, `runs` climbing, three identical refusals in `daemon.err.log`, daemon never up). The label is public, so the no-secrets rule below is untouched.
- **No secrets in the plist** (0644 in ~/Library/LaunchAgents). Bootstrap gains an optional env-file loader: `${runtime_root}/env` (0600, `KEY=VALUE` lines) loaded **fill-only** (never overrides existing env) before surface preflights, so `MINIMAX_API_KEY` etc. survive the launchd context. Keying by runtime_root (not a hardcoded `~/.jarvis/env`) keeps temp-root test daemons naturally isolated from Allen's real keys — a test bootstrap must never flip itself to live TTS. Missing file → existing degradation (voice preflight drops to text-only, unchanged).
- CLI verbs (thin callers over `deployment/launchd.py`): `jarvis daemon install` (validate interpreter → render plist → `launchctl bootout` if loaded → `bootstrap gui/$UID` → `enable`; idempotent), `jarvis daemon uninstall` (bootout + optionally remove plist), `jarvis daemon status` (`launchctl print` summary incl. last exit status + lock probe + pid + interpreter/log-size checks).
- Manual `jarvis serve` while the agent is installed: detected via the plist's presence → warn ("LaunchAgent installed; manual serve will fight respawn — run `jarvis daemon uninstall` first"), proceed only with `--force-manual`. The plist's presence cannot by itself tell a human's `serve` apart from launchd's child — both see the same file — so the guard additionally skips when `JARVIS_LAUNCHD_AGENT` names this label. The marker lives in the **environment**, not in `ProgramArguments`, so a human who copies the argv out of the plist still gets the warning.
- `daemon.lock` needs no changes: flock dies with the process, stale-pid recovery already handles kill -9 → respawn cycles.

Rejected alternatives — **LaunchDaemon**: no GUI session → `say`/banner/mic dead. **Login Items app wrapper**: no KeepAlive, no throttled respawn, adds an app bundle for zero gain. **`KeepAlive={SuccessfulExit:false}`**: see rationale above — SIGTERM exits 0 and kills residency silently. **Keep manual `serve`**: contradicts §1 "安静常驻" and makes every watcher below worthless after a reboot.

### D2. One-shot CLI forwards to the daemon instead of refusing

When `daemon.lock` is held **or the LaunchAgent is installed**, `python -m jarvis "<utterance>"` becomes a thin client. The agent-installed condition matters: it also **disables fork-detach** — during a crash/respawn window (`ThrottleInterval` ≈ 10s) the CLI must not spawn a detached child that races the respawned daemon's sweep and watchers (see D4 cross-process note); it retries or refuses with a "daemon restarting" hint instead.

Wire contract (this is a **wire change**, flagged for inherent-swift compatibility):

- `POST /inherent/submit` response gains `turn_id`: `{"status": "accepted", "turn_id": "..."}` (additive; `submit_callable` already mints it internally and currently discards it — inherent_loop.py:928-946).
- The CLI connects the existing WS stream **before** POSTing (there is no late-subscriber replay — ADR-0003 explicitly deferred the replay queue), buffers envelopes client-side, then POSTs, then filters the buffer + live stream by the returned `turn_id`, prints the response text, exit 0.
- Contract table: POST connection-refused (lock held but uvicorn not yet bound — `acquire_exclusive` runs before bind, voice preflight in between) → bounded retry 3×1s → exit 3 with "daemon starting or hung; try again / jarvis daemon status". Response wait: default 120s, `--timeout` override → on expiry exit 4 printing the `turn_id` for later inspection (the turn keeps running daemon-side). `--no-forward` → old exit-2 refusal verbatim (scripts). The B-NEW-5 second guard (non-default `--runtime-root` while default root's daemon runs) keeps refusing — forwarding across roots would answer from the wrong event log.
- Deliberate UX change, stated: a forwarded long-run "跑 X" waits for the real response (or times out with the turn_id) instead of the fork-detach path's instant "好的，跑起来了" ack. The fork-detach path remains for the daemon-not-installed context and is otherwise untouched.

Rejected alternatives — **keep the refusal**: with an always-on daemon the CLI dies permanently as a surface. **Second in-process runtime**: violates ADR-0003 D4 single-writer by design. **Reply-over-HTTP long-poll endpoint**: a second wire for the same payload the WS already carries.

### D3. Real sleep/wake: IOKit power notifications on a dedicated CFRunLoop thread

Mechanism pinned: `IORegisterForSystemPower` via **ctypes against IOKit + CoreFoundation** — PyObjC does not wrap IOKit (`pyobjc-framework-IOKit` does not exist; `pyobjc-framework-Cocoa` ships Foundation/AppKit only), so ctypes is the primary path and needs no new dependency at all. The full surface is ~7 calls (`IORegisterForSystemPower`, `IONotificationPortGetRunLoopSource`, `CFRunLoopAddSource`, `CFRunLoopRun`/`CFRunLoopStop`, `IOAllowPowerChange`, `IODeregisterForSystemPower`, `IONotificationPortDestroy`) plus a `CFUNCTYPE` callback whose Python wrapper is **kept referenced for the daemon's lifetime** (GC while registered = crash; a unit test pins the reference). `pyobjc-framework-Cocoa` becomes a dependency **only if** the Step-0 spike selects the NSWorkspace fallback.

- **Step 0 is a spike** with two equal candidates — ctypes-IOKit vs NSWorkspace-notifications-on-NSRunLoop-thread — proving callbacks fire across `pmset sleepnow` + scheduled wake in a plain daemon process (no NSApplication). The winner is recorded in progress.md and this section is amended if it is not ctypes-IOKit; last-resort fallback is `pmset -g log` polling (amendment required, never a silent swap).
- `sleep_wake.register()` gains the real subscription; the `observer_factory` injection seam is **preserved byte-for-byte** — K7/K8 stub tests keep passing unchanged.
- **Before-sleep ack protocol** (kIOMessageSystemWillSleep): the CFRunLoop thread schedules the best-effort emit (`mac.sleeping(reason, in_progress_actions)` + WAL flush) onto the asyncio loop via `call_soon_threadsafe`, waits on a `threading.Event` with a **3s hard bound**, then calls `IOAllowPowerChange` **unconditionally** (never veto, never exceed the OS's ack window). A missed bound (loop busy in a long turn) degrades to the F3 wake-side path — per §3.7.8 "Sleep hook 不可靠", correctness never depends on before-sleep. `kIOMessageCanSystemSleep` is allowed immediately.
- On-wake: emit `mac.awake(slept_for_ms)` + run `reconcile_after_wake` (existing, idempotent), marshaled the same way. `slept_for_ms` source: `sysctl kern.sleeptime`/`kern.waketime` (kernel-stamped wall-clock), fallback = wall-clock delta against the last `mac.sleeping` row; IOKit itself delivers no timestamps.
- **Teardown pinned**: `shutdown()` = set a closed-flag consulted at callback entry → `IODeregisterForSystemPower` → `CFRunLoopStop` → `thread.join(timeout)`, completed inside `serve_inherent`'s `finally` **before** the loop and `runtime.conn` close (a notification firing between watcher-cancel and process exit must find the closed-flag, not a dead loop). Unit test: fire the stub observer after `shutdown()`, assert no emit.
- Wired in `serve_inherent` after lock acquisition; the fork-detach child keeps its existing install (unchanged).
- Degradation (F4): IOKit unavailable → one warning, daemon runs without power observer; orphan closure falls to the bootstrap sweep (D4) on next restart.

### D4. Supervisor sweep: fold-the-log, emit `action.timeout_assumed`, let the existing machinery do the rest

- **Deadline persistence first**: `action.dispatched` payload gains optional `result_expected_by_ms` (registry change, additive optional field, schema_version stays 1), stamped by the dispatcher from the per-tool budget (Codex: 600s + grace). Fallback anchor for legacy rows lacking the field: the action's `action.dispatched` ts + `supervisor.default_budget_s` (700s); for pre-dispatch-era rows, first-seen `action.running` ts.
- **Fold**: one shared low-level fold inside `sleep_wake.py` (H13 keeps the `deployment → state.event_log` exception file-scoped), parameterized where the wake path and the sweep differ: the sweep **includes actions that never reached `run.started`** (the daemon-died-during-preflight window `_in_progress_actions` deliberately skips) and reads deadlines from `action.dispatched`; it emits **only** `action.timeout_assumed`, for run-less and run-ful orphans alike — never a `worker.*` event. (Clarified at Step 6, where the original wording read as implying run-ful orphans get something worker-shaped: a run-less orphan has no run to terminate, and §4 registers no supervisor-cause `worker.*` type, so a run-ful orphan has none that would be true either — reusing `worker.terminated_by_sleep` would stamp a false cause on an append-only log. M4's acceptance row likewise names only `action.timeout_assumed(reason=supervisor_sweep…)`. Adding `worker.terminated_by_supervisor` would be a registry amendment, not an implementation choice.) The sweep's SELECT is scoped by the ~8 action/run event types (watcher IN-list pattern), not `iter_events` over the whole log — this fold runs every 30s forever, on a log this same ADR makes grow per-minute.
- **Active-turn exclusion, defined**: a runtime-owned thread-safe set of live `action_id`s — registered by the dispatch path, removed in a `finally` that runs even when `drive_turn` raises (a crashed turn must not pin its actions "active" forever; the canary covers exactly this). Runtime wires the set into both the sweep task and `_system_trigger_watcher` — no `deployment → execution` import.
- **Double-consumption excluded, both directions** (this is the load-bearing part):
  - *Emission*: sweep skips active-set action_ids; immediately before emitting it **re-checks** for a terminal event on that action_id (cross-process safety — a detached child from the pre-install era, or a race with the in-turn driver, may have just closed it); terminal-dedup is the same check `reconcile_after_wake` uses.
  - *Consumption*: today `_wait_for_next_trigger` selects by TYPE+id only and would hand a foreign orphan's `action.timeout_assumed` to an unrelated in-flight turn, which would adopt its correlation and end the wrong turn with the wrong limitation. Pinned fix: the in-turn waiter's SELECT is **scoped to the turn's own action_id set** (correlation filter), and `_system_trigger_watcher` handles only events whose action_id is NOT in the live set. Canary: an in-flight waiter never returns a foreign action's terminal event.
- **Sweep task + bootstrap sweep, ordering pinned**: on serve start, snapshot `MAX(events.id)` FIRST and hand it to `_system_trigger_watcher` as its cursor anchor (watchers signal "anchored" via `asyncio.Event`), THEN run the one-shot bootstrap sweep — otherwise the watcher's own MAX(id) anchor swallows the bootstrap emissions and orphan closure never drives a turn. Periodic task joins the `serve_inherent` watchers list (interval `supervisor.sweep_interval_s`, 30s).
- **Turn-driving**: `_system_trigger_watcher` (same cursor pattern as `_user_intent_watcher`) starts a system-triggered decide() turn for orphan terminal events. The Result Interpreter + `_handle_action_terminal_failure` branch already produce the Limitation claim + limitation utterance, and the ADR-0002 amendment pins the channel to `queue_review`. The sweep is an emitter, not a second brain.
- **System turns are silent, enforced**: the daemon streams every turn (`streaming_enabled=True`) and `_tts_watcher` currently feeds **every** response chunk to TTS regardless of channel — a 3am orphan closure would speak. Pinned: `surface.response_open` gains optional `attention_channel` (registry change); `_tts_watcher` skips `queue_review`/`silent_log` turns, and the WS broadcaster skips `silent_log` only. (Corrected at Step 8 — the original text had the broadcaster skip both, which is wrong. `attention_policy`'s final `return` makes `queue_review` the **default** verdict for an ordinary user utterance, and `ATTENTION_CHANNEL_TO_SURFACES` maps it to `("cli_stdout",)` — a text surface — while `silent_log` maps to `()`. The daemon renders with `available_surfaces=frozenset()`, so the WS *is* that `cli_stdout`. Skipping `queue_review` on the wire would blank the Inherent text surface for nearly every turn and hang D2's forwarding CLI to its 120s timeout on the happy path. "Silent" here means no audio, not no text: a queued turn still lands on the card, which is what "queue for review" means.) Canary: `test_canary_system_turns_never_tts`. (This closes the gap for ALL queue_review turns, aligning L5 streaming with the ADR-0002 routing amendment.)

Rejected alternatives — **query ActionLifecycle FSM**: per-process memory, empty after every restart. **Sweep emits Limitation directly**: duplicates the Result Interpreter's ladder and violates emit-ownership (§5.4.2 precedent). **Reusing `_in_progress_actions` unchanged**: inherits the run.started blind spot; see fold bullet.

### D5. Repo observer v0: an L5 input adapter under the `observer` principal

- Module `jarvis/surface/repo_observer.py` (L5 per §2.1 — "Mac observer" sits in the INPUTS/Surface row). Wiring in `runtime/inherent_loop.py` as one more watchers-list task; emits on the loop thread over `runtime.conn` (single-writer preserved); snapshot subprocess work via `asyncio.to_thread`.
- Pure snapshot function per repo (config `observer.repos`): local-only `git` reads — current branch (`rev-parse --abbrev-ref HEAD`; detached HEAD yields the literal `"HEAD"`, payload contract is total), HEAD sha, dirty file count (`status --porcelain` line count), last commit subject + sha. **Every invocation runs with `GIT_OPTIONAL_LOCKS=0`** — plain `git status` opportunistically writes `.git/index.lock` (the classic watcher-vs-user bug: Allen's own `git commit` dies with "Unable to create index.lock" when the poll fires), and the spec row this section cites ("read-only observation；不写外部世界") forbids exactly that. Never `fetch`/network, never write; 5s subprocess timeout, failure → log + skip cycle (F5; deleted/moved repo path → F6, same skip).
- §3.6.1 ladder compliance: poll every `observer.poll_interval_s` (default 60s); **emit only on change**. The change-baseline is **recovered from the event log at startup** (fold the latest `repo.state_observed` per repo) — the in-memory buffer is a cache, not the source of truth. First poll after a restart therefore emits exactly the delta accumulated while the daemon was down (commit walk `old_head..HEAD`, burst-capped) — no spurious restart emissions, and no permanent holes in `project.commit_seen` history across the overnight window that is this ADR's headline use case.
- Two new registered event types (both `evidence_semantics=observation`, owner_layer=L5, bounded payloads — subject fields capped at 200 chars, no artifacts in v0). Provenance: the L2 schema has **no actor column** — `actor: "observer"` is a required **payload** field of both types (the §5.1 actor enumeration realized as a payload convention, as `payload.by` already is for `task.verified`):

  > **Amendment (2026-08-25) — the L2 schema now HAS an actor column.**
  > The §5.1 schema backfill (L2 schema v1) added `actor` as a real
  > column, stamped by `emit_event` from the registry's per-type actor
  > value; the migration backfilled historical rows from this payload
  > convention first, then from the registry. The column is authoritative
  > from v1 on. The payload `actor` field on `repo.state_observed` /
  > `project.commit_seen` REMAINS required — existing consumers and this
  > ADR's contract stay valid — but new event types should rely on the
  > column and not replicate the payload convention.
  - `repo.state_observed` — required `repo_path, branch, head_sha, dirty_file_count, last_commit_subject, observed_at_ms, actor`. Emitted when any field changes. Net-new family (V1).
  - `project.commit_seen` — required `repo_path, commit_sha, subject, committed_at_ms, actor`; optional `truncated, skipped_count`. The spec-canonical name (§6 Drift Watch sources — its only occurrence; registered here per §5.4's own rule). One event per newly-seen first-parent commit since the last baseline, burst-capped at 20 per poll with `truncated: true` + `skipped_count` on the final event (rebase/history-walk safety). ADR-0010 note: a truncated window means gapped, not contiguous, history.
- **No decision triggers.** Neither type joins any trigger tuple; observations fold silently (§3.4.1; §3.2.5 安静优先). Existing watcher cursors filter by type-IN lists, so the new rows are ignored for free.

Rejected alternatives — **FSEvents push**: real-time file granularity nobody asked for, new dep, debounce complexity; poll-on-change at 60s matches §3.3.3's "git branch / file state 分钟级" class. **Observer as gated L4 ActionRequest per poll**: gate-evaluation noise × repos × 1440/day for read-only observation; the L5-adapter path (§3.2.4 "emit event") is the spec's shape for continuous inputs. **`git.observed` name**: the spec's only commit-observation vocabulary is `project.commit_seen`; inventing a parallel family violates single-source naming.

### D6. Status Board projection + packet exposure

- New frozen `StatusBoard` in `jarvis/state/projections.py`, folded in `rebuild_projections` (same full-refold pattern; Stage-2 incrementality stays deferred). Fold sources: `repo.state_observed` + `project.commit_seen` (latest per repo, `observed_at_ms` kept) · `mac.sleeping`/`mac.awake` (last transition) · `action.dispatched`/`action.result_observed`/`action.failed`/`action.timeout_assumed`/**`action.cancelled`** (open-action summary — cancelled is terminal in both the FSM and the sleep/wake fold; omitting it would leave phantom open actions) — the §6 canonical sources minus the not-yet-existing domain events (V2).
- `SituationPacket` gains additive `status_board` field (assembled in `assemble_packet`; packet rebuilt per invocation so freshness is automatic). Rendered into the LLM context as a system note mirroring `_format_open_tasks_note` — per-repo one-liner with **freshness age wording** per §3.6.9 ("refreshed 42s ago" / "stale since wake"), bounded by the payload caps (D5) plus a repos cap in the note itself. Stale rule v0: age > 3 × poll interval → prefix "stale"; the full per-domain TTL ladder (§3.3.3) is deferred (V4) — v0 never *hides* age, so honesty is preserved while the ladder is absent.
- This is the minimum L3-visible slice: cockpit rendering, Drift Watch judgment (ADR-0010), and packet-v2 fields beyond `status_board` stay out (§12).

### D7. Acceptance narrative (not a canonical event trace — this is an infra ADR)

1. Allen runs `jarvis daemon install` once. After next login, `launchctl print gui/$UID/com.allen.jarvis` shows it running; `daemon.lock` held by the launchd-spawned pid. `kill -9` it → respawned < 15s, stale lock recovered, still exactly one instance (M1/M2).
2. Allen commits twice in a watched repo from a plain terminal (jarvis uninvolved). Within a poll interval `project.commit_seen` ×2 + `repo.state_observed` land in `mac_events.db`; an unchanged next poll emits nothing (M5).
3. Allen asks "jarvis repo X 现在什么状态". The turn answers branch/HEAD/dirty-count with freshness wording straight from the Status Board note — no git subprocess runs inside the turn (M6).
4. Mac sleeps overnight (real lid-close): `mac.sleeping` best-effort, `mac.awake(slept_for_ms)` + reconciliation on wake, via the real power-notification path (M3, supervised).
5. A Codex action orphaned by a daemon crash is closed by the bootstrap sweep on respawn: `action.timeout_assumed(reason=supervisor_sweep…)` → system-triggered turn → Limitation claim → `queue_review` response, with zero TTS (M4) — the 2026-08-10 routing amendment completing the loop, silently.

---

## 4. Event Type Registry Changes

| Type | Owner | Producer | Payload (required unless noted) | evidence_semantics | Projection consumers |
|---|---|---|---|---|---|
| `repo.state_observed` (new) | L5 | repo observer | repo_path, branch, head_sha, dirty_file_count, last_commit_subject (≤200), observed_at_ms, actor | observation | Status Board |
| `project.commit_seen` (new) | L5 | repo observer | repo_path, commit_sha, subject (≤200), committed_at_ms, actor; optional: truncated, skipped_count | observation | Status Board; Drift Watch (ADR-0010) |
| `action.dispatched` (amended) | — | — | + optional `result_expected_by_ms` | — | (supervisor sweep reads) |
| `surface.response_open` (amended) | — | — | + optional `attention_channel` | — | (`_tts_watcher` / broadcaster channel filter, D4) |

`mac.sleeping` / `mac.awake` / `action.timeout_assumed` are already registered (K7/K8); no scheduler.* types are registered by this ADR (deferred with §3.7.9). Registry enforcement note (Step 2 verification honesty): `emit_event` validates **type registration + required-field presence**; unknown extra payload keys are accepted Day-1 (spec §5.4 strict mode deferred — pre-existing contract, unchanged here).

---

## 5. Failure Modes

| # | Trigger | Detection | Behavior | Note |
|---|---|---|---|---|
| F1 | daemon crash loop | launchd ThrottleInterval + `daemon.err.log` | respawn ≥10s apart; KeepAlive keeps trying | `jarvis daemon status` surfaces last exit |
| F2 | sleep during active Codex turn | real before-sleep path (3s ack bound) | existing K7 machinery (`worker.suspended_by_sleep` attempt → wake reconciliation) on real events | bound missed → F3 |
| F3 | before-sleep hook never ran (force sleep, battery death, SIGKILL, missed 3s bound) | wake reconciliation + bootstrap sweep find orphans | orphans closed per §3.7.8 ("Sleep hook 不可靠" — wake side is the mandatory half) | correctness never depends on F2's path |
| F4 | IOKit path unavailable | register() failure at serve start | one warning, daemon runs without power observer; bootstrap sweep covers orphan closure on restart | fail-open residency |
| F5 | observer `git` subprocess hangs/fails | 5s timeout / non-zero exit | log + skip this repo this cycle; no event emitted | `GIT_OPTIONAL_LOCKS=0` on every call |
| F6 | watched repo path deleted/moved | snapshot fn error | same skip as F5; config is Allen-managed | |
| F7 | sweep vs in-flight driver / detached child double-emission | active-set skip + pre-emit terminal re-check + terminal dedup | at most one `action.timeout_assumed` per action_id | canary incl. crashed-turn case |
| F8 | clock jump across sleep | intervals on `time.monotonic` (pauses during sleep — fine for intervals), `slept_for_ms` from kern.sleeptime/waketime sysctls | debounce immune; sleep duration kernel-stamped | wall-clock delta vs `mac.sleeping` as fallback |
| F9 | foreign terminal event during live turn | waiter scoped to own action_id set | in-flight turn never adopts an orphan's timeout | canary-pinned (D4) |

---

## 6. Spec Deviations Declared

| # | Spec section | Deviation | Reason |
|---|---|---|---|
| V1 | §5.2/§5.4 | net-new `repo.*` event family (spec names no Mac-observation family) | §3.3.1 mandates "git observation" enter as events; registry is designed to grow via §5.4 entries |
| V2 | §6 Status Board row | fold sources extended with `repo.state_observed`/`project.commit_seen`/`mac.*`/`action.timeout_assumed`/`action.cancelled`; `domain_availability.changed`/`domain_projection.stale` omitted (not yet registered, no home domain) | Mac-only scope; extension recorded, not silent |
| V3 | §16.1 | launchd named, mechanics unspecified — plist details are this ADR's choice | spec delegates deployment detail to ADRs |
| V4 | §3.3.3 | stale rule v0 = 3× poll interval, not the per-domain TTL ladder | age always displayed; full ladder deferred to its own slice |
| V5 | §3.7.8 | M3 (real pmset sleep/wake burn) is a supervised manual smoke, not automated pytest | machine sleeps mid-test; logistics, not fidelity |
| V6 | §5.1 | `actor` realized as a required payload field on the two new types, not an L2 schema column | no-migration additive path; §5.1's actor enumeration honored at the payload level |

---

## 7. Build Order

14 steps: Step 0 is a spike note (docs commit), Steps 1–13 are code commits. Tier-1 green at every step.

| Step | What | References | Verification |
|---|---|---|---|
| 0 | Spike: power-notification proof script — ctypes-IOKit vs NSWorkspace-on-NSRunLoop, result → progress.md | D3 · spec §3.7.8 | manual: callbacks fire across `pmset sleepnow` + scheduled wake, both candidates scored |
| 1 | env-file loader `${runtime_root}/env` (fill-only) in bootstrap | D1 | unit: parsed, fill-only, absent = no-op, temp-root isolation |
| 2 | registry: `repo.state_observed` + `project.commit_seen` + `action.dispatched.result_expected_by_ms` + `surface.response_open.attention_channel` | §4 · spec §5.4 | unit: new types registered w/ required fields incl. actor; amended optionals accepted |
| 3 | `sleep_wake`: real power observer per spike (callback-lifetime ref, ack protocol, teardown incl. closed-flag) | D3 · spec §3.7.8 · F2-F4/F8 | unit (stub factory): K7/K8 unchanged-green; `test_canary_power_observer_seam`; post-shutdown fire = no emit |
| 4 | serve wiring: `install_power_observer` + teardown-before-close in `serve_inherent`; docstring drift fix | D3 · F2/F3 | unit: install/teardown ordering in loop lifecycle |
| 5 | dispatcher stamps `result_expected_by_ms`; runtime live action-id set (register/finally-remove) | D4 | unit: payload present on Codex path; set emptied on turn crash |
| 6 | sweep fold + emit inside `sleep_wake.py` (run-less orphans, dispatched-ts deadlines, pre-emit terminal re-check, typed SELECT) | D4 · spec §3.4.8 · F7 | unit: run.started-less window closed; dedup; legacy fallback anchors |
| 7 | waiter action-id scoping + `_system_trigger_watcher` + anchor-then-bootstrap-sweep ordering + periodic task | D4 · F7/F9 | unit: seeded orphan → serve start → system turn fires; `test_canary_sweep_skips_active_turn`; waiter-foreign-event canary |
| 8 | channel-aware streaming: `attention_channel` on response_open; `_tts_watcher`/broadcaster skip queue_review/silent_log | D4 · ADR-0002 amendment | unit + `test_canary_system_turns_never_tts` |
| 9 | `repo_observer.py`: snapshot fn (`GIT_OPTIONAL_LOCKS=0`, detached-HEAD, caps) + log-recovered baseline + burst cap | D5 · spec §3.6.1 · F5/F6 | unit: debounce, restart-delta, cap+truncated, timeout path; `test_canary_observer_never_triggers_decide` |
| 10 | observer task wiring + `observer.repos` config | D5 | unit: config parsing, multi-repo loop, to_thread isolation |
| 11 | `StatusBoard` projection + `ProjectionSet` + packet field + system note render | D6 · spec §6/§3.6.9 | unit: fold incl. cancelled, freshness wording, stale-at-3×, note caps |
| 12 | CLI forward mode (WS-before-POST, turn_id wire change, contract table, agent-installed fork-detach disable) | D2 · F-table n/a | unit: forward mocked; exit 2/3/4 paths; B-NEW-5 preserved; no-fork-when-installed |
| 13 | `deployment/launchd.py` + `jarvis daemon install/uninstall/status` + plist template + logs dir | D1 · spec §16.1 | unit: plist golden-file; interpreter validation; idempotent re-install; H8 canary green |
| 14 | M-family live tests + DoD manual smokes + progress.md | §8 | `--live-daemon` suite green |

---

## 8. Acceptance

### Tier 1 (LLM-free, every step)

The four standing gates (see DoD 1–4) plus new canaries, each assigned to its build step above: `test_canary_power_observer_seam` (Step 3), `test_canary_sweep_skips_active_turn` + waiter-foreign-event canary (Step 7), `test_canary_system_turns_never_tts` (Step 8), `test_canary_observer_never_triggers_decide` (Step 9).

### Tier 2 M — Residency & perception (live)

Gated by `--live-daemon` (spawns a real `serve` subprocess on a temp runtime root; no launchd needed except M1/M2).

| ID | Invariant |
|---|---|
| M1* | after login, `launchctl print` shows com.allen.jarvis running; lock held by launchd pid |
| M2* | `kill -9` daemon → respawn < 15s; stale lock recovered; exactly one instance |
| M3* | real `pmset sleepnow` (+ scheduled wake): `mac.sleeping` best-effort, `mac.awake(slept_for_ms>0)`, reconciliation rows — real notification path, zero simulate stubs |
| M4 | seeded orphan (incl. a run.started-less one) + daemon start → bootstrap sweep closes both: `action.timeout_assumed(reason=supervisor_sweep…)` + Limitation claim + `queue_review` response + **no TTS events** |
| M5 | external commit in watched repo → `project.commit_seen` + `repo.state_observed` ≤ poll interval; unchanged next poll emits nothing; daemon restart emits nothing for unchanged repo |
| M6 | utterance "repo X 现在什么状态" → response carries branch/HEAD/dirty + freshness wording; no git subprocess inside the turn |
| M7 | nonexistent repo in config → skip + log; daemon healthy |
| M8 | one-shot CLI while daemon runs → forwarded response, exit 0; `--no-forward` → exit 2; daemon stopped + agent installed → no fork-detach, retry hint |

\* M1–M3 are DoD supervised manual smokes; M4–M8 are pytest.

### Definition of Done

1. `lint-imports` reports KEPT (1/1). Layer DAG unbroken.
2. `ruff check` clean across all changed files.
3. `mypy --strict .` clean (full repo).
4. `pytest -q tests/unit tests/scenarios` green; wall < 30s (unit suite budget per CLAUDE.md; the live M-suite is outside this budget).
5. M4–M8 green under `--live-daemon`.
6. Manual smoke, Allen-supervised, exact commands (machine sleeps ~2 min in M3):
   - M1: `jarvis daemon install` → log out/in (or reboot + login) → `launchctl print gui/$(id -u)/com.allen.jarvis | grep state` shows `running`; `cat ~/.jarvis/daemon.lock` pid matches.
   - M2: `kill -9 $(cat ~/.jarvis/daemon.lock)` → within 15s `launchctl print …` shows a new pid; `pgrep -f "jarvis serve" | wc -l` prints 1.
   - M3: `sudo pmset schedule wake "$(date -v+2M '+%m/%d/%y %H:%M:%S')" && pmset sleepnow` → after wake, `sqlite3 ~/.jarvis/mac_events.db "SELECT type FROM events ORDER BY id DESC LIMIT 20"` contains `mac.awake` (and `mac.sleeping` unless the 3s bound was missed), with `slept_for_ms > 0` in the payload.
7. Spec self-review pass: placeholder scan, cite audit, defer-table cross-check against ADR-0003/0005 reservations.
8. Allen flips Status → Approved.

---

## 9. Consequences

### Tier-1 gate budget impact

Est. net new prod ~1000 LOC (power observer ~300 with callback-lifetime + ack protocol, sweep + watcher ~200, repo_observer ~150, StatusBoard + note ~130, launchd.py + CLI verbs ~180, CLI forward ~120, env loader ~40) + test ~1200 LOC. **Zero new runtime dependencies on the primary path** (ctypes is stdlib); `pyobjc-framework-Cocoa` only if the spike picks NSWorkspace (recorded by amendment). Wall-clock: unit suite stays < 30s (no live calls in Tier-1).

### Canary impact

Plausibly-affected set, with verdicts:
- `test_layer_ownership_boundaries` (H13): **affected-avoided** — the sweep fold lives inside `sleep_wake.py`, the only deployment file whose `jarvis.state.event_log` import is excepted; no new exception rows.
- `test_no_hardcoded_runtime_root` (H8): **affected-avoided** — all `~/.jarvis` literals (plist paths, logs dir) live in `jarvis/deployment/launchd.py`; CLI verbs are thin callers.
- `test_canary_runtime_trigger_types`: **not affected** — no trigger tuple gains a type (the waiter gains a correlation *filter*, not a type; observer events trigger nothing).
- `test_canary_daemon_ack_before_fork`: **affected** — D2 disables fork-detach when the agent is installed; the canary's precondition (fork path reachable) must gain the not-installed guard. Exact edit at Step 12.
- Full sweep of the remaining canaries executes at Steps 4/7/12 per house rule.

### Spec deviations declared

V1–V6 (§6 above).

### Open against the user contract

M3 requires Allen-supervised machine sleep; `${runtime_root}/env` introduces a file Allen creates once for voice keys under launchd; a forwarded long-run CLI call waits (or times out printing turn_id) instead of the instant fork-detach ack. Otherwise none — Allen remains sole operator; quiet-by-default is strengthened (observers never speak; system turns are TTS-filtered by canary).

---

## 10. Module Map

| Path | New/Mod | Responsibility | ~LOC |
|---|---|---|---|
| `jarvis/deployment/sleep_wake.py` | mod | real power observer (spike winner) + shared open-action fold + sweep emit | +450 |
| `jarvis/deployment/launchd.py` | new | plist template render, path constants (`~/.jarvis` literals live here — H8), install/uninstall/status logic | ~180 |
| `jarvis/surface/repo_observer.py` | new | snapshot fn + baseline recovery + burst cap + emit | ~150 |
| `jarvis/state/projections.py` | mod | `StatusBoard` fold + `ProjectionSet` field | +130 |
| `jarvis/state/event_log.py` | mod | 2 new + 2 amended registry entries | +40 |
| `jarvis/decision/packet.py` | mod | `status_board` field + note render | +40 |
| `jarvis/runtime/__init__.py` | mod | waiter action-id scoping; live action-id set | +60 |
| `jarvis/runtime/inherent_loop.py` | mod | observer/sweep/system-trigger watcher wiring, anchor ordering, TTS channel filter | +120 |
| `jarvis/surface/inherent_server.py` | mod | submit returns turn_id | +10 |
| `jarvis/surface/cli_render.py` | mod | attention_channel on response_open | +10 |
| `jarvis/cli/__init__.py` | mod | forward mode, daemon verbs (thin), fork-detach install guard | +150 |
| `jarvis/execution/tools.py` | mod | result_expected_by_ms stamp; action-id set hooks | +30 |

Layer edges (all pre-existing patterns): `deployment → state.event_log` (H13 exception file only) · `surface → state` (existing emit path) · `cli → deployment.launchd` (new, same-direction as existing `cli → deployment.process_lock`) · all cross-layer wiring in `runtime/`.

## 11. Legacy Audit

`jarvis-legacy` has no launchd plist (only app-bundle Info.plists), no sleep/wake code (zero hits for pmset/IORegisterForSystemPower/NSWorkspaceWillSleep), no git-observation code (zero hits for `status --porcelain` / `rev-parse HEAD`). Nothing to port. Pre-written commit trailers:
- Legacy-bypass: `jarvis-legacy/core/health.py` — per-process circuit breaker; conflicts with event-log-fold liveness (sweep derives truth from L2, not process state).
- Legacy-bypass: `jarvis-legacy/core/scheduler.py` — APScheduler wrapper; deferred with the scheduler ADR (§3.7.9), not this one.
- Legacy consulted: `jarvis-legacy/deploy/jarvis.service` (systemd unit — the RPi-node analogue of D1's plist; KeepAlive/Restart semantics comparison only).

## 12. Out of Scope

| Item | Why deferred | Successor |
|---|---|---|
| Drift Watch projection ("我是不是跑偏了") | needs `project.commit_seen` history + judgment design | **ADR-0010** (reserved) |
| Scheduler / DeferredExecution / reminders | §3.7.9 record shape + cross-sleep fire semantics is its own design | separate ADR |
| FSEvents file watch, app sampler, screen/clipboard observe | each is a new adapter + noise-ladder design | future perception ADRs |
| Inherent cockpit panel (rendering Status Board) | surface work; the fold ships first | cockpit ADR |
| Full evidence-TTL ladder + `stale_warning` | §3.3.3 per-domain model; v0 shows age honestly | evidence-freshness slice |
| `domain_availability.changed` / `domain_projection.stale` emission | no second domain on Mac-only scope | RPi ADR (standing defer) |
| Memory write path, SituationPacket v2 beyond `status_board` | separate roadmap batches | their own ADRs |
| WS replay queue for late subscribers | D2 works around it via WS-before-POST | ADR-0003's original defer row stands |

## 13. References

- spec.html §1, §1.4, §2.1, §3.2.4/§3.2.5, §3.3.1–§3.3.3, §3.3.9, §3.4.1, §3.4.8, §3.4.15, §3.5.2, §3.6.1, §3.6.9, §3.7.2, §3.7.8, §3.7.9, §5.1, §5.2, §5.4, §6, §16.1
- ADR-0002 — Limitation-routing amendment (2026-08-10): the `queue_review` channel M4 completes; K7/K8 sleep/wake acceptance rows; Step 16's power-observer reservation
- ADR-0003 — D4 single-writer daemon.lock (+ the CLI refusal this ADR amends); watcher/cursor pattern; WS replay defer row; number reservations (0004/0006/0007/0008)
- ADR-0005 — serve process co-tenancy (voice stack shares the daemon this ADR makes resident); front-matter style precedent
- Code anchors as cited inline (verified against `a793784`, working tree clean, by the v2 cite-audit pass)
