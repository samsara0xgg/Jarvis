// ADR 0093: the night run's cards on the companion, against a fake daemon. Run after `npm run build`.
// Her menu starts a run; the bedtime card lists who works and who waits, counts down, keeps the screen on while you
// answer, goes dark now or cancels; the night card is dim, tells whether the Mac is held and why, and ends the run;
// the morning card compares the deadline with the watch and goes with its ×, for good. Then the star-trail look,
// switched in her wardrobe, draws the same three on a twelve-hour dial. She sleeps in the island through the night.
// CHROMIUM_PATH runs it on a Chromium where Chrome is not installed.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = path.join(root, 'evidence', 'night-card');
mkdirSync(dir, { recursive: true });
const port = '8799', daemon = `http://127.0.0.1:${port}`, web = Number(process.env.NIGHT_CARD_PORT ?? 5196);
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log(`PASS ${name}`); };
const exe = process.env.CHROMIUM_PATH;
const browser = await chromium.launch({ headless: true, ...exe ? { executablePath: exe } : { channel: 'chrome' }, args: ['--disable-web-security'] });
const hm = ms => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
const M = 60_000, H = 60 * M;

// The sessions the fake daemon's run watches, as GET /inherent/night's `watch` carries them.
const sess = (id, title, st, busy, extra = {}) => ({ id, agent: 'claude', title, st, busy, since_ms: null, what: '', changed_ms: 0, trail: [], ...extra });
function watchAt(start, phase) {
  const A = 'Startrail 后端补全', B = '夜间挂机第二版', C = '语音延迟 A 方案', D = '桌面整理 ADR';
  const wait = sess('d', D, 'wait', false, { what: '要跑 Bash', since_ms: start - 9 * M, changed_ms: start });
  if (phase === 'bed') return { seen: true, blind: false, blind_since_ms: null, lists: { startrail: true }, busy: 3, quiet_ms: null, sessions: [
    sess('a', A, 'work', true, { since_ms: start - 35 * M, changed_ms: start, trail: [[start, null]] }),
    sess('b', B, 'done', true, { what: '1 个后台任务 · 构建', changed_ms: start, trail: [[start, null]] }),
    sess('c', C, 'pack', true, { since_ms: start - 42 * M, changed_ms: start, trail: [[start, null]] }), wait] };
  if (phase === 'night') return { seen: true, blind: false, blind_since_ms: null, lists: { startrail: true }, busy: 1, quiet_ms: null, sessions: [
    sess('a', A, 'work', true, { since_ms: start - 35 * M, changed_ms: start, trail: [[start, null]] }),
    sess('b', B, 'done', false, { changed_ms: start + 72 * M, trail: [[start, start + 72 * M]] }),
    sess('c', C, 'err', false, { changed_ms: start + 87 * M, trail: [[start, start + 87 * M]] }), wait], release_ms: null };
  // settled: A stopped past the deadline, the hold goes three minutes later.
  return { seen: true, blind: false, blind_since_ms: null, lists: { startrail: true }, busy: 0, quiet_ms: start + 151 * M, sessions: [
    sess('a', A, 'done', false, { changed_ms: start + 151 * M, trail: [[start, start + 151 * M]] }),
    sess('b', B, 'done', false, { changed_ms: start + 72 * M, trail: [[start, start + 72 * M]] }),
    sess('c', C, 'err', false, { changed_ms: start + 87 * M, trail: [[start, start + 87 * M]] }), wait], release_ms: start + 154 * M };
}

try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const context = await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));

  // The fake daemon's run: started at bedtime, the screen off 8 s later, held at least two hours, the morning at 06:00.
  const posts = [];
  const bed = new Date(); bed.setDate(bed.getDate() - 1); bed.setHours(23, 40, 0, 0);
  const six = new Date(); six.setHours(6, 0, 0, 0); if (six.getTime() <= Date.now()) six.setDate(six.getDate() + 1);
  let state = { night: null, last: null, hours: 2, laptop: true }, runs = 0, blindNext = false;
  const act = body => {
    const run = state.night;
    if (body.action === 'start' && !run) {
      const at = Date.now(), blind = blindNext;
      const watch = blind ? { seen: false, blind: true, blind_since_ms: at, lists: { startrail: false }, busy: 0, quiet_ms: null, sessions: [], release_ms: at + 2 * H } : watchAt(at, 'bed');
      state = { ...state, night: { id: `n${++runs}`, phase: 'starting', started_ms: at, until_ms: at + 2 * H, cap_ms: at + 12 * H, wake_at_ms: six.getTime(), dark_at_ms: at + 8000,
        stay: false, released_ms: null, guarded: true, watch } };
    } else if (body.action === 'stay' && run?.phase === 'starting') state = { ...state, night: { ...run, stay: true, dark_at_ms: null } };
    else if (body.action === 'dark' && run?.phase === 'starting') state = { ...state, night: { ...run, phase: 'dark', stay: false, dark_at_ms: Date.now() } };
    else if (body.action === 'end' && run) {
      // The morning card reads last night's times: bedtime yesterday at 23:40, A stopped at 02:11, let go at 02:14.
      const cancelled = run.phase === 'starting', started = cancelled ? run.started_ms : bed.getTime(), watch = watchAt(started, 'settled');
      state = { ...state, night: null, last: { id: run.id, started_ms: started, until_ms: started + 2 * H, released_ms: cancelled ? null : started + 154 * M,
        release_reason: cancelled ? null : 'settled', ended_ms: Date.now(), reason: cancelled ? 'cancelled' : 'ended', slept_ms: cancelled ? null : started + 171 * M,
        restored: { brightness: !cancelled, volume: !cancelled },
        watch: { ...watch, monitor_ms: started + 154 * M, extra_ms: 0, busy_at_deadline: 1, busy_at_release: 0 },
        totals: { nights: 7, extra_ms: 125 * M, blind: 2 } } };
    }
    return state;
  };
  await page.addInitScript(() => {
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {}, openAgents: () => { window.__agentsOpened = (window.__agentsOpened ?? 0) + 1; },
    };
    // A WebSocket that never leaves the page.
    window.WebSocket = class { constructor(url) { this.url = url; setTimeout(() => this.onopen?.(), 0); } send() {} close() { this.onclose?.(); } };
  });
  await page.route(`${daemon}/**`, async route => {
    const url = new URL(route.request().url()), method = route.request().method();
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (url.pathname === '/inherent/night') {
      if (method !== 'POST') return json(state);
      const body = JSON.parse(route.request().postData() || '{}');
      posts.push(body);
      return json(act(body));
    }
    if (url.pathname === '/inherent/language') return json({ language: 'zh' });
    if (url.pathname === '/inherent/controls') return json({ mic_muted: false, speech_muted: false, conversation: false });
    if (url.pathname === '/inherent/confirmation' || url.pathname === '/inherent/clarification') return json({ card: null });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });

  const open = async () => {
    await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${port}`);
    await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
    await page.evaluate(() => window.__cursor({ x: 600, y: 860 }));
    await page.waitForTimeout(1800);
  };
  await open();
  const card = page.locator('.notch-note .nc-night');
  const face = () => page.evaluate(() => document.querySelector('.companion-canvas')?.dataset.face);
  const shot = async name => { const box = await page.locator('.notch-note').boundingBox(); await page.screenshot({ path: path.join(dir, `${name}.png`), clip: { x: 100, y: 0, width: 440, height: Math.ceil((box?.y ?? 0) + (box?.height ?? 300)) + 12 } }); };
  const shown = async () => { await card.waitFor({ state: 'visible', timeout: 5000 }); await page.waitForFunction(() => document.querySelector('.notch-note')?.classList.contains('is-open')); await page.waitForTimeout(800); };
  const says = text => page.waitForFunction(want => document.querySelector('.notch-note .nc-night')?.textContent.includes(want), text, { timeout: 4000 });
  const startFromMenu = async () => {
    await page.locator('.companion-hit').click({ button: 'right', force: true });
    await page.getByRole('menuitem', { name: '睡了，至少挂 2 小时' }).click();
    await shown();
  };
  check('no run, no card', await card.count() === 0);

  // ---- the list look ----
  await startFromMenu();
  check('her menu starts the default run', posts.length === 1 && posts[0].action === 'start' && posts[0].hours === undefined);
  const until = hm(state.night.until_ms), cap = hm(state.night.cap_ms);
  const bedtime = await card.textContent();
  check(`the bedtime card: the floor, the watch, the cap, who works and who waits (${bedtime})`,
    bedtime.includes(`至少挂到 ${until}（2 小时）`) && bedtime.includes('在干活的都停下 3 分钟，就放开防睡') && bedtime.includes(`${cap}（12 小时）`)
    && /\d+ 秒后熄屏/.test(bedtime) && bedtime.includes('盯着3') && bedtime.includes('在干活 35 分') && bedtime.includes('后台任务在跑') && bedtime.includes('在压缩')
    && bedtime.includes('轮到你1') && bedtime.includes('等你 · 要跑 Bash') && bedtime.includes('终端里开的 Claude Code 会话看不到') && bedtime.includes('合上盖子'));
  check('she watches the bedtime card from the island', await face() === 'ask');
  await shot('01-list-bedtime');
  await card.getByRole('button', { name: '去回答' }).click();
  await says('你回完、一分钟不动就熄屏');
  check('Answer keeps the screen on and opens the Agents window', posts.at(-1).action === 'stay' && await page.evaluate(() => window.__agentsOpened) === 1);
  await shot('02-list-bedtime-answering');

  await card.getByRole('button', { name: '现在熄屏' }).click();
  await page.waitForFunction(() => document.querySelector('.notch-note .nc-night')?.classList.contains('is-dim'));
  state = { ...state, night: { ...state.night, watch: watchAt(state.night.started_ms, 'night') } };
  await says('出错了');
  const night = await card.textContent();
  check(`the night card: still held, what works, what needs you, what is done (${night})`, posts.at(-1).action === 'dark' && night.includes('还在挂着') && night.includes(`至少到 ${until}`)
    && night.includes('在干活1') && night.includes('轮到你2') && night.includes('做完了1') && night.includes('它停下 3 分钟后放开防睡') && night.includes('回了它就接着干'));
  check('she sleeps in the island', await face() === '00');
  await shot('03-list-night');
  const s = state.night.started_ms;
  state = { ...state, night: { ...state.night, watch: watchAt(s, 'settled') } };
  await says('都停了');
  const settling = await card.textContent();
  check(`past the deadline, all stopped: when it lets go (${settling})`, settling.includes(`${hm(s + 154 * M)} 放开防睡`) && settling.includes(`最后一个 ${hm(s + 151 * M)} 停下`));
  await shot('04-list-settling');
  state = { ...state, night: { ...state.night, released_ms: s + 154 * M } };
  await says('已放开防睡');
  check('after it lets go the card says the Mac may sleep', (await card.textContent()).includes('Mac 可以照常睡'));
  await shot('05-list-released');

  await card.getByRole('button', { name: '我起来了' }).click();
  await page.waitForFunction(() => document.querySelector('.notch-note .nc-night')?.classList.contains('is-morning'));
  await page.waitForTimeout(800);
  const morning = await card.textContent();
  check(`the morning card: both clocks, which held, when the Mac slept, the running totals (${morning})`, posts.at(-1).action === 'end' && morning.includes('昨晚')
    && morning.includes('23:40 – 02:14') && morning.includes('那时还有 1 个在干活') && morning.includes('02:14 · 最后一个 02:11 停下按这个')
    && morning.includes('02:31 睡着了') && morning.includes('亮度和声音') && morning.includes('近 7 晚 · 兜底共多挂 2 小时 5 分 · 有 2 晚看不到会话'));
  check('she is pleased to see you', await face() === 'fin');
  await shot('06-list-morning');
  await card.getByRole('button', { name: '知道了' }).click();
  await page.waitForTimeout(900);
  const folded = () => page.evaluate(() => !document.querySelector('.notch-note')?.classList.contains('is-open'));
  check('its × puts the morning card away', await folded() && await page.evaluate(() => localStorage.getItem('companion-night-seen-v1')) === 'n1');
  await open();
  check('and it stays away after a reload', await card.count() === 0);

  blindNext = true;
  await startFromMenu();
  const blind = await card.textContent();
  check(`no session list: the bedtime card says it is the deadline alone (${blind})`, blind.includes(`挂到 ${until.slice(0, 2)}`) && blind.includes('看不到会话列表，这次只按时间') && !blind.includes('最长'));
  await shot('07-list-bedtime-blind');
  await card.getByRole('button', { name: '不挂了' }).click();
  await page.waitForTimeout(900);
  check('cancelled before dark: nothing to tell in the morning', posts.at(-1).action === 'end' && state.last.reason === 'cancelled' && await folded());
  blindNext = false;

  // ---- the star-trail look, from her wardrobe ----
  await page.evaluate(() => { const w = JSON.parse(localStorage.getItem('companion-wardrobe-v1') ?? '{}'); localStorage.setItem('companion-wardrobe-v1', JSON.stringify({ ...w, night: 'trail' })); });
  await open();
  await startFromMenu();
  const dial = page.locator('.notch-note .nc-dial');
  check('the star-trail bedtime card draws the dial with a ring per session', await dial.count() === 1 && await dial.locator('.nc-trail').count() >= 3
    && await dial.locator('.nc-hold.is-plan').count() === 1 && (await card.textContent()).includes('转满一圈'));
  await shot('08-trail-bedtime');
  await card.getByRole('button', { name: '现在熄屏' }).click();
  await page.waitForFunction(() => document.querySelector('.notch-note .nc-night')?.classList.contains('is-dim'));
  state = { ...state, night: { ...state.night, watch: watchAt(state.night.started_ms, 'night') } };
  await says('出错了');
  check('the star-trail night card: held so far, planned, one at work', await dial.locator('.nc-hold.is-both').count() === 1 && (await dial.locator('.nc-big').textContent()) === '1');
  await shot('09-trail-night');
  await card.getByRole('button', { name: '我起来了' }).click();
  await page.waitForFunction(() => document.querySelector('.notch-note .nc-night')?.classList.contains('is-morning'));
  await page.waitForTimeout(800);
  const trail = await card.textContent();
  check(`the star-trail morning card: the extension past the deadline in blue, the hours held (${trail})`, trail.includes('昨晚的星轨')
    && await dial.locator('.nc-hold.is-mon').count() === 1 && (await dial.locator('.nc-big').textContent()) === '2:34' && trail.includes('02:14'));
  await shot('10-trail-morning');
  check('the wardrobe setting names both looks', await page.evaluate(() => JSON.parse(localStorage.getItem('companion-wardrobe-v1')).night) === 'trail');

  check('no page errors', errors.length === 0);
  writeFileSync(path.join(dir, 'checks.json'), JSON.stringify({ checks, errors }, null, 2));
  console.log(`${checks.length} night card checks passed`);
} finally {
  await browser.close();
  server.kill();
}
