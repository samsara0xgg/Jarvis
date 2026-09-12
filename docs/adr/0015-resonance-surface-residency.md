# ADR 0015 — Resonance Surface, Mute Controls, Surface Residency

**Status:** Approved (2026-09-12; Allen: "OK没问题" on the plan, "不要 inherent 的了，全部接到 Resonance … 你直接自己搞定就行" on scope)
**Date:** 2026-09-12
**Supersedes:** the Swift Inherent card (`desktop/inherent-swift/`, the client half of ADR-0003 and ADR-0014) as the Mac desktop surface. The `/inherent/*` wire the daemon serves is unchanged and keeps its name.
**Depends on:** ADR-0003 (v1 wire), ADR-0005 §6 (`voice` envelope), ADR-0008 D10 (`cancel-response`), ADR-0009 D1 (launchd residency).
**Number note:** 0004, 0007, 0010 and 0013 stay reserved by earlier defer tables; this ADR takes the next free number.

## 1. Context

- Allen's target is voice-first "Live" mode: the capsule's left button is text
  input, the right one notifications, the center is voice. Almost every
  interaction is spoken.
- A Jarvis session is two OS processes: the daemon (`python -m jarvis serve`,
  which owns mic, wake word, VAD, ASR and TTS in-process) and one desktop
  surface that is only a WebSocket client. The Swift card had no supervisor,
  ADR-0009 D1's `jarvis daemon install` was built but never installed on
  Allen's Mac, and every session was hand-wired by a script that killed and
  restarted the daemon, waited for the port, found the card binary and waited
  for its socket. The overlay config that script generated is dead: main
  ships every switch it flipped ON (728afae).
- Resonance (`desktop/resonance/`, Electron + React, Codex 92a503a) is the
  approved surface. Its HANDOFF asked for runtime wiring to be decided by the
  repository's ownership rules rather than inside the prototype.

## 2. Decision

### D1. Resonance is the only desktop surface and speaks the v1 wire

- In: `ws://127.0.0.1:<port>/inherent/ws` envelopes `voice` (phases),
  `open` / `append` / `done`. Out: `POST /inherent/submit`,
  `POST /inherent/cancel-response` with `scope: foreground_output`,
  `POST /inherent/controls` (D2). No audio crosses the wire; the daemon owns
  mic and speaker, Resonance records nothing and plays no speech.
- Additive wire change: the `open` payload carries `response_id` whenever the
  `surface.response_open` event has one, so a v1 client can target
  `cancel-response`. Legacy events without it are unchanged.
- Phase mapping: daemon `listening` → UI `hearing` (active waveform);
  `transcribing` / `accepted` / `open` → `processing`; first `append` →
  `speaking` (or stays `processing` when speech is muted); `spoken`, `empty`,
  `error` → resting `listening`; `done` fades the reply text `fadeMs` later
  and settles the phase if no `spoken` arrived (text-only daemons).
  WebSocket loss → `error` panel with a 1/2/4/8/16 s reconnect ladder.
- Port: `JARVIS_INHERENT_BRIDGE_PORT` (default 8006), handed to the renderer
  as the `port` query by Electron. `--lab` and `--verify` runs get no port and
  keep the prototype's simulation; the two sources never run together.
- The v2 wire (`realtime.inherent.v2_sequencer`) stays off. Resonance adopts
  it when it needs durable views or client-originated frames.
- `desktop/inherent-swift/`, `scripts/test_inherent_swift.sh` and the Swift
  test rule are deleted in one commit; history keeps them.

### D2. Mute switches: `POST /inherent/controls`

- Body `{"mic_muted"?: bool, "speech_muted"?: bool}`; a missing field leaves
  that switch alone, so `{}` is a pure read. The response is always the full
  state `{"mic_muted": bool, "speech_muted": bool}`, and the surface treats
  every answer as authoritative (it syncs with `{}` on each connect).
- `mic_muted`: the wake owner drops detections instead of arming an utterance,
  in both `DuplexVoiceSession._drain_detection_commands` and the legacy
  `WakeListener`. Barge-in while Jarvis is speaking never reaches that queue,
  so Allen can still interrupt Jarvis mid-sentence while muted; the PTT upload
  route is untouched.
- `speech_muted`: the TTS player's output gain goes to 0.0 over a 10 ms ramp
  and back to 1.0 on unmute (`AudioStreamPlayer.set_gain`, reached through
  `set_output_gain` on both pipelines). Synthesis, timing, phases and events
  run exactly as unmuted; only the speaker is silent, so a mute lands within
  one audio block and an unmute mid-sentence resumes audibly. The audibility
  ledger classifies those blocks `muted`, so `heard_text` excludes them:
  "heard" means audible, text read on screen is another channel.
  Rejected the same day: not synthesizing a turn opened while muted (the
  first cut). Allen: mute is the volume switch, not a synthesis switch, and
  the TTS cost of a muted turn is accepted.
- State lives in `jarvis/surface/voice_controls.VoiceControls`, in memory,
  outside the event log. It is operational UI state like `voice_capability`,
  not a durable fact about the world; a daemon restart comes back unmuted and
  the surface re-syncs. No spec deviation: §3 L2 governs durable state.

### D3. Residency: `jarvis daemon install` installs two LaunchAgents

- `com.allen.jarvis` is unchanged (ADR-0009 D1). `com.allen.jarvis.resonance`
  runs `[<node>, scripts/launch.mjs]` with `WorkingDirectory`
  `<repo>/desktop/resonance`, `RunAtLoad` and `KeepAlive` true,
  `ThrottleInterval` 10, logs at `~/.jarvis/logs/resonance.{out,err}.log`,
  and `EnvironmentVariables.PATH` = node's own bin directory plus the system
  directories, because launchd's PATH carries no node and npm's shims are
  `#!/usr/bin/env node`.
- `launch.mjs` rebuilds when any source is newer than the build (the same
  staleness rule the Swift launcher gained in 742bc2a), then runs Electron
  attached so launchd owns its lifetime. Both agents run from the checkout;
  there is no packaged app. After a merge to main the one command is
  `jarvis daemon restart`: `launchctl kill TERM` on both, each exits cleanly
  and `KeepAlive` respawns it; an agent that is not running is `kickstart`ed.
- `install` validates the interpreter (as before), `node`, and
  `desktop/resonance/node_modules/electron` (telling you to `npm ci`), and
  refuses while a manual daemon holds `daemon.lock`, since KeepAlive would
  otherwise respawn-loop against it. `uninstall` boots out both; `status`
  reports both.
- Rejected: launchd `WatchPaths` on `.git` (lanes commit to main constantly;
  every commit would kill an in-flight turn), packaging a `.app` (a frozen
  snapshot contradicts "always the merged version"), the daemon spawning
  Electron as a child (a daemon crash would drop the window; the surface
  reconnects on its own instead).

## 3. Acceptance

- Hermetic: `tests/integration/test_inherent_controls.py` asserts the
  `/inherent/controls` wire (read, partial update, full state, route absent
  without deps) and follows the speech switch to the PCM the player hands the
  device (ones, a 10 ms ramp to exact zeros, ones again); `tests/integration/test_launchd_install.py` asserts both
  rendered plists and the `bootout → bootstrap → enable` sequence per label
  against a fake `launchctl`.
- Live, against an isolated daemon (`--runtime-root ~/.jarvis-resonance-test
  --port 8016`, no MiniMax key, wake disabled):
  `desktop/resonance/scripts/verify-runtime.mjs` types a turn through the real
  composer, uploads a synthesized utterance to `/inherent/asr-submit`, flips
  both mutes and reads them back from the daemon, reloads the window and sees
  the state re-sync, streams a speech-muted turn still through `speaking`,
  and shows the reconnect panel when the daemon is SIGTERMed.
- Live, on Allen's Mac from the main checkout after merge: stop the manual
  daemon, `npm ci` in `desktop/resonance`, `jarvis daemon install`, speak to
  Jarvis, then `git pull && jarvis daemon restart`.

## 4. Consequences

- Once the agents are installed a manual `jarvis serve` needs
  `--force-manual` (ADR-0009 D1 guard); test rigs keep using other ports and
  runtime roots.
- Swift tooling (xcodegen, xcodebuild, the `**/*.swift` test rule) leaves the
  repo; the only remaining `.swift` file is the AEC spike script.
- Open, deliberately not in this ADR: a measured audio level for the waveform
  (the daemon would push an ephemeral `level`), notifications fed by real task
  results, and Resonance on the v2 wire.
