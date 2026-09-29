// Companion on the daemon link, in headless Chrome against the built page. Run after `npm run build`.
// Default: a fake daemon (routed HTTP plus a fake WebSocket) drives every live path — voice turns,
// her bubble, typed text, the conversation, Agents, plugins — so nothing is spoken or written to
// the real history. `--real`: read-only against the running daemon (JARVIS_INHERENT_BRIDGE_PORT,
// default 8006); it pokes nothing and sends nothing, and checks each home row against the daemon.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const real = process.argv.includes('--real');
const dir = path.join(root, 'evidence', real ? 'companion-live-real' : 'companion-live');
mkdirSync(dir, { recursive: true });
const port = real ? (process.env.JARVIS_INHERENT_BRIDGE_PORT ?? '8006') : '8799';
const daemon = `http://127.0.0.1:${port}`;
// Every daemon route needs the local key; the page gets it as a header on each request.
const pluginToken = real ? (() => { try { return JSON.parse(readFileSync(path.join(homedir(), '.jarvis/plugin-access.json'), 'utf8')).token; } catch { return null; } })() : null;
const daemonHeaders = pluginToken ? { Authorization: `Bearer ${pluginToken}` } : {};
const web = Number(process.env.COMPANION_PORT ?? 5192);
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log(`PASS ${name}`); };
// The page is served from vite, the daemon from another origin; the Electron window has no such wall.
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security'] });
const plain = text => text.replace(/<\/?(voice|document)>/g, '').replace(/\*\*|`/g, '').trim();
// An answer's words with its markdown and spacing gone, to compare the source with the rendered page.
const words = text => text.replace(/<\/?(voice|document)>/g, '').replace(/^\s*(\d+[.)]|[-*•])\s+/gm, '').replace(/[\s*`#|:-]/g, '');
const hm = ms => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const context = await browser.newContext({ viewport: { width: 640, height: 722 }, deviceScaleFactor: 2, extraHTTPHeaders: daemonHeaders });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));

  // The fake daemon: what the companion posts, and what it is told.
  const posts = [], pluginOps = [];
  let typed = 0; // the daemon mints a fresh turn id for every submit
  const now = Date.now(), iso = ms => new Date(ms).toISOString();
  // Yesterday's turn sits past the fake daemon's page of two rows, so only a longer page brings it.
  const yesterday = [
    { seq: 9, id: 'y1', ts: iso(now - 86_400_000), source: 'allen', text: 'Is the reSpeaker plugged in?' },
    { seq: 10, id: 'y2', ts: iso(now - 86_400_000 + 3000), source: 'jarvis', text: 'Yes, on the left USB-C port.' },
  ], reads = [];
  const rows = [
    { seq: 11, id: 'a', ts: iso(now - 9 * 60_000), source: 'allen', text: 'What’s on my plate today?' },
    { seq: 12, id: 'b', ts: iso(now - 9 * 60_000 + 4000), source: 'jarvis', text: 'Two things: the **voice test** at four, and `the demo cut`.' },
  ];
  let controls = { mic_muted: false, speech_muted: false, conversation: false };
  // What POST /inherent/setup/key answers next (Settings › Accounts).
  let keyAnswer = { ok: false, checks: [] };
  // ADR 0069, 0070: the daemon's marks, each session's conversation, and what a typed reply does to the board.
  const agentMarks = {}, conversations = {};
  let replied = () => {};
  let snapshot = { request: null, plugins: [
    { id: 'notion', name: 'Notion', description: 'Pages and databases.', capabilities: ['Read'], skill_count: 0, supported: true, unavailable_reason: null, enabled: false, status: 'needs_auth', error: null, auth: 'oauth', credential_fields: [], credentials_saved: false, approval_mode: 'auto', tools: [] },
    { id: 'linear', name: 'Linear', description: 'Issues and projects.', capabilities: ['Read', 'Write'], skill_count: 0, supported: true, unavailable_reason: null, enabled: true, status: 'ready', error: null, auth: 'oauth', credential_fields: [], credentials_saved: true, approval_mode: 'auto',
      tools: [{ name: 'mcp__linear__list_issues', description: '', read_only: true, requires_confirmation: false }] },
  ] };
  let presentation = 0;
  const request = (plugin_id, purpose = '') => ({ id: `r${++presentation}`, plugin_id, purpose, presentation, continue_task: !!purpose, error: null, resume_status: 'not_requested',
    state: snapshot.plugins.find(p => p.id === plugin_id).status === 'ready' ? 'ready' : 'offered' });
  const claude = [
    { agent: 'claude', session_id: 'c-wait', kind: 'interactive', phase: 'needs_input', title: 'Wire the companion', project: 'jarvis', branch: 'worktree-companion-ball', cwd: '/x', where: 'Ghostty', prompt: 'wire the whole backend', activity: 'Wants to run npm run build', last_message: '', started_ms: now - 3_600_000, updated_ms: now - 60_000 },
    { agent: 'claude', session_id: 'c-bg', kind: 'background', phase: 'working', title: 'Nightly audit', project: 'jarvis', branch: '', cwd: '/x', where: 'background', prompt: 'audit the repo', activity: 'Reading runtime/', last_message: '', started_ms: now - 600_000, updated_ms: now - 5000 },
  ];
  const codexId = '0199a3c2-1111-4222-8333-444455556666';
  const fixtures = {
    '/inherent/claude-sessions': { sessions: claude, error: null },
    '/inherent/codex-sessions': { sessions: [{ session_id: codexId, state: 'running', cwd: '/Users/x/Projects/typlus', model: 'gpt', prompt: 'fix the overlay', detail: 'Editing Overlay.swift', last_message: '', since_ms: now - 120_000 }] },
    '/inherent/usage': { services: { claude: { status: 'ok', observed_at_ms: now, data: { plan: '20X', windows: [{ key: 'five_hour', label: '5 hours', percent: 41, resets_at: iso(now + 3_600_000) }] } },
      codex: { status: 'ok', observed_at_ms: now, data: { plan: 'Pro Lite', windows: [{ key: 'primary_window', label: '7 d', percent: 64, resets_at: iso(now + 86_400_000) }], reset_credits: 1 } },
      deepseek: { status: 'ok', observed_at_ms: now, data: { balance: -0.1, currency: 'USD', is_available: true } },
      openai: { status: 'ok', observed_at_ms: now, data: { today_usd: 0.32, month_usd: 14.53, by_key: [], balance_usd: 23.25, balance_recorded_usd: 25, balance_recorded_at: '2026-09-25T18:00:00+00:00',
        by_model: [{ model: 'gpt-5.6-sol', today_usd: 0, month_usd: 4.37 }, { model: 'gpt-5.6-luna', today_usd: 0.2817, month_usd: 1.8 }, { model: 'gpt-5.4-mini', today_usd: 0.0356, month_usd: 0.88 },
          { model: 'gpt-6-luna', today_usd: 0.0034, month_usd: 0.27 }, { model: 'gpt-live-1', today_usd: 0, month_usd: 2.16 }] } },
      minimax: { status: 'ok', observed_at_ms: now, data: { balance: 15.36 } } } },
    '/inherent/work-state': { state: { version: 1, analyzed_at: iso(now - 60_000), observed_until: iso(now - 60_000), evidence: { coverage: {}, counts: {}, limits: [] }, now: { text: 'Wiring the companion to the daemon.', basis: 'observed', refs: [] }, activities: [], links: [], uncertainties: [], note: null },
      data: null, freshness: { checked_at_ms: now, latest_observed_at: iso(now - 60_000), analyzed_at: iso(now - 60_000), analysis_observed_until: iso(now - 60_000) }, refreshing: false, outcome: 'analyzed', error: null },
    '/inherent/projects': { days: Array.from({ length: 7 }, (_, i) => iso(now - (6 - i) * 86_400_000).slice(0, 10)), projects: [{ id: 'jarvis', name: 'jarvis', seconds: 7200, today_seconds: 3600, days: [0, 0, 0, 0, 0, 3600, 3600], last_seen: null, commits: { count: 2, items: [] }, recent: [] }],
      other: { seconds: 0, days: [0, 0, 0, 0, 0, 0, 0], count: 0 }, unsorted: { seconds: 0, days: [0, 0, 0, 0, 0, 0, 0], count: 0 }, coverage: { timesink: 'ok', git: 'ok' }, latest_observed_at: null, sorted_at: null, refreshing: false, outcome: null, error: null },
  };
  fixtures['/inherent/projects/refresh'] = fixtures['/inherent/projects'];
  const LOGO = `data:image/svg+xml;base64,${Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><circle cx="4" cy="4" r="4" fill="#5e6ad2"/></svg>').toString('base64')}`;
  fixtures['/inherent/usage/refresh'] = fixtures['/inherent/usage'];
  // What the Usage page asked main to spend; `resetFails` makes the next call fail like a lost daemon.
  const resets = [], refreshes = [], balances = [];
  await page.exposeFunction('__usageBalance', async (service, usd) => { balances.push({ service, usd }); return { recorded: true }; });
  let resetFails = 0;
  await page.exposeFunction('__usageReset', async (service, id) => {
    resets.push({ service, id });
    if (resetFails) { resetFails--; throw new Error("Error invoking remote method 'usage-reset': Error: Could not reach Jarvis. Try again."); }
    return { code: 'reset', windows_reset: 1 };
  });
  await page.exposeFunction('__plugins', async (operation, data) => {
    pluginOps.push({ operation, data });
    if (real) {
      // Read only: the live catalog, never an action.
      if (!['read', 'icon'].includes(operation) || !pluginToken) throw new Error('read only');
      const route = operation === 'icon' ? `/${encodeURIComponent(data.plugin_id)}/icon` : '';
      const r = await fetch(`${daemon}/inherent/plugins${route}`, { headers: { Authorization: `Bearer ${pluginToken}` } });
      if (!r.ok) throw new Error(`plugins ${r.status}`);
      return r.json();
    }
    if (operation === 'icon') return { icon: data.plugin_id === 'linear' ? LOGO : null };
    if (operation === 'open') snapshot = { ...snapshot, request: request(data.plugin_id) };
    else if (operation === 'connect') snapshot = { ...snapshot, request: { ...snapshot.request, state: 'authorizing' } };
    else if (operation === 'cancel') snapshot = { ...snapshot, request: { ...snapshot.request, state: 'cancelled' } };
    return structuredClone(snapshot);
  });
  await page.addInitScript(fake => {
    window.__state = { opened: [], accounts: [], jumps: [] };
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; },
      onCommand: callback => { window.__command = callback; return () => {}; },
      passthrough: () => {}, focus: async on => { window.__state.focus = on; }, material: () => {},
      codexTitles: async () => ({}), openCodex: async id => { window.__state.opened.push(id); return true; },
      watchGhostty: on => { window.__state.watch = on; }, onGhostty: callback => { window.__ghostty = callback; return () => {}; },
      jumpGhostty: async (title, job) => { window.__state.jumps.push([title, job]); return true; },
      openAccount: async id => { window.__state.accounts.push(id); return true; }, usageReset: (service, id) => window.__usageReset(service, id),
      usageBalance: (service, usd) => window.__usageBalance(service, usd),
      plugins: (operation, data = {}) => window.__plugins(operation, data),
    };
    if (!fake) return;
    // A WebSocket that never leaves the page: the test pushes the daemon's envelopes through __emit.
    window.__sockets = [];
    window.WebSocket = class { constructor(url) { this.url = url; window.__sockets.push(this); setTimeout(() => this.onopen?.(), 0); } send() {} close() { this.onclose?.(); } };
    window.__emit = (op, payload) => window.__sockets.at(-1).onmessage({ data: JSON.stringify({ op, payload }) });
  }, !real);
  // Read only means read only: every POST to the real daemon (controls sync, project sorting) is refused here.
  if (real) await page.route(`${daemon}/**`, route => route.request().method() === 'POST' ? (posts.push(new URL(route.request().url()).pathname), route.abort()) : route.continue());
  if (!real) await page.route(`${daemon}/**`, async route => {
    const url = new URL(route.request().url()), method = route.request().method();
    const body = method === 'POST' ? JSON.parse(route.request().postData() || '{}') : null;
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (method === 'POST' && !url.pathname.endsWith('/refresh')) posts.push({ path: url.pathname, body });
    if (url.pathname === '/inherent/controls') { controls = { ...controls, ...body }; return json(controls); }
    if (url.pathname === '/inherent/submit') return json({ turn_id: `typed-${++typed}` });
    if (url.pathname === '/inherent/cancel-response') return json({});
    if (url.pathname === '/inherent/setup/key') return json(keyAnswer);
    if (url.pathname === '/inherent/restart') return json({ ok: true });
    if (url.pathname === '/inherent/usage/refresh') refreshes.push(Date.now());
    if (url.pathname.startsWith('/inherent/claude-requests/')) return json({ ok: true });
    if (url.pathname === '/inherent/agent-marks') return json({ marks: agentMarks });
    const marked = /^\/inherent\/agent-marks\/(.+)$/.exec(url.pathname), said = /^\/inherent\/claude-sessions\/([^/]+)\/(conversation|reply)$/.exec(url.pathname);
    if (marked && method === 'POST') {
      const m = agentMarks[marked[1]] ??= {};
      if (body.seen) m.unread = false;
      if (typeof body.unread === 'boolean') m.unread = body.unread;
      if (typeof body.park === 'boolean') m.parked_ms = body.park ? m.parked_ms || Date.now() : null;
      if (typeof body.archive === 'boolean') m.archived_ms = body.archive ? m.archived_ms || Date.now() : null;
      return json(m);
    }
    if (said?.[2] === 'conversation') return json({ messages: conversations[said[1]] ?? [] });
    if (said?.[2] === 'reply') { replied(said[1], body.text); return json({ ok: true }); }
    if (url.pathname === '/inherent/conversation') {
      const after = Number(url.searchParams.get('after') ?? 0), limit = Number(url.searchParams.get('limit') ?? 2), all = [...yesterday, ...rows];
      reads.push({ at: Date.now(), after, limit });
      return json({ since: after, rows: after ? all.filter(r => r.seq > after) : all.slice(-limit) });
    }
    // The home's new writes: a to-do checked off, and Jarvis's own settings once it serves them.
    if (method === 'POST' && url.pathname === '/inherent/today/todo') return json({ ok: true });
    if (method === 'POST' && url.pathname === '/inherent/settings' && fixtures['/inherent/settings']) { Object.assign(fixtures['/inherent/settings'].values, body.changes); return json(fixtures['/inherent/settings']); }
    if (method === 'POST' && url.pathname === '/inherent/language') { fixtures['/inherent/language'] = { language: body.language }; return json(fixtures['/inherent/language']); }
    if (fixtures[url.pathname]) return json(fixtures[url.pathname]);
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });

  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${port}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  const hit = page.locator('.companion-hit');
  // A click on the notch opens the Dashboard and pins it; a click on a pinned one closes it.
  const island = () => page.locator('.companion-island-target').click({ force: true });
  const closeDash = async () => { for (let i = 0; i < 2 && await page.locator('.companion-dashboard.is-open').count(); i++) { await island(); await page.waitForTimeout(200); } };
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  const waitPlace = async value => { await page.waitForFunction(v => document.querySelector('.companion-hit')?.dataset.place === v, value, { timeout: 5000 }); await page.waitForTimeout(700); };
  const face = (...ids) => page.waitForFunction(v => v.includes(document.querySelector('.companion-canvas')?.dataset.face), ids, { timeout: 2500 }).then(() => ids[0], () => null);
  const shot = (name, clip = { x: 120, y: 0, width: 400, height: 240 }) => page.screenshot({ path: path.join(dir, `${name}.png`), clip });
  const panelShot = name => shot(name, { x: 150, y: 0, width: 340, height: 722 });
  const openRow = name => page.locator(`.ad [data-row="${name}"]`).evaluate(el => (el.matches('button') ? el : el.querySelector('button')).click());
  const back = async () => { await page.locator('.ad .pg-back').click(); await page.waitForTimeout(700); };
  const text = selector => page.locator(selector).first().textContent();
  // At the top of the Conversation page, a fresh scroll up past the resistance; true when the words in view stayed put.
  const pullUp = async () => {
    const pageBody = page.locator('.ad .pg-body');
    await pageBody.evaluate(b => { b.scrollTop = 0; });
    const fromBottom = () => pageBody.evaluate(b => b.scrollHeight - b.scrollTop), before = await fromBottom(), box = await pageBody.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + 80); await page.waitForTimeout(250);
    // Wheel until the day before shows (45 px a notch at 2x), then stop: further notches would scroll into it.
    const dayCount = () => page.locator('.ad .tr').evaluateAll(els => new Set(els.map(e => e.dataset.day)).size), start = await dayCount();
    for (let i = 0; i < 12 && await dayCount() === start; i++) { await page.mouse.wheel(0, -90); await page.waitForTimeout(40); }
    await page.waitForTimeout(800);
    // The notch that loads it still glides on into the new day, so one notch of drift is allowed.
    return Math.abs(await fromBottom() - before) <= 50;
  };
  const out = { x: 195.5, y: 72 };
  await page.waitForTimeout(800);

  if (real) {
    const get = async p => (await fetch(`${daemon}${p}`, { headers: daemonHeaders })).json();
    await island();
    await page.locator('.companion-dashboard.is-open').waitFor();
    await page.waitForTimeout(2500);
    const conversation = await get('/inherent/conversation?after=0');
    const answer = [...conversation.rows].reverse().find(r => r.source !== 'allen');
    const lastYou = [...conversation.rows].reverse().find(r => r.source === 'allen'), since = lastYou ? Date.now() - Date.parse(lastYou.ts) : Infinity;
    const talkShown = await page.locator('.ad [data-block="talk"]').count() === 1;
    check(`R the conversation is on top only within 10 min of your last turn (${Number.isFinite(since) ? Math.round(since / 60_000) : '-'} min ago, ${talkShown ? 'shown' : 'not shown'})`,
      talkShown === since < 10 * 60_000 && (!talkShown || !answer || answer.seq < lastYou.seq || await text('.ad .r-talk .say') === plain(answer.text)));
    const todayRoute = await fetch(`${daemon}/inherent/today`).then(r => r.status, () => 0);
    check(`R Today says the calendar is not connected while the daemon has no /inherent/today (${todayRoute})`,
      todayRoute !== 404 || (await text('.ad [data-block="today"] .muted')) === 'Calendar and to-dos aren’t connected yet.');
    const work = await get('/inherent/work-state');
    const nowText = await text('.ad .r-now .text');
    check(`R Now is the daemon's work state (${work.state?.now ? 'claim' : 'none'})`, work.state?.now ? nowText === work.state.now.text : /No recent activity|Syncing/.test(nowText));
    await panelShot('R-home');
    const usage = await get('/inherent/usage'), first = usage.services.claude?.status === 'ok' ? usage.services.claude.data.windows?.[0] : null;
    check(`R Usage rings are the daemon's (${first ? `${first.key} ${Math.round(first.percent)}%` : 'no Claude'})`, first ? (await text('.ad .r-usage .dial b')).startsWith(String(Math.round(first.percent))) : true);
    const projects = await get('/inherent/projects'), top = projects.projects?.find(p => p.seconds > 0);
    check(`R the Projects tile is the daemon's (${top?.name ?? 'none'})`, top ? (await text('.ad .pj-mini b')) === top.name : true);
    const codex = (await get('/inherent/codex-sessions')).sessions ?? [];
    const claudeRoute = await fetch(`${daemon}/inherent/claude-sessions`, { headers: daemonHeaders });
    const claudeIds = claudeRoute.ok ? (await claudeRoute.json()).sessions.map(r => r.session_id) : [];
    await openRow('agents'); await page.waitForTimeout(3500);
    const shownIds = await page.locator('.ad .ag').evaluateAll(els => els.map(e => e.dataset.id));
    check(`R Agents lists the daemon's sessions (${codex.length} Codex; Claude route ${claudeRoute.status}, ${claudeIds.length} sessions, ${claudeIds.filter(id => shownIds.includes(id)).length} shown) and no demo rows`,
      shownIds.length >= Math.min(1, codex.length) && claudeIds.every(id => shownIds.includes(id)) && !(await page.locator('.ad .ag-title').allTextContents()).includes('Dashboard inner pages'));
    await panelShot('R-agents'); await back();
    await openRow('plugins'); await page.waitForTimeout(2200);
    const names = await page.locator('.ad .pl-name').evaluateAll(els => els.map(e => e.firstChild.textContent));
    const catalog = pluginToken ? (await (await fetch(`${daemon}/inherent/plugins`, { headers: { Authorization: `Bearer ${pluginToken}` } })).json()).plugins.map(p => p.name) : [];
    check(`R Plugins is the daemon's catalog (${names.join(', ') || 'no token'}), read only`, pluginToken ? names.length > 0 && [...names].sort().join() === [...catalog].sort().join() && pluginOps.every(o => ['read', 'icon'].includes(o.operation)) : true);
    await panelShot('R-plugins'); await back();
    // Opened the way a click opens it, timing when the words on screen have faded in.
    const litMs = await page.evaluate(async () => {
      const t0 = performance.now();
      const opener = document.querySelector('.ad [data-row="conversation"]');
      (opener.matches('button') ? opener : opener.querySelector('button')).click();
      while (performance.now() - t0 < 4000) {
        await new Promise(r => requestAnimationFrame(r));
        const body = document.querySelector('.ad .pg-body'), box = body?.getBoundingClientRect();
        const shown = body ? [...body.querySelectorAll('.pg-sec, .pg-input')].filter(e => { const r = e.getBoundingClientRect(); return r.bottom > box.top && r.top < box.bottom; }) : [];
        if (shown.length && shown.every(e => Number(getComputedStyle(e).opacity) > .95)) return Math.round(performance.now() - t0);
      }
      return null;
    });
    await page.waitForTimeout(2500);
    // Re-read the record: a turn may have landed since the start (the page polls every 2 s).
    const newest = [...(await get('/inherent/conversation?after=0')).rows].reverse().find(r => r.source !== 'allen');
    check(`R the Conversation page is the record, newest last (${newest.seq})`, words(await page.locator('.ad .tr-jarvis .md').last().textContent()).endsWith(words(newest.text)));
    const scroll = await page.locator('.ad .pg-body').evaluate(b => ({ top: b.scrollTop, view: b.clientHeight, height: b.scrollHeight }));
    check(`R it opens on the newest turn, not the top (${Math.round(scroll.top)} + ${scroll.view} of ${scroll.height} px)`, scroll.height > scroll.view && scroll.top + scroll.view >= scroll.height - 2);
    check(`R the words on screen are in within 800 ms of the click (${litMs} ms)`, litMs !== null && litMs < 800);
    const shownDays = () => page.locator('.ad .tr').evaluateAll(els => [...new Set(els.map(e => e.dataset.day))]);
    const opening = await shownDays();
    check(`R it holds the newest day only (${opening.join()})`, opening.length === 1 && opening[0] === new Date(newest.ts).toDateString());
    const held = await pullUp();
    const pulled = await shownDays();
    check(`R a fresh scroll up at the top adds the day before, the words in view staying put (${pulled.join(' + ')})`, pulled.length === 2 && pulled[1] === opening[0] && held);
    await panelShot('R-conversation'); await back();
    check(`R nothing was written to the daemon (${posts.length} POSTs refused: ${[...new Set(posts)].join(', ')})`, pluginOps.every(o => ['read', 'icon'].includes(o.operation)));
    check('no page errors', errors.length === 0);
  } else {
    await page.waitForFunction(() => window.__sockets?.length === 1);
    await page.waitForTimeout(300);
    check('L1 she joins the daemon link and syncs its controls', posts.some(p => p.path === '/inherent/controls' && !Object.keys(p.body).length));

    // A poke turns on wave mode; the daemon's phases then drive her.
    await move(out.x, out.y); await waitPlace('out');
    await hit.click({ force: true }); await page.waitForTimeout(600);
    check('L2 a poke asks the daemon for wave mode', posts.at(-1)?.path === '/inherent/controls' && posts.at(-1).body.conversation === true);
    check('L2 in wave mode she listens, strip open, one of her listening faces', await page.locator('.companion-strip.is-open').count() === 1 && await face('35', '35b') === '35');
    await page.evaluate(() => window.__emit('voice', { phase: 'listening', turn_id: 'v1' }));
    await page.waitForTimeout(150);
    check('L3 speech heard lights the strip', await page.locator('.companion-strip.is-hearing').count() === 1);
    await page.evaluate(() => window.__emit('voice', { phase: 'accepted', turn_id: 'v1', text: '明天早上九点提醒我开会' }));
    await page.waitForTimeout(150);
    check('L3 the accepted words show in the strip and she takes them in', await text('.strip-text') === '明天早上九点提醒我开会' && await face('31', '31b', '31c', '31d') === '31');
    await shot('L3-heard', { x: 0, y: 0, width: 400, height: 240 });
    await page.evaluate(() => window.__emit('open', { turn_id: 'v1', response_id: 'resp-1' }));
    check('L3 then she thinks', await face('30') === '30');
    await page.evaluate(() => { window.__emit('append', { turn_id: 'v1', token: '<voice>好的，明早九点' }); window.__emit('append', { turn_id: 'v1', token: '提醒你开会。</voice>' }); });
    await page.locator('.companion-bubble.is-open').waitFor();
    check('L3 her bubble says the spoken form, tags gone, with her replying face', await text('.bubble-text span:last-child') === '好的，明早九点提醒你开会。' && await face('39', '39b', '39c') === '39');
    await page.waitForTimeout(500); await shot('L3-reply', { x: 0, y: 0, width: 400, height: 240 });
    await hit.click({ force: true }); await page.waitForTimeout(600);
    check('L4 a poke while she speaks cuts the answer off', posts.at(-1)?.path === '/inherent/cancel-response' && posts.at(-1).body.response_id === 'resp-1');
    await page.evaluate(() => { window.__emit('done', { turn_id: 'v1', fadeMs: 200 }); window.__emit('voice', { phase: 'spoken', turn_id: 'v1' }); });
    await page.waitForFunction(() => !document.querySelector('.companion-bubble.is-open'), null, { timeout: 3000 });
    check('L4 after the answer settles she is back to listening', await page.locator('.companion-strip.is-open').count() === 1);
    const only = async () => [await page.locator('.companion-bubble.is-open').count(), await page.locator('.companion-strip.is-open').count()].join();
    const turn = (id, heard, said) => page.evaluate(([id, heard, said]) => { window.__emit('voice', { phase: 'listening', turn_id: id }); window.__emit('voice', { phase: 'accepted', turn_id: id, text: heard });
      window.__emit('open', { turn_id: id, response_id: `resp-${id}` }); window.__emit('append', { turn_id: id, token: `<voice>${said}</voice>` }); }, [id, heard, said]);
    // A long answer: its fade (`done` + fadeMs) is over while she is still saying it.
    await turn('v1b', '讲讲今天的安排', '上午十点有组会，下午两点和导师见面，晚上七点健身。');
    await page.evaluate(() => window.__emit('done', { turn_id: 'v1b', fadeMs: 150 }));
    await page.waitForTimeout(500);
    check('L4 a long answer stays up while she still says it, the strip shut', await only() === '1,0' && await face('39', '39b', '39c') === '39');
    await page.evaluate(() => window.__emit('voice', { phase: 'spoken', turn_id: 'v1b' }));
    await page.waitForTimeout(150);
    check('L4 it goes when she stops talking, and she listens again', await only() === '0,1');
    // A short answer: she stops talking before its fade is over; it keeps the spot until then.
    await turn('v1c', '能听到我说话吗', '能听到，Allen。我在。');
    await page.evaluate(() => { window.__emit('done', { turn_id: 'v1c', fadeMs: 150 }); window.__emit('voice', { phase: 'spoken', turn_id: 'v1c' }); });
    await page.waitForTimeout(50);
    check('L4 a short answer keeps the spot while it fades, the strip shut', await only() === '1,0');
    await page.waitForFunction(() => !document.querySelector('.companion-bubble.is-open'), null, { timeout: 3000 });
    // Cutting in: the daemon hears you, stops her (`spoken`), then transcribes; her words hold until yours are in.
    await turn('v1d', '再讲一遍', '好的，上午十点有组会，下午两点和导师见面……');
    await page.evaluate(() => window.__emit('done', { turn_id: 'v1d', fadeMs: 100 }));
    await page.waitForTimeout(300);
    await page.evaluate(() => { window.__emit('voice', { phase: 'listening', turn_id: 'v1e' }); window.__emit('voice', { phase: 'spoken', turn_id: 'v1d' }); });
    await page.waitForTimeout(150);
    const cutIn = await only();
    await page.evaluate(() => window.__emit('voice', { phase: 'transcribing', turn_id: 'v1e' }));
    await page.waitForTimeout(150);
    check('L4 cutting in, her words stay while yours come in', cutIn === '1,0' && await only() === '1,0');
    await page.evaluate(() => window.__emit('voice', { phase: 'accepted', turn_id: 'v1e', text: '等一下，下午那个改到三点' }));
    await page.waitForTimeout(150);
    check('L4 once yours are in, the strip shows them whole', await only() === '0,1' && await text('.strip-text') === '等一下，下午那个改到三点');
    await page.evaluate(() => { window.__emit('open', { turn_id: 'v1e', response_id: 'resp-v1e' }); window.__emit('append', { turn_id: 'v1e', token: '<voice>好，改到三点。</voice>' }); });
    await page.waitForTimeout(150);
    check('L4 then her answer', await only() === '1,0' && await text('.bubble-text span:last-child') === '好，改到三点。');
    await page.evaluate(() => { window.__emit('done', { turn_id: 'v1e', fadeMs: 100 }); window.__emit('voice', { phase: 'spoken', turn_id: 'v1e' }); });
    await page.waitForFunction(() => document.querySelector('.companion-strip.is-open') && !document.querySelector('.companion-bubble.is-open'), null, { timeout: 3000 });
    // A split sentence (ADR 0053): the first half's answer text lands while he says the second half; she only listens.
    await page.evaluate(() => { window.__emit('voice', { phase: 'listening', turn_id: 'v1f' }); window.__emit('voice', { phase: 'accepted', turn_id: 'v1f', text: '怎么说' }); });
    await page.waitForTimeout(300); // live, the second half starts after the first is accepted
    await page.evaluate(() => { window.__emit('voice', { phase: 'listening', turn_id: 'v1g' }); window.__emit('open', { turn_id: 'v1f', response_id: 'resp-v1f' }); window.__emit('append', { turn_id: 'v1f', token: '<voice>你想让我怎么说？</voice>' }); });
    await page.waitForTimeout(150);
    check('L4 an answer written while he still talks stays off screen; she listens', await only() === '0,1' && await page.locator('.companion-strip.is-hearing').count() === 1 && await face('35', '35b') === '35');
    await page.evaluate(() => { window.__emit('voice', { phase: 'transcribing', turn_id: 'v1g' }); window.__emit('voice', { phase: 'accepted', turn_id: 'v1g', text: '到一半停了' }); window.__emit('cancelled', { turn_id: 'v1f' }); });
    await page.waitForTimeout(150);
    check('L4 then the strip shows his second half, still no bubble', await only() === '0,1' && await text('.strip-text') === '到一半停了');
    await page.evaluate(() => { window.__emit('open', { turn_id: 'v1g', response_id: 'resp-v1g' }); window.__emit('append', { turn_id: 'v1g', token: '<voice>刚才是我说到一半断了。</voice>' }); });
    await page.waitForTimeout(150);
    check('L4 and one answer to both halves', await only() === '1,0' && await text('.bubble-text span:last-child') === '刚才是我说到一半断了。');
    await page.evaluate(() => { window.__emit('done', { turn_id: 'v1g', fadeMs: 100 }); window.__emit('voice', { phase: 'spoken', turn_id: 'v1g' }); });
    await page.waitForFunction(() => document.querySelector('.companion-strip.is-open') && !document.querySelector('.companion-bubble.is-open'), null, { timeout: 3000 });
    // A cough as her answer is written (ADR 0053): the answer waits for it; when it comes to nothing she says it, with
    // her speaking face, and a poke stops that answer. Her face follows the turn, never the microphone.
    await page.evaluate(() => { window.__emit('voice', { phase: 'listening', turn_id: 'v1h' }); window.__emit('voice', { phase: 'accepted', turn_id: 'v1h', text: '明天几点开会' }); });
    await page.waitForTimeout(150);
    await page.evaluate(() => { window.__emit('voice', { phase: 'listening', turn_id: 'v1i' }); window.__emit('open', { turn_id: 'v1h', response_id: 'resp-v1h' }); window.__emit('append', { turn_id: 'v1h', token: '<voice>明天上午十点。</voice>' }); });
    await page.waitForTimeout(150);
    const coughing = await only();
    await page.evaluate(() => window.__emit('voice', { phase: 'empty', turn_id: 'v1i' }));
    await page.waitForTimeout(150);
    check('L4 a cough holds her answer, then she says it with her speaking face', coughing === '0,1' && await only() === '1,0'
      && await text('.bubble-text span:last-child') === '明天上午十点。' && await face('39', '39b', '39c') === '39');
    await hit.click({ force: true }); await page.waitForTimeout(600);
    check('L4 and a poke stops that answer', posts.at(-1)?.path === '/inherent/cancel-response' && posts.at(-1).body.response_id === 'resp-v1h');
    await page.evaluate(() => { window.__emit('done', { turn_id: 'v1h', fadeMs: 100 }); window.__emit('voice', { phase: 'spoken', turn_id: 'v1h', output_outcome: 'dropped' }); });
    await page.waitForFunction(() => document.querySelector('.companion-strip.is-open') && !document.querySelector('.companion-bubble.is-open'), null, { timeout: 3000 });
    await page.locator('.strip-stop').click(); await page.waitForTimeout(300);
    check('L5 the strip’s stop ends wave mode', posts.at(-1)?.path === '/inherent/controls' && posts.at(-1).body.conversation === false && await page.locator('.companion-strip.is-open').count() === 0);
    await move(600, 560); await waitPlace('home');

    // A wake-word turn she did not start still brings her out, and markdown never shows.
    await page.evaluate(() => window.__emit('voice', { phase: 'listening', turn_id: 'v2' }));
    await waitPlace('out');
    check('L6 a wake-word turn brings her out listening', await page.locator('.companion-strip.is-open').count() === 1);
    await page.evaluate(() => { window.__emit('voice', { phase: 'accepted', turn_id: 'v2', text: '几点了' }); window.__emit('open', { turn_id: 'v2', response_id: 'resp-2' }); window.__emit('append', { turn_id: 'v2', token: '现在是 **下午四点**。' }); });
    await page.locator('.companion-bubble.is-open').waitFor();
    check('L6 her bubble drops markdown', await text('.bubble-text span:last-child') === '现在是 下午四点。');
    await page.evaluate(() => { window.__emit('done', { turn_id: 'v2', fadeMs: 200 }); window.__emit('voice', { phase: 'spoken', turn_id: 'v2' }); });
    await waitPlace('home');
    check('L6 once it settles she goes home, wave mode off', await page.locator('.companion-strip.is-open').count() === 0);
    // A turn that fails says why in Jarvis's own words (a refused key, no credit…) where the answer would be.
    const quota = 'The account is out of credit. Add credit, then try again.';
    await page.evaluate(() => { window.__emit('voice', { phase: 'listening', turn_id: 'v2f' }); window.__emit('voice', { phase: 'accepted', turn_id: 'v2f', text: 'what is on today' }); });
    await waitPlace('out');
    await page.evaluate(text => window.__emit('failed', { turn_id: 'v2f', reason: 'quota', message: text }), quota);
    const said = await page.waitForFunction(text => document.querySelector('.companion-bubble.is-open .bubble-text span:last-child')?.textContent === text, quota, { timeout: 5000 }).then(() => true, () => false);
    check('L6 a failed turn shows the daemon’s reason in her bubble, with her sorry face', said && await face('38') === '38');
    await page.waitForTimeout(700); await shot('L6-failed', { x: 0, y: 0, width: 400, height: 240 });
    await page.waitForFunction(() => !document.querySelector('.companion-bubble.is-open'), null, { timeout: 10_000 });
    await move(600, 560); await waitPlace('home');
    check('L6 and after 8 s it leaves and she goes home', true);
    // A poke while she thinks: that turn has no answer to name yet, so it is stopped by its turn, not the last answer's id.
    await page.evaluate(() => window.__emit('voice', { phase: 'listening', turn_id: 'v2t' }));
    await waitPlace('out');
    await page.evaluate(() => window.__emit('voice', { phase: 'accepted', turn_id: 'v2t', text: '帮我查一下航班' }));
    const thinks = await face('30');
    const posted = posts.length;
    await hit.click({ force: true }); await page.waitForTimeout(600);
    const stops = posts.slice(posted).filter(p => p.path === '/inherent/cancel-response');
    check('L6 a poke while she thinks stops that turn by its id', thinks === '30' && stops.length === 1 && stops[0].body.turn_id === 'v2t' && !stops[0].body.response_id);
    await page.evaluate(() => window.__emit('cancelled', { turn_id: 'v2t' }));
    await move(600, 560); await waitPlace('home');

    // Typed text from her own box goes to the daemon.
    await move(out.x, out.y); await waitPlace('out');
    await move(out.x + 26 + 12 + 16, out.y);
    await page.locator('.companion-chip button').click();
    await page.waitForFunction(() => document.activeElement?.matches('.companion-composer input'));
    await page.keyboard.type('帮我看看日程', { delay: 30 });
    await page.keyboard.press('Enter');
    await page.waitForTimeout(300);
    check('L7 her text box submits to the daemon', posts.at(-1)?.path === '/inherent/submit' && posts.at(-1).body.text === '帮我看看日程');
    await move(600, 560); await page.waitForTimeout(1200);
    check('L7 she thinks about a typed turn too, out of the island', await face('30') === '30' && await page.locator('.companion-hit').getAttribute('data-place') === 'out');
    const offline = 'Jarvis could not reach the model. Check the network, then try again.';
    await page.evaluate(text => window.__emit('failed', { turn_id: 'typed-1', reason: 'network', message: text }), offline);
    const typedSaid = await page.waitForFunction(text => document.querySelector('.companion-bubble.is-open .bubble-text span:last-child')?.textContent === text, offline, { timeout: 3000 }).then(() => true, () => false);
    check('L7 and its failure says why in her bubble', typedSaid && await face('38') === '38');
    await page.waitForFunction(() => !document.querySelector('.companion-bubble.is-open'), null, { timeout: 10_000 });
    await waitPlace('home');

    // The Dashboard on live data.
    await island();
    await page.locator('.companion-dashboard.is-open').waitFor();
    await page.waitForTimeout(1500);
    check('L8 your turn 9 min ago keeps the conversation on top: your words, then the answer on record, plain',
      (await text('.ad .r-talk .you')).endsWith(rows[0].text) && await text('.ad .r-talk .say') === 'Two things: the voice test at four, and the demo cut.');
    check('L8 Now, Usage and Projects come from the daemon', await text('.ad .r-now .text') === 'Wiring the companion to the daemon.' && (await text('.ad .r-usage .dial b')).startsWith('41') && await text('.ad .pj-mini b') === 'jarvis');
    check('L8 the Agents row counts live sessions', (await text('.ad .r-agents .pill')) === '1 needs you' && (await text('.ad .r-agents .text')).includes('Wire the companion'));
    check('L8 the Plugins tile is the live catalog, connected first, with a manifest logo where there is one', (await page.locator('.ad .pl-mini i').allTextContents()).join('') === 'N' && await page.locator('.ad .pl-mini i:first-child img[src^="data:image/svg"]').count() === 1 && (await text('.ad [data-row="plugins"] .meta')) === '1 on');
    await panelShot('L8-home');
    // ADR 0074: an answer written while Allen is still talking is dropped once his words are in; the home row never shows it.
    const homeSay = () => text('.ad .r-talk .say');
    await page.evaluate(() => { window.__emit('voice', { phase: 'listening', turn_id: 'v8b' }); window.__emit('open', { turn_id: 'v8a', response_id: 'resp-v8a' }); window.__emit('append', { turn_id: 'v8a', token: '<voice>这句会被丢掉。</voice>' }); });
    await page.waitForTimeout(200);
    const whileTalking = await homeSay();
    await page.evaluate(() => { window.__emit('voice', { phase: 'accepted', turn_id: 'v8b', text: '算了不用了' }); window.__emit('cancelled', { turn_id: 'v8a' }); window.__emit('cancelled', { turn_id: 'v8b' }); });
    await page.waitForTimeout(200);
    check('L8 an answer written while he talks never shows on the home row', whileTalking === 'Two things: the voice test at four, and the demo cut.' && await homeSay() === whileTalking);
    await openRow('conversation'); await page.waitForTimeout(900);
    check('L8 the Conversation page shows the record as turns', await page.locator('.ad .tr').count() === 1 && (await text('.ad .tr-you p')) === rows[0].text);
    rows.push({ seq: 13, id: 'c', ts: iso(Date.now()), source: 'allen', text: 'Move the test to five' });
    await page.locator('.ad .pg-input input').fill('Move the test to five');
    await page.locator('.ad .pg-input input').press('Enter');
    await page.waitForTimeout(2600);
    check('L8 its text box submits, and the new row arrives from the record', posts.at(-1)?.path === '/inherent/submit' && posts.at(-1).body.text === 'Move the test to five' && await page.locator('.ad .tr').count() === 2);
    // As the daemon does it: the record is written first, then the answer streams as spoken segments without its line breaks.
    const doc = '## Moved\n\n- **5 PM** voice test\n  - bring `reSpeaker`\n\n| When | What |\n|---|---|\n| 17:00 | voice test |';
    rows.push({ seq: 14, id: 'd', ts: iso(Date.now()), source: 'jarvis', text: doc });
    await page.evaluate(() => { window.__seen = []; new MutationObserver(() => window.__seen.push(document.querySelector('.ad .pg-body').textContent)).observe(document.querySelector('.ad .pg-body'), { subtree: true, childList: true, characterData: true }); });
    const emitted = Date.now();
    await page.evaluate(tokens => { window.__emit('open', { turn_id: 'typed-2', response_id: 'resp-3' }); for (const token of tokens) window.__emit('append', { turn_id: 'typed-2', token }); },
      ['<voice>', 'Moved to five.', '</voice>', '<document>', '## Moved', '- **5 PM** voice test', '- bring `reSpeaker`', '| When | What |', '|---|---|', '| 17:00 | voice test |', '</document>']);
    await page.waitForTimeout(500);
    const md = page.locator('.ad .tr-jarvis .md').last(), asked = reads.find(r => r.at >= emitted), seen = await page.evaluate(() => window.__seen);
    check(`L8 the answer comes from its row, asked for as it starts (${asked ? asked.at - emitted : '-'} ms), never the run-together stream`, !!asked && asked.at - emitted < 150
      && words(await md.textContent()) === words(doc) && await page.locator('.ad .tr').count() === 2 && !seen.some(t => /Moved to five|##|voice test- bring/.test(t)));
    check('L8 the answer\'s markdown renders: heading, bold, nested list, code, table', await md.locator('h5').textContent() === 'Moved' && await md.locator('li strong').textContent() === '5 PM'
      && await md.locator('li > ul > li code').textContent() === 'reSpeaker' && (await md.locator('th').allTextContents()).join() === 'When,What'
      && (await md.locator('td').allTextContents()).join() === '17:00,voice test' && !/\*\*|##|\|/.test(await md.textContent()));
    await panelShot('L8-conversation');
    await page.evaluate(() => { window.__emit('done', { turn_id: 'typed-2', fadeMs: 100 }); window.__emit('voice', { phase: 'spoken', turn_id: 'typed-2' }); });
    const days = () => page.locator('.ad .tr').evaluateAll(els => [...new Set(els.map(e => e.dataset.day))].length);
    check('L8 it holds today only, and offers earlier', await days() === 1 && (await text('.ad .pg-earlier')).includes('earlier'));
    const held = await pullUp(), longer = reads.some(r => r.limit > 2), shown = await days(), first = await text('.ad .tr-you p'), top = await text('.ad .pg-earlier');
    check(`L8 a fresh scroll up at the top fetches a longer page and adds yesterday, the words in view staying put (longer page ${longer}, ${shown} days, held ${held}, top "${top}")`,
      longer && shown === 2 && held && first === yesterday[0].text && top === 'Start of the conversation');
    await panelShot('L8-yesterday');
    await back();
    await openRow('conversation'); await page.waitForTimeout(900);
    check('L8 opened again it holds today only, yesterday one scroll up', await days() === 1 && (await text('.ad .pg-earlier')) === 'Scroll up for yesterday');
    await back();

    await openRow('agents'); await page.waitForTimeout(900);
    const titles = await page.locator('.ad .ag-title').allTextContents();
    check('L9 Agents shows Claude and Codex sessions from the daemon, grouped', titles.join('|') === 'Wire the companion|Nightly audit|fix the overlay');
    check('L9 Claude cards carry their terminal, background jobs say so', (await page.locator('.ad [data-id="c-wait"] .ag-tags').textContent()).includes('Ghostty') && (await page.locator('.ad [data-id="c-bg"] .ag-tags').textContent()).includes('Background'));
    await page.locator('.ad [data-id="c-wait"] .ag-head').click(); await page.waitForTimeout(400);
    check('L9 an open Claude card shows your words and what it waits on, with no dead jump button', (await text('.ad [data-id="c-wait"] .ag-you')).includes('wire the whole backend') && await page.locator('.ad [data-id="c-wait"] .ag-go').count() === 0 && await page.locator('.ad [data-id="c-wait"] .btn-glow').count() === 0);
    await page.locator(`.ad [data-id="${codexId}"] .ag-head`).click(); await page.waitForTimeout(400);
    await page.locator(`.ad [data-id="${codexId}"] .ag-go`).click(); await page.waitForTimeout(200);
    check('L9 a Codex card opens its thread', (await page.evaluate(() => window.__state.opened)).includes(codexId));
    await page.locator('.ad [data-id="c-bg"] .ag-x').click({ force: true }); await page.waitForTimeout(300);
    check('L9 a hidden session is remembered', await page.locator('.ad [data-id="c-bg"]').count() === 0 && (await page.evaluate(() => localStorage.getItem('companion-hidden-agents-v1'))).includes('c-bg'));
    await panelShot('L9-agents'); await back();

    await openRow('plugins'); await page.waitForTimeout(900);
    check('L10 the catalog is the daemon’s, with each status', (await page.locator('.ad .pl-name').allTextContents()).join('|') === 'LinearConnected|NotionNeeds sign-in');
    await page.locator('.ad [data-plugin="notion"]').click(); await page.waitForTimeout(600);
    check('L10 opening a plugin opens its request and its sign-in', pluginOps.some(o => o.operation === 'open' && o.data.plugin_id === 'notion') && (await text('.ad .pl-det .btn-glow')) === 'Sign in');
    await page.locator('.ad .pl-det .btn-glow').click(); await page.waitForTimeout(2000);
    check('L10 sign in connects that request and waits for the browser', pluginOps.some(o => o.operation === 'connect' && o.data.request_id === snapshot.request.id) && (await text('.ad .pl-det .waiting p')).includes('browser') && await face('36') === '36');
    await page.locator('.ad .pl-det .btn-text').click(); await page.waitForTimeout(2000);
    check('L10 cancel cancels it', pluginOps.some(o => o.operation === 'cancel' && o.data.request_id === snapshot.request.id));
    await back(); await back();

    // The Usage page: each service opens its own page, and a Codex reset takes two clicks, never one.
    await openRow('usage'); await page.waitForTimeout(900);
    await page.locator('.ad .us-plan .us-link', { hasText: 'Codex' }).click();
    await page.locator('.ad .bal .us-link', { hasText: 'DeepSeek' }).click();
    check('L12 a service name and a balance open that service\'s own page', (await page.evaluate(() => window.__state.accounts)).join() === 'codex,deepseek');
    const synced = refreshes.length;
    await page.locator('.ad .us-sync').click(); await page.waitForTimeout(400);
    check('L12 the synced time refreshes every source', refreshes.length === synced + 1);
    await page.locator('.ad .us-use').dblclick(); await page.waitForTimeout(100);
    await page.locator('.ad .us-confirm .btn-glow').click({ force: true });
    check('L12 a double click on Use reset, then Yes right away, spends nothing and asks', resets.length === 0 && (await text('.ad .us-confirm b')) === 'Use this reset?' && (await text('.ad .us-confirm p')).includes('only reset'));
    await page.waitForTimeout(700); await panelShot('L12-usage-ask');
    await page.locator('.ad .us-confirm .btn-text').click(); await page.waitForTimeout(200);
    check('L12 No, go back closes it without spending', resets.length === 0 && await page.locator('.ad .us-confirm').count() === 0 && await page.locator('.ad .us-use').count() === 1);
    resetFails = 1;
    await page.locator('.ad .us-use').click(); await page.waitForTimeout(700);
    await page.locator('.ad .us-confirm .btn-glow').click(); await page.waitForTimeout(300);
    check('L12 a failed reset says why and offers Try again', resets.length === 1 && (await text('.ad .us-confirm p')) === 'Could not reach Jarvis. Try again.' && (await text('.ad .us-confirm .btn-glow')) === 'Try again');
    await panelShot('L12-usage-failed');
    const before = refreshes.length;
    await page.locator('.ad .us-confirm .btn-glow').click(); await page.waitForTimeout(400);
    check('L12 Try again resends the same request id for Codex, so it cannot spend twice', resets.length === 2 && resets[1].id === resets[0].id && /^[0-9a-f]{8}-[0-9a-f]{4}-/.test(resets[0].id) && resets.every(r => r.service === 'codex'));
    check('L12 a reset closes the question, says so, and re-reads usage', await page.locator('.ad .us-confirm').count() === 0 && (await text('.ad .toast')) === 'Codex limits reset' && refreshes.length > before);

    // OpenAI: only the models that cost money today; the OpenAI balance Allen records himself (ADR 0065).
    const listed = () => page.locator('.ad .spend li:not(:has(.more))').allTextContents();
    check('L13 OpenAI lists only the models that cost a cent today, the rest fold', (await listed()).join('|') === 'gpt-5.6-luna$0.28|gpt-5.4-mini$0.04' && (await text('.ad .spend .more')) === '3 more at $0.00');
    await page.locator('.ad .spend .more').click();
    check('L13 the fold opens every model', (await listed()).length === 5 && (await text('.ad .spend .more')) === 'Show less');
    const balanceCard = name => page.locator('.ad .bal-card', { hasText: name });
    check('L13 the OpenAI balance says since when, with Update; MiniMax shows its own, with nothing to type', (await balanceCard('OpenAI').locator('b').textContent()) === '≈ $23.25' && (await balanceCard('OpenAI').locator('small').textContent()) === 'since Sep 25' && (await balanceCard('OpenAI').locator('.bal-set').textContent()) === 'Update'
      && (await balanceCard('MiniMax').locator('b').textContent()) === '$15.36' && await balanceCard('MiniMax').locator('.bal-set').count() === 0);
    await page.locator('.ad .pg-body').evaluate(b => { b.scrollTop = 0; }); await page.waitForTimeout(300);
    check('L13 balances open the page, Set in view without scrolling, and a debt reads -$0.10', await page.locator('.ad .pg-body > .pg-sec').first().locator('.bal').count() === 1
      && await balanceCard('OpenAI').locator('.bal-set').evaluate(el => { const r = el.getBoundingClientRect(), b = el.closest('.pg-body').getBoundingClientRect(); return r.bottom <= b.bottom && r.top >= b.top; })
      && (await balanceCard('DeepSeek').locator('b').textContent()) === '-$0.10');
    await panelShot('L13-usage-balances');
    await balanceCard('OpenAI').locator('.bal-set').click(); await page.waitForTimeout(300);
    const save = page.locator('.ad .bal-card.is-editing .btn-glow');
    const emptyDisabled = await save.isDisabled();
    await page.locator('.ad .bal-card.is-editing input').fill('30.5'); await page.waitForTimeout(400);
    await panelShot('L13-usage-typing');
    const beforeSave = refreshes.length;
    await page.locator('.ad .bal-card.is-editing input').press('Enter'); await page.waitForTimeout(500);
    check('L13 typing a balance and Enter records it once, then re-reads usage', emptyDisabled && JSON.stringify(balances) === '[{"service":"openai","usd":30.5}]'
      && await page.locator('.ad .bal-card.is-editing').count() === 0 && refreshes.length > beforeSave && (await text('.ad .toast')) === 'Balance saved');
    await balanceCard('OpenAI').locator('.bal-set').click(); await page.waitForTimeout(300);
    await page.locator('.ad .bal-card.is-editing input').fill('abc'); await page.waitForTimeout(100);
    const lettersIgnored = await page.locator('.ad .bal-card.is-editing input').inputValue() === '' && await save.isDisabled();
    await page.locator('.ad .bal-card.is-editing input').press('Escape'); await page.waitForTimeout(200);
    check('L13 letters are not a balance, and Esc leaves without saving', lettersIgnored && balances.length === 1 && await page.locator('.ad .bal-card.is-editing').count() === 0);
    await back();

    // The home's new sources. Until the daemon serves them, Today says so, the pop-ups stay away and Jarvis's own
    // settings are greyed out; once it does, each block shows what it answers and the writes go where the contract says.
    check('L14 without /inherent/today, Today says the calendar is not connected, and no brief, mail or reminder shows',
      (await text('.ad [data-block="today"] .muted')) === 'Calendar and to-dos aren’t connected yet.' && await page.locator('.ad [data-block="brief"], .ad [data-block="mail"], .ad [data-block="foryou"]').count() === 0);
    await page.locator('.ad .corner .cb').nth(1).click(); await page.waitForTimeout(300);
    check('L14 the corner mute sets speech_muted on the daemon', posts.at(-1)?.path === '/inherent/controls' && posts.at(-1).body.speech_muted === true && await page.locator('.ad .cb.is-muted').count() === 1);
    await page.locator('.ad .corner .cb').nth(1).click(); await page.waitForTimeout(300);
    check('L14 and the second press unmutes it', posts.at(-1)?.body.speech_muted === false && await page.locator('.ad .cb.is-muted').count() === 0);
    await page.locator('.ad .corner [data-row="settings"]').click(); await page.waitForTimeout(900);
    await page.locator('.ad [data-cat="voice"]').click(); await page.waitForTimeout(500);
    check('L14 without /inherent/settings, Jarvis’s own voice settings are greyed out and say they still live in jarvis.yaml',
      (await text('.ad .st-warn')).includes('jarvis.yaml') && await page.locator('.ad .st.is-off').count() === 7);
    await panelShot('L14-settings-missing');
    await back(); await back();
    const today = new Date().toLocaleDateString('en-CA');
    fixtures['/inherent/today'] = { weather: { now_c: 12, summary: 'Rain from 9 PM' },
      events: [{ id: 'e1', title: 'Office hours', start: iso(Date.now() - 60_000), end: iso(Date.now() + 30 * 60_000) }],
      todos: [{ id: 't1', title: 'Send the A3 draft', due: iso(Date.now() + 60_000) }] };
    fixtures['/inherent/brief'] = { date: today, summary: 'Two agents finished overnight.', body: '**Overnight**\n\n- Two agents finished.', items: 1 };
    fixtures['/inherent/mail'] = { unread: [{ id: 'm1', from: 'Prof. Lee', subject: 'Office hours moved', received: iso(Date.now() - 600_000) }] };
    fixtures['/inherent/notices'] = { notices: [{ id: 'n1', text: 'Reminder: call the dentist', at: iso(Date.now()) }] };
    fixtures['/inherent/settings'] = { values: { reply_language: 'follow', wake_threshold: .95, tts_voice: 'Warm Bestie', tts_volume: 1, output_device: 'System default', input_device: 'System default',
      gpt_live: true, mac_aec: false, timesink: true, keep_audio: true, repos: ['jarvis'], model_conversation: 'gpt-5.6-luna', model_background: 'GPT-6 luna', model_report: 'GPT-6 sol' },
      options: { tts_voice: ['Warm Bestie', 'Explorative Girl'], output_device: ['System default'], input_device: ['System default'] } };
    await closeDash();
    await page.waitForFunction(() => !document.querySelector('.companion-dashboard.is-open'), null, { timeout: 3000 });
    await island();
    await page.locator('.companion-dashboard.is-open').waitFor();
    await page.waitForTimeout(1500);
    check('L14 once the daemon answers, Today shows its weather, event and to-do',
      (await text('.ad [data-block="today"] .ev span')) === 'Office hours' && (await text('.ad [data-block="today"] .wx')).includes('12°') && (await text('.ad [data-block="today"] .td span')) === 'Send the A3 draft');
    check('L14 the brief, the mail and Jarvis’s reminder show up', await page.locator('.ad [data-block="brief"]').count() === 1
      && (await text('.ad [data-block="mail"] .ml b')) === 'Prof. Lee' && (await text('.ad [data-block="foryou"]')).includes('call the dentist'));
    await panelShot('L14-home');
    await page.locator('.ad [data-block="today"] .td').click(); await page.waitForTimeout(400);
    check('L14 checking a to-do posts { id, done } to /inherent/today/todo', posts.at(-1)?.path === '/inherent/today/todo' && posts.at(-1).body.id === 't1' && posts.at(-1).body.done === true
      && await page.locator('.ad .td[aria-pressed="true"]').count() === 1);
    await page.locator('.ad .brief-go').click(); await page.waitForTimeout(900);
    check('L14 Read the brief opens its page', await text('.ad .pg-head h3') === 'Morning brief' && (await text('.ad .pg-body')).includes('Two agents finished'));
    await back();
    check('L14 a brief you read does not come back today', await page.locator('.ad [data-block="brief"]').count() === 0);
    await page.locator('.ad .corner [data-row="settings"]').click(); await page.waitForTimeout(900);
    await page.locator('.ad [data-cat="voice"]').click(); await page.waitForTimeout(500);
    await page.locator('.ad .st-opts button', { hasText: 'Explorative Girl' }).click(); await page.waitForTimeout(600);
    check('L14 with /inherent/settings they are live, and a change posts { changes }', await page.locator('.ad .st.is-off').count() === 0
      && posts.at(-1)?.path === '/inherent/settings' && posts.at(-1).body.changes?.tts_voice === 'Explorative Girl'
      && await page.locator('.ad .st-opts button[aria-checked="true"]', { hasText: 'Explorative Girl' }).count() === 1);
    await panelShot('L14-settings-voice');
    // One language switch: Interface language flips her panel and tells Jarvis to say its own phrases in it.
    const langName = () => text('.ad [data-item="lang"] .st-name');
    await back(); await page.locator('.ad [data-cat="general"]').click(); await page.waitForTimeout(500);
    await page.locator('.ad [data-item="lang"] .st-seg button', { hasText: '中文' }).click(); await page.waitForTimeout(600);
    check('L15 Interface language 中文 posts { language: zh } and her panel turns Chinese',
      posts.at(-1)?.path === '/inherent/language' && posts.at(-1).body.language === 'zh' && await langName() === '界面语言');
    await page.locator('.ad [data-item="lang"] .st-seg button', { hasText: 'English' }).click(); await page.waitForTimeout(600);
    check('L15 English posts { language: en } and turns it back', posts.at(-1)?.body.language === 'en' && await langName() === 'Interface language');
    // L16: a first boot's speech-model download shows in the corner until the models are in.
    await back(); await back();
    const corner = () => text('.ad .corner .next-event');
    fixtures['/inherent/setup'] = { keys: { openai: 'bad', minimax: 'missing', tavily: 'missing' }, voice_models: { state: 'downloading', done: 148_000_000, total: 240_193_589 } };
    const downloading = await page.waitForFunction(() => document.querySelector('.ad .corner .next-event')?.textContent === 'Voice · 61%', null, { timeout: 5000 }).then(() => true, () => false);
    check('L16 while the speech models download the corner says how far, from real bytes', downloading);
    await panelShot('L16-voice-downloading');
    fixtures['/inherent/setup'].voice_models = { state: 'ready', done: 0, total: 0 };
    await page.waitForFunction(() => !document.querySelector('.ad .corner .next-event')?.textContent.includes('Voice'), null, { timeout: 5000 }).catch(() => {});
    check('L16 once they are in the corner is the clock again', /\d:\d\d/.test(await corner()));
    // Settings › Accounts takes a new API key: a refused one says why and restarts nothing, a kept one restarts Jarvis.
    await page.locator('.ad .corner [data-row="settings"]').click(); await page.waitForTimeout(900);
    await page.locator('.ad [data-cat="accounts"]').click(); await page.waitForTimeout(900);
    const keyRow = '.ad [data-item="key-openai"]';
    check('L16 Accounts says the saved OpenAI key is refused', (await text(`${keyRow} .st-val`)) === 'Refused');
    keyAnswer = { ok: false, checks: [{ id: 'connect', ok: false, reason: 'unauthorized', detail: '401' }] };
    await page.locator(`${keyRow} input`).fill('sk-wrong'); await page.locator(`${keyRow} .st-key button`).click();
    await page.locator(`${keyRow} .st-why`).waitFor({ timeout: 3000 });
    check('L16 a refused key says why and restarts nothing', (await text(`${keyRow} .st-why`)).includes('(401)')
      && posts.at(-1)?.path === '/inherent/setup/key' && posts.at(-1).body.provider === 'openai' && posts.at(-1).body.key === 'sk-wrong' && !posts.some(p => p.path === '/inherent/restart'));
    await panelShot('L16-key-refused');
    keyAnswer = { ok: true, checks: [{ id: 'connect', ok: true }, { id: 'chat', ok: true }] };
    await page.locator(`${keyRow} input`).fill('sk-right'); await page.locator(`${keyRow} .st-key button`).click();
    await page.waitForTimeout(600);
    check('L16 a kept key restarts Jarvis and empties the field', posts.at(-2)?.path === '/inherent/setup/key' && posts.at(-2).body.key === 'sk-right'
      && posts.at(-1)?.path === '/inherent/restart' && await page.locator(`${keyRow} input`).inputValue() === '' && await page.locator(`${keyRow} .st-why`).count() === 0);
    await back(); await back();
    await closeDash();
    await page.waitForFunction(() => !document.querySelector('.companion-dashboard.is-open'), null, { timeout: 3000 });
    // Jarvis asks for a plugin mid-conversation: the closed panel opens and lands on it.
    snapshot = { ...snapshot, request: request('notion', 'Find last week’s meeting notes.') };
    await page.waitForFunction(() => document.querySelector('.ad .ask-card')?.textContent.includes('last week'), null, { timeout: 4000 });
    check('L11 when Jarvis asks for a plugin the Dashboard opens on it with the reason', await page.locator('.companion-dashboard.is-open').count() === 1 && (await text('.ad .pl-det .btn-glow')) === 'Sign in and continue');
    await page.waitForTimeout(900); await panelShot('L11-asked');

    // L13: agent notices beside the notch (ADR 0049, 0069, 0070). The fake board changes under her: sessions finish,
    // ask, stop. A finish pops its name for 5 s and stays on Allen's turn until he looks at it in Ghostty (a fake
    // front-terminal feed here) or goes to it; a needs-you card hangs from the notch and every answer goes back as a
    // POST for the held prompt. She watches all of it from home.
    await closeDash();
    await page.waitForFunction(() => !document.querySelector('.companion-dashboard.is-open'), null, { timeout: 3000 });
    await waitPlace('home');
    const session = (id, over = {}) => ({ agent: 'claude', session_id: id, kind: 'interactive', job_id: '', phase: 'working', title: id, project: 'jarvis', branch: '', cwd: '/x', where: 'Ghostty',
      prompt: `do ${id}`, activity: 'Working', last_message: '', started_ms: now, updated_ms: now, ...over });
    const board = { 'c-wait': claude[0], 'n-build': session('n-build', { title: 'Build the notices' }), 'n-a': session('n-a', { title: 'Port the sounds' }),
      'n-b': session('n-b', { title: 'Port the faces', kind: 'background', job_id: 'b7a91c02', where: 'background' }), 'n-plan': session('n-plan', { title: 'Plan the wing' }), 'n-x': session('n-x', { title: 'Check the queue' }) };
    const setBoard = (id, over) => { if (id) board[id] = { ...board[id], ...over }; fixtures['/inherent/claude-sessions'] = { sessions: Object.values(board), error: null }; };
    const note = () => page.locator('.notch-note.is-open');
    const noteUp = () => note().waitFor({ timeout: 5000 });
    const noteGone = () => page.waitForFunction(() => !document.querySelector('.notch-note.is-open'), null, { timeout: 7000 });
    const marks = () => page.locator('.notch').getAttribute('data-marks');
    const settle = async () => { await move(600, 560); await page.waitForTimeout(900); };
    const ghostty = (title, front = true) => page.evaluate(([title, front]) => window.__ghostty({ front, title }), [title, front]);
    const answered = path => posts.filter(p => p.path === `/inherent/claude-requests/${path}`).map(p => p.body);
    const drop = page.locator('.notch-drop.is-open');
    const noteShot = name => shot(name, { x: 80, y: 0, width: 560, height: 420 });
    const beaconX = 412.5 + 6 + 9, slotX = i => beaconX + 30 * i;
    const heads = () => drop.locator('.a-h > span').allTextContents();
    const rowsOf = sec => drop.locator(`.a-sec[data-sec="${sec}"] .a-row b`).allTextContents();
    const weights = sec => drop.locator(`.a-sec[data-sec="${sec}"] .a-row b`).evaluateAll(els => [...new Set(els.map(e => getComputedStyle(e).fontWeight))].join());
    const rowAct = async (name, label) => { const row = drop.locator('.a-row', { hasText: name }); await row.hover(); await page.waitForTimeout(150); await row.locator(`[aria-label^="${label}"]`).click(); await page.waitForTimeout(300); };
    setBoard(); await page.waitForTimeout(2500);
    check(`L13 sessions met for the first time pop nothing: the waiting one is on his turn, six working share one mark (${await marks()})`,
      await note().count() === 0 && (await marks()) === 'turn1 work6' && await page.evaluate(() => window.__state.watch === true));
    setBoard('n-build', { phase: 'done', last_message: '**Done.** The wing and the cards are in.\n- 72 checks pass' });
    await noteUp(); await page.waitForTimeout(700);
    check('L13 a finished session pops its name only, out of the island, while she stays home in her done face',
      (await text('.notch-note .c-label')) === 'Done' && (await page.locator('.notch-note .u-row').allTextContents()).join('|') === 'Build the notices'
      && !(await text('.notch-note')).includes('checks pass') && await face('fin') === 'fin' && await hit.getAttribute('data-place') === 'home');
    await noteShot('L13-pop');
    await noteGone(); await settle();
    check(`L13 after 5 s the pop folds into the beacon; the session stays on his turn (${await marks()})`, (await marks()) === 'turn2 work5');
    await move(beaconX, 14); await drop.waitFor(); await page.waitForTimeout(700);
    check(`L13 the panel starts with his turn: the one asking first, warm, then the finished one; both bold, the working ones not (${(await heads()).join('|')})`,
      (await rowsOf('turn')).join('|') === 'Wire the companion|Build the notices' && await drop.locator('.a-sec[data-sec="turn"] .a-row.is-ask').count() === 1
      && (await heads()).join('|') === 'Your turn2|Working5' && Number(await weights('turn')) >= 600 && (await weights('work')) === '400');
    await noteShot('L13-your-turn');
    await settle();
    await ghostty('Build the notices'); await page.waitForTimeout(2200);
    check(`L13 1.5 s on its Ghostty terminal reads it: it leaves his turn for Finished (${await marks()})`, (await marks()) === 'turn1 work5 done1');
    await ghostty('zsh');
    setBoard('n-a', { phase: 'done', last_message: 'Sounds ported.' }); setBoard('n-b', { phase: 'done', last_message: 'Faces ported.' });
    await noteUp(); await page.waitForTimeout(700);
    check('L13 two finishes together share one pop', (await text('.notch-note .c-label')) === '2 done' && await page.locator('.notch-note .u-row').count() === 2);
    await page.locator('.notch-note .u-row', { hasText: 'Port the sounds' }).hover();
    await page.locator('.notch-note .u-row', { hasText: 'Port the sounds' }).locator('[aria-label="Mark as read"]').click(); await page.waitForTimeout(300);
    check('L13 ✕ marks one read and it leaves the pop', (await page.locator('.notch-note .u-row').allTextContents()).join('|') === 'Port the faces');
    await page.locator('.notch-note .u-row', { hasText: 'Port the faces' }).click();
    await noteGone(); await settle();
    check(`L13 a click goes to it: a background session attaches by its job id in Ghostty, and it is read (${await marks()})`,
      JSON.stringify(await page.evaluate(() => window.__state.jumps)) === '[["Port the faces","b7a91c02"]]' && (await marks()) === 'turn1 work3 done3');
    await ghostty('Check the queue'); await page.waitForTimeout(1800);
    setBoard('n-x', { phase: 'done', last_message: 'Queue checked.' }); await page.waitForTimeout(2600);
    check(`L13 a session finishing while he looks at it pops nothing and goes straight to Finished (${await marks()})`,
      await note().count() === 0 && (await marks()) === 'turn1 work2 done4');

    setBoard('c-wait', { request: { id: 'r-bash', tool: 'Bash', input: { command: 'npm run build', description: 'Build the desktop app' }, cwd: '/x/jarvis/desktop/resonance', always: "Don't ask again for Bash(npm run build:*)" } });
    await noteUp(); await page.waitForTimeout(700);
    check('L13 a held Bash prompt hangs a card out of the island with its command, Park, Deny, Always and Allow; she waits on you from home',
      (await text('.notch-note .nc-label')) === 'Needs your OK' && (await text('.notch-note .nc-cmd')).includes('npm run build') && (await text('.notch-note .nc-go')) === 'Open in Ghostty'
      && (await text('.notch-note .nc-x')) === 'Park' && (await page.locator('.notch-note .nc-choice .btn').allTextContents()).join('|') === 'Deny|Always|Allow'
      && await face('ask') === 'ask' && await hit.getAttribute('data-place') === 'home');
    await noteShot('L13-bash');
    await page.locator('.notch-note .btn-warm').click();
    await page.waitForTimeout(200);
    check('L13 Allow goes back for that prompt and the card confirms', JSON.stringify(answered('r-bash')) === '[{"decision":"allow"}]' && (await text('.notch-note .nc-ok')) === 'Allowed · Claude continues');
    setBoard('c-wait', { request: null }); await noteGone();

    const questions = [{ question: 'Where do the marks go?', header: 'Side', multiSelect: false, options: [{ label: 'Right of the notch', description: 'Recommended' }, { label: 'Left of her island' }] },
      { question: 'Finished cards?', header: 'Finish', multiSelect: false, options: [{ label: 'Expand' }, { label: 'Compact' }] }];
    setBoard('c-wait', { request: { id: 'r-ask', tool: 'AskUserQuestion', input: { questions }, cwd: '/x', always: '' } });
    await noteUp();
    check('L13 a question card asks the first of two', (await text('.notch-note .nc-label')) === 'Claude asks' && (await text('.notch-note .nc-qt')).includes('Where do the marks go?'));
    await page.waitForTimeout(600); await noteShot('L13-ask');
    await page.locator('.notch-note .opt').first().click(); await page.waitForTimeout(500);
    await page.locator('.notch-note .opt').first().click(); await page.waitForTimeout(500);
    check('L13 each pick moves on, and the last step shows every answer', (await page.locator('.notch-note .nc-review li').allTextContents()).join('|') === 'SideRight of the notch|FinishExpand');
    await page.locator('.notch-note .btn-warm').click(); await page.waitForTimeout(200);
    check('L13 the answers go back keyed by question', JSON.stringify(answered('r-ask')) === JSON.stringify([{ decision: 'allow', answers: { 'Where do the marks go?': 'Right of the notch', 'Finished cards?': 'Expand' } }]));
    setBoard('c-wait', { request: null }); await noteGone();

    setBoard('c-wait', { request: { id: 'r-plan', tool: 'ExitPlanMode', input: { plan: '## Plan\n1. Marks\n2. Cards' }, cwd: '/x', always: '' } });
    await noteUp();
    check('L13 a plan card shows the plan with Keep planning and Approve', (await text('.notch-note .nc-label')) === 'Plan to review' && (await text('.notch-note .nc-plan')).includes('Marks'));
    await page.locator('.notch-note .nc-choice .btn-ghost').click();
    await page.locator('.notch-note .pg-input input').fill('Cards first');
    await page.locator('.notch-note .pg-input .send').click(); await page.waitForTimeout(200);
    check('L13 keep planning sends what to change as a deny', JSON.stringify(answered('r-plan')) === '[{"decision":"deny","message":"Cards first"}]');
    setBoard('c-wait', { request: null }); await noteGone();

    setBoard('n-plan', { phase: 'done', error: 'Rate limited: 429 Too Many Requests' });
    await noteUp();
    check('L13 a stopped session pops as stopped, with her error face', (await text('.notch-note .c-label')) === 'Stopped' && await face('34') === '34');
    await page.locator('.notch-note .c-x').click(); await noteGone(); await settle();
    check(`L13 closed by hand, a pop stays on his turn (${await marks()})`, (await marks()) === 'turn2 work1 done4');

    await island();
    await page.locator('.companion-dashboard.is-open').waitFor();
    fixtures['/inherent/codex-sessions'] = { sessions: [{ ...fixtures['/inherent/codex-sessions'].sessions[0], state: 'finished', last_message: 'Overlay fixed.' }] };
    await page.waitForTimeout(3500);
    check('L13 nothing pops while the Dashboard is open', await note().count() === 0);
    await closeDash(); await noteUp();
    check('L13 and it comes up once the Dashboard closes', (await page.locator('.notch-note .u-row').allTextContents()).join('|') === 'fix the overlay');
    await page.locator('.notch-note .u-row').click(); await noteGone();
    check('L13 a Codex session opens its thread', JSON.stringify(await page.evaluate(() => window.__state.opened.slice(-1))) === JSON.stringify([codexId]));

    // Park (先放着): off his turn, into the moon at the far right, quiet until he takes it back or it moves again.
    setBoard('c-wait', { request: { id: 'r-edit', tool: 'Edit', input: { file_path: '/x/jarvis/src/Notices.tsx', old_string: 'const a = 1;', new_string: 'const a = 2;\nconst b = 3;' }, cwd: '/x', always: 'Allow edits for the rest of this session' } });
    await noteUp();
    check('L13 an edit prompt shows the file and its diff', (await text('.notch-note .nc-file')).includes('src/Notices.tsx') && await page.locator('.notch-note .nc-diff code.add').count() === 2);
    await page.locator('.notch-note .nc-x').click(); await noteGone(); await settle();
    check(`L13 Park on the card puts the question into the moon, off his turn (${await marks()})`, await note().count() === 0 && (await marks()) === 'turn1 done5 moon1'
      && agentMarks['c-wait']?.parked_ms > 0);
    await move(slotX(2), 14); await drop.waitFor(); await page.waitForTimeout(600);
    check(`L13 the moon lists what is parked, the question still warm (${(await heads()).join('|')})`, (await heads()).join('|') === 'Your turn1|Finished5|Parked1'
      && (await rowsOf('moon')).join() === 'Wire the companion' && await drop.locator('.a-sec[data-sec="moon"] .a-row.is-ask').count() === 1);
    await noteShot('L13-moon');
    await drop.locator('.a-sec[data-sec="moon"] .a-row').click();
    await noteUp();
    check('L13 its row brings the card back from the moon', (await text('.notch-note .nc-file')).includes('Notices.tsx'));
    await page.locator('.notch-note .btn-ghost', { hasText: 'Always' }).click(); await page.waitForTimeout(200);
    check('L13 Always goes back as always', JSON.stringify(answered('r-edit')) === '[{"decision":"always"}]');
    setBoard('c-wait', { request: null, phase: 'working' }); await noteGone(); await settle();
    check(`L13 working again, it leaves the moon (${await marks()})`, (await marks()) === 'turn1 work1 done5' && !agentMarks['c-wait'].parked_ms);
    await move(beaconX, 14); await drop.waitFor(); await page.waitForTimeout(500);
    await rowAct('Plan the wing', 'Park'); await settle();
    check(`L13 a stopped session parks from its row (${await marks()})`, (await marks()) === 'work1 done5 moon1');
    await move(slotX(2), 14); await drop.waitFor(); await page.waitForTimeout(500);
    await rowAct('Plan the wing', 'Take back'); await settle();
    check(`L13 Take back puts it on his turn again (${await marks()})`, (await marks()) === 'turn1 work1 done5');

    // ⌥Tab: the list held for the keys; → on a finished session opens its page, the conversation from the daemon;
    // a line typed there goes to the session as a reply; while the keys hold the island nothing pops.
    conversations['n-b'] = [{ who: 'you', text: 'port the faces' }, { who: 'it', text: '**Faces ported.**\n- 38 faces, same names' }];
    setBoard('n-b', { replyable: true });
    replied = (id, said) => { conversations[id].push({ who: 'you', text: said }); setBoard(id, { phase: 'working', replyable: false, activity: 'Reading your line' }); };
    await page.waitForTimeout(1700);
    await page.evaluate(() => window.__command('agent-keys')); await drop.waitFor(); await page.waitForTimeout(400);
    check(`L13 ⌥Tab holds the list with key focus, on his turn first (${await drop.locator('.a-row.is-cur b').textContent()})`,
      (await drop.locator('.a-row.is-cur b').textContent()) === 'Plan the wing' && await page.evaluate(() => window.__state.focus === true));
    for (let i = 0; i < 4; i++) { await page.keyboard.press('ArrowDown'); await page.waitForTimeout(80); }
    await page.keyboard.press('ArrowRight'); await page.waitForTimeout(900);
    check('L13 → opens its page: your words right, its answer rendered, and a box to reply',
      (await text('.notch-drop .r-head b')) === 'Port the faces' && (await text('.notch-drop .m-you')) === 'port the faces'
      && (await text('.notch-drop .m-it strong')) === 'Faces ported.' && await page.locator('.notch-drop .c-reply input:not([disabled])').count() === 1);
    await noteShot('L13-page');
    setBoard('c-wait', { phase: 'done', last_message: 'Wired.' }); await page.waitForTimeout(2500);
    check('L13 a finish while the keys hold the island pops nothing yet', await note().count() === 0);
    await page.keyboard.type('ship it'); await page.keyboard.press('Enter'); await page.waitForTimeout(2200);
    check('L13 Enter sends the line to that session, and its page shows it working with the box greyed',
      JSON.stringify(posts.filter(x => x.path === '/inherent/claude-sessions/n-b/reply').map(x => x.body)) === '[{"text":"ship it"}]'
      && (await page.locator('.notch-drop .m-you').allTextContents()).at(-1) === 'ship it' && await page.locator('.notch-drop .m-now').count() === 1
      && await page.locator('.notch-drop .c-reply input[disabled]').count() === 1);
    await noteShot('L13-replied');
    await page.keyboard.press('Escape'); await page.waitForTimeout(300);
    await noteUp();
    check('L13 Esc lets go of the keys and the held pop comes up', await page.evaluate(() => window.__state.focus === false)
      && (await page.locator('.notch-note .u-row').allTextContents()).join('|') === 'Wire the companion');
    await page.locator('.notch-note .c-x').click(); await noteGone(); await settle();

    // Archive all on Finished takes them off the island; the daemon keeps his turn, parked and archived, so a
    // restart shows the same row.
    await move(slotX(2), 14); await drop.waitFor(); await page.waitForTimeout(500);
    await drop.locator('.a-sec[data-sec="done"] .a-h button', { hasText: 'Archive all' }).click(); await settle();
    const kept = await marks();
    check(`L13 Archive all takes the finished ones off the island (${kept})`, kept === 'turn2 work1' && agentMarks['n-build']?.archived_ms > 0);
    await page.evaluate(() => localStorage.removeItem('companion-turn-v1'));
    await page.reload(); await page.waitForTimeout(3000);
    check(`L13 after a restart his turn and what he archived come back from the daemon (${await marks()})`, (await marks()) === kept);
    // Jarvis's language wins when she starts: a daemon speaking Chinese turns her panel Chinese.
    fixtures['/inherent/language'] = { language: 'zh' };
    await page.reload(); await page.waitForTimeout(2500);
    check('L15 at start her panel follows the language Jarvis speaks in',
      await page.evaluate(() => JSON.parse(localStorage.getItem('companion-settings-v1') ?? '{}').lang) === 'zh');
    check('no page errors', errors.length === 0);
  }
  await context.close();
  writeFileSync(path.join(dir, 'verification.json'), JSON.stringify({ checks, errors, posts, pluginOps: pluginOps.filter(o => o.operation !== 'read'), resets, balances }, null, 2));
  console.log(`${checks.length} checks passed; evidence in ${dir}`);
} finally {
  await browser.close();
  server.kill();
}
