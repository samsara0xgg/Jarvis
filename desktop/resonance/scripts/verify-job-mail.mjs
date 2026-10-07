// Job mail (ADR 0155), the client's half, in headless Chrome against the built page and a fake daemon (route mocking): a mail card
// from GET /inherent/notices with its seen / feedback / dismiss posts, the cue sound only for card_sound and speak, the 合适吗 row,
// one card per id and no return after a dismiss, the digest, the Dashboard's ledger page with its confirmed delete, the applications table (ADR 0177) with its status select and Add form, the daemon's
// `audio_private` (false: no cue for a sounding mail card or an agent notice; true: the cue as before), and a 404 that keeps the app calm. Silent: no desktop window, no audio. Run after `npm run build`.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = path.join(root, 'evidence', 'job-mail');
mkdirSync(dir, { recursive: true });
const daemonPort = Number(process.env.DAEMON_PORT ?? 8797), web = Number(process.env.COMPANION_PORT ?? 5195), daemon = `http://127.0.0.1:${daemonPort}`;
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security', '--mute-audio'] });
let passed = 0;
const check = (name, pass) => { assert.ok(pass, name); passed++; console.log(`PASS ${name}`); };
const wait = ms => new Promise(r => setTimeout(r, ms));
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await wait(100); } }
  const page = await (await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 })).newPage();
  const errors = [], posts = [], deletes = [];
  let featureOn = false, noticeGets = 0, notices = [], flagStatus = 200, skipped, audioPrivate, board = [];
  const flags = [];
  // ADR 0161: seconds per local day on a company's job pages, and job-site time that names no company (both absent on older daemons).
  const day = back => { const d = new Date(Date.now() - back * 86_400_000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
  let otherS;
  // ADR 0177: the tracker's rows (absent on older daemons, which keep the per-company view above) and what the page posted to its two routes.
  let applications;
  const appEdits = [], appAdds = [];
  // ADR 0158: the daemon's standing alert rules, sent with the ledger (absent on older daemons, like `skipped`).
  const rules = [{ id: 'linkedin_alerts', value: 'ledger_only' }];
  page.on('pageerror', error => errors.push(error.message));
  const at = minutes => new Date(Date.now() - minutes * 60_000).toISOString();
  const mail = (id, level, extra = {}) => ({ id, kind: 'mail', title: '面试邀请 · Northwind', line: 'Northwind wants a 30 minute interview for the Co-op role.', level, text: '面试邀请 · Northwind Northwind wants a 30 minute interview.',
    at: at(3), company: 'Northwind', role: 'Backend Co-op', event_at: null, mail_kind: 'interview', ...extra });
  let ledger = [
    { company: 'Northwind', role: 'Backend Co-op', kind: 'interview', last_at: at(3), next_event_at: new Date(Date.now() + 86_400_000).toISOString(), count: 2, time_spent: [{ day: day(1), seconds: 1200 }, { day: day(0), seconds: 3600 }], time_total_s: 4800, mails: [
      { message_id: 'g-1', kind: 'interview', received_at: at(3), subject: 'Interview slots for next week', event_at: null, event_text: 'Could you do Tuesday 10:00 or Wednesday 14:00?' },
      { message_id: 'g-2', kind: 'receipt', received_at: at(60 * 24 * 3), subject: 'We received your application', event_at: null, event_text: null }] },
    { company: 'Acme Robotics', role: 'ML Intern', kind: 'rejection', last_at: at(60 * 24 * 6), next_event_at: null, count: 1, mails: [
      { message_id: 'g-3', kind: 'rejection', received_at: at(60 * 24 * 6), subject: 'Your application to Acme Robotics', event_at: null, event_text: null }] },
    { company: 'Orbit Labs', role: 'SWE Co-op', kind: 'offer', last_at: at(60 * 5), next_event_at: null, count: 1, time_spent: [{ day: day(0), seconds: 20 }], time_total_s: 20, mails: [
      { message_id: 'g-4', kind: 'offer', received_at: at(60 * 5), subject: 'Offer letter: SWE Co-op', event_at: null, event_text: null }] },
  ];
  await page.addInitScript(() => {
    window.__audio = 0; window.__cues = 0;
    const Real = window.AudioContext;
    const resume = Real.prototype.resume;
    Real.prototype.resume = function (...a) { window.__cues++; return resume.apply(this, a); };
    window.AudioContext = class extends Real { constructor(...a) { super(...a); window.__audio++; } };
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, onGhostty: () => () => {}, onMouseDown: cb => { window.__down = cb; return () => {}; }, onClaudeFront: cb => { window.__front = cb; return () => {}; }, plugins: async () => ({}),
    };
    window.__sockets = [];
    window.WebSocket = class { constructor() { window.__sockets.push(this); setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
  });
  await page.route(`${daemon}/**`, route => {
    const url = new URL(route.request().url()), method = route.request().method(), p = url.pathname;
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (p === '/inherent/controls') return json({ mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' });
    if (p === '/inherent/claude-sessions') return json({ sessions: board, error: null });
    if (p === '/inherent/agent-marks') return json({ marks: {} });
    if (p === '/inherent/notices' && method === 'GET') { noticeGets++; if (featureOn) return json(audioPrivate === undefined ? { notices } : { notices, audio_private: audioPrivate }); }
    else if (p.startsWith('/inherent/notices/') && method === 'POST') { posts.push({ id: decodeURIComponent(p.split('/').pop()), body: JSON.parse(route.request().postData() || '{}') }); return json({ ok: true }); }
    else if (p === '/inherent/jobs' && method === 'GET' && featureOn) return json({ ...(skipped ? { ledger, skipped, rules } : { ledger }), ...(applications ? { applications } : {}), ...(otherS === undefined ? {} : { job_site_other_s: otherS }) });
    else if (p === '/inherent/jobs/applications' && method === 'POST') { appAdds.push(JSON.parse(route.request().postData() || '{}')); return json({ ok: true, id: 'hand-1' }); }
    else if (/^\/inherent\/jobs\/applications\/[^/]+$/.test(p) && method === 'POST') { appEdits.push({ id: decodeURIComponent(p.split('/').pop()), body: JSON.parse(route.request().postData() || '{}') }); return json({ ok: true }); }
    else if (/^\/inherent\/jobs\/[^/]+\/flag$/.test(p) && method === 'POST') {
      if (flagStatus === 404) return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
      const id = decodeURIComponent(p.split('/')[3]); flags.push({ id, body: JSON.parse(route.request().postData() || '{}') });
      skipped = skipped.filter(m => m.message_id !== id); return json({ ok: true });
    }
    else if (/^\/inherent\/jobs\/[^/]+\/delete$/.test(p) && method === 'POST') {
      const id = decodeURIComponent(p.split('/')[3]); deletes.push(id);
      ledger = ledger.map(g => ({ ...g, mails: g.mails.filter(m => m.message_id !== id), count: g.mails.filter(m => m.message_id !== id).length })).filter(g => g.mails.length);
      return json({ ok: true });
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  const open = async () => {
    await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemonPort}`);
    await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
    await page.waitForTimeout(800);
  };
  const shot = async (name, clip = { x: 0, y: 0, width: 640, height: 480 }) => { await page.waitForTimeout(900); await page.screenshot({ path: path.join(dir, `${name}.png`), clip }); };
  const card = () => page.locator('.notch-note.is-open .nc').count();
  const shows = () => page.waitForFunction(() => document.querySelector('.notch-note.is-open .nc'), null, { timeout: 8000 });
  const gone = () => page.waitForFunction(() => !document.querySelector('.notch-note.is-open .nc'), null, { timeout: 4000 });
  const of = id => posts.filter(x => x.id === id).map(x => JSON.stringify(x.body));
  const settle = async () => { await page.mouse.move(5, 600); await page.waitForTimeout(900); };

  // (a) a 404 on /inherent/notices: the feature is off. One request, then silence; no card, no error.
  await open();
  await page.waitForTimeout(11_500);
  check('404: notices were asked once, then never again', noticeGets === 1);
  check('404: no card and no page error', await card() === 0 && errors.length === 0);
  // Her feedback sounds warm one AudioContext on load; a cue of the notices would make another.
  const warm = await page.evaluate(() => window.__audio);

  // (b) the feature on. A mail at level card: it shows, silent, and tells the daemon `seen` once.
  featureOn = true; notices = [mail('n-1', 'card')];
  await open();
  await shows();
  const text = await page.locator('.notch-note.is-open .nc').innerText();
  check('a mail card shows its title, line, company and role', /面试邀请 · Northwind/.test(text) && /30 minute interview/.test(text) && /Northwind/.test(text) && /Backend Co-op/.test(text));
  check('level card: no cue sound', await page.evaluate(() => window.__audio) === warm);
  await page.mouse.move(5, 600);
  await page.waitForTimeout(6500);
  check('seen is sent once, even after more polls', JSON.stringify(of('n-1')) === JSON.stringify(['{"action":"seen"}']) && await card() === 1);
  check('a mail card has no Allow or Deny', await page.locator('.notch-note.is-open .btn-warm:not(.nc-right), .notch-note.is-open [data-deny]').count() === 0);
  await shot('mail-card');

  // the 合适吗 row: folded, opens, five levels with the current one marked, 对 tells the daemon.
  check('the feedback row is folded at first', await page.locator('.notch-note.is-open .nc-rate').count() === 0 && await page.locator('.notch-note.is-open .nc-rate-open').count() === 1);
  await page.locator('.notch-note.is-open .nc-rate-open').click();
  await page.waitForSelector('.notch-note.is-open .nc-rate');
  const levels = await page.locator('.notch-note.is-open .nc-lv').allInnerTexts();
  check('five level chips, the card level marked', levels.join('|') === '记下|亮一下|卡片|卡片带声|开口' && await page.locator('.notch-note.is-open .nc-lv.is-now').innerText() === '卡片');
  await shot('mail-card-feedback');
  await page.locator('.notch-note.is-open .nc-right').click();
  await gone();
  check('对 sends feedback right', of('n-1').at(-1) === '{"action":"feedback","reaction":"right"}');
  check('and no dismissed follows it', !of('n-1').some(b => /dismissed/.test(b)));

  // (c) card_sound has the cue; a level chip sends level:<name>.
  const audioBefore = await page.evaluate(() => window.__audio);
  notices = [mail('n-1', 'card'), mail('n-2', 'card_sound', { title: '其他求职邮件 · Orbit Labs', company: 'Orbit Labs', role: 'SWE Co-op', mail_kind: 'job_other' })];
  await shows();
  check('a second mail comes as one card (the first is not shown again)', /Orbit Labs/.test(await page.locator('.notch-note.is-open .nc').innerText()));
  check('level card_sound: the cue sounds', audioBefore === warm && await page.evaluate(() => window.__audio) > warm);
  await settle();
  await page.locator('.notch-note.is-open .nc-rate-open').click();
  await page.locator('.notch-note.is-open .nc-lv', { hasText: '开口' }).click();
  await gone();
  check('a level chip sends level:开口', of('n-2').at(-1) === '{"action":"feedback","reaction":"level:开口"}');

  // (d) the dismiss gestures: Esc, ×, a sideways swipe, a press elsewhere. Each sends dismissed, once.
  const next = async (id, extra) => { notices = [...notices, mail(id, 'card', extra)]; await shows(); await settle(); };
  await next('n-3');
  await page.keyboard.press('Escape'); await gone();
  check('Esc puts the card away and sends dismissed', JSON.stringify(of('n-3')) === JSON.stringify(['{"action":"seen"}', '{"action":"feedback","reaction":"dismissed"}']));
  await next('n-4');
  await page.locator('.notch-note.is-open .nc-dismiss').click(); await gone();
  check('the × sends dismissed', of('n-4').at(-1) === '{"action":"feedback","reaction":"dismissed"}');
  await next('n-5');
  const box = await page.locator('.notch-note.is-open .nc').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(120, 0); await page.mouse.wheel(120, 0); await gone();
  check('a sideways swipe sends dismissed', of('n-5').at(-1) === '{"action":"feedback","reaction":"dismissed"}');
  await next('n-6');
  await page.evaluate(() => window.__down()); await gone();
  check('a press elsewhere sends dismissed', of('n-6').at(-1) === '{"action":"feedback","reaction":"dismissed"}');
  await page.waitForTimeout(6000);
  check('dismissed ids never come back, though the daemon still lists them', await card() === 0 && posts.filter(x => /dismissed/.test(JSON.stringify(x.body))).length === 4);

  // (e) the 合适吗 row folds away after about 10 s untouched.
  await next('n-7');
  await page.locator('.notch-note.is-open .nc-rate-open').click();
  await page.mouse.move(5, 600);
  await page.waitForTimeout(12_500);
  check('the feedback row folds away untouched', await page.locator('.notch-note.is-open .nc-rate').count() === 0 && await page.locator('.notch-note.is-open .nc-rate-open').count() === 1);
  await page.keyboard.press('Escape'); await gone();

  // (f) the summary (ADR 0158): how many and how many interviews, one row each with kind, company, role and time; rows open nothing, its button opens the ledger page.
  const items = [
    { id: 'i-1', title: '面试邀请 · Northwind', company: 'Northwind', role: 'Backend Co-op', mail_kind: 'interview', at: at(30), event_at: new Date(Date.now() + 2 * 86_400_000).toISOString() },
    { id: 'i-2', title: '拒信 · Acme Robotics', company: 'Acme Robotics', role: 'ML Intern', mail_kind: 'rejection', at: at(120) },
    { id: 'i-3', title: '其他求职邮件 · Orbit Labs', company: 'Orbit Labs', role: 'SWE Co-op', mail_kind: 'job_other', at: at(300) }];
  const base = { kind: 'digest', title: '最近两天有 3 封求职邮件，其中 1 封面试', line: '', level: 'card', text: '最近两天有 3 封求职邮件，其中 1 封面试', at: at(1), link: 'jobs' };
  notices = [...notices, { ...base, id: 'd-1', items }];
  await shows(); await settle();
  const digest = await page.locator('.notch-note.is-open .nc-jobs').innerText();
  check('the digest says how many and how many interviews', /最近两天有 3 封求职邮件，其中 1 封面试/.test(digest));
  check('the digest lists a row per mail with company and role', await page.locator('.notch-note.is-open .nc-jobrow').count() === 3 && /Acme Robotics/.test(digest) && /ML Intern/.test(digest) && /Interview|面试/.test(digest) && /Orbit Labs/.test(digest));
  check('digest rows open nothing: no links, no buttons', await page.locator('.notch-note.is-open .nc-jobrow a, .notch-note.is-open .nc-jobrow button').count() === 0);
  await shot('digest');
  await page.keyboard.press('Escape'); await gone();
  check('Esc on the digest sends dismissed', of('d-1').at(-1) === '{"action":"feedback","reaction":"dismissed"}');
  // ADR 0159: a summary row is one company and thread, with `count` mails. One company, one kind: the title carries company and
  // count, the lone row does not repeat the count; a mixed batch keeps the counts of the rows that hold more than one mail.
  const rc = (id, extra) => ({ id, company: 'Reliable Controls', role: 'Firmware QA Analyst Co-op', mail_kind: 'interview', at: at(20), count: 3, ...extra });
  notices = [{ ...base, id: 'd-3', title: 'Reliable Controls 面试有 3 封新邮件', items: [rc('r-1')] }];
  await shows(); await settle();
  const one = await page.locator('.notch-note.is-open .nc-jobs').innerText();
  check('a grouped summary says company and kind in its title', /Reliable Controls 面试有 3 封新邮件/i.test(one));
  check('one grouped row: kind, company and role, and the count is not repeated', await page.locator('.notch-note.is-open .nc-jobrow').count() === 1 && /Interview|面试/.test(one) && /· Firmware QA Analyst Co-op/.test(one) && !/封往来|emails/.test(one));
  await page.keyboard.press('Escape'); await gone();
  notices = [{ ...base, id: 'd-4', title: '最近两天有 4 封求职邮件，其中 3 封面试', items: [rc('r-2'), { id: 'r-3', company: 'Acme Robotics', role: '', mail_kind: 'rejection', at: at(120), count: 1 }] }];
  await shows(); await settle();
  const mixed = await page.locator('.notch-note.is-open .nc-jobrows li').allInnerTexts();
  check('a mixed summary shows the count of a multi-mail row only', mixed.length === 2 && /Reliable Controls · Firmware QA Analyst Co-op · (3 封往来|3 emails)/.test(mixed[0].replace(/\s+/g, ' ')) && !/往来|emails|·/.test(mixed[1]));
  await shot('digest-mixed');
  await page.keyboard.press('Escape'); await gone();
  // ADR 0159: an interview time read from a mail is the row's key fact (24 h, month/day); a short sentence stands in; with neither, the row shows when the latest mail came.
  notices = [{ ...base, id: 'd-5', title: '最近两天有 4 封求职邮件，其中 4 封面试', items: [rc('t-1', { event_at: '2027-10-08T13:00:00' }), rc('t-2', { company: 'Said Co', role: '', event_text: 'Tuesday 10:00', count: 1 }), rc('t-3', { company: 'Plain Co', role: '', count: 1 })] }];
  await shows(); await settle();
  const when = await page.locator('.notch-note.is-open .nc-jobrows li').evaluateAll(rows => rows.map(r => ({ fact: r.querySelector('time.is-event')?.textContent ?? null, plain: r.querySelector('time:not(.is-event)')?.textContent ?? null })));
  check('a row with an extracted time shows it as the key fact', when[0].fact === 'Interview 10/8 13:00' && when[0].plain === null);
  check('a short event sentence stands in when there is no parsed time', when[1].fact === 'Interview Tuesday 10:00');
  check('a row with neither shows only when the latest mail came', when[2].fact === null && /^\d{1,2}[:/]\d{1,2}$/.test(when[2].plain));
  await shot('digest-time');
  await page.keyboard.press('Escape'); await gone();
  // ADR 0159: the 合适吗 row on the summary, the same component as on a single card, folded at first; a choice applies to the whole card (the daemon logs it per mail).
  notices = [{ ...base, id: 'd-6', level: 'card_sound', items }];
  await shows(); await settle();
  check('the summary has the 合适吗 row, folded at first', await page.locator('.notch-note.is-open .nc-jobs .nc-rate').count() === 0 && await page.locator('.notch-note.is-open .nc-jobs .nc-rate-open').count() === 1);
  await page.locator('.notch-note.is-open .nc-rate-open').click();
  await page.waitForSelector('.notch-note.is-open .nc-jobs .nc-rate');
  check('it offers five levels with the summary level marked', (await page.locator('.notch-note.is-open .nc-lv').allInnerTexts()).join('|') === '记下|亮一下|卡片|卡片带声|开口' && await page.locator('.notch-note.is-open .nc-lv.is-now').innerText() === '卡片带声');
  await shot('digest-feedback');
  await page.locator('.notch-note.is-open .nc-lv', { hasText: '开口' }).click();
  await gone();
  check('a level chip on the summary sends level:开口 for the summary id, and no dismissed', of('d-6').at(-1) === '{"action":"feedback","reaction":"level:开口"}' && !of('d-6').some(b => /dismissed/.test(b)));
  notices = [{ ...base, id: 'd-7', items }];
  await shows(); await settle();
  await page.locator('.notch-note.is-open .nc-rate-open').click();
  check('a card-level summary marks 卡片', await page.locator('.notch-note.is-open .nc-lv.is-now').innerText() === '卡片');
  await page.locator('.notch-note.is-open .nc-right').click();
  await gone();
  check('对 on the summary sends feedback right for the summary id', of('d-7').at(-1) === '{"action":"feedback","reaction":"right"}' && !of('d-7').some(b => /dismissed/.test(b)));
  // Its button opens the Dashboard on the job ledger and sends no dismissed.
  notices = [{ ...base, id: 'd-2', items }];
  await shows(); await settle();
  check('the summary has one button, the job list', await page.locator('.notch-note.is-open .nc-jobs .nc-open-jobs').count() === 1);
  await page.locator('.notch-note.is-open .nc-open-jobs').click();
  await page.waitForSelector('.ad .jp-g', { timeout: 5000 });
  check('the button opens the ledger page without a dismissed', await page.locator('.ad .jp-g').count() === 3 && !of('d-2').some(b => /dismissed/.test(b)));
  notices = [];
  await open();

  // (f2) the summary in Chinese, as Allen reads it: one Reliable Controls thread with an extracted time, and without (evidence screenshots).
  await page.evaluate(() => localStorage.setItem('companion-settings-v1', JSON.stringify({ lang: 'zh' })));
  await open();
  const zhRow = extra => ({ id: 'z-1', company: 'Reliable Controls', role: 'Firmware QA Analyst Co-op', mail_kind: 'interview', at: at(20), count: 3, ...extra });
  const zhTitle = 'Reliable Controls 面试有 3 封新邮件';
  for (const [name, row] of [['summary-grouped-time', zhRow({ event_at: '2027-10-08T13:00:00' })], ['summary-grouped-no-time', zhRow({})]]) {
    notices = [{ ...base, id: name, title: zhTitle, items: [row] }];
    await shows(); await settle();
    const zh = await page.locator('.notch-note.is-open .nc-jobs').innerText();
    check(`${name}: zh title, one row, role, and the count is not repeated`, new RegExp(zhTitle, 'i').test(zh) && await page.locator('.notch-note.is-open .nc-jobrow').count() === 1 && /Firmware QA Analyst Co-op/.test(zh) && !/封往来/.test(zh));
    check(`${name}: the time fact is ${name.endsWith('no-time') ? 'absent' : '面试 10/8 13:00'}`, name.endsWith('no-time') ? await page.locator('.notch-note.is-open .nc-jobrow time.is-event').count() === 0 : await page.locator('.notch-note.is-open .nc-jobrow time.is-event').innerText() === '面试 10/8 13:00');
    await shot(name);
    await page.keyboard.press('Escape'); await gone();
  }
  notices = [{ ...base, id: 'summary-grouped-mixed', title: '最近两天有 5 封求职邮件，其中 3 封面试', items: [zhRow({ event_at: '2027-10-08T13:00:00' }), { id: 'z-2', company: 'Acme Robotics', role: '', mail_kind: 'rejection', at: at(120), count: 1 }, { id: 'z-3', company: 'Orbit Labs', role: 'SWE Co-op', mail_kind: 'job_other', at: at(300), count: 1 }] }];
  await shows(); await settle();
  check('a zh mixed summary counts the multi-mail row: 3 封往来', /3 封往来/.test(await page.locator('.notch-note.is-open .nc-jobs').innerText()));
  await shot('summary-grouped-mixed');
  await page.keyboard.press('Escape'); await gone();
  notices = [];
  await page.evaluate(() => localStorage.removeItem('companion-settings-v1'));
  await open();

  // (g) the ledger page in the Dashboard: grouped by company, expand, a confirmed delete.
  await page.locator('.companion-island-target').click();
  await page.waitForSelector('.ad .cb[data-row="jobs"]', { timeout: 8000 });
  check('the ledger icon shows once the route answers', true);
  await page.locator('.ad .cb[data-row="jobs"]').click();
  await page.waitForSelector('.ad .jp-g', { timeout: 5000 });
  check('the ledger lists a section per company', await page.locator('.ad .jp-g').count() === 3);
  const first = await page.locator('.ad .jp-g').first().innerText();
  check('a company shows role, status, last mail, next time and mail count', /Northwind/.test(first) && /Backend Co-op/.test(first) && /Interview|面试/.test(first) && /Last mail|最近来信/.test(first) && /Next|下一个/.test(first) && /2 mails|2 封/.test(first));
  await page.locator('.ad .jp-g[data-company="Northwind"] .jp-top').click();
  await page.waitForSelector('.ad .jp-mails li');
  check('a company opens to its mails', await page.locator('.ad .jp-g[data-company="Northwind"] .jp-mails li').count() === 2);
  await shot('ledger', { x: 0, y: 0, width: 640, height: 640 });
  // ADR 0161: the time column, per company, dim, only when there is some; the per-day detail opens with the company; the other-sites line only when > 0.
  const spent = company => page.locator(`.ad .jp-g[data-company="${company}"] [data-time]`);
  check('a company shows how long its job pages held him', /^(Spent 1 h 20 min|花了 1 小时 20 分)$/.test(await spent('Northwind').innerText()));
  check('a company without time shows no time line', await spent('Acme Robotics').count() === 0);
  check('under a minute is said so', /^(Spent under 1 min|花了 不到 1 分)$/.test(await spent('Orbit Labs').innerText()));
  check('the time line is dim, not ink', await spent('Northwind').evaluate(el => getComputedStyle(el).color !== getComputedStyle(el.closest('.jp-g').querySelector('.jp-name b')).color));
  const days = await page.locator('.ad .jp-g[data-company="Northwind"] [data-days]').innerText();
  check('an open company lists its days, newest first', days.split(' · ').length === 2 && days.startsWith(`${new Date().getMonth() + 1}/${new Date().getDate()} `) && /(1 h|1 小时)/.test(days.split(' · ')[0]) && /(20 min|20 分)$/.test(days));
  check('without job_site_other_s there is no other-sites line', await page.locator('.ad [data-other]').count() === 0);
  otherS = 300; // the next reload (a delete does one) brings the other-sites line
  await page.locator('.ad li[data-id="g-2"] [data-act="delete"]').click();
  check('delete asks first and posts nothing yet', await page.locator('.ad li[data-id="g-2"] [data-act="yes"]').count() === 1 && deletes.length === 0);
  await shot('ledger-confirm', { x: 0, y: 0, width: 640, height: 640 });
  await page.locator('.ad li[data-id="g-2"] [data-act="no"]').click();
  check('Keep puts it back without a post', await page.locator('.ad li[data-id="g-2"] [data-act="delete"]').count() === 1 && deletes.length === 0);
  await page.locator('.ad li[data-id="g-2"] [data-act="delete"]').click();
  await page.locator('.ad li[data-id="g-2"] [data-act="yes"]').click();
  await page.waitForFunction(() => !document.querySelector('.ad li[data-id="g-2"]'), null, { timeout: 3000 });
  check('a confirmed delete posts the mail id and the row goes', JSON.stringify(deletes) === '["g-2"]');
  await page.waitForSelector('.ad [data-other]', { timeout: 5000 });
  check('job-site time that names no company is its own line', /^(Other job sites spent 5 min|其他求职网站 花了 5 分)$/.test(await page.locator('.ad [data-other]').innerText()));
  await shot('ledger-time', { x: 0, y: 0, width: 640, height: 640 });
  for (const id of ['g-1', 'g-3', 'g-4']) {
    if (!await page.locator(`li[data-id="${id}"]`).count()) {
      const company = { 'g-1': 'Northwind', 'g-3': 'Acme Robotics', 'g-4': 'Orbit Labs' }[id];
      await page.locator(`.ad .jp-g[data-company="${company}"] .jp-top`).click();
      await page.waitForSelector(`.ad li[data-id="${id}"]`);
    }
    await page.locator(`.ad li[data-id="${id}"] [data-act="delete"]`).click();
    await page.locator(`.ad li[data-id="${id}"] [data-act="yes"]`).click();
    await page.waitForFunction(i => !document.querySelector(`.ad li[data-id="${i}"]`), id, { timeout: 3000 });
  }
  check('with every mail deleted the page says so', /No job mail yet|还没有求职邮件/.test(await page.locator('.ad .jp').innerText()) && deletes.length === 4);
  await shot('ledger-empty', { x: 0, y: 0, width: 640, height: 480 });

  // (g2) held-back mail: a collapsed section, the sender, subject and likelihood, a button that flags it; no route = no button.
  skipped = [
    { message_id: 's-1', received_at: at(90), sender_name: 'Zeta Careers', sender_domain: 'zeta.example', subject: 'Your profile caught our eye', p_job: 0.62 },
    { message_id: 's-2', received_at: at(200), sender_name: 'Campus Board', sender_domain: 'board.example', subject: 'Weekly postings from the campus job board: Co-op, intern and new grad roles in Victoria', p_job: 0.31 }];
  // ADR 0158: a ledger row of kind `other` (an account notice) is labelled, not shown as a rejection or blank.
  ledger = [{ company: 'CGI', role: '', kind: 'other', last_at: at(30), next_event_at: null, count: 1, mails: [
    { message_id: 'o-1', kind: 'other', received_at: at(30), subject: 'CGI - User Information', event_at: null, event_text: null }] }];
  await page.locator('.ad .pg-back').click();
  await page.waitForTimeout(800);
  await page.locator('.ad .cb[data-row="jobs"]').click();
  await page.waitForSelector('.ad .jp-skip', { timeout: 40_000 });
  check('the ledger page shows the LinkedIn rule from the daemon rules', /LinkedIn 职位提醒：只进账本，不提醒|LinkedIn job alerts: ledger only, no alert/.test(await page.locator('.ad .jp-rule').innerText()));
  check('a kind other row is labelled as an account notice', /Account|账号通知/.test(await page.locator('.ad .jp-g[data-company="CGI"]').innerText()));
  check('held-back mail is a collapsed section with its count', await page.locator('.ad .jp-skip li').count() === 0 && /Held back as not job|被判成不相关的可疑邮件/.test(await page.locator('.ad .jp-skip').innerText()));
  await page.locator('.ad .jp-skip .jp-top').click();
  const held = await page.locator('.ad .jp-skip').innerText();
  check('it lists sender, subject and likelihood', await page.locator('.ad .jp-skip li').count() === 2 && /Zeta Careers/.test(held) && /Your profile caught our eye/.test(held) && /62%/.test(held) && /31%/.test(held));
  const sub = await page.locator('.ad .jp-skip li[data-id="s-2"] .jp-sk-sub').evaluate(el => ({ box: el.getBoundingClientRect().width, scroll: el.scrollWidth, lines: Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight)), text: el.textContent.length }));
  check('a held-back subject has room: wide enough for 40+ characters, at most two lines', sub.box > 280 && sub.lines <= 2 && sub.text > 40);
  await shot('ledger-held-back', { x: 0, y: 0, width: 640, height: 480 });
  await page.locator('.ad .jp-skip li[data-id="s-1"] [data-act="flag"]').click();
  await page.waitForFunction(() => !document.querySelector('.ad .jp-skip li[data-id="s-1"]'), null, { timeout: 3000 });
  check('the flag button posts should_alert for that mail and the row goes', JSON.stringify(flags) === '[{"id":"s-1","body":{"reaction":"should_alert"}}]');
  flagStatus = 404;
  await page.locator('.ad .jp-skip li[data-id="s-2"] [data-act="flag"]').click();
  await page.waitForTimeout(500);
  check('a 404 on flag hides the buttons and keeps the row', await page.locator('.ad .jp-skip [data-act="flag"]').count() === 0 && await page.locator('.ad .jp-skip li[data-id="s-2"]').count() === 1);
  // an older daemon sends no `skipped`: nothing to show, nothing breaks
  skipped = undefined;
  await open();
  await page.locator('.companion-island-target').click();
  await page.waitForSelector('.ad .cb[data-row="jobs"]', { timeout: 8000 });
  await page.locator('.ad .cb[data-row="jobs"]').click();
  await page.waitForSelector('.ad .jp');
  await page.waitForTimeout(500);
  check('without skipped the page has no held-back section, no rule line and no error', await page.locator('.ad .jp-skip').count() === 0 && await page.locator('.ad .jp-rule').count() === 0 && errors.length === 0);

  // (g4) ADR 0177: with `applications` the page is one table, a row per job applied to.
  applications = [
    { id: 'app-1', company: 'Northwind', role: 'Backend Co-op', status: 'interviewing', status_auto: true, applied_at: '2026-09-12', last_at: at(3), next_event_at: new Date(Date.now() + 86_400_000).toISOString(), count: 2, note: '', source: 'mail', mails: [
      { message_id: 'h-1', kind: 'interview', received_at: at(3), subject: 'Interview slots for next week', event_at: null, event_text: null },
      { message_id: 'h-2', kind: 'receipt', received_at: at(60 * 24 * 3), subject: 'We received your application', event_at: null, event_text: null }] },
    { id: 'app-2', company: 'Orbit Labs', role: 'SWE Co-op', status: 'no_reply', status_auto: false, applied_at: '2026-09-10', last_at: at(60 * 24 * 30), next_event_at: null, count: 0, note: 'via a friend', source: 'manual', mails: [] }];
  await open();
  await page.locator('.companion-island-target').click();
  await page.waitForSelector('.ad .cb[data-row="jobs"]', { timeout: 8000 });
  await page.locator('.ad .cb[data-row="jobs"]').click();
  await page.waitForSelector('.ad .jp-t', { timeout: 5000 });
  const northwind = await page.locator('.ad .jp-r[data-company="Northwind"]').innerText();
  check('the page is a table with a row per application, with company, role, date and mail count', await page.locator('.ad .jp-r').count() === 2 && await page.locator('.ad .jp-g').count() === 0 && /Northwind/.test(northwind) && /Backend Co-op/.test(northwind) && /9\/12/.test(northwind) && /2$/.test(northwind.trim()));
  check('the status select shows the status and a faint mark only where he set it', await page.locator('.ad .jp-r[data-company="Northwind"] select[data-act="status"]').inputValue() === 'interviewing' && await page.locator('.ad .jp-r[data-company="Northwind"] [data-hand]').count() === 0 && await page.locator('.ad .jp-r[data-company="Orbit Labs"] [data-hand]').count() === 1);
  check('the status is worded: 面试中 or Interviewing, 没回音 or No reply', /Interviewing|面试中/.test(northwind) && /No reply|没回音/.test(await page.locator('.ad .jp-r[data-company="Orbit Labs"]').innerText()));
  await shot('applications', { x: 0, y: 0, width: 640, height: 480 });
  await page.locator('.ad .jp-r[data-company="Northwind"] select[data-act="status"]').selectOption('offer');
  await page.waitForFunction(() => document.querySelector('.ad .jp-r[data-company="Northwind"] select')?.value === 'offer', null, { timeout: 3000 });
  check('changing the select posts the edit route with the status', JSON.stringify(appEdits) === '[{"id":"app-1","body":{"status":"offer"}}]');
  await page.locator('.ad .jp-r[data-company="Northwind"] .jp-top').click();
  await page.waitForSelector('.ad .jp-x li');
  check('a row opens to its mails and a note', await page.locator('.ad .jp-x li').count() === 2 && await page.locator('.ad .jp-x [data-act="note"]').count() === 1);
  await page.locator('.ad .jp-x [data-act="note"]').fill('phone screen booked');
  await page.locator('.ad .jp-x [data-act="note"]').blur();
  await page.waitForTimeout(300);
  check('leaving the note posts it', JSON.stringify(appEdits[1]) === '{"id":"app-1","body":{"note":"phone screen booked"}}');
  await page.locator('.ad [data-act="add"]').click();
  await page.locator('.ad .jp-form [data-f="company"]').fill('Helix');
  await page.locator('.ad .jp-form [data-f="role"]').fill('Backend Intern');
  await page.locator('.ad .jp-form [data-f="applied_at"]').fill('2026-09-20');
  await page.locator('.ad .jp-form [data-f="status"]').selectOption('rejected');
  await page.locator('.ad .jp-form [data-act="save"]').click();
  await page.waitForFunction(() => !document.querySelector('.ad .jp-form'), null, { timeout: 3000 });
  check('Add posts the manual route with company, role, date and status, and the form closes', JSON.stringify(appAdds) === '[{"company":"Helix","role":"Backend Intern","applied_at":"2026-09-20","status":"rejected"}]');
  applications = undefined;

  // (g3) ADR 0155: the daemon's `audio_private`. False: a sounding mail card and an agent notice come with no cue. True: the cue as before.
  const cues = () => page.evaluate(() => window.__cues);
  const session = (id, phase) => ({ agent: 'claude', session_id: id, kind: 'interactive', phase, title: `Session ${id}`, project: 'jarvis', branch: 'main', cwd: '/x', where: 'Ghostty', prompt: 'go', activity: 'Wants to run npm test', last_message: 'Needs your decision', started_ms: Date.now() - 60_000, updated_ms: Date.now(), request: null, compacting: false, error: '' });
  featureOn = true; audioPrivate = false; board = []; notices = [mail('p-1', 'card_sound', { company: 'Orbit Labs' })];
  await open(); await shows(); await page.waitForTimeout(500);
  check('audio_private false: a card_sound mail still shows', await card() === 1);
  check('audio_private false: its cue does not sound', await cues() === 0);
  await page.keyboard.press('Escape'); await gone();
  audioPrivate = true; notices = [mail('p-1', 'card_sound'), mail('p-2', 'card_sound', { company: 'Helix' })];
  await shows();
  check('audio_private true: the next card_sound mail sounds its cue', await cues() > 0);
  notices = []; audioPrivate = false; board = [session('a-1', 'working')];
  await open(); await page.waitForTimeout(1800);
  board = [session('a-1', 'needs_input')];
  await shows(); await page.waitForTimeout(500);
  check('audio_private false: an agent notice shows', await card() === 1);
  check('audio_private false: its cue does not sound', await cues() === 0);
  audioPrivate = true; board = [session('a-2', 'working')];
  await open(); await page.waitForTimeout(1800);
  board = [session('a-2', 'needs_input')];
  await shows(); await page.waitForTimeout(500);
  check('audio_private true: an agent notice sounds its cue', await cues() > 0);
  audioPrivate = undefined; board = [session('a-3', 'working')];
  await open(); await page.waitForTimeout(1800);
  board = [session('a-3', 'needs_input')];
  await shows(); await page.waitForTimeout(500);
  check('no audio_private field: the old gate, the cue sounds', await cues() > 0);

  // (h) the route gone (404): no ledger icon, no card, no error.
  featureOn = false; notices = []; ledger = [];
  await open();
  await page.locator('.companion-island-target').click();
  await page.waitForSelector('.ad .view', { timeout: 5000 });
  await page.waitForTimeout(1500);
  check('404 on jobs: the ledger icon is hidden', await page.locator('.ad .cb[data-row="jobs"]').count() === 0 && await page.locator('.ad .cb[data-row="settings"]').count() === 1);
  check('no page errors', errors.length === 0);
  if (errors.length) console.log(errors);
  console.log(`${passed} checks passed`);
} finally { await browser.close(); server.kill(); }
