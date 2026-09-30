// Run after npm run build. 回头 on the stage (scripts/agents-stage.mjs): /rewind and /fork from the list of what you
// said, the bar over selected words, a question on the side under its paragraph (asked again, 拖到右边, 收起), and the
// conversation as Markdown. SHOTS=<folder> keeps a screenshot of each state.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { stage } from './agents-stage.mjs';

const st = await stage();
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const p = st.page, ta = p.locator('#msg');
const notes = () => readFileSync(path.join(st.repo, 'notes.txt'), 'utf8');
const items = async id => (await st.call(`/sessions/${id}`)).items ?? [];
const yous = async id => (await items(id)).filter(i => i.k === 'you').map(i => i.text);
const pkOpen = () => p.evaluate(() => !document.querySelector('.bk-pk').hidden);
const pkHead = () => p.evaluate(() => document.querySelector('.bk-pk .bk-pk-h')?.textContent ?? '');
const rowsText = () => p.evaluate(() => [...document.querySelectorAll('.bk-pk .bk-r .bk-r-t')].map(e => e.textContent));
const selText = () => p.evaluate(() => document.querySelector('.bk-pk .bk-r.bk-sel .bk-r-t')?.textContent ?? '');
const shown = () => p.evaluate(() => document.querySelector('.m-head .h-t')?.textContent ?? '');
// The session a step made: the one that was not there before it (a fork keeps its source's start time, so not the newest).
const ids = () => new Set(st.rows.keys()), added = was => [...st.rows.keys()].find(id => !was.has(id));
// A command as it is typed: "/" opens the list (and loads it), Enter completes it, Enter runs it.
async function cmd(c, arg = '') {
  await ta.focus();
  await ta.fill('/');
  await p.waitForSelector('.c-menu.on');
  if (arg) { await ta.fill(`${c} ${arg}`); await p.waitForTimeout(80); await p.keyboard.press('Enter'); return; }
  await ta.fill(c);
  await st.until(`${c} in the list`, () => p.evaluate(x => document.querySelector('.c-menu.on button')?.dataset.v === x, c));
  await p.keyboard.press('Enter');
  await st.until(`${c} completed`, async () => (await ta.inputValue()) === `${c} `);
  await p.keyboard.press('Enter');
}
// Words selected with the mouse, in paragraph `k` of the last answer on screen.
async function selectIn(k = 0, answer = -1) {
  await st.until('the answer on screen', () => p.evaluate(([k, answer]) => !![...document.querySelectorAll('.c-items .item > .it')].at(answer)?.querySelectorAll(':scope > .md > p')[k], [k, answer]));
  const b = await p.evaluate(([k, answer]) => {
    const its = [...document.querySelectorAll('.c-items .item > .it')], it = its.at(answer), el = it.querySelectorAll(':scope > .md > p')[k];
    el.scrollIntoView({ block: 'center' });
    const r = document.createRange(); r.selectNodeContents(el); const q = r.getClientRects()[0];
    return { x: q.left + 2, y: q.top + q.height / 2, w: Math.min(q.width - 4, 120) };
  }, [k, answer]);
  await p.mouse.move(b.x, b.y); await p.mouse.down(); await p.mouse.move(b.x + b.w, b.y, { steps: 6 }); await p.mouse.up();
  await st.until('the bar over the selection', () => p.evaluate(() => !document.querySelector('.bk-bar').hidden));
}
const sideAsked = () => st.claude().filter(l => l.ev === 'side');
// The title's ··· menu (in the long exposure's head it shows while the pointer is over the title).
async function more() { await p.hover('.m-head .h-t'); await p.click('.m-head [data-act="menu"][data-v="more"]'); await p.waitForSelector('.pop [data-act="bk-fork"]'); }

try {
  await st.context.grantPermissions(['clipboard-read', 'clipboard-write']);
  // The window's save dialog, as the stage's stand-in for window.agents records it.
  await st.context.addInitScript(() => { window.agents.saveFile = (name, text) => { window.__agentsCalls.push(['saveFile', name, text]); return Promise.resolve(`/Users/allen/Downloads/${name}`); }; });

  // ---------- host: where the commands live ----------
  const a = await st.session('先看一下 README');
  await st.send(a, 'EDIT notes.txt 改笔记');
  await st.send(a, '再说一句');
  // Codex's turns take a moment (SLOW): a turn the stand-in ends at once can outrun the host's turn/start reply and
  // leave the row working (codex.ts send: s.ref after the turn already ended), which is not what is checked here.
  const cx = await st.session('codex 第一句 SLOW', { agent: 'codex' });
  await st.send(cx, 'codex 第二句 SLOW');
  const cc = (await st.call(`/commands?id=${a}`)).commands, xc = (await st.call(`/commands?id=${cx}`)).commands;
  check('Claude\'s commands carry /btw to the window\'s side question, and /fork says it starts before a sentence', cc.some(c => c[0] === '/btw' && c[2] === 'btw') && cc.some(c => c[0] === '/fork' && c[2] === 'fork' && c[1].includes('之前')));
  check('Codex\'s commands carry /rewind and /side to the window', xc.some(c => c[0] === '/rewind' && c[2] === 'rewind') && xc.some(c => c[0] === '/side' && c[2] === 'btw'));

  // ---------- bk-rewind: the list, the choice, then the conversation ends before it and the sentence is back ----------
  await st.open(a);
  const edited = notes();
  check('the session to go back in has changed notes.txt', edited.includes('edited by the stand-in'));
  await cmd('/rewind');
  await st.until('the list', pkOpen);
  check('/rewind opens the list of what you said above the composer, the latest picked', (await pkHead()) === '退回 · 退到哪一句之前' && (await rowsText()).join('|') === '先看一下 README|EDIT notes.txt 改笔记|再说一句' && (await selText()) === '再说一句');
  const metas = await p.evaluate(() => [...document.querySelectorAll('.bk-pk .bk-r-f')].map(e => e.textContent).join('|'));
  check('each sentence says what going back before it would put back: its files, or none', metas === '1 个文件 +1 −0|1 个文件 +1 −0|没改文件', metas);
  await p.keyboard.press('ArrowUp');
  check('↑ moves the pick', (await selText()) === 'EDIT notes.txt 改笔记');
  await st.shot('bk-rewind-list');
  await p.keyboard.press('Enter');
  await st.until('the choices', async () => (await pkHead()).startsWith('退回到你说'));
  await st.until('the files the host would put back', () => p.evaluate(() => document.querySelector('.bk-pk .bk-fl code')?.textContent === 'notes.txt'));
  check('⏎ goes on to what to take back: both, the conversation, the files, or not', (await pkHead()) === '退回到你说「EDIT notes.txt 改笔记」之前'
    && (await rowsText()).join('|') === '对话和文件都退|只退对话|只退文件|算了' && (await selText()) === '对话和文件都退', [await pkHead(), await rowsText(), await selText()]);
  check('the first choice lists the files that go back, with their lines', await p.evaluate(() => document.querySelector('.bk-pk .bk-fl').textContent.includes('+1 −0')));
  await st.shot('bk-rewind');
  await p.keyboard.press('Escape');
  check('esc goes back to the list, a second esc puts it away', (await pkHead()) === '退回 · 退到哪一句之前' && (await p.keyboard.press('Escape'), !(await pkOpen())));
  await cmd('/rewind');
  await st.until('the list again', pkOpen);
  await p.keyboard.press('ArrowUp'); await p.keyboard.press('Enter');
  await st.until('the choices again', async () => (await rowsText())[0] === '对话和文件都退');
  let was = ids();
  await p.keyboard.press('Enter');
  const b = await st.until('the session gone back', () => st.row(a)?.archived && added(was));
  await st.until('the page on it with the sentence back', async () => (await ta.inputValue()) === 'EDIT notes.txt 改笔记');
  check('both back: the files are as they were before that sentence', notes() === 'line one\nline two\n');
  check('the conversation ends before it: a session with the same name, the old one archived', (await yous(b)).join('|') === '先看一下 README' && st.row(b).title === st.row(a).title && st.row(a).archived && !st.row(b).archived);
  await st.until('the line on the page', async () => await p.locator('.c-items .note').filter({ hasText: '退回到你说「EDIT notes.txt 改笔记」之前' }).count() === 1);
  check('one quiet line says what went back', (await items(b)).some(i => i.k === 'note' && i.text === '退回到你说「EDIT notes.txt 改笔记」之前 · 对话和 1 个文件'));
  check('its text is back in the composer, the caret at the end', await p.evaluate(() => document.activeElement?.id === 'msg' && document.activeElement.selectionStart === document.activeElement.value.length));
  check('the plain fork line is not written when going back', !(await items(b)).some(i => i.k === 'note' && i.text.includes('分叉')));
  await st.shot('bk-rewind-after');

  // ---------- 只退对话 and 只退文件 ----------
  await p.keyboard.press('Enter');
  await st.until('the sentence sent again', async () => (await yous(b)).length === 2 && st.row(b).st === 'done');
  const again = notes();
  check('sent again, it runs again: the file is changed again', again.includes('edited by the stand-in'));
  await cmd('/rewind');
  await st.until('the list', pkOpen);
  await p.keyboard.press('Enter');
  await st.until('the choices', async () => (await rowsText())[1] === '只退对话');
  was = ids();
  await p.keyboard.press('2');
  const c = await st.until('gone back in the conversation only', () => st.row(b)?.archived && added(was));
  await st.until('the sentence back', async () => (await ta.inputValue()) === 'EDIT notes.txt 改笔记');
  check('只退对话: the conversation goes back under the same name, the file stays as it is', notes() === again && (await yous(c)).join('|') === '先看一下 README' && st.row(c).title === st.row(b).title
    && (await items(c)).some(i => i.k === 'note' && i.text === '退回到你说「EDIT notes.txt 改笔记」之前 · 只退了对话'));
  await p.keyboard.press('Enter');
  await st.until('sent once more', async () => (await yous(c)).length === 2 && st.row(c).st === 'done');
  const twice = notes();
  await cmd('/rewind');
  await st.until('the list', pkOpen);
  await p.keyboard.press('Enter');
  await st.until('the choices', async () => (await rowsText())[2] === '只退文件');
  await st.until('the files known', () => p.evaluate(() => !!document.querySelector('.bk-pk .bk-fl')));
  await p.keyboard.press('3');
  await st.until('the files back', () => notes() === again);
  await st.until('the list put away', async () => !(await pkOpen()));
  check('只退文件: the files go back, the conversation stays and nothing comes back to the composer', twice !== again && (await yous(c)).length === 2 && !st.row(c).archived && (await ta.inputValue()) === ''
    && (await items(c)).some(i => i.k === 'note' && i.text.startsWith('文件退回到了你发这一句之前的样子')));

  // ---------- back to before the very first sentence: it goes back when you send ----------
  const e = await st.session('最开始那句');
  await st.send(e, 'EDIT notes.txt 然后改');
  const before = notes();
  await p.evaluate(id => window.__agentsOpen(id), e);
  await st.until('on it', async () => (await shown()) === st.row(e).title);
  await cmd('/rewind');
  await st.until('the list', pkOpen);
  await p.keyboard.press('ArrowUp'); await p.keyboard.press('Enter');
  await st.until('the choices', async () => (await rowsText())[0] === '对话和文件都退');
  await p.keyboard.press('Enter');
  await st.until('the banner', () => p.evaluate(() => document.querySelector('.c-rows .bk-bn')?.textContent.startsWith('退回到最开头')));
  check('before the first sentence nothing is left, so the banner says it starts again when you send, the sentence in the composer', (await ta.inputValue()) === '最开始那句'
    && !st.row(e).archived && await p.evaluate(() => document.querySelector('.c-rows .bk-bn').textContent.includes('文件也退回去')));
  await ta.fill('最开始那句，换个说法');
  was = ids();
  await p.keyboard.press('Enter');
  const f = await st.until('the new start', () => st.row(e)?.archived && added(was));
  await st.until('its first turn', async () => (await yous(f)).length === 1 && st.row(f).st === 'done');
  check('sent: a session starts from that sentence, the files as they were, the old one archived', (await yous(f))[0] === '最开始那句，换个说法' && notes() !== before
    && !notes().endsWith('edited by the stand-in\nedited by the stand-in\nedited by the stand-in') && await p.evaluate(() => !document.querySelector('.c-rows .bk-bn')));

  // ---------- bk-rewind-codex: the file choices greyed with the reason; the conversation goes back ----------
  await p.evaluate(id => window.__agentsOpen(id), cx);
  await st.until('on the Codex session', async () => (await shown()) === st.row(cx).title);
  await cmd('/rewind');
  await st.until('the list', pkOpen);
  await p.keyboard.press('Enter');
  await st.until('the choices', async () => (await rowsText())[0] === '对话和文件都退');
  check('Codex: both file choices are greyed, the reason said, 只退对话 picked', await p.evaluate(() => [...document.querySelectorAll('.bk-pk .bk-r')].map(r => r.classList.contains('bk-off')).join()) === 'true,false,true,false'
    && await p.evaluate(() => document.querySelector('.bk-pk .bk-pk-w')?.textContent) === 'Codex 不记文件的检查点，只能退对话' && (await selText()) === '只退对话');
  await p.keyboard.press('1');
  check('a greyed choice does nothing when pressed', (await pkOpen()) && (await selText()) === '只退对话');
  await st.shot('bk-rewind-codex');
  was = ids();
  await p.keyboard.press('Enter');
  const h = await st.until('the Codex session gone back', () => st.row(cx)?.archived && added(was));
  await st.until('its sentence back', async () => (await ta.inputValue()) === 'codex 第二句 SLOW');
  check('Codex goes back in the conversation: the turns before it kept (thread/turns/list), the name kept', (await yous(h)).join('|') === 'codex 第一句 SLOW' && st.row(h).title === st.row(cx).title
    && (await items(h)).some(i => i.k === 'note' && i.text === '退回到你说「codex 第二句 SLOW」之前 · 只退了对话'));

  // ---------- bk-rewind-live: while a turn runs it asks first, inside the list ----------
  const i = await st.session('准备一下');
  await p.evaluate(id => window.__agentsOpen(id), i);
  await st.until('on it', async () => (await shown()) === st.row(i).title);
  const pre = notes();
  await st.call(`/sessions/${i}/send`, { text: 'EDIT notes.txt SLOW, SLOW, SLOW, SLOW 慢慢改' });
  await st.until('the edit made, the turn still running', () => notes() !== pre && st.row(i).st === 'work');
  await st.until('the page shows it', () => p.evaluate(() => [...document.querySelectorAll('.c-items .you')].at(-1)?.textContent === 'EDIT notes.txt SLOW, SLOW, SLOW, SLOW 慢慢改'));
  await cmd('/rewind');
  await st.until('the list', pkOpen);
  check('the sentence being worked on says so', await p.evaluate(() => document.querySelector('.bk-pk .bk-r.bk-sel .bk-r-live')?.textContent) === '在干活', [await p.evaluate(() => document.querySelector('.bk-pk').innerHTML), st.row(i).st]);
  await p.keyboard.press('Enter');
  await st.until('it asks first', async () => (await pkHead()) === '它还在干活，先打断它？');
  check('while it works it asks first: stop it, then choose', (await rowsText()).join('|') === '先打断它|算了' && await p.evaluate(() => document.querySelector('.bk-pk .bk-pk-p').textContent.includes('会停在半路')) && st.row(i).st === 'work');
  await st.shot('bk-rewind-live');
  await p.keyboard.press('Enter');
  await st.until('stopped', () => st.row(i).st !== 'work');
  await st.until('then the choices', async () => (await rowsText())[0] === '对话和文件都退');
  await st.until('the turn stopped in the stand-in', () => st.claude().some(l => l.ev === 'turn end' && l.how === 'stop'));
  check('⏎ 先打断它 stops the turn (not waited out) and goes on to the choices', (await pkHead()).startsWith('退回到你说'));
  await st.until('the files known', () => p.evaluate(() => !!document.querySelector('.bk-pk .bk-fl')));
  await p.keyboard.press('Enter');
  await st.until('gone back', () => st.row(i)?.archived);
  await st.until('the file back', () => notes() === pre);
  await st.until('the sentence back', async () => (await ta.inputValue()) === 'EDIT notes.txt SLOW, SLOW, SLOW, SLOW 慢慢改');
  check('then it goes back, the file included, the sentence in the composer', notes() === pre);
  await ta.fill('');

  // ---------- bk-fork: /fork and 分叉… from the title menu ----------
  const j = await st.session('分叉用的第一句');
  await st.send(j, '分叉用的第二句');
  await p.evaluate(id => window.__agentsOpen(id), j);
  await st.until('on it', async () => (await shown()) === st.row(j).title);
  await cmd('/fork');
  await st.until('the list', pkOpen);
  was = ids();
  check('/fork: the same list, the whole conversation at the end, picked', (await pkHead()) === '分叉 · 从哪一句之前分出去' && (await rowsText()).join('|') === '分叉用的第一句|分叉用的第二句|整段对话' && (await selText()) === '整段对话');
  await p.keyboard.press('ArrowUp');
  await st.shot('bk-fork');
  await p.keyboard.press('Enter');
  await st.until('the banner', () => p.evaluate(() => document.querySelector('.c-rows .bk-bn b')?.textContent === '从这一句分叉'));
  check('picking a sentence puts it in the composer under a banner, nothing forked yet', (await ta.inputValue()) === '分叉用的第二句' && !added(was));
  await st.shot('bk-fork-banner');
  await ta.fill('分叉用的第二句，换个问法');
  was = ids();
  await p.keyboard.press('Enter');
  const k = await st.until('the fork', () => added(was));
  await st.until('its turn', async () => (await yous(k)).length === 2 && st.row(k).st === 'done');
  check('sent: a new session from before that sentence with the new one; the original stays as it was', (await yous(k)).join('|') === '分叉用的第一句|分叉用的第二句，换个问法'
    && (await yous(j)).join('|') === '分叉用的第一句|分叉用的第二句' && !st.row(j).archived && (await items(k)).some(x => x.k === 'note' && x.text.includes('的这一句之前分叉')),
    [await yous(k), await yous(j), st.row(j).archived, (await items(k)).filter(x => x.k === 'note').map(x => x.text)]);
  await st.until('the page on the fork', async () => p.evaluate(() => [...document.querySelectorAll('.c-items .you')].at(-1)?.textContent === '分叉用的第二句，换个问法'));
  await more();
  check('the title menu has 分叉… and 导出成 Markdown…', await p.locator('.pop [data-act="bk-fork"]').count() === 1 && await p.locator('.pop [data-act="bk-export"]').count() === 1
    && await p.locator('.pop [data-act="fork"]').count() === 0);
  await p.click('.pop [data-act="bk-fork"]');
  await st.until('the list from the menu', pkOpen);
  await p.keyboard.press('Enter');
  await st.until('the banner', () => p.evaluate(() => document.querySelector('.c-rows .bk-bn b')?.textContent === '从现在分叉'));
  await p.keyboard.press('Escape');
  await st.until('the banner gone', () => p.evaluate(() => !document.querySelector('.c-rows .bk-bn')));
  check('the whole conversation: 从现在分叉, and esc takes the banner away', true);
  await more(); await p.click('.pop [data-act="bk-fork"]');
  await st.until('the list', pkOpen);
  await p.click('.bk-pk .bk-r.bk-now');
  await ta.fill('从现在接着问');
  was = ids();
  await p.keyboard.press('Enter');
  const l = await st.until('the whole fork', () => added(was));
  await st.until('its turn', async () => (await yous(l)).length === 3 && st.row(l).st === 'done');
  check('clicked, the whole conversation forks with what you send', (await yous(l)).join('|') === '分叉用的第一句|分叉用的第二句，换个问法|从现在接着问' && !st.row(k).archived);

  // ---------- m-sel and bk-btw: selected words, 引用 and 问一句 ----------
  const m = await st.session('侧问用的');
  await p.evaluate(id => window.__agentsOpen(id), m);
  await st.until('on it', async () => (await shown()) === st.row(m).title);
  await selectIn(0);
  check('selecting words in an answer shows the bar over them: 引用 · 问一句', await p.evaluate(() => [...document.querySelectorAll('.bk-bar button')].map(x => x.textContent).join('|')) === '引用|问一句');
  const barAbove = await p.evaluate(() => { const r = document.getSelection().getRangeAt(0).getBoundingClientRect(), bb = document.querySelector('.bk-bar').getBoundingClientRect(); return bb.bottom <= r.top; });
  check('the bar sits over the selection', barAbove);
  await st.shot('m-sel');
  const picked = await p.evaluate(() => document.getSelection().toString().trim());
  await p.click('.bk-bar [data-act="bk-quote"]');
  check('引用 quotes the words into the composer', (await ta.inputValue()) === `> ${picked}\n\n` && await p.evaluate(() => document.querySelector('.bk-bar').hidden));
  await ta.fill('');
  await selectIn(1);
  await p.click('.bk-bar [data-act="bk-ask"]');
  await st.until('the block', () => p.evaluate(() => !!document.querySelector('.bk-q')));
  check('问一句 opens a block right under the paragraph selected, the box ready', await p.evaluate(() => { const q = document.querySelector('.bk-q'); return q.previousElementSibling?.textContent === '第二段在这里。' && q.hasAttribute('data-x') && document.activeElement?.classList.contains('bk-qi'); }));
  const n0 = (await items(m)).length;
  await p.keyboard.type('这段是什么意思');
  await p.keyboard.press('Enter');
  await st.until('the answer', () => p.evaluate(() => document.querySelector('.bk-q .bk-qa-a')?.textContent === '侧答：这段是什么意思'));
  check('the side answer comes into the block, not into the conversation', (await items(m)).length === n0 && !(await yous(m)).includes('这段是什么意思')
    && sideAsked().at(-1)?.question === '这段是什么意思' && (sideAsked().at(-1).history ?? []).length === 0);
  await st.shot('bk-btw');

  // ---------- bk-btw-more: asked again with what was said, twelve lines at most, not stopping the turn ----------
  await st.until('the box kept its focus', () => p.evaluate(() => document.activeElement?.classList.contains('bk-qi')));
  await p.keyboard.type('再说说');
  await p.keyboard.press('Enter');
  await st.until('the second answer', () => p.evaluate(() => document.querySelectorAll('.bk-q .bk-qa-a').length === 2 && document.querySelectorAll('.bk-q .bk-qa-a')[1].textContent === '侧答：再说说'));
  check('asked again: the side talk so far goes with it', JSON.stringify(sideAsked().at(-1).history) === JSON.stringify([{ question: '这段是什么意思', response: '侧答：这段是什么意思' }]), sideAsked().at(-1));
  await st.call(`/sessions/${m}/send`, { text: 'SLOW 慢慢做' });
  await st.until('it works', () => st.row(m).st === 'work');
  const long = '这一段为什么这样写，'.repeat(40);
  await p.fill('.bk-q .bk-qi', long);
  await p.press('.bk-q .bk-qi', 'Enter');
  await st.until('the long answer', () => p.evaluate(() => document.querySelectorAll('.bk-q .bk-qa-a').length === 3 && !!document.querySelectorAll('.bk-q .bk-qa-a')[2].textContent));
  check('asked while it works, it is answered during the turn and the turn goes on', sideAsked().at(-1).during === true && st.row(m).st === 'work');
  const sc = await p.evaluate(() => { const s = document.querySelector('.bk-q .bk-q-s'); return { h: s.clientHeight, sh: s.scrollHeight, top: s.scrollTop, max: 13 * 1.65 * 12 }; });
  check('about twelve lines at most, scrolling inside, kept at the latest', sc.sh > sc.h && sc.h <= sc.max + 1 && sc.top > 0, sc);
  await st.shot('bk-btw-more');
  await st.until('the turn done', () => st.row(m).st === 'done', 8000);
  check('the turn was not interrupted', st.claude().some(x => x.ev === 'turn end' && x.how === 'ok' && st.claude().find(y => y.ev === 'turn' && y.uuid === x.uuid)?.text === 'SLOW 慢慢做'));
  check('the block is still there after the turn\'s answer came', await p.evaluate(() => document.querySelectorAll('.bk-q .bk-qa').length === 3));

  // ---------- bk-btw-mark: 收起 leaves a mark; the mark opens it again with everything ----------
  await p.click('.bk-q [data-act="bk-q-min"]');
  await st.until('folded', () => p.evaluate(() => !document.querySelector('.bk-q') && !!document.querySelector('.bk-mk')));
  check('收起 leaves a small mark by the paragraph, with how many were asked', await p.evaluate(() => { const k = document.querySelector('.bk-mk'); return k.closest('p')?.textContent.startsWith('第二段在这里。') && k.dataset.n === '3'; }));
  await st.shot('bk-btw-mark');
  await p.click('.bk-mk');
  await st.until('open again', () => p.evaluate(() => document.querySelectorAll('.bk-q .bk-qa').length === 3));
  check('clicking the mark opens it again with everything', await p.evaluate(() => document.querySelector('.bk-q .bk-qa-a').textContent === '侧答：这段是什么意思'));
  await p.press('.bk-q .bk-qi', 'Escape');
  await st.until('folded by esc', () => p.evaluate(() => !document.querySelector('.bk-q') && !!document.querySelector('.bk-mk')));
  check('esc in its box folds it too', true);
  await p.evaluate(id => window.__agentsOpen(id), l);
  await st.until('elsewhere', async () => (await shown()) === st.row(l).title);
  await p.evaluate(id => window.__agentsOpen(id), m);
  await st.until('back', async () => (await shown()) === st.row(m).title);
  check('kept per session: away and back, the mark is still there', await p.evaluate(() => document.querySelector('.bk-mk')?.dataset.n === '3'));

  // ---------- bk-btw-right: 拖到右边, by the button and by dragging ----------
  await p.click('.bk-mk');
  await st.until('open', () => p.evaluate(() => !!document.querySelector('.bk-q')));
  await p.click('.bk-q [data-act="bk-q-right"]');
  await st.until('on the right', () => p.evaluate(() => document.querySelectorAll('.pv-view .bk-sp .bk-qa').length === 3 && !document.querySelector('.bk-q')));
  await st.until('the mark lit', () => p.evaluate(() => !!document.querySelector('.c-items .bk-mk.bk-on')));
  check('拖到右边 moves it to the sheet, the paragraph keeps a lit mark', await p.evaluate(() => document.querySelector('.pv .sh b')?.textContent === '侧问' && document.querySelector('.pv .sh small')?.textContent === '不打断它 · 不进对话'));
  await p.waitForTimeout(600);
  await p.fill('.pv-view .bk-qi', '右边接着问');
  await p.press('.pv-view .bk-qi', 'Enter');
  await st.until('answered on the right', () => p.evaluate(() => [...document.querySelectorAll('.pv-view .bk-qa-a')].at(-1)?.textContent === '侧答：右边接着问'));
  check('the side talk goes on there, with what was said', sideAsked().at(-1).history.length === 3);
  await st.shot('bk-btw-right');
  await p.press('.pv-view .bk-qi', 'Escape');
  await st.until('the sheet away', () => p.evaluate(() => !document.querySelector('.pv-view .bk-sp') && !!document.querySelector('.bk-mk:not(.bk-on)')));
  check('esc there puts the sheet away; the mark stays, no longer lit', await p.evaluate(() => document.querySelector('.bk-mk').dataset.n === '4'));
  await p.click('.bk-mk');
  await st.until('open', () => p.evaluate(() => !!document.querySelector('.bk-q')));
  const hd = await p.evaluate(() => { const r = document.querySelector('.bk-q .bk-q-k').getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
  await p.mouse.move(hd.x, hd.y); await p.mouse.down(); await p.mouse.move(hd.x + 160, hd.y + 4, { steps: 10 }); await p.mouse.up();
  await st.until('dragged right', () => p.evaluate(() => document.querySelectorAll('.pv-view .bk-sp .bk-qa').length === 4));
  check('pulled right by its top row, it goes to the sheet too', true);
  await p.waitForTimeout(600);
  await p.press('.pv-view .bk-qi', 'Escape');
  await st.until('the sheet away', () => p.evaluate(() => !document.querySelector('.pv-view .bk-sp')));

  // ---------- /btw typed, and Codex's /side ----------
  await cmd('/btw', '最后那段说了什么');
  await st.until('asked under the latest answer', () => p.evaluate(() => [...document.querySelectorAll('.c-items .item > .it')].at(-1).querySelector('.bk-q .bk-qa-a')?.textContent === '侧答：最后那段说了什么'));
  check('/btw 问题 opens it under the latest answer and asks at once', await p.evaluate(() => [...document.querySelectorAll('.c-items .item > .it')].at(-1).querySelector('.bk-q').previousElementSibling?.textContent === '第二段在这里。')
    && !(await yous(m)).some(x => x.includes('最后那段')));
  await p.evaluate(id => window.__agentsOpen(id), h);
  await st.until('on Codex', async () => (await shown()) === st.row(h).title);
  await cmd('/side', '这是哪一步');
  await st.until('Codex answers on the side', () => p.evaluate(() => document.querySelector('.bk-q .bk-qa-a')?.textContent === '侧答：这是哪一步'), 20000);
  check('Codex\'s /side asks the same way', !(await yous(h)).some(x => x.includes('这是哪一步')));

  // ---------- bk-export: looked at first, then 复制 or 存成文件… ----------
  await p.evaluate(id => window.__agentsOpen(id), m);
  await st.until('on it', async () => (await shown()) === st.row(m).title);
  const md = await st.call(`/sessions/${m}/export`);
  await more(); await p.click('.pop [data-act="bk-export"]');
  await st.until('the sheet', () => p.evaluate(() => !document.querySelector('.bk-ex').hidden && !!document.querySelector('.bk-ex .bk-md h4')));
  check('导出成 Markdown… shows it first: the name, how long it is, the text drawn', await p.evaluate(() => document.querySelector('.bk-ex .sh b').textContent) === md.name
    && await p.evaluate(() => document.querySelector('.bk-ex .sh small').textContent) === `整段对话 · 2 轮 · ${md.text.trimEnd().split('\n').length} 行`
    && await p.evaluate(() => document.querySelector('.bk-ex .bk-md').textContent.includes('侧问用的')));
  await p.waitForTimeout(300);
  await st.shot('bk-export');
  await p.click('.bk-ex [data-act="bk-ex-copy"]');
  await st.until('copied', async () => (await p.evaluate(() => navigator.clipboard.readText())) === md.text);
  check('复制 puts the Markdown on the clipboard', await p.evaluate(() => document.querySelector('.toast').textContent.startsWith('复制了 Markdown')));
  await p.keyboard.press('Enter');
  await st.until('saved', () => p.evaluate(() => document.querySelector('.bk-ex').hidden));
  const saved = await p.evaluate(() => window.__agentsCalls.filter(x => x[0] === 'saveFile'));
  check('⏎ 存成文件… asks the window to save it (the save dialog), then says where', saved.length === 1 && saved[0][1] === md.name && saved[0][2] === md.text
    && await p.evaluate(() => document.querySelector('.toast').textContent) === `存到了 ~/Downloads/${md.name}`);
  await cmd('/export');
  await st.until('the sheet from /export', () => p.evaluate(() => !document.querySelector('.bk-ex').hidden));
  await p.keyboard.press('Escape');
  check('/export opens the same sheet; esc closes it without saving', await p.evaluate(() => document.querySelector('.bk-ex').hidden && window.__agentsCalls.filter(x => x[0] === 'saveFile').length === 1));

  check('no errors on the page', !st.errors.length, st.errors);
  console.log(`\n${checks.length} checks passed`);
} catch (e) {
  console.error(e, '\n--- host ---\n', st.log().slice(-3000));
  process.exitCode = 1;
} finally { await st.close(); }
