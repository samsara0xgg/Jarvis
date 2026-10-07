// The Dashboard's Memory page (ADR 0154) against a fake daemon: the home block, the kept notes with last night's entries and their
// actions, one note opened with its sources and the conversation they point to, editing in place (autofocus, Esc, Cmd+Enter), moving,
// deleting, undo, the debounced search, the day summaries with their edit, the changes with undo and the character limit.
// Real AroundDashboard through vite, the native bridge stubbed, every daemon route answered here. Screenshots land in /tmp/memory-ev/.

// a letter's body, focus, trash / archive / read with their undo, and Jarvis's reply draft (404 = no drafts, a draft, a rewrite that
// morphs in, hand edits that save, send -> confirmation card for the right thread -> accept, cancel, discard).
// Real AroundDashboard through vite, the native bridge stubbed, every daemon route answered here. Screenshots land in /tmp/mail-ev/.
import { chromium } from 'playwright';
import { createServer, transformWithOxc } from 'vite';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdirSync, writeFileSync } from 'node:fs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.MEMORY_EVIDENCE_DIR ?? '/tmp/memory-ev';
const port = Number(process.env.MEMORY_PORT ?? 5212);
const origin = 'http://127.0.0.1:61987';
const checks = [], errors = [], posts = [];
const check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log(`PASS ${name}`); };
const moduleId = '/@memory-page-fixture.tsx';
const fixture = `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { AroundDashboard } from '/src/AroundDashboard.tsx';
import '/src/style.css';
import '/src/companion.css';
import '/src/design-tokens.css';
const noop = () => {};
const ctl = { micMuted: false, speechMuted: false, handsFree: false, setMic: noop, setSpeech: noop, setHandsFree: noop,
  look: { skin: 'glass', auto: false, home: 'dark', homeFinish: 'original', marks: 'spark' }, setLook: noop, playFaces: noop,
  cues: { on: false, volume: 0 }, setCues: noop };
createRoot(document.getElementById('root')).render(<main className="companion"><div className="companion-dashboard is-open" style={{ visibility: 'visible', left: 140, top: 32 }}>
  <AroundDashboard open port="61987" onClose={noop} onMood={noop} onHop={noop} ctl={ctl}/></div></main>);
`;
const server = await createServer({ root, server: { host: '127.0.0.1', port, strictPort: true }, plugins: [{
  name: 'memory-page-acceptance',
  configureServer(server) {
    server.middlewares.use('/__memory', (_req, res) => { res.setHeader('Content-Type', 'text/html'); res.end(`<div id="root"></div><script type="module" src="${moduleId}"></script>`); });
  },
  resolveId(id) { if (id === moduleId) return `\0${moduleId}`; },
  load(id) { if (id === `\0${moduleId}`) return transformWithOxc(fixture, path.join(root, 'memory-page-fixture.tsx'), { jsx: { runtime: 'automatic' } }); },
}] });



const iso = (day, hm) => new Date(`${day}T${hm}:00`).toISOString();
const SECTIONS = ['关于你', '偏好', '常提到的人', '正在做的事', '定下来的规矩', '承诺和待办'];
const SRC = [
  { id: 'r1', who: 'user', ts: iso('2026-10-02', '18:22'), day: '2026-10-02', text: '这周六要去跟朋友吃饭,这周日要去看电影。' },
  { id: 'r2', who: 'jarvis', ts: iso('2026-10-02', '18:23'), day: '2026-10-02', text: '记下了：周六和朋友吃饭，周日看电影。' },
];
const fresh = () => ({
  items: [
    { id: 'a1', section: '关于你', text: '用户叫 Allen。', pinned: false, src: [] },
    { id: 'a2', section: '正在做的事', text: '报告下周三之前交。', pinned: false, src: [SRC[0]] },
    { id: 'a3', section: '承诺和待办', text: '周六和朋友吃饭，周日看电影。', pinned: false, src: SRC },
  ],
  hidden: new Set(), versions: [{ id: 'v0', ts: iso('2026-10-03', '05:00'), origin: 'nightly', kind: 'nightly', day: '2026-10-02', undoable: true, lines: [{ tag: 'add', text: '周六和朋友吃饭，周日看电影。' }] }],
  snaps: {}, cap: 4000, seq: 0,
});
let S = fresh();
const entries = () => [
  { kind: 'add', id: 'a3', section: '承诺和待办', text: S.items.find(i => i.id === 'a3')?.text ?? '', quote: { who: 'user', text: SRC[0].text } },
  { kind: 'rewrite', id: 'a2', section: '正在做的事', text: '报告下周三之前交。', before: '报告下周五交。', quote: null },
  { kind: 'stale', id: 'old1', section: '正在做的事', text: '周四前订好机票。', quote: { who: 'user', text: '机票订好了。' } },
].filter(e => !S.hidden.has(e.id) && (e.kind === 'stale' || S.items.some(i => i.id === e.id)));
const grouped = () => SECTIONS.map(name => ({ name, items: S.items.filter(i => i.section === name).map(({ id, text, pinned }) => ({ id, text, pinned })) }));
const overview = () => ({ version: { id: S.versions[0].id, ts: S.versions[0].ts, origin: S.versions[0].origin }, items: S.items.length, days: 3, chars: S.items.reduce((n, i) => n + i.text.length, 0), max_chars: S.cap, booted_max_chars: 4000,
  sections: grouped(), new: { day: '2026-10-02', ts: iso('2026-10-03', '05:00'), version: 'v0', entries: entries() } });
const versions = () => ({ total: S.versions.length, versions: S.versions.map((v, i) => ({ edited: 0, moved: 0, counts: { add: 0, chg: 0, old: 0 }, more: 0, chars: 100, ...v, current: i === 0 })) });
const DAYS = { '2026-10-02': { kind: 'model', records: 215, sections: { topics: ['回顾昨天的工作：实时语音和字幕。', '问了 Rust 学习资料。'], decisions: ['周六和朋友吃饭。'], unfinished: [] } },
  '2026-10-01': { kind: 'verbatim', records: 2, lines: ['今天测试一下麦克风。', '好的，测试通过。'] }, '2026-09-30': { kind: 'model', records: 40, sections: { topics: ['听故事。'], decisions: [], unfinished: ['整理邮件。'] } } };
const dayCards = () => ({ days: Object.entries(DAYS).map(([day, d]) => ({ day, ts: iso(day, '23:00'), kind: d.kind, records: d.records, lines: d.lines ?? d.sections.topics })) });
const push = (kind, extra = {}) => { const id = `v${++S.seq}`; S.snaps[id] = JSON.stringify(S.items); S.versions.unshift({ id, ts: new Date().toISOString(), origin: 'user', kind, day: '2026-10-02', undoable: true, lines: [], ...extra }); return id; };
const sent = p => posts.filter(x => x.path === `/inherent/memory${p}`);
const gets = p => gotten.filter(x => x.startsWith(`/inherent/memory${p}`));
const gotten = [];

let browser;
mkdirSync(dir, { recursive: true });
try {
  await server.listen();
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const context = await browser.newContext({ timezoneId: 'America/Vancouver', viewport: { width: 700, height: 900 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => { if (!localStorage.getItem('companion-settings-v1')) localStorage.setItem('companion-settings-v1', JSON.stringify({ lang: 'en' })); window.jarvis = { focus: async () => {}, usage: async () => null, material: () => {}, openUrl: async () => true }; });
  await page.route(`${origin}/**`, async route => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname, method = request.method();
    const reply = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*' }, body: JSON.stringify(data) });
    if (method === 'OPTIONS') return reply({});
    const body = method === 'POST' ? request.postDataJSON() ?? {} : undefined;
    if (method === 'POST') posts.push({ path: p, body }); else gotten.push(p + url.search);
    let m;
    if (!p.startsWith('/inherent/memory')) return reply({}, 404);
    const rest = p.slice('/inherent/memory'.length);
    if (rest === '' ) return reply(overview());
    if ((m = rest.match(/^\/item\/(\w+)$/)) && method === 'GET') {
      const i = S.items.find(x => x.id === m[1]); if (!i) return reply({ detail: 'gone' }, 404);
      return reply({ id: i.id, section: i.section, text: i.text, pinned: i.pinned, number: S.items.indexOf(i) + 1, chars: i.text.length, sources: i.src, born: { ts: iso('2026-10-03', '05:00'), origin: 'nightly', day: '2026-10-02' },
        edits: i.pinned ? [{ ts: new Date().toISOString(), origin: 'user', day: '2026-10-02' }] : [], edit_count: i.pinned ? 1 : 0, reminder: false });
    }
    if (rest === '/item/edit') { const i = S.items.find(x => x.id === body.id), v = push(body.section && body.section !== i.section ? 'user' : 'user', { edited: 1, lines: [{ tag: 'chg', text: body.text }] }); i.text = body.text; i.section = body.section ?? i.section; i.pinned = true; return reply({ version: v }); }
    if (rest === '/item/delete') { const v = push('user', { lines: [{ tag: 'old', text: 'deleted' }], counts: { add: 0, chg: 0, old: 1 } }); S.items = S.items.filter(x => x.id !== body.id); return reply({ version: v }); }
    if (rest === '/item/confirm') { const v = push('confirm'); S.hidden.add(body.id); return reply({ version: v }); }
    if (rest === '/item/keep') { const v = push('user'); S.hidden.add(body.id); return reply({ version: v }); }
    if (rest === '/undo') { const snap = S.snaps[body.version]; if (!snap) return reply({ detail: 'no' }, 409); S.items = JSON.parse(snap); S.hidden = new Set(); S.versions.unshift({ id: `v${++S.seq}`, ts: new Date().toISOString(), origin: 'user', kind: 'undo', day: null, undoable: true, lines: [] }); return reply({ version: 'v-undo' }); }
    if (rest === '/versions') return reply(versions());
    if (rest === '/cap') { if (body.max_chars < 1000 || body.max_chars > 20000) return reply({ detail: 'range' }, 400); S.cap = body.max_chars; return reply({ max_chars: S.cap, booted_max_chars: 4000 }); }
    if (rest === '/search') {
      const q = url.searchParams.get('q'), who = url.searchParams.get('who'), hits = [{ id: 'r1', ts: SRC[0].ts, who: 'user', text: SRC[0].text }, { id: 'r2', ts: SRC[1].ts, who: 'jarvis', text: SRC[1].text }].filter(h => h.text.includes(q) && (who === 'all' || (who === 'user') === (h.who === 'user')));
      return reply({ words: [q], days: hits.length ? [{ day: '2026-10-02', hits }] : [], total: hits.length, truncated: false, since: iso('2026-09-01', '00:00') });
    }
    if (rest === '/days') return reply(dayCards());
    if (rest === '/day/edit') { DAYS[body.day] = { ...DAYS[body.day], kind: 'user', sections: Object.fromEntries(Object.entries(body.sections).map(([k, v]) => [k, v.filter(Boolean)])) }; return reply({ ok: true }); }
    if ((m = rest.match(/^\/day\/([\d-]+)\/records$/))) return reply({ day: m[1], total: 3, offset: 0, records: [{ id: 'r0', ts: iso(m[1], '18:21'), who: 'user', text: '嗨。' }, ...SRC] });
    if ((m = rest.match(/^\/day\/([\d-]+)$/))) { const d = DAYS[m[1]]; return reply({ day: m[1], ts: iso(m[1], '23:00'), ...d }); }
    return reply({}, 404);
  });
  const shot = name => page.locator('.companion-dashboard').screenshot({ path: path.join(dir, `${name}.png`) });
  const count = sel => page.locator(sel).count();
  const texts = sel => page.locator(sel).allTextContents();
  const strip = async () => (await page.locator('.ad .toast').textContent()) ?? '';
  const settle = ms => page.waitForTimeout(ms ?? 700);
  const back = async () => { await page.locator('.pg-back').click(); await settle(); };
  await page.goto(`http://127.0.0.1:${port}/__memory`);

  // Home block and the page
  await page.waitForSelector('.ad [data-block="memory"]', { timeout: 8000 }); await settle(900);
  check('the home shows a Memory block with the count and the new ones', (await page.locator('.ad [data-block="memory"] .meta').textContent()).includes('3 kept') && (await page.locator('.ad [data-block="memory"] .meta').textContent()).includes('2 new'));
  await shot('00-home');
  await page.locator('.ad [data-block="memory"] .fill').click(); await settle(900);
  check('it opens the page titled Memory with the section headings and a count each', (await page.locator('.pg-head h3').textContent()) === 'Memory' && (await texts('.mem-sh h4')).filter(x => /· \d/.test(x)).length === 7 && (await count('.mem-row')) === 3);
  check('last night’s entries carry New / Edited / Stale tags, the quote and the right actions', (await texts('.mem-ent .mem-tag')).join('|') === 'New|Edited|Stale'
    && (await texts('.mem-q')).some(x => x.includes('You said')) && (await texts('.mem-ent[data-kind="stale"] .mem-act')).join('|') === 'Right|Keep it' && (await texts('.mem-ent[data-kind="add"] .mem-act')).join('|') === 'Right|Edit|Delete');
  check('a bare day shows as that day in any time zone (TZ=America/Vancouver): the night of day 2026-10-02 reads 10/2, not 10/1', (await texts('.mem-sh small')).includes('from 10/2'));
  check('a section with nothing says so', (await texts('.mem-empty')).includes('Nothing yet.'));
  await shot('01-kept');

  // 对 on an entry: it folds away, posts, and the strip undoes it
  await page.locator('.mem-ent[data-kind="add"] .mem-act.is-ok').click(); await settle(500);
  check('Right posts /item/confirm, the entry folds out and the strip offers Undo', JSON.stringify(sent('/item/confirm').at(-1)?.body) === '{"id":"a3"}' && (await page.locator('.mem-ent[data-kind="add"]').count()) === 0 && (await strip()).includes('Marked right'));
  await page.locator('.ad .toast button').click(); await settle(500);
  check('Undo posts /undo for that version and the entry is back', sent('/undo').length === 1 && (await page.locator('.mem-ent[data-kind="add"]').count()) === 1);

  // One note: sources, the conversation, going back
  await page.locator('.mem-ent-t.is-link').first().click(); await settle(900);
  check('a note opens large with where it came from, who said it and when', (await count('.mem-src')) === 2 && (await texts('.mem-src-w b')).join('|') === 'You said|Jarvis said' && (await texts('.mem-big-t')).join('').includes('周六和朋友吃饭'));
  check('and its timeline says which day it was noted from, as that day', (await texts('.mem-tl span')).join('').includes('Noted from 10/2'));
  await shot('02-item');
  await page.locator('.mem-src').first().click(); await settle(900);
  check('a source opens that day’s conversation and marks the line', (await count('.mem-line')) === 3 && (await page.locator('.mem-line[data-id="r1"]').count()) === 1);
  await shot('04-convo');
  await back(); check('back from the conversation returns to the note', (await count('.mem-big-t')) === 1);

  // Edit: autofocus, Esc cancels without a write, Cmd+Enter saves and pins
  await page.locator('.mem-bar3 .mem-act').first().click(); await settle(700);
  check('Edit focuses the field, with the original shown and the pin note', await page.evaluate(() => document.activeElement?.id === 'mem-text') && (await count('.mem-lock')) === 1 && (await texts('.mem-was.is-block')).join('').includes('周六和朋友吃饭'));
  await shot('03-edit');
  await page.keyboard.press('Escape'); await settle(600);
  check('Esc leaves the edit with nothing written', (await count('.mem-big-t')) === 1 && sent('/item/edit').length === 0);
  await page.locator('.mem-bar3 .mem-act').first().click(); await settle(600);
  await page.locator('#mem-text').fill(''); check('Save is off for an empty note', await page.locator('.mem-foot .is-ok').isDisabled());
  await page.locator('#mem-text').fill('周六和朋友吃饭，周日看电影，周一交报告。'); await page.keyboard.press('Meta+Enter'); await settle(600);
  check('Cmd+Enter posts /item/edit with the id, text and section, then shows the note pinned', JSON.stringify(sent('/item/edit').at(-1)?.body) === JSON.stringify({ id: 'a3', text: '周六和朋友吃饭，周日看电影，周一交报告。', section: '承诺和待办' })
    && (await strip()).includes('Saved') && (await count('.mem-where .mem-tag')) === 1);
  await settle(1200); check('and the new text has morphed in', (await texts('.mem-big-t')).join('').includes('周一交报告'));
  await shot('05-saved');

  // Move to another section, then delete
  await page.locator('.mem-bar3 .mem-act').nth(1).click(); await settle(400);
  await page.locator('.mem-secpick .mem-chip', { hasText: 'Rules' }).click(); await settle(700);
  check('Move posts the same text with the new section', sent('/item/edit').at(-1)?.body.section === '定下来的规矩' && (await page.locator('.mem-where').textContent()).includes('Rules'));
  await page.locator('.mem-bar3 .mem-act.is-del').click(); await settle(900);
  check('Delete posts /item/delete, returns to the list and the strip offers Undo', JSON.stringify(sent('/item/delete').at(-1)?.body) === '{"id":"a3"}' && (await count('.mem-row')) === 2 && (await strip()).includes('Deleted'));
  await page.locator('.ad .toast button').click(); await settle(600);
  check('Undo brings it back', (await count('.mem-row')) === 3 && sent('/undo').length === 2);

  // Search: debounced, highlighted, filtered, Esc clears
  await page.locator('.mem-search input').click(); await page.keyboard.type('朋友', { delay: 40 }); await settle(700);
  check('typing searches once after the pause', gets('/search').length === 1 && gets('/search')[0].includes('who=all') && (await count('.mem-hit')) === 2 && (await count('.mem mark')) === 2);
  await shot('06-search');
  await page.locator('.mem-chip', { hasText: 'You' }).click(); await settle(500);
  check('the You filter asks for who=user', gets('/search').at(-1).includes('who=user') && (await count('.mem-hit')) === 1);
  await page.keyboard.press('Escape'); await settle(500);
  check('Esc clears the search and the list returns', (await count('.mem-search input:not(:placeholder-shown)')) === 0 && (await count('.mem-row')) === 3);
  await page.locator('.mem-search input').fill('xyzzy'); await settle(700);
  check('a search with no hits says so', (await texts('.mem-results .mem-empty')).join('').includes('Nothing says'));
  await page.locator('.mem-clear').click(); await settle(500);

  // Back from a row's note puts the focus on that row again
  await page.locator('.mem-row[data-id="a1"]').click(); await settle(700); await back();
  check('going back focuses the row that was opened', await page.evaluate(() => document.activeElement?.getAttribute('data-id') === 'a1'));

  // Days
  await page.locator('.mem-chip', { hasText: 'Days' }).click(); await settle(600);
  check('the days list newest first, a verbatim day shows its lines', (await count('.mem-card')) === 3 && (await texts('.mem-card-top time'))[0].startsWith('10/2') && (await texts('.mem-card-s'))[1].includes('“今天测试一下麦克风。”'));
  await page.locator('.mem-card-h').first().click(); await settle(800);
  check('a day opens to its three headings', (await texts('.mem-dsec h5')).join('|') === 'What we talked about|What you decided|Not finished');
  await page.locator('.mem-dfoot .mem-act').first().click(); await settle(500);
  const area = page.locator('.mem-dsec .mem-ed').first();
  await area.fill('回顾昨天的工作：实时语音和字幕。\n问了 Rust 学习资料。\n新加的一行。'); await page.keyboard.press('Meta+Enter'); await settle(700);
  check('Cmd+Enter posts /day/edit with one line per bullet', JSON.stringify(sent('/day/edit').at(-1)?.body.sections.topics) === JSON.stringify(['回顾昨天的工作：实时语音和字幕。', '问了 Rust 学习资料。', '新加的一行。']) && (await strip()).includes('Saved'));
  await shot('07-days');
  await page.locator('.mem-card-h').nth(1).click(); await settle(800);
  check('a verbatim day has no Edit, only its words', (await texts('.mem-card:nth-child(2) .mem-dfoot .mem-act')).join('|') === 'That day, word for word ›');
  await page.locator('.mem-card:nth-child(2) .mem-dfoot .mem-act').click(); await settle(800);
  check('That day, word for word opens the conversation', (await count('.mem-line')) === 3); await back();

  // Changes and the limit
  await page.locator('.mem-chip', { hasText: 'Changes' }).click(); await settle(800);
  check('every version is a row, the newest marked Now', (await count('.mem-ver')) >= 4 && (await texts('.mem-ver')).at(0).includes('Now') && (await texts('.mem-ver .mem-ver-t'))[0].length > 0);
  check('the night pass is titled with its own day', (await texts('.mem-ver .mem-ver-t')).some(x => x.includes('Night pass · 10/2')));
  await shot('08-changes');
  const undos = await count('.mem-ver > .mem-act');
  check('versions that can be taken back have an Undo, named for screen readers', undos >= 2 && (await page.locator('.mem-ver > .mem-act').first().getAttribute('aria-label')).startsWith('Undo: '));
  await page.locator('.mem-cap').click(); await settle(400);
  check('tapping the limit opens a focused field', await page.evaluate(() => document.activeElement?.getAttribute('aria-label') === 'Character limit'));
  await page.locator('.mem-cap-ed input').fill('500'); check('under 1,000 cannot be saved', await page.locator('.mem-cap-ed .mem-act').isDisabled());
  await page.locator('.mem-cap-ed input').fill('6000'); await page.keyboard.press('Enter'); await settle(700);
  check('6000 posts /cap and the strip says it applies after a restart', JSON.stringify(sent('/cap').at(-1)?.body) === '{"max_chars":6000}' && (await strip()).includes('restart'));
  await shot('09-limit');

  // Reduced motion
  await page.locator('.mem-chip', { hasText: 'Kept' }).click(); await page.emulateMedia({ reducedMotion: 'reduce' }); await settle(300);
  check('reduced motion turns the fold transition off', await page.evaluate(() => getComputedStyle(document.querySelector('.mem-fold')).transitionDuration) === '0s');

  // Chinese
  await page.evaluate(() => localStorage.setItem('companion-settings-v1', JSON.stringify({ lang: 'zh' }))); S = fresh();
  await page.reload(); await page.locator('.ad [data-block="memory"] .fill').click(); await settle(900);
  check('in Chinese the chips and tags read 记着的 / 日摘要 / 改动 and 新 / 改 / 过时', (await texts('.mem-chip')).join('|').startsWith('记着的') && (await texts('.mem-ent .mem-tag')).join('|') === '新|改|过时' && (await page.locator('.mem-search input').getAttribute('placeholder')) === '搜你说过的每一句');
  await shot('10-zh');
  check('no unhandled renderer errors', errors.length === 0);
  writeFileSync(path.join(dir, 'checks.json'), JSON.stringify({ checks, errors, posts }, null, 2));
  console.log(`Memory page: ${checks.length} checks passed`);
} catch (error) {
  writeFileSync(path.join(dir, 'failure.json'), JSON.stringify({ checks, errors, posts, error: String(error) }, null, 2));
  throw error;
} finally { await browser?.close(); await server.close(); }
