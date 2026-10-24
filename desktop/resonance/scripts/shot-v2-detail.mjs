// WP3 (the selected row's detail in the V2 right column) in pictures and checks: a stretch of what he was doing (its lines, how she knows), a reminder that rang (知道了),
// a commitment mail noted (是我的 / 不是), a place that can be renamed, a long list that scrolls with edge fades, and the same in English. PNGs land in $OUT
// (default verify-out/v2-detail/, not committed). Same harness as shot-v2.mjs: headless Chrome against the built page with the bridge stubbed and a fake daemon.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = process.env.OUT ?? path.join(root, 'verify-out/v2-detail');
mkdirSync(out, { recursive: true });
const web = Number(process.env.COMPANION_PORT ?? 5189), daemonPort = Number(process.env.COMPANION_DAEMON_PORT ?? 8789), daemon = `http://127.0.0.1:${daemonPort}`;
const STAGE = 836;
const errors = [], check = (name, pass, detail = '') => { assert.ok(pass, `${name} ${detail}`); console.log('ok', name); };
const LANG = process.env.LANG_UI === 'en' ? 'en' : 'zh', en = LANG === 'en';

// ---------- the day: 2026-10-19 in Vancouver (PDT, UTC-7), now 21:50 ----------
const DATE = '2026-10-19', TZ = 'America/Vancouver';
const T = (h, m = 0, day = 19) => Date.UTC(2026, 9, day, h + 7, m);
const NOW = T(21, 50), iso = (h, m) => `2026-10-19T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00-07:00`;
const it = (id, kind, start, end, extra = {}, lane = 'you') => ({ id, lane, kind, start_ms: start, end_ms: end, state: end === null ? (start > NOW ? 'next' : 'now') : end <= NOW ? 'done' : start > NOW ? 'next' : 'now', ...extra });
const sp = (id, start, end, extra) => ({ id, lane: 'doing', kind: 'doing', start_ms: start, end_ms: end, title: null, detail: null, category: 'unknown', basis: 'inferred', confidence: 'high', clues: [], place_id: null, item_ids: [], ...extra });
const home = { id: 'p-home', name: '家', role: 'home' };
const HEX = 'c1'.repeat(16);
const TITLES = Array.from({ length: 40 }, (_, i) => `第 ${i + 1} 个聊天的标题，写得比较长一点点`).join('; ');
// A 25-minute stretch at the desk's edge: three worktree sessions (one he prompted, titled 部署, and two that ran by themselves), each in a repo called Jarvis that no line may say, a Remote Control session he prompted, six
// 小红书 bursts that only came in (each spans 2 minutes, first message to last, which is no time spent) and one 微信 burst he answered.
const code = (id, from, to, extra) => it(id, 'code', T(14, from), T(14, to), { project: 'Jarvis', account: 'A', session_id: id, ...extra });
const burst = (id, who, extra) => it(id, 'messages', T(14, 2), T(14, 4), { device: 'iPhone', app: '小红书', conversation: who, sent: 0, received: 2, ...extra });
const BREAK = [
  code('code-b1', 4, 25, { title: '部署', prompts: 3 }), code('code-b2', 4, 25, { title: '', prompts: 0 }), code('code-b3', 4, 25, { title: '', prompts: 0 }),
  code('code-rc', 4, 6, { title: 'Remote Control session', prompts: 2 }),
  ...['贝小贝贝', 'yooi三要鱼', 'zoey是美少女吖', '不知道起什么网名的小女孩', 'meiyy~', '分享到小红书'].map((who, i) => burst(`msg-xhs-${i}`, who)),
  burst('msg-reply', 'Mia', { app: '微信', sent: 2, received: 3 }),
];
const day = (date = DATE) => ({
  date, tz: TZ, start_ms: T(0), end_ms: T(24), now_ms: NOW,
  items: [
    it('sleep-1', 'sleep', T(0, 55), T(9, 5), { minutes: 470 }),
    it('stay-home-3', 'stay', T(18, 50), null, { place: '12 Example St, Victoria', kept_place: home }),
    it('work-2', 'work', T(19, 35), T(21, 40), { active_s: 6900, projects: [{ name: 'Jarvis', seconds: 6000 }], apps: [{ name: 'Ghostty', seconds: 4200 }, { name: 'Claude', seconds: 1800 }] }),
    it('pay-1', 'payment', T(20, 5), T(20, 5), { source: 'alipay', amount: '50.00', currency: 'CAD', direction: 'in', counterparty: 'Mia', text: '转来饭钱' }),
    it('picture-1', 'picture', T(20, 40), T(20, 40), { attachment: 'a'.repeat(32), note: '桌上的咖啡\n和写了一半的笔记' }),
    it('claude-day-1', 'claude_day', T(9, 40), T(22, 5), { source: 'claude_daily', text: TITLES, chats: 40 }),
    it('reminder-2', 'reminder', T(9, 30), null, { text: '给银行打电话', fired: true, acknowledged: false }, 'her'),
    it('reminder-1', 'reminder', T(22, 0), null, { text: '把垃圾拿出去', fired: false, acknowledged: false }, 'her'),
    ...date === DATE ? BREAK : [],
    it(`commitment-${HEX}@${DATE}`, 'event', T(22, 30), T(23, 30), { title: '小组讨论', place: 'Library', private: false, source: 'mail', confirmed: false }),
  ],
  spans: date !== DATE ? [] : [
    sp('doing-3', T(14, 0), T(14, 25), { title: '离开电脑休息', category: 'rest', basis: 'inferred', confidence: 'low', item_ids: BREAK.map(x => x.id) }),
    sp('doing-1', T(18, 50), T(19, 35), { title: null }),
    sp('doing-2', T(19, 35), null, { title: 'Jarvis', category: 'work', basis: 'observed', detail: '对照桌面端的行文案，中间去写了论文第三章的数据，现在还在做。', place_id: 'stay-home-3',
      clues: [{ kind: 'app', at_ms: null, text: 'Xcode 和终端在前台' }, { kind: 'app', at_ms: null, text: 'Claude Code 会话 3 个' }], item_ids: ['stay-home-3', 'work-2', 'pay-1', 'picture-1'] }),
  ],
  labels: [], missing: [], line: { text: '今天一半在路上，一半在 UVic。晚上回家后写了会儿代码。', draft: false, written_at: '2026-10-19T21:30:00-07:00' }, highlights: [],
});
const mail = (id, from, subject, received, extra = {}) => ({ id, from, address: `${id}@example.com`, thread_id: `th-${id}`, subject, received, reply: null, junk: false, importance: 1, category: null, ...extra });
const LETTERS = [
  mail('m-nw', 'Northwind Recruiting', 'Interview times — please confirm', iso(9, 12), { reply: 'yes' }),
  mail('m-reg', 'UVic Registrar', 'Your fall schedule', iso(11, 2), { reply: 'fyi' }),
  mail('m-mom', '妈妈', '周末回家吃饭吗？', iso(15, 5), { reply: 'yes' }),
  mail('m-gh', 'GitHub', '[jarvis] Run failed: verify', iso(16, 30), { reply: 'fyi' }),
];

// ---------- the built page ----------
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const exe = [process.env.CHROMIUM, '/opt/pw-browsers/chromium'].find(p => p && existsSync(p));
const browser = await chromium.launch({ headless: true, ...exe ? { executablePath: exe } : { channel: 'chrome' }, args: ['--disable-web-security', '--mute-audio'] });
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const ctx = await browser.newContext({ viewport: { width: STAGE, height: 760 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  page.on('pageerror', e => { errors.push(e.message); console.log('pageerror', e.message); });
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
  await page.addInitScript(([width, LANG]) => {
    localStorage.setItem('companion-settings-v1', JSON.stringify({ lang: LANG }));
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: width, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {}, wearing: () => {}, onDictation: () => () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, onGhostty: () => () => {}, onAgentsPresence: () => () => {}, onMouseDown: () => () => {}, onClaudeFront: () => () => {}, plugins: async () => ({}),
      openMail: async () => true,
    };
    window.WebSocket = class { constructor() { setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
  }, [STAGE, LANG]);
  await page.route(`${daemon}/**`, async route => {
    const req = route.request(), url = new URL(req.url()), p = url.pathname, post = req.method() === 'POST';
    const json = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    let m;
    if (p === '/inherent/day') { const d = url.searchParams.get('date'); return json(day(d || DATE)); }
    if (p === '/inherent/mail' && !post) return json({ unread: LETTERS });
    if (/^\/inherent\/mail\/[^/]+\/(draft|summary)$/.test(p)) return json({ detail: 'none' }, 404);
    if (!post && (m = p.match(/^\/inherent\/mail\/([^/]+)$/))) {
      const l = LETTERS.find(x => x.id === decodeURIComponent(m[1]));
      return l ? json({ ...l, to: 'me@example.com', text: `The letter itself: ${l.subject}.`, layout: 'text' }) : json({ detail: 'no such letter' }, 404);
    }
    if (p === '/inherent/controls') return json({ mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' });
    if (p === '/inherent/claude-sessions') return json({ sessions: [], error: null });
    if (p === '/inherent/agent-marks') return json({ marks: {} });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  await page.clock.setFixedTime(new Date(NOW));
  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemonPort}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  const rect = sel => page.evaluate(sel => { const e = document.querySelector(sel); return e ? e.getBoundingClientRect().toJSON() : null; }, sel);
  const press = async (x, y) => { await move(x, y); await page.mouse.down(); await page.mouse.up(); };
  const pressOn = async sel => { const r = await rect(sel); assert.ok(r, `${sel} is on the page`); await press(r.x + r.width / 2, r.y + r.height / 2); };
  const settle = ms => page.waitForTimeout(ms);
  // the pointer rests inside the pane, on the right column's empty foot (outside it the pane would fold)
  const park = () => move(STAGE - 70, 520);
  const sel = () => page.evaluate(() => document.querySelector('.notch .dt-list [aria-current="true"]')?.dataset.key ?? null);
  const shot = async name => { const g = await rect('.notch-drop'); await page.screenshot({ path: path.join(out, `${name}.png`), clip: { x: 0, y: 0, width: STAGE, height: Math.ceil(g.bottom) + 12 } }); };

  // open the day: a click on the wing's line pins it
  await page.waitForFunction(() => document.querySelector('.notch')?.dataset.line, undefined, { timeout: 8000 });
  const [a0, a1] = (await page.evaluate(() => document.querySelector('.notch').dataset.lineAt)).split('-').map(Number);
  await press((a0 + a1) / 2, 14);
  await page.waitForSelector('.notch-drop.is-open .dt-row', { timeout: 8000 });
  await settle(1200);
  await park();

  const cls = sel => page.evaluate(sel => { const e = document.querySelector(sel); return e ? e.className : null; }, sel);
  const count = sel => page.locator(sel).count();
  const pick = async key => { await pressOn(`.notch .dt-list [data-key="${key}"]`); await settle(450); await park(); await settle(200); };
  const side = '.notch .dt-side .dt-pin.is-day';

  // ---- a stretch: head, lines, how she knows ----
  await pick('doing-2');
  check('the stretch is selected: ring on its row, its detail in the right column', await sel() === 'doing-2' && await count(side) === 1);
  check('the detail has the head (glyph, title, when), 问她 in the head, a small way back, and no ↑ ↓ / page-back chrome', await page.evaluate(sel => {
    const d = document.querySelector(sel);
    return !!d.querySelector('.dt-dh .dt-dic') && !!d.querySelector('.dt-dt b')?.textContent && !!d.querySelector('.dt-dt em')?.textContent && d.querySelectorAll('.dt-dh > .dt-btn').length === 1 && !!d.querySelector('.dt-up') && !d.querySelector('.dt-pn, .dt-pb, .dt-ph');
  }, side));
  await shot('detail-stretch');
  check('its lines are dense: a time, a glyph and words each', await page.evaluate(sel => { const l = [...document.querySelectorAll(`${sel} .dt-il`)]; return l.length >= 2 && l.every(x => x.querySelector('time')?.textContent && x.getBoundingClientRect().height < 48); }, side));
  check('the stretch\'s parts are a strip under its words, before its lines', await page.evaluate(sel => { const b = document.querySelector(sel + ' .dt-body'), k = [...b.children].map(c => c.className); return b.querySelectorAll('.dt-sp-b i').length >= 2 && k.indexOf('dt-sp') > k.indexOf('dt-lines') && k.indexOf('dt-sp') < k.indexOf('dt-ins'); }, side));
  check('how she knows is the last line, and is there only because the data is', await page.evaluate(sel => { const d = document.querySelector(sel + ' .dt-body'); return d.lastElementChild?.classList.contains('dt-how') && d.lastElementChild.textContent.includes('Xcode'); }, side));
  check('the list row has no inline buttons (the star and 问她 are in the right column now)', await count('.notch .dt-list .dt-quick') === 0 && await count('.notch .dt-list .dt-ask') === 0);
  const ring = await page.evaluate(() => { const r = document.querySelector('.notch .dt-list [aria-current="true"]'); const c = getComputedStyle(r); return { shadow: c.boxShadow, bg: c.backgroundColor, h: r.getBoundingClientRect().height }; });
  check('the selected row is framed: a ring and a lighter ground, and the row did not grow', ring.shadow !== 'none' && /inset/.test(ring.shadow) && ring.h < 60, JSON.stringify(ring));
  await press(STAGE - 70, 520);
  await pressOn(`${side} .dt-dh .dt-btn`); await settle(500);
  check('问她 in the head opens the input at the pane foot with the row as its chip', await count('.notch .dt-dock .dt-about') === 1);
  await shot('detail-ask');
  await page.keyboard.press('Escape'); await settle(400);

  // ---- sessions and message bursts: icons and names, counts and no minutes ----
  await pick('doing-3');
  const il = await page.evaluate(sel => [...document.querySelectorAll(`${sel} .dt-il`)].map(l => ({ at: l.querySelector('time').textContent, title: l.querySelector('.dt-il-t').childNodes[0].textContent, detail: l.querySelector('.dt-il-t em')?.textContent ?? '', side: l.querySelector('.dt-il-s')?.textContent ?? '', icon: !!l.querySelector('.dt-il-i svg') })), side);
  const lineOf = title => il.find(x => x.title === title);
  const W = en ? { bg: '2 more background sessions', xhs: '小红书', xhsDetail: '6 people · 12 received', reply: 'Sent 2 · Received 3' } : { bg: '另有 2 个后台会话', xhs: '小红书', xhsDetail: '6 人 · 收到 12 条', reply: '发 2 · 收 3' };
  check('a stretch with three worktree sessions and a Remote Control one has five lines for the lot: his two sessions by their titles, the background ones together, the received bursts and the reply, each with its time', il.length === 5 && il.every(x => x.at), JSON.stringify(il));
  check('his sessions are lines named by their title with an icon, and their length; no "Claude Code ·", no repo (Jarvis), no worktree folder', lineOf('部署')?.icon && lineOf('部署').side === (en ? '21 min' : '21 分钟') && lineOf('Remote Control session')?.icon && lineOf('Remote Control session').side === (en ? '2 min' : '2 分钟') && il.every(x => !/Claude Code|bridge-cse|Jarvis/.test(x.title + x.detail)), JSON.stringify(il));
  check('the two sessions that ran by themselves are one line of their own', lineOf(W.bg)?.side === (en ? '21 min' : '21 分钟') && lineOf(W.bg).icon, JSON.stringify(lineOf(W.bg)));
  check('the six bursts only received are one line: the app, how many people, how many messages; no minutes', lineOf(W.xhs)?.detail === W.xhsDetail && lineOf(W.xhs).side === '' && lineOf(W.xhs).icon, JSON.stringify(lineOf(W.xhs)));
  check('a burst he replied to stays its own line with sent and received, and no minutes', lineOf('Mia')?.detail === W.reply && lineOf('Mia').side === '', JSON.stringify(lineOf('Mia')));
  check('no line of the stretch says a span of minutes for messages', il.filter(x => x.icon && /^(Mia|小红书)$/.test(x.title)).every(x => !/min|分钟/.test(x.detail + x.side)));
  check('the row badge still counts the seven bursts', await page.evaluate(() => /7/.test(document.querySelector('.notch .dt-list [data-key="doing-3"]').textContent)));
  await shot('detail-sessions-messages');

  // ---- a stretch nothing says the words of ----
  await pick('doing-1');
  check('a hole in the day: a dashed ring in its head', (await cls(`${side} .dt-dic`))?.includes('is-unknown'));
  await shot('detail-unknown');

  // ---- a reminder that rang: 知道了 ----
  await pick('reminder-2');
  const ack = await page.evaluate(sel => [...document.querySelectorAll(`${sel} .dt-acts .dt-btn`)].map(b => b.textContent), side);
  check('a reminder that rang has the amber 知道了 among the buttons', ack.length === 1 && ack[0] === (en ? 'Got it' : '知道了'), JSON.stringify(ack));
  await shot('detail-reminder');

  // ---- a commitment mail noted ----
  await pick(`commitment-${HEX}@${DATE}`);
  const cm = await page.evaluate(sel => [...document.querySelectorAll(`${sel} .dt-acts .dt-btn`)].map(b => b.textContent), side);
  check('a commitment has 是我的 and 不是', cm.length === 2 && cm[0] === (en ? 'Mine' : '是我的') && cm[1] === (en ? 'Not mine' : '不是'), JSON.stringify(cm));
  await shot('detail-commit');

  // ---- a kept place: rename ----
  await pressOn('.notch .dt-nav button:first-child'); await settle(900);
  check('the day before (served with places, no stretches) has the stay in the list', await count('.notch .dt-list .dt-sh[data-key="stay-home-3"]') === 1);
  await pick('stay-home-3');
  const rn = await page.evaluate(sel => [...document.querySelectorAll(`${sel} .dt-acts .dt-btn`)].map(b => b.textContent), side);
  check('a kept place has 改名字', rn.includes(en ? 'Rename' : '改名字'), JSON.stringify(rn));
  await pressOn(`${side} .dt-acts .dt-btn`); await settle(300);
  check('renaming puts the field in the head, with the cursor in it', await page.evaluate(sel => document.activeElement === document.querySelector(sel + ' .dt-dt input.dt-rename'), side));
  await shot('detail-rename');
  await page.keyboard.press('Escape'); await settle(300);

  // ---- a long one: it scrolls inside the column, its edges fading ----
  await pick('claude-day-1');
  const long = await page.evaluate(sel => { const d = document.querySelector(sel), s = document.querySelector('.notch .dt-side'); return { sh: d.scrollHeight, ch: d.clientHeight, below: d.classList.contains('more-below'), above: d.classList.contains('more-above'), side: s.scrollHeight - s.clientHeight, bottom: d.getBoundingClientRect().bottom, drop: document.querySelector('.notch .dt').getBoundingClientRect().bottom }; }, side);
  check('a long detail scrolls inside the column (not the pane or the side), and fades at its foot', long.sh > long.ch + 50 && long.below && !long.above && long.side <= 1 && long.bottom < long.drop, JSON.stringify(long));
  await shot('detail-long-top');
  await page.evaluate(sel => { const d = document.querySelector(sel); d.scrollTop = 150; }, side); await settle(150);
  check('scrolled, both edges fade', await page.evaluate(sel => { const d = document.querySelector(sel); return d.classList.contains('more-above') && d.classList.contains('more-below'); }, side));
  await page.evaluate(sel => { const d = document.querySelector(sel); d.scrollTop = d.scrollHeight; }, side); await settle(150);
  check('at the end only the top edge fades', await page.evaluate(sel => { const d = document.querySelector(sel); return d.classList.contains('more-above') && !d.classList.contains('more-below'); }, side));
  await shot('detail-long-end');

  await pressOn('.notch .dt-nav button:last-child'); await settle(900);

  // ---- the way back, and the keys ----
  await pick('doing-2');
  await pressOn(`${side} .dt-up`); await settle(400);
  check('the small way back clears the selection to the home slot', await sel() === null && await count('.notch .dt-side .ds') === 1);
  await page.keyboard.press('ArrowDown'); await settle(300);
  await page.keyboard.press('a'); await settle(500);
  check('A asks her about the selected row', await count('.notch .dt-dock .dt-about') === 1);
  await page.keyboard.press('Escape'); await settle(300);
  check('no errors on the page', errors.length === 0, errors.join(' | '));
  await ctx.close();
} finally {
  await browser.close();
  server.kill();
}
