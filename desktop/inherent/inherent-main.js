// inherent-main.js — Electron main entry for Jarvis "inherent" mode.
//
// Owns a single Siri-style floating card. Visual surface is CSS-driven
// (background tint + backdrop-filter blur + inset highlights in card.css);
// macOS provides the outer drop-shadow via BrowserWindow.hasShadow:true so
// the click area stays tight to the visible card (no transparent margin).
//
// Wave 1 (2026-05-06): adds hotkey-driven text input. ⌘+Space → openInputMode
// reveals the card focused-with-input; Enter POSTs to /inherent/submit on the
// jarvis web backend; the response surfaces back via the same response.* →
// /inherent/ws → siri:* path that the response-only mode used.
//
// IPC contract:
//   main → renderer (webContents.send):
//     siri:open       { content: markdown, kind?: 'text'|'code'|'mixed' }
//     siri:append     { token: string }    (streaming, path 3)
//     siri:done       { fadeMs?: number }
//     siri:reset                            (clear card before next turn)
//     card:openInput                        (hotkey hit, enter input mode)
//
//   renderer → main (ipcRenderer.invoke):
//     card:resize       height_px
//     card:setWidth     width_px   (widens leftward, right edge fixed — for popover)
//     card:show
//     card:close
//     card:fadeOut      ms
//     card:cancelFade
//     card:submit       text   → POST /inherent/submit, returns {ok, reason?}
//     card:submitImage  payload→ POST /inherent/image-submit, returns {ok, reason?}
//     card:submitVoice  wav    → POST /inherent/asr-submit, returns {ok, status, text?}
//     card:duckAudio           → mute system output during microphone capture
//     card:restoreAudio        → restore system output after microphone capture

const { app, BrowserWindow, screen, ipcMain, globalShortcut, session } = require('electron');
const { execFile } = require('child_process');
const path = require('path');
const liquidGlass = require('electron-liquid-glass');
const WebSocketClient = require('ws');

const argv = process.argv;
const demoArg = argv.find(a => a.startsWith('--demo='));
const demoMode = demoArg ? demoArg.split('=')[1] : null;
const scenarioArg = argv.find(a => a.startsWith('--scenario='));
const scenario = scenarioArg ? scenarioArg.split('=')[1] : null;

const CARD_WIDTH = 360;
const CARD_INITIAL_HEIGHT = 120;
const CARD_MARGIN = 16;
const CORNER_RADIUS = 30;
const TINT_COLOR = '#00000010';

// Bridge to jarvis Python backend (ui/web/server.py:/inherent/ws). Receives
// {op, payload} JSON for siri:open/append/done/reset. The backend hosts the
// authoritative state — we just dispatch to the local siri* handlers.
const INHERENT_WS_URL = 'ws://127.0.0.1:8006/inherent/ws';
const RECONNECT_BACKOFF_MS = [1000, 2000, 4000, 8000, 16000];
// Watchdog: backend may crash after siri:open without ever sending siri:done.
// Without this, turnState would stick at 'open' and every subsequent open
// would silently auto-reset. 30s covers the longest expected LLM responses
// (cloud streaming + tool-use loops) with margin.
const BRIDGE_WATCHDOG_MS = 30000;

let audioDuckDepth = 0;
let audioDuckSnapshot = null;

function runOsascript(script) {
  return new Promise((resolve, reject) => {
    execFile('/usr/bin/osascript', ['-e', script], { timeout: 2000 }, (err, stdout) => {
      if (err) {
        reject(err);
        return;
      }
      resolve(String(stdout || '').trim());
    });
  });
}

function parseVolumeSnapshot(raw) {
  const parts = String(raw || '').trim().split(',').map(part => part.trim());
  if (parts.length !== 2) throw new Error(`unexpected volume settings: ${raw}`);
  const outputVolume = Math.max(0, Math.min(100, Number.parseInt(parts[0], 10)));
  if (!Number.isFinite(outputVolume)) throw new Error(`unexpected output volume: ${raw}`);
  return {
    outputVolume,
    outputMuted: parts[1].toLowerCase() === 'true'
  };
}

async function duckSystemAudio() {
  if (process.platform !== 'darwin') return { ok: false, reason: 'unsupported_platform' };
  if (audioDuckDepth > 0) {
    audioDuckDepth += 1;
    return { ok: true };
  }
  let snapshot = null;
  try {
    snapshot = parseVolumeSnapshot(await runOsascript(`
      set s to get volume settings
      return (output volume of s as text) & "," & (output muted of s as text)
    `));
    await runOsascript(`
      set volume output volume 0
      try
        set volume output muted true
      end try
    `);
    audioDuckSnapshot = snapshot;
    audioDuckDepth = 1;
    return { ok: true };
  } catch (err) {
    if (snapshot) {
      await runOsascript(`
        set volume output volume ${snapshot.outputVolume}
        try
          set volume output muted ${snapshot.outputMuted ? 'true' : 'false'}
        end try
      `).catch(() => {});
    }
    audioDuckSnapshot = null;
    audioDuckDepth = 0;
    console.warn(`[audio-ducking] failed to duck system output: ${err?.message}`);
    return { ok: false, reason: 'duck_failed' };
  }
}

async function restoreSystemAudio(force = false) {
  if (audioDuckDepth <= 0 && !force) return { ok: true };
  if (!force) {
    audioDuckDepth -= 1;
    if (audioDuckDepth > 0) return { ok: true };
  } else {
    audioDuckDepth = 0;
  }
  const snapshot = audioDuckSnapshot;
  audioDuckSnapshot = null;
  if (!snapshot || process.platform !== 'darwin') return { ok: true };
  try {
    await runOsascript(`
      set volume output volume ${snapshot.outputVolume}
      try
        set volume output muted ${snapshot.outputMuted ? 'true' : 'false'}
      end try
    `);
    return { ok: true };
  } catch (err) {
    console.warn(`[audio-ducking] failed to restore system output: ${err?.message}`);
    return { ok: false, reason: 'restore_failed' };
  }
}

let card = null;
let glassId = null;
// fadeGen: bumped to cancel an in-flight fade tick chain. Each card:fadeOut captures
// myGen = ++fadeGen; the tick checks myGen !== fadeGen and bails if superseded.
let fadeGen = 0;
// turnState gates contract violations: open/append/done outside the expected order
// just warn rather than crash — useful when backend has bugs and we want a clear log.
let turnState = 'idle';  // 'idle' | 'open'
// userHidden: set true when the user explicitly hid the card (⌘+Space toggle off,
// or fade-out completion). Renderer-driven implicit shows (cardAPI.show called
// from flushHeight during streaming, etc.) are blocked while this is true.
// Cleared on explicit user action (openInputMode) or backend turn (siriOpen).
let userHidden = false;

// Bridge state
let wsClient = null;
let reconnectAttempt = 0;
let reconnectTimer = null;
let watchdogTimer = null;
let bridgeShuttingDown = false;

// ─── Diagnostic state tracking ───────────────────────────────
// Electron has no getter for ignoreMouseEvents; track it ourselves so logs
// reflect the current state. Updated whenever we call setIgnoreMouseEvents.
let ignoreMouseEventsState = false;
function setIgnoreMouseEventsTracked(value) {
  if (!card || card.isDestroyed()) return;
  card.setIgnoreMouseEvents(value);
  ignoreMouseEventsState = value;
}

// ─── Sticky display anti-teleport ────────────────────────────
// macOS auto-relocates panel-type windows back to the primary display after
// focus / show events (likely tied to setVisibleOnAllWorkspaces + focus-
// stealing show()). Trace evidence: explicit setBounds to display 1
// succeeded, then ~100ms later an os:move event teleported the panel to
// display 2 with no setBounds call from us. To counter: stamp a sticky
// display id on every explicit reposition, and in the os:move handler,
// if the panel is found on a non-sticky display within the sticky window,
// move it back. lastSetBounds disambiguates our own moves from OS moves
// to avoid an infinite ping-pong.
let stickyDisplayId = null;
let stickyExpiresAt = 0;
let lastSetBounds = null;
const STICKY_DURATION_MS = 3000;

function applyBounds(target) {
  if (!card || card.isDestroyed()) return;
  lastSetBounds = { ...target };
  card.setBounds(target);
}

function setStickyDisplay(displayId) {
  stickyDisplayId = displayId;
  stickyExpiresAt = Date.now() + STICKY_DURATION_MS;
}

function isOurMove(currentBounds) {
  if (!lastSetBounds) return false;
  return (
    currentBounds.x === lastSetBounds.x &&
    currentBounds.y === lastSetBounds.y &&
    currentBounds.width === lastSetBounds.width &&
    currentBounds.height === lastSetBounds.height
  );
}

function maybeAntiTeleport() {
  if (!card || card.isDestroyed()) return;
  if (Date.now() > stickyExpiresAt) return;
  if (!stickyDisplayId) return;
  const bounds = card.getBounds();
  if (isOurMove(bounds)) return;
  const cardCenter = { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 };
  const currentDisplay = screen.getDisplayNearestPoint(cardCenter);
  if (currentDisplay.id === stickyDisplayId) return;
  // OS teleported the panel off the sticky display. Bring it back.
  const target = screen.getAllDisplays().find(d => d.id === stickyDisplayId);
  if (!target) return;
  const { workArea } = target;
  const fixed = {
    x: workArea.x + workArea.width - bounds.width - CARD_MARGIN,
    y: workArea.y + CARD_MARGIN - 38,
    width: bounds.width,
    height: bounds.height,
  };
  console.log(`[fix] anti-teleport: os moved card to display ${currentDisplay.id}, restoring to sticky ${stickyDisplayId}`);
  applyBounds(fixed);
}

function snapshotState() {
  if (!card || card.isDestroyed()) return { card: 'none' };
  const cursor = screen.getCursorScreenPoint();
  const cursorDisplay = screen.getDisplayNearestPoint(cursor);
  const bounds = card.getBounds();
  const cardCenter = { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 };
  const cardDisplay = screen.getDisplayNearestPoint(cardCenter);
  return {
    bounds,
    opacity: card.getOpacity(),
    isVisible: card.isVisible(),
    isFocused: card.isFocused(),
    ignoreMouseEvents: ignoreMouseEventsState,
    cursor,
    cursorDisplay: cursorDisplay.id,
    cardDisplay: cardDisplay.id,
    sameDisplay: cursorDisplay.id === cardDisplay.id,
    userHidden,
    turnState,
  };
}

function logState(event, extra = {}) {
  console.log(`[state] ${event}`, JSON.stringify({ ...snapshotState(), ...extra }));
}

function createCardWindow() {
  const display = screen.getPrimaryDisplay();
  const { workArea } = display;

  card = new BrowserWindow({
    width: CARD_WIDTH,
    height: CARD_INITIAL_HEIGHT,
    x: workArea.x + workArea.width - CARD_WIDTH - CARD_MARGIN,
    // Window y is shifted up by (PILL_RESERVED - CARD_MARGIN) so the body's
    // 38px top padding can host the pill flush above the card while the card
    // itself stays at its original screen position (workArea.y + CARD_MARGIN).
    // PILL_RESERVED (=38) lives in card.css as body padding-top.
    y: workArea.y + CARD_MARGIN - 38,
    transparent: true,
    frame: false,
    // hasShadow: false — CSS-side outer drop-shadow was tried and removed
    // (clipped at window edge → visible dark frame on bright backdrops).
    // The dark glass tint + inset highlights carry visual presence; depth
    // can be re-attempted later via macOS Vibrancy when geometry caching
    // issues in electron-liquid-glass are resolved.
    hasShadow: false,
    alwaysOnTop: true,
    resizable: false,
    vibrancy: false,
    // focusable: true so the hotkey-driven input mode can take keystrokes.
    // Response-only siri:* paths still call setCardVisible (showInactive),
    // which avoids stealing focus from whatever app the user was using.
    focusable: true,
    // type: 'panel' was removed because macOS NSPanel has built-in
    // auto-positioning logic (follows cursor display or sticks to menubar
    // display depending on collectionBehavior) that fights any explicit
    // setBounds. Plain BrowserWindow → NSWindow has no such auto-positioning.
    // Trade-off: showInactive() may behave slightly differently re: focus,
    // but focusable:true + frame:false + alwaysOnTop:floating still gives us
    // a Siri-style summon panel.
    show: false,  // first card:show IPC reveals it
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'inherent-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false
    }
  });

  // 'floating' (= NSFloatingWindowLevel) is enough to keep the card above app
  // windows. Earlier we used 'screen-saver' (= CGShieldingWindowLevel) but on
  // multi-display setups that level + type:'panel' caused the OS to shunt all
  // hit-testing on the card's display to the panel — making the rest of that
  // display unclickable when focus moved to a different display.
  card.setAlwaysOnTop(true, 'floating');
  // visibleOnAllWorkspaces is needed so the card surfaces on whichever macOS
  // Space the user is on (full-screen apps included). When type: 'panel' was
  // set, this combination triggered OS auto-relocation toward the menubar
  // display; on a plain NSWindow the auto-positioning logic doesn't apply.
  card.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  // setMovable(false) was tried here to stop OS auto-positioning during
  // streaming. It "worked" but introduced a hard regression: the entire host
  // display (the Mac internal screen) became unclickable. Removed.

  const cardPath = path.join(__dirname, 'card.html');
  const urlParams = new URLSearchParams();
  if (demoMode) urlParams.set('demo', demoMode);
  if (scenario === 'drip-fade') urlParams.set('animateChars', '1');
  const qs = urlParams.toString();
  const url = qs ? `file://${cardPath}?${qs}` : `file://${cardPath}`;
  card.loadURL(url);

  session.defaultSession.setPermissionRequestHandler((webContents, permission, callback) => {
    const isCard = card && !card.isDestroyed() && webContents.id === card.webContents.id;
    if (isCard && (permission === 'media' || permission === 'microphone')) {
      callback(true);
      return;
    }
    callback(false);
  });

  // NSGlassEffectView (electron-liquid-glass) is intentionally NOT applied:
  // its geometry caching during rapid window resizes (drip streaming) leaves
  // misaligned rounded corners that show as stray "ear" shapes on the sides
  // of the card. CSS handles glass surface (backdrop-filter blur+saturate)
  // and inset highlights — geometry follows the .card element directly, no
  // caching gap.

  card.on('closed', () => { card = null; glassId = null; });

  // OS-level lifecycle: log anything that could move/show/hide the window
  // outside our explicit code paths (NSPanel restore, Mission Control, etc.)
  card.on('move',  () => { logState('os:move'); maybeAntiTeleport(); });
  card.on('focus', () => logState('os:focus'));
  card.on('blur',  () => logState('os:blur'));
  card.on('show',  () => logState('os:show'));
  card.on('hide',  () => logState('os:hide'));

  // Dump the full display layout once at startup so multi-monitor bug reports
  // include enough context to correlate with bounds traces below.
  const displays = screen.getAllDisplays().map(d => ({
    id: d.id,
    primary: d.id === screen.getPrimaryDisplay().id,
    bounds: d.bounds,
    workArea: d.workArea,
    scaleFactor: d.scaleFactor,
  }));
  console.log('[state] displays', JSON.stringify(displays));
  logState('createCardWindow:done');
}

// ─── Visibility helpers ──────────────────────────────────────
// "Hidden" means alphaValue=0 + ignoreMouseEvents=true. We never call
// card.hide() / card.show(): orderOut+orderFront re-anchors the NSPanel to
// its "home" macOS Space, and the next show() drags the user back to that
// Space. So the window is always orderFront'd, just transparent and
// click-through when hidden.
// Move the card to the top-right of whichever display the cursor is on.
// Called before show on user-driven entry points (Cmd+Space input mode + new
// backend turn) so the card follows the user across monitors instead of being
// stuck on the primary display where it was first created.
function repositionCardToCursorDisplay() {
  if (!card || card.isDestroyed()) return;
  const cursor = screen.getCursorScreenPoint();
  const display = screen.getDisplayNearestPoint(cursor);
  const { workArea } = display;
  const bounds = card.getBounds();
  const target = {
    x: workArea.x + workArea.width - bounds.width - CARD_MARGIN,
    y: workArea.y + CARD_MARGIN - 38,
    width: bounds.width,
    height: bounds.height,
  };
  setStickyDisplay(display.id);
  logState('reposition:before', { targetDisplayId: display.id, target });
  applyBounds(target);
  logState('reposition:after');
}

function setCardHidden() {
  if (!card || card.isDestroyed()) return;
  logState('setCardHidden:enter');
  // Width might have widened for the popover; collapse it back so a stale
  // 678px-wide hidden window doesn't trap clicks across the empty left half
  // when ignoreMouseEvents flips back to false on the next show.
  const bounds = card.getBounds();
  if (bounds.width !== CARD_WIDTH) {
    const rightEdge = bounds.x + bounds.width;
    applyBounds({ x: rightEdge - CARD_WIDTH, y: bounds.y, width: CARD_WIDTH, height: bounds.height });
  }
  card.setOpacity(0);
  setIgnoreMouseEventsTracked(true);
  userHidden = true;
  logState('setCardHidden:done');
}

function setCardVisible() {
  if (!card || card.isDestroyed()) return;
  logState('setCardVisible:enter');
  // Block implicit shows after a manual hide. Renderer's flushHeight calls
  // cardAPI.show() during streaming and that would otherwise resurrect the
  // card a few ms after ⌘+Space toggled it off. Explicit user paths
  // (openInputMode / siriOpen) clear userHidden first, so this only blocks
  // the implicit case.
  if (userHidden) {
    logState('setCardVisible:blocked-userHidden');
    return;
  }
  card.setOpacity(1);
  setIgnoreMouseEventsTracked(false);
  if (!card.isVisible()) {
    card.showInactive();
    card.setAlwaysOnTop(true, 'floating');
    card.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  }
  logState('setCardVisible:done');
}

// ─── Renderer → Main IPC ─────────────────────────────────────
ipcMain.handle('card:resize', (_, height) => {
  if (!card || card.isDestroyed()) return;
  const bounds = card.getBounds();
  const clamped = Math.min(Math.max(60, Math.ceil(height)), 800);
  applyBounds({ ...bounds, height: clamped });
  logState('ipc:resize', { requested: height, clamped });
});

// Widen the window leftward to accommodate the history-preview popover.
// The card itself is right-anchored to its original screen position (top-right
// corner); we move x leftward by the delta so the right edge stays put.
ipcMain.handle('card:setWidth', (_, width) => {
  if (!card || card.isDestroyed()) return;
  const bounds = card.getBounds();
  const clamped = Math.min(Math.max(CARD_WIDTH, Math.ceil(width)), 900);
  if (clamped === bounds.width) return;
  const rightEdge = bounds.x + bounds.width;
  applyBounds({ x: rightEdge - clamped, y: bounds.y, width: clamped, height: bounds.height });
  logState('ipc:setWidth', { requested: width, clamped });
});

ipcMain.handle('card:show', () => {
  logState('ipc:show');
  setCardVisible();
});

ipcMain.handle('card:close', () => {
  logState('ipc:close');
  // Hide-only — keep the Electron process alive across turns.
  if (!card || card.isDestroyed()) return;
  fadeGen++;
  setCardHidden();
  turnState = 'idle';
});

ipcMain.handle('card:fadeOut', (_, ms) => {
  if (!card || card.isDestroyed()) return;
  logState('ipc:fadeOut', { ms });
  // Guard: a fadeOut on an already-hidden card flashes the window — the
  // tick loop sets opacity = (steps-1)/steps ≈ 0.94 on its first frame,
  // making a 0 → 0.94 → 0 pop. After auto-fade, mouseleave events still
  // fire on transparent click-through panels (macOS quirk) and re-schedule
  // fades; this gate makes those a no-op.
  if (card.getOpacity() <= 0.01) return;
  const total = Math.max(60, Math.min(2000, Number(ms) || 280));
  const steps = 18;
  const stepMs = total / steps;
  let n = steps;
  const myGen = ++fadeGen;
  const tick = () => {
    if (!card || card.isDestroyed()) return;
    if (myGen !== fadeGen) return;  // superseded by cancelFade or new turn
    n -= 1;
    if (n <= 0) {
      setCardHidden();
      turnState = 'idle';
    } else {
      card.setOpacity(n / steps);
      setTimeout(tick, stepMs);
    }
  };
  tick();
});

ipcMain.handle('card:cancelFade', () => {
  if (!card || card.isDestroyed()) return;
  // electron-liquid-glass NSGlassEffectView seems to bypass NSWindow's
  // ignoresMouseEvents — DOM mouseenter still fires when the card is
  // alpha=0 + click-through. Without this gate, hover over the idle
  // card area calls cardAPI.cancelFade → setCardVisible and the empty
  // card pops back into view. Only honor cancelFade during an open turn.
  if (turnState === 'idle') return;
  fadeGen++;
  setCardVisible();
});

ipcMain.handle('card:duckAudio', async () => duckSystemAudio());

ipcMain.handle('card:restoreAudio', async () => restoreSystemAudio());

ipcMain.handle('card:submit', async (_, text) => {
  const t = String(text || '').trim();
  if (!t) return { ok: false, reason: 'empty' };
  try {
    const res = await fetch('http://127.0.0.1:8006/inherent/submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: t })
    });
    if (!res.ok) {
      console.warn(`[inherent] submit failed: HTTP ${res.status}`);
      return { ok: false, reason: `http_${res.status}` };
    }
    return { ok: true };
  } catch (err) {
    console.warn(`[inherent] submit network error: ${err?.message}`);
    return { ok: false, reason: 'network' };
  }
});

ipcMain.handle('card:submitImage', async (_, payload) => {
  const text = String(payload?.text || '').trim();
  const mime = String(payload?.mime || 'image/png').split(';')[0].trim() || 'image/png';
  const name = String(payload?.name || 'image.png').replace(/[\\/]/g, '_');
  const bytes = normalizeWavBytes(payload?.buffer);
  if (!bytes || bytes.byteLength <= 0) {
    return { ok: false, reason: 'empty_image' };
  }
  try {
    const form = new FormData();
    form.append('text', text);
    form.append('image', new Blob([bytes], { type: mime }), name || 'image.png');
    const res = await fetch('http://127.0.0.1:8006/inherent/image-submit', {
      method: 'POST',
      body: form
    });
    let body = {};
    try {
      body = await res.json();
    } catch (err) {
      body = {};
    }
    if (!res.ok) {
      console.warn(`[inherent] image submit failed: HTTP ${res.status}`);
      return { ok: false, reason: `http_${res.status}`, ...body };
    }
    return { ok: true, ...body };
  } catch (err) {
    console.warn(`[inherent] image submit network error: ${err?.message}`);
    return { ok: false, reason: 'network' };
  }
});

function normalizeWavBytes(wavBytes) {
  if (wavBytes instanceof ArrayBuffer) {
    return new Uint8Array(wavBytes);
  }
  if (ArrayBuffer.isView(wavBytes)) {
    return new Uint8Array(wavBytes.buffer, wavBytes.byteOffset, wavBytes.byteLength);
  }
  if (Array.isArray(wavBytes)) {
    return Uint8Array.from(wavBytes);
  }
  return null;
}

ipcMain.handle('card:submitVoice', async (_, wavBytes) => {
  const bytes = normalizeWavBytes(wavBytes);
  if (!bytes || bytes.byteLength <= 44) {
    return { ok: false, reason: 'empty_audio' };
  }
  try {
    const form = new FormData();
    form.append('audio', new Blob([bytes], { type: 'audio/wav' }), 'inherent.wav');
    const res = await fetch('http://127.0.0.1:8006/inherent/asr-submit', {
      method: 'POST',
      body: form
    });
    let payload = {};
    try {
      payload = await res.json();
    } catch (err) {
      payload = {};
    }
    if (!res.ok) {
      console.warn(`[inherent] voice submit failed: HTTP ${res.status}`);
      return { ok: false, reason: `http_${res.status}`, ...payload };
    }
    return { ok: true, ...payload };
  } catch (err) {
    console.warn(`[inherent] voice submit network error: ${err?.message}`);
    return { ok: false, reason: 'network' };
  }
});

// ─── Input mode (hotkey-driven) ──────────────────────────────
// Distinct from setCardVisible (response-only): input mode steals focus
// so the renderer's <input> can take keystrokes. Cancels any in-flight
// fade and tells the renderer to clear answer + focus the input.
function openInputMode() {
  if (!card || card.isDestroyed()) return;
  logState('openInputMode:enter');
  fadeGen++;
  userHidden = false;  // user is invoking; clear the implicit-show block
  card.setOpacity(1);
  setIgnoreMouseEventsTracked(false);
  if (!card.isVisible()) {
    card.show();  // focus-stealing variant — input mode wants keyboard focus
    card.setAlwaysOnTop(true, 'floating');
    card.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  } else {
    card.focus();
  }
  // Reposition AFTER show: macOS panels restore to the last NSScreen they were
  // visible on when show() is called, which would undo any pre-show setBounds.
  repositionCardToCursorDisplay();
  logState('openInputMode:done');
  card.webContents.send('card:openInput');
  // Clear watchdog: the user is now driving, not the LLM. A fresh
  // turn from siri:open will rearm it.
  clearBridgeWatchdog();
  turnState = 'idle';
}

// ⌘+Space toggle. Three branches:
//   visible + same display as cursor  → hide
//   visible + different display       → relocate to cursor's display + refocus
//                                       (equivalent to "close A + open B" in one
//                                       press; preserves any in-flight answer
//                                       or typed input)
//   hidden                            → openInputMode (full summon)
// setCardHidden uses opacity rather than orderOut, so checking opacity is the
// right "is it currently shown" test (isVisible() stays true after hide).
function toggleHotkey() {
  if (!card || card.isDestroyed()) return;
  logState('toggleHotkey:enter');
  if (card.getOpacity() > 0.5) {
    const cursor = screen.getCursorScreenPoint();
    const cursorDisplay = screen.getDisplayNearestPoint(cursor);
    const bounds = card.getBounds();
    const cardCenter = { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 };
    const cardDisplay = screen.getDisplayNearestPoint(cardCenter);
    if (cardDisplay.id !== cursorDisplay.id) {
      logState('toggleHotkey:relocate', { from: cardDisplay.id, to: cursorDisplay.id });
      repositionCardToCursorDisplay();
      card.focus();
      return;
    }
    fadeGen++;
    setCardHidden();
    clearBridgeWatchdog();
    turnState = 'idle';
  } else {
    openInputMode();
  }
}

// ─── Public dispatchers (callable from a future backend bridge) ───
function siriOpen(payload) {
  if (!card || card.isDestroyed()) return;
  logState('siriOpen:enter', { streaming: !!payload?.streaming });
  const content = payload?.content;
  const streaming = !!payload?.streaming;
  if (!streaming && (content == null || content === '')) {
    console.warn('[inherent] siriOpen called with empty content (non-streaming); ignoring');
    return;
  }
  if (turnState === 'open') {
    console.warn('[inherent] siriOpen while turn already open; auto-resetting');
    siriReset();
  }
  // Cancel any pending fade and ensure the card is visible so a new turn
  // arriving mid-fade (or post-hidden) reveals reliably.
  fadeGen++;
  userHidden = false;  // backend turn arriving — unblock implicit shows
  setCardVisible();
  // Reposition AFTER show (see openInputMode comment for rationale).
  repositionCardToCursorDisplay();
  logState('siriOpen:done');
  card.webContents.send('siri:open', payload);
  turnState = 'open';
}
function siriAppend(payload) {
  if (!card || card.isDestroyed()) return;
  if (turnState === 'idle') {
    console.warn('[inherent] siriAppend called while turn idle; ignoring');
    return;
  }
  card.webContents.send('siri:append', payload);
}
function siriDone(payload) {
  if (!card || card.isDestroyed()) return;
  if (turnState === 'idle') {
    console.warn('[inherent] siriDone called while turn idle; ignoring');
    return;
  }
  card.webContents.send('siri:done', payload);
  turnState = 'idle';
}
function siriReset() {
  if (!card || card.isDestroyed()) return;
  fadeGen++;
  setCardHidden();
  card.webContents.send('siri:reset');
  turnState = 'idle';
}
function voiceState(payload) {
  if (!card || card.isDestroyed()) return;
  fadeGen++;
  userHidden = false;
  setCardVisible();
  repositionCardToCursorDisplay();
  card.webContents.send('card:voice', payload);
}

// Expose for backend bridge (path 3): jarvis.py will require this module
// or talk over a WS / unix socket → these functions.
module.exports = { siriOpen, siriAppend, siriDone, siriReset, voiceState };

// ─── Backend WS bridge ───────────────────────────────────────
// Connects to ui/web/server.py:/inherent/ws. Server broadcasts response.*
// events as {op, payload} JSON; we route to siri* dispatchers.
//
// Resilience model:
//   - exponential backoff reconnect (1/2/4/8/16s, max 16s)
//   - on (re)connect with turn already open: forced reset (we may have
//     missed siri:done while disconnected, can't trust local state)
//   - watchdog timer started on each siri:open; cleared on done/reset.
//     Fires forced reset if the backend dies mid-turn.
//   - JSON parse failures and unknown ops log + ignore (don't crash bridge)
function connectBridge() {
  if (bridgeShuttingDown) return;
  let ws;
  try {
    ws = new WebSocketClient(INHERENT_WS_URL);
  } catch (err) {
    console.warn(`[bridge] ws ctor failed: ${err?.message}`);
    scheduleBridgeReconnect();
    return;
  }
  wsClient = ws;

  ws.on('open', () => {
    console.log(`[bridge] connected (${INHERENT_WS_URL})`);
    reconnectAttempt = 0;
    if (turnState === 'open') {
      console.warn('[bridge] (re)connect mid-turn; forcing card reset');
      siriReset();
      clearBridgeWatchdog();
    }
  });

  ws.on('message', (data) => {
    let msg;
    try {
      msg = JSON.parse(data.toString());
    } catch (err) {
      console.warn(`[bridge] JSON parse failed: ${err?.message}; raw=${String(data).slice(0, 200)}`);
      return;
    }
    dispatchBridgeMessage(msg);
  });

  ws.on('close', (code) => {
    console.log(`[bridge] closed (code=${code}); will reconnect`);
    if (wsClient === ws) wsClient = null;
    scheduleBridgeReconnect();
  });

  ws.on('error', (err) => {
    // 'close' will fire next and handle reconnect; just log here.
    console.warn(`[bridge] ws error: ${err?.message}`);
  });
}

function scheduleBridgeReconnect() {
  if (bridgeShuttingDown) return;
  if (reconnectTimer) return;  // already scheduled
  const idx = Math.min(reconnectAttempt, RECONNECT_BACKOFF_MS.length - 1);
  const delay = RECONNECT_BACKOFF_MS[idx];
  console.log(`[bridge] reconnecting in ${delay}ms (attempt ${reconnectAttempt + 1})`);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    reconnectAttempt += 1;
    connectBridge();
  }, delay);
}

function dispatchBridgeMessage(msg) {
  if (!msg || typeof msg !== 'object') {
    console.warn('[bridge] message not an object:', msg);
    return;
  }
  const op = msg.op;
  const payload = msg.payload || {};
  switch (op) {
    case 'open':
      siriOpen(payload);
      armBridgeWatchdog();
      break;
    case 'append':
      siriAppend(payload);
      break;
    case 'done':
      siriDone(payload);
      clearBridgeWatchdog();
      break;
    case 'reset':
      siriReset();
      clearBridgeWatchdog();
      break;
    case 'voice':
      voiceState(payload);
      break;
    default:
      console.warn(`[bridge] unknown op: ${op}`);
  }
}

function armBridgeWatchdog() {
  clearBridgeWatchdog();
  watchdogTimer = setTimeout(() => {
    watchdogTimer = null;
    console.warn(`[bridge] watchdog: no done within ${BRIDGE_WATCHDOG_MS}ms; forcing reset`);
    siriReset();
  }, BRIDGE_WATCHDOG_MS);
}

function clearBridgeWatchdog() {
  if (watchdogTimer) {
    clearTimeout(watchdogTimer);
    watchdogTimer = null;
  }
}

function shutdownBridge() {
  bridgeShuttingDown = true;
  clearBridgeWatchdog();
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (wsClient) {
    try { wsClient.close(); } catch {}
    wsClient = null;
  }
}

// Exposed for tests + scenarios that want to drive the dispatcher path
// without an actual backend connection.
module.exports.dispatchBridgeMessage = dispatchBridgeMessage;

// ─── Test scenarios ──────────────────────────────────────────
const scenarios = {
  basic() {
    setTimeout(() => {
      siriOpen({
        content: '# 现在 23°\n\nbedroom · 客厅 22°  \n_(via siri:open IPC)_',
        kind: 'text'
      });
      setTimeout(() => siriDone({ fadeMs: 5000 }), 100);
    }, 500);
  },

  'multi-turn'() {
    setTimeout(() => {
      siriOpen({ content: '# Turn 1\n\n_(first turn — fades after 1s)_', kind: 'text' });
      setTimeout(() => siriDone({ fadeMs: 1000 }), 100);
      setTimeout(() => {
        siriOpen({ content: '# Turn 2\n\n_(second turn, 6s after first)_', kind: 'text' });
        setTimeout(() => siriDone({ fadeMs: 5000 }), 100);
      }, 6000);
    }, 500);
  },

  overflow() {
    const longContent = `# 长内容溢出测试\n\n` +
      Array.from({ length: 40 }, (_, i) =>
        `## 段 ${i + 1}\n\n这是第 ${i + 1} 段文字, 用来测试卡片在内容超过 800px 时是否能正确显示并允许内部滚动。卡片应当 ≤ 800px 高度, 内容超出部分通过卡内滚动条访问。`
      ).join('\n\n');
    setTimeout(() => {
      siriOpen({ content: longContent, kind: 'text' });
      setTimeout(() => siriDone({ fadeMs: 8000 }), 100);
    }, 500);
  },

  empty() {
    setTimeout(() => {
      siriOpen({ content: '', kind: 'text' });
      // Expectation: no card appears, stderr shows the "empty content" warning.
    }, 500);
  },

  'append-no-open'() {
    setTimeout(() => {
      siriAppend({ token: 'orphan token' });
      // Expectation: no card appears, stderr shows the "while turn idle" warning.
    }, 500);
  },

  'done-no-open'() {
    setTimeout(() => {
      siriDone({ fadeMs: 3000 });
      // Expectation: no card appears, stderr shows the "while turn idle" warning.
    }, 500);
  },

  'gen-race'() {
    setTimeout(() => {
      siriReset();
      siriOpen({ content: '# A — should NOT remain\n\n_(turn a, superseded 50ms later)_', kind: 'text' });
      setTimeout(() => {
        siriOpen({ content: '# B — final\n\n_(turn b, the only one that should render)_', kind: 'text' });
        setTimeout(() => siriDone({ fadeMs: 5000 }), 100);
      }, 50);
    }, 500);
  },

  // Mode A streaming demo — simulates LLM token bursts (180ms cadence) so the
  // frontend drip timer (30ms/char) has plenty to smooth. drip-plain and
  // drip-fade share content; only the URL param (animateChars) differs, set
  // when the window loads above.
  'drip-plain'() { runDripScenario(); },
  'drip-fade'()  { runDripScenario(); },

  // Drives dispatchBridgeMessage directly (no WS) — verifies the full
  // backend → dispatcher → siri* → renderer path without needing jarvis.
  // Sequence mirrors what 1b's server.py would broadcast for one cloud LLM
  // turn: open(streaming) → 4× append → done.
  'bridge-mock'() {
    setTimeout(() => {
      dispatchBridgeMessage({
        op: 'open',
        payload: { content: '', streaming: true, kind: 'text' },
      });
      const tokens = [
        '# Bridge mock\n\n',
        '这条消息没经过 WS, ',
        '直接走 `dispatchBridgeMessage`. ',
        '\n\n如果你看到逐字流式 + fade, 那说明 1c 的 dispatcher 路径完整.',
      ];
      tokens.forEach((tok, i) => {
        setTimeout(() => {
          dispatchBridgeMessage({ op: 'append', payload: { token: tok } });
        }, 200 + i * 250);
      });
      setTimeout(() => {
        dispatchBridgeMessage({ op: 'done', payload: { fadeMs: 5000 } });
      }, 200 + tokens.length * 250 + 100);
    }, 500);
  }
};

const STREAM_TEXT_PARTS = [
  '# 流式输出',
  '测试\n\n',
  '正在',
  '生成响应',
  '中…\n\n',
  '**关键发现**',
  '：\n\n',
  '- 第一项',
  '：温度',
  ' 23°\n',
  '- 第二项',
  '：湿度',
  ' 65%\n',
  '- 第三项',
  '：气压',
  ' 1013 hPa\n\n',
  '```python\n',
  'def hello():\n',
  '    print("Hello,',
  ' jarvis!")\n',
  '    return 42\n',
  '```\n\n',
  '测试结束。'
];

function runDripScenario() {
  setTimeout(() => {
    siriOpen({ content: '', streaming: true, kind: 'text' });
    let i = 0;
    const sendNext = () => {
      if (i >= STREAM_TEXT_PARTS.length) {
        siriDone({ fadeMs: 5000 });
        return;
      }
      siriAppend({ token: STREAM_TEXT_PARTS[i] });
      i += 1;
      setTimeout(sendNext, 180);
    };
    setTimeout(sendNext, 100);
  }, 500);
}

// ─── App lifecycle ───────────────────────────────────────────
app.whenReady().then(() => {
  createCardWindow();

  if (!demoMode && scenario) {
    const fn = scenarios[scenario];
    if (fn) {
      fn();
    } else {
      console.warn(`[inherent] unknown scenario: ${scenario} (known: ${Object.keys(scenarios).join(', ')})`);
    }
  }

  // Connect WS bridge only in live mode — scenarios drive siri* directly,
  // and demos render fixtures from card.js. WS would just be noise there.
  if (!demoMode && !scenario) {
    connectBridge();
  }

  // ⌘+Space: universal summon → input mode. Demos/scenarios skip this so
  // the test harness doesn't reset state mid-fixture.
  if (!demoMode && !scenario) {
    const ok = globalShortcut.register('CommandOrControl+Space', toggleHotkey);
    if (!ok) {
      console.warn('[inherent] failed to register CommandOrControl+Space — another app may own it.');
    }
  }
});

app.on('before-quit', () => {
  restoreSystemAudio(true).catch(err => {
    console.warn(`[audio-ducking] quit restore failed: ${err?.message}`);
  });
  shutdownBridge();
  globalShortcut.unregisterAll();
});
app.on('window-all-closed', () => app.quit());
