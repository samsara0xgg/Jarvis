// Run after npm run build. 出手和别的 on the stage (scripts/agents-stage.mjs): the ⌘N slip that replaces the new-session
// page, its destination list (⇥) and agents (⇧⇥), the three-second fuse and ⌘Z, ⌘[ back, the picture viewer, the key
// sheet and !命令 in the workbench terminal. A real host on stand-in agents, the built page in Chromium. SHOTS=<folder>
// keeps a screenshot of each state.
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { deflateSync } from 'node:zlib';
import { stage } from './agents-stage.mjs';

const st = await stage();
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
let shell = ''; // the session whose terminal !命令 opened: its shell ends before the stage goes

// A small PNG of w×h in two colours, as a data: URL, for pictures sent with a message.
const crcT = new Uint32Array(256).map((_, n) => { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; return c >>> 0; });
const crc = b => { let c = 0xffffffff; for (const x of b) c = crcT[(c ^ x) & 255] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
const chunk = (t, d) => { const n = Buffer.alloc(4); n.writeUInt32BE(d.length); const td = Buffer.concat([Buffer.from(t), d]), c = Buffer.alloc(4); c.writeUInt32BE(crc(td)); return Buffer.concat([n, td, c]); };
function png(w, h, a, b) {
  const head = Buffer.alloc(13); head.writeUInt32BE(w, 0); head.writeUInt32BE(h, 4); head[8] = 8; head[9] = 2;
  const raw = Buffer.alloc((w * 3 + 1) * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) { const k = (x / w + y / h) / 2, o = y * (w * 3 + 1) + 1 + x * 3; for (let i = 0; i < 3; i++) raw[o + i] = Math.round(a[i] * (1 - k) + b[i] * k); }
  return `data:image/png;base64,${Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', head), chunk('IDAT', deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]).toString('base64')}`;
}

try {
  const p = st.page;
  const slip = () => p.evaluate(() => { const s = document.querySelector('.slip'); return { open: !!s && !s.hidden, text: s?.querySelector('textarea')?.value ?? '', dest: s?.querySelector('.mt-dest')?.textContent ?? '', focused: document.activeElement === s?.querySelector('textarea') }; });
  const bar = () => p.evaluate(() => { const b = document.querySelector('.mt-fuse'); return b && !b.hidden ? b.textContent : ''; });
  const onScreen = () => p.evaluate(() => document.querySelector('.m-head .h-t')?.textContent ?? '');
  const newRow = (known, pred = () => true) => [...st.rows.values()].find(s => !known.has(s.id) && pred(s));
  const known = () => new Set(st.rows.keys());
  const turns = () => [...st.claude(), ...st.codex()].filter(l => l.ev === 'turn').map(l => l.text ?? '');
  const itemsOf = async id => (await st.call(`/sessions/${id}`)).items ?? [];
  const typeSlip = async text => { await p.locator('.slip .mt-ta').fill(text); };
  const cmd = key => p.keyboard.press(`Meta+${key}`);
  // A screenshot with the pointer out of the way, so no hover card sits on what it shows.
  const snap = async name => { await p.mouse.move(4, 600); await p.waitForTimeout(200); await st.shot(name); };

  // ---------- an empty window: the slip is the way in ----------
  await st.open();
  let s = await slip();
  check('an empty window drops the slip by itself, ready to write in', s.open && s.focused, s);
  check('the new-session page is gone: no agent cards, no folder select, no composer', await p.locator('#proj, .agents .agent').count() === 0 && await p.locator('.composer').isHidden());
  check('the slip reads as a sentence: 去 新会话 · the folder, 用 Claude Code, 新 worktree', /去新会话·app/.test(s.dest.replace(/\s/g, '')) && s.dest.includes('Claude Code') && s.dest.includes('新 worktree'), s.dest);
  await typeSlip('draft kept on esc');
  await p.keyboard.press('Escape');
  await p.waitForTimeout(400);
  s = await slip();
  check('esc puts the slip away and the window says how to start', !s.open && await p.locator('.n-empty [data-act="new"]').isVisible(), s);
  await snap('mt-empty');
  await p.locator('.n-empty [data-act="new"]').click();
  s = await slip();
  check('写一句 (as 新会话 and ⌘N) brings the slip back with its words kept as a draft', s.open && s.text === 'draft kept on esc', s);

  // ---------- the fuse: nothing reaches the host for three seconds ----------
  let had = known();
  await typeSlip('hello from the slip');
  const thrown = Date.now();
  await p.keyboard.press('Enter');
  await sleep(1200);
  const fuse = await bar();
  check('⏎ throws it: the slip folds away and a fuse burns above the composer, with 撤回 ⌘Z', !(await slip()).open && fuse.includes('抛出去了：hello from the slip') && fuse.includes('撤回'), fuse);
  const ring = await p.evaluate(() => parseFloat(document.querySelector('.mt-fuse .fg')?.getAttribute('stroke-dasharray') ?? '0'));
  check('the fuse ring burns round as the seconds pass', ring > 20 && ring < 80, ring);
  await snap('mt-fuse');
  const A = (await st.until('the session thrown from an empty window', () => newRow(had), 8000)).id;
  await st.until('the first turn', () => ['done', 'err', 'wait'].includes(st.row(A)?.st));
  const heard = st.claude().find(l => l.ev === 'turn' && l.text === 'hello from the slip')?.at ?? 0;
  check('nothing reaches the host until the fuse has burnt: the session starts, and the agent hears it, 3 s after ⏎', st.row(A).created - thrown >= 2900 && heard - thrown >= 2900, [st.row(A).created - thrown, heard - thrown]);
  await p.waitForTimeout(600);
  check('once it has burnt the session starts, and an empty window opens it', (await onScreen()).includes('hello from the slip') && st.row(A).cwd.includes(path.join('.claude', 'worktrees')), [await onScreen(), st.row(A).cwd]);
  check('the folder, the agent and a new worktree are what the slip said', st.row(A).agent === 'claude' && st.row(A).tree === true && st.row(A).project === 'app');

  // ---------- ⌘N over a session, ⌘Z takes a throw back ----------
  await cmd('n');
  s = await slip();
  check('⌘N over a session drops the slip on top of the conversation', s.open && s.focused && await p.locator('.conv .you').filter({ hasText: 'hello from the slip' }).isVisible());
  await typeSlip('second, taken back');
  await snap('mt-n');
  had = known();
  await p.keyboard.press('Enter');
  await sleep(700);
  await cmd('z');
  s = await slip();
  check('⌘Z before the fuse has burnt takes the throw back into the slip, words and all', s.open && s.text === 'second, taken back', s);
  await sleep(3400);
  check('what was taken back never reaches the host', !newRow(had) && !turns().includes('second, taken back'));

  // ---------- ⏎ throws and you stay ----------
  await typeSlip('second, for real');
  await p.keyboard.press('Enter');
  const B = (await st.until('the session thrown to stay', () => newRow(had), 6000)).id;
  await p.waitForTimeout(500);
  check('⏎: the session starts and you stay where you were', (await onScreen()).includes('hello from the slip') && st.row(B).title.includes('second, for real'), await onScreen());
  const said = await bar();
  check('a moment after it starts, the line above the composer says so and offers 过去', said.includes('开跑了') && said.includes('过去'), said);
  await snap('mt-said');

  // ---------- ⌘⏎ throws and follows ----------
  had = known();
  await cmd('n');
  await typeSlip('third, follow it');
  const t0 = Date.now();
  await p.keyboard.press('Meta+Enter');
  const C = (await st.until('the followed session', () => newRow(had), 3000)).id;
  await st.until('the page on the followed session', async () => (await onScreen()).includes('third, follow it'), 3000);
  check('⌘⏎: no fuse, the session starts at once and you follow it there', Date.now() - t0 < 2800 && (await onScreen()).includes('third, follow it'), Date.now() - t0);
  await st.until('its first turn', () => ['done', 'err', 'wait'].includes(st.row(C)?.st));

  // ---------- 去处 ⇥: folders, sessions, 先存着; typing narrows it ----------
  await st.open(A);
  await cmd('n');
  await typeSlip('queue this behind');
  await p.keyboard.press('Tab');
  await p.waitForTimeout(300);
  const groups = await p.locator('.slip .mp-gh').allTextContents(), rows = await p.locator('.slip .mp-it .mp-l').allTextContents();
  check('⇥ opens 去处: folders to start in, sessions to queue behind, and 先存着', groups.join('|') === '新会话，在|排进|先存着' && rows.includes('app') && rows.includes('别的文件夹…') && rows.some(r => r.includes('third, follow it')) && rows.includes('先存着'), { groups, rows });
  const dirRow = await p.locator('.slip .mp-it').filter({ hasText: 'app' }).first().textContent();
  check('a folder shows its path and when it was last used', dirRow.includes(st.repo) && /刚用过|今天/.test(dirRow), dirRow);
  await snap('mt-where');
  await p.keyboard.type('follow');
  await p.waitForTimeout(200);
  const narrowed = await p.locator('.slip .mp-it .mp-l').allTextContents();
  check('typing narrows the list to what matches', narrowed.length === 1 && narrowed[0].includes('third, follow it'), narrowed);
  await snap('mt-where-typed');
  await p.keyboard.press('Enter');
  s = await slip();
  check('picking a session makes the slip say 排进 it, and what that will do', s.dest.includes('排进') && s.dest.includes('third, follow it') && s.dest.includes('做完了，接着往下做'), s.dest);
  had = known();
  await p.keyboard.press('Enter');
  await sleep(2200);
  check('thrown to a session, it waits on the fuse too', !(await itemsOf(C)).some(it => it.k === 'you' && it.text === 'queue this behind'));
  await st.until('the message in that session', async () => (await itemsOf(C)).some(it => it.k === 'you' && it.text === 'queue this behind'), 6000);
  check('then it goes to that session as a message, no new session, and you stay', !newRow(had) && (await onScreen()).includes('hello from the slip'));

  // ---------- 先存着 ----------
  await cmd('n');
  await typeSlip('keep this for later');
  await p.keyboard.press('Tab');
  await p.keyboard.type('先存');
  await p.keyboard.press('Enter');
  s = await slip();
  check('先存着 as the destination: 不开跑，不花 token, and ⏎ reads 存下', s.dest.includes('先存着') && s.dest.includes('不开跑，不花 token') && s.dest.includes('存下'), s.dest);
  had = known();
  await p.keyboard.press('Enter');
  const stored = await bar();
  await sleep(3500);
  check('a kept note starts nothing and sends nothing; the line above the composer says where it went', !newRow(had) && !turns().includes('keep this for later') && stored.includes('存下了') && stored.includes('先存着'), stored);
  await cmd('n');
  await p.keyboard.press('Tab');
  await p.keyboard.type('later');
  const kept = await p.locator('.slip .mp-g[aria-label="先存着"] .mp-it').filter({ hasText: 'keep this for later' }).count();
  check('the note waits in the same list, under 先存着', kept === 1);
  await snap('mt-keep');
  await p.locator('.slip .mp-it').filter({ hasText: 'keep this for later' }).click();
  s = await slip();
  check('picking the note brings its words back into the slip, to throw now', s.text === 'keep this for later' && !s.dest.includes('先存着'), s);
  await p.keyboard.press('Escape');

  // ---------- 别的文件夹… and 单独一个 worktree ----------
  const other = path.join(st.tmp, 'elsewhere');
  await mkdir(other, { recursive: true });
  await p.evaluate(dir => { window.agents.folder = async () => { window.__agentsCalls.push(['folder']); return dir; }; }, other);
  await cmd('n');
  await typeSlip('in another folder');
  await p.keyboard.press('Tab');
  await p.locator('.slip .mp-it').filter({ hasText: '别的文件夹…' }).click();
  await p.waitForTimeout(200);
  s = await slip();
  check('别的文件夹… asks the window for a folder and the slip goes there', (await p.evaluate(() => window.__agentsCalls.some(c => c[0] === 'folder'))) && s.dest.includes('elsewhere'), s.dest);
  had = known();
  await p.keyboard.press('Meta+Enter');
  const D = (await st.until('the session in the other folder', () => newRow(had), 3000)).id;
  check('the session runs in the folder picked', st.row(D).cwd === other, st.row(D).cwd);
  await st.until('its first turn', () => ['done', 'err', 'wait'].includes(st.row(D)?.st));
  await st.open(A);
  await cmd('n');
  await typeSlip('on the current branch');
  await p.keyboard.press('Tab');
  await p.keyboard.type('projects/app');
  await p.keyboard.press('Enter');
  await p.locator('.slip [data-act="slip-tree"]').click();
  s = await slip();
  check('the worktree switch reads 当前分支 once turned off', s.dest.includes('当前分支'), s.dest);
  had = known();
  await p.keyboard.press('Meta+Enter');
  const E = (await st.until('the session on the current branch', () => newRow(had), 3000)).id;
  check('turned off, the session works in the folder itself, not a worktree', st.row(E).tree === false && st.row(E).cwd === st.repo, st.row(E));
  await st.until('its first turn', () => ['done', 'err', 'wait'].includes(st.row(E)?.st));

  // ---------- 用谁 ⇧⇥ ----------
  await st.open(A);
  await cmd('n');
  await typeSlip('codex, take this');
  await p.keyboard.press('Shift+Tab');
  await p.waitForTimeout(300);
  const who = await p.locator('.slip .mp-g').first().locator('.mp-l').allTextContents();
  check('⇧⇥ opens 用谁 with the agents the host runs, and only those', who.join('|') === 'Claude Code|Codex', who);
  await snap('mt-who');
  await p.keyboard.press('ArrowDown');
  await p.keyboard.press('Enter');
  s = await slip();
  check('picking Codex puts it in the sentence', s.dest.includes('用Codex') || s.dest.includes('用 Codex'), s.dest);
  had = known();
  await p.keyboard.press('Meta+Enter');
  const F = (await st.until('the Codex session', () => newRow(had), 5000)).id;
  await st.until('Codex hearing it', () => st.codex().some(l => l.method === 'turn/start' && l.params?.threadId === F && l.params.input?.[0]?.text === 'codex, take this'));
  check('the session starts with Codex, and Codex is the one that hears it', st.row(F).agent === 'codex');

  // ---------- a window that closes while the fuse burns sends nothing ----------
  await st.open(A);
  await cmd('n');
  await typeSlip('closed while burning');
  had = known();
  await p.keyboard.press('Enter');
  await sleep(500);
  await st.open(A);
  await sleep(3500);
  await cmd('n');
  s = await slip();
  check('a window closed while a throw burns sends nothing; the words wait in the slip', !newRow(had) && !turns().includes('closed while burning') && s.text.includes('closed while burning'), s);
  await typeSlip('');
  await p.keyboard.press('Escape');

  // ---------- ⌘[ back to where you were ----------
  for (let k = 0; k < 4; k++) await st.send(A, `a longer answer ${k} PLAN`);
  await st.open(A);
  await p.waitForTimeout(400);
  await p.locator('#msg').fill('draft in the first one');
  const top = await p.evaluate(() => { const c = document.querySelector('.host .conv'); c.scrollTop = Math.max(0, (c.scrollHeight - c.clientHeight) / 2); return c.scrollTop; });
  await p.evaluate(id => window.__agentsOpen(id), C);
  await p.waitForTimeout(500);
  check('(another session comes on screen)', (await onScreen()).includes('third, follow it'));
  await p.locator('#msg').focus();
  await cmd('BracketLeft');
  await p.waitForTimeout(500);
  const back = await p.evaluate(() => ({ title: document.querySelector('.m-head .h-t')?.textContent, draft: document.querySelector('#msg').value, top: document.querySelector('.host .conv').scrollTop }));
  check('⌘[ goes back to the session you were in, its draft still there, at the scroll you left', back.title.includes('hello from the slip') && back.draft === 'draft in the first one' && Math.abs(back.top - top) < 4 && top > 0, { back, top });
  await snap('hist');
  await cmd('BracketRight');
  await p.waitForTimeout(400);
  check('⌘] goes forward again', (await onScreen()).includes('third, follow it'));
  await cmd('BracketRight');
  await p.waitForTimeout(200);
  check('past the newest it says so', (await p.locator('.toast').textContent()).includes('已经是最近的了'));

  // ---------- 大图 ----------
  await st.send(A, 'look at this one', { files: [{ name: 'one.png', url: png(320, 200, [90, 70, 200], [40, 160, 220]) }] });
  await st.send(A, 'SHOT the page');
  await st.send(A, 'and this one', { files: [{ name: 'two.png', url: png(200, 300, [255, 180, 120], [120, 60, 160]) }] });
  await st.open(A);
  await p.waitForTimeout(500);
  await p.locator('.conv .you .pic').first().click();
  const ql = () => p.evaluate(() => { const d = document.querySelector('dialog.ql'); return { open: !!d?.open, name: d?.querySelector('.ql-h b')?.textContent ?? '', n: d?.querySelector('.ql-n')?.textContent ?? '', thumbs: d?.querySelectorAll('.ql-f button').length ?? 0 }; });
  // The picture on show once it has loaded (its size is read from it).
  const shown = name => st.until(`${name} on show`, async () => { const v = await ql(); return v.name === name && v.n.includes('×') && v; }, 4000).catch(() => ql());
  let v = await shown('one.png');
  check('a picture opens large with its name, size and place among the session\'s pictures', v.open && v.name === 'one.png' && v.n === '320 × 200 · 1 / 3' && v.thumbs === 3, v);
  await p.waitForTimeout(300);
  await snap('ql');
  await p.keyboard.press('ArrowRight');
  v = await shown('图片 1');
  check('→ steps to the next picture: the one its tool gave back', v.name === '图片 1' && v.n === '48 × 32 · 2 / 3', v);
  await p.keyboard.press('ArrowRight');
  v = await shown('two.png');
  check('→ again: the picture of the later message', v.name === 'two.png' && v.n === '200 × 300 · 3 / 3', v);
  await p.keyboard.press('ArrowRight');
  const round = (await shown('one.png')).name;
  await p.keyboard.press('ArrowLeft');
  const backed = (await shown('two.png')).name;
  check('past the last it comes round to the first; ← steps back', round === 'one.png' && backed === 'two.png', [round, backed]);
  await p.keyboard.press('Space');
  const shut = async () => !(await ql()).open;
  check('space puts it away, back into the picture it came from', await st.until('the viewer to close on space', shut, 2000));
  await p.locator('.conv .you .pic').first().click();
  await p.waitForTimeout(400);
  await p.keyboard.press('Escape');
  check('esc puts it away too, and the conversation keeps the keys again', await st.until('the viewer to close on esc', shut, 2000) && await p.locator('.slip').isHidden());
  await st.context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(p.url()).origin });
  await p.locator('.conv .you .pic').first().click();
  await p.waitForTimeout(400);
  await p.locator('.ql [data-act="ql-copy"]').click();
  const copied = await st.until('the copy button to answer', async () => { const t = await p.locator('.ql [data-act="ql-copy"]').textContent(); return /复制好了|没能复制/.test(t) && t; }, 5000);
  const clip = await p.evaluate(async () => (await navigator.clipboard.read()).flatMap(i => i.types));
  check('复制图片 puts the picture itself on the clipboard, and the button says so', clip.includes('image/png') && copied.includes('复制好了'), [copied, clip]);
  await p.locator('.ql [data-act="ql-stage"]').click();
  await st.until('the picture on the stage', () => p.locator('.pv:not(.off) img.ql-stage').isVisible(), 4000).catch(() => false);
  await p.waitForTimeout(500);
  check('放上舞台 moves it onto the workbench\'s stage', !(await ql()).open && await p.locator('.pv:not(.off) img.ql-stage').isVisible());
  await snap('ql-stage');
  await p.keyboard.press('Escape');
  await p.waitForTimeout(700);

  // ---------- 键位表 ? ----------
  await p.locator('#msg').fill('');
  await p.locator('#msg').focus();
  await p.keyboard.press('?');
  await p.waitForTimeout(300);
  const sheet = await p.evaluate(() => { const k = document.querySelector('.keys'); return { open: !!k && !k.hidden, text: k?.textContent ?? '', groups: [...(k?.querySelectorAll('h5') ?? [])].map(h => h.textContent) }; });
  check('? with the composer empty shows every key of the window on one sheet', sheet.open && sheet.groups.join('|') === '出手|等你的|去哪|眼前这个会话|消息|esc 的顺序' && ['⌘', 'N', '!命令', '⌃', '/rewind'].every(k => sheet.text.includes(k)) && (await p.locator('#msg').inputValue()) === '', sheet.groups);
  check('what the window does not do is not on it (⌘Space from another app, dictation)', !sheet.text.includes('Space') && !sheet.text.includes('听写'));
  await snap('keys');
  await p.keyboard.press('Escape');
  await p.waitForTimeout(200);
  check('esc puts the sheet away', await p.locator('.keys').isHidden());
  await p.locator('#msg').focus();
  await p.keyboard.type('why?');
  check('while you are writing, ? is only a question mark', await p.locator('.keys').isHidden() && (await p.locator('#msg').inputValue()) === 'why?');
  await p.locator('#msg').fill('');

  // ---------- !命令 ----------
  const before = turns().length;
  await p.locator('#msg').focus();
  shell = A;
  await p.keyboard.type('!echo mt-$((6*7))');
  await p.keyboard.press('Enter');
  await st.until('the command in the terminal', () => p.evaluate(() => (document.querySelector('.tm .xterm-rows')?.textContent ?? '').includes('mt-42')), 10000);
  const term = await p.evaluate(() => ({ open: !document.querySelector('.tm')?.classList.contains('off'), tab: document.querySelector('.tm [data-t="term"]')?.getAttribute('aria-selected'), composer: document.querySelector('#msg').value, focus: document.activeElement?.id }));
  check('!命令 runs once in the session\'s terminal: the pane opens on 终端 and shows what it printed', term.open && term.tab === 'true', term);
  await sleep(600);
  check('and it never reaches the agent; the composer is empty and keeps the keys', turns().length === before && !turns().some(t => t.includes('echo mt-')) && term.composer === '' && term.focus === 'msg', { term, turns: turns().slice(before) });
  await snap('term');

  check('no errors on the page', !st.errors.length, st.errors);
  console.log(`\n${checks.length} checks passed`);
} catch (e) {
  console.error(e, '\n--- host ---\n', st.log().slice(-3000));
  if (st.shots) await st.shot('failed').catch(() => {});
  process.exitCode = 1;
} finally {
  if (shell) { await st.call(`/term/${shell}`, undefined, 'DELETE').catch(() => {}); await sleep(500); }
  await st.close();
}
