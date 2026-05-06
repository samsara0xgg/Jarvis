// inherent-main.js — Electron main entry for Jarvis "inherent" mode.
//
// Owns a single Siri-style floating card (NSGlassEffectView via electron-liquid-glass).
// Path 3 (2): hide-only lifecycle, fade cancellation, turn-state contract guards,
// scenario-based test harness.
//
// IPC contract:
//   main → renderer (webContents.send):
//     siri:open    { content: markdown, kind?: 'text'|'code'|'mixed' }
//     siri:append  { token: string }    (streaming, path 3)
//     siri:done    { fadeMs?: number }
//     siri:reset                          (clear card before next turn)
//
//   renderer → main (ipcRenderer.invoke):
//     card:resize       height_px
//     card:show
//     card:close
//     card:fadeOut      ms
//     card:cancelFade

const { app, BrowserWindow, screen, ipcMain } = require('electron');
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

let card = null;
let glassId = null;
// fadeGen: bumped to cancel an in-flight fade tick chain. Each card:fadeOut captures
// myGen = ++fadeGen; the tick checks myGen !== fadeGen and bails if superseded.
let fadeGen = 0;
// turnState gates contract violations: open/append/done outside the expected order
// just warn rather than crash — useful when backend has bugs and we want a clear log.
let turnState = 'idle';  // 'idle' | 'open'

// Bridge state
let wsClient = null;
let reconnectAttempt = 0;
let reconnectTimer = null;
let watchdogTimer = null;
let bridgeShuttingDown = false;

function createCardWindow() {
  const display = screen.getPrimaryDisplay();
  const { workArea } = display;

  card = new BrowserWindow({
    width: CARD_WIDTH,
    height: CARD_INITIAL_HEIGHT,
    x: workArea.x + workArea.width - CARD_WIDTH - CARD_MARGIN,
    y: workArea.y + CARD_MARGIN,
    transparent: true,
    frame: false,
    hasShadow: false,
    alwaysOnTop: true,
    resizable: false,
    vibrancy: false,
    focusable: false,
    type: 'panel',
    show: false,  // first card:show IPC reveals it
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'inherent-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false
    }
  });

  card.setAlwaysOnTop(true, 'screen-saver');
  card.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  const cardPath = path.join(__dirname, 'card.html');
  const urlParams = new URLSearchParams();
  if (demoMode) urlParams.set('demo', demoMode);
  if (scenario === 'drip-fade') urlParams.set('animateChars', '1');
  const qs = urlParams.toString();
  const url = qs ? `file://${cardPath}?${qs}` : `file://${cardPath}`;
  card.loadURL(url);

  card.webContents.once('did-finish-load', () => {
    glassId = liquidGlass.addView(card.getNativeWindowHandle(), {
      cornerRadius: CORNER_RADIUS,
      tintColor: TINT_COLOR
    });
  });

  card.on('closed', () => { card = null; glassId = null; });
}

// ─── Visibility helpers ──────────────────────────────────────
// We *never* call card.hide() — on a transparent panel + NSGlassEffectView,
// hide() leaves the window in a state where showInactive() does not reliably
// re-reveal it (multi-turn and gen-race both showed B never appearing).
// Instead, "hidden" means alphaValue=0 + ignoreMouseEvents=true, so the
// window is invisible and click-through, yet a single setOpacity(1) fully
// restores it without any orderOut/orderFront cycle.
function setCardHidden() {
  if (!card || card.isDestroyed()) return;
  card.setOpacity(0);
  card.setIgnoreMouseEvents(true);
}

function setCardVisible() {
  if (!card || card.isDestroyed()) return;
  card.setOpacity(1);
  card.setIgnoreMouseEvents(false);
  if (!card.isVisible()) {
    card.showInactive();
    card.setAlwaysOnTop(true, 'screen-saver');
    card.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  }
}

// ─── Renderer → Main IPC ─────────────────────────────────────
ipcMain.handle('card:resize', (_, height) => {
  if (!card || card.isDestroyed()) return;
  const bounds = card.getBounds();
  const clamped = Math.min(Math.max(60, Math.ceil(height)), 800);
  card.setBounds({ ...bounds, height: clamped });
});

ipcMain.handle('card:show', () => {
  setCardVisible();
});

ipcMain.handle('card:close', () => {
  // Hide-only — keep the Electron process alive across turns.
  if (!card || card.isDestroyed()) return;
  fadeGen++;
  setCardHidden();
  turnState = 'idle';
});

ipcMain.handle('card:fadeOut', (_, ms) => {
  if (!card || card.isDestroyed()) return;
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

// ─── Public dispatchers (callable from a future backend bridge) ───
function siriOpen(payload) {
  if (!card || card.isDestroyed()) return;
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
  setCardVisible();
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

// Expose for backend bridge (path 3): jarvis.py will require this module
// or talk over a WS / unix socket → these functions.
module.exports = { siriOpen, siriAppend, siriDone, siriReset };

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
});

app.on('before-quit', shutdownBridge);
app.on('window-all-closed', () => app.quit());
