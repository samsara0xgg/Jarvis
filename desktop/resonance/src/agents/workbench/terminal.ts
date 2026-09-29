// The workbench's terminal pane (ADR 0086): the same glass and type as the preview, three tabs. 终端 is the session's own
// shell, running in the host and drawn here by xterm only while it shows; 服务 is the daemon and the companion as launchd
// sees them, with a restart each; 日志 follows Jarvis's logs.
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import type { Service } from '../../../electron/agents/types';

type Hooks = { api: string; call(route: string, body?: unknown, method?: string): Promise<unknown>; current(): string; toast(t: string): void; changed(): void };
export type Tab = 'term' | 'svc' | 'log';
export type TPos = 'side' | 'drawer' | 'island';
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const svg = (d: string) => `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true">${d}</svg>`;
const POS: [TPos, string, string][] = [
  ['side', '并排', svg('<rect x="2" y="3" width="12" height="10" rx="2"/><path d="M9.5 3v10"/>')],
  ['drawer', '底部抽屉', svg('<rect x="2" y="3" width="12" height="10" rx="2"/><path d="M2 9.5h12"/>')],
  ['island', '浮岛', svg('<rect x="2" y="3" width="12" height="10" rx="2"/><rect x="8" y="8" width="4.5" height="3.2" rx=".8"/>')],
];
const X = svg('<path d="M4 4l8 8M12 4l-8 8"/>');
const LOGS = ['companion', 'companion 错误', 'daemon', '后台'];
function since(ms?: number) {
  if (!ms) return '';
  const m = Math.max(0, Math.round((Date.now() - ms) / 60000));
  return m < 1 ? '刚刚' : m < 60 ? `${m} 分钟` : m < 1440 ? `${Math.floor(m / 60)} 小时` : `${Math.floor(m / 1440)} 天`;
}

export function mountTerminal(pane: HTMLElement, hooks: Hooks) {
  const st = { tab: 'term' as Tab, pos: (localStorage.getItem('agents.termPos') as TPos | null) ?? 'side', shown: false, id: '', svc: [] as Service[], kicking: new Set<string>(), log: localStorage.getItem('agents.log') ?? 'companion', logAt: -1, logKey: '' };
  if (!POS.some(p => p[0] === st.pos)) st.pos = 'side';
  pane.innerHTML = `<div class="tm-h" role="tablist"></div><div class="tm-b"><div class="xt"></div><div class="tm-o" hidden></div></div>`;
  const head = pane.querySelector<HTMLElement>('.tm-h')!, body = pane.querySelector<HTMLElement>('.tm-b')!, xt = pane.querySelector<HTMLElement>('.xt')!, other = pane.querySelector<HTMLElement>('.tm-o')!;
  const term = new Terminal({
    allowTransparency: true, fontFamily: '"IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace', fontSize: 12, lineHeight: 1.72,
    cursorBlink: true, cursorStyle: 'block', scrollback: 5000, macOptionIsMeta: true, drawBoldTextInBrightColors: false,
    theme: {
      background: '#00000000', foreground: 'rgba(214,219,240,0.88)', cursor: 'rgba(157,180,255,0.8)', cursorAccent: '#0b0d24', selectionBackground: 'rgba(157,180,255,0.3)',
      black: '#1b1e33', red: '#ff7a66', green: '#6fe0b4', yellow: '#ffc98f', blue: '#6c9cff', magenta: '#c7a8ff', cyan: '#9db4ff', white: '#d6dbf0',
      brightBlack: '#838ba6', brightRed: '#ffb3a6', brightGreen: '#9ff0cf', brightYellow: '#ffe0bd', brightBlue: '#9db4ff', brightMagenta: '#dcc8ff', brightCyan: '#c4d2ff', brightWhite: '#ffffff',
    },
  });
  const fit = new FitAddon();
  term.loadAddon(fit);
  let opened = false, es: EventSource | null = null, ended = false;

  // ---------- the shell: keys go to the host in order, output comes back on one stream ----------
  let pending = '', sending = false;
  async function flush() {
    sending = true;
    while (pending) { const d = pending; pending = ''; await hooks.call(`/term/${st.id}/input`, { data: d }).catch(() => {}); }
    sending = false;
  }
  term.onData(d => {
    if (ended) { if (d === '\r') void connect(true); return; }
    pending += d; if (!sending) void flush();
  });
  // The drawing follows the pane every frame; the shell hears its new size once the pane stops moving, so a prompt is
  // not redrawn for every step of a layout change.
  let sized = '', settle = 0;
  function resize() {
    if (!opened || !xt.offsetWidth || !xt.offsetHeight) return;
    try { fit.fit(); } catch { return; }
    clearTimeout(settle);
    settle = window.setTimeout(() => {
      const k = `${term.cols}x${term.rows}`;
      if (k !== sized && st.id) { sized = k; void hooks.call(`/term/${st.id}/resize`, { cols: term.cols, rows: term.rows }).catch(() => {}); }
    }, 260);
  }
  let frame = 0;
  new ResizeObserver(() => { cancelAnimationFrame(frame); frame = requestAnimationFrame(resize); }).observe(xt);
  async function connect(fresh = false) {
    es?.close(); es = null; ended = false;
    if (!st.id || !st.shown || st.tab !== 'term') return;
    if (!opened) { term.open(xt); opened = true; }
    resize();
    const id = st.id;
    try { await hooks.call(`/term/${id}`, { cols: term.cols, rows: term.rows }); }
    catch (e) { term.reset(); term.write(`\x1b[38;2;255;179;166m${e instanceof Error ? e.message : String(e)}\x1b[0m\r\n`); return; }
    if (id !== st.id || !st.shown) return;
    if (fresh) term.reset();
    sized = '';
    const s = es = new EventSource(`${hooks.api}/term/${id}/stream`);
    s.addEventListener('replay', m => { term.reset(); term.write(JSON.parse((m as MessageEvent).data) as string); });
    s.onmessage = m => term.write(JSON.parse(m.data) as string);
    s.addEventListener('exit', () => { s.close(); if (es === s) { es = null; ended = true; term.write('\r\n\x1b[38;2;131;139;166mshell 退出了 · 按回车重开\x1b[0m\r\n'); } });
  }

  // ---------- 服务 and 日志 ----------
  async function loadSvc() {
    const r = await hooks.call('/services').catch(() => null) as { services: Service[] } | null;
    if (!r) return;
    for (const s of r.services) if (st.kicking.has(s.name) && s.running && s.since && Date.now() - s.since < 60000) st.kicking.delete(s.name);
    st.svc = r.services; draw(); hooks.changed();
  }
  const svcHTML = () => `<div class="svc">${st.svc.map(s => `<div><i class="${st.kicking.has(s.name) ? 're' : s.running ? '' : 'off'}"></i><b>${s.name}</b><button type="button" data-wb="kick" data-n="${s.name}"${st.kicking.has(s.name) ? ' disabled' : ''}>重启</button><small>${esc(s.label)} · ${st.kicking.has(s.name) ? '重启中' : s.running ? `运行中 · ${since(s.since)}` : '没在跑'}</small></div>`).join('') || '<p class="dim">在问 launchd…</p>'}</div>`;
  const cls = (l: string) => /(error|exception|traceback|fatal|✕|✗|\bfail)/i.test(l) ? 'er' : /(warn)/i.test(l) ? 'wr' : /(ready|✓|\bok\b|listening|started)/i.test(l) ? 'ok' : '';
  let logLines: string[] = [];
  async function loadLog() {
    const name = st.log, r = await hooks.call(`/logs?name=${encodeURIComponent(name)}&from=${st.logAt}`).catch(() => null) as { file: string; size: number; text: string } | null;
    if (!r || name !== st.log) return;
    if (st.logAt < 0 || r.size < st.logAt) logLines = [];
    st.logAt = r.size;
    if (r.text) logLines = [...logLines, ...r.text.replace(/\n$/, '').split('\n')].slice(-800);
    const box = other.querySelector<HTMLElement>('.lg'), stick = !box || box.scrollTop >= box.scrollHeight - box.clientHeight - 30;
    const key = `${st.log}|${r.file}|${logLines.length}|${logLines.at(-1)}`;
    if (key === st.logKey && box) return;
    st.logKey = key;
    other.innerHTML = `<div class="lg-h"><span class="dim">~/.jarvis/logs/${esc(r.file)}</span>${LOGS.map(n => `<button type="button" data-wb="log" data-n="${esc(n)}"${n === st.log ? ' aria-pressed="true"' : ''}>${esc(n)}</button>`).join('')}</div><div class="lg">${logLines.map(l => `<div class="${cls(l)}">${esc(l) || '&nbsp;'}</div>`).join('')}</div>`;
    const lg = other.querySelector<HTMLElement>('.lg')!;
    if (stick) lg.scrollTop = lg.scrollHeight; else if (box) lg.scrollTop = box.scrollTop;
  }
  // While the pane shows, services every 5 s (the dot in the tabs follows them) and the log every 2 s.
  let tick = 0;
  setInterval(() => {
    if (!st.shown || document.hidden) return;
    tick++;
    if (st.tab === 'log') void loadLog();
    if (tick % 3 === 0 || st.tab === 'svc') void loadSvc();
  }, 2000);

  function draw() {
    const down = st.svc.some(s => !s.running), re = st.kicking.size > 0;
    head.innerHTML = [['term', '终端'], ['svc', '服务'], ['log', '日志']].map(([k, t]) => `<button type="button" class="t" role="tab" data-wb="tab" data-t="${k}" aria-selected="${st.tab === k}">${t}</button>`).join('')
      + `<span class="dot${re ? ' re' : down ? ' down' : ''}" title="${re ? '在重启' : down ? `${st.svc.filter(s => !s.running).map(s => s.name).join('、')} 没在跑` : 'daemon 和 companion 都在跑'}"></span><span class="sp"></span>`
      + `<span class="tpos" role="group" aria-label="终端放在">${POS.map(([k, t, i]) => `<button type="button" class="ib" data-wb="pos" data-p="${k}" data-tip="放在${t}" aria-label="放在${t}" aria-pressed="${st.pos === k}">${i}</button>`).join('')}</span>`
      + `<button type="button" class="ib" data-wb="close" aria-label="收起终端" data-tip="收起终端" data-key="⌃\`">${X}</button>`;
    xt.hidden = st.tab !== 'term';
    other.hidden = st.tab === 'term';
    if (st.tab === 'svc') other.innerHTML = svcHTML();
    body.classList.toggle('pad', st.tab !== 'term');
  }
  pane.addEventListener('click', e => {
    const b = (e.target as Element).closest<HTMLElement>('[data-wb]');
    if (!b) { if (st.tab === 'term' && !(e.target as Element).closest('.tm-h')) term.focus(); return; }
    const a = b.dataset.wb;
    if (a === 'tab') api.tab(b.dataset.t as Tab);
    else if (a === 'pos') api.onPos(b.dataset.p as TPos);
    else if (a === 'close') api.onClose();
    else if (a === 'log') { st.log = b.dataset.n!; st.logAt = -1; st.logKey = ''; localStorage.setItem('agents.log', st.log); void loadLog(); }
    else if (a === 'kick') {
      const n = b.dataset.n!;
      st.kicking.add(n); draw();
      void hooks.call(`/services/${n}/restart`, { id: hooks.current() }).catch(e => { st.kicking.delete(n); draw(); hooks.toast(e instanceof Error ? e.message : String(e)); });
    }
  });

  const api = {
    get pos() { return st.pos; }, get down() { return st.svc.some(s => !s.running); },
    onPos: (_: TPos) => {}, onClose: () => {},
    setPos(p: TPos) { st.pos = p; localStorage.setItem('agents.termPos', p); draw(); },
    tab(t: Tab) {
      if (st.tab === t) return;
      st.tab = t; draw();
      if (t === 'term') void connect(); else { es?.close(); es = null; }
      if (t === 'svc') void loadSvc();
      if (t === 'log') { st.logAt = -1; st.logKey = ''; void loadLog(); }
    },
    // The pane shows or hides; the shell and its buffer stay in the host either way.
    show(on: boolean, id: string) {
      const moved = id !== st.id;
      st.shown = on; st.id = id;
      if (!on) { es?.close(); es = null; return; }
      draw(); void loadSvc();
      if (st.tab === 'term' && (moved || !es)) void connect(true);
      if (st.tab === 'log') void loadLog();
    },
    focus() { if (st.tab === 'term') term.focus(); },
    fit: resize,
  };
  draw();
  return api;
}
