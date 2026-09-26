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
const web = 5192;
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
  const context = await browser.newContext({ viewport: { width: 640, height: 592 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));

  // The fake daemon: what the companion posts, and what it is told.
  const posts = [], pluginOps = [];
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
    '/inherent/usage': { services: { claude: { status: 'ok', observed_at_ms: now, data: { plan: '20X', windows: [{ key: 'five_hour', label: '5 hours', percent: 41, resets_at: iso(now + 3_600_000) }] } }, codex: { status: 'unconfigured', data: {} } } },
    '/inherent/work-state': { state: { version: 1, analyzed_at: iso(now - 60_000), observed_until: iso(now - 60_000), evidence: { coverage: {}, counts: {}, limits: [] }, now: { text: 'Wiring the companion to the daemon.', basis: 'observed', refs: [] }, activities: [], links: [], uncertainties: [], note: null },
      data: null, freshness: { checked_at_ms: now, latest_observed_at: iso(now - 60_000), analyzed_at: iso(now - 60_000), analysis_observed_until: iso(now - 60_000) }, refreshing: false, outcome: 'analyzed', error: null },
    '/inherent/projects': { days: Array.from({ length: 7 }, (_, i) => iso(now - (6 - i) * 86_400_000).slice(0, 10)), projects: [{ id: 'jarvis', name: 'jarvis', seconds: 7200, today_seconds: 3600, days: [0, 0, 0, 0, 0, 3600, 3600], last_seen: null, commits: { count: 2, items: [] }, recent: [] }],
      other: { seconds: 0, days: [0, 0, 0, 0, 0, 0, 0], count: 0 }, unsorted: { seconds: 0, days: [0, 0, 0, 0, 0, 0, 0], count: 0 }, coverage: { timesink: 'ok', git: 'ok' }, latest_observed_at: null, sorted_at: null, refreshing: false, outcome: null, error: null },
  };
  fixtures['/inherent/projects/refresh'] = fixtures['/inherent/projects'];
  const LOGO = `data:image/svg+xml;base64,${Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><circle cx="4" cy="4" r="4" fill="#5e6ad2"/></svg>').toString('base64')}`;
  const pluginToken = real ? (() => { try { return JSON.parse(readFileSync(path.join(homedir(), '.jarvis/plugin-access.json'), 'utf8')).token; } catch { return null; } })() : null;
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
    window.__state = { opened: [] };
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionMenu: () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; },
      onCommand: callback => { window.__command = callback; return () => {}; },
      passthrough: () => {}, focus: async () => {}, material: () => {},
      codexTitles: async () => ({}), openCodex: async id => { window.__state.opened.push(id); return true; },
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
    if (url.pathname === '/inherent/submit') return json({ turn_id: 'typed-1' });
    if (url.pathname === '/inherent/cancel-response') return json({});
    if (url.pathname === '/inherent/conversation') {
      const after = Number(url.searchParams.get('after') ?? 0), limit = Number(url.searchParams.get('limit') ?? 2), all = [...yesterday, ...rows];
      reads.push({ at: Date.now(), after, limit });
      return json({ since: after, rows: after ? all.filter(r => r.seq > after) : all.slice(-limit) });
    }
    if (fixtures[url.pathname]) return json(fixtures[url.pathname]);
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });

  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${port}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  const hit = page.locator('.companion-hit');
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  const waitPlace = async value => { await page.waitForFunction(v => document.querySelector('.companion-hit')?.dataset.place === v, value, { timeout: 5000 }); await page.waitForTimeout(700); };
  const face = (...ids) => page.waitForFunction(v => v.includes(document.querySelector('.companion-canvas')?.dataset.face), ids, { timeout: 2500 }).then(() => ids[0], () => null);
  const shot = (name, clip = { x: 120, y: 0, width: 400, height: 240 }) => page.screenshot({ path: path.join(dir, `${name}.png`), clip });
  const panelShot = name => shot(name, { x: 150, y: 0, width: 340, height: 592 });
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
    const get = async p => (await fetch(`${daemon}${p}`)).json();
    await page.evaluate(() => window.__command('dashboard'));
    await page.locator('.companion-dashboard.is-open').waitFor();
    await page.waitForTimeout(2500);
    const conversation = await get('/inherent/conversation?after=0');
    const answer = [...conversation.rows].reverse().find(r => r.source !== 'allen');
    const say = await text('.ad .say');
    check(`R her words are the last answer on record (${answer?.seq})`, !!answer && say === plain(answer.text) && (await text('.ad .cap')).includes(hm(Date.parse(answer.ts))));
    const work = await get('/inherent/work-state');
    const nowText = await text('.ad .r-now .text');
    check(`R Now is the daemon's work state (${work.state?.now ? 'claim' : 'none'})`, work.state?.now ? nowText === work.state.now.text : /No recent activity|Syncing/.test(nowText));
    await panelShot('R-home');
    const usage = await get('/inherent/usage'), first = usage.services.claude?.status === 'ok' ? usage.services.claude.data.windows?.[0] : null;
    check(`R Usage rings are the daemon's (${first ? `${first.key} ${Math.round(first.percent)}%` : 'no Claude'})`, first ? (await text('.ad .r-usage .dial b')).startsWith(String(Math.round(first.percent))) : true);
    const projects = await get('/inherent/projects'), top = projects.projects?.find(p => p.seconds > 0);
    check(`R the Projects tile is the daemon's (${top?.name ?? 'none'})`, top ? (await text('.ad .pj-mini b')) === top.name : true);
    const codex = (await get('/inherent/codex-sessions')).sessions ?? [];
    const claudeRoute = await fetch(`${daemon}/inherent/claude-sessions`);
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
      document.querySelector('.ad [data-row="conversation"] button').click();
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
    await page.evaluate(() => { window.__emit('done', { turn_id: 'v1', fadeMs: 200 }); });
    await page.waitForFunction(() => !document.querySelector('.companion-bubble.is-open'), null, { timeout: 3000 });
    check('L4 after the answer settles she is back to listening', await page.locator('.companion-strip.is-open').count() === 1);
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

    // The Dashboard on live data.
    await page.evaluate(() => window.__command('dashboard'));
    await page.locator('.companion-dashboard.is-open').waitFor();
    await page.waitForTimeout(1500);
    check('L8 her words are the last answer on record, plain, with its time', await text('.ad .say') === 'Two things: the voice test at four, and the demo cut.' && (await text('.ad .cap')).includes(hm(Date.parse(rows[1].ts))));
    check('L8 Now, Usage and Projects come from the daemon', await text('.ad .r-now .text') === 'Wiring the companion to the daemon.' && (await text('.ad .r-usage .dial b')).startsWith('41') && await text('.ad .pj-mini b') === 'jarvis');
    check('L8 the Agents row counts live sessions', (await text('.ad .r-agents .pill')) === '1 needs you' && (await text('.ad .r-agents .text')).includes('Wire the companion'));
    check('L8 the Plugins tile is the live catalog, connected first, with a manifest logo where there is one', (await page.locator('.ad .pl-mini i').allTextContents()).join('') === 'N' && await page.locator('.ad .pl-mini i:first-child img[src^="data:image/svg"]').count() === 1 && (await text('.ad [data-row="plugins"] .meta')) === '1 on');
    await panelShot('L8-home');
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
    await page.evaluate(tokens => { window.__emit('open', { turn_id: 'typed-1', response_id: 'resp-3' }); for (const token of tokens) window.__emit('append', { turn_id: 'typed-1', token }); },
      ['<voice>', 'Moved to five.', '</voice>', '<document>', '## Moved', '- **5 PM** voice test', '- bring `reSpeaker`', '| When | What |', '|---|---|', '| 17:00 | voice test |', '</document>']);
    await page.waitForTimeout(500);
    const md = page.locator('.ad .tr-jarvis .md').last(), asked = reads.find(r => r.at >= emitted), seen = await page.evaluate(() => window.__seen);
    check(`L8 the answer comes from its row, asked for as it starts (${asked ? asked.at - emitted : '-'} ms), never the run-together stream`, !!asked && asked.at - emitted < 150
      && words(await md.textContent()) === words(doc) && await page.locator('.ad .tr').count() === 2 && !seen.some(t => /Moved to five|##|voice test- bring/.test(t)));
    check('L8 the answer\'s markdown renders: heading, bold, nested list, code, table', await md.locator('h5').textContent() === 'Moved' && await md.locator('li strong').textContent() === '5 PM'
      && await md.locator('li > ul > li code').textContent() === 'reSpeaker' && (await md.locator('th').allTextContents()).join() === 'When,What'
      && (await md.locator('td').allTextContents()).join() === '17:00,voice test' && !/\*\*|##|\|/.test(await md.textContent()));
    await panelShot('L8-conversation');
    await page.evaluate(() => window.__emit('done', { turn_id: 'typed-1', fadeMs: 100 }));
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
    await hit.dblclick({ force: true });
    await page.waitForFunction(() => !document.querySelector('.companion-dashboard.is-open'), null, { timeout: 3000 });
    // Jarvis asks for a plugin mid-conversation: the closed panel opens and lands on it.
    snapshot = { ...snapshot, request: request('notion', 'Find last week’s meeting notes.') };
    await page.waitForFunction(() => document.querySelector('.ad .ask-card')?.textContent.includes('last week'), null, { timeout: 4000 });
    check('L11 when Jarvis asks for a plugin the Dashboard opens on it with the reason', await page.locator('.companion-dashboard.is-open').count() === 1 && (await text('.ad .pl-det .btn-glow')) === 'Sign in and continue');
    await page.waitForTimeout(900); await panelShot('L11-asked');
    check('no page errors', errors.length === 0);
  }
  await context.close();
  writeFileSync(path.join(dir, 'verification.json'), JSON.stringify({ checks, errors, posts, pluginOps: pluginOps.filter(o => o.operation !== 'read') }, null, 2));
  console.log(`${checks.length} checks passed; evidence in ${dir}`);
} finally {
  await browser.close();
  server.kill();
}
