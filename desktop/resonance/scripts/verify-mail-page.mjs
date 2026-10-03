// The Dashboard's Mail page against a fake daemon: entry from the home block and from a row, the ranked list with its tags and filters,
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
const dir = process.env.MAIL_EVIDENCE_DIR ?? '/tmp/mail-ev';
const port = Number(process.env.MAIL_PORT ?? 5209);
const origin = 'http://127.0.0.1:61987';
const checks = [], errors = [], posts = [];
const check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log(`PASS ${name}`); };
const moduleId = '/@mail-page-fixture.tsx';
const fixture = `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { AroundDashboard } from '/src/AroundDashboard.tsx';
import '/src/style.css';
import '/src/companion.css';
const noop = () => {};
const ctl = { micMuted: false, speechMuted: false, handsFree: false, setMic: noop, setSpeech: noop, setHandsFree: noop,
  look: { skin: 'glass', auto: false, home: 'dark', homeFinish: 'original', marks: 'spark' }, setLook: noop, playFaces: noop,
  cues: { on: false, volume: 0 }, setCues: noop };
createRoot(document.getElementById('root')).render(<main className="companion"><div className="companion-dashboard is-open" style={{ visibility: 'visible', left: 140, top: 32 }}>
  <AroundDashboard open port="61987" onClose={noop} onMood={noop} onHop={noop} ctl={ctl}/></div></main>);
`;
const server = await createServer({ root, server: { host: '127.0.0.1', port, strictPort: true }, plugins: [{
  name: 'mail-page-acceptance',
  configureServer(server) {
    server.middlewares.use('/__mail', (_req, res) => { res.setHeader('Content-Type', 'text/html'); res.end(`<div id="root"></div><script type="module" src="${moduleId}"></script>`); });
  },
  resolveId(id) { if (id === moduleId) return `\0${moduleId}`; },
  load(id) { if (id === `\0${moduleId}`) return transformWithOxc(fixture, path.join(root, 'mail-page-fixture.tsx'), { jsx: { runtime: 'automatic' } }); },
}] });

// The fake daemon: five unread letters, drafts per letter id, one confirmation slot.
const today = new Date(); today.setHours(0, 1, 0, 0);
const daysAgo = new Date(Date.now() - 2 * 86_400_000); daysAgo.setHours(12, 0, 0, 0);
const MAIL = [
  { id: 'm1', from: 'Prof. Lee', address: 'lee@uni.example', thread_id: 't1', subject: 'Office hours moved', received: today.toISOString(), reply: 'fyi', importance: 2.8 },
  { id: 'm2', from: 'Mom', address: 'mom@home.example', thread_id: 't2', subject: 'Still on for tonight?', received: today.toISOString(), reply: 'yes', importance: 1.7 },
  { id: 'm3', from: 'Northwind Recruiting', address: 'jobs@northwind.example', thread_id: 't3', subject: 'Interview slots', received: daysAgo.toISOString(), reply: 'yes', importance: .6, category: 'job_search' },
  { id: 'm4', from: 'Shop Deals', address: 'hi@shop.example', thread_id: 't4', subject: '50% off everything', received: today.toISOString(), reply: 'fyi', junk: true, importance: .2 },
  { id: 'm5', from: 'Weekly digest', address: 'news@digest.example', thread_id: 't5', subject: 'This week in tech', received: new Date().toISOString() },
];
const TEXT = { m1: 'Hi all,\n\nOffice hours move to Thursday.\nQuestions on A3: https://courses.example/csc370/a3.\n\nProf. Lee', m2: 'Dinner tonight?\n\nMom', m3: 'Please click here\n<https://jobs.example/listing?id=7\n>  to see the slots.  \n\n\n\nNorthwind\n\nOn Mon, Oct 1, 2026 at 9:00 AM Allen wrote:\n> Any slots?' };
const DRAFT1 = 'Hi Prof. Lee,\n\nThursday works for me. I will bring my questions about A3.\n\nAllen';
const DRAFT2 = 'Hi Prof. Lee,\n\nThanks for the update. Thursday at 3 suits me fine. I will bring my questions about A3 and a printed copy of the plan.\n\nBest,\nAllen';
const daemon = { unread: new Set(MAIL.map(m => m.id)), drafts: {}, off: true, card: null };
const card = (id, threadId, args = {}) => ({ id, tool: 'gmail_send', action: 'Send email', source: 'gmail', letter: true, args: { threadId, to: ['lee@uni.example'], subject: 'Re: Office hours moved', body: 'Draft as it will go.', ...args } });
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const sent = (p) => posts.filter(x => x.path === p);

let browser;
mkdirSync(dir, { recursive: true });
try {
  await server.listen();
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const page = await (await browser.newContext({ viewport: { width: 700, height: 900 }, deviceScaleFactor: 2 })).newPage();
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.__opened = []; window.__gmail = [];
    window.jarvis = { focus: async () => {}, usage: async () => null, material: () => {}, openUrl: async url => { window.__opened.push(url); return true; }, openMail: async id => { window.__gmail.push(id); return true; } };
  });
  await page.route(`${origin}/**`, async route => {
    const request = route.request(), p = new URL(request.url()).pathname, method = request.method();
    const reply = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*' }, body: JSON.stringify(data) });
    const body = method === 'POST' ? request.postDataJSON() ?? {} : undefined;
    if (method === 'POST' && p !== '/inherent/projects/refresh') posts.push({ path: p, body });
    let m;
    if (method === 'OPTIONS') return reply({});
    if (p === '/inherent/mail') return reply({ unread: MAIL.filter(x => daemon.unread.has(x.id)) });
    if ((m = p.match(/^\/inherent\/mail\/(trash|read|archive|untrash|unread|unarchive)$/))) {
      for (const id of body.ids) (/^un/.test(m[1]) ? daemon.unread.add(id) : daemon.unread.delete(id));
      return reply({ ok: true });
    }
    if ((m = p.match(/^\/inherent\/mail\/([^/]+)\/draft$/))) {
      if (daemon.off) return reply({}, 404);
      if (method === 'GET') return reply({ draft: daemon.drafts[m[1]] ?? null });
      const old = daemon.drafts[m[1]];
      daemon.drafts[m[1]] = { ...old, subject: body.subject, body: body.body, revision: old.revision + 1, by: 'owner' };
      return reply({ draft: daemon.drafts[m[1]] });
    }
    if ((m = p.match(/^\/inherent\/mail\/([^/]+)\/draft\/(send|discard)$/))) {
      if (m[2] === 'discard') delete daemon.drafts[m[1]]; else daemon.card = card('c1', 't-other');
      return reply({ ok: true });
    }
    if ((m = p.match(/^\/inherent\/mail\/([^/]+)$/))) {
      if (!TEXT[m[1]]) return reply({}, 404);
      if (m[1] === 'm3') await wait(700);
      const x = MAIL.find(l => l.id === m[1]);
      return reply({ id: x.id, thread_id: x.thread_id, from: x.from, address: x.address, to: 'me@example.com', subject: x.subject, received: x.received, text: TEXT[x.id] });
    }
    if (p === '/inherent/confirmation') { if (method === 'POST') daemon.card = null; return reply(method === 'GET' ? { card: daemon.card } : { ok: true }); }
    if (p === '/inherent/setup') return reply({ keys: { openai: 'missing', minimax: 'missing', tavily: 'missing' }, voice_models: { state: 'ready' } });
    if (p.endsWith('sessions')) return reply({ sessions: [] });
    return reply(p === '/inherent/settings' ? { values: {}, options: {} } : {});
  });

  const shot = name => page.locator('.companion-dashboard').screenshot({ path: path.join(dir, `${name}.png`) });
  const until = (fn, arg, timeout = 6000) => page.waitForFunction(fn, arg, { timeout }).then(() => true, () => false);
  const count = sel => page.locator(sel).count();
  const texts = sel => page.locator(sel).allTextContents();
  const lastFocus = () => sent('/inherent/focus').at(-1)?.body;
  const goBack = async () => { await page.locator('.pg-back').click(); await page.waitForTimeout(450); };
  await page.goto(`http://127.0.0.1:${port}/__mail`);
  await page.locator('.ad [data-block="mail"] .ml').first().waitFor();
  await page.waitForTimeout(600);

  // Entry. The head of the home block opens the page; a row in it opens the page on that letter.
  check('the home block still shows the two letters Jev ranked first', (await texts('.ad [data-block="mail"] .ml b')).join(',') === 'Prof. Lee,Mom');
  await page.locator('.ad [data-block="mail"] .head').click(); await page.waitForTimeout(700);
  check('clicking the Mail block header opens the Mail page on its list', await page.locator('.ad[data-page="mail"]').count() === 1 && await count('.mp-list') === 1 && await count('.mp-det') === 0);
  check('the page carries the Mail title and the unread count', await page.locator('.pg-head h3').textContent() === 'Mail' && (await page.locator('.pg-head .meta').textContent()).includes('5 unread'));

  // The list: the home's order, tags, times, counts.
  check('the list is sorted like the home: non-junk first, importance desc, unrated, junk last',
    (await texts('.mp-row b')).join(',') === 'Prof. Lee,Mom,Northwind Recruiting,Weekly digest,Shop Deals');
  const tagsOf = id => texts(`.mp-row[data-id="${id}"] .mp-tag`);
  check('a letter shows the Jev mark and its importance level', (await tagsOf('m1')).join('|') === 'FYI|Urgent' && (await tagsOf('m2')).join('|') === 'Reply|Important');
  check('a Normal letter and an unrated letter: Normal is faint, no level tag when unrated, junk shows its mark', (await tagsOf('m3')).join('|') === 'Reply|Normal'
    && (await tagsOf('m5')).length === 0 && (await tagsOf('m4')).join('|') === 'Junk|Low');
  check('the faint levels are drawn fainter than Important', await page.evaluate(() => {
    const c = sel => getComputedStyle(document.querySelector(sel)).color;
    return c('.mp-row[data-id="m3"] .mp-tag:last-child') === c('.mp-row[data-id="m4"] .mp-tag:last-child') && c('.mp-row[data-id="m3"] .mp-tag:last-child') !== c('.mp-row[data-id="m2"] .mp-tag:last-child');
  }));
  check('the time is HH:MM today and M/D otherwise', (await texts('.mp-row[data-id="m1"] time'))[0] === '00:01'
    && /^\d{1,2}\/\d{1,2}$/.test((await texts('.mp-row[data-id="m3"] time'))[0]));
  check('subject is one line with an ellipsis', await page.locator('.mp-row[data-id="m1"] .mp-sub').evaluate(el => getComputedStyle(el).textOverflow === 'ellipsis' && getComputedStyle(el).overflow === 'hidden' && getComputedStyle(el).whiteSpace === 'nowrap'));
  const chips = () => texts('.mp-chip');
  check('filter chips carry their counts', (await chips()).join('|') === 'All5|Reply2|Job search1');
  await page.locator('.mp-chip[data-filter="yes"]').click();
  check('Reply keeps the letters Jev marked Reply', (await texts('.mp-row b')).join(',') === 'Mom,Northwind Recruiting');
  await page.locator('.mp-chip[data-filter="job"]').click();
  check('Job search keeps the job_search category', (await texts('.mp-row b')).join(',') === 'Northwind Recruiting');
  await page.locator('.mp-chip[data-filter="all"]').click();
  await shot('1-list');
  await goBack();
  check('Back from the list returns to the home', await count('.ad:not([data-page])') === 1);
  await page.locator('.ad [data-block="mail"] .ml').first().click(); await page.waitForTimeout(800);
  check('clicking a letter row on the home opens the page on that letter, not Gmail', await count('.mp-det') === 1
    && await page.locator('.mp-subject').textContent() === 'Office hours moved' && await page.evaluate(() => window.__gmail.length === 0));

  // The letter.
  await until(() => document.querySelector('.mp-text'));
  check('the header shows the sender, address and time', (await page.locator('.mp-who').textContent()).includes('Prof. Lee') && (await page.locator('.mp-who').textContent()).includes('<lee@uni.example>'));
  check('the body shows, line breaks kept', (await page.locator('.mp-text').textContent()).includes('Office hours move to Thursday.\nQuestions on A3:')
    && await page.locator('.mp-text').evaluate(el => getComputedStyle(el).whiteSpace === 'pre-wrap'));
  check('a URL in the body is a link that opens through the shell, without its trailing period', await count('.mp-text a.lk') === 1
    && (await page.locator('.mp-text a.lk').click(), await page.evaluate(() => window.__opened.join())) === 'https://courses.example/csc370/a3');
  check('opening a letter does not mark it read', sent('/inherent/mail/read').length === 0);
  check('opening it tells the daemon which letter is open', JSON.stringify(sent('/inherent/focus')[0]?.body) === JSON.stringify({ kind: 'mail', id: 'm1', thread_id: 't1', sender: 'Prof. Lee', subject: 'Office hours moved' }));
  check('no drafts on the daemon (404): no draft area', await count('.mp-draft') === 0);
  await page.locator('[data-act="draft"]').click(); await page.waitForTimeout(1500);
  check('让 Jarvis 起草回复 posts the typed turn, and with no draft route the area stays away', sent('/inherent/submit').at(-1)?.body.text === '给这封邮件起草一封回复' && await count('.mp-draft') === 0);
  await page.locator('[data-act="gmail"]').click();
  check('在 Gmail 打开 hands the letter id to the shell', await page.evaluate(() => window.__gmail.join()) === 'm1');
  await shot('2-letter');

  // The draft: appears for the open letter, a rewrite morphs in, hand edits save, nothing from a poll overwrites them.
  daemon.off = false;
  const body = () => page.locator('.mp-draft textarea.mp-bodytext').inputValue();
  daemon.drafts.m1 = { revision: 1, to: 'lee@uni.example', subject: 'Re: Office hours moved', body: DRAFT1, by: 'jarvis' };
  await page.locator('.mp-draft').waitFor({ timeout: 4000 });
  check('the draft shows a read-only To line, an editable subject and body', (await page.locator('.mp-to').textContent()).includes('lee@uni.example')
    && await page.locator('.mp-subj').inputValue() === 'Re: Office hours moved' && await body() === DRAFT1);
  check('a first draft appears without a morph', await count('.mp-draft .mt') === 0);
  daemon.drafts.m1 = { ...daemon.drafts.m1, revision: 2, body: DRAFT2, by: 'jarvis' };
  const morphing = await page.waitForSelector('.mp-draft .mt .mt-add', { timeout: 3000 }).then(() => true, () => false);
  const mid = await page.evaluate(() => ({ adds: document.querySelectorAll('.mp-draft .mt-add').length, dels: document.querySelectorAll('.mp-draft .mt-del').length, area: document.querySelectorAll('.mp-draft textarea.mp-bodytext').length }));
  await shot('3-draft-mid-morph');
  check('a revision by Jarvis runs the morph: inserted and removed word spans exist mid-transition, no textarea', morphing && mid.adds > 5 && mid.dels > 0 && mid.area === 0);
  check('the morph ends as an editable textarea with the new text', await until(() => !document.querySelector('.mp-draft .mt') && document.querySelector('.mp-draft textarea.mp-bodytext'), null, 5000) && await body() === DRAFT2);
  await page.locator('.mp-draft textarea.mp-bodytext').fill(`${DRAFT2}\nPS thanks`);
  await page.waitForTimeout(1000);
  const saves = sent('/inherent/mail/m1/draft');
  check('hand edits save to the daemon after about 0.6 s, as { subject, body }', saves.length === 1 && saves[0].body.body.endsWith('PS thanks') && saves[0].body.subject === 'Re: Office hours moved');
  await page.waitForTimeout(2200);
  check('polls echoing his own revision do not overwrite or morph his text', (await body()).endsWith('PS thanks') && await count('.mp-draft .mt') === 0);

  // Sending: the confirmation for another thread is not this letter's; the right thread's card is the confirm step.
  await page.locator('[data-act="send"]').click(); await page.waitForTimeout(2600);
  check('发送 posts { subject, body } to draft/send', sent('/inherent/mail/m1/draft/send').length === 1 && sent('/inherent/mail/m1/draft/send')[0].body.body.endsWith('PS thanks'));
  check('a confirmation card for another thread does not turn the draft into the confirm step', await count('.mp-confirm') === 0 && (await page.locator('.mp-draft').textContent()).includes('ready to send'));
  daemon.card = card('c2', 't1', { subject: 'Re: Office hours moved', body: 'Final draft as the card holds it.' });
  await page.locator('.mp-confirm').waitFor({ timeout: 4000 });
  check('a gmail_send card for this thread becomes the confirm step with To and editable subject and body',
    (await page.locator('.mp-confirm .mp-to').textContent()).includes('lee@uni.example') && await page.locator('.mp-confirm .mp-subj').inputValue() === 'Re: Office hours moved'
    && await page.locator('.mp-confirm textarea').inputValue() === 'Final draft as the card holds it.');
  await shot('4-confirm');
  await page.locator('.mp-confirm textarea').fill('Final words.');
  await page.locator('[data-act="confirm"]').click(); await page.waitForTimeout(500);
  const accept = sent('/inherent/confirmation').at(-1)?.body;
  check('确认发送 posts accept with { subject, body } edits', accept?.confirmation_id === 'c2' && accept.decision === 'accept' && accept.edits.subject === 'Re: Office hours moved' && accept.edits.body === 'Final words.');
  check('after accept the draft is gone and 已发送 shows', await count('.mp-draft') === 0 && await page.locator('.mp-sent').textContent() === 'Sent.');
  await goBack();
  check('Back from the letter tells the daemon nothing is open', lastFocus()?.kind === null && await count('.mp-list') === 1);

  // Cancel and discard on another letter; the draft there is Jarvis's from before the letter was opened.
  daemon.drafts.m2 = { revision: 1, to: 'mom@home.example', subject: 'Re: Still on for tonight?', body: 'Yes, see you at seven.', by: 'jarvis' };
  await page.locator('.mp-row[data-id="m2"]').click(); await page.waitForTimeout(800);
  await page.locator('.mp-draft').waitFor({ timeout: 4000 });
  check('a draft already on the daemon shows when its letter opens, and it is that letter’s', await page.locator('.mp-subj').inputValue() === 'Re: Still on for tonight?' && await body() === 'Yes, see you at seven.');
  check('the focus carries the second letter', lastFocus()?.id === 'm2' && lastFocus().thread_id === 't2');
  await page.locator('[data-act="send"]').click(); await page.waitForTimeout(1200);
  daemon.card = card('c3', 't2', { to: ['mom@home.example'] });
  await page.locator('.mp-confirm').waitFor({ timeout: 4000 });
  await page.locator('[data-act="cancel"]').click(); await page.waitForTimeout(500);
  check('取消 posts reject and returns to the draft', sent('/inherent/confirmation').at(-1)?.body.decision === 'reject' && sent('/inherent/confirmation').at(-1).body.confirmation_id === 'c3'
    && await count('.mp-confirm') === 0 && await count('.mp-draft textarea.mp-bodytext') === 1);
  await page.locator('[data-act="discard"]').click(); await page.waitForTimeout(500);
  check('不要了 posts draft/discard and clears the area', sent('/inherent/mail/m2/draft/discard').length === 1 && await count('.mp-draft') === 0);
  await goBack();

  // Read, trash and archive: at once, back to the list, an undo strip; the undo calls the opposite route.
  const strip = () => page.locator('.ad .toast.is-on').textContent();
  await page.locator('.mp-row[data-id="m3"]').click();
  check('while a body loads the page says so', await page.locator('.mp-body .muted').textContent() === 'Loading…');
  await until(() => document.querySelector('.mp-text'));
  check('mail as it comes reads tidy: the <url> sits in the sentence as a short link, blank runs close up, the quoted letter folds away',
    await page.locator('.mp-text').first().textContent() === 'Please click here jobs.example/listing?id=7 to see the slots.\n\nNorthwind' && await count('.mp-quote') === 0
    && (await page.locator('.mp-quoted').click(), await page.locator('.mp-quote').textContent()).startsWith('On Mon, Oct 1'));
  await page.locator('[data-act="read"]').click(); await page.waitForTimeout(600);
  check('标为已读 posts /read, returns to the list, removes the letter and shows an undo strip',
    JSON.stringify(sent('/inherent/mail/read').at(-1)?.body) === '{"ids":["m3"]}' && await count('.mp-list') === 1 && await count('.mp-row[data-id="m3"]') === 0 && (await strip()).includes('Marked as read'));
  await page.locator('.ad .toast button').click(); await page.waitForTimeout(500);
  check('Undo posts /unread and the letter is back', JSON.stringify(sent('/inherent/mail/unread').at(-1)?.body) === '{"ids":["m3"]}' && await count('.mp-row[data-id="m3"]') === 1);
  await page.locator('.mp-row[data-id="m5"]').click(); await page.waitForTimeout(500);
  check('a letter whose body 404s says to read it in Gmail', (await page.locator('.mp-body .muted').textContent()).includes('open it in Gmail'));
  await page.locator('[data-act="trash"]').click(); await page.waitForTimeout(600);
  check('Trash posts /trash, returns to the list, removes the letter and shows an undo strip',
    JSON.stringify(sent('/inherent/mail/trash').at(-1)?.body) === '{"ids":["m5"]}' && await count('.mp-list') === 1 && await count('.mp-row[data-id="m5"]') === 0 && (await strip()).includes('Trash'));
  await page.locator('.ad .toast button').click(); await page.waitForTimeout(500);
  check('Undo posts /untrash', JSON.stringify(sent('/inherent/mail/untrash').at(-1)?.body) === '{"ids":["m5"]}' && await count('.mp-row[data-id="m5"]') === 1);
  await page.locator('.mp-row[data-id="m4"]').click(); await page.waitForTimeout(500);
  await page.locator('[data-act="archive"]').click(); await page.waitForTimeout(600);
  check('Archive posts /archive and shows an undo strip', JSON.stringify(sent('/inherent/mail/archive').at(-1)?.body) === '{"ids":["m4"]}' && await count('.mp-row[data-id="m4"]') === 0 && (await strip()).includes('Archived'));
  await page.locator('.ad .toast button').click(); await page.waitForTimeout(500);
  check('Undo posts /unarchive', JSON.stringify(sent('/inherent/mail/unarchive').at(-1)?.body) === '{"ids":["m4"]}' && await count('.mp-row[data-id="m4"]') === 1);
  await page.locator('.mp-row[data-id="m3"]').click(); await until(() => document.querySelector('.mp-text'));
  await page.locator('[data-act="trash"]').click(); await page.waitForTimeout(500);
  await page.locator('.mp-chip[data-filter="job"]').click();
  check('a filter with nothing in it says so, and its chip counts 0', await count('.mp-row') === 0 && (await page.locator('.mp-list .muted').textContent()) === 'Nothing here.' && (await chips()).join('|') === 'All4|Reply1|Job search0');

  // Page height, and the Chinese strings.
  const view = await page.locator('.ad .view').evaluate(el => el.getBoundingClientRect().height);
  check('the page keeps the Dashboard sizing (VIEW_MIN..VIEW_MAX + DOCK) and scrolls inside', view >= 466 && view <= 646 && await page.locator('.ad .page .pg-body').evaluate(el => getComputedStyle(el).overflowY === 'auto'));
  await page.evaluate(() => localStorage.setItem('companion-settings-v1', JSON.stringify({ lang: 'zh' })));
  await page.reload(); await page.locator('.ad [data-block="mail"] .head').click(); await page.waitForTimeout(800);
  check('in Chinese the chips and tags read 全部 / 要回 / 找工作, 紧急, 知会', (await chips()).join('|').startsWith('全部') && (await chips()).join('|').includes('要回') && (await chips()).join('|').includes('找工作')
    && (await tagsOf('m1')).join('|') === '知会|紧急');
  await page.locator('.mp-row[data-id="m1"]').click(); await page.waitForTimeout(1200);
  check('and the letter’s bar reads 让 Jarvis 起草回复 with 在 Gmail 打开 / 标为已读 / 删除 / 归档 as named icons', (await texts('.mp-bar .btn')).join('|') === '让 Jarvis 起草回复'
    && (await page.locator('.mp-bar .icon-btn').evaluateAll(els => els.map(e => e.getAttribute('aria-label')))).join('|') === '在 Gmail 打开|标为已读|删除|归档');
  check('no unhandled renderer errors', errors.length === 0);
  writeFileSync(path.join(dir, 'checks.json'), JSON.stringify({ checks, errors, posts }, null, 2));
  console.log(`Mail page: ${checks.length} checks passed`);
} catch (error) {
  writeFileSync(path.join(dir, 'failure.json'), JSON.stringify({ checks, errors, posts, error: String(error) }, null, 2));
  throw error;
} finally { await browser?.close(); await server.close(); }
