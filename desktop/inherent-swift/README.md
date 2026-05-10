# Inherent — Native Swift edition

Native SwiftUI replacement for the inherent summon card. The app target no
longer embeds or loads `desktop/inherent/card.html`, `card.js`, `card.css`, or
the old WKWebView bridge resources; those files remain in the repo only as the
Electron/web reference implementation.

## Run (dev)

```bash
python desktop/inherent-swift/launcher.py
```

The launcher regenerates `InherentCard.xcodeproj` from `Project.yml` if needed,
builds the .app if missing, then spawns it with `JARVIS_PROJECT_ROOT` and
`JARVIS_INHERENT_PARENT_LIFETIME` set. Ctrl+C terminates the card.

The web backend (`ui/web/server.py`) must be running separately on port 8006.

## Run (Xcode)

Open `InherentCard.xcodeproj` (run `xcodegen generate` first), set the scheme
environment variables `JARVIS_PROJECT_ROOT=/abs/path/to/jarvis` in
Edit Scheme → Run → Arguments, then ⌘R.

## Test

```bash
cd desktop/inherent-swift
xcodebuild test -project InherentCard.xcodeproj -scheme InherentCard -derivedDataPath build
```

Native visual smoke harness:

```bash
INHERENT_DEBUG_FAKE_TURNS=1 python desktop/inherent-swift/launcher.py
INHERENT_DEBUG_FAKE_TURNS=collapse python desktop/inherent-swift/launcher.py
INHERENT_DEBUG_FAKE_TURNS=fade python desktop/inherent-swift/launcher.py
```

Tests cover the existing bridge/backend math plus native model transitions:
- DisplayMathTests (4)
- FadeControllerTests (3)
- SubmitRequestTests
- BridgeDispatchTests (6)
- ReconnectBackoffTests (2)
- DisplayManagerLookupTests (3)
- NativeCardModelTests

## Hotkey

⌘+Space toggles the card. macOS Spotlight binds ⌘+Space by default — disable
the Spotlight binding in System Settings → Keyboard → Keyboard Shortcuts → Spotlight.

## Architecture

- `CardPanel` — NSPanel subclass with `.stationary` collectionBehavior
- `NativeCardController` — NSPanel + SwiftUI host, height tracking, passthrough hit testing, hotkey
- `NativeCardModel` — Swift-native card state machine replacing `card.js`
- `NativeCardView` — SwiftUI card, history strip, popover, attachment UI, answer rendering
- `NativeVoiceRecorder` — AVAudioEngine WAV capture replacing renderer AudioWorklet capture
- `BridgeBackend` — Swift WS client + HTTP submit/image/asr endpoints, 5-step backoff + 30s watchdog
- `HotkeyManager` — Carbon RegisterEventHotKey
- `FadeController` — alpha animation with generation-counter cancel
- `ParentWatchdog` — kqueue NOTE_EXIT for dev-mode parent lifetime

## Fallback

The Electron version at `desktop/inherent/` remains intact. To use it instead:

```bash
cd desktop && npm run inherent
```
