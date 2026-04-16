# Jarvis Desktop (Pet Mode) — Design Spec

**Date**: 2026-04-16
**Status**: Approved, ready for implementation
**Owner**: Frontend / Electron subagents
**Related notes**: `notes/olv-deep-dive-2026-04-16.md` (source of architectural decisions)

---

## 1. Summary

Wrap the existing `ui/web/` Live2D frontend in an Electron shell to enable a desktop Pet Mode — a transparent, frameless, always-on-top floating Live2D character that supports click-through and multi-screen, inspired by Open-LLM-VTuber (OLV) v1.2 Pet Mode. In Window Mode, the Electron app presents the existing `ui/web/` UI unchanged.

**Backend is untouched**. The Electron renderer loads `http://localhost:8006` and talks to the existing FastAPI + SSE endpoints.

---

## 2. Goals & Non-Goals

### Goals (MVP)
- `desktop/` Electron project at repo root, independent `package.json`.
- Two modes, switched at runtime without destroying `BrowserWindow`:
  - **Window Mode** — standard bordered window, 100% of current `ui/web/` experience preserved.
  - **Pet Mode** — transparent, frameless, borderless, always-on-top, multi-screen virtual rectangle, click-through to non-model areas.
- Right-click context menu (ported verbatim from OLV's `main/menu-manager.ts`, user will refine later).
- Multi-screen support (Pet Mode window covers virtual desktop spanning all displays).
- Unsigned dmg buildable via `electron-builder`.

### Non-Goals (explicitly deferred)
- Floating chat panel / `⌘/` toggle / client-side command recognition — dropped for MVP.
- Subtitle / speech bubble.
- Code-signed dmg / auto-updates.
- Windows or Linux builds.
- Any backend Python changes (handled by a separate session; out of scope here).
- Any OLV WebSocket adapter — renderer talks to existing Jarvis SSE API.

---

## 3. Architecture

```
┌────────────────────────────────────────────────────────┐
│ Electron main process (desktop/main.js)                │
│  - BrowserWindow (single instance, mode switches in-  │
│    place via setBounds/setIgnoreMouseEvents/etc.)      │
│  - Cursor polling loop (16ms, main → IPC → renderer)  │
│  - Multi-screen virtual rect calc (Pet Mode)          │
│  - Right-click menu (Menu.buildFromTemplate)          │
│  - App lifecycle / tray / quit                        │
└─────────────┬──────────────────────────────────────────┘
              │ IPC via contextBridge (preload.js)
              ▼
┌────────────────────────────────────────────────────────┐
│ Renderer = ui/web/index.html (loaded from              │
│             http://localhost:8006)                     │
│  - Listens: cursor-update → Cubism hit-test → hover-  │
│    update reply                                        │
│  - Listens: mode-change → toggle body.pet-mode class  │
│  - Existing FastAPI + SSE flow to Jarvis, unchanged    │
└────────────────────────────────────────────────────────┘
```

**Prerequisite**: `python -m ui.web.server` running on port 8006 (not started by Electron; user or a launcher script runs it separately).

---

## 4. Directory Layout

```
jarvis/
├── desktop/                         (new)
│   ├── main.js                      ~250 LOC — window manager, polling, menu glue
│   ├── preload.js                   ~40 LOC — contextBridge IPC surface
│   ├── menu.js                      ~180 LOC — right-click menu template (OLV port)
│   ├── package.json                 electron, electron-builder deps
│   ├── electron-builder.yml         appId: com.jarvis.xiaoyue, productName: 小月
│   ├── build/
│   │   └── icon.png                 placeholder 512x512 (user provides real later)
│   └── README.md                    one-liner run instructions
│
├── ui/web/                          (minimal diff)
│   ├── css/test_page.css            +body.pet-mode rules
│   └── js/ui/controller.js          +IPC handler for mode change
│
└── .gitignore                       +desktop/node_modules/ +desktop/dist/
```

---

## 5. Detailed Design

### 5.1 Click-Through Polling Loop (CRITICAL — macOS correctness)

**Why this architecture**: On macOS, when `setIgnoreMouseEvents(true)` is active, the renderer receives zero mouse events — so hit-testing cannot be driven by renderer-side mousemove. `setIgnoreMouseEvents(true, {forward: true})` only works on Windows/Linux. Therefore main must **poll the cursor from the OS** and drive hit-testing through IPC.

**Reference source (MUST read before implementing)**: OLV `Open-LLM-VTuber-Web` repo, file `main/window-manager.ts`, polling section. This MUST be read from local clone at `~/Projects/external/Open-LLM-VTuber-Web/` (clone if not present: `git clone https://github.com/Open-LLM-VTuber/Open-LLM-VTuber-Web ~/Projects/external/Open-LLM-VTuber-Web`).

**Loop (implemented in `main.js`)**:

```
Pet Mode enter:
  lastHit = false
  setIgnoreMouseEvents(true, {forward: false})   // start fully click-through
  setInterval(16ms, () => {
    const {x, y} = screen.getCursorScreenPoint()
    const b = win.getBounds()
    if cursor outside window bounds: return
    const relX = x - b.x, relY = y - b.y
    win.webContents.send('cursor-update', {x: relX, y: relY})
    // renderer runs Cubism model.anyhitTest(relX, relY) and replies
  })

Renderer (via preload-exposed jarvis.onCursorUpdate):
  ipcRenderer.on('cursor-update', ({x, y}) => {
    const hit = window.chatApp?.live2dManager?.isHitOnModel?.(x, y) ?? false
    ipcRenderer.send('hover-update', hit)
  })

Main:
  ipcMain.on('hover-update', (_, hit) => {
    if (hit !== lastHit) {
      win.setIgnoreMouseEvents(!hit, {forward: false})
      lastHit = hit
    }
  })

Pet Mode exit:
  clearInterval(pollId)
  setIgnoreMouseEvents(false)
```

**`isHitOnModel(x, y)` implementation in `live2d.js`** (add new method):
Use existing `this.live2dModel.getBounds()` and Cubism's built-in `model.hitTest` if available, else fall back to bounds-rectangle test (loose but acceptable for MVP).

**Performance note**: 16ms poll + IPC round-trip is ~60fps, in line with OLV. If this causes CPU overhead in profiling, drop to 33ms (30fps) — acceptable for Pet UX.

### 5.2 Window Mode ↔ Pet Mode State Machine

Single `BrowserWindow` instance. Mode switch modifies properties in place:

| Property | Window Mode | Pet Mode |
|---|---|---|
| `transparent` | false (set at create) | true (set at create) |
| `frame` | false (set at create; our Window mode still hides OS chrome for consistency — can adjust) | false |
| `alwaysOnTop` | false | true |
| `hasShadow` | true | false |
| `resizable` | true | false |
| `bounds` | user's saved size/pos | virtual rectangle spanning all displays |
| `ignoreMouseEvents` | false | true (with polling override) |
| `backgroundColor` | `#FFFFFF` | `#00000000` |

**Important**: `transparent` and `frame` must be set at window creation and **cannot be changed at runtime on macOS**. So the shell always creates the window with `transparent:true, frame:false, hasShadow:false`. Window mode compensates visually via CSS (renderer sets opaque background) and we accept the absence of native frame/titlebar in Window mode — OLV does the same.

**Renderer sync**: After each mode switch, main calls `win.webContents.send('mode-change', 'pet' | 'window')`. Renderer toggles `document.body.classList.toggle('pet-mode', mode === 'pet')`.

### 5.3 Multi-Screen Virtual Rectangle (Pet Mode Bounds)

```
const displays = screen.getAllDisplays()
const left = Math.min(...displays.map(d => d.bounds.x))
const top = Math.min(...displays.map(d => d.bounds.y))
const right = Math.max(...displays.map(d => d.bounds.x + d.bounds.width))
const bottom = Math.max(...displays.map(d => d.bounds.y + d.bounds.height))
win.setBounds({x: left, y: top, width: right-left, height: bottom-top})
```

Listen to `screen.on('display-added' | 'display-removed' | 'display-metrics-changed')` to recompute bounds when monitors change.

### 5.4 Right-Click Menu (`menu.js`)

Port OLV's `main/menu-manager.ts` verbatim in spirit (translated to JS). Keep all OLV menu items as-is — user will customize later. Expected items (subject to OLV structure; final list is whatever OLV ships):

- Mode toggle (Window ↔ Pet)
- Model picker submenu (7 models)
- Background toggle
- Settings / Preferences
- About
- Quit
- Hide to tray

Bind via `webContents.on('context-menu', ...)`.

### 5.5 Preload IPC Surface

```js
// desktop/preload.js
const { contextBridge, ipcRenderer } = require('electron')
contextBridge.exposeInMainWorld('jarvis', {
  onModeChange: (cb) => ipcRenderer.on('mode-change', (_, mode) => cb(mode)),
  onCursorUpdate: (cb) => ipcRenderer.on('cursor-update', (_, pos) => cb(pos)),
  sendHover: (hit) => ipcRenderer.send('hover-update', hit),
  setMode: (mode) => ipcRenderer.send('set-mode', mode),
  quit: () => ipcRenderer.send('quit'),
  hideToTray: () => ipcRenderer.send('hide-to-tray'),
})
```

### 5.6 Renderer Integration (`ui/web/js/ui/controller.js`)

Add to `init()`:

```js
if (window.jarvis) {
  window.jarvis.onModeChange((mode) => {
    document.body.classList.toggle('pet-mode', mode === 'pet')
  })
  window.jarvis.onCursorUpdate(({x, y}) => {
    const live2d = window.chatApp?.live2dManager
    const hit = live2d?.isHitOnModel?.(x, y) ?? false
    window.jarvis.sendHover(hit)
  })
}
```

Add to `live2d.js`:

```js
isHitOnModel(x, y) {
  if (!this.live2dModel) return false
  const b = this.live2dModel.getBounds()
  return b && b.contains(x, y)
}
```

### 5.7 Pet Mode CSS Rules (`ui/web/css/test_page.css`)

Append:

```css
body.pet-mode {
  background: transparent !important;
}
body.pet-mode .background-container,
body.pet-mode .background-overlay,
body.pet-mode .chat-container,
body.pet-mode .control-bar,
body.pet-mode .connection-status-top,
body.pet-mode #logPanel {
  display: none !important;
}
body.pet-mode #live2d-stage {
  background: transparent !important;
}
```

Live2D `PIXI.Application` is already created with `backgroundAlpha: 0`, so canvas transparency works out of the box.

### 5.8 Packaging (`electron-builder.yml`)

```yaml
appId: com.jarvis.xiaoyue
productName: 小月
mac:
  target: dmg
  category: public.app-category.utilities
  identity: null            # unsigned
  hardenedRuntime: false
directories:
  output: dist
files:
  - main.js
  - preload.js
  - menu.js
  - package.json
  - build/**/*
```

`npm run dist` produces `dist/小月-0.1.0.dmg`.

---

## 6. Dependencies

```json
{
  "name": "jarvis-desktop",
  "version": "0.1.0",
  "main": "main.js",
  "scripts": {
    "start": "electron .",
    "dev": "electron .",
    "dist": "electron-builder --mac"
  },
  "devDependencies": {
    "electron": "^33.0.0",
    "electron-builder": "^25.0.0"
  }
}
```

TypeScript is **not used** (per user direction).

---

## 7. User Workflow

```
# one-time setup
cd /Users/alllllenshi/Projects/jarvis/desktop
npm install

# dev run (Jarvis web server must be running separately on :8006)
python -m ui.web.server &
cd desktop && npm start

# build dmg
cd desktop && npm run dist

# install produced dmg — first launch:
xattr -dr com.apple.quarantine "/Applications/小月.app"
```

---

## 8. Verification Checklist

Subagents must confirm each of these before claiming done:

- [ ] `desktop/` directory created with all files listed in §4
- [ ] `npm install` succeeds
- [ ] `npm start` launches an Electron window that loads `http://localhost:8006` and shows the existing Live2D UI
- [ ] Right-click the model → OLV-style menu appears
- [ ] Menu → switch to Pet Mode → background disappears, chat/controls hidden, model floats, always on top
- [ ] Mouse cursor over the model → cursor becomes interactive (can click); cursor outside model → click passes through to underlying app
- [ ] Multi-monitor: dragging model across screens works without getting trapped at display edge (verify only if user has multi-monitor)
- [ ] Menu → switch back to Window Mode → full UI returns
- [ ] `npm run dist` produces a dmg under `desktop/dist/`
- [ ] `ui/web/` changes are purely additive — browser-only usage (without Electron) still works identically

---

## 9. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| macOS transparent-window + Live2D canvas render bugs | OLV runs this combo in production; follow their `webPreferences` settings |
| 16ms cursor polling + IPC overhead | Measurable; drop to 33ms if CPU > 2% idle |
| Live2D `isHitOnModel` precision is rectangle not alpha-contour | Acceptable for MVP; upgrade to Cubism `anyhitTest` or alpha sampling later |
| OLV source breaking changes / unavailable clone | Mitigation: clone specific tag `v1.2.1` if main branch has drifted |
| User has no monitors during verification | Mark multi-screen test as manual / best-effort |

---

## 10. References

- Deep dive notes: `notes/olv-deep-dive-2026-04-16.md` §4 (Pet Mode internals), §7-9 (verification)
- OLV source clone target: `~/Projects/external/Open-LLM-VTuber-Web/` (clone from `https://github.com/Open-LLM-VTuber/Open-LLM-VTuber-Web`)
- Key OLV files to read:
  - `main/window-manager.ts` — Pet Mode window state machine + cursor polling
  - `main/menu-manager.ts` — right-click menu template
  - `use-live2d-model.ts` — drag / resize handling (renderer-side, already covered by existing `ui/web/js/live2d/live2d.js`)

---

## 11. Future Work (Not MVP)

Items deferred and designed to be added incrementally without restructuring:
- Floating chat panel with Liquid Glass style + 5-line history + command recognition (dropped from MVP per user call, user may request later)
- Subtitle / speech bubble
- Code signing + notarization
- Auto-update via electron-updater
- Custom icon artwork
- Per-pixel alpha hit-test (upgrade from rectangle bounds)
- Pet Mode window-position persistence per-display
