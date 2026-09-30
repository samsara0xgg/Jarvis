// Run after npm run build. WP-A · 消息 on the stage (scripts/agents-stage.mjs): a real host on stand-in agents, the built
// page in Chromium. Under each message: 复制 on both kinds of session; 修改 of a middle message on Claude (the files back
// from its checkpoints) and on Codex (the conversation only), one while a turn runs; the versions ‹ n/m ›; reactions
// that stay on the host and ride the next message once; 👀 on a message Claude took while it worked, and ↑ taking a
// queued one back. SHOTS=<folder> keeps a screenshot of each state.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { stage } from './agents-stage.mjs';

const st = await stage();
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const p = st.page, sleep = ms => new Promise(r => setTimeout(r, ms));
const items = async id => (await st.call(`/sessions/${id}`)).items;
const conv = '.host > .conv .c-items > .item';
const youEl = text => p.locator(conv).filter({ has: p.locator('.you', { hasText: text }) });
const itEl = text => p.locator(conv).filter({ has: p.locator('.it', { hasText: text }) });
const settled = s => s && !['work', 'pack', 'wait'].includes(s.st);
const turns = () => st.claude().filter(e => e.ev === 'turn');
const clip = () => p.evaluate(() => navigator.clipboard.readText());
// Typed into the composer and sent, the way Allen sends one.
async function type(text) { await p.fill('#msg', text); await p.press('#msg', 'Enter'); }
// The new version an edit made: the session in the list with this root that is not `old`.
const versionOf = old => [...st.rows.values()].find(s => s.id !== old && !s.archived && s.vers?.root === (st.row(old).vers?.root ?? old));
try {
  await st.context.grantPermissions(['clipboard-read', 'clipboard-write']);

  // ======================= 复制 · 修改 · 表情 under each message (m-acts) =======================
  const A = await st.session('hello there');
  await st.send(A, 'second one');
  await st.open(A);
  const lastIt = itEl('second one'), firstYou = youEl('hello there'), lastYou = youEl('second one');
  const acts = async el => el.locator('.it-acts, .m-acts').first().evaluate(r => ({ tips: [...r.querySelectorAll('[data-tip]')].map(b => b.dataset.tip), time: !!r.querySelector('time'),
    order: [...r.children].map(c => c.tagName === 'TIME' ? 'time' : c.dataset.tip ?? c.className).map(String), shown: getComputedStyle(r).opacity }));
  const ai = await acts(lastIt), yi = await acts(lastYou), yOld = await acts(firstYou);
  check('under an answer: its time, 复制 and 表情; under what you said: 表情, 修改, 复制 and its time', ai.tips.join() === '复制,表情' && ai.time
    && yi.tips.join() === '表情,修改,复制' && yi.time && yi.order.at(-1) === 'time', { ai, yi });
  check('the latest thing you said and the latest answer keep their row showing; older ones show it on hover', ai.shown === '1' && yi.shown === '1' && yOld.shown === '0', { ai, yi, yOld });
  await st.shot('m-acts');
  await lastIt.locator('.it-acts [data-act="copy"]').click();
  const itText = (await items(A)).filter(i => i.k === 'it').at(-1).text;
  check('复制 under an answer gives the Markdown it wrote, and says so', (await clip()) === itText && await lastIt.locator('.it-acts .ia.ok').count() === 1, await clip());
  await lastYou.locator('.m-acts [data-act="copy"]').click();
  check('复制 under what you said gives its words', (await clip()) === 'second one' && await lastYou.locator('.m-acts .ia.ok').count() === 1, await clip());

  const C = await st.session('codex hello', { agent: 'codex' });
  await st.send(C, 'codex second');
  await st.open(C);
  await itEl('codex second').locator('.it-acts [data-act="copy"]').click();
  const cIt = (await items(C)).filter(i => i.k === 'it').at(-1).text;
  const cCopied = await clip();
  await youEl('codex second').locator('.m-acts [data-act="copy"]').click();
  check('the same on a Codex session: 复制 gives the answer\'s Markdown and your words', cCopied === cIt && (await clip()) === 'codex second', { cCopied, cIt });
  await st.shot('m-acts-codex');

  // ======================= 修改 a middle message on Claude: its files go back (m-edit, m-ver) =======================
  const A_TS = path.join(st.repo, 'src', 'a.ts'), was = readFileSync(A_TS, 'utf8');
  const B = await st.session('first ask');
  await st.send(B, 'the middle one');
  await st.send(B, 'EDIT src/a.ts');
  await st.call(`/sessions/${B}/meta`, { pinned: true });
  check('the later turn changed a file', readFileSync(A_TS, 'utf8') !== was);
  await st.open(B);
  const place = () => p.evaluate(() => { const rows = [...document.querySelectorAll('.row[data-id]')]; return { on: document.querySelector('.row.is-on')?.dataset.id, at: rows.findIndex(r => r.classList.contains('is-on')), n: rows.length }; });
  const before = await place(), title = st.row(B).title, midId = (await items(B)).find(i => i.k === 'you' && i.text === 'the middle one').id;
  const mid = youEl('the middle one');
  await mid.hover();
  await mid.locator('[data-act="m-edit"]').click();
  const box = p.locator('.m-eta');
  await p.waitForFunction(() => /之后改的 1 个文件也回去/.test(document.querySelector('.m-erow small')?.textContent ?? ''), null, { timeout: 5000 });
  check('修改 opens what you said in place, with 取消 and 重发, and says the file changed after it goes back too',
    (await box.inputValue()) === 'the middle one' && await box.evaluate(t => t === document.activeElement) && await p.locator('.m-edit [data-act="m-ex"]').count() === 1
    && (await p.locator('.m-edit [data-act="m-ego"]').innerText()).startsWith('重发'));
  const short = await box.evaluate(t => t.getBoundingClientRect().height);
  await box.fill('the middle one, changed\nwith a second line\nand a third');
  const tall = await box.evaluate(t => t.getBoundingClientRect().height);
  check('the box grows with what is typed in it', tall > short, { short, tall });
  await st.shot('m-edit');
  await box.press('Escape');
  await box.waitFor({ state: 'detached', timeout: 5000 });
  check('esc leaves it as it was', (await youEl('the middle one').locator('.you').innerText()) === 'the middle one' && await youEl('the middle one').locator('.m-acts').count() === 1
    && await p.evaluate(() => document.activeElement?.id === 'msg'));
  await mid.hover();
  await mid.locator('[data-act="m-edit"]').click();
  await box.fill('the middle one, changed');
  await box.press('Control+Enter');
  const B2 = await st.until('the new version', () => versionOf(B)?.id);
  await st.until('its answer', async () => settled(st.row(B2)) && (await items(B2)).some(i => i.k === 'it' && i.text.includes('the middle one, changed')));
  const b2 = await items(B2), says = b2.filter(i => i.k === 'you').map(i => i.text), lastB2 = b2.filter(i => i.k !== 'note').at(-1);
  check('重发 on Claude: the file the later turn changed is back', readFileSync(A_TS, 'utf8') === was);
  check('the conversation ends with the new words and their answer', says.join('|') === 'first ask|the middle one, changed' && lastB2.k === 'it' && lastB2.text.includes('the middle one, changed'), says);
  check('one session in the list: the new one, with the title and the pin; the old one archived as the version before',
    [...st.rows.values()].filter(s => !s.archived && (s.id === B || s.vers?.root === B)).length === 1 && st.row(B2).title === title && st.row(B2).pinned
    && st.row(B).archived && !st.row(B).pinned && JSON.stringify(st.row(B).vers?.at[`you:${midId}`]) === JSON.stringify([`${B}:${midId}`, 1]), { B2: st.row(B2), B: st.row(B) });
  check('it notes one quiet line of what happened, not the fork line', b2.some(i => i.k === 'note' && i.text === '改过这一句 · 之后改的 1 个文件回去了') && !b2.some(i => i.k === 'note' && /分叉/.test(i.text)),
    b2.filter(i => i.k === 'note'));
  await p.waitForFunction(id => document.querySelector('.row.is-on')?.dataset.id === id, B2, { timeout: 5000 });
  const after = await place();
  check('the page went to the new version, in the old one\'s place in the list', after.at === before.at && after.n === before.n, { before, after });
  const ver = youEl('the middle one, changed').locator('.m-ver');
  await ver.waitFor({ timeout: 5000 });
  check('‹ 2/2 › under the changed message', (await ver.locator('em').innerText()) === '2/2' && await ver.locator('[data-d="1"]').isDisabled() && !await ver.locator('[data-d="-1"]').isDisabled());
  await st.shot('m-ver-2');
  await ver.locator('[data-d="-1"]').click();
  await youEl('EDIT src/a.ts').first().waitFor({ timeout: 5000 });
  const oldVer = youEl('the middle one').locator('.m-ver');
  check('‹ steps to the version before: its answers and steps, 1/2, and a quiet line that the files stay as they are',
    await itEl('EDIT src/a.ts').count() === 1 && (await oldVer.locator('em').innerText()) === '1/2' && (await p.locator('.m-old').innerText()).includes('文件还是现在的样子')
    && readFileSync(A_TS, 'utf8') === was && await p.locator(`.row[data-id="${B}"]`).count() === 0);
  await st.shot('m-ver-1');
  await oldVer.locator('[data-d="1"]').click();
  await youEl('the middle one, changed').first().waitFor({ timeout: 5000 });
  check('› steps back to the new one', (await youEl('the middle one, changed').locator('.m-ver em').innerText()) === '2/2');
  // Writing in the version before makes it the current one again.
  await youEl('the middle one, changed').locator('.m-ver [data-d="-1"]').click();
  await youEl('EDIT src/a.ts').first().waitFor({ timeout: 5000 });
  await type('back to this one');
  await st.until('the old version answers', async () => settled(st.row(B)) && (await items(B)).some(i => i.k === 'it' && i.text.includes('back to this one')));
  await p.waitForFunction(id => document.querySelector('.row.is-on')?.dataset.id === id, B, { timeout: 5000 });
  check('writing in the version before brings it back into the list, with the pin, and archives the other', !st.row(B).archived && st.row(B).pinned && st.row(B2).archived && !st.row(B2).pinned
    && st.row(B).title === title && (await place()).at === before.at && await p.locator(`.row[data-id="${B2}"]`).count() === 0 && (await youEl('the middle one').locator('.m-ver em').innerText()) === '1/2');

  // ======================= the same on Codex: the conversation only =======================
  const X = await st.session('codex one', { agent: 'codex' });
  await st.send(X, 'codex two');
  await st.send(X, 'codex three');
  const HAND = path.join(st.repo, 'hand.txt');
  writeFileSync(HAND, 'changed by hand\n');
  await st.open(X);
  const two = youEl('codex two');
  await two.hover();
  await two.locator('[data-act="m-edit"]').click();
  await p.locator('.m-eta').waitFor();
  check('on Codex the box says only the conversation goes back', (await p.locator('.m-erow small').innerText()) === 'Codex 只回退对话，文件不动');
  await st.shot('m-edit-codex');
  await p.locator('.m-eta').fill('codex two, changed');
  await p.locator('.m-edit [data-act="m-ego"]').click();
  const X2 = await st.until('the Codex version', () => versionOf(X)?.id);
  await st.until('its answer', async () => settled(st.row(X2)) && (await items(X2)).some(i => i.k === 'it' && i.text.includes('codex two, changed')));
  const x2 = await items(X2), twoId = (await items(X)).find(i => i.k === 'you' && i.text === 'codex two').id;
  check('重发 on Codex: forked before that turn, the conversation ends with the new words and their answer, files untouched',
    st.codex().some(e => e.ev === 'request' && e.method === 'thread/fork' && e.params.threadId === X && e.params.beforeTurnId === twoId)
    && x2.filter(i => i.k === 'you').map(i => i.text).join('|') === 'codex one|codex two, changed' && x2.filter(i => i.k !== 'note').at(-1).text.includes('codex two, changed')
    && readFileSync(HAND, 'utf8') === 'changed by hand\n' && x2.some(i => i.k === 'note' && i.text === '改过这一句 · Codex 只回退对话，文件不动'), x2);
  check('one Codex session in the list, with the title; the old one archived',
    [...st.rows.values()].filter(s => !s.archived && (s.id === X || s.vers?.root === X)).length === 1 && st.row(X).archived && st.row(X2).title === st.row(X).title
    && st.codex().some(e => e.ev === 'request' && e.method === 'thread/name/set' && e.params.threadId === X2 && e.params.name === st.row(X).title));
  await p.waitForFunction(id => document.querySelector('.row.is-on')?.dataset.id === id, X2, { timeout: 5000 });
  await youEl('codex two, changed').locator('.m-ver [data-d="-1"]').click();
  await youEl('codex three').first().waitFor({ timeout: 5000 });
  check('‹ 1/2 › reaches the Codex version before, with its answers', await itEl('codex three').count() === 1 && (await youEl('codex two').locator('.m-ver em').innerText()) === '1/2');

  // ======================= 修改 while a turn runs: it stops first =======================
  const D = await st.session('quick start');
  await st.open(D);
  await st.call(`/sessions/${D}/send`, { text: 'SLOW down, SLOW down, SLOW down, this is wrong' });
  await st.until('working', () => st.row(D)?.st === 'work');
  const wrong = youEl('SLOW down, SLOW down, SLOW down, this is wrong');
  await wrong.waitFor();
  await wrong.hover();
  await wrong.locator('[data-act="m-edit"]').click();
  await p.waitForFunction(() => (document.querySelector('.m-erow small')?.textContent ?? '').startsWith('这一轮会停下'), null, { timeout: 5000 });
  await st.shot('m-edit-busy');
  await p.locator('.m-eta').fill('no hurry now');
  await p.locator('.m-eta').press('Control+Enter');
  const D2 = await st.until('the version after the stop', () => versionOf(D)?.id);
  await st.until('its answer', async () => settled(st.row(D2)) && (await items(D2)).some(i => i.k === 'it' && i.text.includes('no hurry now')));
  const log = st.claude(), slow = log.find(e => e.ev === 'turn' && e.text === 'SLOW down, SLOW down, SLOW down, this is wrong');
  const stopped = log.findIndex(e => e.ev === 'turn end' && e.uuid === slow.uuid), again = log.findIndex(e => e.ev === 'turn' && e.text === 'no hurry now');
  check('修改 while it works stops that turn first, then sends the new words', log[stopped]?.how === 'stop' && stopped < again && st.row(D).st !== 'work'
    && (await items(D2)).some(i => i.k === 'note' && i.text.startsWith('改过这一句 · 那一轮停下了')), { stopped: log[stopped], again });

  // The old message's picture goes with the new words.
  const png = `data:image/png;base64,${Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4a40000000049454e44ae426082', 'hex').toString('base64')}`;
  const G = await st.session('picture next');
  await st.send(G, 'look at this', { files: [{ name: 'a.png', url: png }] });
  const look = (await items(G)).find(i => i.k === 'you' && i.text === 'look at this');
  const G2 = (await st.call(`/sessions/${G}/edit`, { at: look.id, text: 'look at this again' })).id;
  await st.until('the picture version', async () => settled(st.row(G2)) && (await items(G2)).some(i => i.k === 'it' && i.text.includes('look at this again')));
  const picTurn = turns().find(t => t.text === 'look at this again'), withPic = (await items(G2)).find(i => i.k === 'you' && i.text === 'look at this again');
  check('the pictures of the changed message go with the new words', picTurn?.blocks.join() === 'image,text' && withPic?.files?.[0]?.name === 'a.png' && /\.png$/.test(withPic.files[0].img ?? ''), { picTurn, withPic });

  // ======================= 表情: kept on the host, ride the next message once (m-rx) =======================
  const E = await st.session('tell me about apples');
  await st.send(E, 'and pears too');
  await st.open(E);
  const nt = turns().length, apples = itEl('tell me about apples');
  await apples.hover();
  await apples.locator('[data-act="m-rx"]').click();
  const pop = p.locator('.pop.rxp');
  check('表情 opens the eight, and says it will not wake it', (await pop.locator('.rx-e').allInnerTexts()).join('') === '👍❤️😂🎉🤔👀🙏👎' && (await pop.locator('small').innerText()) === '不叫醒它 · 跟你下一句一起带过去');
  await st.shot('m-rx-pop');
  await pop.locator('.rx-e', { hasText: '👍' }).click();
  const pears = youEl('and pears too');
  await pears.locator('[data-act="m-rx"]').click();
  await pop.locator('.rx-e', { hasText: '❤️' }).click();
  await st.until('kept on the host', () => Object.values(st.row(E).rx ?? {}).flatMap(r => r.mine ?? []).length === 2);
  check('a reaction shows as a dashed chip, and never wakes the agent', await apples.locator('.rx-c.wait').innerText() === '👍' && await pears.locator('.rx-c.wait').innerText() === '❤️'
    && turns().length === nt && st.row(E).st === 'done');
  await st.shot('m-rx');
  await st.open(E);
  const saved = JSON.parse(readFileSync(path.join(st.tmp, 'root', 'agents', 'sessions.json'), 'utf8'));
  const kept = (Array.isArray(saved) ? saved : saved.sessions ?? Object.values(saved)).find(s => s.id === E)?.rx;
  check('they are still there after a reload, and saved with the session', await itEl('tell me about apples').locator('.rx-c.wait').count() === 1 && await youEl('and pears too').locator('.rx-c.wait').count() === 1
    && Object.values(kept ?? {}).flatMap(r => r.mine ?? []).sort().join() === ['👍', '❤️'].sort().join(), kept);
  await type('one more thing');
  await st.until('the next turn', () => turns().length > nt && settled(st.row(E)));
  await youEl('one more thing').locator('.m-ride').waitFor({ timeout: 5000 });
  const rode = turns().filter(t => t.text.includes('[Reactions:'));
  check('the next message carries them to the agent as one plain line in front of it', rode.length === 1 && rode[0].text.startsWith('[Reactions: 👍 on your reply "好的，做完了：tell me about apples')
    && rode[0].text.includes('❤️ on my message "and pears too"') && rode[0].text.endsWith(']\n\none more thing'), rode.map(t => t.text));
  const said = await youEl('one more thing').locator('.you').innerText(), ride = await youEl('one more thing').locator('.m-ride').innerText();
  await p.waitForFunction(() => !document.querySelector('.rx-c.wait'), null, { timeout: 5000 });
  check('that message shows 带上了 with them, not the line, and the chips are solid now', ride === '带上了 👍 ❤️' && said === 'one more thing' && await p.locator('.rx-c').count() === 2,
    { ride, chips: await p.locator('.rx-c').allInnerTexts() });
  await st.shot('m-rx-ride');
  await type('and that is all');
  await st.until('the turn after', () => turns().some(t => t.text === 'and that is all') && settled(st.row(E)));
  check('they ride exactly once', turns().filter(t => t.text.includes('[Reactions:')).length === 1);
  await st.open(E);
  check('带上了 is still there after a reload', (await youEl('one more thing').locator('.m-ride').innerText()) === '带上了 👍 ❤️');
  await youEl('and pears too').locator('.rx-c', { hasText: '❤️' }).click();
  await st.until('taken off on the host', () => !Object.values(st.row(E).rx ?? {}).some(r => r.mine?.includes('❤️')));
  check('clicking your chip takes it off', await youEl('and pears too').locator('.rx-c').count() === 0);

  // ======================= 👀 on what it took while working · ↑ takes a queued one back (m-eyes) =======================
  const F = await st.session('start here');
  await st.open(F);
  await st.call(`/sessions/${F}/send`, { text: 'SLOW down, SLOW down, take your time' });
  await st.until('working', () => st.row(F)?.st === 'work');
  await type('while you work');
  await st.until('queued', () => st.row(F)?.queue?.length === 1);
  const q = p.locator('.host > .conv .c-queue > .item').filter({ has: p.locator('.you.queued') });
  await q.waitFor();
  check('what you send while it works waits in the queue, with 拿回来改 and 撤回', await q.locator('[data-act="m-qedit"]').count() === 1 && await q.locator('[data-act="m-qdrop"]').count() === 1
    && (await q.locator('time.q').innerText()) === '排着');
  await st.shot('m-eyes-queued');
  await st.until('taken', () => Object.values(st.row(F).rx ?? {}).some(r => r.by === '👀'), 15000);
  await st.until('done', () => settled(st.row(F)) && !st.row(F).queue);
  const took = (await items(F)).find(i => i.k === 'you' && i.text === 'while you work');
  await youEl('while you work').locator('.rx-c.by').waitFor({ timeout: 5000 });
  check('when it takes a queued message the host marks it with its 👀 (Claude 看到了)', st.row(F).rx[`you:${took.id}`]?.by === '👀'
    && (await youEl('while you work').locator('.rx-c.by').getAttribute('data-tip')) === 'Claude 看到了' && (await youEl('while you work').locator('.rx-c.by').innerText()) === '👀');
  await st.shot('m-eyes');
  await st.call(`/sessions/${F}/send`, { text: 'SLOW down, SLOW once more' });
  await st.until('working', () => st.row(F)?.st === 'work');
  await type('take me back');
  await st.until('queued', () => st.row(F)?.queue?.length === 1);
  await p.locator('#msg').focus();
  await p.keyboard.press('ArrowUp');
  await st.until('taken back', () => !st.row(F).queue);
  await st.until('done', () => settled(st.row(F)));
  await sleep(300);
  check('↑ in an empty composer takes the newest queued message back to change, and it never reaches the agent',
    (await p.locator('#msg').inputValue()) === 'take me back' && !turns().some(t => t.text.includes('take me back')) && await p.locator('.you.queued').count() === 0);
  await p.fill('#msg', '');
  const cx = await st.call(`/sessions/${X2}/queue`, { action: 'cancel', text: 'x' });
  check('Codex cannot give one back, and says so', cx.status === 409 && JSON.stringify(cx).includes('Codex 收下就放进这一轮了，撤不回来'), cx);

  check('no errors on the page', !st.errors.length, st.errors);
  console.log(`\n${checks.length} checks passed`);
} catch (e) {
  console.error(e, '\n--- host ---\n', st.log().slice(-3000));
  process.exitCode = 1;
} finally { await st.close(); }
