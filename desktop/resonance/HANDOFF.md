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

Run `npm run verify` for the current desktop UI. The check launches a real
Electron window with isolated verification storage and muted output. Use
`node scripts/verify-desktop-orbit.mjs --packaged` for the packaged application.
`verify-live-morph.mjs` checks the enlarged preview separately. Older scripts
that select `.voice-pill` target the previous UI and are historical checks.
Generated evidence is local and is not required to run the current checks.

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
  microphone and playback mute buttons POST `/inherent/controls`; the daemon
  owns that state (ADR-0015 D2) and every answer, including the `{}` sync on
  connect, drives the buttons. Muted speech is the daemon player's gain at
  zero: the reply still runs through `speaking` and the stop button still works.
  `live` and `subtitles` mirror the daemon's GPT-Live session (`live` op and
  the `live` field of every controls answer); subtitle deltas are merged per
  role for display only and dropped when their session id is not current.
- `src/main.tsx`: mock text-send/reply timers and the speaking auto-timeout run
  only when `live` is false; the inline notification reply timer is still a
  mock. Reply text is shown with `<voice>`/`<document>` markup stripped.
  The UI starts collapsed; clicking the disc expands the capsule. Closing it
  returns to the disc without hiding or clearing existing results. The status
  line carries the GPT-Live toggle (start / hang up with a per-second clock).
- `src/PresentationCapsule.tsx` and `src/LivePresence.tsx`: controlled mode,
  non-Live status, voice presence, theme color, and UI callbacks. Ring/waveform
  energy remains synthetic. `nativeSurface` enables native-glass geometry updates
  and draggable hit regions; `active` suspends the hidden canvas during text input.
  `VoicePresence.tsx` preserves the earlier waveform study and shared state types.
  Under a GPT-Live session the presence follows the daemon's `speaking`
  (local player draining) and `hearing` (recent user transcript) bits.
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

Non-Live is 120 × 40; Live controls are 270 × 40 logical pixels. The center is 90 × 36 (2.5:1);
icons remain 20 and detached buttons 40. Text expands to 330 around the same
center. Glass is neutral; only the waveform uses the selected theme color.
All solid capsule areas drag; drags suppress click actions. Transparent gaps
pass through. Microphone mute and speech-output mute are independent.
Multiple notifications overlap by default and expand on click, never on hover.
Reply input stays in its notification. The approved voice animation explicitly
plays under reduced motion; other UI transitions still follow the system setting.
Preserve hidden-window animation suspension, focus release, and verification checks.

Core/runtime integration is intentionally not designed or implemented here.
Follow the repository's ownership boundaries when choosing the actual transport.

## Current selected desktop design

Selected concept 01: a static, flat nine-pixel disc, without outline, gradient,
glow, or breathing. Hover slightly brightens it. Rest rendering suspends once
settled, and resumes for mode/color/visibility changes. Clicking the disc morphs
to Live; clicking the voice returns to the compact disc. Desktop uses the
controlled presentation path again; `waveOnly` remains an unused alternative.
No tests were run for this visual selection at the user's request.

## Archived Non-Live / Live UI experiment

`PresentationCapsule.tsx` is controlled by `presentation` (`collapsed` or
`expanded`), `restState`, and Live `presence`. The caller owns actual mode
selection. Clicking the center reports `onActivate` / `onCollapse`; microphone
and speaker toggles are independent callbacks. Message and notification buttons
also expose UI-intent callbacks. The desktop wires these to its existing simulated reducer and message/notification
UI. The independent preview uses local state only.

Non-Live shows message, center disc, and notification inside a 120 × 40
capsule. Message and notification move continuously to detached outer positions
while the core expands to 174 × 40 and fades in the microphone and speaker,
for a total 270 × 40 group. Icons remain 20 pixels. Only mode changes alter
width; both voice states and non-Live states keep their respective widths.

`LivePresence.tsx` uses a single persistent canvas. Non-Live is now a single
nine-pixel flat disc; the earlier
double ring, bright arc, and orbit dot are removed from this desktop shape. The two halves of each ring
converge onto the waveform while the same critically damped progress drives
shell width, gaps, and side-button reveal. Interrupted motion retains position
and velocity. Ring position and waveform phases continue across mode changes.
The preview supplies `renderScale=2.5` for its enlarged sample. Canvas resolution
accounts for display scale and device pixel ratio, with 1.5× supersampling;
curve sampling also scales up. Rendering pauses while hidden. This intentional motion preview animates under
the current system reduced-motion preference.

`OrbitPreview.tsx` preserves the earlier six-state circle study, accessible from
the preview footer. `OrbitPresence.tsx` remains its standalone circle component.
The current non-Live choices are standby, background processing, notification,
and unavailable; Live retains the five voice states. All activity is simulated.
No microphone, wake-word, runtime, or business logic was added. This worktree’s
desktop app now uses the approved UI; other sessions and their checkouts are unchanged.

Run `node scripts/verify-live-morph.mjs --packaged` to verify actual intermediate
widths, common shell/canvas progress, stable center/icon sizes, rapid reversals,
correct controls in each mode, independent mute controls, raster density, and
the preserved gallery. Evidence is written to
`evidence/live-morph/`. `verify-presentation.mjs` runs the same current checks.

## Notification motion refinement

Notification cards remain mounted through stack/detail transitions. Geometry
uses a shared 420 ms easing, with content fading after movement begins. Inbox
closing retains the DOM for 260 ms; dismissal completes before removal. Native
material tracks transitions, entrance animations, and scrolling. Opening an
inbox requests keyboard focus: Escape closes a detail first, then collapses
expanded cards, then closes the inbox. These UI transitions remain enabled
under the current reduced-motion preference, consistent with the approved
presentation motion. Bounded window observation is in
`evidence/notification-motion/review.json`; no runtime wiring was changed.
