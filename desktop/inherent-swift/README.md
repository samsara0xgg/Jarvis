# Inherent — Swift edition

Replacement host for the inherent summon card. Replaces `desktop/inherent/inherent-main.js`.

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

24 tests across 6 suites:
- DisplayMathTests (4)
- FadeControllerTests (3)
- SubmitRequestTests (6)
- BridgeDispatchTests (6)
- ReconnectBackoffTests (2)
- DisplayManagerLookupTests (3)

## Hotkey

⌘+Space toggles the card. macOS Spotlight binds ⌘+Space by default — disable
the Spotlight binding in System Settings → Keyboard → Keyboard Shortcuts → Spotlight.

## Architecture

- `CardPanel` — NSPanel subclass with `.stationary` collectionBehavior
- `CardWebView` — WKWebView host loading `desktop/inherent/card.html`
- `IPCBridge` — JS↔Swift via WKScriptMessageHandler + WithReply for `card:submit`
- `BridgeBackend` — WS client + HTTP submit, 5-step backoff + 30s watchdog
- `HotkeyManager` — Carbon RegisterEventHotKey
- `FadeController` — alpha animation with generation-counter cancel
- `ParentWatchdog` — kqueue NOTE_EXIT for dev-mode parent lifetime

## Fallback

The Electron version at `desktop/inherent/` remains intact. To use it instead:

```bash
cd desktop && npm run inherent
```
