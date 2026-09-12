# Resonance voice integration handoff

The desktop build talks to the Jarvis daemon over the Inherent v1 wire
(`src/runtime.ts`): `voice` phases and `open`/`append`/`done` reply envelopes
in over `ws://127.0.0.1:<port>/inherent/ws`, text (`/inherent/submit`) and
stop-speaking (`/inherent/cancel-response`, `foreground_output`) out over HTTP.
It records no audio and plays no speech; the daemon owns mic and speaker. The
port comes from `JARVIS_INHERENT_BRIDGE_PORT` (default 8006, same as the Swift
card) and reaches the renderer as the `port` query. `--lab` and `--verify` runs
get no `port` and keep the simulation; the two sources never run together.

## Run and verify

From the repository root: `cd desktop/resonance`, `npm ci`, `npm start`.
macOS native material needs Xcode Command Line Tools and Node C headers
(`NODE_INCLUDE` can override discovery). `npm run package` creates a local,
ad-hoc signed app; it is not a distribution or notarization pipeline.

Run `npm run verify`, then `node scripts/verify-presence.mjs` and
`node scripts/verify-notification-details.mjs` sequentially. They launch real
Electron windows with isolated verification storage and muted output. The last
UI iteration passed 45 + 17 + 16 checks. Reference recordings and generated
evidence are intentionally local; they are not required to run these checks.

Live link: start a daemon on a test port and run
`JARVIS_INHERENT_BRIDGE_PORT=8016 RESONANCE_TEST_WAV=<16 kHz mono wav>
RESONANCE_TEST_DAEMON_PID=<pid> node scripts/verify-runtime.mjs`. It types a
turn through the real composer, uploads the utterance to `/inherent/asr-submit`,
and SIGTERMs the daemon to prove the reconnect panel. Never point it at the
real 8006 daemon.

## Current seams

- `src/model.ts`: reducer and examples. With a runtime, `open`/`append`/
  `settle` from `runtime.ts` own phase and reply; `hearing` is the daemon's
  `listening` (utterance capture) and maps to the `listening` presence. The
  microphone and playback mute flags are still local UI state: the daemon has
  no mute endpoint yet, so those two buttons do not change capture or speech.
- `src/main.tsx`: mock text-send/reply timers and the speaking auto-timeout run
  only when `live` is false; the inline notification reply timer is still a
  mock. Reply text is shown with `<voice>`/`<document>` markup stripped.
- `src/VoicePresence.tsx`: receives a visual state and theme color. Geometry
  interpolates continuously; current phrase energy is synthetic. There is no
  real audio-level input yet. Add a measured level/envelope here when wiring
  without resetting the animation clock or changing the agreed proportions.
- `electron/preload.cts`: narrow IPC bridge for window layout/material, focus,
  drag, clipboard, and menu commands. No audio or runtime transport bridge yet.
- `electron/main.ts`: sandboxed, context-isolated renderer, all media permission
  requests denied. Real capture requires a deliberate recording/permission
  lifecycle, macOS usage description and packaged-app verification. Do not
  treat the prototype microphone toggle as capture authorization or success.
- `src/feedback.ts`: short operation cues, independent of speech playback mute.
  Real speech output needs its own playback/cancellation lifecycle. The five
  small WAV assets are filtered candidates from supplied references; auditory
  fidelity remains provisional.
- `src/preferences.ts`: only appearance and operation-sound preferences persist.

## Preserve during wiring

Voice controls are 270 × 40 logical pixels. The center is 90 × 36 (2.5:1);
icons remain 20 and detached buttons 40. Text expands to 330 around the same
center. Glass is neutral; only the waveform uses the selected theme color.
All solid capsule areas drag; drags suppress click actions. Transparent gaps
pass through. Microphone mute and speech-output mute are independent.
Multiple notifications overlap by default and expand on click, never on hover.
Reply input stays in its notification. Respect reduced motion, hidden-window
animation suspension, focus release, and the existing verification checks.

Core/runtime integration is intentionally not designed or implemented here.
Follow the repository's ownership boundaries when choosing the actual transport.
