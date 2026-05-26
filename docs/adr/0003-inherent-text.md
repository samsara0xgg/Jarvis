# ADR 0003 — Inherent Text Surface

## Status

- **Step 1** (non-streaming text + minimal wire): Proposed 2026-05-25,
  implemented + Tier-1 green 2026-05-25 (commit stack 2bea0a7..09b925f
  on `worktree-claude-adr0001`). Pending Allen smoke + flip to Approved.
- **Step 2** (sentence-chunked text + full legacy wire compat,
  **post-hoc replay model**): Proposed 2026-05-25, revised 2026-05-25
  to (a) commit to post-hoc replay (A1) over real-time streaming, (b)
  collapse three response-event watchers into one race-free watcher,
  (c) drop the redundant `seq` field and the unneeded
  `MAX_SENTENCE_CHARS` safety valve. Real-time streaming
  (`chat_stream` per-token + per-sentence gate + `op:reset`) is
  deferred to **ADR-0008**. Replaces Step 1's simplified envelopes;
  Step 1's shipped code is amended (not re-shipped) as part of Step 2
  build order. Pending Allen review.

This ADR continues to scope **text-only**: image, ASR, streaming TTS,
fallback chain remain in follow-on ADRs (see § Out of Scope).

## Step 1 Context

### What exists today

- **New Jarvis runtime** (ADR-0001 / ADR-0002): event-log-spine, L3
  multi-trigger decide loop inside `run_turn`, single L5 surface
  (`surface/cli.py` + `surface/cli_render.py`). Tier-1 verified green.
- **Legacy Inherent backend** at `jarvis-legacy/ui/web/server.py` (1374
  LOC): FastAPI app exposing `/inherent/submit`, `/inherent/image-submit`,
  `/inherent/asr-submit`, `/inherent/ws`, plus `/api/*` endpoints. Wires
  to legacy `JarvisApp.handle_text` via thread-pool fire-and-forget;
  responses surface via event_bus → WS push (`siri:open` / `siri:append`
  / `siri:done` / `siri:reset`).
- **Inherent-swift client** at `jarvis-legacy/desktop/inherent-swift/`:
  Native macOS card. Wire contract in `BridgeBackend.swift`:
  - HTTP: `POST http://127.0.0.1:8006/inherent/submit { text }` returns
    `{ status: "accepted" }` immediately.
  - WS: `ws://127.0.0.1:8006/inherent/ws` outbound-only. Server pushes
    `{ op, payload }`; `op ∈ {"open", "append", "done", "reset", "voice"}`.
    Client reconnect backoff `[1, 2, 4, 8, 16]s`; 30s watchdog after
    `open` expects `done`.
  - Turn-gate rule: `open` is only accepted if `streaming=true` OR
    non-empty `content` in payload.

### Why this ADR

Allen wants to retire the legacy backend and have inherent-swift talk to
the new runtime. The migration is incremental:

- **Step 1 (this ADR)**: text-in, text-out. Inherent card POSTs typed
  text; new runtime drives the same `decide`-loop / `render_response`
  pipeline as CLI; response pushes to WS as one `open` + `done` (no
  streaming).
- Steps 2-5 (out of scope, listed below): image, ASR, streaming TTS,
  surface fallback chain.

### Spec touchpoints

Spec sections this ADR aligns with:

- §3.4.1 — Event-triggered, not turn-bound. Runtime decision is triggered
  by event log writes; user turns are one trigger flavor among many.
  Drives the outer-watcher design over thread-per-submit.
- §3.6.1 — Input adapter: raw → buffer → canonical event. `/submit` is
  the raw input; canonical event is `surface.user_intent`.
- §3.6.3 — AttentionRouting / PresentationIntent are L3→L5 message
  contracts, NOT events. `surface.response_emitted` is the audit event
  that L5 emits at delivery time (ADR-0002 accepted deviation).
- §3.6.4 — Channel → physical surface is L5's choice within the channel.
  Daemon mode must NOT fire all surfaces in parallel; it selects Inherent.
- §3.6.7 — Inherent is a dumb client. UI button path = `surface.user_intent`
  → L3 → L4 → events → projection → re-render. Client owns no truth,
  triggers no tool directly.
- §3.6.11 — Surface failure / fallback chain. Step 1 has only one
  surface; explicit deviation declared.
- §3.3.1 — Event Log is the sole entry point for durable facts.

### Non-spec context

- The new runtime today has **no daemon mode**. CLI is a foreground
  REPL/one-shot that calls `run_turn` directly. `jarvis serve` will be
  the first long-running process.
- `.importlinter` enforces the layer DAG `cli > runtime >
  {decision | execution | surface | deployment} > state >
  {constitution | shared}`. `runtime/` is the only place allowed to wire
  across the middle four layers.
- `surface.response_emitted` is already emitted by `cli_render.py:272`
  on every turn; payload shape is fixed:
  ```
  { turn_id, text, voice_text, document_text,
    delivered_via, attention_channel, response_hash }
  ```

## Step 1 Decision

### D1. Trigger model: outer event-loop watcher

`/inherent/submit` HTTP handler does NOT call `run_turn` directly. It
emits `surface.user_intent` to the event log and returns HTTP 202. A
daemon-side async task (`_user_intent_watcher`) polls the event log for
new `surface.user_intent` rows and drives `drive_turn(user_intent_event=ev)`
for each. This matches spec §3.4.1 literally — the runtime watches the
event log, not the HTTP request lifecycle.

Rejected alternatives:

- **Thread-per-submit (HTTP handler calls `run_turn`)**: faster to ship,
  but couples HTTP lifetime to turn duration, requires explicit lock for
  SQLite serialization, and forces a refactor when sensor / scheduler
  triggers land later. Spec §3.4.1 explicitly names six other trigger
  types beyond `surface.user_intent`; building the watcher now amortizes
  that machinery.

### D2. `run_turn` refactor: extract `drive_turn`

`run_turn` today is the sole driver — it emits `surface.user_intent`,
runs the decide loop, and calls `render_response`. To let the watcher
drive from an already-emitted intent event, extract the post-emit body
into `drive_turn(runtime, *, user_intent_event, available_surfaces,
max_iterations, trigger_timeout_s) -> RunTurnResult`. `run_turn` becomes
a thin wrapper:

```python
def run_turn(runtime, utterance, *, turn_id=None, ...) -> RunTurnResult:
    effective_turn_id = turn_id or _new_turn_id()
    intent = emit_surface_user_intent(
        runtime.conn, transcript=utterance, turn_id=effective_turn_id,
    )
    return drive_turn(runtime, user_intent_event=intent, ...)
```

Existing `run_turn` callers (CLI sync path, scenario tests) are
behaviour-preserving. Canary impact: `test_canary_stash_pop_after_verify`
walks `run_turn`'s AST for `decide(...)` and `_pop_pending_stashes(...)`
call ordering. After refactor those calls live in `drive_turn`. Update:

```python
_RUN_TURN_NAME: str = "drive_turn"   # was "run_turn"
```

(`_VERIFY_DISPATCH_CALL_NAME = "decide"` is unchanged — it's the call
being searched for, not the function searched in.)

### D3. Physical-surface selection via `available_surfaces`

`render_response` today fires every surface listed by
`ATTENTION_CHANNEL_TO_SURFACES[channel]` (e.g. for `voice_notify`:
`say` + `osascript_banner` + `cli_stdout`). In daemon mode, firing
say / banner from the daemon process would double-deliver alongside the
Inherent WS push, violating spec §3.6.4 ("L5 selects within channel").

Add `available_surfaces: frozenset[str]` parameter to `render_response`:

```python
def render_response(
    state, response_plan, *, conn, turn_id, attention_channel,
    stream=None,
    available_surfaces: frozenset[str] = _CLI_DEFAULT_SURFACES,
) -> tuple[SurfaceState, Event]:
    ...
    for surface in surfaces:
        if surface not in available_surfaces:
            continue
        if surface == "say":
            ...

_CLI_DEFAULT_SURFACES: frozenset[str] = frozenset({
    "say", "say_bell", "osascript_banner",
    "osascript_banner_title_only", "cli_stdout",
})
```

`_CLI_DEFAULT_SURFACES` lives in `cli_render.py` next to the existing
`ATTENTION_CHANNEL_TO_SURFACES` mapping. The string values match the
literal surface IDs in the current `ATTENTION_CHANNEL_TO_SURFACES`
codomain (read out by the dispatch loop at lines 232-264).

- CLI path (`run_turn` → `render_response`): omits `available_surfaces`
  kwarg, gets default, day-1 behavior preserved.
- Daemon path (`_user_intent_watcher` → `drive_turn` → `render_response`):
  passes `frozenset()`. cli_render skips every physical surface; the
  audit `surface.response_emitted` still emits; `InherentBroadcaster`
  observes the event and does the actual delivery via WS.

### D4. CLI vs daemon mutex via PID file lock

Both CLI's `run_turn` and daemon's watcher would react to a
`surface.user_intent` event in the log, so running them together would
double-execute. Mitigation: `jarvis serve` acquires an exclusive flock
on `${runtime_root}/daemon.lock` containing its PID; `jarvis "..."`
checks the lock before bootstrap. If held → stderr message + exit 2.

Lock location is `runtime_root/daemon.lock` (NOT XDG state) because per
spec §3.7.2 each domain owns its own deployment artifacts, and the lock
is a per-runtime-root single-daemon guarantee. Multiple Jarvis
installations with separate runtime roots each get their own lock.

TOCTOU race window (lock probe at t0, daemon acquires at t1, CLI emits
intent at t2) is accepted for Step 1 — Allen is the sole operator and
the race requires sub-second concurrent invocation, which is
operationally implausible.

### D5. Module layout

New / modified files (spec layer where applicable; `cli/` and `runtime/`
are not numbered spec layers — `cli/` is the top-level argv entrypoint,
`runtime/` is the composition root):

```
-- cli/__main__.py             (modify)  +PID-lock probe; add `serve` subcommand
-- runtime/__init__.py         (refactor) extract drive_turn from run_turn body
-- runtime/inherent_loop.py    (new)     serve_inherent(), _user_intent_watcher,
                                          _response_broadcaster
L5 surface/inherent_server.py  (new)     FastAPI app factory
L5 surface/inherent_output.py  (new)     InherentBroadcaster (WS registry + translator)
L5 surface/cli_render.py       (modify)  +_CLI_DEFAULT_SURFACES constant,
                                          +available_surfaces parameter,
                                          +1-line filter in dispatch loop (~5 LOC)
L6 deployment/process_lock.py  (new)     flock primitive: acquire_exclusive,
                                          is_held, holder_pid
L2 state/event_log.py          (modify)  +turn.failed EventTypeSchema
   pyproject.toml              (modify)  +fastapi +uvicorn[standard]
   tests/unit/test_process_lock.py        (new)
   tests/unit/test_inherent_output.py     (new)
   tests/unit/test_inherent_server.py     (new)
   tests/unit/test_drive_turn.py          (new)
   tests/unit/test_inherent_loop.py       (new)
   tests/unit/test_cli_render_available_surfaces.py (new)
   tests/integration/test_serve_inherent_smoke.py    (new)
   tests/integration/test_cli_lock_refusal.py        (new)
   tests/canary/test_canary_stash_pop_after_verify.py (modify, 1 line)
```

`jarvis/runtime/daemon.py` (fork_detach helper) is **not** used by
`jarvis serve` — serve runs in foreground until SIGINT/SIGTERM.
fork_detach remains in service of CLI's long-run utterance path (ADR-0002).

### D6. Layer-boundary verification

All new imports respect `.importlinter`. The contract orders layers
top-to-bottom as `cli > runtime > {decision | execution | surface |
deployment} > state > {constitution | shared}`. Higher entries may
import lower entries; siblings in `{}` cannot import each other.

| From | To | Allowed? |
|---|---|---|
| `surface/inherent_server.py` | `state.event_log.emit_event` | Yes (surface → state, downward) |
| `surface/inherent_output.py` | `state.event_log.Event` (type only) | Yes |
| `runtime/inherent_loop.py` | `surface.inherent_server.create_app` | Yes (runtime → surface, downward) |
| `runtime/inherent_loop.py` | `surface.inherent_output.InherentBroadcaster` | Yes |
| `runtime/inherent_loop.py` | `deployment.process_lock.acquire_exclusive` | Yes (runtime → deployment, downward) |
| `runtime/inherent_loop.py` | `runtime.drive_turn` | Yes (intra-package) |
| `cli/__main__.py` | `deployment.process_lock.is_held` | Yes (cli → deployment, skip-level downward, importlinter `layers` contract allows) |

### D7. Module contracts

#### `pyproject.toml`

```toml
[project]
dependencies = [
    "openai", "anthropic", "PyYAML",
    "fastapi",            # new
    "uvicorn[standard]",  # new — pulls uvloop + httptools + websockets
]
```

#### `deployment/process_lock.py` (~50 LOC)

```python
class ProcessLockHeld(RuntimeError):
    def __init__(self, holder_pid: int) -> None: ...
    holder_pid: int

@contextmanager
def acquire_exclusive(lock_path: Path) -> Iterator[None]:
    """fcntl.flock LOCK_EX | LOCK_NB. Writes own pid to lock_path.
    Raises ProcessLockHeld(holder_pid) if held by another live process.
    Cleans up stale lock (holder dead per os.kill(pid, 0)) and retries
    once. Unlinks on context exit."""

def is_held(lock_path: Path) -> bool:
    """Non-blocking probe: attempt LOCK_EX | LOCK_NB on a copy fd;
    release immediately. Returns True iff held by another live process.
    No state mutation."""

def holder_pid(lock_path: Path) -> int | None:
    """Read pid from lock_path; None if file missing or empty or
    contents are not a positive int. Does not verify the process is alive."""
```

#### `runtime/__init__.py` — `drive_turn` extracted

```python
def drive_turn(
    runtime: JarvisRuntime,
    *,
    user_intent_event: Event,
    available_surfaces: frozenset[str] = _CLI_DEFAULT_SURFACES,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    trigger_timeout_s: float = _DEFAULT_TRIGGER_TIMEOUT_S,
) -> RunTurnResult:
    """Same body as today's run_turn lines 484-end. The user_intent
    event is supplied externally; drive_turn does NOT re-emit it.
    turn_id is read from user_intent_event.payload."""
```

`run_turn` thin wrapper passes through `available_surfaces=_CLI_DEFAULT_SURFACES`
(the default). Watcher path passes `frozenset()`.

#### `runtime/inherent_loop.py` (~120 LOC)

```python
async def serve_inherent(
    runtime: JarvisRuntime,
    *,
    host: str = "127.0.0.1",
    port: int = 8006,
    lock_path: Path,
    poll_interval_s: float = 0.01,
) -> None:
    """Daemon entry. Lifecycle:
    1. process_lock.acquire_exclusive(lock_path)  (raises ProcessLockHeld)
    2. broadcaster = InherentBroadcaster()
    3. app = create_app(InherentDeps(submit_callable=..., broadcaster=broadcaster))
    4. uvicorn.Config(app, host, port, log_level="warning", lifespan="off")
    5. watchers = [create_task(_user_intent_watcher), create_task(_response_broadcaster)]
    6. await server.serve()  # blocks until SIGINT/SIGTERM sets should_exit
    7. finally: cancel watchers + gather; lock released by ctx exit
    """

async def _user_intent_watcher(
    runtime: JarvisRuntime, *, poll_interval_s: float,
) -> None:
    """Single async task. Cursor over event log; for each new
    surface.user_intent: await asyncio.to_thread(drive_turn, runtime,
    user_intent_event=ev, available_surfaces=frozenset()).
    Catches Exception, emits turn.failed event, logs, continues."""

async def _response_broadcaster(
    runtime: JarvisRuntime, broadcaster: InherentBroadcaster, *,
    poll_interval_s: float,
) -> None:
    """Cursor over event log for surface.response_emitted; for each:
    await broadcaster.broadcast(event)."""
```

#### `surface/inherent_server.py` (~90 LOC)

```python
class SubmitRequest(BaseModel):
    text: str

@dataclass(frozen=True)
class InherentDeps:
    submit_callable: Callable[[str], None]    # (text) -> emit_surface_user_intent(...)
    broadcaster: InherentBroadcaster

def create_app(deps: InherentDeps) -> FastAPI:
    app = FastAPI()

    @app.post("/inherent/submit")
    async def submit(req: SubmitRequest) -> dict[str, str]:
        text = req.text.strip()
        if not text:
            raise HTTPException(400, "text required")
        await asyncio.to_thread(deps.submit_callable, text)
        return {"status": "accepted"}

    @app.websocket("/inherent/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        await deps.broadcaster.register(ws)
        try:
            while True:
                await ws.receive_text()    # keep-alive; legacy clients don't send
        except WebSocketDisconnect:
            pass
        finally:
            await deps.broadcaster.unregister(ws)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/inherent/image-submit")
    async def image_submit() -> None:
        raise HTTPException(501, "image not implemented in step 1 (ADR-0004)")

    @app.post("/inherent/asr-submit")
    async def asr_submit() -> None:
        raise HTTPException(501, "asr not implemented in step 1 (ADR-0005)")

    return app
```

#### `surface/inherent_output.py` (~90 LOC)

```python
class InherentBroadcaster:
    """In-memory WS client registry + surface.response_emitted ->
    siri:open + siri:done translator. Single instance per daemon process,
    shared between FastAPI WS endpoints (register/unregister) and
    _response_broadcaster task (broadcast)."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket) -> None: ...
    async def unregister(self, ws: WebSocket) -> None: ...

    async def broadcast(self, event: Event) -> None:
        text = event.payload.get("text", "")
        if not text:
            return
        open_msg = {"op": "open",
                    "payload": {"streaming": False, "content": text}}
        done_msg = {"op": "done", "payload": {}}
        async with self._lock:
            if not self._clients:
                LOGGER.warning(
                    "inherent broadcaster: response dropped, no clients "
                    "(turn_id=%s)", event.payload.get("turn_id"),
                )
                return
            dead = []
            for ws in self._clients:
                try:
                    await ws.send_json(open_msg)
                    await ws.send_json(done_msg)
                except Exception as exc:
                    LOGGER.warning("inherent ws send failed: %s", exc)
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)
```

#### `cli/__main__.py` (modify, ~25 LOC added)

```python
def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # args.runtime_root is the existing kwarg threaded into
    # bootstrap_runtime_app today; reuse it as the lock's directory.
    lock_path = args.runtime_root / "daemon.lock"

    if args.subcommand == "serve":
        try:
            asyncio.run(serve_inherent(
                bootstrap_runtime_app(runtime_root=args.runtime_root),
                lock_path=lock_path,
            ))
        except ProcessLockHeld as exc:
            sys.stderr.write(
                f"jarvis daemon already running at pid {exc.holder_pid}\n"
            )
            return 2
        return 0

    if process_lock.is_held(lock_path):
        pid = process_lock.holder_pid(lock_path)
        sys.stderr.write(
            f"jarvis daemon running at pid {pid}; stop it or "
            f"POST to http://127.0.0.1:8006/inherent/submit\n"
        )
        return 2

    return _main_sync(args)
```

#### `state/event_log.py` — register `turn.failed`

```python
EventTypeSchema(
    type="turn.failed",
    schema_version=1,
    owner_layer="L5",   # runtime/inherent_loop's watcher emits it
    required_payload_keys=frozenset({"turn_id", "exception_repr"}),
    optional_payload_keys=frozenset({"trigger_event_id"}),
    semantics=(
        "Watcher-level catch-all when drive_turn raises uncaught. "
        "Records the meta-failure as a durable fact per spec §3.3.1. "
        "Day-2 has no projection consumer; future task / claim ladder "
        "may fold turn.failed into Limitation Claims."
    ),
),
```

### D8. Happy-path data flow

Spec-layer labels: `surface/*` = L5, `state/*` = L2, `runtime/*` is the
composition root (not a spec layer).

```
[Swift InherentCard]
    POST 127.0.0.1:8006/inherent/submit  { text: "hi" }
        |
        v
[surface/inherent_server: /submit handler]            (L5)
        |  await to_thread(deps.submit_callable, "hi")
        v
[surface/cli.emit_surface_user_intent]                (L5)
        |  INSERT events (type="surface.user_intent",
        v                   payload={ transcript, turn_id })
[state/event_log]                  (L2)         ---> HTTP 202 returned
        |
        v  poll every 10 ms (after_id cursor)
[runtime/inherent_loop._user_intent_watcher]   (composition root)
        |
        v  await to_thread(drive_turn, runtime,
                            user_intent_event=ev,
                            available_surfaces=frozenset())
[runtime.drive_turn]                            (composition root)
        |  decide() loop (multi-trigger, sub-triggers via
        |    _wait_for_next_trigger, unchanged)
        |  render_response(..., available_surfaces=frozenset())
        |    -> all physical surfaces skipped
        |    -> still emits audit event
        v
[state/event_log]                               (L2)
        INSERT events (type="surface.response_emitted",
                       payload={ turn_id, text, voice_text, document_text,
                                 delivered_via=[], attention_channel,
                                 response_hash })
        |
        v  poll every 10 ms (after_id cursor)
[runtime/inherent_loop._response_broadcaster]  (composition root)
        |
        v  await broadcaster.broadcast(event)
[surface/inherent_output.InherentBroadcaster]  (L5)
        |  translate -> { op:"open", payload:{ streaming:false, content:text } }
        |               { op:"done", payload:{} }
        |
        v  for ws in clients: await ws.send_json(...)
[Swift InherentCard WSClient.receiveLoop]
        |  BridgeMessageRouter.dispatch
        v
[NativeCardController.siriOpen + siriDone -> card renders]
```

### D9. Failure modes

| # | Trigger | Detection | Behavior | Spec ref / note |
|---|---|---|---|---|
| F1 | `jarvis serve` when lock held | `acquire_exclusive` raises `ProcessLockHeld` | stderr "daemon at pid N already running" → exit 2 | §3.7 process placement |
| F2 | `jarvis "test"` when daemon up | `process_lock.is_held` true | stderr "daemon running … POST /inherent/submit" → exit 2 | — |
| F3 | `drive_turn` raises uncaught | watcher try / except wrapping `to_thread(drive_turn, ...)` | emit `turn.failed(turn_id, exception_repr)`; log warning; continue to next event | Watcher-level catch is meta-level; in-decide failures still go through L3 Limitation Claim per spec §3.4.11 |
| F4 | `ws.send_json` fails for a client | broadcaster except clause | swallow, log warn, mark dead, continue other clients | spec §3.6.11 — "单次 transient retry 可以留本地 telemetry"; `surface.failed` deferred to Step 5 (fallback chain) |
| F5 | broadcaster has 0 clients when response_emitted fires | `not self._clients` | log warn, return (response dropped) | **Explicit Step-1 deviation from §3.6.11** (MUST emit `surface.failed`); deferred until fallback chain exists in Step 5 / ADR-0007 |
| F6 | uvicorn startup error (port in use) | `server.serve()` raises | finally cancels watchers + releases lock; serve_inherent re-raises; cli stderr + exit 1 | — |
| F7 | SQLite write error in `emit_surface_user_intent` from /submit | `to_thread` propagates | FastAPI returns 500; swift client `BridgeBackend.classify` reports `http_500` | — |
| F8 | `kill -9 jarvis serve` leaves stale lock | next `jarvis serve` startup; `acquire_exclusive` checks holder via `os.kill(pid, 0)` | Stale → unlink + retry once | Standard PID file pattern |
| F9 | WS client disconnect mid-session | `receive_text` raises `WebSocketDisconnect` | finally clause unregisters; swift client handles reconnect via its own `[1,2,4,8,16]s` backoff + 30s watchdog | swift contract in `BridgeBackend.swift` |
| F10 | `response_emitted` fires while client disconnected (mid-reconnect window) | broadcaster falls through to F5 path | Response dropped; client 30s watchdog issues reset | **Explicit Step-1 deviation**: no replay queue. Step 5 / ADR-0007 will add `queue_policy` per spec §3.6.11 |
| F11 | `/submit` with empty body | Pydantic 422 OR my 400 (empty after strip) | swift client `BridgeBackend.classify` reports `http_422` / `http_400` | — |
| F12 | Two CLI invocations race when daemon down | TOCTOU on `is_held` probe | Both bootstrap; each is independent turn (own turn_id); SQLite WAL serializes writes | Acceptable Step-1 limitation; documented |

## Step 1 Consequences

### Tier-1 gate budget impact

- New LOC budget: ~500 production + ~250 test = ~750 LOC.
- `mypy --strict`, `ruff`, `lint-imports`, `pytest -q` all must remain
  green. Per project commit rules, tests must complete < 30s wall.
- `fastapi` and `uvicorn[standard]` are new third-party deps. Both are
  permissively licensed (MIT / BSD), widely audited, and used by legacy
  backend — low supply-chain risk.

### Canary impact

- `test_canary_stash_pop_after_verify.py`: 1-line change (`_RUN_TURN_NAME = "drive_turn"`).
- `test_canary_runtime_trigger_types.py`: **not affected**. The
  `_RUNTIME_TRIGGER_TYPES` set is for `_wait_for_next_trigger`
  (sub-triggers inside a turn). Watcher uses a separate poll mechanism
  and watches `surface.user_intent` — a different code path.
- `test_canary_surface_user_intent_swap.py`: **not affected**. Design
  emits `surface.user_intent` only, never `utterance.received`.
- `test_canary_daemon_ack_before_fork.py`: **not affected**. `jarvis
  serve` is foreground (no fork_detach). The canary continues to guard
  the CLI long-run utterance path.

### Spec deviations declared

- **F5 / F10 (`surface.failed` not emitted)**: spec §3.6.11 mandates
  emission for delivery failures. Step 1 logs only because (a) no
  fallback chain exists; (b) no projection consumer reads
  `surface.failed`. Compensating action: explicit listing here and in
  § Out of Scope; deferred to Step 5 / ADR-0007.

### Open against the user contract

None. Allen remains the sole operator; no new actor introduced.

## Step 1 Build Order

Land in this order — each step independently green on Tier-1 before the
next begins. Each step = one commit per project commit rules.

1. **`deployment/process_lock.py` + tests** (no dependencies on other
   new modules).
2. **`state/event_log.py` `turn.failed` registration + test**
   (single registry addition + assertion that emit succeeds).
3. **`runtime/__init__.py` extract `drive_turn` + canary update +
   `test_drive_turn.py`**. `run_turn` semantics unchanged for existing
   callers.
4. **`surface/cli_render.py` add `available_surfaces` filter +
   `test_cli_render_available_surfaces.py`**. CLI default preserves
   day-1 behaviour; empty-set skip path covered.
5. **`pyproject.toml` add fastapi + uvicorn[standard] + `uv sync`**.
   Must land before any step that imports starlette / uvicorn /
   fastapi (step 6 onward). Own commit so dep change is reviewable in
   isolation.
6. **`surface/inherent_output.py` `InherentBroadcaster` + unit tests**
   (pure asyncio + mocked WebSocket-shaped object; no FastAPI
   `TestClient` needed at this step).
7. **`surface/inherent_server.py` `create_app` + unit tests**
   (FastAPI `TestClient`; `/submit`, `/ws`, `/api/health`, 501 stubs).
8. **`runtime/inherent_loop.py` `serve_inherent` + watcher / broadcaster
   + unit tests**. Wires steps 1, 3, 6, 7 together.
9. **`cli/__main__.py` `serve` subcommand + lock probe + integration
   tests**. End-to-end smoke + lock-refusal subprocess test.

## Step 1 Definition of Done

All items must be green before Step 1 flips Status → Approved.

1. `lint-imports` reports KEPT (1/1). Layer DAG unbroken.
2. `ruff check` + `ruff format --check` clean across all changed files.
3. `mypy --strict` clean across all changed files.
4. `pytest -q` green for the union of (existing tests + new unit + new
   integration + updated canary). Wall clock < 30s.
5. Manual smoke (documented in progress.md):
   - `jarvis serve &` then `curl -XPOST localhost:8006/inherent/submit
     -d '{"text":"hi"}'` returns `{"status":"accepted"}` with 202.
   - A WS client (e.g. `websocat ws://127.0.0.1:8006/inherent/ws`)
     receives `{op:"open",...}` then `{op:"done",...}`.
   - SQLite event log shows `surface.user_intent` followed by
     `surface.response_emitted` rows for the turn.
   - `jarvis "second"` invoked in a separate shell returns exit 2 with
     stderr matching "daemon running".
   - SIGINT to `jarvis serve` releases lock cleanly; immediate
     `jarvis "test"` works.
6. Inherent-swift client (legacy `desktop/inherent-swift/InherentCard/`)
   launched against the new daemon: typing into the card produces a
   visible response in the card UI for at least one round-trip.
7. ADR-0003 spec self-review pass: placeholder scan, internal
   consistency, scope check, ambiguity check.

## Step 1 Out of Scope

| Item | Why deferred | Successor ADR |
|---|---|---|
| `/inherent/image-submit` implementation | Needs artifact store + `multimodal.staged` event + L3 staged_item_id resolution per spec §3.6.8 | Step 2 / ADR-0004 |
| `/inherent/asr-submit` implementation | Needs `utterance.received(transcript, confidence)` per spec §3.6.1, audio adapter, confidence threading into ResponsePlan | Step 3 / ADR-0005 |
| Streaming TTS (`/api/tts/stream`) | Needs three-tier ResponsePlan gate behaviour per spec §3.6.6 (routine / consequential_claim / high_risk_claim) | Step 4 / ADR-0006 |
| Multi-surface fallback chain (say → banner → Inherent fallback) | Needs `SurfaceFallbackChain` per spec §3.6.11 with availability_probe + cooldown + `surface.failed` emission | Step 5 / ADR-0007 |
| WS push replay queue for client reconnect | Same as above; queue_policy is part of SurfaceFallbackChain | Step 5 / ADR-0007 |
| Mode runtime state / mode preset table | Spec §3.3.6 mode system not yet built | Separate ADR |
| Cross-domain Inherent client (RPi, mobile) | This ADR is Mac-domain only per ADR-0001 | RPi domain ADR |
| `/submit` authentication | Localhost-only, single user; spec does not require auth | Multi-user / remote scenarios |
| Legacy `/api/session`, `/api/llm/presets`, `/api/llm/switch`, `/api/hidden-mode` endpoints | New runtime is session-less; LLM presets via Mode (deferred); hidden-mode is legacy private | Not planned |
| WS inbound from swift client (e.g. ctrl messages) | Spec §3.6.7 — Inherent does NOT trigger tools directly; UI button must round-trip via `/submit` → `surface.user_intent` | Not planned |
| Daemon auto-restart on crash | Out of scope for in-process design; launchd plist or similar belongs to L6 deployment ADR | Separate ADR |

## Step 2 Context

### What changes vs Step 1

Step 1 ships simplified WS envelopes:
- `open{streaming:false, content:<full text>}` + `done{}` (no `append`)
- Single `surface.response_emitted` event drives both envelopes

This works for the inherent-swift client (its `siriOpen` tolerates
`content` in the open frame) but **diverges from the legacy wire
schema**:
- `open{content:"", streaming:true, kind:"text", q:<query>}`
- `append{token:<chunk>}` (zero or more)
- `done{fadeMs:5000}`

Step 2 brings the daemon to byte-level wire compatibility with legacy
AND chunks routine responses sentence-by-sentence on the wire so the
card's frontend drip animation paces the reveal naturally.

### Streaming model: post-hoc replay (A1)

Pre-emit Gate (`decision/gates.pre_emit_gate`) needs the **full LLM
text** before it can derive `output_risk_class` and decide
`required_gate_mode`. Two streaming models were considered:

1. **Real-time streaming** (legacy-style): LLM streams via
   `chat_stream`; chunks emit to the WS as sentences form; gate runs
   per-sentence; downgrade triggers a new `surface.response_reset`
   event + `op:reset` envelope and re-stream. Latency win on first
   sentence (~300-800ms instead of full LLM duration). Cost: ~400-500
   prod LOC, new event type + wire envelope, potential reset flicker
   on gate retry.

2. **Post-hoc replay** (A1, this ADR): LLM runs **non-streaming** via
   `chat()`; Pre-emit Gate runs on full text via existing
   `_finalize_response` (unchanged); `render_response` splits the
   gated text into sentences and emits N `surface.response_chunk`
   events back-to-back. The card's frontend drip animation paces the
   visual reveal. **No latency win** — first chunk arrives after full
   LLM + gate time — but the wire is legacy-compatible, gate
   semantics stay clean, and no `op:reset` flicker exists.

Step 2 takes the **post-hoc replay** path. Rationale:

- The flagship scenario's text emits are mostly post-`worker.reported`
  (consequential claims with verified Postcondition evidence) — these
  would fall back to non-streaming anyway under real-time. The
  latency benefit applies only to routine acks where the LLM finishes
  in 1-2s to begin with.
- Spec §3.4.13 is silent on real-time vs replay; both satisfy
  "sentence-boundary streaming for routine".
- Real-time's reset-flicker UX risk is harder to validate at this
  stage than its latency benefit is to demonstrate.
- `LLMClient.chat_stream()` stays in tree (unused) for **ADR-0008**
  real-time streaming, if telemetry justifies it later.

Deferred to **ADR-0008** (real-time streaming): per-token LLM
streaming inside `decide()`, per-sentence gate, the
`surface.response_reset` event type, and the wire `op:reset` envelope.

### Spec touchpoints (new vs Step 1)

- **§3.4.13** — Pre-emit Gate output schema. A1's post-hoc replay
  satisfies both spec arms:
  - `required_gate_mode == "sentence"` → `render_response` splits and
    emits one `surface.response_chunk` per sentence.
  - `required_gate_mode == "full_text"` or `"structured"` →
    `render_response` emits a single `surface.response_chunk` with the
    full text (no speculative streaming).
- **§3.6.6** — Surface honors ResponsePlan; surface NEVER decides risk
  independently. `render_response` reads `required_gate_mode` but does
  not re-derive it.
- **§3.3.1** (re-confirm) — Every observable state change emits a
  durable event. Each chunk arrival is an observable state change;
  one `surface.response_chunk` event per chunk.

### What's already in place

- `decision/gates.py:99` `ResponsePlan` carries `output_risk_class` +
  `required_gate_mode` derived by the Pre-emit Gate. Step 2 consumes
  `required_gate_mode` in `render_response`; **no schema change** to
  `ResponsePlan`.
- `decision/__init__.py:1716` `_finalize_response` runs Pre-emit Gate
  on full text and owns the retry chain. **Unchanged in Step 2** — A1
  preserves the gate's input shape (full text) and the gate's call
  site.
- `decision/llm.py:411` `chat_stream()` is implemented for both
  OpenAI and Anthropic backends but **unused in Step 2** (A1 uses
  `chat()`). Preserved for ADR-0008.
- `surface/inherent_output.py` Step 1 broadcaster framework reused;
  the translator methods are rewritten (D13).

### What's verbatim-portable from legacy

- `jarvis-legacy/core/llm.py:1025-1095` (`_find_split_point` +
  `_possible_abbreviation_prefix`) — sentence boundary scanner +
  decimal/abbreviation guards. **Ported in Step 2 D14** as a pure
  function over the full text. Legacy's stateful streaming buffer
  (`_flush_sentences` lines 971-1003) is NOT ported — A1 operates on
  the full LLM text, so no incremental buffer is needed.
- `jarvis-legacy/ui/web/server.py:357-414` — `_broadcast_inherent` +
  `_on_response_{start,chunk,final}` is the canonical envelope-mapping
  reference. Step 2 broadcaster mirrors it byte-for-byte (D11).

## Step 2 Decision

### D10. Three-event taxonomy for chunked responses

Three L2 event types drive every Inherent text response (single-chunk
or sentence-chunked):

| Event type | Payload | When emitted |
|---|---|---|
| `surface.response_open` | `{turn_id, query, kind:"text"}` | Once per turn, BEFORE first chunk |
| `surface.response_chunk` | `{turn_id, text}` | Once per sentence (`sentence` mode) or once total (`full_text` / `structured` mode) |
| `surface.response_emitted` | (existing, unchanged) | Once per turn, AFTER all chunks. Audit-bearer. |

Rationale (spec §3.3.1 — one event per observable state change).
Conflating into a single `surface.response_event {kind:...}` was
rejected:
- Event-type-based filtering (watcher `WHERE type = ?`) gets messy
  when same type carries different payloads.
- Replay / audit tools that group events by type lose granularity.
- `surface.response_emitted` already exists; reusing-with-kind would
  amend its current consumers (cli_render, scenario tests).

The `query` field on `response_open` is the user's original
transcript, lifted from the trigger
`surface.user_intent.payload.transcript` (D15 plumbs it through
`drive_turn` → `render_response`). The wire envelope's `q` field
requires it.

**No `seq` field on `response_chunk`**: SQLite `id` is the monotonic
sequence and the watcher's `ORDER BY id` cursor already preserves
per-turn order. A turn-local `seq` would be redundant audit
metadata.

### D11. Wire envelope mapping (verbatim legacy)

Broadcaster translates 1-to-1, no aggregation:

```
surface.response_open    -> {"op":"open",   "payload":{"content":"", "streaming":True, "kind":"text", "q":<query>}}
surface.response_chunk   -> {"op":"append", "payload":{"token":<text>}}
surface.response_emitted -> {"op":"done",   "payload":{"fadeMs":5000}}
```

`fadeMs:5000` is the legacy default
(`jarvis-legacy/ui/web/server.py:404`). Swift card uses it to drive
`FadeController`'s alpha animation timer; mismatching alters card
on-screen duration vs what the user is used to.

`streaming:true` is ALWAYS set, even in `full_text` mode (only one
append). The flag means "expect appends", which is true in both modes
(`full_text` = exactly one append, then done). Card UX is identical
from the user's POV — the single append fills the bubble; the
frontend's per-char drip paces the visual reveal regardless of how
many appends arrived.

`content` is ALWAYS empty in the open envelope; chunk content arrives
via `append`. This deviates from Step 1's `open{content:<full text>}`
pattern; the Step 1 broadcaster is rewritten (D13).

### D12. Streaming trigger condition

`render_response` (in `surface/cli_render.py`) gains a
`streaming_enabled: bool = False` kwarg and a `query: str = ""`
kwarg. The chunked emission is gated thus:

```python
if streaming_enabled:
    _emit_surface_response_open(conn, turn_id=turn_id, query=query)
    if response_plan.required_gate_mode == "sentence":
        chunks = split_into_sentences(response_plan.text)
    else:  # "full_text" or "structured"
        chunks = [response_plan.text]
    for chunk in chunks:
        _emit_surface_response_chunk(conn, turn_id=turn_id, text=chunk)

# existing path: physical-surface dispatch + surface.response_emitted
...
```

When `streaming_enabled=False` (CLI default), only the existing
`surface.response_emitted` is emitted — Step-1 behavior preserved
byte-for-byte for the CLI path.

Daemon mode passes `streaming_enabled=True` from
`_user_intent_watcher` → `drive_turn` → `render_response`.

Note: `required_gate_mode` is the canonical surface-facing field per
spec §3.4.13; `output_risk_class` is NOT consulted independently
(Pre-emit Gate already derives one from the other).

### D13. Step 1 envelope refactor (supersede simplified path)

Step 1's `InherentBroadcaster.broadcast(event)` reads
`event.payload["text"]` and emits `open{content,streaming:false}` +
`done{}` for every `surface.response_emitted`. **This is replaced**,
not extended:

After Step 2:
- `InherentBroadcaster` exposes three async methods:
  `broadcast_open(event)`, `broadcast_chunk(event)`,
  `broadcast_done(event)` — one per event type.
- The single response watcher (D16) dispatches by `event.type`.
- Step 1's `open{content:full text}` wire shape no longer exists.

This is wire-breaking for any external consumer depending on Step 1's
exact envelopes. The only known consumer is the inherent-swift card,
which already speaks the legacy schema and is happier with this
change than with Step 1's simplification.

Tests in `tests/unit/test_inherent_output.py` and
`tests/integration/test_serve_inherent_smoke.py` are updated in-place;
no Step 1 commit is reverted.

### D14. Sentence boundary detection — pure function in `surface/sentence_splitter.py`

New module `surface/sentence_splitter.py` (~50 LOC) exposes a pure,
stateless function:

```python
def split_into_sentences(text: str) -> list[str]:
    """Split `text` into sentences at sentence-ending punctuation.

    Boundaries: ASCII ``.!?``, CJK ``。！？``, newline.

    Guards (ported from
    ``jarvis-legacy/core/llm.py:1025-1095``):
    - Decimals: ``3.14`` does NOT split on the inner ``.``.
    - Abbreviations: ``Dr.``, ``e.g.``, ``Mrs.``, ``Mr.``, ``Ms.``,
      ``Prof.``, ``i.e.``, ``Jr.``, ``Sr.``, ``St.``, ``Rd.``,
      ``Inc.``, ``Ltd.``, ``vs.`` do NOT split.

    Returns a list of non-empty sentence strings (whitespace
    trimmed). If `text` has no sentence-ending punctuation, returns
    ``[text.strip()]`` unchanged (a single-sentence list). Empty
    intermediate sentences are dropped.
    """
```

Because A1 operates on the **full LLM text** (not a streaming
buffer), the function is stateless and has **no `MAX_SENTENCE_CHARS`
safety valve** — a degenerate LLM output without punctuation simply
yields one sentence (the whole text). Legacy's `force=True` flush at
stream end is not needed under A1.

Layer placement: **`jarvis/surface/`**, not `jarvis/decision/`.
`surface` and `decision` are siblings in the `.importlinter` DAG
(`cli > runtime > {decision | execution | surface | deployment} >
state`); a cross-sibling import would break the contract. The
splitter is a presentation-layer concern (rendering text into wire
chunks), not a decision/gate concern, so `surface/` is the correct
home. If a future ADR (e.g. ADR-0008 real-time streaming) needs the
splitter inside `decide()`, it can be promoted to `shared/` at that
time.

Test: `tests/unit/test_sentence_splitter.py` — Chinese punctuation,
English punctuation, mixed-language, decimal-guard, abbreviation-
guard for each abbreviation, no-punctuation single-sentence, empty
input, multi-newline, leading/trailing whitespace.

### D15. `render_response` owns chunking; `decide()` unchanged

`decide()` and `_finalize_response` are **unchanged** in Step 2.
`ResponsePlan` schema is **unchanged** (no new field). The Pre-emit
Gate continues to run on the full LLM text via
`LLMClient.chat()`.

Chunking happens in
`surface/cli_render.py::render_response`:

```python
def render_response(
    state, response_plan, *, conn, turn_id, attention_channel,
    stream=None,
    available_surfaces=_CLI_DEFAULT_SURFACES,
    streaming_enabled: bool = False,   # NEW
    query: str = "",                    # NEW
) -> tuple[SurfaceState, Event]:
    if streaming_enabled:
        _emit_surface_response_open(conn, turn_id=turn_id, query=query)
        chunks = (
            split_into_sentences(response_plan.text)
            if response_plan.required_gate_mode == "sentence"
            else [response_plan.text]
        )
        for chunk_text in chunks:
            _emit_surface_response_chunk(conn, turn_id=turn_id, text=chunk_text)

    # existing path unchanged: physical-surface dispatch +
    # surface.response_emitted emit
    ...
```

`drive_turn` (in `runtime/__init__.py`) gains a `streaming_enabled:
bool = False` kwarg, lifts `query` from
`user_intent_event.payload["transcript"]`, and passes both into
`render_response`. The daemon watcher path
(`runtime/inherent_loop.py::_user_intent_watcher` →
`_drive_turn_in_worker_thread`) overrides
`streaming_enabled=True`; the CLI path (`runtime.run_turn`) keeps
the default `False`.

Rejected alternative — **`decide()` does the streaming**: needed
only if real-time streaming was desired (A2/A3). Under A1, no
benefit; would complicate `decide()` with chunk accumulation it
never uses; would add `stream_chunks: tuple[str, ...] | None` to
`ResponsePlan` for no current consumer.

### D16. Single response watcher (race-free by construction)

`surface/inherent_output.py` `InherentBroadcaster` gains three async
methods (one per event type) and drops Step 1's `broadcast(event)`:

```python
class InherentBroadcaster:
    async def register(self, ws): ...
    async def unregister(self, ws): ...

    async def broadcast_open(self, event: Event) -> None:
        payload = {"content": "", "streaming": True, "kind": "text",
                   "q": event.payload.get("query", "")}
        await self._send_all({"op": "open", "payload": payload}, event)

    async def broadcast_chunk(self, event: Event) -> None:
        text = event.payload.get("text", "") or ""
        if not text:
            return  # mirror legacy _on_response_chunk empty-suppression
        await self._send_all({"op": "append",
                              "payload": {"token": text}}, event)

    async def broadcast_done(self, event: Event) -> None:
        await self._send_all({"op": "done",
                              "payload": {"fadeMs": 5000}}, event)

    async def _send_all(self, msg: dict, event: Event) -> None:
        # F4 dead-client tracking, F5 no-client logging. No asyncio.Lock
        # needed — the single response watcher (below) is the sole
        # caller, so per-call serialization is implicit.
```

`runtime/inherent_loop.py` replaces Step 1's `_response_broadcaster`
with a **single** response watcher that polls all three event types
in one cursor and dispatches by `event.type`:

```python
_SELECT_RESPONSE_EVENTS_AFTER_ID_SQL = (
    "SELECT id, event_uid, type, schema_version, ts_epoch_ms, "
    "payload_json, source_event_id, correlation_json "
    "FROM events WHERE id > ? AND type IN "
    "('surface.response_open', 'surface.response_chunk', "
    "'surface.response_emitted') "
    "ORDER BY id ASC"
)

async def _response_watcher(
    runtime, broadcaster, *, poll_interval_s,
) -> None:
    after_id = _latest_id(runtime.conn)
    while True:
        rows = _fetch_response_events_after(runtime.conn, after_id=after_id)
        for row_id, ev in rows:
            after_id = max(after_id, row_id)
            if ev.type == "surface.response_open":
                await broadcaster.broadcast_open(ev)
            elif ev.type == "surface.response_chunk":
                await broadcaster.broadcast_chunk(ev)
            else:  # surface.response_emitted
                await broadcaster.broadcast_done(ev)
        await asyncio.sleep(poll_interval_s)
```

The Step 1 `_user_intent_watcher` is unchanged except its call to
`drive_turn` now passes `streaming_enabled=True`.

Rejected alternative — **three concurrent watchers (one per event
type)**: with three sibling asyncio tasks polling SQLite at 10 ms,
asyncio's scheduler does NOT guarantee wakeup order between them.
A chunk-watcher that wakes before the open-watcher can race
`op:append` ahead of `op:open` on the WS — the swift card discards
the orphan append, breaking the turn. Single watcher with `WHERE
type IN (...) ORDER BY id` eliminates the race **by construction**.
The SQL cost is one IN clause over the existing `(id > ?)`
predicate; no new index needed (the `id` column is the primary
key).

### D17. Module touch list

| Layer | File | Change | LOC (net) |
|---|---|---|---|
| L5 | `surface/sentence_splitter.py` | new | ~50 |
| L2 | `state/event_log.py` | register `surface.response_open` + `surface.response_chunk` | ~+20 |
| L5 | `surface/cli_render.py` | add `streaming_enabled` + `query` kwargs; conditional open/chunk emit; import `split_into_sentences` | ~+30 |
| L5 | `surface/inherent_output.py` | broadcaster rewrite (3 methods; drop old `broadcast`); drop `asyncio.Lock` | ~+30 |
| comp | `runtime/__init__.py` | add `streaming_enabled` kwarg to `drive_turn`; thread `query` from intent event into `render_response` | ~+15 |
| comp | `runtime/inherent_loop.py` | single response watcher (replaces `_response_broadcaster`); `streaming_enabled=True` in `drive_turn` call | ~+30 |
| — | `tests/unit/test_sentence_splitter.py` | new | ~80 |
| — | `tests/unit/test_event_log_response_types.py` | new (or extend existing event-log tests) | ~30 |
| — | `tests/unit/test_cli_render_streaming.py` | new | ~80 |
| — | `tests/unit/test_drive_turn_streaming.py` | new (or extend existing `test_drive_turn.py`) | ~40 |
| — | `tests/unit/test_inherent_output.py` | rewrite (3 broadcaster paths) | ~+30 |
| — | `tests/unit/test_inherent_loop.py` | single-watcher coverage | ~+40 |
| — | `tests/integration/test_serve_inherent_smoke.py` | end-to-end open/append/done verify | ~+30 |

Estimated net new prod LOC ~175, net new test LOC ~330. Total ~505.

`decision/__init__.py`, `decision/gates.py`, and `decision/llm.py`
are **not** modified by Step 2 (vs the original revision of this
ADR which proposed `decide()` streaming and a new `ResponsePlan`
field).

### D18. Layer-boundary verification

All new imports respect `.importlinter`. The contract orders layers
top-to-bottom as `cli > runtime > {decision | execution | surface |
deployment} > state > {constitution | shared}`; siblings cannot
import each other.

| From | To | Allowed? |
|---|---|---|
| `surface/cli_render.py` | `surface.sentence_splitter.split_into_sentences` | Yes (intra-package, same layer) |
| `surface/cli_render.py` | `state.event_log.emit_event` | Yes (existing, surface → state downward) |
| `surface/inherent_output.py` | `state.event_log.Event` (type-only) | Yes (existing) |
| `runtime/inherent_loop.py` | `surface.inherent_output.InherentBroadcaster` (extended) | Yes (existing) |
| `runtime/__init__.py` | `surface.cli_render.render_response` (extended kwargs) | Yes (existing) |

No new cross-sibling edges. The original revision's
`surface/cli_render.py → decision.sentence_splitter` edge would have
violated the sibling rule; placing the splitter in
`surface/sentence_splitter.py` removes the violation.

### D19. Failure modes (Step 2 additions)

Step 1's F1-F12 carry over unchanged. New / amended:

| # | Trigger | Detection | Behavior | Note |
|---|---|---|---|---|
| F13 | LLM `chat()` raises during the turn | existing `_finalize_response` retry chain (Step 1 F3 path catches at the watcher level) | Emit `turn.failed`; no `surface.response_*` events emitted → broadcaster sees nothing → card 30 s watchdog issues `op:reset` locally | Spec-compliant; matches Step 1 F3 |
| F14 | LLM returns text without any sentence-ending punctuation | `split_into_sentences` returns `[text.strip()]` | Single chunk emitted; UX = one `append` then `done` | Stateless splitter; no overflow valve needed under A1 |
| F15 | `streaming_enabled=True` but gate returns `required_gate_mode == "full_text"` | `render_response` falls through to the single-chunk branch | Emits `open` + 1 `chunk` (full text) + `done` | Spec-compliant gate downgrade |
| F16 (amends F5) | `surface.response_*` fires while 0 WS clients | broadcaster F5 path (`_send_all` early return on empty registry) | All 3 envelopes dropped (was 2 in Step 1); same warning log per event | Inherits Step 1 deviation (Step 5 / ADR-0007) |

## Step 2 Consequences

### Tier-1 gate budget impact

- Net new prod ~175 LOC + net new test ~330 LOC = ~505 LOC.
- No new third-party deps.
- `mypy --strict`, `ruff`, `lint-imports`, `pytest -q` must stay green.
- Wall < 30s for `pytest -q` continues to apply.

### Canary impact

- `test_canary_stash_pop_after_verify`: no change (Step 1 already set
  `_RUN_TURN_NAME = "drive_turn"`).
- `test_canary_runtime_trigger_types`: confirm `surface.response_open`
  + `surface.response_chunk` should NOT appear in
  `_RUNTIME_TRIGGER_TYPES` (they trigger only the L5 broadcaster, not
  the L3 decide loop). Verification = read canary, confirm
  non-membership, leave as-is.
- `test_canary_surface_user_intent_swap`: unaffected.
- `test_canary_daemon_ack_before_fork`: unaffected.

### Spec deviations declared

- **F16 (amends Step 1's F5 / F10)**: Step 1's `surface.failed`
  deviation continues; Step 2 amplifies (3 envelope types vs 1
  dropped on no clients). Same compensating action: documented +
  Step 5 / ADR-0007 closes.

### Open against the user contract

None. Allen remains sole operator.

## Step 2 Build Order

Six commits. Each independently green on Tier-1 before the next
begins. Each commit owns one logical concern; the wire-breaking pair
(broadcaster + watcher rewrite) is intentionally bundled into a
single commit because half-landed state would leave the daemon
emitting nothing.

1. **`surface/sentence_splitter.py` + `tests/unit/test_sentence_splitter.py`**
   — pure CPU, no other-module deps; standalone. Layer: L5.

2. **`state/event_log.py` register `surface.response_open` +
   `surface.response_chunk`** + emit/read-back tests
   (`tests/unit/test_event_log_response_types.py` or extension to
   existing event-log tests). No consumer yet — durable schema
   ready for D15's emit sites.

3. **`surface/cli_render.py` `streaming_enabled` + `query` kwargs +
   conditional open/chunk emit** + `tests/unit/test_cli_render_streaming.py`.
   CLI default (`streaming_enabled=False`) preserves Step-1 emit
   shape byte-for-byte. The daemon path is still not yet wired
   (drive_turn doesn't pass `True` yet), so this commit changes
   nothing observable from the daemon side; it lays the rails.

4. **`runtime/__init__.py` `drive_turn` gets `streaming_enabled` +
   passes `query` from `user_intent_event`** + `tests/unit/test_drive_turn_streaming.py`
   (or extension to existing `test_drive_turn.py`). Daemon's existing
   `drive_turn` call site in `runtime/inherent_loop.py` still omits
   the new kwarg, defaulting to `False` — so daemon still emits
   Step-1 envelopes through the OLD broadcaster. Tier-1 green
   through this step is "Step-1 daemon still works".

5. **PAIRED COMMIT — wire-breaking**: 
   - `surface/inherent_output.py` broadcaster rewrite: drop
     `broadcast(event)`; add `broadcast_open` / `broadcast_chunk` /
     `broadcast_done`; drop `asyncio.Lock` (single caller); rewrite
     `tests/unit/test_inherent_output.py`.
   - `runtime/inherent_loop.py`: replace `_response_broadcaster`
     with `_response_watcher` (single watcher, `WHERE type IN (...)
     ORDER BY id`, dispatch by `event.type`); change
     `_user_intent_watcher`'s `drive_turn` call to pass
     `streaming_enabled=True`; extend `tests/unit/test_inherent_loop.py`.
   - Daemon now emits the legacy 3-envelope wire end-to-end. Step-1
     `open{content:full text}` shape ceases to exist.

6. **`tests/integration/test_serve_inherent_smoke.py` end-to-end
   open / append / done verify** for both `sentence` and `full_text`
   gate modes; manual Swift card smoke per DoD §5.

(The original 9-step Build Order in this ADR's first revision was
collapsed to 6 once `decide()` / `gates.py` / `ResponsePlan` /
`runtime.run_turn` were taken out of scope by A1's post-hoc replay
design and Issue B's single-watcher refactor.)

## Step 2 Definition of Done

All items green before Step 2 flips Status → Approved:

1. `lint-imports` KEPT (1/1).
2. `ruff check` + `ruff format --check` clean.
3. `mypy --strict` clean.
4. `pytest -q` green; wall < 30 s.
5. Manual smoke (daemon running, Swift card connected):
   - **Routine prompt** (`required_gate_mode == "sentence"` because
     no Postcondition Claim exists for the active subject — e.g. a
     fresh runtime root + the prompt "你好，介绍一下自己。三句话以
     上。"). Card shows multiple sentences arriving in sequence (one
     `append` per sentence on the wire).
   - **Consequential prompt** (`required_gate_mode == "full_text"`
     — requires a verified Postcondition Claim in the projection;
     easiest reproduction is the flagship "task done" path:
     `worker.reported` → `verify_diff` →
     `claim.postcondition(verified)` then a user follow-up prompt
     that asks about the verified task, e.g. "刚才那个 task 怎么样
     了？"). Card shows the full reply as a single `append`.
   - WS frames inspected (browser devtools or `websocat
     ws://127.0.0.1:8006/inherent/ws`) show the legacy schema:
     `open{streaming:true,kind:"text",q:<query>}` then
     `append{token:<chunk>}*N` then `done{fadeMs:5000}` in that
     exact order.
6. CLI sync mode unchanged: `jarvis "hello"` prints final text once,
   no intermediate output, no `surface.response_*` rows in the event
   log for that turn (only the existing `surface.response_emitted`).
7. ADR-0003 Step 2 spec self-review: placeholder scan, internal
   consistency, scope check, ambiguity check.

## Step 2 Out of Scope

Step 1's Out-of-Scope table carries forward unchanged. Items
explicitly NOT addressed by Step 2:

- **Real-time streaming** (per-token LLM stream + per-sentence gate
  + `surface.response_reset` event + `op:reset` wire envelope) —
  **ADR-0008** if telemetry on Step 2's post-hoc replay justifies
  the latency win.
- Streaming TTS (audio sentence streaming with MiniMax WS) — Step 4
  / ADR-0006.
- Per-sentence emotion / voice_text overrides (legacy
  `on_sentence(text, emotion=, voice_text=)` signature) — Step 4 /
  ADR-0006.
- `op:voice` envelope (voice state updates) — Step 3 / ADR-0005.
- Backpressure / slow-client timeout on WS send — separate ADR if
  observed in practice.
- Replay queue for client reconnect mid-stream — Step 5 / ADR-0007.

## References

- `spec.html` §3.3.1, §3.4.1, §3.4.2, §3.4.11, §3.4.13, §3.6.1,
  §3.6.3, §3.6.4, §3.6.6, §3.6.7, §3.6.11, §3.6.12, §3.7.2
- ADR-0001 — Mac-only Flagship Scenario
- ADR-0002 — Real Codex Flagship Scenario (the
  `surface.response_emitted` accepted-deviation precedent; daemon /
  CLI contract for fork_detach)
- Legacy: `jarvis-legacy/ui/web/server.py` (1374 LOC) — endpoint shape
  reference; Step 1 did not reuse; Step 2 mirrors lines 357-414
  envelope mapping byte-for-byte
- Legacy: `jarvis-legacy/core/llm.py:993, 1001` — sentence boundary
  heuristic (ported in Step 2 D14)
- Legacy: `jarvis-legacy/core/tts_preprocessor.py` — text cleanup
  (Step 2 may consume; otherwise deferred to TTS step)
- Legacy: `jarvis-legacy/desktop/inherent-swift/InherentCard/BridgeBackend.swift`
  (398 LOC) — wire contract source; client preserved unchanged through
  both Step 1 and Step 2
