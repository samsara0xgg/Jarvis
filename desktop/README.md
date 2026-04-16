# Jarvis Desktop (小月)

Electron shell that wraps Jarvis's existing Live2D web UI (`ui/web/`) and adds
a transparent, click-through, always-on-top **Pet Mode**.

The backend is untouched: the Electron renderer simply loads
`http://localhost:8006`.

## Prerequisites

1. Node 18+ and npm.
2. The Jarvis web server running:

   ```bash
   cd /Users/alllllenshi/Projects/jarvis
   python -m ui.web.server
   ```

   It listens on port 8006 by default. Override with
   `JARVIS_WEB_URL=http://host:port` when launching Electron.

## Install

```bash
cd /Users/alllllenshi/Projects/jarvis/desktop
npm install
```

## Run (dev)

```bash
npm start
```

- Right-click the window for the context menu — switch between **Window Mode**
  and **Pet Mode**, toggle mic / interrupt, hide to tray, quit.
- In Pet Mode the window becomes transparent and click-through; only the Live2D
  model hit-region captures the mouse.

## Build (unsigned dmg)

```bash
npm run dist
```

Output: `desktop/dist/小月-0.1.0.dmg`.

Because the build is unsigned, macOS Gatekeeper will quarantine it on first
install. Strip the quarantine attribute:

```bash
xattr -dr com.apple.quarantine "/Applications/小月.app"
```

## Icon

`build/icon.png` is a placeholder. Replace with a real 512×512 PNG before
shipping — `electron-builder` uses the same file for the app bundle and dmg
background.

## Files

| File                   | Purpose                                                        |
| ---------------------- | -------------------------------------------------------------- |
| `main.js`              | BrowserWindow, mode state machine, cursor polling, tray        |
| `preload.js`           | `contextBridge` bridge exposing the `window.jarvis` IPC object |
| `menu.js`              | Right-click context menu (ported from OLV menu-manager.ts)     |
| `package.json`         | Electron + electron-builder dev deps                           |
| `electron-builder.yml` | dmg packaging config                                           |
| `build/icon.png`       | Placeholder app icon                                           |
