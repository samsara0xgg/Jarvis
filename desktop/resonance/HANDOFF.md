# Resonance voice integration handoff

This is a standalone Electron surface. It currently makes no network requests,
does not record audio, does not connect to the Jarvis runtime, and does not
persist conversations. Keep UI refinement and runtime wiring scoped separately.

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

## Current seams

- `src/model.ts`: simulated reducer, examples, microphone/playback flags and
  listening/processing/speaking/error phases. These are local UI state, not
  acknowledgements from a backend. Replace simulated actions with confirmed
  runtime state when wiring.
- `src/main.tsx`: mock text-send/reply timers, inline notification reply timer,
  phase-to-presence mapping, and a settings-only demonstration sequence.
  Automatic presence rests quietly while waiting. Remove or gate mock timers
  when real events own the corresponding state; do not run both sources.
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
