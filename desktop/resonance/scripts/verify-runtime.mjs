// Live acceptance for the daemon link: a real Electron window against a running `python -m jarvis serve`.
//   JARVIS_INHERENT_BRIDGE_PORT  daemon port (default 8016 — keep the real 8006 daemon out of tests)
//   RESONANCE_TEST_WAV           optional 16 kHz mono WAV utterance; exercises the voice phases via /inherent/asr-submit
//   RESONANCE_TEST_DAEMON_PID    optional; when set the daemon is SIGTERMed at the end to prove the reconnect panel
import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
const port = process.env.JARVIS_INHERENT_BRIDGE_PORT ?? '8016';
const http = `http://127.0.0.1:${port}`;
mkdirSync('evidence', { recursive: true });
const checks = [];
const check = (name, ok) => { assert.ok(ok, name); checks.push(name); console.log(`PASS ${name}`); };
assert.ok(await fetch(`${http}/api/health`).then(r => r.ok).catch(() => false), `no daemon on ${port}`);
const app = await electron.launch({ args: ['.'], cwd: process.cwd(), env: { ...process.env, JARVIS_INHERENT_BRIDGE_PORT: port } });
try {
  const page = await app.firstWindow();
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  const statuses = []; const presences = [];
  await page.exposeFunction('__status', s => statuses.push(s));
  await page.exposeFunction('__presence', s => presences.push(s));
  await page.waitForSelector('.voice-presence[data-state="standby"]', { timeout: 15000 });
  await page.evaluate(() => {
    const status = document.querySelector('.status-line [role=status]');
    new MutationObserver(() => window.__status(status.textContent)).observe(status, { childList: true, characterData: true, subtree: true });
    // The waveform is rebuilt when the mode switches, so watch the whole shell for its data-state.
    new MutationObserver(() => { const el = document.querySelector('.voice-presence'); if (el) window.__presence(el.getAttribute('data-state')); }).observe(document.querySelector('.shell'), { attributes: true, subtree: true, attributeFilter: ['data-state'] });
  });
  await page.waitForTimeout(500);
  check('connects and rests in standby without the error panel', await page.locator('.error-panel').count() === 0);
  check('live mode ships no demo notification or demo label', await page.locator('.notification .unread').count() === 0 && await page.locator('.demo-label').count() === 0);

  // Text turn through the real composer: renderer fetch → POST /inherent/submit → WS open/append/done.
  await page.getByRole('button', { name: '展开文字输入' }).click();
  await page.getByRole('textbox', { name: '文字输入' }).fill('请只回答两个字：收到');
  await page.getByRole('textbox', { name: '文字输入' }).press('Enter');
  await page.waitForSelector('.reply p', { timeout: 120000 });
  await page.waitForFunction(() => document.querySelector('.reply p')?.textContent.trim().length > 0, null, { timeout: 60000 });
  const reply = await page.locator('.reply p').innerText();
  console.log(`reply: ${reply.trim().slice(0, 80)}`);
  await page.screenshot({ path: 'evidence/runtime-text-reply.png', omitBackground: true });
  check('typed text streams a real reply into the capsule', reply.trim().length > 0);
  check('voice/document markup never reaches the reply text', !reply.includes('<'));
  check('status walked processing → speaking during the turn', statuses.includes('正在处理') && statuses.includes('正在播报'));
  await page.waitForSelector('.reply', { state: 'detached', timeout: 20000 });
  check('reply fades fadeMs after done and status returns to listening', statuses.at(-1) === '正在听取');

  // Voice turn: an uploaded utterance walks transcribing → accepted → reply while the waveform is visible.
  const wav = process.env.RESONANCE_TEST_WAV;
  if (wav) {
    await page.getByRole('button', { name: '收起文字，返回语音' }).click();
    await page.waitForSelector('.voice-presence');
    statuses.length = 0; presences.length = 0;
    const form = new FormData();
    form.append('audio', new Blob([readFileSync(wav)], { type: 'audio/wav' }), 'utterance.wav');
    form.append('language', 'zh-CN'); form.append('channel', 'inherent_ptt');
    const r = await fetch(`${http}/inherent/asr-submit`, { method: 'POST', body: form });
    console.log(`asr-submit: ${r.status} ${await r.text()}`);
    check('daemon accepts the utterance upload', r.ok);
    await page.waitForSelector('.reply p', { timeout: 120000 });
    await page.waitForFunction(() => document.querySelector('.reply p')?.textContent.trim().length > 0, null, { timeout: 60000 });
    console.log(`voice reply: ${(await page.locator('.reply p').innerText()).trim().slice(0, 80)}`);
    await page.screenshot({ path: 'evidence/runtime-voice-reply.png', omitBackground: true });
    check('voice phases drive the waveform through thinking and speaking', presences.includes('thinking') && presences.includes('speaking'));
    await page.waitForSelector('.reply', { state: 'detached', timeout: 20000 });
    check('waveform rests in standby after the spoken turn', await page.locator('.voice-presence').getAttribute('data-state') === 'standby');
  } else console.log('SKIP voice turn (set RESONANCE_TEST_WAV)');

  const pid = Number(process.env.RESONANCE_TEST_DAEMON_PID);
  if (pid) {
    process.kill(pid, 'SIGTERM');
    await page.waitForSelector('.error-panel', { timeout: 15000 });
    check('losing the daemon shows the reconnect panel', await page.getByRole('button', { name: '立即重连' }).count() === 1);
  } else console.log('SKIP daemon-loss check (set RESONANCE_TEST_DAEMON_PID)');
  check('renderer raised no page errors', errors.length === 0);
  writeFileSync('evidence/runtime-verification.json', JSON.stringify({ port, checks, statuses, presences, errors }, null, 2));
  console.log(`${checks.length} checks passed`);
} finally { await app.close(); }
