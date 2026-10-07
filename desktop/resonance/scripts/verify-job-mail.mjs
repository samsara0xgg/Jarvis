// Job mail (ADR 0155), the client's half, in headless Chrome against the built page and a fake daemon (route mocking): a mail card
// from GET /inherent/notices with its seen / feedback / dismiss posts, the cue sound only for card_sound and speak, the 合适吗 row,
// one card per id and no return after a dismiss, the digest, the Dashboard's ledger page with its confirmed delete, the applications as cards (ADR 0177, 0182) with their status pill, timeline, interview, links, Gmail buttons and Add form, the daemon's
// `audio_private` (false: no cue for a sounding mail card or an agent notice; true: the cue as before), a glow (ADR 0187: one amber point in the
// wing's turn group, no card and no cue, listed under Your turn, cleared by its ✕ or by opening the job list, shown through no-pop and a call and
// frozen at dnd), and a 404 that keeps the app calm. Silent: no desktop window, no audio. Run after `npm run build`.
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
  let featureOn = false, noticeGets = 0, notices = [], flagStatus = 200, skipped, audioPrivate, board = [], quietLevel = 'off', holdWord;
  const flags = [];
  // ADR 0161: seconds per local day on a company's job pages, and job-site time that names no company (both absent on older daemons).
  const day = back => { const d = new Date(Date.now() - back * 86_400_000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
  let otherS;
  // ADR 0177: the tracker's rows (absent on older daemons, which keep the per-company view above) and what the page posted to its two routes.
  let applications;
  const appEdits = [], appAdds = [], cancels = [];
  // ADR 0176: what the page told the daemon is on screen (POST /inherent/view).
  const views = [];
  const lastJobsView = () => views.filter(v => v.page === 'jobs').at(-1);
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
    window.__audio = 0; window.__cues = 0; window.__opened = [];
    const Real = window.AudioContext;
    const resume = Real.prototype.resume;
    Real.prototype.resume = function (...a) { window.__cues++; return resume.apply(this, a); };
    window.AudioContext = class extends Real { constructor(...a) { super(...a); window.__audio++; } };
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, openUrl: async url => { window.__opened.push(['url', url]); return true; }, openMail: async id => { window.__opened.push(['mail', id]); return true; }, onGhostty: () => () => {}, onMouseDown: cb => { window.__down = cb; return () => {}; }, onClaudeFront: cb => { window.__front = cb; return () => {}; }, plugins: async () => ({}),
    };
    window.__sockets = [];
    window.WebSocket = class { constructor() { window.__sockets.push(this); setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
    window.__emit = (op, payload) => window.__sockets.at(-1).onmessage({ data: JSON.stringify({ op, payload }) });
  });
  await page.route(`${daemon}/**`, route => {
    const url = new URL(route.request().url()), method = route.request().method(), p = url.pathname;
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (p === '/inherent/view') { views.push(JSON.parse(route.request().postData() || '{}')); return json({ ok: true }); }
    if (p === '/inherent/controls') return json({ mic_muted: false, speech_muted: false, conversation: false, quiet: quietLevel });
    if (p === '/inherent/claude-sessions') return json({ sessions: board, error: null });
    if (p === '/inherent/agent-marks') return json({ marks: {} });
    if (p === '/inherent/notices' && method === 'GET') { noticeGets++; if (featureOn) return json({ notices, ...(audioPrivate === undefined ? {} : { audio_private: audioPrivate }), ...(holdWord === undefined ? {} : { hold: holdWord }) }); }
    else if (p.startsWith('/inherent/notices/') && method === 'POST') { posts.push({ id: decodeURIComponent(p.split('/').pop()), body: JSON.parse(route.request().postData() || '{}') }); return json({ ok: true }); }
    else if (p === '/inherent/jobs' && method === 'GET' && featureOn) return json({ ...(skipped ? { ledger, skipped, rules } : { ledger }), ...(applications ? { applications } : {}), ...(otherS === undefined ? {} : { job_site_other_s: otherS }) });
    else if (p === '/inherent/jobs/applications' && method === 'POST') { appAdds.push(JSON.parse(route.request().postData() || '{}')); return json({ ok: true, id: 'hand-1' }); }
    else if (/^\/inherent\/jobs\/applications\/[^/]+\/cancel-reminders$/.test(p) && method === 'POST') { const id = decodeURIComponent(p.split('/')[4]); cancels.push(id); const a = (applications ?? []).find(x => x.id === id); if (a?.reminders) a.reminders.cancelled = true; return json({ ok: true }); }
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
  // The Dashboard page scrolls in 466 px; the evidence shots lift that so a whole list shows.
  const tall = () => page.addStyleTag({ content: '.ad .view{height:920px!important}' });
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
  check('five level chips, the card level marked', levels.join('|') === 'Log only|Glow|Card|Card + sound|Speak' && await page.locator('.notch-note.is-open .nc-lv.is-now').innerText() === 'Card');
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
  await page.locator('.notch-note.is-open .nc-lv', { hasText: 'Speak' }).click();
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
  check('it offers five levels with the summary level marked', (await page.locator('.notch-note.is-open .nc-lv').allInnerTexts()).join('|') === 'Log only|Glow|Card|Card + sound|Speak' && await page.locator('.notch-note.is-open .nc-lv.is-now').innerText() === 'Card + sound');
  await shot('digest-feedback');
  await page.locator('.notch-note.is-open .nc-lv', { hasText: 'Speak' }).click();
  await gone();
  check('a level chip on the summary sends level:开口 for the summary id, and no dismissed', of('d-6').at(-1) === '{"action":"feedback","reaction":"level:开口"}' && !of('d-6').some(b => /dismissed/.test(b)));
  notices = [{ ...base, id: 'd-7', items }];
  await shows(); await settle();
  await page.locator('.notch-note.is-open .nc-rate-open').click();
  check('a card-level summary marks Card', await page.locator('.notch-note.is-open .nc-lv.is-now').innerText() === 'Card');
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

  // (g4) ADR 0177, 0182: with `applications` the page is a list of cards, one per job applied to.
  const soonAt = new Date(Date.now() + 2 * 86_400_000 + 3_600_000).toISOString();
  const teams = 'https://teams.microsoft.com/l/meetup-join/19%3Ameeting_abc/0';
  applications = [
    { id: 'app-1', company: 'Reliable Controls', role: 'Firmware QA Analyst Co-op', status: 'interviewing', status_auto: true, applied_at: '2026-09-28', last_at: at(3), next_event_at: soonAt, count: 2, note: '', source: 'mail',
      timeline: [{ kind: 'applied', at: at(60 * 24 * 8), future: false }, { kind: 'interview_invite', at: at(60 * 24 * 3), future: false }, { kind: 'interview', at: soonAt, future: true }],
      interview: { at: soonAt, mode: 'online', platform: 'Teams', join_url: teams, location: null, interviewers: ['Jill Crowe'] },
      reminders: { at: soonAt, evening: true, before: true, evening_at: '20:00', before_min: 30, outlook: true, cancelled: false },
      links: { portal_url: 'https://reliable.wd3.myworkdayjobs.com/en-US/careers/userHome', posting_url: 'https://reliablecontrols.com/careers/firmware-qa-analyst-co-op' },
      mails: [
      { message_id: 'h-1', thread_id: 'th-1', kind: 'interview', received_at: at(3), subject: 'Interview slots for next week', event_at: null, event_text: null },
      { message_id: 'h-2', thread_id: null, kind: 'receipt', received_at: at(60 * 24 * 3), subject: 'We received your application', event_at: null, event_text: null }] },
    { id: 'app-4', company: 'Cambio Earth', role: 'Software Engineering Co-op', status: 'applied', status_auto: true, applied_at: '2026-10-02', last_at: at(60 * 24 * 4), next_event_at: null, count: 1, note: '', source: 'mail',
      timeline: [{ kind: 'applied', at: at(60 * 24 * 4), future: false }], interview: null, links: { portal_url: null, posting_url: null }, mails: [
      { message_id: 'h-4', thread_id: 'th-4', kind: 'receipt', received_at: at(60 * 24 * 4), subject: 'Your application was sent to Cambio Earth', event_at: null, event_text: null }] },
    { id: 'app-2', company: 'Orbit Labs', role: 'SWE Co-op', status: 'no_reply', status_auto: false, applied_at: '2026-09-10', last_at: at(60 * 24 * 30), next_event_at: null, count: 0, note: 'via a friend', source: 'manual', mails: [],
      timeline: [{ kind: 'applied', at: '2026-09-10', future: false }], interview: null, links: { portal_url: null, posting_url: null } },
    { id: 'app-3', company: 'Acme Robotics', role: 'ML Intern', status: 'rejected', status_auto: true, applied_at: '2026-09-14', last_at: at(60 * 24 * 6), next_event_at: null, count: 1, note: '', source: 'mail',
      timeline: [{ kind: 'applied', at: at(60 * 24 * 12), future: false }, { kind: 'rejection', at: at(60 * 24 * 6), future: false }], interview: null, links: { portal_url: null, posting_url: null }, mails: [
      { message_id: 'h-3', thread_id: 'th-3', kind: 'rejection', received_at: at(60 * 24 * 6), subject: 'Your application to Acme Robotics', event_at: null, event_text: null }] }];
  await open();
  await page.locator('.companion-island-target').click();
  await page.waitForSelector('.ad .cb[data-row="jobs"]', { timeout: 8000 });
  await page.locator('.ad .cb[data-row="jobs"]').click();
  await page.waitForSelector('.ad .jc', { timeout: 5000 });
  const reliable = await page.locator('.ad .jc[data-company="Reliable Controls"]').innerText();
  check('the page is a list of cards, one per application, with company, role line and applied day', await page.locator('.ad .jc').count() === 4 && await page.locator('.ad .jp-t').count() === 0 && await page.locator('.ad .jp-g').count() === 0 && /Reliable Controls/.test(reliable) && /Firmware QA Analyst Co-op/.test(reliable) && /9\/28 投递|Applied 9\/28/.test(reliable));
  check('the vid hook rides on every card', await page.locator('.ad .jc[data-vid="Reliable Controls|Firmware QA Analyst Co-op"]').count() === 1 && await page.locator('.ad .jc[data-vid]').count() === 4);
  check('the status is a pill, coloured by status, worded 面试中 or Interviewing', await page.locator('.ad .jc[data-company="Reliable Controls"] .jc-pill.is-interviewing').count() === 1 && /Interviewing|面试中/.test(await page.locator('.ad .jc[data-company="Reliable Controls"] .jc-pill').innerText()) && await page.locator('.ad .jc[data-company="Acme Robotics"] .jc-pill.is-rejected').count() === 1 && await page.locator('.ad .jc[data-company="Orbit Labs"] .jc-pill.is-no_reply').count() === 1);
  check('the pill is the status picker, and a faint mark shows only where he set it', await page.locator('.ad .jc[data-company="Reliable Controls"] select[data-act="status"]').inputValue() === 'interviewing' && await page.locator('.ad .jc[data-company="Reliable Controls"] [data-hand]').count() === 0 && await page.locator('.ad .jc[data-company="Orbit Labs"] [data-hand]').count() === 1);
  check('an interviewing card names its interview time on its second line', /面试 \d+\/\d+ \d\d:\d\d|Interview \d+\/\d+ \d\d:\d\d/.test(await page.locator('.ad .jc[data-company="Reliable Controls"] [data-line]').innerText()));
  check('a no_reply card says how many days of silence', /已 30 天没回音|No reply for 30 days/.test(await page.locator('.ad .jc[data-company="Orbit Labs"] [data-line]').innerText()));
  check('a collapsed card shows no timeline, interview or mails', await page.locator('.ad .jc-x').count() === 0);
  await page.waitForTimeout(300);
  const folded = lastJobsView();
  check('folded, the view report keeps each row\'s jobKey id and adds its newest mail id (none for a hand-added card)', folded?.item === null && folded.rows[0].id === 'Reliable Controls|Firmware QA Analyst Co-op' && folded.rows[0].mail_id === 'h-1' && folded.rows.find(r => r.id.startsWith('Cambio Earth')).mail_id === 'h-4' && !('mail_id' in folded.rows.find(r => r.id.startsWith('Orbit Labs'))));
  await tall(); await shot('applications', { x: 0, y: 0, width: 640, height: 640 });
  await page.locator('.ad .jc[data-company="Reliable Controls"] select[data-act="status"]').selectOption('offer');
  await page.waitForFunction(() => document.querySelector('.ad .jc[data-company="Reliable Controls"] select')?.value === 'offer', null, { timeout: 3000 });
  check('changing the pill posts the edit route with the status', JSON.stringify(appEdits) === '[{"id":"app-1","body":{"status":"offer"}}]');
  await page.locator('.ad .jc[data-company="Reliable Controls"] .jp-top').click();
  await page.waitForSelector('.ad .jc-x li');
  const steps = page.locator('.ad .jc-x [data-row="timeline"] li');
  check('an open card shows its timeline, the interview ahead hollow with the days left', await steps.count() === 3 && await page.locator('.ad .jc-x [data-row="timeline"] .is-future').count() === 1 && /(in 2 days|还有 2 天)/.test(await page.locator('.ad .jc-x [data-step="interview"]').innerText()) && await page.locator('.ad .jc-x [data-step="interview_invite"]').count() === 1);
  const interview = await page.locator('.ad .jc-x [data-row="interview"]').innerText();
  check('the interview row has the time, mode and platform, the interviewer and a join button', /\d+\/\d+ .+ \d\d:\d\d/.test(interview) && /(Online|线上) · Teams/.test(interview) && /Jill Crowe/.test(interview) && await page.locator('.ad .jc-x [data-act="join"]').count() === 1);
  check('there are links to the application status and the posting', await page.locator('.ad .jc-x [data-row="links"] button').count() === 2);
  check('the mails are listed with a Gmail button each', await page.locator('.ad .jc-x [data-row="mails"] li').count() === 2 && await page.locator('.ad .jc-x li [data-act="gmail"]').count() === 2 && await page.locator('.ad .jc-x [data-act="note"]').count() === 1);
  await page.waitForTimeout(300);
  const unfolded = lastJobsView();
  check('unfolded, the card is the open item and its mails are the rows in screen order, as a kind label and the subject', JSON.stringify(unfolded?.item) === JSON.stringify({ kind: 'job', id: 'Reliable Controls|Firmware QA Analyst Co-op', title: 'Reliable Controls — Firmware QA Analyst Co-op' }) && JSON.stringify(unfolded.rows) === JSON.stringify([{ id: 'h-1', title: 'Interview Interview slots for next week' }, { id: 'h-2', title: 'Received We received your application' }]));
  await tall(); await shot('applications-open', { x: 0, y: 0, width: 640, height: 1000 });
  // ADR 0186: the muted line of what Jarvis set for the interview, and the one button that undoes it.
  const armed = await page.locator('.ad .jc-x [data-reminders]').innerText();
  check('the interview row says which reminders are set and that Outlook has it, with a cancel button', /已设提醒：前一晚 20:00、开始前 30 分钟 · 已写入 Outlook 日历|Reminders set: evening before 20:00, 30 min before · written to Outlook calendar/.test(armed) && /取消提醒|Cancel reminders/.test(armed) && await page.locator('.ad .jc-x [data-act="cancel-reminders"]').count() === 1);
  await page.locator('.ad .jc-x [data-act="cancel-reminders"]').click();
  await page.waitForFunction(() => /已取消|cancelled/.test(document.querySelector('.ad .jc-x [data-reminders]')?.textContent ?? ''), null, { timeout: 3000 });
  check('cancel posts the application id, and the line says so with no button left', JSON.stringify(cancels) === '["app-1"]' && await page.locator('.ad .jc-x [data-act="cancel-reminders"]').count() === 0);
  await page.locator('.ad .jc-x [data-act="join"]').click();
  await page.locator('.ad .jc-x [data-act="portal"]').click();
  await page.locator('.ad .jc-x [data-act="posting"]').click();
  await page.locator('.ad .jc-x li[data-id="h-1"] [data-act="gmail"]').click();
  await page.locator('.ad .jc-x li[data-id="h-2"] [data-act="gmail"]').click();
  check('Join, Status and Posting open their https address; Gmail opens the thread, else the message', JSON.stringify(await page.evaluate(() => window.__opened)) === JSON.stringify([['url', teams], ['url', 'https://reliable.wd3.myworkdayjobs.com/en-US/careers/userHome'], ['url', 'https://reliablecontrols.com/careers/firmware-qa-analyst-co-op'], ['mail', 'th-1'], ['mail', 'h-2']]));
  await page.locator('.ad .jc-x [data-act="note"]').fill('phone screen booked');
  await page.locator('.ad .jc-x [data-act="note"]').blur();
  await page.waitForTimeout(300);
  check('leaving the note posts it', JSON.stringify(appEdits[1]) === '{"id":"app-1","body":{"note":"phone screen booked"}}');
  await page.locator('.ad .jc[data-company="Orbit Labs"] .jp-top').click();
  await page.waitForSelector('.ad .jc[data-company="Orbit Labs"] .jc-x');
  check('a hand-added card opens to its one step and no interview, links or mails', await page.locator('.ad .jc[data-company="Orbit Labs"] [data-row="timeline"] li').count() === 1 && await page.locator('.ad .jc[data-company="Orbit Labs"] [data-row="interview"], .ad .jc[data-company="Orbit Labs"] [data-row="links"], .ad .jc[data-company="Orbit Labs"] [data-row="mails"]').count() === 0);
  await page.locator('.ad [data-act="add"]').click();
  await page.locator('.ad .jp-form [data-f="company"]').fill('Helix');
  await page.locator('.ad .jp-form [data-f="role"]').fill('Backend Intern');
  await page.locator('.ad .jp-form [data-f="applied_at"]').fill('2026-09-20');
  await page.locator('.ad .jp-form [data-f="status"]').selectOption('rejected');
  await page.locator('.ad .jp-form [data-act="save"]').click();
  await page.waitForFunction(() => !document.querySelector('.ad .jp-form'), null, { timeout: 3000 });
  check('Add posts the manual route with company, role, date and status, and the form closes', JSON.stringify(appAdds) === '[{"company":"Helix","role":"Backend Intern","applied_at":"2026-09-20","status":"rejected"}]');
  // ADR 0176: she names a mail id of a folded card; the card unfolds and the mail's line is lit.
  await page.evaluate(() => window.__sockets.at(-1).onmessage({ data: JSON.stringify({ op: 'present', payload: { page: 'jobs', item_id: 'h-2', kind: 'row' } }) }));
  await page.waitForSelector('.ad .jc[data-company="Reliable Controls"] li[data-id="h-2"].is-lit', { timeout: 3000 });
  check('show_on_dashboard with a job mail id unfolds that card and lights the mail', await page.locator('.ad .jc-x').count() === 1 && await page.locator('.ad .jc[data-company="Orbit Labs"] .jc-x').count() === 0);
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

  // (g4) ADR 0187: a glow is one more amber point in the wing's turn group: no card, no cue, never told `seen` on arrival (the daemon serves it
  // until it is). Its row follows the sessions under Your turn; a click opens the job list and tells `seen`, the ✕ tells `dismissed`. It shows
  // at quiet, no-pop and through a call; dnd freezes the wing as it was, a glow included.
  const glow = (id, company, extra = {}) => mail(id, 'glow', { title: `Application update · ${company}`, line: `${company} replied to your application (ML Intern): not this time.`, mail_kind: 'rejection', company, role: 'ML Intern', at: at(7), ...extra });
  const marks = () => page.locator('.notch').getAttribute('data-marks');
  const counts = () => page.locator('.notch').getAttribute('data-counts');
  const rings = () => page.locator('.notch').getAttribute('data-rings');
  const markIs = (want, ms = 8000) => page.waitForFunction(w => document.querySelector('.notch')?.dataset.marks === w, want, { timeout: ms });
  const emitQuiet = level => page.evaluate(q => window.__emit('controls', { mic_muted: false, speech_muted: false, conversation: false, quiet: q }), level);
  const toWing = async () => { await page.mouse.move(447, 14); await page.evaluate(() => window.__cursor({ x: 447, y: 14 })); };
  // The pointer on a row, for the page's mouse and for the island's own pointer report (it reads the second).
  const onRow = async row => { await row.hover(); const b = await row.boundingBox(); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [b.x + b.width / 2, b.y + b.height / 2]); };
  const away = async () => { await page.mouse.move(5, 600); await page.evaluate(() => window.__cursor({ x: -1e4, y: -1e4 })); await page.waitForTimeout(900); };
  const drop = page.locator('.notch-drop.is-open');
  // The colour the turn mark is drawn in: the strongest pixel beside its centre (amber is red over blue, the finished ring is green over red).
  const turnColour = () => page.evaluate(() => {
    const cv = document.querySelector('.notch-fx'), r = cv.getBoundingClientRect(), k = cv.width / r.width, cx = 437.7 + 2.6, d = cv.getContext('2d').getImageData(Math.round((cx - 4 - r.left) * k), Math.round(12 * k), Math.round(8 * k), Math.round(8 * k)).data;
    let best = [0, 0, 0, 0];
    for (let i = 0; i < d.length; i += 4) if (d[i + 3] > best[3]) best = [d[i], d[i + 1], d[i + 2], d[i + 3]];
    return best;
  });
  featureOn = true; audioPrivate = true; board = []; quietLevel = 'off'; holdWord = undefined;
  notices = [glow('g-a', 'Acme Robotics')];
  await open(); await page.waitForTimeout(500);
  await markIs('turn1');
  check('a glow alone makes the turn mark: data-marks turn1, counted in data-counts', await marks() === 'turn1' && await counts() === 'turn1');
  check('the new glow jumps and rings once, as any arrival does', await rings() === '1');
  check('a glow raises no card and no cue', await card() === 0 && await page.locator('.notch-note.is-open').count() === 0 && await cues() === 0);
  const amber = await turnColour();
  check(`in 点线环 the mark is the amber point (${amber.join(',')})`, amber[3] > 0 && amber[0] > 200 && amber[2] < 190 && amber[0] > amber[1]);
  await page.waitForTimeout(3200);
  check('then it rests: the rings stop, the point stays', await rings() === '' && await marks() === 'turn1');
  check('arrival tells the daemon nothing: it serves a glow until it is seen', of('g-a').length === 0);
  await page.waitForTimeout(5500);
  check('another poll does not repeat the arrival or send anything', await rings() === '' && of('g-a').length === 0 && await card() === 0);
  await shot('glow-wing', { x: 400, y: 0, width: 120, height: 34 });
  // Resting on the wing opens the list, and it stays open: only a glow is in it.
  await toWing(); await drop.waitFor({ timeout: 4000 });
  await page.waitForTimeout(1800);
  const heads = await drop.locator('.a-h > span').allTextContents(), grow = drop.locator('.a-row.is-glow');
  check(`the list opens on a glow alone and stays open (${heads.join('|')})`, await drop.count() === 1 && heads.join('|') === 'Your turn1' && await grow.count() === 1);
  const gtext = (await grow.innerText()).replace(/\s+/g, ' ');
  check(`its row: the title, its one line and how long ago (${gtext})`, /Application update · Acme Robotics/.test(gtext) && /replied to your application \(ML Intern\): not this time\./.test(gtext) && /^[78]m ago$/.test(await grow.locator('.a-ago').innerText()));
  await shot('glow-panel', { x: 100, y: 0, width: 440, height: 160 });
  await onRow(grow);
  check('the ✕ is a Clear button on the row', await grow.locator('[role="button"][aria-label="Clear"]').count() === 1 && await grow.locator('[aria-label="Clear"]').getAttribute('title') === 'Clear');
  await grow.locator('[aria-label="Clear"]').click();
  await markIs('', 4000);
  check('the ✕ tells the daemon dismissed, once, and the mark goes', JSON.stringify(of('g-a')) === JSON.stringify(['{"action":"feedback","reaction":"dismissed"}']) && await marks() === '');
  await away();
  await page.waitForTimeout(6000);
  check('though the daemon still lists it, it does not come back', await marks() === '' && of('g-a').length === 1);
  // Opening a row goes where the summary's button goes (the Dashboard on the job list), tells `seen`, and the glow goes.
  notices = [glow('g-b', 'Orbit Labs')];
  await markIs('turn1');
  await toWing(); await drop.waitFor({ timeout: 4000 });
  await onRow(drop.locator('.a-row.is-glow'));
  await drop.locator('.a-row.is-glow').click();
  await page.waitForSelector('.ad .jp', { timeout: 5000 });
  check('a click on the row opens the Dashboard on the job list', await page.locator('.ad .jp').count() === 1);
  check('and tells the daemon seen, not dismissed', JSON.stringify(of('g-b')) === JSON.stringify(['{"action":"seen"}']));
  await markIs('', 4000);
  check('the glow went with it', await marks() === '' && await card() === 0);
  // After a restart the daemon still serves what was not seen: the mark is back, with no card and no cue, and nothing posted.
  notices = [glow('g-c', 'Helix')];
  await open(); await markIs('turn1');
  check('a restart brings a glow that was never seen back', await marks() === 'turn1' && await card() === 0 && await cues() === 0 && of('g-c').length === 0);
  // Two glows, one waiting session: the turn counts all three; sessions first, then glows newest first.
  notices = [glow('g-c', 'Helix', { at: at(40) }), glow('g-d', 'Northwind', { at: at(2) })];
  board = [session('t-1', 'needs_input')];
  await open(); await markIs('turn3', 10_000);
  check('sessions and glows count together in the turn mark', await marks() === 'turn3' && await counts() === 'turn3');
  await toWing(); await drop.waitFor({ timeout: 4000 }); await page.waitForTimeout(500);
  const rows = await drop.locator('.a-sec[data-sec="turn"] .a-row').evaluateAll(els => els.map(e => ({ glow: e.classList.contains('is-glow'), text: e.querySelector('b').textContent })));
  check(`Your turn lists the session first, then the glows (${rows.map(r => r.text).join(' | ')})`, rows.length === 3 && !rows[0].glow && rows[1].glow && rows[2].glow && /Northwind/.test(rows[1].text) && /Helix/.test(rows[2].text));
  await page.screenshot({ path: path.join(dir, 'glow-panel-three.png'), clip: { x: 100, y: 0, width: 440, height: 200 } });
  await away();
  // In the dot look a glow makes the amber point the lead even when the first member of the turn is a finished session.
  notices = []; board = [session('t-2', 'working')];
  await open(); await page.waitForTimeout(1800);
  board = [session('t-2', 'done')];
  await markIs('turn1', 10_000); await page.waitForTimeout(2800);
  const ring = await turnColour();
  check(`a finished session alone leads the turn mark in its own colour (${ring.join(',')})`, ring[3] > 0 && ring[1] > ring[0]);
  notices = [glow('g-b', 'Orbit Labs')];
  await markIs('turn2', 10_000); await page.waitForTimeout(500);
  const lead = await turnColour();
  check(`with a glow among them the lead is the amber point (${lead.join(',')})`, lead[3] > 0 && lead[0] > lead[1]);
  board = []; notices = [];
  // The holds: a glow is a mark, not a card. Quiet and no-pop keep cards back, never the mark; so does a call.
  for (const level of ['quiet', 'no-pop']) {
    // At no-pop the daemon's mail card is held back as ever; the glow beside it is not.
    quietLevel = level; notices = level === 'quiet' ? [glow('g-a', 'Acme Robotics')] : [glow('g-a', 'Acme Robotics'), mail('np-1', 'card')];
    await open(); await page.waitForSelector('.companion-quiet', { timeout: 4000 }); await markIs('turn1');
    await page.waitForTimeout(level === 'quiet' ? 300 : 2500);
    check(`quiet level ${level}: the glow shows${level === 'quiet' ? '' : ' while the card is held'}, no cue`, await marks() === 'turn1' && await card() === 0 && await cues() === 0);
  }
  quietLevel = 'off'; holdWord = 'call'; notices = [glow('g-a', 'Acme Robotics'), mail('hold-1', 'card')];
  await open(); await markIs('turn1');
  await page.waitForTimeout(2500);
  check('a call hold: the glow shows while the card waits', await marks() === 'turn1' && await card() === 0 && await cues() === 0);
  holdWord = undefined;
  await page.waitForFunction(() => document.querySelector('.notch-note.is-open .nc'), null, { timeout: 14_000 });
  check('the call ends: the held card comes up, the glow was there all along', await card() === 1 && await marks() === 'turn1');
  await page.keyboard.press('Escape'); await gone();
  holdWord = 'away'; notices = [glow('g-a', 'Acme Robotics')];
  await open(); await markIs('turn1');
  check('an away hold: the glow shows too', await marks() === 'turn1' && await card() === 0);
  holdWord = undefined;
  // dnd freezes the wing as it was: a glow that comes after it began is not drawn; one that was there stays, and nothing more joins it.
  notices = []; quietLevel = 'dnd';
  await open(); await page.waitForSelector('.companion-quiet', { timeout: 4000 });
  notices = [glow('g-a', 'Acme Robotics')];
  await page.waitForTimeout(6500);
  check('dnd: a glow that comes in leaves no mark', await marks() === '' && await card() === 0 && await cues() === 0);
  await emitQuiet('off');
  await markIs('turn1', 8000);
  check('dnd ends: the glow was waiting and the mark comes up', await marks() === 'turn1');
  await emitQuiet('dnd');
  await page.waitForTimeout(600);
  notices = [glow('g-a', 'Acme Robotics'), glow('g-c', 'Helix')];
  await page.waitForTimeout(6500);
  check('dnd freezes the mark as it was: the second glow is not counted yet', await marks() === 'turn1' && await counts() === 'turn1');
  await emitQuiet('off');
  await markIs('turn2', 8000);
  check('and when dnd ends the count catches up', await counts() === 'turn2');
  quietLevel = 'off'; holdWord = undefined; notices = [];
  await open(); await page.waitForTimeout(800);
  check('glow: no page errors', errors.length === 0);

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
