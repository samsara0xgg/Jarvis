// Run after npm run build. The top right of the long exposure, on the stage (scripts/agents-stage.mjs): who waits on
// you, in queue.ts's one order, gathered by her (A · 落星排队, B · 北极星); clicking her and ⌥↓ go down it; pointing
// at her lists it; a right click puts one aside; her one line at a pause and on coming back; requests answered in the
// conversation with their keys; a stopped session's line. SHOTS=<folder> keeps a screenshot of each state.
import assert from 'node:assert/strict';
import { stage } from './agents-stage.mjs';

const st = await stage();
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const p = st.page, sleep = ms => p.waitForTimeout(ms);
const TOP = { x: 700, y: 0, width: 580, height: 260 }, LIST = { x: 780, y: 0, width: 500, height: 320 };
const title = () => p.locator('.m-head .h-t').innerText();
const on = async id => { await st.until(`on ${st.row(id)?.title}`, async () => (await title()) === st.row(id).title, 6000); return true; };
const shown = sel => p.evaluate(s => { const el = document.querySelector(s); return !!el && !el.hidden && el.getClientRects().length > 0; }, sel);
const since = t0 => st.claude().filter(l => l.at >= t0);
// Her list: point somewhere else first, so pointing at her is a fresh visit.
async function list() {
  await p.mouse.move(300, 420); await sleep(350);
  await p.locator('.bw-her').hover();
  await p.waitForSelector('.bw-hq:not([hidden])', { timeout: 3000 });
  return p.$$eval('.bw-hq .hq-r', els => els.map(e => ({ id: e.dataset.id, n: e.querySelector('.hq-n')?.textContent, kind: e.querySelector('.hq-g')?.className.replace('hq-g ', ''), t: e.querySelector('.hq-t b')?.textContent, when: e.querySelector('em')?.textContent })));
}
const away = async () => { await p.mouse.move(300, 420); await sleep(700); };
const quiet = async () => { if (await shown('.bw-say')) await p.click('.bw-say .say-x'); };
// How much the canvas holds: the sum of its alpha.
const ink = sel => p.evaluate(s => { const c = document.querySelector(s); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let a = 0; for (let i = 3; i < d.length; i += 4) a += d[i]; return a; }, sel);

try {
  // ---------- six sessions, five of them waiting on you in one way or another ----------
  const home = await st.session('hello PLAN');
  const edit = await st.session('work EDIT notes.txt');   // finished, not read
  const ask = await st.session('please ASK');             // a shell command to allow
  const fail = await st.session('this will FAIL');        // stopped
  const pick = await st.session('choose PICK');           // a question with three options
  const ask2 = await st.session('second ASK');
  const pick2 = await st.session('third PICK');
  await st.open(home);
  await sleep(2200);

  // ---------- what went ----------
  check('the deck, the offer, her held pill and the layout toggle are gone from the page',
    await p.locator('.bw-deck, .dk-card, .bw-offer, .ex-held, .c-held, .ex-mode').count() === 0);

  // ---------- the order, the line (A) and the list ----------
  const want = [ask, pick, ask2, pick2, fail, edit];
  const L = await list();
  await st.shot('q-list', LIST);
  check('the queue runs asks, then errors, then finished ones not yet read, the longest waiting first',
    JSON.stringify(L.map(r => r.id)) === JSON.stringify(want), L.map(r => r.t));
  check('pointing at her lists each one numbered, with its kind, title and how long it has waited',
    L.map(r => r.n).join() === '1,2,3,4,5,6' && L.map(r => r.kind).join() === 'q-ask,q-ask,q-ask,q-ask,q-err,q-done'
    && L.every((r, i) => r.t === st.row(want[i]).title && r.when === '刚刚'), L);
  await away();
  const boxes = await Promise.all(want.map(id => p.locator(`.bw-stars [data-session="${id}"]`).boundingBox()));
  check('A lines the waiting stars up left of her, the next one nearest her, a little below the horizon',
    boxes.every(Boolean) && boxes.every((b, i) => i === 0 || b.x < boxes[i - 1].x) && boxes.every(b => Math.abs(b.y - boxes[0].y) < 1)
    && boxes[0].x > 1180 && (await p.locator(`.bw-stars [data-session="${home}"]`).boundingBox()).y < boxes[0].y, boxes.map(b => b && [b.x, b.y]));
  await st.shot('q-a', TOP);

  // ---------- 先放着 ----------
  await list();
  await p.locator(`.bw-hq .hq-r[data-id="${edit}"]`).click({ button: 'right' });
  await p.waitForSelector('.pop.on [data-q="park"]', { timeout: 3000 });
  await st.shot('q-park', { ...LIST, height: 460 });
  await p.click('.pop.on [data-q="park"]');
  await st.until('parked on the host', () => st.row(edit)?.parked === true, 5000);
  const L2 = await list();
  check('a right click puts one aside: the host keeps it parked and it leaves the queue',
    !L2.some(r => r.id === edit) && L2.length === 5 && await p.locator(`.bw-stars [data-session="${edit}"]`).count() === 1, L2.map(r => r.t));
  await away();

  // ---------- B · 北极星, chosen on her right-click menu ----------
  await p.locator('.bw-her').click({ button: 'right' });
  await p.waitForSelector('.pop.on [data-q="look"]', { timeout: 3000 });
  const menu = await p.$$eval('.pop.on [data-q="look"]', els => els.map(e => `${e.textContent}${e.classList.contains('on') ? '✓' : ''}`));
  await p.click('.pop.on [data-q="look"][data-v="B"]');
  await sleep(1800);
  const ringInk = await ink('.bw-qb');
  await st.shot('q-b', TOP);
  check('her right-click menu offers 落星排队 (current) and 北极星; B draws the queue as rings round her and is remembered',
    menu.join() === '落星排队✓,北极星' && ringInk > 20000 && await p.evaluate(() => localStorage.getItem('agents.queue')) === 'B'
    && await p.locator(`.bw-stars [data-session="${ask}"]`).count() === 0, { menu, ringInk });
  await st.open(home);
  await sleep(1500);
  const kept = await p.evaluate(() => localStorage.getItem('agents.queue')) === 'B' && await ink('.bw-qb') > 20000;
  await p.locator('.bw-her').click({ button: 'right' });
  await p.click('.pop.on [data-q="look"][data-v="A"]');
  await sleep(600);
  check('B survives a reload, and A takes the rings away again', kept && await ink('.bw-qb') === 0 && await p.evaluate(() => localStorage.getItem('agents.queue')) === 'A');
  await away();

  // ---------- the long exposure's rows carry the numbers ----------
  await p.focus('#msg');
  await p.keyboard.press('Alt+ArrowUp');
  await sleep(1400);
  const badges = await p.$$eval('.bw-row', els => els.map(e => [e.dataset.session, e.querySelector('.qn')?.textContent ?? '']));
  await st.shot('q-sky');
  const num = Object.fromEntries(badges);
  check('the long exposure numbers its rows in the same order, and none on ones that do not wait',
    num[ask] === '1' && num[pick] === '2' && num[ask2] === '3' && num[pick2] === '4' && num[fail] === '5' && num[edit] === '' && num[home] === '', badges);
  await p.keyboard.press('Escape');
  await sleep(900);

  // ---------- her one line at a pause: you just sent ----------
  await p.focus('#msg');
  await p.keyboard.type('more PLAN');
  await p.keyboard.press('Enter');
  await p.waitForSelector('.bw-say:not([hidden])', { timeout: 9000 });
  await sleep(300);
  const say = await p.locator('.bw-say').innerText();
  await st.shot('q-say', TOP);
  check('when you pause after sending, she says how many wait and which is first', say.includes('5 个等你') && say.includes(st.row(ask).title), say);
  await p.click('.bw-say .say-go');
  check('clicking her line goes to that one', await on(ask) && !await shown('.bw-say'));

  // ---------- requests, in the conversation, with their keys ----------
  await sleep(600);
  const card = await p.locator('.conv .req').count() > 0 && (await p.locator('.conv .req').last().innerText()).includes('esc');
  await st.shot('req');
  let t0 = Date.now();
  await quiet(); await p.focus('#msg'); await p.keyboard.press('Enter');
  await st.until('allowed', () => since(t0).some(l => l.ev === 'permission' && l.behavior === 'allow'), 6000);
  await st.until('ask done', () => st.row(ask).st !== 'wait', 6000);
  check('a request stands in the conversation and ⏎ allows it', card);

  await away();
  await p.locator('.bw-her').click();
  check('clicking her opens the next one waiting, in order', await on(pick));
  await sleep(600);
  const opts = await p.$$eval('.conv .req.ask .opts .opt i', els => els.map(e => e.textContent));
  await st.shot('ask');
  t0 = Date.now();
  await quiet(); await p.focus('#msg'); await p.keyboard.press('2');
  const q1 = await st.until('picked', () => since(t0).find(l => l.ev === 'question'), 6000);
  check('a question numbers its options and a digit answers it', opts.join() === '1,2,3' && q1.behavior === 'allow' && JSON.stringify(q1.answers).includes('Green'), q1);

  await away();
  await p.locator('.bw-her').click();
  await on(ask2);
  await sleep(600);
  t0 = Date.now();
  await quiet(); await p.focus('#msg'); await p.keyboard.press('Escape');
  await st.until('denied', () => since(t0).some(l => l.ev === 'permission' && l.behavior === 'deny'), 6000);
  check('esc says no to a request', true);

  await p.keyboard.press('Alt+ArrowDown');
  check('⌥↓ goes to the next one too', await on(pick2));
  await sleep(600);
  t0 = Date.now();
  await quiet(); await p.focus('#msg'); await p.keyboard.type('Purple, please'); await p.keyboard.press('Enter');
  const q2 = await st.until('typed answer', () => since(t0).find(l => l.ev === 'question'), 6000);
  check('typing answers a question', q2.behavior === 'allow' && JSON.stringify(q2.answers).includes('Purple, please'), q2);

  // ---------- a stopped one: its line offers to carry on, or the log ----------
  await p.keyboard.press('Alt+ArrowDown');
  await on(fail);
  await sleep(500);
  const stop = await p.locator('.c-stop').innerText().catch(() => '');
  await st.shot('err');
  await p.click('.c-stop [data-act="stoplog"]');
  await st.until('the log pane', async () => (await p.locator('.lg-h [aria-pressed="true"]').innerText().catch(() => '')) === '后台', 5000);
  check('a stopped one says why and 在终端里看 opens the terminal on the host\'s log', stop.includes('出错了') && stop.includes('让它接着来'), stop);
  await p.keyboard.press('Control+Backquote');
  await sleep(500);
  t0 = Date.now();
  await p.click('.c-stop [data-act="stopgo"]');
  await st.until('resumed', () => since(t0).some(l => l.ev === 'turn' && /接着来/.test(l.text ?? '')), 6000);
  await st.until('no longer stopped', () => st.row(fail).st !== 'err', 8000);
  await sleep(600);
  check('让它接着来 sends it on, and the line goes once it is running again', await p.locator('.c-stop').count() === 0);

  // ---------- a new one falls into the line ----------
  await st.open(home);
  await sleep(1500);
  // one already on the horizon asks again: it falls from its place there to the head of the line
  const late = ask, star = () => p.locator(`.bw-stars [data-session="${late}"]`).boundingBox();
  const from = await star();
  await st.call(`/sessions/${late}/send`, { text: 'one more ASK' });
  await st.until('late waits', () => st.row(late)?.st === 'wait', 6000);
  for (const [k, ms] of [[1, 60], [2, 180], [3, 200], [4, 1600]]) { await sleep(ms); await st.shot(`q-new-${k}`, TOP); }
  const to = await star();
  check('one that comes to wait leaves its place on the horizon for the head of the line',
    from.y < to.y && to.x > from.x && to.x > 1180, [from, to]);

  // ---------- coming back after a while: what happened meanwhile ----------
  await quiet();
  await p.evaluate(() => window.dispatchEvent(new Event('blur')));
  const a1 = await st.session('away one');
  const a2 = await st.session('away FAIL');
  const a3 = await st.session('away ASK');
  await sleep(800);
  // A minute and more later, by the page's clock.
  await p.evaluate(() => { const now = Date.now.bind(Date); Date.now = () => now() + 61000; });
  await p.evaluate(() => window.dispatchEvent(new Event('focus')));
  await p.waitForSelector('.bw-say:not([hidden])', { timeout: 4000 });
  await sleep(300);
  const back = await p.locator('.bw-say').innerText(), first = await p.locator('.bw-say .say-f em').innerText();
  await st.shot('back', TOP);
  check('back after a minute away, her line says what happened meanwhile and names the first waiting',
    back.includes('你不在时 1 个做完 · 1 个出错 · 1 个要你批') && first === st.row(late).title, back);
  await p.click('.bw-say .say-go');
  check('and a click on it goes there', await on(late) && [a1, a2, a3].every(id => st.row(id)));

  check('no errors on the page', !st.errors.length, st.errors);
  console.log(`\n${checks.length} checks passed`);
} catch (e) {
  console.error(e, '\n--- host ---\n', st.log().slice(-3000));
  process.exitCode = 1;
} finally { await st.close(); }
