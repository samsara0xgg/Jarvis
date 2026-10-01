// Run after npm run build. The long exposure's sky on the stage (scripts/agents-stage.mjs): sessions with a history
// spread over two days, taken in from transcripts the way Claude Code writes them, and a live one on the stand-in agent.
// Its keys, ⌘K over what was said, the away line and the trails under the words. SHOTS=<folder> keeps a screenshot of
// each state.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdirSync, utimesSync, writeFileSync } from 'node:fs';
import { rm } from 'node:fs/promises';
import path from 'node:path';
import { stage } from './agents-stage.mjs';

const st = await stage({ viewport: { width: 1180, height: 740 } });
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const p = st.page;

// A transcript as Claude Code writes one, its turns [minutes ago, what you said, what it answered], then taken in.
async function history(title, turns) {
  const sid = randomUUID(), dir = path.join(st.HOME, '.claude', 'projects', st.repo.replace(/[^a-zA-Z0-9]/g, '-'));
  mkdirSync(dir, { recursive: true });
  let last = null; const lines = [];
  const put = (e, ago) => { const uuid = randomUUID(); lines.push(JSON.stringify({ parentUuid: last, isSidechain: false, userType: 'external', cwd: st.repo, sessionId: sid, version: '9.9.9', gitBranch: 'main', timestamp: new Date(Date.now() - ago * 60000).toISOString(), uuid, ...e })); last = uuid; };
  for (const [ago, you, it] of turns) {
    put({ type: 'user', message: { role: 'user', content: you } }, ago);
    put({ type: 'assistant', message: { id: `msg_${randomUUID().slice(0, 8)}`, type: 'message', role: 'assistant', model: 'fake-sonnet', content: [{ type: 'text', text: it }], stop_reason: 'end_turn', usage: { input_tokens: 10, output_tokens: 10 } } }, ago - 2);
  }
  lines.push(JSON.stringify({ type: 'ai-title', aiTitle: title, sessionId: sid }));
  const f = path.join(dir, `${sid}.jsonl`);
  writeFileSync(f, `${lines.join('\n')}\n`);
  const t = (Date.now() - turns.at(-1)[0] * 60000) / 1000; utimesSync(f, t, t);
  const r = await st.call('/import', { agent: 'claude', id: sid, cwd: st.repo, force: true });
  if (!r.id) throw new Error(`import: ${JSON.stringify(r)}`);
  return r.id;
}

// The page's clock can be moved on (being away without waiting), and stays moved across a reload. What the sky's
// canvas drew in its last whole frame is kept: each stroke's style, dash and points, and each text.
await st.context.addInitScript(() => {
  const real = Date.now.bind(Date); let shift = Number(localStorage.getItem('verify.shift')) || 0;
  window.__shift = ms => { shift = ms; localStorage.setItem('verify.shift', String(ms)); };
  Date.now = () => real() + shift;
  const P = CanvasRenderingContext2D.prototype, sky = c => c.canvas.classList?.contains('bw-cv');
  let frame = { strokes: [], texts: [], ticks: [] }, done = frame, subs = [];
  window.__frame = () => done;
  const wrap = (k, f) => { const o = P[k]; P[k] = function (...a) { if (sky(this)) f.apply(this, a); return o.apply(this, a); }; };
  wrap('clearRect', () => { done = frame; frame = { strokes: [], texts: [], ticks: [] }; });
  // Your points: the short white tick above each thing you said.
  wrap('fillRect', function (x, y, w, h) { if (w < 2 && h >= 6) frame.ticks.push([x + w / 2, y + h]); });
  wrap('beginPath', () => { subs = []; });
  wrap('moveTo', (x, y) => { subs.push([[x, y]]); });
  wrap('lineTo', (x, y) => { subs.at(-1)?.push([x, y]); });
  wrap('stroke', function () { frame.strokes.push({ style: String(this.strokeStyle), dash: this.getLineDash().join(','), subs: subs.map(s => s.slice()) }); });
  wrap('fillText', t => { frame.texts.push(String(t)); });
});

// What the sky shows: its rows (sessions and the lines under them), where you stand, the floating words.
const sky = () => p.evaluate(() => {
  const pop = document.querySelector('.bw-open'), field = document.querySelector('.bw-find');
  return {
    on: document.querySelector('#win').classList.contains('sky-on'),
    rows: [...document.querySelectorAll('.bw-rows .bw-row')].map(b => ({ text: b.textContent, sel: b.classList.contains('sel'), nm: b.classList.contains('nm'), x: b.dataset.x ?? '', id: b.dataset.session ?? '', off: b.disabled })),
    words: pop.hidden ? null : pop.textContent, q: pop.hidden ? null : pop.querySelector('.pp-q')?.textContent ?? null,
    n: pop.hidden ? null : pop.querySelector('.pp-t small')?.textContent ?? null, marks: pop.hidden ? [] : [...pop.querySelectorAll('mark')].map(m => m.textContent),
    field: !field.hidden, typing: document.activeElement === field.querySelector('input'), value: field.querySelector('input').value,
    pull: !document.querySelector('.bw-pull').hidden, composer: document.querySelector('#msg')?.value ?? '',
  };
});
const selected = s => s.rows.find(r => r.sel);
const press = async (key, n = 1, ms = 260) => { for (let i = 0; i < n; i++) { await p.keyboard.press(key); await p.waitForTimeout(ms); } };
// The conversation on screen: the message lit by a landing, its place, whether it is in view, what is marked in it.
const landed = () => p.evaluate(() => {
  const box = document.querySelector('.host .conv'), list = box?.querySelector('.c-items'), hit = list?.querySelector('.item.hit');
  if (!box || !hit) return null;
  const a = box.getBoundingClientRect(), b = (hit.querySelector('.you') ?? hit).getBoundingClientRect();
  return { index: [...list.children].indexOf(hit), you: hit.querySelector('.you')?.textContent ?? '', marks: [...hit.querySelectorAll('mark')].map(m => m.textContent),
    inView: b.top >= a.top - 1 && b.top < a.bottom - 20, atEnd: box.scrollTop >= box.scrollHeight - box.clientHeight - 40 };
});
const indexOfYou = text => p.evaluate(t => [...document.querySelectorAll('.host .conv .c-items > *')].findIndex(el => el.querySelector('.you')?.textContent.includes(t)), text);

try {
  // ---------- the sessions: two days of history, one archived, one live ----------
  await history('timesink daily narrative', [[2900, '把每天的叙事整理成一页', '整理好了，放在 docs/narrative.md。'], [1300, '加上周末那两天', '加上了。']]);
  const host = await history('companion host', [[1500, 'host 挂了以后 companion 要能自己重连', '加了重连：断开后 0.5、1、2、4 秒各试一次，之后每 10 秒一次。'], [300, '把日志也打出来', '好了，日志在 logs/host.log。']]);
  await history('9.24 mac alarm mode', [[700, '闹钟模式在 mac 上响两次', '查到了：通知和声音各响了一次，合成一次了。'], [180, '再确认一下锁屏时', '锁屏时也只响一次。'], [60, '好，收尾吧', '收好了。']]);
  await history('Notion plugin sign-in', [[400, 'Notion 插件登录总失败', '令牌过期后没刷新，改成过期前五分钟刷新。'], [120, '测一下', '测过了，没再掉线。']]);
  const said = Array.from({ length: 12 }, (_, k) => k === 3 ? '菜单栏那个图标在刘海后面看不见' : `第 ${k + 1} 轮：把菜单栏这一块再理一遍`);
  const menu = await history('menu bar', said.map((you, k) => [260 - k * 20, you,
    Array.from({ length: 4 }, (_, j) => `第 ${k + 1} 轮的第 ${j + 1} 段：菜单栏里的东西一项一项搬进了设置和面板，旧的入口留着一个版本再拿掉，省得有人找不到。`).join('\n\n')]));
  const tray = await history('tray theme', [[900, '托盘图标要跟着暗色模式变', '改好了：暗色模式下换成浅色的图标。']]);
  await st.call(`/sessions/${tray}/meta`, { archived: true });
  const live = await st.session('hello PLAN');
  await st.send(live, 'EDIT notes.txt');
  await st.open(live);

  // ---------- ⌥↑: the sky, its keys and the lines under the sessions ----------
  await p.locator('#msg').focus();
  await press('Alt+ArrowUp', 1, 1400);
  let s = await sky();
  check('⌥↑ opens the sky on the last thing you said to the session on screen, its words out', s.on && selected(s)?.id === live && !selected(s).nm && /^第 (\d+) \/ \1 句$/.test(s.n ?? '') && s.words?.includes('进去'), [s.n, s.rows]);
  await st.until('the day-old session resting', async () => (await sky()).rows.some(r => r.x === 'x:more'), 6000);
  s = await sky();
  check('under the sessions one line: ＋ 新会话, and at its end what rests behind it — the day-old session and the archived one', s.rows.some(r => r.x === 'x:new' && r.text === '＋ 新会话') && s.rows.some(r => r.x === 'x:more' && r.text === '还有 2 个 ›') && !s.rows.some(r => r.text.includes('timesink')), s.rows.map(r => r.text));
  await st.shot('sky');
  await press(' ');
  s = await sky();
  check('a space in the sky is a pause, not a word for the composer', s.on && s.composer === '', s.composer);
  await press('ArrowUp');
  s = await sky();
  check('↑ moves to the session above, onto what you said nearest that moment', selected(s)?.id === menu && !selected(s).nm && s.n === '第 12 / 12 句' && s.q === said[11], [selected(s), s.n, s.q]);
  await press('ArrowLeft', 2);
  s = await sky();
  check('← again reads the sentence before, one at a time', s.n === '第 10 / 12 句' && s.q === said[9], [s.n, s.q]);
  await press('ArrowRight', 2);
  s = await sky();
  check('→ reads forward to the newest sentence', s.n === '第 12 / 12 句', s.n);
  await press('ArrowRight');
  s = await sky();
  check('→ past the newest sentence stops on the name', selected(s)?.nm && s.words?.includes('现在'), s.words);
  await press('ArrowLeft', 9);
  s = await sky();
  check('stepping back reaches the fourth sentence', s.n === '第 4 / 12 句' && s.q === said[3], [s.n, s.q]);
  await press('Enter', 1, 900);
  s = await sky();
  let l = await landed();
  check('⏎ on a sentence goes in, and the conversation stands on that sentence, lit', !s.on && l?.inView && l.you === said[3] && !l.atEnd && l.index === await indexOfYou(said[3]), l);
  await st.shot('sky-landed');

  await press('Alt+ArrowUp', 1, 1200);
  for (let i = 0; i < 12 && selected(await sky())?.x !== 'x:new'; i++) await press('ArrowDown');
  s = await sky();
  check('↓ walks on past the sessions onto ＋ 新会话, where there are no words to read', selected(s)?.x === 'x:new' && s.words === null, selected(s));
  await press('ArrowDown');
  s = await sky();
  check('↓ again steps along the same line to what rests', selected(s)?.x === 'x:more', selected(s));
  await press('ArrowRight', 1, 400);
  s = await sky();
  check('→ on it opens the fold: the resting session as a row, and the archived one to take back', s.rows.some(r => r.x === 'x:more' && r.text === '收起') && s.rows.some(r => r.id && r.text.includes('timesink')) && s.rows.some(r => r.x === `x:a:${tray}` && r.text.includes('tray theme') && r.text.includes('拿回来')), s.rows.map(r => r.text));
  await press('ArrowDown'); await press('Enter', 1, 700);
  await st.until('tray taken back', () => st.row(tray)?.archived === false, 5000);
  s = await sky();
  check('⏎ on it takes it back, and the sky stands on it as a session', s.on && selected(s)?.id === tray && selected(s).nm, selected(s));
  const stops = s.rows.filter(r => !r.off).length, from = s.rows.findIndex(r => r.sel);
  await press('ArrowDown', stops - 1 - from);
  s = await sky();
  const lastLine = selected(s)?.text;
  await press('ArrowDown', 1, 500);
  check('↓ from the last line closes the sky', lastLine && !(await sky()).on, lastLine);
  await st.call(`/sessions/${tray}/meta`, { archived: true });
  await st.until('tray archived again', () => st.row(tray)?.archived === true, 5000);

  // ---------- the words' trails bend down once and stay down to the left ----------
  await p.locator('#msg').focus();
  await press('Alt+ArrowUp', 1, 1000);
  for (let i = 0; i < 8 && selected(await sky())?.id !== host; i++) await press('ArrowUp');
  await p.waitForTimeout(1600);
  s = await sky();
  const lens = await p.evaluate(() => {
    const cv = document.querySelector('.bw-cv').getBoundingClientRect(), pop = document.querySelector('.bw-open').getBoundingClientRect();
    const sel = [...document.querySelectorAll('.bw-rows .bw-row')].findIndex(b => b.classList.contains('sel'));
    return { left: pop.left - cv.left, right: pop.right - cv.left, sel, lines: window.__frame().strokes.filter(k => /196, 204, 238, 0\.07/.test(k.style)).map(k => k.subs[0]) };
  });
  // The rows below the one you stand on, whose trails reach back past the words.
  const below = lens.lines.filter(pts => pts.at(-1)[1] > 82 + lens.sel * 27 + 1 && pts[0][0] < lens.left);
  const once = below.map(pts => {
    const down = pts.slice(1).every((pt, k) => pt[1] <= pts[k][1] + .01), drop = pts[0][1] - pts.at(-1)[1];
    const straight = pts.filter(pt => pt[0] <= lens.right + 14).every(pt => Math.abs(pt[1] - pts[0][1]) < .5);
    return { down, drop: Math.round(drop), straight };
  });
  check('standing on a sentence, the trails under the words bend down once and stay straight to the left', s.n === '第 2 / 2 句' && below.length >= 2 && once.every(o => o.down && o.straight && o.drop > 30), { n: s.n, once });
  await st.shot('sky-lens');
  await press('Escape', 1, 600);

  // ---------- two days on one axis: it opens on the recent stretch, older time slides in, points never pile ----------
  await p.locator('#msg').focus();
  await press('Alt+ArrowUp', 1, 1400);
  const axis = () => p.evaluate(() => {
    const rows = new Map();
    for (const [x, y] of window.__frame().ticks) rows.set(Math.round(y), [...(rows.get(Math.round(y)) ?? []), x].sort((a, b) => a - b));
    const gaps = [...rows.values()].flatMap(xs => xs.slice(1).map((x, i) => x - xs[i]));
    return { closest: Math.min(...gaps), points: [...rows.values()].reduce((n, xs) => n + xs.length, 0),
      words: [...document.querySelectorAll('.bw-axis span')].filter(el => +el.style.opacity > .5).map(el => el.textContent) };
  });
  let ax = await axis();
  check('it opens on the recent stretch: the axis runs hours back to now, the two-day sessions do not squeeze it', ax.words.includes('4 小时前') && ax.words.includes('现在') && !ax.words.some(w => w.includes('天前')), ax.words);
  check('points on a row never pile: close ones merge, and no two stand closer than 7 px', ax.points >= 6 && ax.closest >= 7, ax);
  await p.mouse.move(400, 200);
  for (let i = 0; i < 12; i++) { await p.mouse.wheel(-90, 0); await p.waitForTimeout(30); }
  await p.waitForTimeout(500);
  ax = await axis();
  check('a sideways swipe slides the sky to older time in cells as wide', ax.words.some(w => w.includes('天前')) && !ax.words.includes('现在') && ax.closest >= 7, ax.words);
  s = await sky();
  check('slid back to its time, the resting session comes in under the rest, and stays while the sky is open', s.rows.findIndex(r => r.text.includes('timesink')) > s.rows.findIndex(r => r.id === live) && s.rows.some(r => r.x === 'x:more' && r.text === '还有 1 个 ›'), s.rows.map(r => r.text));
  for (let i = 0; i < 10 && !selected(await sky())?.text.includes('timesink'); i++) await press('ArrowDown');
  await press('ArrowLeft', 1, 1400);
  const needle = await p.evaluate(() => { const cv = document.querySelector('.bw-cv').getBoundingClientRect(), pop = document.querySelector('.bw-open').getBoundingClientRect(); return { left: pop.left - cv.left, right: pop.right - cv.left, width: cv.width }; });
  s = await sky();
  check('← to a sentence two days back slides the sky there, its words in view', s.n === '第 1 / 2 句' && needle.left >= 0 && needle.right <= needle.width - 250, { n: s.n, needle });
  await st.shot('sky-older');
  await press('Escape', 1, 600);

  // ---------- ⌘K: what you said, in every session ----------
  await p.locator('#msg').focus();
  await press('ControlOrMeta+k', 1, 1000);
  s = await sky();
  check('⌘K opens the sky with its field where 收起 stands, ready to type', s.on && s.field && s.typing && !s.pull, s);
  await p.keyboard.type('重连'); await p.waitForTimeout(1200);
  s = await sky();
  const sessions = s.rows.filter(r => r.id);
  check('what you type keeps only the sessions that said it, with how many of their sentences do', sessions.length === 1 && sessions[0].id === host && sessions[0].text.includes('1 处说过'), s.rows.map(r => r.text));
  check('a session found in what was said stands on that sentence, the needle there, the words lit', !selected(s).nm && s.n === '第 1 / 2 句' && s.q?.includes('要能自己重连') && s.marks.length >= 2 && s.marks.every(m => m === '重连'), [s.n, s.q, s.marks]);
  await st.shot('sky-find');
  await p.locator('.bw-find input').dispatchEvent('compositionstart');
  check('an input method starting in the field keeps the sky open', (await sky()).on);
  await press('Enter', 1, 900);
  l = await landed();
  check('⏎ goes in and the conversation stands on that sentence with what you typed marked', !(await sky()).on && l?.inView && l.you.includes('要能自己重连') && l.marks.includes('重连'), l);

  await press('ControlOrMeta+k', 1, 800);
  await p.keyboard.type('暗色模式');
  await st.until('the archived session found', async () => { const r = (await sky()).rows.find(r => r.id === tray); return r?.text.includes('1 处说过') && !r.nm; }, 6000);
  s = await sky();
  check('the search reaches an archived session this window had not read, and stands on its sentence', selected(s)?.id === tray && selected(s).text.includes('已归档') && s.q === '托盘图标要跟着暗色模式变', [selected(s), s.q]);
  await press('Escape', 1, 400);
  s = await sky();
  check('esc empties the field first, and the sessions come back, the day-old one resting again', s.on && s.value === '' && s.rows.filter(r => r.id).length >= 5 && s.rows.some(r => r.x === 'x:more'), s.rows.map(r => r.text));
  await p.keyboard.type('zzqx'); await p.waitForTimeout(700);
  s = await sky();
  check('nothing found says so in the sky', s.rows.length === 1 && s.rows[0].text === '没找到「zzqx」' && s.rows[0].off, s.rows.map(r => r.text));
  await press('Escape'); await press('Escape', 1, 500);
  check('esc on an empty field closes the sky', !(await sky()).on);

  // ---------- 离开线: away from the window a while, then back ----------
  const spans = () => p.evaluate(() => JSON.parse(localStorage.getItem('agents.away') ?? '[]'));
  await p.evaluate(() => { dispatchEvent(new Event('blur')); dispatchEvent(new Event('focus')); });
  check('a glance at another window is not being away', (await spans()).length === 0, await spans());
  await p.evaluate(() => { dispatchEvent(new Event('blur')); window.__shift(10 * 60000); dispatchEvent(new Event('focus')); });
  const kept = await spans();
  check('ten minutes away is kept as a span, from leaving to coming back', kept.length === 1 && Math.abs(kept[0].b - kept[0].a - 600000) < 5000, kept);
  await p.locator('#msg').focus();
  await press('Alt+ArrowUp', 1, 1600);
  const away = async () => p.evaluate(() => {
    const f = window.__frame(), dpr = document.querySelector('.bw-cv').width / document.querySelector('.bw-cv').getBoundingClientRect().width;
    const bracket = f.strokes.filter(k => /214, 224, 255, 0\.55/.test(k.style)).flatMap(k => k.subs);
    const xs = bracket.flat().map(pt => pt[0]), ys = bracket.flat().map(pt => pt[1]), x0 = Math.min(...xs), x1 = Math.max(...xs);
    // Between the first two rows (the second and third when the first is the one you stand on, whose band is lit), across
    // the span, away from the time guides: nothing may be drawn there.
    const guides = f.strokes.filter(k => /157, 180, 255, 0\.06/.test(k.style)).map(k => k.subs[0][0][0]);
    const sel = [...document.querySelectorAll('.bw-rows .bw-row[data-session]')].findIndex(b => b.classList.contains('sel')), gapY = 90 + (sel === 0 ? 27 : 0);
    const c = document.querySelector('.bw-cv').getContext('2d'); let tint = 0;
    for (let x = Math.ceil(x0 + 4); x < x1 - 4; x++) if (!guides.some(g => Math.abs(g - x) < 3)) tint = Math.max(tint, c.getImageData(Math.round(x * dpr), Math.round(gapY * dpr), 1, 1).data[3]);
    return { label: f.texts.find(t => t.startsWith('你不在')), x0, x1, top: Math.min(...ys), bottom: Math.max(...ys), tint };
  });
  let a = await away();
  check('the sky marks the span on its time axis as a bracket saying how long', a.label === '你不在 · 10 分' && a.x1 - a.x0 > 8 && a.bottom - a.top <= 6, a);
  check('the bracket covers none of the trails: between the rows the span stays clear', a.tint < 8, a);
  await st.shot('sky-away');
  await st.open(live);
  await p.locator('#msg').focus();
  await press('Alt+ArrowUp', 1, 1600);
  a = await away();
  check('the span is still there after a reload', a.label === '你不在 · 10 分', a);
  await press('Escape', 1, 500);

  // ---------- past fourteen sessions, the long-read ones rest behind one line ----------
  for (let k = 0; k < 10; k++) await history(`旧事 ${k + 1}`, [[3000 + k * 30, `第 ${k + 1} 件旧事`, '做完了。']]);
  // The host dates a session taken in by when it was taken in: three hours on, every one but the live one has rested.
  await p.evaluate(() => window.__shift(3 * 3600000));
  await st.open(live);
  await p.locator('#msg').focus();
  await press('Alt+ArrowUp', 1, 1200);
  s = await sky();
  const rest = s.rows.find(r => r.x === 'x:more'), before = s.rows.filter(r => r.id).length;
  check('past fourteen sessions the long-read ones rest behind the same line', /^还有 \d+ 个 ›$/.test(rest?.text ?? '') && !s.rows.some(r => r.text.includes('旧事')), s.rows.map(r => r.text));
  for (let i = 0; i < 20 && selected(await sky())?.x !== 'x:more'; i++) await press('ArrowDown', 1, 150);
  await press('ArrowRight', 1, 500);
  s = await sky();
  // The archived one counts behind the line too, and opens as a line of its own.
  const n = Number(rest.text.match(/\d+/)[0]) - 1;
  check('→ on it opens the fold, and folds it again from the same line', s.rows.filter(r => r.id).length === before + n && selected(s)?.text === '收起', s.rows.map(r => r.text));
  await press('Escape', 1, 400);

  check('no errors on the page', !st.errors.length, st.errors);
  console.log(`\n${checks.length} checks passed`);
} catch (e) {
  console.error(e, '\n--- host ---\n', st.log().slice(-3000));
  process.exitCode = 1;
} finally {
  // The host can still be writing its state as it stops: its folder then goes a moment later.
  await st.close().catch(async e => { if (e.code !== 'ENOTEMPTY') throw e; await new Promise(r => setTimeout(r, 800)); await rm(st.tmp, { recursive: true, force: true }); });
}
