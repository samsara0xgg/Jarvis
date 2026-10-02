// The talk area under her (对话区), in headless Chrome against the built page. Run after `npm run build`.
// A fake daemon (routed HTTP plus a fake WebSocket, as in verify-companion-live) drives every turn: nothing is spoken, no model
// is called, nothing is written to the real history. Dates are skewed from the page (`window.__skew`) to reach the 3 s, 8 s and
// 10 min rules and the follow-along without waiting, except where the real 8 s is the thing being checked.
// Screenshots land in evidence/talk-area/; `--frames` also records every screencast frame around the transitions.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.TALK_EVIDENCE_DIR ?? path.join(root, 'evidence/talk-area');
mkdirSync(dir, { recursive: true });
const frames = process.argv.includes('--frames');
const web = Number(process.env.COMPANION_PORT ?? 5193), daemon = 8798;
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
// TALK_KEEP_GOING=1: a failed check is printed and the run goes on (exit 1 at the end), to see every result past a known-flaky one.
const checks = [], failed = [], check = (name, pass) => {
  if (!pass && process.env.TALK_KEEP_GOING) { failed.push(name); console.log(`FAIL ${name}`); return; }
  assert.ok(pass, name); checks.push(name); console.log(`PASS ${name}`);
};
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security'] });
const out = { x: 195.5, y: 72 }; // where she stands out of the island in the 640 px window

// One page per scenario: its own settings, a fresh fake daemon.
async function scene({ captions = 'brief', lang = 'en', reduced = false, demo = false } = {}) {
  const context = await browser.newContext({ viewport: { width: 640, height: 722 }, deviceScaleFactor: 2, reducedMotion: reduced ? 'reduce' : 'no-preference' });
  const page = await context.newPage();
  const errors = [], posts = [];
  page.on('pageerror', e => errors.push(e.message));
  const daemonState = { controls: { mic_muted: false, speech_muted: false, conversation: false }, typed: 0, think: { on: false, on_words: '深想', turn_id: null } };
  await page.route(`http://127.0.0.1:${daemon}/**`, async route => {
    const url = new URL(route.request().url()), method = route.request().method();
    const body = method === 'POST' ? JSON.parse(route.request().postData() || '{}') : null;
    const json = v => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(v), headers: { 'access-control-allow-origin': '*' } });
    if (method === 'POST') posts.push({ path: url.pathname, body });
    if (url.pathname === '/inherent/controls') { daemonState.controls = { ...daemonState.controls, ...body }; return json(daemonState.controls); }
    if (url.pathname === '/inherent/submit') return json({ turn_id: `typed-${++daemonState.typed}` });
    if (url.pathname === '/inherent/cancel-response') return json({});
    if (url.pathname === '/inherent/think') return json(daemonState.think);
    if (url.pathname === '/inherent/language') return json({ language: lang });
    if (url.pathname === '/inherent/confirmation' || url.pathname === '/inherent/clarification') return json({ card: null });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  await page.addInitScript(([captions, lang]) => {
    try { if (!localStorage.getItem('companion-settings-v1')) localStorage.setItem('companion-settings-v1', JSON.stringify({ lang, captions })); } catch { /* private window */ } // (once: a reload keeps what was chosen)
    const real = Date.now.bind(Date);
    window.__skew = 0;
    Date.now = () => real() + window.__skew;
    window.__state = { passthrough: true, glass: [], focus: null };
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: cb => { window.__cursor = cb; return () => {}; }, onCommand: cb => { window.__command = cb; return () => {}; },
      passthrough: v => { window.__state.passthrough = v; }, focus: async on => { window.__state.focus = on; }, material: rects => { window.__state.glass = rects; },
    };
    window.__sockets = [];
    window.WebSocket = class { constructor(url) { this.url = url; window.__sockets.push(this); setTimeout(() => this.onopen?.(), 0); } send() {} close() { this.onclose?.(); } };
    window.__emit = (op, payload) => window.__sockets.at(-1).onmessage({ data: JSON.stringify({ op, payload }) });
  }, [captions, lang]);
  await page.goto(`http://127.0.0.1:${web}/?companion=1${demo ? '' : `&port=${daemon}`}`);
  await page.addStyleTag({ content: `html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}
    body::before{content:'';position:fixed;inset:0 0 auto;height:32px;background:rgb(255 255 255/.18)}
    body::after{content:'';position:fixed;z-index:10;pointer-events:none;top:0;left:227.5px;width:185px;height:32px;background:#000;border-radius:0 0 10px 10px}` });
  if (!demo) await page.waitForFunction(() => window.__sockets?.length === 1);
  // (After `listening` the area opens and stamps the session's start: words heard before that stamp belong to an earlier session, so it is given a moment.)
  const emit = async (op, payload) => { await page.evaluate(([op, payload]) => window.__emit(op, payload), [op, payload]); if (op === 'voice' && payload.phase === 'listening') await page.waitForTimeout(150); };
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  const skew = ms => page.evaluate(ms => { window.__skew += ms; }, ms);
  const shot = (name, clip = { x: 10, y: 20, width: 380, height: 460 }) => page.screenshot({ path: path.join(dir, `${name}.png`), clip });
  // Everything the checks read from the area, in one go.
  const area = () => page.evaluate(() => {
    const t = document.querySelector('.talk'), r = t.getBoundingClientRect(), last = sel => [...t.querySelectorAll(sel)].at(-1), tr = t.querySelector('.talk-tr');
    return { up: t.hasAttribute('data-hit'), kind: t.dataset.kind, state: t.dataset.state, deep: t.hasAttribute('data-deep'), vis: getComputedStyle(t).visibility,
      r: { x: r.x, y: r.y, w: r.width, h: r.height, right: r.right }, label: t.querySelector('.lb')?.textContent ?? '', you: last('.tk-u')?.textContent ?? '', her: last('.tk-h .tk-s')?.textContent ?? '',
      err: last('.tk-h.is-err .tk-s')?.textContent ?? '', yous: t.querySelectorAll('.tk-u').length, hers: t.querySelectorAll('.tk-s').length, rows: t.querySelectorAll('.doc > div').length,
      lit: t.querySelectorAll('.tk-s i.on').length, all: t.querySelectorAll('.tk-s.all').length, think: last('.tk-think')?.textContent ?? '', footer: t.querySelector('.talk-ft')?.textContent ?? '',
      tr: tr && { top: tr.scrollTop, client: tr.clientHeight, scroll: tr.scrollHeight, end: tr.scrollHeight - tr.scrollTop - tr.clientHeight < 3, youIn: !!last('.tk-u') && last('.tk-u').getBoundingClientRect().top >= tr.getBoundingClientRect().top - 1 && last('.tk-u').getBoundingClientRect().bottom <= tr.getBoundingClientRect().bottom + 1 }, fieldShown: !t.querySelector('.talk-fd').hidden };
  });
  const settled = () => page.waitForTimeout(900); // a spring has come to rest
  const turn = async (id, heard, said, { done = true, spoken = false } = {}) => {
    await emit('voice', { phase: 'listening', turn_id: id }); await emit('voice', { phase: 'accepted', turn_id: id, text: heard });
    await emit('open', { turn_id: id, response_id: `r-${id}` });
    for (const token of [].concat(said)) await emit('append', { turn_id: id, token });
    if (done) await emit('done', { turn_id: id, fadeMs: 100 });
    if (spoken) await emit('voice', { phase: 'spoken', turn_id: id });
  };
  const folded = (timeout = 20_000) => page.waitForFunction(() => !document.querySelector('.talk').hasAttribute('data-hit'), null, { timeout });
  const rec = frames ? await record(page) : null;
  return { page, context, emit, move, skew, shot, area, settled, turn, folded, posts, errors, daemonState, rec };
}

// CDP screencast: every frame, jpeg, with its time (for frame sheets of the transitions).
async function record(page) {
  const cdp = await page.context().newCDPSession(page), list = [];
  cdp.on('Page.screencastFrame', f => { list.push({ t: f.metadata.timestamp * 1000, data: f.data }); cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }).catch(() => {}); });
  await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 90, everyNthFrame: 1 });
  return { list, stop: async () => { await cdp.send('Page.stopScreencast').catch(() => {}); return list; } };
}
const sheet = async (list, name, crop, cols = 8) => {
  const t0 = list[0]?.t ?? 0, page = await browser.newPage({ viewport: { width: crop.width * cols + 8 * (cols + 1), height: 400 } });
  await page.setContent(`<style>body{margin:0;background:#222;display:grid;grid-template-columns:repeat(${cols},auto);gap:8px;padding:8px;justify-content:start}figure{margin:0}figcaption{font:11px monospace;color:#ccc}</style>`
    + list.map((f, i) => `<figure><div style="width:${crop.width}px;height:${crop.height}px;overflow:hidden;position:relative"><img src="data:image/jpeg;base64,${f.data}" style="position:absolute;left:${-crop.x}px;top:${-crop.y}px;width:640px"></div><figcaption>${i} · ${Math.round(f.t - t0)} ms</figcaption></figure>`).join(''));
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(dir, name), fullPage: true });
  await page.close();
};

try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const long = '好的，我把这件事从头到尾说清楚。第一，今天下午三点之后的三场会议我都已经往后推了一个小时，对应的日历邀请也已经更新。第二，我给王老师发了一条消息，说明了改期的原因，并且问他明天上午是否方便再约一次。第三，周五的评审会议和你的健身课时间冲突了，我没有擅自改动，等你决定。第四，下周一的飞机票价格上涨了大约一成，如果你还想订的话，最好今天就定下来，我可以随时帮你处理。另外，第一，今天下午三点之后的三场会议我都已经往后推了一个小时，对应的日历邀请也已经更新。第二，我给王老师发了一条消息。第三，周五的评审会议和你的健身课时间冲突了，我没有擅自改动，等你决定。第四，下周一的飞机票价格上涨了大约一成，如果你还想订的话，最好今天就定下来。';
  const written = '<voice>今天有三件事，我列在下面了。</voice><document>### 今天\n- 10:00 和 Anna 的产品会 · 3F 会议室\n- 14:30 评审会议 · https://zoom.us/j/123\n- 17:00 健身课\n\n**提醒**：明早九点交报告。</document>';

  // ---- everything: the area, its states, its words ----
  {
    const s = await scene({ captions: 'all' });
    const { page, emit, move, skew, shot, area, settled, turn, folded, posts } = s;
    await page.waitForTimeout(600);
    let a = await area();
    check('idle: nothing is up, nothing blocks the desktop', !a.up && a.vis === 'hidden' && await page.evaluate(() => window.__state.passthrough === true && window.__state.glass.length === 0));

    // appears when she starts listening
    await emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); s.daemonState.controls.conversation = true;
    await emit('voice', { phase: 'listening', turn_id: 'v1' }); await settled();
    a = await area();
    check('she starts listening: a capsule of state grows from under her (glyph, “Listening”, keyboard, end), 44 tall', a.up && a.kind === 'capsule' && Math.round(a.r.h) === 44 && a.label === 'Listening' && a.state === 'hearing');
    check('it hangs under her: centred on her, 11 px below her', Math.abs(a.r.x + a.r.w / 2 - out.x) < 1.5 && Math.abs(a.r.y - (out.y + 26 + 11)) < 1.5);
    check('it is glass for the native material, round as the capsule', await page.evaluate(() => window.__state.glass.some(g => Math.round(g.height) === 44 && Math.round(g.radius) === 22)));
    await shot('01-capsule');
    await emit('voice', { phase: 'partial', turn_id: 'v1', text: '帮我查一下明天的天气' }); await page.waitForTimeout(600);
    a = await area();
    check('the capsule takes the words as they come in, in place of “Listening”, and widens', a.kind === 'capsule' && a.label === '帮我查一下明天的天气' && a.r.w > 183 && a.r.w <= 360.5);
    await shot('01a-capsule-live');
    await emit('voice', { phase: 'empty', turn_id: 'v1' });

    // you said, she thinks, she answers
    await emit('voice', { phase: 'listening', turn_id: 'v2' });
    await emit('voice', { phase: 'accepted', turn_id: 'v2', text: '把今天下午三点之后的会议都往后推一个小时，再给王老师发条消息' }); await settled();
    a = await area();
    check('what you said lands right-aligned, small and dim, no bubble; the area is 360 wide', a.kind === 'area' && Math.round(a.r.w) === 360 && a.r.right - (await page.evaluate(() => document.querySelector('.talk .tk-u').getBoundingClientRect().right)) < 20
      && await page.evaluate(() => { const u = getComputedStyle(document.querySelector('.talk .tk-u')); return Math.abs(parseFloat(u.fontSize) - 12.5) < .6 && u.backgroundColor === 'rgba(0, 0, 0, 0)'; }));
    check('the footer says she is thinking, and shimmers', a.state === 'thinking' && a.label === 'Thinking' && await page.evaluate(() => document.querySelector('.talk .lb').classList.contains('shim')));
    await shot('02-thinking');
    await emit('open', { turn_id: 'v2', response_id: 'r-v2' });
    await emit('append', { turn_id: 'v2', token: '<voice>好，三点之后的会议都推后一小时。' });
    await page.waitForTimeout(500);
    a = await area();
    check('her words come in on the left, large, under yours; the footer says she is speaking', a.her.startsWith('好，三点之后') && a.state === 'speaking' && a.label === 'Speaking · poke to interrupt'
      && await page.evaluate(() => parseFloat(getComputedStyle(document.querySelector('.talk .tk-s')).fontSize) === 14.5));
    const lit0 = a.lit;
    await skew(2000); await page.waitForTimeout(250);
    a = await area();
    check(`follow-along: her words light up as she says them, about 4.5 characters a second (${lit0} → ${a.lit})`, a.lit >= lit0 + 7 && a.lit <= lit0 + 14);
    await emit('append', { turn_id: 'v2', token: '我也会给王老师发消息。</voice>' }); await emit('done', { turn_id: 'v2', fadeMs: 100 });
    await skew(2000); await page.waitForTimeout(250);
    a = await area();
    check('the sentence she has said settles back to a softer white while the next one waits dim', a.lit > 16 && a.all === 0 && await page.evaluate(() => document.querySelector('.talk .tk-s span.done') !== null));
    await shot('03-speaking');
    await emit('voice', { phase: 'spoken', turn_id: 'v2' }); await page.waitForTimeout(400);
    a = await area();
    check('when she has finished everything is lit, and the footer no longer carries the sentence: it says Listening', a.all === 1 && a.state === 'listening' && a.label === 'Listening' && !a.footer.includes('好'));

    // a long spoken answer scrolls, never truncated, follows her until you scroll away
    await turn('v3', '把所有事情的进展都详细告诉我', ['<voice>', long, '</voice>']);
    await page.waitForTimeout(1200);
    a = await area();
    check(`a long answer: the area stops at 360 and the transcript scrolls (${Math.round(a.r.h)} tall, ${a.tr.scroll} of content)`, Math.round(a.r.h) === 360 && a.tr.scroll > a.tr.client);
    check('it shows all of it: not a character of the spoken form is cut', await page.evaluate(n => [...document.querySelectorAll('.talk .tk-h .tk-s')].at(-1).textContent.length === n, [...long].length));
    await skew(70_000); await page.waitForTimeout(700);
    a = await area();
    check(`the view follows the lit words down (scrolled ${a.tr.top} px)`, a.tr.top > 20);
    await shot('04-long');
    await page.mouse.move(196, 240); await page.mouse.wheel(0, -2000); await page.waitForTimeout(700);
    check('scrolled up by hand, it stops following and offers “Back to latest”', (await area()).tr.top < 5 && await page.locator('.talk .tk-latest button').count() === 1);
    await shot('05-scrolled-up');
    await page.locator('.talk .tk-latest button').click(); await page.waitForTimeout(900);
    a = await area();
    check('“Back to latest” goes to the lit words and follows again', a.tr.top > 20 && await page.locator('.talk .tk-latest button').count() === 0);
    await emit('append', { turn_id: 'v3', token: '' }); await emit('voice', { phase: 'spoken', turn_id: 'v3' }); await emit('done', { turn_id: 'v3', fadeMs: 100 });
    await page.waitForTimeout(500);

    // an answer with a written part: the spoken form on top, the written part below as a list, as written
    await turn('v4', '我今天有什么安排', written, { spoken: false });
    await page.waitForTimeout(350);
    a = await area();
    check('written part: it comes up behind the spoken line, a moment after it', a.hers >= 1 && a.rows === 0);
    await page.waitForTimeout(1500);
    a = await area();
    check('written part: the spoken line on top, then the grouped list with the times, the places and the link as written', a.rows === 3
      && (await page.locator('.talk .doc time').allTextContents()).join() === '10:00,14:30,17:00' && (await page.locator('.talk .doc').last().textContent()).includes('https://zoom.us/j/123') && (await page.locator('.talk .doc').last().textContent()).includes('3F 会议室'));
    await shot('06-written');
    await emit('voice', { phase: 'spoken', turn_id: 'v4' });
    await page.waitForTimeout(900);
    a = await area();
    check(`she has finished: the view rests at the end, the written part's last line in sight (${a.tr.top} of ${a.tr.scroll - a.tr.client})`, a.tr.end);

    // typing in the middle of the conversation
    await page.locator('.talk-ft .kb').click(); await page.waitForTimeout(900);
    a = await area();
    check('typing mid-voice: the footer gives way to the field, with the microphone to its left', a.fieldShown && await page.locator('.talk .mic').isVisible());
    check('the microphone pauses while you type: the daemon’s own mute, and the mic button goes dim', posts.some(p => p.path === '/inherent/controls' && p.body.mic_muted === true) && await page.locator('.talk .mic.dim').count() === 1);
    check('the send button is grey while the field is empty', await page.locator('.talk .send').isDisabled() && await page.locator('.talk .send.off').count() === 1);
    await page.keyboard.type('第一行');
    check('typing lights the send button in her colour', !(await page.locator('.talk .send').isDisabled()) && await page.evaluate(() => getComputedStyle(document.querySelector('.talk .send')).backgroundColor !== 'rgba(255, 255, 255, 0.1)'));
    await page.keyboard.press('Shift+Enter'); await page.keyboard.type('第二行');
    await page.waitForTimeout(300);
    check('Shift+Enter is a new line and the field grows with it', (await page.locator('.talk textarea').inputValue()) === '第一行\n第二行' && await page.evaluate(() => document.querySelector('.talk textarea').getBoundingClientRect().height > 50));
    for (let i = 0; i < 9; i++) { await page.keyboard.press('Shift+Enter'); await page.keyboard.type(`第${i + 3}行`); }
    await page.waitForTimeout(300);
    check('at six lines the field stops growing and scrolls', await page.evaluate(() => { const t = document.querySelector('.talk textarea'); return Math.round(t.getBoundingClientRect().height) === 136 && t.scrollHeight > t.clientHeight; }));
    await shot('07-typing');
    await page.keyboard.press('Escape'); await page.waitForTimeout(700);
    a = await area();
    check('Esc closes the field and the area stays, with the mic back on', !a.fieldShown && a.up && posts.filter(p => p.path === '/inherent/controls').at(-1).body.mic_muted === false && a.yous >= 1);
    await page.locator('.talk-ft .kb').click(); await page.waitForTimeout(700);
    await page.locator('.talk textarea').fill('把周五的会议改到下午');
    await page.keyboard.press('Enter');
    const flying = await page.waitForSelector('.talk .tk-fly .tk-ghost', { timeout: 800 }).then(() => true, () => false);
    check('a copy of it flies from the field to the transcript, drawn in the area and not in the clipped transcript', flying);
    await settled();
    check('Enter sends: the text goes to the daemon', posts.some(p => p.path === '/inherent/submit' && p.body.text === '把周五的会议改到下午'));
    a = await area();
    check('it lands in view: the transcript scrolls to the end, so the line you sent is not hidden below the footer', a.tr.end && a.tr.youIn);
    check('it lands right-aligned in the transcript; the footer says Thinking; the mic is back', a.you === '把周五的会议改到下午' && a.state === 'thinking' && a.label === 'Thinking' && !a.fieldShown
      && posts.filter(p => p.path === '/inherent/controls').at(-1).body.mic_muted === false);
    await shot('08-sent');
    await emit('open', { turn_id: 'typed-1', response_id: 'r-t1' }); await emit('append', { turn_id: 'typed-1', token: '<voice>好，已经改到下午三点。</voice>' });
    await emit('done', { turn_id: 'typed-1', fadeMs: 100 }); await emit('voice', { phase: 'spoken', turn_id: 'typed-1' });
    await page.waitForTimeout(600);

    // the pointer, the glass
    await move(out.x, out.y + 100);
    a = await area();
    check('over the area the pointer is its own: clicks do not fall through', await page.evaluate(() => window.__state.passthrough === false));
    await move(600, 650); await page.waitForTimeout(100);
    check('away from it they do', await page.evaluate(() => window.__state.passthrough === true));
    check('its glass rect follows its shape: 360 wide, 22 round', await page.evaluate(() => window.__state.glass.some(g => Math.round(g.width) === 360 && Math.round(g.radius) === 22)));

    // an end the person asked for
    await page.locator('.talk-ft .st').click(); await page.waitForTimeout(900);
    check('the end button ends the conversation and folds the area into her', posts.some(p => p.path === '/inherent/controls' && p.body.conversation === false) && !(await area()).up);
    check('no page errors (everything)', s.errors.length === 0);
    await s.context.close();
  }

  // ---- shown only what is worth reading (the default) ----
  {
    const s = await scene({ captions: 'brief' });
    const { page, emit, skew, shot, area, settled, turn } = s;
    await page.waitForTimeout(600);
    await emit('voice', { phase: 'listening', turn_id: 'b1' }); await settled();
    let a = await area();
    check('the default level: a 36 tall pill, glyph and end button, no “Listening” text', a.kind === 'pill' && Math.round(a.r.h) === 36 && a.label === '' && a.state === 'hearing');
    check('the pill is glass, fully round', await page.evaluate(() => window.__state.glass.some(g => Math.round(g.height) === 36 && Math.round(g.radius) === 18)));
    await shot('10-pill');
    // What you say shows while you are still saying it (ADR 0111): the pill widens with the words, the end button stays right behind them.
    const said = '明天上午十点提醒我开会然后把下午的评审改到四点再通知王老师';
    const live = async n => { await emit('voice', { phase: 'partial', turn_id: 'b1', text: [...said].slice(0, n).join('') }); await page.waitForTimeout(550); };
    const behind = () => page.evaluate(() => { const t = document.querySelector('.talk'), lb = t.querySelector('.lb'), st = t.querySelector('.st'), r = t.getBoundingClientRect(), l = lb.getBoundingClientRect(), b = st.getBoundingClientRect();
      return { w: r.width, label: lb.textContent, gap: b.left - l.right, inside: b.right <= r.right + .5, clipped: lb.scrollWidth > lb.clientWidth + 1, ellipsis: getComputedStyle(lb).textOverflow }; });
    await live(3); const p1 = await behind();
    await live(10); const p2 = await behind();
    check('while you speak the pill shows the words so far and widens with them', p1.label === [...said].slice(0, 3).join('') && p2.label === [...said].slice(0, 10).join('') && p2.w > p1.w + 40);
    check('the end button rides right behind the words, not at some far edge', Math.abs(p2.gap - 10) < 3 && p2.inside);
    await shot('10a-live');
    await live(27); const p3 = await behind();
    check('a long sentence stops at 360 wide, shows its newest words behind an ellipsis, and the end button stays inside', p3.w <= 360.5 && p3.clipped && p3.ellipsis === 'ellipsis' && p3.inside && p3.label === [...said].slice(0, 27).join(''));
    await shot('10b-live-long');
    await emit('voice', { phase: 'accepted', turn_id: 'b1', text: '明天上午十点提醒我开会' }); await page.waitForTimeout(800);
    await emit('voice', { phase: 'partial', turn_id: 'b1', text: '迟到的一句' }); await page.waitForTimeout(400);
    a = await area();
    check('it shows what you just said for a moment, and a partial that comes in after it is ignored', a.kind === 'pill' && a.label === '明天上午十点提醒我开会' && a.r.w > 150);
    await shot('11-heard');
    await emit('open', { turn_id: 'b1', response_id: 'r-b1' }); await emit('append', { turn_id: 'b1', token: '<voice>好，明早十点提醒你开会。</voice>' }); await emit('done', { turn_id: 'b1', fadeMs: 100 });
    await skew(3500); await page.waitForTimeout(900);
    a = await area();
    check('after 3 s it fades and the pill closes up around the glyph', a.kind === 'pill' && a.label === '' && a.r.w < 100);
    check('a plain spoken line is not shown at this level', a.hers === 0 && a.yous === 0);
    await emit('voice', { phase: 'spoken', turn_id: 'b1' });
    await turn('b2', '我今天有什么安排', written);
    await page.waitForTimeout(2000);
    a = await area();
    check('an answer with a written part: the pill grows into the area showing only the written part', a.kind === 'area' && a.rows === 3 && a.hers === 0 && a.yous === 0 && Math.round(a.r.w) === 360);
    await shot('12-written-only');
    await emit('voice', { phase: 'spoken', turn_id: 'b2' });
    // A follow-up answered in speech only ("shorter, please"): the area follows her, not the written answer before it.
    const shorter = '短一点：十点开会，两点半评审，五点健身。';
    await turn('b2s', '说短一点', shorter); await page.waitForTimeout(2000);
    a = await area();
    check('a spoken-only follow-up to a written answer shows under it, so the area never reads one answer while she says another', a.kind === 'area' && a.her === shorter && a.hers === 1 && a.yous === 0);
    await shot('12a-follow-up');
    await emit('voice', { phase: 'spoken', turn_id: 'b2s' });
    const shortest = '十点、两点半、五点。';
    await turn('b2t', '再短一点', shortest); await page.waitForTimeout(2000);
    a = await area();
    check('and a second spoken-only follow-up replaces the first in turn', a.kind === 'area' && a.her === shortest && a.hers === 1);
    await emit('voice', { phase: 'spoken', turn_id: 'b2t' });
    await emit('voice', { phase: 'listening', turn_id: 'b3' }); await emit('voice', { phase: 'accepted', turn_id: 'b3', text: '再查一下' });
    await emit('failed', { turn_id: 'b3', reason: 'network', message: 'The network is down, so that turn did not finish.' }); await page.waitForTimeout(800);
    a = await area();
    check('an error always shows', a.err === 'The network is down, so that turn did not finish.');
    await shot('13-error');
    check('no page errors (brief)', s.errors.length === 0);
    await s.context.close();
  }

  // ---- a structured answer (ADR 0114): her spoken line, lit as she says it, and the written part under it, at the middle level ----
  {
    const s = await scene({ captions: 'brief' });
    const { page, emit, shot, area } = s;
    await page.waitForTimeout(600);
    const line = '今天有三件事，时间我写在屏幕上了。', details = '- 10:00 和 Anna 的产品会 · 3F 会议室\n- 14:30 评审会议 · https://zoom.us/j/123\n- 17:00 健身课';
    await emit('voice', { phase: 'listening', turn_id: 'sa1' }); await emit('voice', { phase: 'accepted', turn_id: 'sa1', text: '我今天有什么安排' });
    await emit('open', { turn_id: 'sa1', response_id: 'r-sa1' }); await emit('append', { turn_id: 'sa1', token: line });
    await emit('done', { turn_id: 'sa1', fadeMs: 100, written: details });
    await emit('voice', { phase: 'playing', turn_id: 'sa1', played: 5, ahead: 12, held: false });
    await page.waitForSelector('.talk [data-written]', { timeout: 6000 }); await page.waitForTimeout(1500);
    let a = await area();
    console.log(JSON.stringify({ kind: a.kind, her: a.her, hers: a.hers, rows: a.rows }));
    check('a structured answer at the middle level: the area opens with her spoken line and the written part under it', a.kind === 'area' && a.her === line && a.hers === 1 && a.rows === 3);
    check('her spoken line is lit with her voice, the written part is not', a.lit > 0 && a.lit < [...line].length && await page.evaluate(() => !document.querySelector('.talk [data-written] i.on')));
    const order = await page.evaluate(() => { const t = document.querySelector('.talk'), s = t.querySelector('.tk-s').getBoundingClientRect(), w = t.querySelector('[data-written]').getBoundingClientRect(); return { below: w.top >= s.bottom - 1, text: t.querySelector('[data-written]').textContent }; });
    check('the written part sits below the spoken line and holds the details', order.below && order.text.includes('Anna') && order.text.includes('zoom.us'));
    await shot('13b-structured-brief');
    await emit('voice', { phase: 'spoken', turn_id: 'sa1' });
    // The same answer without the signal is the old one: a <document> shows alone.
    await s.turn('sa2', '再说一遍', written); await page.waitForTimeout(2000);
    a = await area();
    check('an answer with a <document> and no signal still shows the document alone', a.kind === 'area' && a.hers === 0 && a.rows === 3);
    check('no page errors (structured)', s.errors.length === 0);
    await s.context.close();
  }

  // ---- the written part writes itself in on a clock of its own, quickly: not tied to her voice, and the screen stays on what she says ----
  const para = '下午三点之后的三场会议都已经往后推了一个小时，对应的日历邀请也已经更新。我给王老师发了一条消息，说明了改期的原因，并且问他明天上午是否方便再约一次。周五的评审会议和你的健身课时间冲突了，我没有擅自改动，等你决定。';
  const tail = '### 今天\n- 10:00 和 Anna 的产品会 · 3F 会议室\n- 14:30 评审会议\n- 17:00 健身课\n\n**提醒**：明早九点交报告。';
  // What the written block shows now: characters lit or grey, in order, and what it is made of.
  const wr = page => page.evaluate(() => {
    const w = [...document.querySelectorAll('.talk .tk-w')].at(-1), cs = [...w.querySelectorAll('.md p i')], on = cs.filter(i => i.classList.contains('on')), off = cs.filter(i => !i.classList.contains('on'));
    const col = i => i && getComputedStyle(i).color, sp = [...document.querySelectorAll('.talk .tk-s')].at(-1);
    return { total: cs.length, on: on.length, off: off.length, prefix: cs.slice(0, on.length).every(i => i.classList.contains('on')), all: w.classList.contains('all'), h: w.offsetHeight,
      onColor: col(on[0]), offColor: col(off.at(-1)), text: [...w.querySelectorAll('.md p')].map(p => p.textContent).join('|'), rows: w.querySelectorAll('.doc > div').length, inDoc: w.querySelectorAll('.doc i, h5 i, code i').length,
      bold: [...w.querySelectorAll('strong')].map(b => b.textContent).join(), boldChars: w.querySelectorAll('strong i').length, spokenAll: !!sp?.classList.contains('all') };
  });
  for (const captions of ['brief', 'all']) {
    const s = await scene({ captions });
    const { page, emit, skew, shot, area, turn } = s;
    await page.waitForTimeout(600);
    const doc = `<voice>好，我把今天的情况都写在下面了，你慢慢看，有不清楚的地方再问我。</voice><document>${para}\n\n${tail}</document>`;
    await turn('w1', '我今天有什么安排', doc, { spoken: false });
    await page.waitForSelector('.talk .tk-w'); await page.waitForTimeout(150);
    let m = await wr(page);
    check(`write-in (${captions}): the paragraphs are a character each, grey until the write-in reaches them (${m.on} of ${m.total} lit)`, m.total > 100 && m.off > 0 && m.on < m.total / 2 && m.prefix);
    await skew(900); await page.waitForTimeout(350);
    const m2 = await wr(page), T2 = await page.evaluate(() => Date.now());
    check(`write-in (${captions}): partway through some but not all are lit, in order (${m2.on} of ${m2.total})`, m2.on > m.on && m2.off > 0 && m2.on / m2.total > .2 && m2.on / m2.total < .8 && m2.prefix && !m2.all);
    check(`write-in (${captions}): lit characters are bright, unread ones the same dim grey as her spoken line`, m2.onColor === 'rgb(238, 240, 251)' && m2.offColor === 'rgba(238, 240, 251, 0.28)');
    check(`write-in (${captions}): the list stays the grouped .doc rows, whole, and headings and code are not split`, m2.rows === 3 && m2.inDoc === 0 && (await page.locator('.talk .doc time').allTextContents()).join() === '10:00,14:30,17:00');
    check(`write-in (${captions}): Markdown still renders: the bold stays bold, the text is as written`, m2.bold === '提醒' && m2.boldChars === 2 && m2.text === `${para}|提醒：明早九点交报告。`);
    await shot(`write-in-${captions}-mid`);
    // Her voice is stopped here: that is not what writes it, so it goes on. (The clock goes back to where it was at the partway look, so the checks above cost no write-in time.)
    await skew(T2 - await page.evaluate(() => Date.now()));
    await emit('voice', { phase: 'spoken', turn_id: 'w1', output_outcome: 'interrupted' }); await skew(300); await page.waitForTimeout(350);
    const m3 = await wr(page);
    check(`write-in (${captions}): her voice being stopped does not freeze it: more is lit and it is not finished (${m3.on} of ${m3.total})`, m3.on > m2.on && m3.off > 0 && m3.prefix && !m3.all);
    await skew(3000); await page.waitForTimeout(400);
    const m4 = await wr(page);
    check(`write-in (${captions}): within a few seconds all of it is lit`, m4.on === m4.total && m4.off === 0 && m4.all);
    check(`write-in (${captions}): no layout jump while it writes in: the block is as tall at the end as at the start (${Math.round(m.h)}, ${Math.round(m4.h)})`, Math.abs(m.h - m4.h) < 1.5 && Math.abs(m2.h - m4.h) < 1.5);
    await shot(`write-in-${captions}-done`);
    // Her voice never reports in: the written part still finishes on its own, and her spoken line is not the one that decides it.
    await turn('w3', '再说一遍', doc, { spoken: false });
    await page.waitForFunction(() => !!document.querySelector('.talk [data-line="her:w3"] .tk-w'));
    await skew(3600); await page.waitForTimeout(500);
    const m5 = await wr(page);
    check(`write-in (${captions}): with her voice still going (it never says it has finished) it is all lit on its own (${m5.on} of ${m5.total})`, m5.total > 100 && m5.on === m5.total && m5.all && (captions === 'brief' || !m5.spokenAll));
    // Nothing spoken: the document is there whole.
    await turn('w2', '写在文档里', `<document>${para}</document>`); await page.waitForTimeout(1500);
    const bare = await page.evaluate(() => { const w = [...document.querySelectorAll('.talk .tk-w')].at(-1); return { chars: w.querySelectorAll('i').length, text: w.textContent }; });
    check(`write-in (${captions}): an answer with nothing spoken is a plain block, not split into characters`, bare.chars === 0 && bare.text === para);
    if (captions === 'all') {
      // Her voice has already finished when the written part first shows (it comes up 0.7 s after the line): there is nothing to write along with.
      await turn('w4', '再来一次', doc, { spoken: false }); await emit('voice', { phase: 'spoken', turn_id: 'w4' }); await page.waitForTimeout(1500);
      const done = await page.evaluate(() => { const w = [...document.querySelectorAll('.talk .tk-w')].at(-1); return { chars: w.querySelectorAll('i').length, text: w.textContent }; });
      check('write-in (all): her voice was already done when the written part first showed: it is there whole, no characters split', done.chars === 0 && done.text.startsWith(para));
    }
    check(`no page errors (write-in, ${captions})`, s.errors.length === 0);
    await s.context.close();
  }

  // ---- the screen stays on what she says; with nothing said on screen it follows the write-in ----
  {
    const s = await scene({ captions: 'all' });
    const { page, emit, skew, shot, area, turn, settled } = s;
    await page.waitForTimeout(600);
    await turn('l1', '全部告诉我', `<voice>${long}${long}</voice><document>${para}\n\n${tail}</document>`, { spoken: false });
    await page.waitForSelector('.talk .tk-w'); await settled();
    await skew(110_000); await page.waitForTimeout(900); // the write-in is long over; she is still saying it
    const see = () => page.evaluate(() => {
      const tr = document.querySelector('.talk-tr').getBoundingClientRect(), on = [...document.querySelectorAll('.talk .tk-s i.on')].at(-1).getBoundingClientRect(), last = [...document.querySelectorAll('.talk .tk-w .md p')].at(-1).getBoundingClientRect();
      const t = document.querySelector('.talk-tr');
      return { front: on.top >= tr.top && on.bottom <= tr.bottom, writtenOut: last.top >= tr.bottom, top: t.scrollTop, end: t.scrollHeight - t.scrollTop - t.clientHeight < 3, scroll: t.scrollHeight - t.clientHeight };
    });
    const w = await wr(page), a = await see();
    check(`follow (all): while she speaks the written write-in is finished (${w.on} of ${w.total})`, w.on === w.total && w.all);
    check(`follow (all): the view stays on the line she is saying, not the written part below it (front in view, written's last line out of it, scrolled ${a.top} of ${a.scroll})`, a.front && a.writtenOut && !a.end && a.top > 20);
    await shot('write-in-all-follows-speech');
    await emit('voice', { phase: 'spoken', turn_id: 'l1' }); await page.waitForTimeout(1100);
    const e = await see();
    check('follow (all): once she has finished it rests at the end, the written part in view', e.end && !e.writtenOut);
    check('no page errors (follow, all)', s.errors.length === 0);
    await s.context.close();
  }
  {
    const s = await scene({ captions: 'brief' });
    const { page, emit, skew, shot, turn, area, settled } = s;
    await page.waitForTimeout(600);
    await turn('l2', '全部告诉我', `<voice>好，都写在下面。</voice><document>${[para, para, para, para, para].join('\n\n')}\n\n${tail}</document>`, { spoken: false });
    await page.waitForSelector('.talk .tk-w'); await settled();
    await skew(6000); await page.waitForTimeout(900);
    const see = () => page.evaluate(() => {
      const t = document.querySelector('.talk-tr'), tr = t.getBoundingClientRect(), on = [...document.querySelectorAll('.talk .tk-w i.on')].at(-1)?.getBoundingClientRect();
      return { front: !!on && on.top >= tr.top && on.bottom <= tr.bottom, top: t.scrollTop, scroll: t.scrollHeight - t.clientHeight, lit: document.querySelectorAll('.talk .tk-w i.on').length, all: document.querySelectorAll('.talk .tk-w i').length };
    });
    let a = await see();
    check(`follow (brief): she is still saying it, so the write-in does not take the view down: it stays at the top (${a.lit} of ${a.all} lit, scrolled ${a.top} of ${a.scroll})`, a.lit > 0 && a.lit < a.all && a.top < 3 && a.scroll > 100);
    await shot('write-in-brief-holds');
    await emit('voice', { phase: 'spoken', turn_id: 'l2' });
    await skew(1500); await page.waitForTimeout(900);
    a = await see();
    check(`follow (brief): once her voice is done the view follows the write-in front down (${a.lit} of ${a.all} lit, scrolled ${a.top} of ${a.scroll})`, a.lit > 0 && a.front && a.top > 40);
    await shot('write-in-brief-follows');
    await page.mouse.move(196, 240); await page.mouse.wheel(0, -3000); await page.waitForTimeout(700);
    await skew(1500); await page.waitForTimeout(700);
    const b = await see();
    check(`follow (brief): scrolled away by hand, it stays where the reader is while the write-in goes on (${b.top}; ${a.lit} → ${b.lit} lit)`, b.top < 5 && b.lit > a.lit);
    check('no page errors (follow, brief)', s.errors.length === 0);
    await s.context.close();
  }

  // ---- a long answer opens showing its top; the view goes down only as her lit words do, never with the write-in ----
  {
    const spokenLine = '好，都写在下面。这件事我从头到尾说一遍，你慢慢听，不清楚的地方再问我。';
    const doc = `<document>${[para, para, para, para].join('\n\n')}\n\n${tail}</document>`;
    // The view's scroll position, sampled every 40 ms from before the answer comes: its highest, and where it is now.
    const watch = page => page.evaluate(() => { const tr = document.querySelector('.talk-tr'); window.__top = [0]; window.__iv = setInterval(() => window.__top.push(tr.scrollTop), 40); });
    const seen = page => page.evaluate(() => { const t = document.querySelector('.talk-tr'); return { max: Math.max(...window.__top), now: t.scrollTop, room: t.scrollHeight - t.clientHeight }; });
    const answer = async (s, id, text) => {
      await s.emit('voice', { phase: 'listening', turn_id: id }); await s.emit('voice', { phase: 'accepted', turn_id: id, text: '全部告诉我' });
      await s.emit('open', { turn_id: id, response_id: `r-${id}` }); await s.emit('append', { turn_id: id, token: text }); await s.emit('done', { turn_id: id, fadeMs: 100 });
      await s.emit('voice', { phase: 'playing', turn_id: id, played: 2, ahead: 8, held: false });
    };
    // The middle level shows a written part alone while she says something else: nothing of hers is on screen to follow.
    for (const [how, fieldFirst] of [['the pill grows into the area', false], ['the typing field keeps the area up', true]]) {
      const s = await scene({ captions: 'brief' });
      const { page, emit, move, skew, shot, settled } = s;
      await page.waitForTimeout(600);
      if (fieldFirst) { await move(out.x, out.y); await page.waitForTimeout(700); await page.locator('.companion-chip button').click(); await page.waitForTimeout(900); } // (the typing field keeps the area up)
      await emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); s.daemonState.controls.conversation = true;
      await emit('voice', { phase: 'listening', turn_id: 'tp0' });
      await watch(page);
      await answer(s, 'tp1', `<voice>${spokenLine}</voice>${doc}`);
      await page.waitForSelector('.talk .tk-w'); await page.waitForTimeout(900);
      let a = await seen(page);
      check(`a long answer, middle level (${how}): it opens at its top (highest scroll ${a.max}, of ${a.room})`, a.room > 100 && a.max < 3);
      await shot(`talk-long-brief-first-${fieldFirst ? 'up' : 'pill'}`);
      await skew(10_000); await page.waitForTimeout(900);
      a = await seen(page);
      check(`a long answer, middle level (${how}): the write-in is all in and she is still saying it, and the view has stayed at the top (highest scroll ${a.max})`, await page.evaluate(() => { const w = document.querySelectorAll('.talk .tk-w i'); return w.length > 100 && [...w].every(i => i.classList.contains('on')); }) && a.max < 3);
      await shot(`talk-long-brief-mid-${fieldFirst ? 'up' : 'pill'}`);
      await page.evaluate(() => clearInterval(window.__iv));
      check(`no page errors (long answer, middle level, ${how})`, s.errors.length === 0);
      await s.context.close();
    }
    // Everything: her spoken line is short and the written part under it is long; she says the line, the write-in does not take the view.
    {
      const s = await scene({ captions: 'all' });
      const { page, emit, skew, shot, area } = s;
      await page.waitForTimeout(600);
      await emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); s.daemonState.controls.conversation = true;
      await watch(page);
      await answer(s, 'tp2', `<voice>${spokenLine}</voice>${doc}`);
      await page.waitForSelector('.talk .tk-w'); await page.waitForTimeout(900);
      await skew(10_000); await page.waitForTimeout(900);
      const a = await seen(page);
      check(`a long answer, everything: the write-in is all in, the view stayed at the top with her spoken line (highest scroll ${a.max})`, a.room > 100 && a.max < 3 && await page.evaluate(() => [...document.querySelectorAll('.talk .tk-w i')].every(i => i.classList.contains('on'))));
      await page.evaluate(() => clearInterval(window.__iv));
      check('no page errors (long answer, everything, written part)', s.errors.length === 0);
      await s.context.close();
    }
    // Everything, her words alone: it opens at the top and the view goes down with the lit words.
    {
      const s = await scene({ captions: 'all' });
      const { page, emit, shot } = s;
      await page.waitForTimeout(600);
      await emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); s.daemonState.controls.conversation = true;
      await emit('voice', { phase: 'listening', turn_id: 'tp3' }); await page.waitForTimeout(700);
      await watch(page);
      await answer(s, 'tp3', `<voice>${long}${long}</voice>`);
      await page.waitForTimeout(1200);
      let a = await seen(page);
      check(`a long answer, everything: it opens at its top (highest scroll ${a.max}, of ${a.room})`, a.room > 100 && a.max < 3);
      await shot('talk-long-all-first');
      await emit('voice', { phase: 'playing', turn_id: 'tp3', played: 330, ahead: 340, held: false }); await page.waitForTimeout(900); // (her voice is that far on)
      a = await seen(page);
      const front = await page.evaluate(() => { const tr = document.querySelector('.talk-tr').getBoundingClientRect(), on = [...document.querySelectorAll('.talk .tk-s i.on')].at(-1).getBoundingClientRect(); return on.top >= tr.top && on.bottom <= tr.bottom; });
      check(`a long answer, everything: partway through the view has gone down with the lit words, which are in sight (scrolled ${a.now} of ${a.room})`, a.now > 20 && a.now < a.room - 20 && front);
      await shot('talk-long-all-mid');
      await page.evaluate(() => clearInterval(window.__iv));
      check('no page errors (long answer, everything)', s.errors.length === 0);
      await s.context.close();
    }
  }

  // ---- the pill after an answer was shown: it fits its words, not the transcript folded away in it ----
  {
    const s = await scene({ captions: 'brief' });
    const { page, emit, skew, area, turn, folded } = s;
    await page.waitForTimeout(600);
    await turn('f1', '我今天有什么安排', written);
    await page.waitForTimeout(2000);
    await emit('voice', { phase: 'spoken', turn_id: 'f1' });
    await skew(9000); await folded(); await page.waitForTimeout(1600);
    await emit('voice', { phase: 'listening', turn_id: 'f2' });
    await emit('voice', { phase: 'partial', turn_id: 'f2', text: '再查一下' }); await page.waitForTimeout(900);
    const fit = () => page.evaluate(() => { const t = document.querySelector('.talk'), kids = [...t.querySelector('.talk-ft').children].filter(k => getComputedStyle(k).display !== 'none'), tb = t.getBoundingClientRect();
      return { shell: tb.width, content: kids.at(-1).getBoundingClientRect().right - tb.left + 5 }; });
    let a = await area(), f = await fit();
    check('after an answer was shown and folded, the pill is a pill again and as wide as its words, not as wide as that answer', a.kind === 'pill' && Math.abs(f.shell - f.content) < 4);
    await emit('voice', { phase: 'partial', turn_id: 'f2', text: '再查一下明天上午的天气怎么样' }); await page.waitForTimeout(700);
    const longer = await fit();
    await emit('voice', { phase: 'accepted', turn_id: 'f2', text: '好。' }); await page.waitForTimeout(1500);
    f = await fit();
    check('it widens with what you say and goes back to fit a shorter sentence', longer.shell > 150 && f.shell < longer.shell - 40 && Math.abs(f.shell - f.content) < 4);
    check('no page errors (fit)', s.errors.length === 0);
    await s.context.close();
  }

  // ---- "Hey Jarvis" and a pause: the words heard so far do not outlive their turn ----
  {
    const s = await scene({ captions: 'brief' });
    const { page, emit, area, folded, posts, daemonState } = s;
    await page.waitForTimeout(600);
    const conversation = async on => { daemonState.controls.conversation = on; await emit('controls', { mic_muted: false, speech_muted: false, conversation: on }); };
    const heardHi = async id => { await emit('voice', { phase: 'listening', turn_id: id }); await emit('voice', { phase: 'partial', turn_id: id, text: 'Hey hi' }); await emit('voice', { phase: 'transcribing', turn_id: id }); await page.waitForTimeout(700); };
    await conversation(true);
    await heardHi('w1');
    let a = await area();
    check('wake only, before the verdict: the pill shows what was heard, hearing', a.up && a.label === 'Hey hi' && a.state === 'hearing');
    // The daemon ends the wake-only turn with `empty` (voice_session.py), then conversation mode times out.
    await emit('voice', { phase: 'empty', turn_id: 'w1', reason: 'wake_only' }); await page.waitForTimeout(700);
    a = await area();
    check('wake only: `empty` drops the stale words and the pill goes back to listening, still up', a.up && a.label === '' && a.state === 'listening');
    await conversation(false); await folded();
    check('then the daemon goes idle: the pill folds', !(await area()).up);
    // Whatever the daemon did or did not say about the turn, idle ends it on screen.
    await conversation(true);
    await heardHi('w2');
    await conversation(false); await folded();
    check('a turn nobody ended: conversation going idle still folds the pill', !(await area()).up);
    await conversation(true);
    await heardHi('w3');
    await emit('voice', { phase: 'listening', turn_id: 'w4' }); await page.waitForTimeout(500); // the re-armed listen has no words yet
    a = await area();
    check('the re-armed listen starts with no words from the turn before', a.label === '');
    await conversation(true);
    await heardHi('w5');
    // The end button with nothing to cancel (no answer, nothing being thought about): it still gets back to idle.
    posts.length = 0;
    await page.locator('.talk-ft .st').click(); await emit('controls', { mic_muted: false, speech_muted: false, conversation: false }); await page.waitForTimeout(1500);
    a = await area();
    check('the end button with no turn to cancel still ends the conversation and folds the pill', posts.some(p => p.path === '/inherent/controls' && p.body.conversation === false) && !a.up);
    await conversation(true); await page.waitForTimeout(900); // she is asked to listen again: nothing of the ended turn is left
    a = await area();
    check('after the end button the next conversation starts clean: no stale words, not hearing', a.up && a.label === '' && a.state === 'listening');
    check('no page errors (wake only)', s.errors.length === 0);
    await s.context.close();
  }

  // ---- shown nothing; and her voice off ----
  {
    const s = await scene({ captions: 'none' });
    const { page, emit, area, settled, turn, shot } = s;
    await page.waitForTimeout(600);
    await emit('voice', { phase: 'listening', turn_id: 'n1' }); await settled();
    await emit('voice', { phase: 'accepted', turn_id: 'n1', text: '把窗帘拉上' }); await settled();
    let a = await area();
    check('shown nothing: only the glyph and the end button, nothing said', a.kind === 'pill' && a.label === '' && a.yous === 0 && Math.round(a.r.w) < 80);
    await shot('14-none');
    await emit('open', { turn_id: 'n1', response_id: 'r-n1' }); await emit('append', { turn_id: 'n1', token: '<voice>好的，窗帘拉上了。</voice>' }); await emit('done', { turn_id: 'n1', fadeMs: 100 }); await settled();
    a = await area();
    check('and her answer stays unshown', a.kind === 'pill' && a.hers === 0 && a.state === 'speaking');
    await emit('voice', { phase: 'spoken', turn_id: 'n1' });
    // her voice off: nothing else would reach you
    await emit('controls', { mic_muted: false, speech_muted: true, conversation: false });
    await turn('n2', '几点了', '<voice>现在下午四点，我没有出声。</voice>'); await settled();
    a = await area();
    check('her voice off: the captions are all shown, whatever the setting, and the words are all there to read, not lit as she goes', a.kind === 'area' && a.you === '几点了' && a.her === '现在下午四点，我没有出声。' && a.all === a.hers);
    check('and the footer is neutral: no “Speaking · poke to interrupt” and no speaking bars for a voice that is off', a.state === 'idle' && a.label === '');
    await shot('15-voice-off');
    await turn('n3', '今天呢', '<voice>写在下面了。</voice><document>下午三点之后的三场会议都已经往后推了一个小时。</document>'); await page.waitForTimeout(1200);
    check('her voice off: the written part is there whole at once, not written in', await page.evaluate(() => { const w = [...document.querySelectorAll('.talk .tk-w')].at(-1); return !!w && w.querySelectorAll('i').length === 0 && w.textContent === '下午三点之后的三场会议都已经往后推了一个小时。'; }));
    check('no page errors (none)', s.errors.length === 0);
    await s.context.close();
  }

  // ---- the caption setting: her menu, persisted ----
  {
    const s = await scene({ captions: 'brief' });
    const { page, move, area } = s;
    await page.waitForTimeout(700);
    await move(out.x, out.y); await page.waitForTimeout(900);
    await page.locator('.companion-hit').click({ button: 'right', force: true });
    await page.getByRole('menu').waitFor();
    const radio = name => page.getByRole('menuitemradio', { name });
    check('her menu has the three caption levels, the middle one chosen by default', await radio('Show all').count() === 1 && await radio('Only what to read').getAttribute('aria-checked') === 'true' && await radio('None').count() === 1);
    await radio('Show all').click(); await page.waitForTimeout(300);
    check('choosing one persists it with the other companion settings', await page.evaluate(() => JSON.parse(localStorage.getItem('companion-settings-v1')).captions === 'all'));
    await page.reload(); await page.waitForTimeout(800);
    await move(out.x, out.y); await page.waitForTimeout(900);
    await page.locator('.companion-hit').click({ button: 'right', force: true });
    check('and it is still chosen after a restart', await radio('Show all').getAttribute('aria-checked') === 'true');
    check('no page errors (menu)', s.errors.length === 0);
    await s.context.close();
  }

  // ---- showing and hiding: 8 s after a turn, never under the pointer, a new session each time it opens (ADR 0113) ----
  {
    const s = await scene({ captions: 'all' });
    const { page, emit, move, skew, area, turn, folded, shot } = s;
    await page.waitForTimeout(600);
    await turn('c1', '明天几点开会', '<voice>明天上午十点。</voice>', { spoken: true });
    await page.waitForTimeout(600);
    let a = await area();
    const spokenAt = Date.now();
    check('a turn is over and she is not listening: the area is still up', a.up && a.her === '明天上午十点。' && a.state === 'idle');
    await shot('20-lingering');
    await folded();
    const lingered = (Date.now() - spokenAt) / 1000;
    check(`it folds away 8 s after the turn (${lingered.toFixed(1)} s)`, lingered > 7.5 && lingered < 10.5);
    check('and she goes back into the island', await page.waitForFunction(() => document.querySelector('.companion-hit')?.dataset.place === 'home', null, { timeout: 4000 }).then(() => true, () => false));
    // opened again, however soon, it starts empty
    await skew(5 * 60_000);
    await emit('voice', { phase: 'listening', turn_id: 'c2' }); await page.waitForTimeout(1200);
    a = await area();
    check('opened again 5 minutes later it starts empty: the last session is not shown', a.up && a.yous === 0 && a.hers === 0 && a.kind === 'capsule');
    await emit('voice', { phase: 'empty', turn_id: 'c2' }); await emit('controls', { mic_muted: false, speech_muted: false, conversation: false });
    // never while the pointer is over it
    await page.waitForTimeout(500);
    a = await area();
    await move(a.r.x + a.r.w / 2, a.r.y + a.r.h / 2);
    await page.waitForTimeout(11_000);
    check('it does not fold while the pointer is over it (11 s)', (await area()).up);
    await move(600, 650);
    await folded(6000);
    check('and folds once the pointer has gone', !(await area()).up);
    // and so it does after ten
    await skew(11 * 60_000);
    await emit('voice', { phase: 'listening', turn_id: 'c3' }); await page.waitForTimeout(1200);
    a = await area();
    check('after ten minutes of quiet it starts empty', a.up && a.yous === 0 && a.hers === 0 && a.kind === 'capsule');
    check('no page errors (presence)', s.errors.length === 0);
    await s.context.close();
  }

  // ---- think mode: the deep look belongs to its turn ----
  {
    const s = await scene({ captions: 'all' });
    const { page, emit, area, turn, shot, settled, daemonState } = s;
    await page.waitForTimeout(600);
    await emit('voice', { phase: 'listening', turn_id: 'd1' });
    daemonState.think = { on: true, on_words: '深想', turn_id: 'd1' };
    await emit('voice', { phase: 'accepted', turn_id: 'd1', text: '深想一下，我下周该不该换工作' });
    await page.waitForTimeout(2600);
    let a = await area();
    check('a deep turn pending: the area takes the deep look, and the footer counts the seconds', a.deep && /^Thinking \d+ s$/.test(a.label) && await page.evaluate(() => getComputedStyle(document.querySelector('.talk')).getPropertyValue('--lit').trim() === 'rgb(154, 134, 255)'));
    check('its base is the deep gradient, its glyph the deep colour', await page.evaluate(() => getComputedStyle(document.querySelector('.tk-deep')).opacity === '1' && getComputedStyle(document.querySelector('.talk .gl'), '::before').backgroundColor !== 'rgba(0, 0, 0, 0)'));
    await shot('30-deep-thinking');
    daemonState.think = { on: false, on_words: '深想', turn_id: null }; // the daemon's `on` ends with the turn, around the answer opening
    await emit('open', { turn_id: 'd1', response_id: 'r-d1' }); await emit('append', { turn_id: 'd1', token: '<voice>先别急着换，下周你手上有两个关键交付。</voice>' }); await emit('done', { turn_id: 'd1', fadeMs: 100 });
    await page.waitForTimeout(1200);
    a = await area();
    check('her deep answer keeps the “Thought for N s” line above her words, and the deep look', a.deep && /^Thought for \d+\.\d s$/.test(a.think) && a.her.startsWith('先别急着换'));
    await shot('31-deep-answer');
    await emit('voice', { phase: 'spoken', turn_id: 'd1' });
    await emit('voice', { phase: 'listening', turn_id: 'n1' }); await emit('voice', { phase: 'accepted', turn_id: 'n1', text: '现在几点' });
    await page.waitForTimeout(1200);
    a = await area();
    check('the next, normal turn is normal again: no deep look, plain “Thinking”', !a.deep && a.label === 'Thinking' && await page.evaluate(() => getComputedStyle(document.querySelector('.talk')).getPropertyValue('--lit').trim() !== 'rgb(154, 134, 255)'));
    await shot('32-normal-again');
    check('no page errors (deep)', s.errors.length === 0);
    await s.context.close();
  }

  // ---- a slow tool shows its fixed line while the turn waits on it, at both levels that show state ----
  for (const captions of ['all', 'brief']) {
    const s = await scene({ captions });
    const { page, emit, skew, area, settled, shot } = s;
    const base = captions === 'all' ? 'Thinking' : '';
    await page.waitForTimeout(600);
    await emit('voice', { phase: 'listening', turn_id: 'k1' });
    await emit('voice', { phase: 'accepted', turn_id: 'k1', text: '帮我查一下今天的新闻' });
    await skew(3500); await settled(); // (the pill shows what she heard for 3 s)
    let a = await area();
    check(`${captions}: waiting with no tool running, the footer is the plain state (“${base}”)`, a.state === 'thinking' && a.label === base);
    await emit('tool', { turn_id: 'someone-else', label: 'Looking at your screen...' }); await settled();
    a = await area();
    check(`${captions}: a tool line for another turn does not show`, a.label === base);
    await emit('tool', { turn_id: 'k1', label: 'Searching the web...' }); await settled();
    a = await area();
    const fit = await page.evaluate(() => { const t = document.querySelector('.talk'), lb = t.querySelector('.lb'), r = t.getBoundingClientRect(), st = t.querySelector('.st').getBoundingClientRect();
      return { clipped: lb.scrollWidth > lb.clientWidth + 1, shim: lb.classList.contains('shim'), inside: st.right <= r.right + .5 }; });
    check(`${captions}: while the tool runs the line “Searching the web...” shows under her, shimmering, unclipped, the end button inside`, a.state === 'thinking' && a.label === 'Searching the web...' && fit.shim && !fit.clipped && fit.inside);
    await shot(`40-tool-${captions}`);
    await emit('tool', { turn_id: 'k1', label: 'Looking at your screen...' }); await settled();
    a = await area();
    check(`${captions}: a second tool replaces it with the latest`, a.label === 'Looking at your screen...');
    await emit('tool', { turn_id: 'k1', label: '' }); await settled();
    a = await area();
    check(`${captions}: the daemon clearing it brings the plain state back`, a.state === 'thinking' && a.label === base);
    await emit('tool', { turn_id: 'k1', label: 'Searching the web...' }); await settled();
    // ADR 0121: the daemon's wait line ("One moment.") comes as a commentary-phase response of the same turn, and a wait line is speech only.
    const waitLine = async () => {
      await emit('open', { turn_id: 'k1', response_id: 'r-wait', response_phase: 'commentary' });
      await emit('append', { turn_id: 'k1', token: '<voice>One moment.</voice>', response_phase: 'commentary' });
      await emit('done', { turn_id: 'k1', fadeMs: 100, response_phase: 'commentary' });
      await emit('voice', { phase: 'playing', turn_id: 'k1', played: 3, ahead: 6, held: false, response_phase: 'commentary' });
      await emit('voice', { phase: 'spoken', turn_id: 'k1', output_outcome: 'completed', response_phase: 'commentary' });
      await page.waitForTimeout(1200);
    };
    await waitLine();
    a = await area();
    check(`${captions}: a wait line neither clears the tool line nor shows as her answer`, a.label === 'Searching the web...' && a.state === 'thinking' && !a.her.includes('moment') && a.hers === 0);
    await emit('open', { turn_id: 'k1', response_id: 'r-k1' }); await emit('append', { turn_id: 'k1', token: '<voice>今天有三条新闻。</voice>' }); await emit('done', { turn_id: 'k1', fadeMs: 100 });
    await page.waitForTimeout(1200);
    a = await area();
    check(`${captions}: when her answer opens the line is gone`, !/\.\.\.$/.test(a.label) && a.footer.indexOf('Searching') < 0);
    await emit('voice', { phase: 'spoken', turn_id: 'k1' });
    // The live order: her wait line opens first, the tool starts after it, and the line still shows.
    await emit('voice', { phase: 'listening', turn_id: 'k3' }); await emit('voice', { phase: 'accepted', turn_id: 'k3', text: '搜一下' });
    await emit('open', { turn_id: 'k3', response_id: 'r-wait3', response_phase: 'commentary' });
    await emit('append', { turn_id: 'k3', token: '<voice>On it.</voice>', response_phase: 'commentary' });
    await emit('done', { turn_id: 'k3', fadeMs: 100, response_phase: 'commentary' });
    await skew(3500); await settled(); // (the pill shows what she heard for 3 s)
    await emit('tool', { turn_id: 'k3', label: 'Searching the web...' }); await settled();
    a = await area();
    check(`${captions}: a wait line before the tool starts still lets the tool line show`, a.label === 'Searching the web...' && a.state === 'thinking');
    await emit('open', { turn_id: 'k3', response_id: 'r-k3' }); await emit('append', { turn_id: 'k3', token: '<voice>好了。</voice>' }); await emit('done', { turn_id: 'k3', fadeMs: 100 });
    await page.waitForTimeout(1200);
    a = await area();
    check(`${captions}: and her answer's open still clears it`, a.footer.indexOf('Searching') < 0 && a.label.indexOf('Searching') < 0);
    await emit('voice', { phase: 'spoken', turn_id: 'k3' });
    // The line is the tool's, not her state's: it stays while she says something else. (1) The turn's first words opened before the tool ran.
    await emit('voice', { phase: 'listening', turn_id: 'k5' }); await emit('voice', { phase: 'accepted', turn_id: 'k5', text: '再搜一下' });
    await emit('open', { turn_id: 'k5', response_id: 'r-k5' }); await emit('append', { turn_id: 'k5', token: '<voice>好，我来查一下。</voice>' });
    await skew(3500); await settled();
    await emit('tool', { turn_id: 'k5', label: 'Searching the web...' }); await settled();
    a = await area();
    check(`${captions}: a tool that runs after her first words opened still shows its line, while she says them`, a.label === 'Searching the web...' && a.state === 'speaking');
    await shot(`41-tool-while-speaking-${captions}`);
    await emit('tool', { turn_id: 'k5', label: '' }); await settled();
    a = await area();
    check(`${captions}: the daemon clearing it while she speaks takes the line and nothing else`, a.state === 'speaking' && a.label === (captions === 'all' ? 'Speaking · poke to interrupt' : ''));
    await emit('voice', { phase: 'spoken', turn_id: 'k5' });
    // (2) An earlier answer is still being said when the next turn's tool starts.
    await emit('voice', { phase: 'listening', turn_id: 'k6' }); await emit('voice', { phase: 'accepted', turn_id: 'k6', text: '先说一个' });
    await emit('open', { turn_id: 'k6', response_id: 'r-k6' }); await emit('append', { turn_id: 'k6', token: '<voice>今天有三条新闻，我慢慢给你说。</voice>' }); await emit('done', { turn_id: 'k6', fadeMs: 100 });
    await emit('voice', { phase: 'listening', turn_id: 'k7' }); await emit('voice', { phase: 'accepted', turn_id: 'k7', text: '再查一个' });
    await skew(3500); await settled();
    await emit('tool', { turn_id: 'k7', label: 'Searching the web...' }); await settled();
    a = await area();
    check(`${captions}: an earlier answer still being said does not hide the next turn's tool line`, a.label === 'Searching the web...');
    await emit('voice', { phase: 'spoken', turn_id: 'k6' }); await settled();
    a = await area();
    check(`${captions}: and its end does not take the line either`, a.label === 'Searching the web...' && a.state === 'thinking');
    await emit('failed', { turn_id: 'k7', reason: 'error', message: 'x' }); await settled();
    await emit('voice', { phase: 'listening', turn_id: 'k2' }); await emit('voice', { phase: 'accepted', turn_id: 'k2', text: '再查一个' });
    await skew(3500); await settled();
    await emit('tool', { turn_id: 'k2', label: 'Searching the web...' }); await settled();
    a = await area();
    check(`${captions}: a second turn shows its tool too`, a.label === 'Searching the web...');
    await emit('failed', { turn_id: 'k2', reason: 'error', message: 'x' }); await settled();
    a = await area();
    check(`${captions}: the turn ending without an answer takes the line with it`, !a.label.includes('Searching') && !a.footer.includes('Searching'));
    check(`${captions}: no page errors (tool line)`, s.errors.length === 0);
    await s.context.close();
  }

  // ---- reduced motion ----
  {
    const s = await scene({ captions: 'all', reduced: true });
    const { page, emit, area } = s;
    await page.waitForTimeout(600);
    await emit('voice', { phase: 'listening', turn_id: 'r1' }); await emit('voice', { phase: 'accepted', turn_id: 'r1', text: '你好' });
    await emit('open', { turn_id: 'r1', response_id: 'r-r1' }); await emit('append', { turn_id: 'r1', token: '<voice>你好，Allen。</voice>' });
    await page.waitForTimeout(250);
    const a = await area(), running = await page.evaluate(() => document.querySelector('.talk').getAnimations({ subtree: true }).filter(x => x.playState === 'running' && x.effect.getComputedTiming().iterations !== Infinity).length);
    check(`reduced motion: it is simply there at its size (${Math.round(a.r.w)} x ${Math.round(a.r.h)}), no shape animation running (${running})`, a.up && Math.round(a.r.w) === 360 && a.her === '你好，Allen。' && running === 0);
    await emit('append', { turn_id: 'r1', token: '<document>下午三点之后的三场会议都已经往后推了一个小时。</document>' }); await emit('done', { turn_id: 'r1', fadeMs: 100 }); await page.waitForTimeout(1200);
    check('reduced motion: the written part is there whole, fully lit, nothing writes in', await page.evaluate(() => { const w = [...document.querySelectorAll('.talk .tk-w')].at(-1); return !!w && w.querySelectorAll('i').length === 0 && getComputedStyle(w.querySelector('p')).color === 'rgb(238, 240, 251)'; }));
    check('no page errors (reduced)', s.errors.length === 0);
    await s.context.close();
  }

  // ---- review round: endings, queues, the fold, the pill's motion, the copy ----
  {
    const s = await scene({ captions: 'all' });
    const { page, emit, move, skew, area, settled, turn, posts } = s;
    await page.waitForTimeout(600);
    const mic = () => posts.filter(p => p.path === '/inherent/controls' && 'mic_muted' in p.body).map(p => p.body.mic_muted);
    const lits = () => page.evaluate(() => [...document.querySelectorAll('.talk .tk-h')].map(h => [h.querySelectorAll('i.on').length, h.querySelectorAll('.tk-s.all').length]));

    // stop pressed while the area lingers must not leave the next conversation without it
    await emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); s.daemonState.controls.conversation = true;
    await turn('r1', '几点了', '<voice>现在下午四点。</voice>'); await emit('voice', { phase: 'spoken', turn_id: 'r1' }); await settled();
    await emit('controls', { mic_muted: false, speech_muted: false, conversation: false }); s.daemonState.controls.conversation = false; await settled();
    let a = await area();
    check('a turn is over and she is not listening: it lingers', a.up && a.state === 'idle');
    await page.locator('.talk-ft .st').click(); await page.waitForTimeout(900);
    check('the end button while it lingers folds it', !(await area()).up);
    await emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); s.daemonState.controls.conversation = true; await emit('voice', { phase: 'listening', turn_id: 'r2' }); await settled();
    check('and the next conversation opens it again (it is not left off for good)', (await area()).up);

    // an answer queued behind one still being said does not cut it, and starts only when it is over
    await emit('voice', { phase: 'accepted', turn_id: 'r2', text: '念长一点' }); await emit('open', { turn_id: 'r2', response_id: 'r-r2' });
    await emit('append', { turn_id: 'r2', token: `<voice>${long}</voice>` }); await emit('done', { turn_id: 'r2', fadeMs: 100 });
    await skew(4000); await page.waitForTimeout(300);
    const first = (await lits()).at(-1)[0];
    await emit('open', { turn_id: 'q2', response_id: 'r-q2' }); await emit('append', { turn_id: 'q2', token: '<voice>第二个回答，排在后面。</voice>' }); await emit('done', { turn_id: 'q2', fadeMs: 100 });
    await skew(4000); await page.waitForTimeout(300);
    let l = await lits();
    check(`the answer queued behind it does not cut it: the first goes on lighting (${first} → ${l.at(-2)[0]}), the second has not begun (${l.at(-1)[0]})`, l.at(-2)[0] > first + 10 && l.at(-1)[0] === 0 && l.at(-2)[1] === 0);
    await emit('voice', { phase: 'spoken', turn_id: 'r2' }); await skew(1500); await page.waitForTimeout(300);
    l = await lits();
    check(`when the first is over the second begins (${l.at(-1)[0]})`, l.at(-1)[0] > 0 && l.at(-2)[1] === 1);
    await emit('voice', { phase: 'spoken', turn_id: 'q2' });

    // playback stopped: where she got to stays lit, not everything
    await emit('open', { turn_id: 'q3', response_id: 'r-q3' }); await emit('append', { turn_id: 'q3', token: `<voice>${long}</voice>` }); await emit('done', { turn_id: 'q3', fadeMs: 100 });
    await skew(4000); await page.waitForTimeout(300);
    await emit('voice', { phase: 'spoken', turn_id: 'q3', output_outcome: 'interrupted' }); await page.waitForTimeout(400);
    l = await lits();
    const total = await page.evaluate(() => [...document.querySelectorAll('.talk .tk-h')].at(-1).querySelectorAll('i').length);
    check(`a spoken that says it was interrupted leaves it lit only as far as she got (${l.at(-1)[0]} of ${total})`, l.at(-1)[0] > 5 && l.at(-1)[0] < total - 20 && l.at(-1)[1] === 0);

    // poking her to end the voice while the field is open gives the microphone back and closes the field
    await page.locator('.talk-ft .kb').click(); await page.waitForTimeout(700);
    check('the field is open and the microphone is paused', (await area()).fieldShown && mic().at(-1) === true);
    await page.keyboard.type('一些字');
    await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace'); await page.waitForTimeout(300);
    check('select-all and delete keeps the field open', (await area()).fieldShown);
    await move(out.x, out.y); await page.locator('.companion-hit').click({ force: true });
    await page.waitForTimeout(900);
    a = await area();
    check('poking her to end the voice closes the field and unmutes the microphone', !a.fieldShown && mic().at(-1) === false && posts.some(p => p.path === '/inherent/controls' && p.body.conversation === false));
    check('and no key is left behind saying we paused it', await page.evaluate(() => localStorage.getItem('companion-mic-paused') === null));
    check('no page errors (review round)', s.errors.length === 0);
    await s.context.close();
  }

  // a mic paused for typing is given back after a reload, unless someone else unmuted it meanwhile
  {
    const s = await scene({ captions: 'all' });
    const { page, emit, turn, posts } = s;
    await page.waitForTimeout(600);
    await emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); s.daemonState.controls.conversation = true;
    await turn('p1', '你好', '<voice>你好。</voice>'); await page.waitForTimeout(800);
    await page.locator('.talk-ft .kb').click(); await page.waitForTimeout(700);
    check('opening the field in a voice conversation records that we paused the microphone', await page.evaluate(() => localStorage.getItem('companion-mic-paused') === '1'));
    const before = posts.filter(p => p.path === '/inherent/controls' && p.body.mic_muted === false).length;
    await page.reload(); await page.waitForFunction(() => window.__sockets?.length === 1); await page.waitForTimeout(900);
    check('a reload with the field open gives the microphone back and clears the record', posts.filter(p => p.path === '/inherent/controls' && p.body.mic_muted === false).length === before + 1 && await page.evaluate(() => localStorage.getItem('companion-mic-paused') === null));
    await s.context.close();
    const t = await scene({ captions: 'all' });
    await t.page.waitForTimeout(600);
    await t.emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); t.daemonState.controls.conversation = true;
    await t.turn('p2', '你好', '<voice>你好。</voice>'); await t.page.waitForTimeout(800);
    await t.page.locator('.talk-ft .kb').click(); await t.page.waitForTimeout(700);
    const n = t.posts.filter(p => p.path === '/inherent/controls' && p.body.mic_muted === false).length;
    await t.emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); // someone else unmutes it
    await t.page.waitForTimeout(300);
    await t.page.keyboard.press('Escape'); await t.page.waitForTimeout(500);
    check('a microphone someone else unmuted while the field was up is left as it is when it closes', t.posts.filter(p => p.path === '/inherent/controls' && p.body.mic_muted === false).length === n && await t.page.evaluate(() => localStorage.getItem('companion-mic-paused') === null));
    await t.context.close();
  }

  // Esc on an empty field with nothing to show does not leave an empty capsule up
  {
    const s = await scene({ captions: 'all' });
    const { page, move, area, folded } = s;
    await page.waitForTimeout(600);
    await move(out.x, out.y); await page.waitForTimeout(700);
    await page.locator('.companion-chip button').click(); await page.waitForTimeout(900);
    check('the keyboard opens the field with nothing to show yet', (await area()).fieldShown);
    await page.keyboard.press('Escape');
    await folded(2500);
    check('Esc on the empty field folds it at once, not 8 s later', !(await area()).up);
    await s.context.close();
  }

  // deep: a stale answer of the daemon for a cancelled turn does not leak onto the next ordinary turn
  {
    const s = await scene({ captions: 'all' });
    const { page, emit, area, daemonState } = s;
    await page.waitForTimeout(600);
    await emit('voice', { phase: 'listening', turn_id: 'c1' });
    daemonState.think = { on: true, on_words: '深想', turn_id: 'c1' };
    await emit('voice', { phase: 'accepted', turn_id: 'c1', text: '深想一下' }); await page.waitForTimeout(1500);
    check('a deep turn pending takes the deep look', (await area()).deep);
    await emit('cancelled', { turn_id: 'c1' }); await page.waitForTimeout(500);
    await emit('voice', { phase: 'listening', turn_id: 'c2' }); await emit('voice', { phase: 'accepted', turn_id: 'c2', text: '现在几点' }); await page.waitForTimeout(1500);
    const a = await area();
    check('the daemon still answers on:true for the cancelled turn: the next ordinary turn is not deep', !a.deep && a.label === 'Thinking');
    await s.context.close();
  }

  // the bottom of the transcript fades like the top, and “Back to latest” is not in the faded transcript
  {
    const s = await scene({ captions: 'all' });
    const { page, emit, skew, area, turn } = s;
    await page.waitForTimeout(600);
    await emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); s.daemonState.controls.conversation = true;
    await turn('f1', '念一遍', ['<voice>', long, long, '</voice>']); await skew(70_000); await page.waitForTimeout(1200);
    await emit('voice', { phase: 'spoken', turn_id: 'f1' }); await page.waitForTimeout(800);
    check('at the end of a long transcript only the top fades', await page.evaluate(() => { const t = document.querySelector('.talk-tr'); return t.classList.contains('more') && !t.classList.contains('below'); }));
    await page.mouse.move(196, 240); await page.mouse.wheel(0, -90); await page.waitForTimeout(600);
    check('scrolled into the middle it fades at both edges, and the chip sits outside the faded words', await page.evaluate(() => { const t = document.querySelector('.talk-tr'); return t.classList.contains('more') && t.classList.contains('below') && !t.querySelector('.tk-latest') && !!document.querySelector('.talk > .tk-latest button'); }));
    await page.mouse.wheel(0, -2000); await page.waitForTimeout(600);
    check('at the top only the bottom fades', await page.evaluate(() => { const t = document.querySelector('.talk-tr'); return !t.classList.contains('more') && t.classList.contains('below'); }));
    // the fold keeps what was on screen
    await page.locator('.talk .tk-latest button').click(); await page.waitForTimeout(1000);
    const start = (await area()).tr.top;
    await page.evaluate(() => { window.__st = []; const tr = document.querySelector('.talk-tr'); window.__sample = setInterval(() => { if (document.querySelector('.talk').hasAttribute('data-hit')) window.__st.push(tr.scrollTop); }, 16); });
    await emit('controls', { mic_muted: false, speech_muted: false, conversation: false }); s.daemonState.controls.conversation = false;
    await skew(9000); await s.folded();
    const seen = await page.evaluate(() => { clearInterval(window.__sample); return window.__st; });
    check(`while it folds the transcript stays where it was and does not jump to the top (${start} px, ${Math.min(...seen)} px lowest of ${seen.length} samples)`, start > 20 && seen.length > 3 && Math.min(...seen) >= start - 3);
    await s.context.close();
  }

  // the brief pill: its button stays inside the shape, pinned to the right, while it widens and closes up
  {
    const s = await scene({ captions: 'brief', lang: 'zh' });
    const { page, emit, skew, area, settled } = s;
    await page.waitForTimeout(600);
    await page.evaluate(() => {
      window.__rec = [];
      const f = () => { const t = document.querySelector('.talk'), b = t.getBoundingClientRect(), st = t.querySelector('.talk-ft .st')?.getBoundingClientRect(), gl = t.querySelector('.talk-ft .gl')?.getBoundingClientRect();
        if (t.hasAttribute('data-hit') && st) window.__rec.push({ bw: b.width, out: st.right - b.right, gl: gl.left - b.left }); requestAnimationFrame(f); };
      f();
    });
    await emit('voice', { phase: 'listening', turn_id: 'k1' }); await settled();
    await page.evaluate(() => { window.__rec.length = 0; });
    await emit('voice', { phase: 'accepted', turn_id: 'k1', text: '明天上午十点提醒我开会' }); await page.waitForTimeout(900);
    let rec = await page.evaluate(() => window.__rec.filter(r => r.bw >= 72));
    check(`widening to show what you said: the end button is inside the shape and pinned right in every frame (${rec.length} frames, ${Math.max(...rec.map(r => r.out)).toFixed(1)} to ${Math.min(...rec.map(r => r.out)).toFixed(1)})`, rec.length > 8 && rec.every(r => r.out <= 1 && r.out >= -9));
    await emit('open', { turn_id: 'k1', response_id: 'r-k1' }); await emit('append', { turn_id: 'k1', token: '<voice>好。</voice>' }); await emit('done', { turn_id: 'k1', fadeMs: 100 });
    await skew(2700); await page.evaluate(() => { window.__rec.length = 0; }); await skew(300); await page.waitForTimeout(900);
    rec = await page.evaluate(() => window.__rec.filter(r => r.bw >= 72));
    check(`closing up around the glyph: the end button does not jump left while the shape is still wide (${rec.length} frames, ${Math.max(...rec.map(r => r.out)).toFixed(1)} to ${Math.min(...rec.map(r => r.out)).toFixed(1)})`, rec.length > 3 && rec.every(r => r.out <= 1 && r.out >= -9));
    const end = await area();
    check('and it ends as the 36 tall pill round the glyph and the button', end.r.h === 36 && end.r.w < 100);
    await s.context.close();
  }

  // sending from the field: the footer waits until the sent words have left it
  {
    const s = await scene({ captions: 'all' });
    const { page, emit, turn } = s;
    await page.waitForTimeout(600);
    await emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); s.daemonState.controls.conversation = true;
    await turn('g1', '你好', '<voice>你好。</voice>'); await emit('voice', { phase: 'spoken', turn_id: 'g1' }); await page.waitForTimeout(900);
    await page.locator('.talk-ft .kb').click(); await page.waitForTimeout(800);
    await page.keyboard.type('帮我把周五的会议改到下午'); await page.keyboard.press('Enter');
    await page.waitForTimeout(110);
    const early = await page.evaluate(() => parseFloat(getComputedStyle(document.querySelector('.talk-ft')).opacity));
    await page.waitForTimeout(1100);
    const late = await page.evaluate(() => parseFloat(getComputedStyle(document.querySelector('.talk-ft')).opacity));
    check(`the footer is not there while the ghost of what you sent leaves the field (${early.toFixed(2)}), and is there after (${late.toFixed(2)})`, early < .1 && late > .95);
    await s.context.close();
  }

  // typed in the Dashboard: the question is in the conversation under her above its answer
  {
    const s = await scene({ captions: 'all' });
    const { page, move, emit, shot } = s;
    await page.waitForTimeout(600);
    await move(600, 650);
    await page.locator('.companion-island-target').click({ position: { x: 155, y: 14 }, force: true });
    await page.locator('.companion-dashboard.is-open').waitFor(); await page.waitForTimeout(900);
    await page.locator('.ad .cmp input').first().fill('明天有什么会'); await page.keyboard.press('Enter'); await page.waitForTimeout(400);
    await emit('open', { turn_id: 'typed-1', response_id: 'r-t1' }); await emit('append', { turn_id: 'typed-1', token: '<voice>明天有两个会。</voice>' }); await emit('done', { turn_id: 'typed-1', fadeMs: 100 });
    await emit('voice', { phase: 'spoken', turn_id: 'typed-1' }); await page.waitForTimeout(300);
    await page.locator('.companion-island-target').click({ position: { x: 155, y: 14 }, force: true }); await move(600, 650); await page.waitForTimeout(1200);
    await emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); s.daemonState.controls.conversation = true; await emit('voice', { phase: 'listening', turn_id: 'typed-2' }); await page.waitForTimeout(1500);
    const lines = () => page.evaluate(() => [...document.querySelectorAll('.talk-tr [data-line]')].map(n => n.textContent));
    check('opened after a question typed in the Dashboard: it starts empty', (await lines()).length === 0);
    await emit('voice', { phase: 'accepted', turn_id: 'typed-2', text: '后天呢' });
    await emit('open', { turn_id: 'typed-2', response_id: 'r-t2' }); await emit('append', { turn_id: 'typed-2', token: '<voice>后天没有会。</voice>' }); await emit('done', { turn_id: 'typed-2', fadeMs: 100 });
    await emit('voice', { phase: 'spoken', turn_id: 'typed-2' }); await page.waitForTimeout(1500);
    check('the new exchange alone shows', (await lines()).join('|') === '后天呢|后天没有会。');
    const r = await page.locator('.talk-tr').boundingBox();
    await page.mouse.move(r.x + r.width / 2, r.y + r.height / 2);
    for (let i = 0; i < 14; i++) { await page.mouse.wheel(0, -60); await page.waitForTimeout(16); }
    await page.waitForTimeout(1200);
    await shot('40-pulled-earlier');
    const text = await lines();
    check(`pulled up, the question typed in the Dashboard is a line of the conversation, above its answer and above the new exchange (${text.join('|')})`, text.join('|') === '明天有什么会|明天有两个会。|后天呢|后天没有会。');
    await s.context.close();
  }

  // ---- English, kept in step with where her voice really is (ADR 0112) ----
  // The daemon says how many letters and digits she has played and how many the segment playing now reaches; the lit words
  // follow that, carried on at pace in between, and stop where she is held or stopped. Nothing here is estimated from a clock
  // alone: the clock is skewed by a minute and the lit words stay where she is.
  {
    const s = await scene({ captions: 'all', lang: 'en' });
    const { page, emit, skew, shot, area, turn } = s;
    await page.waitForTimeout(600);
    const sentenceList = ['Mara found a brass compass in her grandfather’s attic.', ' Its needle pointed not north, but toward whatever someone had lost.', ' She followed it through town, returning a red scarf, a missing violin, and a little boy’s courage before his school play.',
      ' At sunset, the compass led her to the harbor, where her grandfather waited beside his old sailboat.', ' “I lost my first adventure,” he said.', ' Together they sailed around the bay, laughing as the wind filled the sails.', ' When they returned, the compass needle rested quietly.'];
    const story = sentenceList.join('');
    // The characters said once `n` letters and digits have been (and the marks right after the last): what the page must have lit.
    const L = /[\p{L}\p{N}]/u, chars = [...story];
    const after = n => { let seen = 0, i = 0; if (n <= 0) return 0; for (; i < chars.length; i++) if (L.test(chars[i]) && ++seen === n) { i++; break; } while (i < chars.length && !/\s/.test(chars[i]) && !L.test(chars[i])) i++; return i; };
    const letters = text => [...text].filter(c => L.test(c)).length;
    const upto = k => letters(sentenceList.slice(0, k).join('')); // letters through the first k sentences
    const playing = (turnId, played, ahead, held = false) => emit('voice', { phase: 'playing', turn_id: turnId, played, ahead, held });
    await emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); s.daemonState.controls.conversation = true;

    await turn('e1', 'tell me a story', sentenceList, { done: true });
    await page.waitForTimeout(1200);
    let a = await area();
    check('English: sentences keep the space between them (no “attic.Its”, “lost.She”)', a.her === story && !/[.”]\p{Lu}/u.test(a.her));
    check('English: the space is drawn as a space, not collapsed away by the page', await page.evaluate(() => { const t = document.querySelector('.talk .tk-s').innerText; return t.includes('attic. Its needle') && t.includes('lost. She followed') && t.includes('play. At sunset') && t.includes('boat. “I lost'); }));
    await shot('en-01-spaces');

    // she is far ahead of where the clock puts her: the first report catches the lit words up at once
    const lit0 = a.lit;
    await playing('e1', upto(3), upto(3));
    await page.waitForTimeout(300);
    a = await area();
    check(`anchored: the first report puts the lit words at the end of the third sentence at once (${lit0} → ${a.lit}, wanted ${after(upto(3))})`, a.lit === after(upto(3)) && lit0 < 60);
    await shot('en-02-caught-up');
    // …and not a character past the report, whatever the clock says
    await skew(60_000); await page.waitForTimeout(400);
    a = await area();
    check(`anchored: a minute of the clock later the lit words have not moved past the report (${a.lit})`, a.lit === after(upto(3)) && a.all === 0);
    // while she is inside the next segment the clock carries the words on, but never past its end
    await playing('e1', upto(3), upto(4));
    await skew(1500); await page.waitForTimeout(400);
    a = await area();
    check(`between reports the lit words move on at pace inside the segment she is in (${a.lit})`, a.lit > after(upto(3)) && a.lit <= after(upto(4)));
    await skew(60_000); await page.waitForTimeout(400);
    a = await area();
    check(`…and stop at the end of that segment until the next one starts (${a.lit} of ${after(upto(4))})`, a.lit === after(upto(4)));
    await playing('e1', upto(5), upto(6));
    await page.waitForTimeout(300);
    a = await area();
    check(`the next report moves them on: at least up to the text before the segment now playing (${a.lit}, from ${after(upto(5))})`, a.lit >= after(upto(5)) && a.lit < after(upto(5)) + 12);
    // her voice is slower than the pace: the next report is behind where the pace carried the words. They wait for her, never step back.
    await skew(2500); await page.waitForTimeout(400);
    const carried = (await area()).lit, seen = [];
    await playing('e1', upto(5) + 3, upto(6));
    for (let k = 0; k < 6; k++) { await page.waitForTimeout(90); seen.push((await area()).lit); }
    check(`a report behind the paced words does not pull them back (${carried} → ${seen.join(',')}, report at ${after(upto(5) + 3)})`, carried > after(upto(5) + 3) && seen.every(n => n >= carried));

    // soft barge-in: she is held while Allen's words are judged; the lit words stop with her
    await playing('e1', upto(5) + 7, upto(6));
    await page.waitForTimeout(300);
    await playing('e1', upto(5) + 7, upto(6), true);
    await page.waitForTimeout(300);
    const before = (await area()).lit;
    await skew(30_000); await page.waitForTimeout(500);
    a = await area();
    check(`held: the lit words do not move while she is held, a half minute of the clock later (${before} → ${a.lit})`, a.lit === before && a.all === 0 && before >= after(upto(5) + 7));
    await playing('e1', upto(5) + 7, upto(6), false);
    await page.waitForTimeout(300);
    check('let go: they go on from where she was held, not from where the clock has got to', (await area()).lit - before <= 6);
    await skew(1500); await page.waitForTimeout(400);
    a = await area();
    check(`let go: and then they move again (${before} → ${a.lit})`, a.lit > before + 6 && a.lit <= after(upto(6)));

    // stopped for good: held at the stop, then the terminal event much later; nothing moves after the audio did
    await playing('e1', upto(6) - 12, upto(6), true);
    await page.waitForTimeout(300);
    const stopped = (await area()).lit;
    await skew(8000);
    await emit('voice', { phase: 'spoken', turn_id: 'e1', output_outcome: 'interrupted' });
    await skew(8000); await page.waitForTimeout(500);
    a = await area();
    check(`interrupted: the lit words stop where the audio stopped, not at the terminal event and not at the end (${stopped} → ${a.lit})`, a.lit === stopped && a.all === 0 && stopped === after(upto(6) - 12));
    await shot('en-03-interrupted');
    check('no page errors (English anchors)', s.errors.length === 0);
    await s.context.close();
  }

  // interrupted with no report in between (a hard stop that the daemon answers only with the terminal event): the lit words stop at that event
  {
    const s = await scene({ captions: 'all', lang: 'en' });
    const { page, emit, skew, area, turn } = s;
    await page.waitForTimeout(600);
    const text = 'The harbor was quiet that morning, and the boats rocked slowly against the old wooden pier while the gulls circled above them.';
    await emit('controls', { mic_muted: false, speech_muted: false, conversation: true }); s.daemonState.controls.conversation = true;
    await turn('h1', 'go on', [text], { done: true });
    await emit('voice', { phase: 'playing', turn_id: 'h1', played: 4, ahead: 40, held: false });
    await skew(2000); await page.waitForTimeout(300);
    await emit('voice', { phase: 'spoken', turn_id: 'h1', output_outcome: 'interrupted' });
    await page.waitForTimeout(300);
    const at = (await area()).lit;
    await skew(20_000); await page.waitForTimeout(500);
    const a = await area();
    check(`interrupted without a hold: the lit words stop at the terminal event and stay (${at}), whatever the clock does after`, a.lit === at && at > 4 && a.all === 0);
    await s.context.close();
  }

  // the copy: one set of caption names, and the control is not under the “can't change these yet” banner
  {
    const s = await scene({ captions: 'brief', lang: 'en' });
    const { page, move } = s;
    await page.waitForTimeout(600);
    await move(600, 650);
    await page.locator('.companion-island-target').click({ position: { x: 155, y: 14 }, force: true });
    await page.locator('.companion-dashboard.is-open').waitFor(); await page.waitForTimeout(900);
    await page.locator('.ad .corner [data-row="settings"]').click(); await page.waitForTimeout(700);
    await page.locator('.ad [data-cat="voice"]').click(); await page.waitForTimeout(700);
    const names = await page.locator('.ad [role=radiogroup][aria-label="Captions"] button').allTextContents();
    check(`Settings names the levels ${names.join(' / ')}`, names.join() === 'Show all,Only what to read,None');
    check('the captions control sits above the banner, which is about what the daemon keeps', await page.evaluate(() => { const warn = document.querySelector('.ad .st-warn'), group = document.querySelector('.ad [role=radiogroup][aria-label="Captions"]');
      return !!warn && !!group && !!(warn.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_PRECEDING); }));
    await s.context.close();
  }

  // the demo (no daemon): typing in the middle of a voice turn answers like a real turn, the footer follows
  {
    const s = await scene({ captions: 'all', demo: true });
    const { page, move, area } = s;
    await page.waitForTimeout(800);
    await move(out.x, out.y); await page.locator('.companion-hit').click({ force: true }); await page.waitForTimeout(1200);
    await page.locator('.talk-ft .kb').click(); await page.waitForTimeout(800);
    await page.keyboard.type('收到吗'); await page.keyboard.press('Enter'); await page.waitForTimeout(300);
    let a = await area();
    check('demo: after typing mid-voice the footer says Thinking, not Listening', a.state === 'thinking' && a.label === 'Thinking');
    await page.waitForTimeout(1500);
    a = await area();
    check('demo: then she speaks, and the answer is lit as she says it', a.state === 'speaking' && a.her !== '');
    await s.context.close();
  }

  // ---- frames of every transition, on request ----
  if (frames) {
    const s = await scene({ captions: 'all' });
    const { page, emit, skew, rec, folded } = s;
    await page.waitForTimeout(600);
    await emit('voice', { phase: 'listening', turn_id: 'f1' }); await page.waitForTimeout(1000);
    await emit('voice', { phase: 'accepted', turn_id: 'f1', text: '把明天的安排念一遍' }); await page.waitForTimeout(1000);
    await emit('open', { turn_id: 'f1', response_id: 'r-f1' }); await emit('append', { turn_id: 'f1', token: written }); await emit('done', { turn_id: 'f1', fadeMs: 100 }); await page.waitForTimeout(1800);
    await emit('voice', { phase: 'spoken', turn_id: 'f1' }); await skew(0); await page.waitForTimeout(500);
    await emit('controls', { mic_muted: false, speech_muted: false, conversation: false });
    await folded();
    await page.waitForTimeout(800);
    const list = await rec.stop();
    await sheet(list, 'frames.png', { x: 10, y: 70, width: 190, height: 220 }, 8);
    await s.context.close();
  }

  writeFileSync(path.join(dir, 'verification.json'), JSON.stringify({ checks }, null, 2));
  console.log(`${checks.length} checks passed, ${failed.length} failed; evidence in ${dir}`);
  if (failed.length) process.exitCode = 1;
} finally {
  await browser.close();
  server.kill();
}
