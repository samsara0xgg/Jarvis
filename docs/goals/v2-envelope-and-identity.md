# Goal: v2-envelope-and-identity

## Goal
The daemon serves an authenticated `/inherent/ws/v2` endpoint that completes the ADR-0014 D7 hello handshake with typed D6 envelopes, and Python and Swift decode the same golden fixtures.

## Why
Every later Inherent v2 card (snapshot, sequencer, controls) needs the envelope, the identities, and the authenticated socket first; none exist.

## Current behavior
- One WS route, unauthenticated, outbound-only: `/inherent/ws` accepts, registers, and drains `receive_text` without interpreting frames (jarvis/surface/inherent_server.py:359-389). No Bearer check anywhere under jarvis/surface or jarvis/deployment.
- Wire frames are `{"op", "payload"}` with ops `open/append/done/voice/voice_capability` (jarvis/surface/inherent_output.py:18-23, :132-227); no `protocol_version`, `message_id`, `connection_id`, `log_epoch`, `boot_id`, `event_cursor`, `ephemeral_sequence`, `sent_at_ms`.
- `jarvis/shared/realtime.py:214-308` mints response/group/authorization IDs only; no `log_epoch`, `boot_id`, `connection_id`, `client_instance_id`. The `boot_id` in jarvis/surface/voice_media.py:368 is a voice-media session marker, unrelated.
- No Event Log operational metadata table; `log_epoch` appears nowhere outside the ADR.
- `RuntimePaths` (jarvis/deployment/__init__.py:43-108) has no token path.
- Swift decodes frames with `JSONSerialization` into `[String: Any]` and routes on the `op` string (desktop/inherent-swift/InherentCard/BridgeBackend.swift:32-45, :176-246); no Codable types, no v2 transport. Tests run via scripts/test_inherent_swift.sh (88 passing on main).
- No Python-to-Swift golden fixture mechanism exists.
- Layer header in inherent_server.py:24-32 restricts L5 imports to stdlib, fastapi, pydantic, starlette, and two L5 siblings; everything else arrives through `InherentDeps` callables.

## Target behavior
- ADR-0014 D5, D6, D7 (branch text §5, lines 323-528) are the contract: token file at `<runtime_root>/inherent-v2.token`, 256-bit random, written atomically with mode 0600, rotated on every boot, creation refusing symlink, non-regular file, wrong owner, unsafe parent, or broader mode; `Authorization: Bearer <token>` on the upgrade; token never in a frame, URL, log, or environment variable; client frames capped at 64 KiB and rejected before routing when unauthenticated; a per-connection frame rate limit with a configured initial value whose breach closes with `protocol_error`.
- `ClientEnvelope` and `ServerEnvelope` are strict DTOs in a new L5 module (`jarvis/surface/inherent_protocol.py` is the ADR's hint) with the exact D6 field sets; unknown optional fields are ignored; missing required IDs, negative cursor or sequence, invalid enum, oversized text, and wrong `delivery_class` fail closed.
- `log_epoch` lives in a one-row operational metadata table in the Event Log, assigned exactly once under `BEGIN IMMEDIATE` during migration for new and pre-existing databases, stable across restart, VACUUM, and byte copy; `boot_id` is minted once per daemon process; `connection_id` once per accepted socket. Minting functions live in jarvis/shared/realtime.py next to the existing ID minters.
- Handshake per D7: the first client frame must be a valid `client.hello` within two seconds or the socket closes; the server answers `server.hello` with `selected_version: 2`, `view_schema_version: 1`, `resume_mode: "snapshot"`, `server_high_water_cursor` equal to the current maximum `events.id`, `required_client_capabilities: ["paged_snapshot", "transport_ack"]`, and `runtime_capabilities` computed from live routes (`image_input` false while the image endpoint is a stub). An unsupported version or schema fails hello with `upgrade_required`.
- After a successful hello this card sends nothing further and keeps the socket open; snapshot and deltas belong to the next card.
- Swift gains a Codable `RealtimeProtocol` (envelopes, hello payloads, strict decoding mirroring Python's rules) and a v2 transport that reads the token from the path the launcher passes, opens the v2 socket with the Bearer header, sends `client.hello`, validates `server.hello`, and exposes its state; it is not wired into the card UI and the feature stays off.
- Golden fixtures under tests/fixtures/inherent_v2/ (client.hello, server.hello, one `ServerEnvelope` per `delivery_class`, one malformed set) are decoded by Python tests and by Swift tests from the same files.
- `/inherent/ws` and its frames are byte-identical to today.

## Affected contracts and files
- L6 jarvis/deployment/__init__.py:RuntimePaths — token path; token creation and validation helpers.
- L2 jarvis/state/event_log.py — operational metadata table and `log_epoch` assignment in migration (explicit exception to the single-table comment, per D6).
- L5 jarvis/surface/inherent_protocol.py (new) — DTOs; jarvis/surface/inherent_server.py — v2 route, auth, size and rate limits, hello.
- shared jarvis/shared/realtime.py — `log_epoch`, `boot_id`, `connection_id`, `client_instance_id` minters and prefixes.
- runtime jarvis/runtime/inherent_loop.py — inject the token validator, boot_id, high-water cursor reader, and capability computer into `InherentDeps`.
- desktop/inherent-swift/InherentCard/RealtimeProtocol.swift and a v2 transport file (new); desktop/inherent-swift/launcher.py — pass the token path; InherentCardTests — fixture decode and rejection tests.
- tests/integration — v2 server tests; tests/fixtures/inherent_v2/.

## Boundaries and non-goals
- Layers that may change: L2 (metadata table only), L5, L6, shared IDs, runtime wiring, Swift, tests, fixtures.
- Must not change: any v1 frame or the v1 route; voice, playback, or decision code; the launcher's existing v1 behavior; L5's import restrictions (validators arrive as injected callables).
- Non-goals: snapshot, `view.delta`, sequencer, ACK, flow control, reconnect policy, any control message, making v2 the default, showing v2 state in the UI.

## Rejected approaches
- Token via environment variable or query string — D5 forbids both.
- A version field on the v1 socket instead of a second route — D32 forbids mixing v1 and v2 in one reducer and v1 must stay byte-compatible.
- Treating loopback binding as authentication — D5 says loopback alone is not authorization.

## Acceptance evidence
- Positive (hermetic Python): an integration test starting the FastAPI app in-process shows raw pytest output for: upgrade without a token rejected; wrong token rejected; correct token plus valid hello yields `server.hello` with the fields above and a `log_epoch` equal to the value read from the metadata table; hello later than two seconds closes the socket; a frame over 64 KiB closes; malformed hello yields a protocol error; every golden fixture decodes; an unknown optional field is ignored; `upgrade_required` on an unsupported version.
- Positive (L2): a fresh database and a copy of a pre-upgrade database each receive exactly one `log_epoch` that survives close and reopen.
- Positive (Swift): `bash scripts/test_inherent_swift.sh` output shows the new RealtimeProtocol tests (fixture decode plus the rejection cases) passing alongside the existing 88.
- Live (required): start the daemon from this worktree on its own runtime root and port (do not touch the daemon on 127.0.0.1:8006); a small client using the token file completes hello; show the raw `server.hello` frame and, in a second connection without the token, the refusal. Canary value: the `log_epoch` printed from the metadata table and from the hello frame, identical.
- Regression: tests/integration/test_serve_inherent_smoke.py and tests/integration/test_inherent_server_asr_submit.py pass unchanged; full hermetic suite, `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools` exit 0. Raw output shown.

## Docs to sync
- docs/adr/0014-inherent-realtime-ux.md §16 (spec changes list, lines ~2024-2050) — apply the items that D5 to D7 need in docs/spec.html, or state per item that it is not yet applicable; D5 to D7 judged unchanged unless a detail deviates.
- docs/spec.html §3 Event Log description — the operational metadata table, one sentence, if §3 states the single-table rule.

## Open questions
(none)

## /goal condition
Implement docs/goals/v2-envelope-and-identity.md on the current branch. The goal is met when all of the following appear in the transcript: (1) the diff adds the token file under RuntimePaths with 0600 creation checks, the `log_epoch` metadata table with exactly-once assignment, the `log_epoch`/`boot_id`/`connection_id`/`client_instance_id` minters, strict ClientEnvelope/ServerEnvelope DTOs, the authenticated `/inherent/ws/v2` route with size and rate limits and the two-second hello deadline, Swift Codable protocol types plus a v2 transport, and golden fixtures under tests/fixtures/inherent_v2/, with no change to v1 frames or the v1 route; (2) raw pytest output of the v2 server tests covering rejection without token, wrong token, valid hello, hello timeout, oversized frame, malformed hello, fixture decode, unknown optional field, and upgrade_required, ending in a pass line; (3) raw output of the L2 test showing exactly one stable `log_epoch` for a fresh and a pre-upgrade database; (4) raw output of `bash scripts/test_inherent_swift.sh` showing the new tests passing and 0 failures; (5) raw output of the live hello against a daemon started from this worktree on a port other than 8006, with the `server.hello` frame and the untokened refusal shown and the `log_epoch` value quoted from both the table and the frame; (6) raw output of the two named regression test files, the full hermetic suite with live tests excluded, `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools`, each ending with a pass line or exit 0; (7) ADR-0014 §16 items for D5 to D7 applied to docs/spec.html or each explicitly judged not applicable, following the rule: update the canonical document that owns a changed contract, do not document what the code makes clear, do not duplicate a fact across documents; (8) each slice committed with the project commit skill and `git status` clean; (9) a Progress line per slice in the card. Or stop after 80 turns.

## Progress
- slice 1 identities + log_epoch — f11185a — 4/4 new L2 checks pass; full hermetic 719 passed / 63 deselected in 30.85s; lint-imports KEPT (1/1), ruff 203 files, mypy strict 201 files.
- slice 2 inherent v2 token file — ab11190, checks relocated in 5e2ebf5 — 10/10 tests in test_inherent_v2_identity.py pass (6 token checks) and 6/6 in test_realtime_observability.py; full hermetic 737 passed / 63 deselected in 32.16s; lint-imports KEPT (1/1), ruff 205 files, mypy strict 203 files.
- slice 3 v2 envelope DTOs + golden fixtures — 735481a — 12/12 protocol checks pass (5 fixture decodes, 18 malformed rejections); full hermetic 737 passed / 63 deselected in 33.28s; lint-imports KEPT (1/1), ruff 205 files, mypy strict 203 files.
- slice 4 authenticated /inherent/ws/v2 hello — 253c7cd — 13/13 checks in test_inherent_v2_server.py pass; live daemon on 127.0.0.1:8017 answered the D7 server.hello and refused the untokened upgrade with HTTP 403, log_epoch L8820e70272e748f58b7603571df89c16 identical in the metadata table and the frame; full hermetic 750 passed / 63 deselected in 33.09s; lint-imports KEPT (1/1), ruff 206 files, mypy strict 204 files.
- slice 5 Swift v2 protocol types + dormant transport — a7c1260 — 105/105 Swift tests pass, 0 failures (88 baseline + 17 new: 7 fixture decodes, 18 malformed rejections, hello encoding, 4 server.hello validation rejections, 2 token checks); InherentCard Debug build succeeded; Python gates not run (no Python under jarvis/tests/scripts/tools changed).
- docs sync D5-D7 — 55025e5 — docs/spec.html §5 gained the event_log_metadata/log_epoch sentence (ADR-0014 D6); ADR-0014 §16 items 13 (L2 half) applied, item 13 (L6 half), 3, 7 confirmed code-evident/unchanged, item 14 not yet applicable to this card.
- verifier follow-up: malformed-fixture rejection class — 4487cf8 — malformed.json entries gained loc/swift_error; tests/integration/test_inherent_v2_protocol.py 12 passed in 0.03s; scripts/test_inherent_swift.sh Executed 105 tests, with 0 failures; ruff clean (206 files), mypy strict clean (204 files).
- verifier follow-up: launcher runtime root resolution — 803431f — desktop/inherent-swift/launcher.py now matches L6 `_resolve_root`'s `.expanduser().resolve()`.
