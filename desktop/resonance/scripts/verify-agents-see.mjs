// Run after npm run build. What it is doing, on the stage (scripts/agents-stage.mjs): its thinking as a line of its own,
// a sub-agent on the right with its own 停, background work above the composer, files in steps and answers with the
// file menu, paths that link only when the host finds them, the name Claude Code gives a session, the skeleton on first
// open, and 打开 in the title's menu. SHOTS=<folder> keeps a screenshot of each.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { utimesSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { stage } from './agents-stage.mjs';

const st = await stage();
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const p = st.page, sleep = ms => new Promise(r => setTimeout(r, ms));
const settled = id => !['work', 'pack'].includes(st.row(id)?.st);
const items = async id => (await st.call(`/sessions/${id}`)).items;
const steps = async id => (await items(id)).filter(i => i.k === 'steps').flatMap(i => i.steps);
const calls = async k => (await p.evaluate(() => window.__agentsCalls)).filter(c => c[0] === k);
const text = sel => p.locator(sel).first().innerText().catch(() => '');
const turnEnds = () => st.claude().filter(e => e.ev === 'turn end').length;
// Opens the title's ··· menu (it shows while the pointer is on the title strip).
const more = async () => { await p.hover('.m-head'); await p.locator('.m-head [data-act="menu"][data-v="more"]').click(); await p.waitForSelector('.pop.on'); };
// A right click on a file, and one line of the menu it gives.
const fileMenu = async (loc, line) => { await loc.click({ button: 'right' }); await p.waitForSelector('.pop.on [data-act="seefside"]'); if (line) await p.locator(`.pop.on [data-act="${line}"]`).click(); };
try {
  const id = await st.session('hello there');
  await st.open(id);
  const repo = st.repo;

  // ---------- 想: a line of its own, the seconds moving; afterwards 想了 N 秒 and the whole thought ----------
  await st.call(`/sessions/${id}/send`, { text: 'THINK first' });
  await p.waitForSelector('.step .see-th.run', { timeout: 8000 });
  await sleep(200);
  const live = (await steps(id)).at(-1);
  check('while Claude thinks, the host already has its thought as a step, not yet timed', live?.k === 'think' && live.t === '' && live.ms === undefined, live);
  const t1 = await text('.step .see-th.run'); await sleep(2000); const t2 = await text('.step .see-th.run');
  check('the page shows 想 on a line of its own, 在想 · N 秒 with the seconds moving', /^在想 · \d+ 秒$/.test(t1) && t1 !== t2 && /想/.test(await text('.steps.live .step:last-child .k')), [t1, t2]);
  await st.shot('see-think-run');
  await st.until('the thought ends', () => settled(id), 15000);
  const th = (await steps(id)).filter(s => s.k === 'think').at(-1);
  check('when it is done, the step carries the whole thought and how long it took', th.ms >= 3800 && th.ms < 8000 && th.out.includes('Weighing where the switch belongs') && th.t === '**Weighing where the switch belongs**', th);
  await p.locator('.s-sum').last().click(); await sleep(300);
  const row = p.locator('.step:has(.see-th)').last();
  check('afterwards the line reads 想了 N 秒', /^想了 [4-7] 秒$/.test(await row.locator('.see-th').innerText()), await row.innerText());
  await row.click(); await sleep(400);
  const box = await row.locator('.see-tb').evaluate(el => ({ h: el.getBoundingClientRect().height, sh: el.scrollHeight, ch: el.clientHeight, t: el.innerText }));
  check('a click shows the whole thought in a box of limited height that scrolls', box.h <= 124 && box.sh > box.ch && box.t.includes('Weighing where the switch belongs') && box.t.includes('run the companion check again'), box);
  await row.locator('.see-tb').click(); await sleep(200);
  const stays = await row.locator('.see-tb').count();
  await st.shot('see-think');
  await row.locator('.see-th').click(); await sleep(300);
  check('a click inside the thought keeps it open; a click on its line folds it', stays === 1 && await row.locator('.see-tb').count() === 0);
  const cx = await st.session('THINK please', { agent: 'codex' });
  const cth = (await steps(cx)).find(s => s.k === 'think');
  check('Codex\'s reasoning comes with how long it took', cth?.ms >= 1100 && cth.out.includes('Checking the ask'), cth);
  // A session Claude Code wrote in a terminal: the thought read back takes the time since the entry before it.
  const OUT = crypto.randomUUID(), dir = path.join(st.HOME, '.claude', 'projects', repo.replace(/[^a-zA-Z0-9]/g, '-')), T = Date.now() - 10 * 60e3, [u1, u2, u3] = [0, 1, 2].map(() => crypto.randomUUID());
  const at = ms => ({ isSidechain: false, userType: 'external', cwd: repo, sessionId: OUT, version: '9.9.9', gitBranch: 'main', timestamp: new Date(T + ms).toISOString() });
  const asst = content => ({ id: `msg_${crypto.randomUUID().slice(0, 6)}`, type: 'message', role: 'assistant', model: 'fake-sonnet', content });
  await mkdir(dir, { recursive: true });
  await writeFile(path.join(dir, `${OUT}.jsonl`), `${[{ ...at(0), parentUuid: null, type: 'user', message: { role: 'user', content: 'think in the terminal' }, uuid: u1 },
    { ...at(4000), parentUuid: u1, type: 'assistant', message: asst([{ type: 'thinking', thinking: 'Four seconds of it.', signature: 'x' }]), uuid: u2 },
    { ...at(5000), parentUuid: u2, type: 'assistant', message: asst([{ type: 'text', text: 'Thought it over.' }]), uuid: u3 }].map(l => JSON.stringify(l)).join('\n')}\n`);
  utimesSync(path.join(dir, `${OUT}.jsonl`), new Date(T + 5000), new Date(T + 5000));
  await st.call('/import', { agent: 'claude', id: OUT, cwd: repo });
  const oth = (await steps(OUT)).find(s => s.k === 'think');
  check('a thought read back from a transcript takes the time since the entry before it', oth?.ms === 4000, oth);

  // ---------- 子任务: one row; on the right its steps, live, and a 停 that stops only it; done, its result ----------
  const n0 = turnEnds();
  await st.call(`/sessions/${id}/send`, { text: 'AGENT stop me' });
  await p.waitForSelector('.step .see-sa', { timeout: 8000 });
  await st.until('the sub-agent has its task', async () => (await steps(id)).some(s => s.k === 'agent' && s.task));
  const ag = (await steps(id)).find(s => s.k === 'agent' && s.task), task = st.row(id).tasks.find(t => t.id === ag.task);
  check('the host ties the sub-agent\'s step to its task, a task started in the foreground', task?.kind === 'local_agent' && task.fg === true && task.st === 'run', { ag, task });
  check('the sub-agent is one row: what it does and how far', (await text('.step .see-sa')) === 'Explore · Find the readme' && /^(在起|第 \d+ 步)$/.test(await text('.step .see-sn')));
  check('a sub-agent the turn waits on is not background work: no row above the composer', await p.locator('.c-rows .see-bg').count() === 0);
  await p.locator('.step .see-sa').click();
  await p.waitForSelector('.pv:not(.off) .see-bar', { timeout: 5000 });
  const k0 = await p.locator('.pv .see-steps .step').count();
  check('a click opens it on the right: what it is doing now, how long, its own 停', (await text('.pv .sh b')) === 'Explore · Find the readme' && /^子任务 · 在跑/.test(await text('.pv .sh small'))
    && (await text('.pv .see-now')).length > 0 && /^\d+:\d\d$/.test(await text('.pv .see-bar em')) && await p.locator('.pv [data-act="seesubstop"]').count() === 1 && await p.locator('.step .see-sa.on').count() === 1);
  await st.until('another step of its own', async () => await p.locator('.pv .see-steps .step').count() > k0 && (await text('.pv .see-steps')).includes('*.md'), 5000);
  check('its own steps come in on the right as it works', await p.locator('.pv .see-steps .step').count() > k0 && (await text('.pv .see-steps')).includes('*.md'), await text('.pv .see-steps'));
  await st.shot('see-sub');
  await p.locator('.pv [data-act="seesubstop"]').click();
  await st.until('the sub-agent stopped', () => st.row(id).tasks?.find(t => t.id === ag.task)?.st === 'stop', 5000);
  await st.until('the turn ends', () => turnEnds() > n0 && settled(id), 15000);
  await sleep(1200);
  const stopped = (await steps(id)).find(s => s.k === 'agent' && s.task === ag.task), fin = await items(id);
  check('停 stops only the sub-agent: Claude Code is asked to stop that task, and the turn goes on to its answer', st.claude().some(e => e.subtype === 'stop_task' && e.request.task_id === ag.task)
    && stopped.ok === false && st.claude().filter(e => e.ev === 'turn end').at(-1).how === 'ok' && fin.at(-1).k === 'it' && fin.at(-1).text.includes('AGENT stop me') && !st.row(id).stopped, { stopped, last: fin.at(-1) });
  check('the panel says it stopped, and how far it got', /^子任务 · 停了 · 做到第 \d+ 步$/.test(await text('.pv .sh small')) && await p.locator('.pv .see-bar').count() === 0, await text('.pv .sh small'));
  await st.shot('see-sub-stop');
  await st.call(`/sessions/${id}/send`, { text: 'AGENT again' });
  await p.waitForSelector('.steps.live .step .see-sa', { timeout: 8000 });
  await p.locator('.steps.live .step .see-sa').click();
  await st.until('the second sub-agent is done', () => settled(id), 20000);
  await sleep(1500);
  const res = await text('.pv .see-res');
  check('a sub-agent that finishes shows its result on the right, its paths linked', res.includes('The readme is a short list: one, two.') && /^子任务 · 做完了 · 4 步/.test(await text('.pv .sh small'))
    && await p.locator('.pv .see-res .see-p[data-ref="src/a.ts"]').count() === 1, [res, await text('.pv .sh small')]);
  await st.shot('see-sub-done');
  await p.locator('.pv [data-act="pvclose"]').click(); await sleep(700);

  // ---------- 后台: a row above the composer; what, how long, 看输出, 停 ----------
  await st.send(id, 'TASK go');
  const bg = st.row(id).tasks.find(t => t.kind === 'local_bash');
  check('a shell sent to the background says where its output goes while it runs', bg?.st === 'run' && !bg.fg && bg.out?.endsWith(`${bg.id}.output`), bg);
  await p.waitForSelector('.c-rows .see-bgh', { timeout: 5000 });
  const h1 = await text('.see-bgh'); await sleep(1500); const h2 = await text('.see-bgh');
  check('a row above the composer: 后台, how many run, what, and how long, counting', /后台/.test(h1) && h1.includes('1 个在跑') && h1.includes('Wait in the background') && h1 !== h2, [h1, h2]);
  await st.shot('see-bg-row');
  await p.locator('.see-bgh').click(); await sleep(300);
  const r1 = await text('.see-bgr');
  check('it opens into a line each: what, how long, 看输出, 停', r1.includes('Wait in the background') && /在跑 \d+:\d\d/.test(r1) && r1.includes('看输出') && r1.includes('停'), r1);
  await p.locator('.see-bgr [data-act="seebgout"]').click();
  await p.waitForSelector('.pv:not(.off) .see-out', { timeout: 5000 });
  const o1 = (await text('.pv .see-out')).match(/tick/g)?.length ?? 0; await sleep(2600); const o2 = (await text('.pv .see-out')).match(/tick/g)?.length ?? 0;
  check('看输出 opens its output on the right, and it keeps coming', (await text('.pv .see-out')).startsWith('waiting in the background') && o2 > o1 && (await text('.pv .sh small')) === '后台 · 在跑' && await p.locator('.see-bgo.on').count() === 1, [o1, o2]);
  await st.shot('see-bg');
  await p.locator('.see-bgr [data-act="seebgstop"]').click();
  await st.until('the shell stopped', () => st.row(id).tasks.find(t => t.id === bg.id)?.st === 'stop', 5000);
  await sleep(1800);
  const o3 = await text('.pv .see-out'); await sleep(1600);
  check('停 stops it: Claude Code is asked to stop that task, the line says so, its output stops', st.claude().some(e => e.subtype === 'stop_task' && e.request.task_id === bg.id) && (await text('.see-bgr')).includes('停了')
    && (await text('.pv .sh small')) === '后台 · 停了' && await p.locator('.pv .see-bar').count() === 0 && o3 === await text('.pv .see-out'));
  await p.locator('.pv [data-act="pvclose"]').click(); await sleep(700);
  await p.locator('#msg').focus(); await p.keyboard.press('Escape'); await sleep(300);
  check('esc folds the open list back into its one row', await p.locator('.see-bgr').count() === 0 && await p.locator('.see-bgh').count() === 1);
  await p.locator('#msg').fill('/tasks'); await p.keyboard.press('End'); await p.keyboard.type(' '); await sleep(500);
  await p.keyboard.press('Enter'); await sleep(400);
  check('/tasks opens the list', await p.locator('.see-bgr').count() === 1 && !(await items(id)).some(i => i.k === 'you' && i.text.startsWith('/tasks')));
  await p.keyboard.press('Escape');

  // ---------- files in steps and answers, and the file menu on a right click ----------
  await st.send(id, 'EDIT notes.txt: `notes.txt` `src/a.ts:2` README.md `src/nope.ts` docs/none.md');
  await sleep(600);
  await p.locator('.s-sum').last().click(); await sleep(300);
  const fb = p.locator('.step .see-f[data-act="peek"][data-ref="notes.txt"]').last();
  check('a step\'s file is a file button', await fb.count() === 1 && (await fb.innerText()) === 'notes.txt');
  await fb.click();
  await p.waitForSelector('.pv:not(.off) .op-code', { timeout: 5000 });
  await sleep(700);
  check('a click on it opens the file on the right, the button marked', (await text('.pv .sh b')) === 'notes.txt' && await fb.evaluate(el => el.classList.contains('on')));
  await p.locator('.pv [data-act="pvclose"]').click(); await sleep(700);
  await fileMenu(fb);
  check('a right click gives 在右边打开 · 在编辑器里打开 · 在访达里显示 · 复制路径', (await text('.pop.on')).split('\n').map(s => s.trim()).filter(Boolean).join(' · ') === '在右边打开 · 在编辑器里打开 · 在访达里显示 · 复制路径', await text('.pop.on'));
  await st.shot('see-file');
  const abs = path.join(repo, 'notes.txt');
  await p.locator('.pop.on [data-act="seefed"]').click(); await sleep(200);
  check('在编辑器里打开 asks the window for this Mac\'s editor, with the path resolved against the session\'s folder', (await calls('openInEditor')).some(c => c[1] === abs), await calls('openInEditor'));
  await fileMenu(fb, 'seeffind'); await sleep(200);
  check('在访达里显示 asks the window to show it in Finder', (await calls('revealFile')).some(c => c[1] === abs), await calls('revealFile'));
  await st.context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(p.url()).origin });
  await fileMenu(fb, 'seefcopy'); await sleep(300);
  check('复制路径 copies the whole path', (await p.evaluate(() => navigator.clipboard.readText())) === abs);
  const ans = p.locator('.conv .c-items .it').last();
  const line = ans.locator('code.see-p[data-ref="src/a.ts:2"]');
  await fileMenu(line, 'seefed'); await sleep(200);
  check('a path with a line opens in the editor at that line', (await calls('openInEditor')).some(c => c[1] === path.join(repo, 'src/a.ts') && c[2] === 2), await calls('openInEditor'));
  await fileMenu(ans.locator('.see-p[data-ref="README.md"]'), 'seefside');
  await p.waitForSelector('.pv:not(.off) .op-md', { timeout: 5000 }); await sleep(700);
  check('在右边打开 on a path in an answer opens it on the right, the path marked', (await text('.pv .sh b')) === 'README.md' && await ans.locator('.see-p.on[data-ref="README.md"]').count() === 1);
  await p.locator('.pv [data-act="pvclose"]').click(); await sleep(700);
  await fileMenu(ans.locator('.lnk[data-ref="notes.txt"]'), 'seeffind'); await sleep(200);
  check('a card of a file it changed has the same menu', (await calls('revealFile')).filter(c => c[1] === abs).length === 2);

  // ---------- paths in answers: links only when the host finds them, looked up together, never in the way ----------
  const html = await ans.locator('.md').innerHTML();
  check('in an answer, paths the host finds are links, relative ones from the session\'s folder', await ans.locator('.see-p[data-ref="src/a.ts:2"]').count() === 1 && await ans.locator('a.see-p[data-ref="README.md"]').count() === 1
    && await ans.locator('code.see-p[data-ref="notes.txt"]').count() === 1 && await ans.locator('a.see-p[data-ref="notes.txt"]').count() === 1, html);
  check('paths it does not find stay plain text', await ans.locator('code:not(.ref)', { hasText: 'src/nope.ts' }).count() === 1 && await ans.locator('[data-ref="src/nope.ts"], [data-ref="docs/none.md"]').count() === 0 && (await ans.innerText()).includes('docs/none.md'), html);
  const asked = [];
  p.on('request', q => { if (q.url().includes('/resolve') && q.method() === 'POST') asked.push({ at: Date.now(), refs: JSON.parse(q.postData() ?? '{}').refs ?? [] }); });
  // The host is slow to answer: the answer is drawn at once with its paths plain, and they link when it answers.
  const slow = u => u.href.startsWith(`${st.API}/resolve`);
  await p.route(slow, async r => { await sleep(1500); await r.fallback(); });
  await st.call(`/sessions/${id}/send`, { text: 'Also `./src/a.ts`, ./README.md and `nowhere/x.ts`' });
  const last = () => p.locator('.conv .c-items .it').last();
  await st.until('the answer is drawn', async () => settled(id) && (await last().innerText()).includes('nowhere/x.ts'), 8000);
  const plain = await last().locator('.see-p').count();
  await st.until('its paths are linked', async () => await last().locator('.see-p').count() === 2, 6000);
  check('an answer is drawn without waiting for its paths to be looked up, then they link; one request for them all', plain === 0 && asked.length >= 1
    && ['./src/a.ts', './README.md', 'nowhere/x.ts'].every(r => asked[0].refs.includes(r)) && await last().locator('code:not(.ref)', { hasText: 'nowhere/x.ts' }).count() === 1, { plain, asked });
  await p.unroute(slow);
  await st.shot('see-path');

  // ---------- the title: your first words, then the name Claude Code gives it; your own name stays ----------
  const tl = await st.session('hello LATE');
  check('a session is titled with your first words while Claude Code has not named it', st.row(tl).title === 'hello LATE');
  await p.evaluate(i => window.__agentsOpen(i), tl);
  await p.waitForFunction(() => document.querySelector('.m-head .h-t b')?.textContent === 'hello LATE');
  await st.until('Claude Code\'s name', () => st.row(tl).title === 'Stand-in: hello LATE', 9000);
  await p.waitForFunction(() => document.querySelector('.m-head .h-t b')?.textContent === 'Stand-in: hello LATE', null, { timeout: 3000 });
  check('then it takes the name Claude Code gives it, even one written after the turn ended', true);
  await st.shot('see-title');
  await p.locator('.m-head .h-t b').click();
  await p.locator('#rename').fill('My own name'); await p.keyboard.press('Enter');
  await st.until('renamed', () => st.row(tl).title === 'My own name' && st.row(tl).named);
  await st.send(tl, 'one more');
  await sleep(5500);
  check('a name you gave it stays through the turns after', st.row(tl).title === 'My own name' && (await text('.m-head .h-t b')) === 'My own name');

  // ---------- the skeleton, the first time a session is opened ----------
  const sk = await st.session('second one');
  const unread = u => u.href === `${st.API}/sessions/${sk}`;
  await p.route(unread, async r => { await sleep(1500); await r.fallback(); });
  await p.evaluate(i => window.__agentsOpen(i), sk);
  await sleep(500);
  const bones = await p.locator('.conv .c-items .skel i').count();
  check('opening a session not read yet shows a skeleton of a conversation, not a line of text', bones >= 8 && await p.locator('.conv .c-items .skel').isVisible() && !(await text('.conv')).includes('在读这个会话'));
  await st.shot('see-skel');
  await st.until('the conversation', async () => (await text('.conv .c-items .it')).includes('好的，做完了：second one'), 6000);
  check('then the conversation takes its place', await p.locator('.conv .skel').count() === 0);
  await p.unroute(unread);

  // ---------- 打开 in the title's ··· menu ----------
  await more();
  check('the title\'s ··· menu has the folder, 在编辑器里打开 · 在访达里显示 · 在终端里打开', (await text('.pop.on .ph')) === 'app' && (await text('.pop.on')).includes('在编辑器里打开\n在访达里显示\n在终端里打开'), await text('.pop.on'));
  await st.shot('see-open');
  await p.locator('.pop.on [data-act="seeed"]').click(); await sleep(200);
  await more(); await p.locator('.pop.on [data-act="reveal"]').click(); await sleep(200);
  await more(); await p.locator('.pop.on [data-act="seeterm"]').click(); await sleep(300);
  check('each asks the window to open the session\'s folder: the editor, Finder, a shell in the terminal', (await calls('openInEditor')).some(c => c[1] === repo && c.length === 2)
    && (await calls('reveal')).some(c => c[1] === repo) && (await calls('terminal')).some(c => c[1] === repo && c[2] === ''), await p.evaluate(() => window.__agentsCalls.filter(c => c[0] !== 'presence')));
  check('a terminal that does not open says so', (await text('.toast')).startsWith('没能打开终端'));
  const e0 = (await calls('openInEditor')).length;
  await p.locator('#msg').fill('/ide'); await p.keyboard.press('End'); await p.keyboard.type(' '); await sleep(500);
  await p.keyboard.press('Enter'); await sleep(300);
  check('/ide opens the folder in the editor too', (await calls('openInEditor')).length === e0 + 1 && (await calls('openInEditor')).at(-1)[1] === repo);

  check('no errors on the page', !st.errors.length, st.errors);
  console.log(`\n${checks.length} checks passed`);
} catch (e) {
  console.error(e, '\n--- host ---\n', st.log().slice(-3000));
  await st.shot('see-fail').catch(() => {});
  process.exitCode = 1;
} finally { await st.close(); }
