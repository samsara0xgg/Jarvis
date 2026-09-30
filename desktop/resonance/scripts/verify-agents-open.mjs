// Run after npm run build. Opening what an answer names, the changes, and landing (review 12, 13, 15, 17, 18), on the
// stage (scripts/agents-stage.mjs): a real host on stand-in agents, the built page in Chromium. A picture, a recording,
// a sound, a PDF, markdown, code at its line, a log, a CSV and JSON open in the preview sheet; Keynote, a disk image and
// what lies outside the session's folders go to Quick Look; one thing at a time with ‹ back; the changes on the stage;
// landing with only the steps its repository has, the way asked once, done in one line. The host gives a file's bytes
// only from the session's own folders, and that is checked here from outside the page too. The PDF viewer is not in
// headless Chromium: the same page is opened in Electron under xvfb for it (skipped where there is no xvfb-run).
// SHOTS=<folder> keeps a screenshot of each point. A fake gh answers the pull request.
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { existsSync, symlinkSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { stage } from './agents-stage.mjs';

const here = path.dirname(fileURLToPath(import.meta.url)), app = path.join(here, '..');
const st = await stage();
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const G = { ...process.env, GIT_AUTHOR_NAME: 't', GIT_AUTHOR_EMAIL: 't@t', GIT_COMMITTER_NAME: 't', GIT_COMMITTER_EMAIL: 't@t' };
const git = (cwd, ...a) => execFileSync('git', ['-C', cwd, ...a], { encoding: 'utf8', env: G }).trim();
const put = async (file, body) => { await mkdir(path.dirname(file), { recursive: true }); await writeFile(file, body); };

// ---------- what the answers name: made here, nothing downloaded ----------
// A small PDF of text pages, with a correct cross-reference table.
function pdf(pages) {
  const objs = [], n = pages.length, font = 3 + 2 * n;
  objs[1] = '<< /Type /Catalog /Pages 2 0 R >>';
  objs[2] = `<< /Type /Pages /Kids [${pages.map((_, i) => `${3 + 2 * i} 0 R`).join(' ')}] /Count ${n} >>`;
  pages.forEach((lines, i) => {
    const text = lines.map((l, k) => `BT /F1 ${k === 0 ? 22 : 13} Tf 64 ${760 - k * 28} Td (${l.replace(/[()\\]/g, m => `\\${m}`)}) Tj ET`).join('\n');
    objs[3 + 2 * i] = `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents ${4 + 2 * i} 0 R /Resources << /Font << /F1 ${font} 0 R >> >> >>`;
    objs[4 + 2 * i] = `<< /Length ${Buffer.byteLength(text)} >>\nstream\n${text}\nendstream`;
  });
  objs[font] = '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>';
  let out = '%PDF-1.4\n';
  const offs = [];
  for (let i = 1; i < objs.length; i++) { offs[i] = Buffer.byteLength(out); out += `${i} 0 obj\n${objs[i]}\nendobj\n`; }
  const x = Buffer.byteLength(out);
  out += `xref\n0 ${objs.length}\n0000000000 65535 f \n${offs.slice(1).map(o => `${String(o).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size ${objs.length} /Root 1 0 R >>\nstartxref\n${x}\n%%EOF\n`;
  return Buffer.from(out, 'latin1');
}
// Five seconds of the window's little sounds: a tick, a send, an open, a done, a call, a drop.
function wav(sec = 5, rate = 22050) {
  const n = sec * rate, b = Buffer.alloc(44 + n * 2);
  b.write('RIFF', 0); b.writeUInt32LE(36 + n * 2, 4); b.write('WAVE', 8); b.write('fmt ', 12); b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22);
  b.writeUInt32LE(rate, 24); b.writeUInt32LE(rate * 2, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write('data', 36); b.writeUInt32LE(n * 2, 40);
  const blips = [[0.12, 1400, .9, .06], [0.9, 660, .8, .22], [1.55, 880, .95, .45], [2.55, 523, .7, .6], [3.35, 740, .8, .5], [4.2, 392, .6, .5]];
  for (let i = 0; i < n; i++) {
    const t = i / rate;
    let v = 0;
    for (const [at, f, a, len] of blips) if (t >= at && t < at + len) v += a * Math.exp(-(t - at) * 6 / len) * Math.sin(2 * Math.PI * f * (t - at));
    b.writeInt16LE(Math.max(-32767, Math.min(32767, Math.round(v * .7 * 32767))), 44 + i * 2);
  }
  return b;
}
// A picture and a recording, drawn by Chromium itself: the landing panel as a design, and three seconds of it moving.
const MOCK = `<body style="margin:0;width:1280px;height:800px;display:grid;place-items:center;background:radial-gradient(900px 500px at 70% 20%,#1d2150,#0b0c1d);font:14px system-ui,sans-serif;color:#e2e6f7">
<div style="width:460px;padding:26px 28px;border-radius:18px;background:#14163a;box-shadow:0 30px 80px -30px #000,inset 0 0 0 1px #2c3170">
<div style="font-weight:700;font-size:18px">落地</div><div style="margin:4px 0 18px;font:12px monospace;color:#8e95c4">worktree-remove-tray → main</div>
${['改动 · 12 个文件', '门禁 · tsc · build · verify', '提交 · feat(desktop): drop the tray', '合进 main', '重启 companion', '推送 · 等你点头', '清理 worktree'].map((t, i) =>
  `<div style="display:flex;gap:12px;align-items:center;padding:7px 10px;border-radius:9px;${i === 5 ? 'background:#23265a' : ''}"><span style="width:16px;height:16px;border-radius:50%;background:${i < 5 ? '#6fe0b4' : 'transparent'};box-shadow:inset 0 0 0 1.5px ${i < 5 ? '#6fe0b4' : '#6b72a8'}"></span><span style="color:${i < 6 ? '#e2e6f7' : '#8e95c4'}">${t}</span></div>`).join('')}
<div style="display:flex;gap:10px;margin-top:18px"><span style="padding:7px 16px;border-radius:9px;background:#ffc98f;color:#2b1705;font-weight:700">允许</span><span style="padding:7px 16px;border-radius:9px;background:#2a2d5c">拒绝</span></div></div></body>`;
async function drawn() {
  const g = await st.context.newPage();
  await g.setViewportSize({ width: 1280, height: 800 });
  await g.setContent(MOCK);
  const png = await g.screenshot({ type: 'png', scale: 'css' });
  const b64 = await g.evaluate(async () => {
    const c = document.createElement('canvas'); c.width = 640; c.height = 360; document.body.append(c);
    const x = c.getContext('2d'), rec = new MediaRecorder(c.captureStream(30), { mimeType: 'video/webm;codecs=vp8' }), parts = [];
    rec.ondataavailable = e => parts.push(e.data);
    const t0 = performance.now(), frame = () => {
      const t = (performance.now() - t0) / 1000;
      x.fillStyle = '#0b0c1d'; x.fillRect(0, 0, 640, 360);
      x.fillStyle = '#14163a'; x.fillRect(360, 40, 240, 280);
      x.fillStyle = '#e2e6f7'; x.font = 'bold 16px sans-serif'; x.fillText('落地', 380, 70);
      for (let i = 0; i < 7; i++) { const on = t > i * .4; x.fillStyle = on ? '#6fe0b4' : '#3a3f78'; x.beginPath(); x.arc(390, 100 + i * 28, 6, 0, 7); x.fill(); x.fillStyle = on ? '#e2e6f7' : '#6b72a8'; x.font = '13px sans-serif'; x.fillText(['改动', '门禁', '提交', '合进 main', '重启', '推送', '清理 worktree'][i], 406, 105 + i * 28); }
      x.fillStyle = '#2a2d5c'; for (let i = 0; i < 6; i++) x.fillRect(40, 60 + i * 40, 150 + (i * 37) % 120, 10);
      x.fillStyle = '#ffffff'; x.beginPath(); x.arc(120 + t * 70, 200 - Math.sin(t * 3) * 40, 5, 0, 7); x.fill();
    };
    const tick = setInterval(frame, 33); frame(); rec.start(250);
    await new Promise(r => setTimeout(r, 3200));
    rec.stop(); await new Promise(r => { rec.onstop = r; }); clearInterval(tick);
    const u = new Uint8Array(await new Blob(parts, { type: 'video/webm' }).arrayBuffer());
    let s = ''; for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode(...u.subarray(i, i + 0x8000));
    return btoa(s);
  });
  await g.close();
  return { png, webm: Buffer.from(b64, 'base64') };
}

const MD = `# Startrail 0.9 演示稿

演示按这个顺序走，每一段都有要打开的东西。*先开 Startrail*，登好 Claude。

## 顺序

1. 设计稿：[落地面板](../design/落地面板.png)
2. 录屏：\`docs/演示.webm\`
3. 提示音：\`design/提示音.wav\`

## 检查项

- [x] 截图都换成 0.9 的
- [x] 录屏重录一遍
- [ ] 落地面板的截图

| 段落 | 时长 | 谁讲 |
|---|---|---|
| 开场 | 1 分钟 | Allen |
| 落地 | 3 分钟 | yilun |

> 演示机提前打开 Startrail，~~别用投屏~~ 用线连。

\`\`\`ts
export const demo = { minutes: 4, steps: ['open', 'land'] };
\`\`\`
`;
const LOG = [
  '23:45:45.665 INFO  keeper claude 3a1e · heartbeat', '23:46:08.816 INFO  host   GET /usage 200 · 40ms', '23:46:31.967 INFO  keeper codex 77b0 · heartbeat',
  '23:47:17.269 INFO  keeper claude 3a1e · turn 7 started', '23:48:17.442 INFO  claude 3a1e · Read electron/agents/files.ts', '23:50:44.913 WARN  keeper claude 3a1e · no output for 90s',
  '23:52:40.118 ERROR keeper claude 3a1e exited 1: read ECONNRESET', '    at TLSSocket.onStreamRead (node:internal/stream_base_commons:216:20)', '    at Session.pump (electron/agents/keeper.ts:188:11)',
  '23:52:40.402 INFO  keeper claude 3a1e · restarting (1/3) in 2s', '23:52:42.455 INFO  keeper claude 3a1e · resumed at turn 7', '23:53:21.771 INFO  claude 3a1e · verify:agents · 118 passed',
  '00:04:40.227 WARN  codex  77b0 · approval asked: git push origin main', '00:05:12.884 INFO  codex  77b0 · approved by you', '00:12:09.020 INFO  host   GET /peek?ref=logs/host.log 200 · 58ms',
].join('\n') + '\n';
const CSV = 'date,plan,five_hour_pct,seven_day_pct,sessions\n2026-09-25,5X,34,51,8\n2026-09-26,5X,61,58,12\n2026-09-27,5X,12,60,3\n2026-09-28,5X,88,71,15\n2026-09-29,5X,47,76,9\n';
const JSONF = JSON.stringify({ default: 'opus', models: [{ id: 'opus', effort: 'xhigh', context: 1000000 }, { id: 'sonnet', effort: 'high', context: 200000 }], fallback: null, thinking: true }, null, 2) + '\n';

try {
  await writeFile(path.join(st.HOME, '.gitconfig'), '[user]\n\tname = Stage\n\temail = stage@example.com\n');
  const R = st.repo, movies = path.join(st.HOME, 'Movies');
  const { png, webm } = await drawn();
  await put(path.join(R, 'design', '落地面板.png'), png);
  await put(path.join(R, 'docs', '演示.webm'), webm);
  await put(path.join(R, 'design', '提示音.wav'), wav());
  await put(path.join(R, 'docs', '发布清单.pdf'), pdf([['Startrail 0.9 release checklist', 'Build: npm run build', 'Checks: verify-agents-*', 'Sign the dmg'],
    ['Demo', '1. The design', '2. The recording', '3. The sounds'], ['Landing', 'diff, gates, commit, merge, restart, push, clean'], ['Afterwards', 'Write the notes']]));
  await put(path.join(R, 'docs', '演示稿.md'), MD);
  await put(path.join(R, 'electron', 'agents', 'files.ts'), await readFile(path.join(app, 'electron', 'agents', 'files.ts')));
  await put(path.join(R, 'logs', 'host.log'), LOG);
  await put(path.join(R, 'data', 'usage-0929.csv'), CSV);
  await put(path.join(R, 'config', 'models.json'), JSONF);
  await put(path.join(R, 'design', 'Startrail 发布.key'), Buffer.concat([Buffer.from('PK\x03\x04'), Buffer.alloc(2048)]));
  await put(path.join(R, 'dist', 'Startrail-0.9.dmg'), Buffer.alloc(4096, 7));
  await put(path.join(R, 'design', 'icon.svg'), '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><script>alert(1)</script><circle cx="5" cy="5" r="4" fill="#9db4ff"/></svg>');
  await put(path.join(movies, '落地演示.webm'), webm);
  await put(path.join(movies, '私人.png'), png);
  symlinkSync(path.join(movies, '私人.png'), path.join(R, 'design', '外链.png'));

  // ---------- a session whose answers name them (the stand-in says back what it was told) ----------
  const A = await st.session('`docs/演示稿.md` `design/落地面板.png` `docs/演示.webm`');
  await st.send(A, '`design/提示音.wav` `docs/发布清单.pdf` `electron/agents/files.ts:94`');
  await st.send(A, '`logs/host.log` `data/usage-0929.csv` `config/models.json`');
  await st.send(A, '`design/Startrail 发布.key` `dist/Startrail-0.9.dmg` `~/Movies/落地演示.webm`');

  // ---------- the host: bytes only from the session's own folders ----------
  const file = (id, ref, h = {}) => fetch(`${st.API}/sessions/${id}/file/x?ref=${encodeURIComponent(ref)}`, { headers: { Authorization: `Bearer ${st.key}`, ...h } });
  let r = await file(A, path.join(R, 'design', '落地面板.png'));
  check('the host gives a picture of the session\'s folder with its type, length and ranges', r.status === 200 && r.headers.get('content-type') === 'image/png'
    && Number(r.headers.get('content-length')) === png.length && r.headers.get('accept-ranges') === 'bytes' && r.headers.get('x-content-type-options') === 'nosniff' && Buffer.from(await r.arrayBuffer()).equals(png));
  r = await file(A, 'docs/演示.webm', { Range: 'bytes=0-99' });
  const head100 = Buffer.from(await r.arrayBuffer());
  check('a range of a recording comes as 206 with where it sits', r.status === 206 && r.headers.get('content-range') === `bytes 0-99/${webm.length}` && head100.equals(webm.subarray(0, 100)) && r.headers.get('content-type') === 'video/webm');
  r = await file(A, 'docs/演示.webm', { Range: 'bytes=-50' });
  check('a range from the end comes too', r.status === 206 && Buffer.from(await r.arrayBuffer()).equals(webm.subarray(webm.length - 50)));
  r = await file(A, 'docs/演示.webm', { Range: `bytes=${webm.length}-` });
  check('a range past the end is 416 with the length', r.status === 416 && r.headers.get('content-range') === `bytes */${webm.length}`);
  r = await file(A, path.join(movies, '落地演示.webm'));
  check('a file outside the session\'s folders is refused (403)', r.status === 403 && (await r.json()).error.includes('不在这个会话的文件夹里'));
  r = await file(A, '../../Movies/落地演示.webm');
  const r2 = await file(A, `${R}/docs/../../../Movies/落地演示.webm`);
  check('climbing out with .. is refused too', r.status === 403 && r2.status === 403);
  r = await file(A, 'design/外链.png');
  check('a link inside that points outside is refused', r.status === 403);
  r = await file(A, 'design/nope.png');
  check('a file that is not there is 404 with its name', r.status === 404 && (await r.json()).error.includes('design/nope.png'));
  r = await file(A, 'notes.txt'); const r3 = await file(A, 'design');
  check('text and folders are not served as bytes (415)', r.status === 415 && r3.status === 415);
  r = await file(A, 'design/icon.svg');
  check('an SVG comes sandboxed, so what it holds never runs', r.status === 200 && r.headers.get('content-type') === 'image/svg+xml' && /sandbox/.test(r.headers.get('content-security-policy') ?? ''));
  r = await fetch(`${st.API}/sessions/${A}/file/x?ref=${encodeURIComponent('design/落地面板.png')}`);
  check('without the host key nothing is served', r.status === 401);
  const B = await st.session('看录屏', { dirs: [movies] });
  r = await file(B, path.join(movies, '落地演示.webm'));
  check('a folder the session was given besides its own is one of its folders', r.status === 200 && r.headers.get('content-type') === 'video/webm');
  const pk = ref => st.call(`/sessions/${A}/peek?ref=${encodeURIComponent(ref)}`);
  const [kp, kq, kf, kl] = await Promise.all([pk('docs/发布清单.pdf'), pk('~/Movies/落地演示.webm'), pk('electron/agents/files.ts:94'), pk('design/Startrail 发布.key')]);
  check('peek says which files the sheet can show itself, and a PDF\'s pages', kp.bytes === true && kp.pages === 4 && kq.kind === 'media' && !kq.bytes && kf.line === 94 && kf.size > 0 && kl.kind === 'quicklook', { kp, kq: kq.bytes, kl: kl.kind });

  // ---------- the preview sheet ----------
  await st.open(A); await sleep(800);
  const p = st.page;
  const ref = r0 => p.locator(`.chat code.ref[data-ref="${r0}"]`).last();
  const sheet = () => p.evaluate(() => {
    const pv = document.querySelector('.pv'), c = document.querySelector('.chat').getBoundingClientRect(), v = pv.getBoundingClientRect();
    return { open: !pv.classList.contains('off'), stage: v.width > c.width, b: pv.querySelector('.sh b').textContent, small: pv.querySelector('.sh small').textContent, back: !pv.querySelector('.pvb').hidden };
  });
  const shut = async () => { await p.locator('.pv [data-act="pvclose"]').click(); await p.waitForFunction(() => document.querySelector('.pv').classList.contains('off')); await sleep(500); };
  const calls = k => p.evaluate(k0 => window.__agentsCalls.filter(c => c[0] === k0).map(c => c.slice(1)), k);

  // op-img
  await ref('design/落地面板.png').click();
  await p.waitForFunction(() => document.querySelector('.pv-view .op-img img')?.naturalWidth > 0 && /1280 × 800/.test(document.querySelector('.pv .sh small').textContent));
  await sleep(600);
  let s = await sheet(), img = await p.evaluate(() => { const i = document.querySelector('.op-img img'); return { w: i.getBoundingClientRect().width, n: i.naturalWidth }; });
  check('op-img: a picture it names opens on the stage, fitted, with its folder, size and bytes', s.open && s.stage && s.b === '落地面板.png' && s.small.startsWith('design · 1280 × 800 · ') && img.w < img.n, { s, img });
  await st.shot('op-img');
  await p.locator('.op-img img').click({ position: { x: 200, y: 120 } });
  await sleep(200);
  img = await p.evaluate(() => { const i = document.querySelector('.op-img img'); return { w: i.getBoundingClientRect().width, n: i.naturalWidth, z: i.parentElement.classList.contains('z'), left: document.querySelector('.pv-view').scrollLeft }; });
  check('op-img: a click shows it 1:1, near where it was clicked; another fits it again', img.z && Math.abs(img.w - img.n) < 1 && img.left > 0, img);
  await st.shot('op-img-1to1');
  await p.locator('.op-img img').click(); await sleep(150);
  check('op-img: fitted again', await p.evaluate(() => !document.querySelector('.op-img').classList.contains('z')));
  await shut();

  // op-video
  await ref('docs/演示.webm').click();
  await p.waitForFunction(() => { const v = document.querySelector('.op-vid video'); return v && v.readyState >= 1 && Number.isFinite(v.duration) && v.duration > 2; }, null, { timeout: 15000 });
  await p.waitForFunction(() => /^docs · 0:03 · 640 × 360 · /.test(document.querySelector('.pv .sh small').textContent));
  s = await sheet();
  check('op-video: a recording opens beside the conversation, stopped, with its length and size', s.open && !s.stage && s.b === '演示.webm' && await p.evaluate(() => document.querySelector('.op-vid video').paused), s);
  await p.locator('.op-vid .op-pp').click();
  await p.waitForFunction(() => { const v = document.querySelector('.op-vid video'); return !v.paused && v.currentTime > 1.2; }, null, { timeout: 10000 });
  check('op-video: play plays it, and the controls say so', await p.evaluate(() => document.querySelector('.op-vid').classList.contains('on') && document.querySelector('.op-pp').getAttribute('aria-label') === '暂停' && document.querySelector('.op-ctl .op-tm').textContent !== '0:00'));
  await p.locator('.op-vid .op-pp').click();
  await sleep(200);
  await st.shot('op-video');
  const box = await p.locator('.op-vid .op-scrub').boundingBox();
  await p.mouse.click(box.x + box.width * .5, box.y + box.height / 2);
  await sleep(300);
  const vt = await p.evaluate(() => { const v = document.querySelector('.op-vid video'); return { t: v.currentTime, d: v.duration, paused: v.paused, knob: document.querySelector('.op-scrub b').style.left }; });
  check('op-video: pause stops it, and the scrubber seeks where it is pressed', vt.paused && Math.abs(vt.t - vt.d / 2) < .35 && parseFloat(vt.knob) > 40 && parseFloat(vt.knob) < 60, vt);
  await shut();

  // op-audio
  await ref('design/提示音.wav').click();
  await p.waitForFunction(() => { const a = document.querySelector('.op-aud audio'); return a && a.readyState >= 1 && /0:05 · WAV/.test(document.querySelector('.pv .sh small').textContent); }, null, { timeout: 10000 });
  await sleep(1200);
  const wv = await p.evaluate(() => {
    const c = document.querySelector('.op-wave canvas'), d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data, a = document.querySelector('.op-aud audio');
    let lit = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) lit++;
    return { lit: lit / (c.width * c.height), paused: a.paused, t: a.currentTime };
  });
  check('op-audio: a sound is its waveform, drawn from its samples, and does not play by itself', wv.lit > .01 && wv.paused && wv.t === 0, wv);
  await p.locator('.op-aud .op-pp').click();
  await p.waitForFunction(() => { const a = document.querySelector('.op-aud audio'); return !a.paused && a.currentTime > 1.2; }, null, { timeout: 10000 });
  await p.locator('.op-aud .op-pp').click();
  await sleep(200);
  check('op-audio: it plays when asked and pauses', await p.evaluate(() => document.querySelector('.op-aud audio').paused && document.querySelector('.op-aud audio').currentTime > 1));
  await st.shot('op-audio');
  await shut();

  // op-pdf (its pages are drawn by the window's own viewer: the Electron part below)
  await ref('docs/发布清单.pdf').click();
  await p.waitForFunction(() => !!document.querySelector('.pv-view iframe.op-pdf') && /4 页/.test(document.querySelector('.pv .sh small').textContent));
  await sleep(600);
  const pdfSrc = await p.evaluate(() => document.querySelector('iframe.op-pdf').src);
  const pdfGot = await p.evaluate(async u => { const x = await fetch(u.split('#')[0]); return { ok: x.ok, type: x.headers.get('content-type'), n: (await x.arrayBuffer()).byteLength }; }, pdfSrc);
  s = await sheet();
  check('op-pdf: a PDF opens on the stage in the window\'s viewer, from the host, with its page count', s.stage && s.b === '发布清单.pdf' && /^docs · 4 页 · /.test(s.small)
    && /\/file\/%E5%8F%91%E5%B8%83%E6%B8%85%E5%8D%95\.pdf\?ref=.+#navpanes=0&view=FitH$/.test(pdfSrc) && pdfGot.ok && pdfGot.type === 'application/pdf', { s, pdfSrc, pdfGot });
  await shut();

  // op-md
  await ref('docs/演示稿.md').click();
  await p.waitForSelector('.pv-view .op-md h1');
  await sleep(500);
  const md = await p.evaluate(() => { const d = document.querySelector('.op-md'); return { h1: d.querySelector('h1').textContent, table: d.querySelectorAll('table tr').length, done: d.querySelectorAll('li.task i.y').length, todo: d.querySelectorAll('li.task i:not(.y)').length,
    code: d.querySelectorAll('pre .hljs-keyword').length, quote: !!d.querySelector('blockquote del'), link: d.querySelector('a.ref')?.dataset.ref, pressed: document.querySelector('.op-seg [aria-pressed="true"]').textContent }; });
  check('op-md: markdown is rendered: headings, a table, its boxes, quotes, code in colour', md.h1 === 'Startrail 0.9 演示稿' && md.table === 3 && md.done === 2 && md.todo === 1 && md.code > 0 && md.quote && md.pressed === '渲染', md);
  check('op-md: a link in it points where the document is, not where the session is', md.link === path.join(R, 'design', '落地面板.png'), md.link);
  await st.shot('op-md');
  await p.locator('.op-seg button[data-v="s"]').click();
  await p.waitForSelector('.pv-view .op-code .op-ln');
  const src = await p.evaluate(() => ({ lines: document.querySelectorAll('.op-code .op-ln').length, head: document.querySelectorAll('.op-code .hljs-section').length, first: document.querySelector('.op-code .op-ln b').textContent }));
  check('op-md: a click switches to its source, numbered and coloured', src.lines === MD.split('\n').length - 1 && src.head >= 3 && src.first === '1', src);
  await st.shot('op-md-source');
  await p.locator('.op-seg button[data-v="r"]').click();
  await p.waitForSelector('.pv-view .op-md h1');

  // op-back: the document's picture takes its place; ‹ brings the document back
  await p.locator('.op-md a.ref').click();
  await p.waitForFunction(() => document.querySelector('.pv .sh b').textContent === '落地面板.png' && document.querySelector('.op-img img')?.naturalWidth > 0);
  await sleep(400);
  s = await sheet();
  check('op-back: opened from inside the sheet, a file takes the document\'s place, with ‹ back', s.open && !s.stage && s.back && await p.locator('.pv').count() === 1, s);
  await st.shot('op-back');
  await p.locator('.pv .pvb').click();
  await p.waitForSelector('.pv-view .op-md h1');
  s = await sheet();
  check('op-back: ‹ brings back the document, rendered as it was, and there is nothing further back', s.b === '演示稿.md' && !s.back && s.small.includes('Markdown'), s);
  await shut();

  // op-code
  await ref('electron/agents/files.ts:94').click();
  await p.waitForSelector('.pv-view .op-ln.at');
  await sleep(300);
  const code = await p.evaluate(() => {
    const v = document.querySelector('.pv-view'), at = v.querySelector('.op-ln.at'), top = at.offsetTop - v.scrollTop;
    return { n: at.querySelector('b').textContent, inView: top > 0 && top < v.clientHeight, kw: v.querySelectorAll('.hljs-keyword').length, str: v.querySelectorAll('.hljs-string').length, small: document.querySelector('.pv .sh small').textContent, b: document.querySelector('.pv .sh b').textContent, badge: document.querySelector('.pv .sh .ic').textContent };
  });
  check('op-code: code opens with line numbers and highlight.js\'s colours, stopped at the line it named', code.n === '94' && code.inView && code.kw > 20 && code.str > 5 && code.b === 'files.ts' && code.badge === 'TS'
    && /^electron\/agents · \d+ 行 · 第 94 行$/.test(code.small), code);
  await st.shot('op-code');
  await shut();

  // op-text: a log, a CSV and JSON, as highlighted text
  await ref('logs/host.log').click();
  await p.waitForSelector('.pv-view .op-log');
  const lg = await p.evaluate(() => ({ lines: document.querySelectorAll('.op-lg').length, warn: document.querySelectorAll('.op-lg.warn').length, err: document.querySelectorAll('.op-lg.error').length, t: document.querySelectorAll('.op-lg .t').length }));
  check('op-text: a log keeps its times and levels apart, the lines of an error with it', lg.lines === 15 && lg.warn === 2 && lg.err === 3 && lg.t === 13, lg);
  await sleep(500);
  await st.shot('op-text');
  await ref('data/usage-0929.csv').click();
  await p.waitForSelector('.pv-view .op-csv');
  const csv = await p.evaluate(() => ({ head: document.querySelectorAll('.op-csv .op-ch').length, nums: document.querySelectorAll('.op-csv .hljs-number').length, back: !document.querySelector('.pv .pvb').hidden }));
  check('op-text: a CSV has its header and its numbers set apart', csv.head === 5 && csv.nums === 15 && csv.back, csv);
  await st.shot('op-text-csv');
  await ref('config/models.json').click();
  await p.waitForSelector('.pv-view .op-code .hljs-attr');
  check('op-text: JSON is highlighted text', await p.evaluate(() => document.querySelectorAll('.op-code .hljs-attr').length >= 8 && document.querySelectorAll('.op-code .hljs-number').length >= 2));
  await shut();

  // op-ql: Keynote, a disk image and what is outside the session's folders go to Quick Look
  for (const [k, r0] of ['design/Startrail 发布.key', 'dist/Startrail-0.9.dmg', '~/Movies/落地演示.webm'].entries()) {
    await ref(r0).click();
    await p.waitForFunction(n => window.__agentsCalls.filter(c => c[0] === 'quickLook').length > n, k);
  }
  await sleep(300);
  const ql = await calls('quickLook'), toast = await p.locator('.toast').textContent();
  check('op-ql: Keynote and a disk image go to Quick Look, the sheet stays shut', ql.some(c => c[0] === path.join(R, 'design', 'Startrail 发布.key')) && ql.some(c => c[0] === path.join(R, 'dist', 'Startrail-0.9.dmg')) && !(await sheet()).open, ql);
  check('op-ql: a recording outside the session\'s folders goes to Quick Look too, and says why', ql.some(c => c[0] === path.join(movies, '落地演示.webm')) && toast.includes('不在这个会话的文件夹里'), { ql, toast });
  await st.shot('op-ql');

  // ---------- 改动 and 落地 ----------
  const origin = async name => { const o = path.join(st.HOME, 'origins', `${name}.git`); await mkdir(path.dirname(o), { recursive: true }); execFileSync('git', ['init', '-q', '--bare', '-b', 'main', o]); return o; };
  const repo = async (name, files, withOrigin = true) => {
    const at = path.join(st.HOME, 'Projects', name);
    for (const [f, b] of Object.entries({ '.gitignore': '.claude/\n', ...files })) await put(path.join(at, f), b);
    execFileSync('git', ['init', '-q', '-b', 'main', at]); git(at, 'add', '-A'); git(at, 'commit', '-q', '-m', 'init');
    if (withOrigin) { git(at, 'remote', 'add', 'origin', await origin(name)); git(at, '-c', 'push.negotiate=false', 'push', '-q', '-u', 'origin', 'main'); }
    return at;
  };
  const narrative = Array.from({ length: 40 }, (_, i) => i === 4 ? 'def evening(day):' : i === 5 ? '    return day.summary(sentences=6)' : `# line ${i + 1} of the narrative`).join('\n') + '\n';
  const R1 = await repo('timesink', { 'timesink/narrative.py': narrative, 'timesink/legacy.py': 'OLD = True\n', 'README.md': '# timesink\n' });
  const R2 = await repo('companion', { 'electron/companion.ts': 'export const tray = true;\nexport const her = 1;\n', 'src/trayIcon.ts': 'export const icon = 1;\n', 'README.md': '# companion\n' });
  const R3 = await repo('notes', { 'README.md': '# notes\n', 'scripts/check-changelog.sh': 'echo "CHANGELOG.md 里还没有 0.9 这一节" >&2\nexit 1\n' });
  const J = await repo('jarvis', { 'jarvis/__init__.py': '', 'desktop/resonance/package.json': '{}\n', 'docs/spec.html': '<p>spec</p>\n' }, false);
  // The owner's gates and restart for two of them; timesink and Jarvis have none of their own.
  const restartCmd = 'echo restarted >> .restarted';
  await st.call('/settings', { land: { [R2]: { gates: ['test -f README.md', 'git diff --check'], restart: restartCmd, via: 'merge' }, [R3]: { gates: ['test -f README.md', 'sh scripts/check-changelog.sh'], via: 'merge' } } });
  // The stand-in gh: notes what it was asked and opens pull request 128.
  const ghCalls = path.join(st.tmp, 'gh-calls.jsonl');
  await put(path.join(st.HOME, '.local', 'bin', 'gh'), `#!/usr/bin/env node\nrequire('fs').appendFileSync(${JSON.stringify(ghCalls)}, JSON.stringify(process.argv.slice(2)) + '\\n');\nconsole.log('https://github.com/example/timesink/pull/128');\n`);
  execFileSync('chmod', ['+x', path.join(st.HOME, '.local', 'bin', 'gh')]);

  // timesink: its first landing, by pull request
  const T = await st.session('每天的叙事改成三段：上午、下午、晚上', { cwd: R1, tree: true });
  const t1 = st.row(T).cwd;
  await writeFile(path.join(t1, 'timesink', 'narrative.py'), narrative.replace('sentences=6', 'sentences=3').replace('# line 30 of the narrative', '# line 30 of the narrative\nMORNING, AFTERNOON, EVENING = range(3)'));
  await put(path.join(t1, 'tests', 'test_narrative.py'), 'def test_three():\n    assert True\n');
  await rm(path.join(t1, 'timesink', 'legacy.py'));
  await put(path.join(t1, 'assets', 'tray.png'), png.subarray(0, 2000));
  await st.send(T, '晚上那段太长了，压到两三句');
  const d1 = await st.until('timesink measured', () => st.row(T)?.dirty?.n === 4 && st.row(T).dirty);
  check('op-route: a repository that could go either way asks on its first landing; it has no gates or restart of its own', d1.ask === true && d1.ways.join() === 'pr,merge' && !d1.gates && !d1.restart && !d1.local, d1);
  await st.open(T);
  const ask = await p.evaluate(() => { const a = document.querySelector('.conv .ask'); return { hidden: a.hidden, q: a.querySelector('.ask-q')?.textContent, b: [...a.querySelectorAll('button')].map(b => b.textContent), note: a.querySelector(':scope > span:last-child')?.textContent }; });
  check('op-route: the question sits under the last answer, with the two ways', !ask.hidden && ask.q === '要落地吗？timesink 第一次落地，走哪条？' && ask.b.join('|') === '合进 main|推分支开 PR' && ask.note === '选一次，这个仓库以后都这样', ask);
  await st.shot('op-route');

  // op-diff: from the title's menu, on the stage
  await p.locator('.m-head .h-t').hover(); await sleep(150);
  await p.locator('[data-act="menu"][data-v="more"]').click();
  const line = await p.locator('.pop button[data-act="changes"]').textContent();
  check('op-diff: the title\'s menu has 改动 with its lines', line === `改动+${d1.add} −${d1.del}`, line);
  await p.locator('.pop button[data-act="changes"]').click();
  await p.waitForSelector('.pv-view .op-dv .op-dh');
  await sleep(500);
  const base7 = git(R1, 'rev-parse', '--short=7', 'main');
  const dv = await p.evaluate(() => ({ files: [...document.querySelectorAll('.op-df')].map(b => `${b.querySelector('.op-st').textContent} ${b.querySelector('b').textContent} ${b.querySelector('.op-pm').textContent}`),
    on: document.querySelector('.op-df.on b').textContent, small: document.querySelector('.pv .sh small').textContent, b: document.querySelector('.pv .sh b').textContent, sum: document.querySelector('.op-dsum').textContent }));
  check('op-diff: every changed file with how it changed and its lines, against main where the branch left it', dv.b === '改动' && dv.small === `对比 main（开分支时的 ${base7}）`
    && dv.files.join('|') === 'A tray.png 二进制|A test_narrative.py +2 −0|D legacy.py +0 −1|M narrative.py +2 −1' && !(await p.locator('.pop.on').count()) && (await sheet()).stage, dv);
  await p.locator('.op-df[data-path="timesink/narrative.py"]').click();
  await p.waitForFunction(() => document.querySelector('.op-dh .op-dp')?.textContent === 'timesink/narrative.py' && document.querySelectorAll('.op-dd .op-ln').length > 3);
  const hk = await p.evaluate(() => ({ rows: [...document.querySelectorAll('.op-dd .op-ln')].map(l => `${l.querySelector('b').textContent}${l.querySelector('u').textContent}`), hunks: document.querySelectorAll('.op-dd .op-hunk').length }));
  check('op-diff: a file\'s hunks, numbered as the file is now, what went and what came marked', hk.rows.includes('6+') && hk.rows.includes('−') && hk.rows.includes('31+') && hk.rows.includes('3') && hk.hunks >= 2, hk);
  await st.shot('op-diff');
  await p.locator('.op-dh button.op-dp').click();
  await p.waitForFunction(() => document.querySelector('.pv .sh b').textContent === 'narrative.py' && !!document.querySelector('.pv-view .op-code'));
  s = await sheet();
  check('op-diff: its name opens the file in the sheet (the lines it added marked), ‹ goes back to the changes', s.back && s.small.includes('+2 −1') && await p.evaluate(() => document.querySelectorAll('.op-code .op-ln.add').length === 2), s);
  await p.locator('.pv .pvb').click();
  await p.waitForFunction(() => document.querySelector('.pv .sh b').textContent === '改动' && document.querySelector('.op-df.on b')?.textContent === 'narrative.py');
  check('op-diff: back on the changes, on the same file', true);
  await shut();
  await p.locator('#msg').fill('/diff');
  await sleep(500); await p.keyboard.press('Enter'); await sleep(200); await p.keyboard.press('Enter');
  await p.waitForFunction(() => document.querySelector('.pv .sh b').textContent === '改动' && !document.querySelector('.pv').classList.contains('off'));
  check('op-diff: /diff opens it too, and never reaches the agent', !st.claude().some(e => e.ev === 'turn' && e.text === '/diff'));
  await shut();

  // ⌘⏎ before the way is picked opens the panel on the question, and starts nothing
  await p.locator('#msg').fill('');
  await p.keyboard.press('Meta+Enter');
  await p.waitForFunction(() => !document.querySelector('.lp').classList.contains('off'));
  await sleep(400);
  const q = await p.evaluate(() => ({ steps: [...document.querySelectorAll('.lp .pl .st')].map(e => e.querySelector('h5').textContent), br: document.querySelector('.lp .br').textContent, b: [...document.querySelectorAll('.lp .op-choose button')].map(b => b.textContent) }));
  check('op-route: ⌘⏎ opens the panel on the same question, and nothing starts until a way is picked', q.steps.join() === '走哪条' && q.b.join('|') === '合进 main|推分支开 PR' && q.br.endsWith('→ ?') && !st.row(T).land, q);
  await st.shot('op-route-panel');

  // Picked: kept for the repository, and the landing runs with only its own steps.
  await st.call(`/sessions/${T}/land`, { action: 'msg', msg: 'feat(narrative): keep the evening to three sentences' });
  await p.locator('.conv .ask [data-act="landvia"][data-v="pr"]').click();
  await p.waitForFunction(() => document.querySelector('.toast')?.textContent === '记住了：timesink 以后都推分支开 PR');
  const kept = (await st.call('/settings')).settings.land;
  check('op-route: the way picked is kept for timesink and nothing else', kept[R1]?.via === 'pr' && kept[R2]?.via === 'merge' && kept[R3]?.gates?.length === 2, kept);
  const l1 = await st.until('timesink at push', () => st.row(T)?.land?.s === 'wait' && st.row(T).land, 30000);
  await sleep(400);
  const pl = await p.evaluate(() => ({ steps: [...document.querySelectorAll('.lp .pl .st')].map(e => `${e.querySelector('h5').firstChild.textContent}:${e.dataset.s}`), br: document.querySelector('.lp .br').textContent,
    chip: document.querySelector('.land .lbl')?.textContent, ask: document.querySelector('.conv .ask .ask-q')?.textContent, why: document.querySelector('.lp .st[data-s="wait"] .why')?.textContent, files: document.querySelectorAll('.lp .files button').length }));
  check('op-plain: no gates and no restart here: 改动, 提交, 推分支开 PR, 清理 worktree, and it waits before pushing', pl.steps.join() === '改动:ok,提交:ok,推分支、开 PR:wait,清理 worktree:todo' && pl.br.endsWith('→ PR')
    && pl.why.includes('开 PR') && pl.chip === '落地 · 等你点头' && pl.ask === '要落地吗？提交，推分支开 PR。' && pl.files === 4 && l1.steps[1].st === 'skip' && l1.steps[4].st === 'skip' && !existsSync(ghCalls), { pl, steps: l1.steps.map(x => x.st) });
  await st.shot('op-plain');
  // The remembered way: a new session in timesink no longer asks.
  const T2 = await st.session('EDIT README.md', { cwd: R1, tree: true });
  const d2 = await st.until('second timesink session measured', () => st.row(T2)?.dirty);
  check('op-route: asked once: the next session in timesink goes by pull request without asking', !d2.ask && d2.ways[0] === 'pr', d2);
  await p.locator('.lp [data-act="lpallow"]').click();
  const l2 = await st.until('timesink landed', () => st.row(T)?.land?.s === 'done' && st.row(T).land, 30000);
  await p.waitForFunction(() => document.querySelector('.lp').classList.contains('off') && !!document.querySelector('.conv .ask .op-landed'));
  await sleep(400);
  const gh = (await readFile(ghCalls, 'utf8')).trim().split('\n').map(x => JSON.parse(x));
  const done = await p.evaluate(() => document.querySelector('.conv .ask').textContent);
  check('op-done: done folds into one line, 落地了 · PR #128, and the panel folds away', done === '✓落地了 · PR #128' && l2.pr === 'https://github.com/example/timesink/pull/128'
    && gh[0].slice(0, 6).join(' ') === `pr create --base main --head ${l1.branch}` && existsSync(t1), { done, gh, pr: l2.pr, branch: l1.branch, t1: existsSync(t1) });
  await st.shot('op-done');
  await p.locator('.conv .ask .op-landed button').click();
  await sleep(200);
  check('op-done: the PR number opens it in the browser', (await calls('openUrl')).some(c => c[0] === 'https://github.com/example/timesink/pull/128'));

  // companion: the owner's gates and restart, merged, stopped before the push
  const C = await st.session('把托盘图标拿掉', { cwd: R2, tree: true });
  const t2 = st.row(C).cwd;
  await writeFile(path.join(t2, 'electron', 'companion.ts'), 'export const her = 1;\n'); await rm(path.join(t2, 'src', 'trayIcon.ts'));
  await st.send(C, '菜单栏图标也不要了');
  const d3 = await st.until('companion measured', () => st.row(C)?.dirty?.n === 2 && st.row(C).dirty);
  check('op-land: configured gates and restart show on the row before it starts; the way is the one configured', !d3.ask && d3.ways[0] === 'merge' && d3.gates?.join() === 'test -f README.md,git diff --check' && d3.restart?.join() === restartCmd, d3);
  await st.open(C);
  await st.call(`/sessions/${C}/land`, { action: 'msg', msg: 'feat(companion): drop the tray icon' });
  await p.locator('.conv .ask [data-act="land"]').click();
  const l3 = await st.until('companion at push', () => st.row(C)?.land?.s === 'wait' && st.row(C).land, 30000);
  await sleep(400);
  const pc = await p.evaluate(() => ({ steps: [...document.querySelectorAll('.lp .pl .st')].map(e => `${e.querySelector('h5').firstChild.textContent}:${e.dataset.s}`), gates: [...document.querySelectorAll('.lp .gates span')].map(g => g.textContent),
    ask: document.querySelector('.conv .ask .ask-q')?.textContent, btn: document.querySelector('.conv .ask button')?.textContent, lbl: document.querySelector('.conv .ask > span:last-child')?.textContent, acts: [...document.querySelectorAll('.lp .st[data-s="wait"] .row2 button')].map(b => b.textContent) }));
  check('op-land: 改动, 门禁, 提交, 合进 main, 重启, then it stops before the push and waits', pc.steps.join() === '改动:ok,门禁:ok,提交:ok,合进 main:ok,重启:ok,推送:wait,清理 worktree:todo'
    && pc.gates.join() === '✓ test -f README.md,✓ git diff --check' && pc.acts.join('|') === '允许|拒绝' && git(R2, 'rev-parse', 'main') !== git(path.join(st.HOME, 'origins', 'companion.git'), 'rev-parse', 'main'), pc);
  check('op-land: the conversation says what it does and where it is', pc.ask === '要落地吗？提交、合进 main、跑重启命令、推送。' && /落地面板开着/.test(pc.btn) && pc.lbl === '落地 · 等你点头' && existsSync(path.join(R2, '.restarted')), pc);
  await st.shot('op-land');
  await p.locator('.lp [data-act="lpallow"]').click();
  await st.until('companion landed', () => st.row(C)?.land?.s === 'done' && st.row(C)?.gone, 30000);
  await p.waitForFunction(() => /落地了 · 合进 main/.test(document.querySelector('.conv .ask')?.textContent ?? ''));
  check('op-done: a merge folds into 落地了 · 合进 main: pushed, and its worktree cleaned away', git(R2, 'rev-parse', 'main') === git(path.join(st.HOME, 'origins', 'companion.git'), 'rev-parse', 'main') && !existsSync(t2) && l3.via === 'merge');
  await st.shot('op-done-merge');

  // notes: a gate that fails says which and why, with the two ways on
  const N = await st.session('写发布说明', { cwd: R3, tree: true });
  await put(path.join(st.row(N).cwd, 'docs', 'release.md'), '# 0.9\n');
  await st.send(N, '再补一句');
  await st.until('notes measured', () => st.row(N)?.dirty?.n === 1);
  await st.open(N);
  await st.call(`/sessions/${N}/land`, { action: 'msg', msg: 'docs: write the release notes' });
  await p.locator('.conv .ask [data-act="land"]').click();
  const l4 = await st.until('notes at the gate', () => st.row(N)?.land?.s === 'fail' && st.row(N).land, 30000);
  await sleep(400);
  const pg = await p.evaluate(() => { const g = document.querySelector('.lp .st[data-id="gate"]'); return { s: g.dataset.s, chips: [...g.querySelectorAll('.gates span')].map(x => x.textContent), why: g.querySelector('.why.er')?.textContent, acts: [...g.querySelectorAll('.row2 button')].map(b => b.textContent),
    foot: document.querySelector('.lp .lp-f').textContent, chip: document.querySelector('.land .lbl')?.textContent, commit: document.querySelector('.lp .st[data-id="commit"]').dataset.s }; });
  check('op-gate: a red gate stops the line there and says which one and why', pg.s === 'fail' && pg.chips.join() === '✓ test -f README.md,✕ sh scripts/check-changelog.sh · 没过' && pg.why.includes('sh scripts/check-changelog.sh 没过') && pg.why.includes('CHANGELOG.md 里还没有 0.9 这一节')
    && pg.commit === 'todo' && pg.foot === '门禁没过，停在这里' && pg.chip === '落地 · 门禁没过' && l4.i === 1, pg);
  check('op-gate: with 让它修 and 留在分支上', pg.acts.join('|') === '让 Claude 修|留在分支上', pg.acts);
  await st.shot('op-gate');
  await p.locator('.lp [data-act="lpstay"]').click();
  await st.until('notes stayed', () => st.row(N) && !st.row(N).land);
  check('op-gate: 留在分支上 leaves the branch and main as they were', git(R3, 'rev-parse', 'main') === git(path.join(st.HOME, 'origins', 'notes.git'), 'rev-parse', 'main') && git(R3, 'log', '-1', '--format=%s', 'main') === 'init');

  // Jarvis's own repository: its restart is its own, and nothing is asked
  const K = await st.session('改一下 companion', { cwd: J, tree: true });
  await put(path.join(st.row(K).cwd, 'desktop', 'resonance', 'src', 'x.ts'), 'export const x = 1;\n');
  await st.send(K, '好了吗');
  const d5 = await st.until('jarvis measured', () => st.row(K)?.dirty?.n === 1 && st.row(K).dirty);
  await st.open(K);
  const sum5 = await p.evaluate(() => document.querySelector('.conv .ask .ask-q')?.textContent);
  check('restart only where it belongs: Jarvis\'s own repository restarts the companion, merges without asking, and has no origin to push to', d5.restart?.join() === 'companion' && !d5.ask && d5.local === true
    && sum5 === '要落地吗？提交、合进 main、重启 companion。' && !d1.restart, { d5, sum5 });

  check('no errors on the page', !st.errors.length, st.errors);

  // ---------- the PDF in the window's own viewer (Electron, as the Agents window: sandboxed, plugins on, the key added) ----------
  if (!existsSync('/usr/bin/xvfb-run')) console.log('SKIP op-pdf in Electron: no xvfb-run here');
  else {
    await st.open(A);
    const ed = await mkdtemp(path.join(os.tmpdir(), 'jarvis-open-electron-')), main = path.join(ed, 'pdf-window.cjs'), out = path.join(ed, 'pdf-window.png');
    // Its page drawn: the share of the PDF's frame that is white paper, and dark text on it.
    await writeFile(main, `const { app, BrowserWindow, session } = require('electron');
const wait = ms => new Promise(r => setTimeout(r, ms)), E = process.env;
app.setPath('userData', require('path').join(E.E_DIR, 'data'));
app.whenReady().then(async () => {
  session.defaultSession.webRequest.onBeforeSendHeaders({ urls: ['http://127.0.0.1/*'] }, (d, cb) => cb({ requestHeaders: new URL(d.url).port === E.E_PORT ? { ...d.requestHeaders, Authorization: 'Bearer ' + E.E_KEY } : d.requestHeaders }));
  const win = new BrowserWindow({ width: 1280, height: 820, show: true, backgroundColor: '#0c0d20', webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, webviewTag: true, plugins: true } });
  await win.loadURL(E.E_URL);
  const js = s => win.webContents.executeJavaScript(s);
  for (let i = 0; i < 100 && await js("!!document.querySelector('#win.booting')"); i++) await wait(150);
  await wait(1000);
  const clicked = await js(\`(() => { const el = [...document.querySelectorAll('.chat code.ref')].reverse().find(e => e.dataset.ref === 'docs/发布清单.pdf'); if (!el) return false; el.click(); return true; })()\`);
  await wait(6000);
  const small = await js("document.querySelector('.pv .sh small')?.textContent ?? ''"), errors = await js("document.querySelectorAll('.pv-err').length");
  const r = await js("(() => { const b = document.querySelector('iframe.op-pdf')?.getBoundingClientRect(); return b ? { x: Math.round(b.x), y: Math.round(b.y), width: Math.round(b.width), height: Math.round(b.height) } : null; })()");
  let paper = 0, ink = 0;
  if (r) {
    const img = await win.webContents.capturePage(r), { width, height } = img.getSize(), px = img.toBitmap();
    for (let i = 0; i < px.length; i += 4) { const v = (px[i] + px[i + 1] + px[i + 2]) / 3; if (v > 245) paper++; else if (v < 90 && Math.abs(px[i] - px[i + 2]) < 12) ink++; }
    paper /= width * height; ink /= width * height;
  }
  require('fs').writeFileSync(E.E_OUT, (await win.webContents.capturePage()).toPNG());
  console.log('RESULT ' + JSON.stringify({ clicked, small, errors, frame: r, paper, ink }));
  app.quit();
});
`);
    const res = await new Promise(done => {
      const c = spawn('xvfb-run', ['-a', '-s', '-screen 0 1400x900x24', path.join(app, 'node_modules', '.bin', 'electron'), '--no-sandbox', main], { env: { ...process.env, E_URL: p.url(), E_PORT: new URL(st.API).port, E_KEY: st.key, E_OUT: out, E_DIR: ed }, stdio: ['ignore', 'pipe', 'pipe'] });
      let o = ''; c.stdout.on('data', b => { o += b; }); c.stderr.on('data', b => { o += b; });
      const t = setTimeout(() => c.kill('SIGKILL'), 60000);
      c.on('exit', () => { clearTimeout(t); done(o); });
    });
    const got = JSON.parse(/RESULT (.*)/.exec(res)?.[1] ?? 'null');
    if (st.shots && existsSync(out)) await writeFile(path.join(st.shots, 'op-pdf.png'), await readFile(out));
    await rm(ed, { recursive: true, force: true });
    check('op-pdf: in Electron (sandboxed, plugins on, the key added) the window\'s own viewer draws the PDF\'s pages in the sheet', !!got?.clicked && /^docs · 4 页 · /.test(got.small) && got.errors === 0
      && got.frame?.width > 500 && got.paper > .2 && got.ink > .0005, got ?? res.slice(-2000));
  }
  console.log(`\n${checks.length} checks passed`);
} catch (e) {
  console.error(e, '\n--- host ---\n', st.log().slice(-3000), '\n--- page errors ---\n', st.errors);
  await st.shot('failed').catch(() => {});
  process.exitCode = 1;
} finally { await st.close(); }
