// First launch: the whole flow in headless Chrome against the built page, from 打开 to the handover.
// The main process is stubbed (notch, cursor, account name, system language, permission answers), and a
// fixture daemon answers the setup routes the way the onboarding contract says. Run after `npm run build`.
// Screenshots land in evidence/first-run/.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = path.join(root, 'evidence/first-run');
mkdirSync(dir, { recursive: true });
const port = Number(process.env.FIRST_RUN_PORT ?? 5193);
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); };

// ---- the fixture daemon: records every call, answers like the draft contract ----
const calls = [];
let pluginPolls = 0;
const VOICES = [{ id: 'English_Graceful_Lady', label: 'Graceful', note: 'Warm, unhurried' }, { id: 'English_Trustworth_Man', label: 'Steady', note: 'Calm and clear' },
  { id: 'English_Whispering_girl', label: 'Soft', note: 'Quiet, close' }, { id: 'English_CalmWoman', label: 'Calm', note: 'Even and gentle' }, { id: 'English_Diligent_Man', label: 'Crisp', note: 'Direct' }];
const wav = (() => { const n = 8000, b = Buffer.alloc(44 + n * 2); b.write('RIFF', 0); b.writeUInt32LE(36 + n * 2, 4); b.write('WAVEfmt ', 8); b.writeUInt32LE(16, 16);
  b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22); b.writeUInt32LE(16000, 24); b.writeUInt32LE(32000, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write('data', 36); b.writeUInt32LE(n * 2, 40); return b; })();
const daemon = createServer((req, res) => {
  const cors = { 'access-control-allow-origin': '*', 'access-control-allow-headers': 'content-type', 'access-control-allow-methods': 'GET, POST' };
  if (req.method === 'OPTIONS') { res.writeHead(204, cors); res.end(); return; }
  let text = '';
  req.on('data', c => { text += c; });
  req.on('end', () => {
    const body = text ? JSON.parse(text) : undefined, url = req.url;
    calls.push({ url, body });
    const json = (status, value) => { res.writeHead(status, { ...cors, 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
    if (url === '/inherent/setup/key' && body.provider === 'openai') {
      if (body.key !== 'sk-good') return json(200, { ok: false, checks: [{ id: 'connect', ok: false, reason: 'unauthorized', detail: 'Incorrect API key' }] });
      return json(200, { ok: true, first_line: 'Hi Allen. I’m Nova, and this key works.', seconds: .42,
        checks: [{ id: 'connect', ok: true }, { id: 'chat', ok: true }, { id: 'deep', ok: true }, { id: 'live', ok: false, reason: 'model_denied', detail: 'gpt-live-1' }] });
    }
    if (url === '/inherent/setup/key') return json(200, { ok: true, checks: [{ id: 'connect', ok: true }], voices: body.provider === 'minimax' ? VOICES : undefined });
    if (url === '/inherent/setup/voice-preview') { res.writeHead(200, { ...cors, 'content-type': 'audio/wav' }); res.end(wav); return; }
    if (url === '/inherent/plugins') {
      const state = ++pluginPolls > 3 ? 'ready' : 'authorizing';
      return json(200, { plugins: [{ id: 'notion', status: state === 'ready' ? 'ready' : 'offered', supported: true }], request: pluginPolls > 1 ? { id: 'r1', plugin_id: 'notion', state } : null });
    }
    if (url === '/inherent/plugins/action') return json(200, { plugins: [{ id: 'notion', status: 'offered', supported: true }], request: { id: 'r1', plugin_id: 'notion', state: body.operation === 'open' ? 'offered' : 'authorizing' } });
    if (url === '/inherent/setup/done') return json(200, { ok: true, restarting: true });
    return json(200, { ok: true });
  });
});
await new Promise(r => daemon.listen(0, '127.0.0.1', r));
const daemonPort = String(daemon.address().port);

const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(port), '--strictPort'], { cwd: root, stdio: 'ignore' });
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--autoplay-policy=no-user-gesture-required'] });
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${port}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const page = await (await browser.newContext({ viewport: { width: 1512, height: 982 } })).newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
  page.on('response', r => { if (r.status() >= 400 && !r.url().endsWith('/favicon.ico')) errors.push(`${r.status()} ${r.url()}`); });
  await page.addInitScript(daemonPort => {
    window.__main = { perms: [], passthrough: [], done: 0, opened: [] };
    const answer = { mic: 'ok', screen: 'relaunch', auto: 'ok', notify: 'asked' };
    window.firstRun = {
      info: async () => ({ top: 32, notch: 185, cursor: [1180, 610], name: 'Allen', lang: 'zh', port: daemonPort }),
      permission: async (kind, ask, note) => { if (ask) window.__main.perms.push([kind, note]); return ask ? answer[kind] : ''; },
      open: page => window.__main.opened.push(page),
      passthrough: on => window.__main.passthrough.push(on),
      done: () => { window.__main.done++; },
    };
  }, daemonPort);
  await page.goto(`http://127.0.0.1:${port}/firstrun.html`);
  // Desktop stand-in behind the transparent page, and the hardware cutout on top like the real notch.
  await page.addStyleTag({ content: `html{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)}
    html::after{content:'';position:fixed;z-index:10;pointer-events:none;top:0;left:663.5px;width:185px;height:32px;background:#000;border-radius:0 0 10px 10px}` });
  const phase = () => page.evaluate(() => document.getElementById('screen').dataset.phase);
  const waitPhase = (p, timeout = 30000) => page.waitForFunction(p => document.getElementById('screen').dataset.phase === p, p, { timeout });
  const shot = name => page.screenshot({ path: path.join(dir, `${name}.png`) });
  const say = () => page.locator('.say').getAttribute('aria-label');
  const act = (a, extra = '') => page.locator(`[data-act="${a}"]${extra}`).first().click();
  const settle = () => page.waitForFunction(() => !document.getElementById('pin').classList.contains('leave'));

  // Act one runs by itself: the chart, the flight, the flash, her; then 开始.
  await waitPhase('intro');
  check('01 space opens where 打开 was clicked', await phase() === 'intro');
  await page.waitForTimeout(6500); await shot('01-chart');
  await page.waitForTimeout(3000); await shot('02-flight');
  await waitPhase('hello', 40000);
  await page.waitForTimeout(600); await shot('03-hello');
  check('03 the word is decoded and 开始 is offered in the system language', await page.locator('#word').textContent() === 'Jarvis' && await page.locator('#go').textContent() === '开始');

  // Act two: she moves into the notch and the panel grows down.
  await page.locator('#go').click();
  await waitPhase('setup', 8000);
  await page.waitForSelector('#f-user'); await page.waitForTimeout(900);
  check('04 page 1 is prefilled with the Mac account name', await page.locator('#f-user').inputValue() === 'Allen' && await page.locator('.src').isVisible());
  await shot('04-name');
  await page.locator('#f-user').fill('Allen');
  await act('next'); await page.waitForSelector('[data-act="asst"]'); await settle();
  await act('asst', '[data-v="Nova"]'); await act('next');
  await page.waitForSelector('[data-act="lang"]'); await settle();
  await act('lang', '[data-v="en"]'); await page.waitForTimeout(400); await settle();
  check('05 picking English switches her words at once', (await say()) === 'Which language should we use?');
  await act('next'); await page.waitForSelector('#f-key'); await settle();
  check('05 leaving the language page tells the daemon', calls.some(c => c.url === '/inherent/language' && c.body.language === 'en'));

  // Page 4: a wrong key is refused with the reason; a good one passes, a missing live model shows but does not block.
  await page.locator('#f-key').fill('sk-wrong'); await act('test');
  await page.waitForSelector('.reply.bad', { timeout: 5000 });
  check('06 a rejected key says why, and Continue stays off', (await page.locator('.reply.bad').innerText()).includes('401') && await page.locator('[data-act="next"]').isDisabled());
  await page.locator('#f-key').fill('sk-good'); await act('test');
  await page.waitForSelector('.reply:not(.bad)', { timeout: 5000 });
  await page.waitForTimeout(400); await shot('06-key');
  check('06 her first line and seconds come from the key test', (await page.locator('.reply p').innerText()).includes('this key works') && (await page.locator('.reply small').innerText()).includes('0.42'));
  check('06 the live-voice miss is named but Continue is on', (await page.locator('.ck .st.bad').count()) === 1 && !(await page.locator('[data-act="next"]').isDisabled()));
  check('06 the key went to the daemon, not anywhere else', calls.filter(c => c.url === '/inherent/setup/key').every(c => c.body.provider === 'openai'));
  await act('next'); await page.waitForSelector('#f-mm', { timeout: 5000 }); await settle();

  // Page 5: MiniMax key, the daemon's five voices, one spoken preview.
  await page.locator('#f-mm').fill('mm-key'); await act('mmtest');
  await page.waitForSelector('.voices:not(.locked)');
  check('07 the voices are the ones the daemon sent', (await page.locator('.voice b').allInnerTexts()).join() === VOICES.map(v => v.label).join());
  await act('voice', '[data-i="1"]'); await page.waitForTimeout(300);
  check('07 a voice preview asks the daemon for that voice in her words', calls.some(c => c.url === '/inherent/setup/voice-preview' && c.body.voice_id === VOICES[1].id && c.body.text.includes('Nova')));
  await shot('07-voice');
  await act('next'); await page.waitForSelector('.prow'); await settle();

  // Page 6: each Allow goes to the main process; the answers show.
  for (const k of ['mic', 'screen', 'auto', 'notify']) { await act('perm', `[data-k="${k}"]`); await page.waitForTimeout(150); }
  const pills = await page.locator('.pill.done').allInnerTexts();
  check('08 every permission was asked and answered', pills.join('|') === 'Allowed|After relaunch|Allowed|Asked' && (await page.evaluate(() => window.__main.perms.length)) === 4);
  check('08 the notification sample speaks in her name', (await page.evaluate(() => window.__main.perms[3][1]))[0] === 'Nova');
  await shot('08-permissions');
  await act('next'); await page.waitForSelector('.tile'); await settle(); await page.waitForTimeout(300);

  // Page 7: Notion signs in through the plugin routes; Microsoft is not offered yet; web search takes a key.
  check('09 Microsoft says it is not available yet', (await page.locator('.tile[data-k="ms"] em').innerText()) === 'Not available yet');
  await act('conn', '[data-k="notion"]');
  await page.waitForSelector('.tile.ok[data-k="notion"]', { timeout: 10000 });
  check('09 Notion connects through open, connect and the snapshot', calls.some(c => c.url === '/inherent/plugins/action' && c.body.operation === 'open' && c.body.data.plugin_id === 'notion')
    && calls.some(c => c.url === '/inherent/plugins/action' && c.body.operation === 'connect' && c.body.data.request_id === 'r1'));
  await act('conn', '[data-k="web"]'); await page.locator('#f-web').fill('tvly-key'); await act('webtest');
  await page.waitForSelector('.tile.ok[data-k="web"]', { timeout: 5000 });
  check('09 the Tavily key is tested as its own provider', calls.some(c => c.url === '/inherent/setup/key' && c.body.provider === 'tavily'));
  await shot('09-connections');
  await act('next'); await page.waitForSelector('.recap'); await settle(); await page.waitForTimeout(400);

  // Page 8 and 进入: the picks go to the daemon in the contract's order, then the finale and the handover.
  const recap = await page.locator('.recap').innerText();
  check('10 the recap shows what was set', ['Allen', 'Nova', 'English', 'Working', 'Steady', '4 / 4', 'Notion, Web search'].every(v => recap.includes(v)));
  await shot('10-recap');
  const before = calls.length;
  await act('enter');
  await waitPhase('finale', 5000);
  const order = calls.slice(before).map(c => c.url);
  check('11 进入 saves language, names, voice, then marks setup done', order.join() === '/inherent/language,/inherent/setup/name,/inherent/settings,/inherent/setup/done');
  const named = calls.find(c => c.url === '/inherent/setup/name').body, voice = calls.find(c => c.url === '/inherent/settings').body;
  check('11 the names and the picked voice id are sent', named.name === 'Allen' && named.assistant_name === 'Nova' && voice.changes.tts_voice === VOICES[1].id);
  await page.waitForTimeout(1100); await shot('11-reveal');
  check('11 clicks pass through once the desktop is back', (await page.evaluate(() => window.__main.passthrough)).join() === 'true');
  await page.waitForTimeout(900); await shot('12-hello-bubble');
  check('12 she greets from home', (await page.locator('#bubble').innerText()).includes('Allen') && await page.locator('#bubble').evaluate(e => e.classList.contains('on')));
  await page.waitForFunction(() => window.__main.done === 1, null, { timeout: 8000 });
  check('12 then the companion takes over', true);
  if (errors.length) console.log(errors.join('\n'));
  check('no page errors', errors.length === 0);
} finally {
  await browser.close(); server.kill(); daemon.close();
}
if (checks.length) console.log(checks.map(c => `ok  ${c}`).join('\n'));
console.log(`${checks.length} checks passed; screenshots in ${path.relative(process.cwd(), dir)}`);
