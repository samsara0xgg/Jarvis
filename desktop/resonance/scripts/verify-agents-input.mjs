// Run after npm run build. The composer beyond typing, on the stage (scripts/agents-stage.mjs): files dropped anywhere
// on the window, of any kind, and a right click on one; the / menu in two groups and /add-dir; each agent's permission
// modes, with 完全放开 asked once; the line under a step that ran into an MCP server that wants a sign-in or failed, and
// /mcp with every server. SHOTS=<folder> keeps a screenshot of each point.
// The window's own calls are recorded as the stage records them; a dropped file's path is a real file in the stage's
// folder, and a dropped folder is told as a folder the way the Mac's drop tells it.
import assert from 'node:assert/strict';
import { mkdirSync, truncateSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { stage } from './agents-stage.mjs';

const st = await stage();
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const p = st.page, wait = ms => p.waitForTimeout(ms);
const calls = k => p.evaluate(k => window.__agentsCalls.filter(c => c[0] === k), k);
const items = async id => (await st.call(`/sessions/${id}`)).items;
// The window's bridge as the Mac has it for what the stage cannot do: where a dropped file is, the folder picker's
// answer, and a file put on the clipboard.
await st.context.addInitScript(() => {
  const rec = (k, f) => (...a) => { window.__agentsCalls.push([k, ...a]); return f(...a); };
  window.__paths = {}; window.__dirs = new Set(); window.__folder = '';
  Object.assign(window.agents, { pathOf: f => window.__paths[f?.name] ?? '', folder: rec('folder', async () => window.__folder), copyFile: rec('copyFile', async () => 'file') });
  const entry = DataTransferItem.prototype.webkitGetAsEntry;
  DataTransferItem.prototype.webkitGetAsEntry = function () { return window.__dirs.has(this.getAsFile()?.name) ? { isDirectory: true } : entry.call(this); };
});
// Files dropped or pasted: made in the page, each with its real path; `big` stands a size in, `dir` says it is a folder.
const DROP = path.join(st.tmp, 'drop');
async function give(kind, target, files) {
  const dt = await p.evaluateHandle(fs => {
    const dt = new DataTransfer();
    for (const f of fs) {
      window.__paths[f.name] = f.path;
      const file = new File([Uint8Array.from(atob(f.b64), c => c.charCodeAt(0))], f.name, { type: f.type });
      if (f.big) Object.defineProperty(file, 'size', { value: f.big });
      dt.items.add(file);
      if (f.dir) window.__dirs.add(f.name);
    }
    return dt;
  }, files);
  if (kind === 'paste') await p.evaluate(dt => document.querySelector('#msg').dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true })), dt);
  else await p.dispatchEvent(target, kind, { dataTransfer: dt });
  return dt;
}
const file = (name, content, type = '', o = {}) => {
  const f = path.join(DROP, name);
  if (o.dir) mkdirSync(f, { recursive: true }); else writeFileSync(f, content);
  if (o.big) truncateSync(f, o.big);
  return { name, path: f, type, b64: Buffer.from(o.dir || o.big ? '' : content).toString('base64'), ...o };
};
try {
  mkdirSync(path.join(DROP, 'adr'), { recursive: true }); writeFileSync(path.join(DROP, 'adr', '0001.md'), '# One\n');
  const id = await st.session('hello PLAN');
  await st.open(id);
  const origin = new URL(p.url()).origin;
  await st.context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin });
  const png = await p.screenshot({ clip: { x: 300, y: 80, width: 480, height: 300 } });
  const PDF = '%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n';
  const F = {
    png: file('界面截图.png', png, 'image/png'), pdf: file('设计评审.pdf', PDF, 'application/pdf'), log: file('crash.log', 'Terminating app due to uncaught exception\n', 'text/plain'),
    ts: file('page.ts', 'export function renderComposer() {\n  return 1;\n}\n', 'video/mp2t'), md: file('发布说明.md', '# 发布说明\n\n- 输入框收各种文件\n', 'text/markdown'),
    mov: file('录屏.mov', '', 'video/quicktime', { big: 186 << 20 }), dir: { name: 'adr', path: path.join(DROP, 'adr'), type: '', b64: '', dir: true }, key: file('评审.key', 'PK', ''),
  };
  F.png.b64 = png.toString('base64');

  // ---------- in-drop: anywhere on the window, a calm veil, then the files in the composer ----------
  const three = [F.png, F.pdf, F.log];
  await give('dragenter', '.conv', three);
  await wait(250);
  const veil = await p.locator('.in-drop:not([hidden])').innerText().catch(() => '');
  check('in-drop: files held over the conversation bring a calm veil that says where they go', veil.includes('放下就行') && veil.includes('3 个文件，放进「Stand-in: hello PLAN」'), veil);
  await st.shot('in-drop');
  await give('drop', '.conv', three);
  await wait(300);
  const chips = () => p.evaluate(() => [...document.querySelectorAll('.c-files > [data-in-k]')].map(c => ({ kind: c.dataset.kind, ref: c.dataset.ref, text: c.innerText.trim().replace(/\s+/g, ' '), img: !!c.querySelector('img') })));
  let cs = await chips();
  check('in-drop: dropped, the veil goes and the files wait in the composer, the picture as a small picture', await p.locator('.in-drop').isHidden() && cs.length === 3 && cs[0].img && cs[0].ref === F.png.path
    && cs[1].text.includes('设计评审.pdf') && cs[2].text.includes('crash.log'), cs);
  await p.click('.c-files [data-in-k="2"] .in-x', { force: true });
  check('a file comes off the composer with its ✕', (await chips()).length === 2);
  await p.evaluate(() => [...document.querySelectorAll('.c-files [data-act="unfile"]')].reverse().forEach(b => b.click()));

  // ---------- in-types: pictures, PDFs, code, text, video, folders and anything else ----------
  const all = [F.png, F.pdf, F.log, F.ts, F.md, F.mov, F.dir, F.key];
  await give('drop', '#msg', all);
  await wait(300);
  cs = await chips();
  const by = n => cs.find(c => c.text.includes(n) || c.ref?.endsWith(n));
  check('in-types: every kind is taken, each with its type and name', cs.map(c => c.kind).join() === 'img,pdf,text,code,md,media,dir,file'
    && by('设计评审.pdf').text.startsWith('PDF') && by('page.ts').text.startsWith('TS') && by('发布说明.md').text.startsWith('MD') && by('crash.log').text.startsWith('LOG') && by('评审.key').text.startsWith('KEY'), cs);
  check('in-types: a folder says so, and a file over 30 MB goes by its path only', by('adr').text.includes('文件夹') && by('录屏.mov').text.includes('186 MB · 只给路径')
    && await p.locator('.in-fc.in-big').count() === 1, cs);
  await p.mouse.move(8, 400);
  await wait(200);
  await st.shot('in-types');
  await p.click('.c-files [data-kind="code"]');
  await p.waitForSelector('.sheet.pv:not(.off)', { timeout: 5000 });
  await wait(700);
  const pv = await p.locator('.sheet.pv .sh b').innerText();
  check('in-types: a click on a file opens it on the right', pv === 'page.ts' && (await p.locator('.sheet.pv .pv-view').innerText()).includes('renderComposer'), pv);
  await st.shot('in-types-open');
  await p.click('.sheet.pv [data-act="pvclose"]');
  await wait(500);
  await p.click('.c-files [data-kind="dir"]');
  check('in-types: a folder opens in Finder', (await calls('reveal')).some(c => c[1] === F.dir.path));

  // ---------- in-attach-menu: 复制 · 复制路径 · 在右边打开 · 在访达里显示 ----------
  await p.click('.c-files [data-kind="pdf"]', { button: 'right' });
  await wait(250);
  const menuText = () => p.locator('.pop.on').innerText().then(t => t.split('\n').map(x => x.trim()).filter(Boolean).join(' · '));
  check('in-attach-menu: a right click on a file in the composer', await menuText() === '复制 · 复制路径 · 在右边打开 · 用默认的 app 打开 · 在访达里显示', await menuText());
  await p.click('.pop.on [data-v="path"]');
  await wait(200);
  check('in-attach-menu: 复制路径 puts its path on the clipboard', await p.evaluate(() => navigator.clipboard.readText()) === F.pdf.path);
  await p.click('.c-files [data-kind="pdf"]', { button: 'right' }); await p.click('.pop.on [data-v="finder"]');
  await p.click('.c-files [data-kind="pdf"]', { button: 'right' }); await p.click('.pop.on [data-v="app"]');
  await p.click('.c-files [data-kind="dir"]', { button: 'right' });
  const dirMenu = await menuText();
  await p.click('.pop.on [data-v="finder"]');
  check('in-attach-menu: 在访达里显示 and 用默认的 app 打开 go to the Mac; a folder has no preview line', (await calls('revealFile')).some(c => c[1] === F.pdf.path) && (await calls('openPath')).some(c => c[1] === F.pdf.path)
    && dirMenu === '复制 · 复制路径 · 在访达里显示' && (await calls('revealFile')).some(c => c[1] === F.dir.path), dirMenu);

  // Sent: each goes as the agent can take it, and the message keeps where it was.
  await p.fill('#msg', '按评审把设置页通知那一组的开关对齐，截图是现在的样子');
  await p.keyboard.press('Enter');
  await st.until('the turn with files', () => st.claude().some(e => e.ev === 'turn' && e.text.includes('Attached files:')));
  await st.until('settled', () => st.row(id).st === 'done');
  const turn = st.claude().find(e => e.ev === 'turn' && e.text.includes('Attached files:'));
  const sent = (await items(id)).filter(i => i.k === 'you').at(-1);
  const lines = turn.text.split('Attached files:\n')[1].split('\n');
  check('sent: the picture and the PDF go as themselves, every other file by its path, a folder\'s ending in /', turn.blocks.includes('image') && turn.blocks.includes('document')
    && [F.log, F.ts, F.md, F.mov, F.key].every(f => lines.includes(`- ${f.path}`)) && lines.includes(`- ${F.dir.path}/`), { blocks: turn.blocks, lines });
  check('sent: the conversation keeps where each file is', sent.files.length === 8 && sent.files.every(f => f.path) && sent.files.find(f => f.name === 'adr').path === `${F.dir.path}/` && !!sent.files[0].img, sent.files);
  await wait(500);
  const mini = await p.locator('.conv .you .att .in-mfc').allInnerTexts();
  check('sent: under the message, its picture as a picture and its files as small chips', await p.locator('.conv .you .att .pic img').count() >= 1 && mini.length === 7 && mini.some(t => t.includes('adr') && t.includes('文件夹')), mini);
  const pdfTag = p.locator('.conv .you .att .in-mfc', { hasText: '设计评审.pdf' }).last();
  await pdfTag.scrollIntoViewIfNeeded();
  await pdfTag.click({ button: 'right', position: { x: 60, y: 20 } });
  await wait(250);
  check('in-attach-menu: a right click on a file a message went with', await menuText() === '复制 · 复制路径 · 在右边打开 · 用默认的 app 打开 · 在访达里显示', await menuText());
  await st.shot('in-attach-menu');
  await p.click('.pop.on [data-v="copy"]');
  await wait(200);
  check('in-attach-menu: 复制 puts the file itself on the clipboard', (await calls('copyFile')).some(c => c[1] === F.pdf.path) && (await p.locator('.toast').innerText()).includes('复制了「设计评审.pdf」'));
  await pdfTag.click({ button: 'right', position: { x: 60, y: 20 } }); await p.click('.pop.on [data-v="side"]');
  await p.waitForSelector('.sheet.pv:not(.off)', { timeout: 5000 });
  check('in-attach-menu: 在右边打开 shows it on the right', (await p.locator('.sheet.pv .sh b').innerText()) === '设计评审.pdf');
  await p.click('.sheet.pv [data-act="pvclose"]');
  await wait(500);
  await p.locator('.conv .you .att .pic').last().click({ button: 'right' });
  await wait(200);
  const picMenu = await menuText();
  await p.click('.pop.on [data-v="copy"]');
  await p.waitForFunction(() => /复制/.test(document.querySelector('.toast')?.textContent ?? '') && document.querySelector('.toast').textContent.includes('界面截图'), null, { timeout: 5000 }).catch(() => {});
  const clip = await p.evaluate(async () => (await navigator.clipboard.read()).flatMap(i => i.types));
  check('in-attach-menu: a picture is copied as a picture', picMenu === '复制 · 复制路径 · 用默认的 app 打开 · 在访达里显示' && clip.includes('image/png'), { picMenu, clip });

  // ---------- the file picker and the clipboard take any file ----------
  check('the file picker takes any kind of file', await p.locator('#file').getAttribute('accept') === null);
  await p.setInputFiles('#file', F.log.path);
  await wait(300);
  await give('paste', '#msg', [F.md]);
  await wait(300);
  cs = await chips();
  check('the picker and a paste bring files that are not pictures', cs.map(c => c.kind).join() === 'text,md' && cs[1].ref === F.md.path, cs);
  await p.evaluate(() => [...document.querySelectorAll('.c-files [data-act="unfile"]')].reverse().forEach(b => b.click()));

  // Read back from the transcript (a fork reads it), the files keep their paths.
  const fk = await st.call(`/sessions/${id}/fork`, {});
  await st.until('the fork read back', async () => (await items(fk.id))?.some(i => i.k === 'you' && i.files?.length));
  const back = (await items(fk.id)).find(i => i.k === 'you' && i.files?.length);
  check('read back, the files named by path keep their paths', [F.log, F.ts, F.md, F.mov, F.key].every(f => back.files.some(b => b.path === f.path && b.name === f.name))
    && back.files.some(b => b.name === 'adr' && b.path === `${F.dir.path}/`), back.files);

  // ---------- in-slash: the window's own commands above the agent's, one filter over both ----------
  const menu = () => p.evaluate(() => {
    const out = []; let g = null, sel = '';
    for (const el of document.querySelector('.c-menu').children) {
      if (el.classList.contains('in-h')) out.push(g = { head: el.textContent, cmds: [] });
      else if (el.tagName === 'BUTTON') { g.cmds.push(el.querySelector('code').textContent); if (el.classList.contains('on')) sel = el.querySelector('code').textContent; }
    }
    return { groups: out, sel, foot: document.querySelector('.c-menu .in-f')?.textContent ?? '', on: document.querySelector('.c-menu').classList.contains('on') };
  });
  const type = async text => { await p.fill('#msg', ''); await p.locator('#msg').pressSequentially(text); await wait(500); };
  await p.locator('#msg').focus();
  await type('/');
  let m = await menu();
  check('in-slash: / opens the window\'s own commands above Claude Code\'s, with how to pick', m.on && m.groups.length === 2 && m.groups[0].head === '窗口里的' && m.groups[0].cmds.includes('/add-dir')
    && m.groups[1].head === 'Claude Code 的' && m.groups[1].cmds.includes('/fake-skill') && !m.groups[1].cmds.includes('/add-dir') && m.sel === m.groups[0].cmds[0] && m.foot.includes('填进去'), m);
  await st.shot('in-slash');
  await type('/d');
  m = await menu();
  check('in-slash: one filter over both groups, a name that only holds it too', m.groups.length === 2 && m.groups[0].cmds.join() === '/add-dir' && m.sel === '/add-dir' && m.groups[1].cmds[0].startsWith('/d') && m.groups[1].cmds.every(c => c.slice(1).includes('d')), m);
  await p.keyboard.press('ArrowDown'); await wait(150);
  m = await menu();
  const second = m.groups[1].cmds[0];
  await p.keyboard.press('Tab'); await wait(200);
  check('in-slash: ↓ moves past the group line to the next command, ⇥ fills it in', m.sel === second && await p.inputValue('#msg') === `${second} ` && !(await menu()).on, { m, v: await p.inputValue('#msg') });

  // ---------- in-adddir: /add-dir picks a folder; the conversation says so ----------
  for (const n of ['timesink', 'resonance-lab']) mkdirSync(path.join(st.HOME, 'Projects', n, '.git'), { recursive: true });
  const other = path.join(st.HOME, 'elsewhere');
  mkdirSync(other, { recursive: true });
  await type('/add-');
  await p.keyboard.press('Enter'); await wait(150);
  check('in-adddir: ⏎ fills /add-dir in', await p.inputValue('#msg') === '/add-dir ');
  await p.keyboard.press('Enter');
  await p.waitForSelector('.in-sheet:not([hidden]) .in-dr', { timeout: 5000 });
  await wait(300);
  const sheetRows = () => p.evaluate(() => [...document.querySelectorAll('.in-sheet .in-dr')].map(b => b.innerText.replace(/\s+/g, ' ').trim() + (b.classList.contains('in-sel') ? '*' : '')));
  let rows = await sheetRows();
  check('in-adddir: /add-dir opens the folders you work in, and the Mac\'s picker for any other', rows.length === 3 && rows[0].startsWith('resonance-lab') && rows[0].endsWith('*') && rows[1].startsWith('timesink')
    && rows[2] === '选别的文件夹…' && !rows.some(r => r.startsWith('app ')) && await p.inputValue('#msg') === '', rows);
  await st.shot('in-adddir');
  await p.keyboard.press('ArrowDown'); await p.keyboard.press('Enter');
  const lab = path.join(st.HOME, 'Projects', 'timesink');
  await st.until('the folder added', () => st.row(id).dirs?.includes(lab));
  await wait(500);
  const noteText = await p.locator('.conv .note').last().innerText();
  check('in-adddir: ⏎ adds the folder for this session, and the conversation says so in one line', await p.locator('.in-sheet').isHidden() && noteText.includes('它也能动这些文件夹了') && noteText.includes('timesink'), noteText);
  await p.evaluate(() => document.querySelectorAll('.conv').forEach(c => { c.scrollTop = c.scrollHeight; }));
  await wait(200);
  await st.shot('in-adddir-done');
  await type('/add-dir'); await p.keyboard.press('Enter'); await p.keyboard.press('Enter');
  await p.waitForSelector('.in-sheet:not([hidden]) .in-dr', { timeout: 5000 });
  rows = await sheetRows();
  await p.evaluate(p => { window.__folder = p; }, other);
  await p.click('.in-sheet .in-other');
  await st.until('the picked folder added', () => st.row(id).dirs?.includes(other));
  check('in-adddir: a folder added shows 加过了; 选别的文件夹… asks the Mac\'s picker', rows.some(r => r.startsWith('timesink') && r.includes('加过了')) && (await calls('folder')).length === 1, rows);
  await p.keyboard.press('Escape');
  const third = path.join(st.HOME, 'Projects', 'resonance-lab');
  await type(`/add-dir ${third}`); await p.keyboard.press('Enter');
  await st.until('the typed folder added', () => st.row(id).dirs?.length === 3);
  check('in-adddir: /add-dir with a path adds that one, keeping the others', st.row(id).dirs.join() === [lab, other, third].join(), st.row(id).dirs);
  await type('/add-dir'); await p.keyboard.press('Enter'); await p.keyboard.press('Enter');
  await p.waitForSelector('.in-sheet:not([hidden])', { timeout: 5000 });
  await p.keyboard.press('Escape'); await wait(200);
  check('in-adddir: esc puts the sheet away and nothing is added', await p.locator('.in-sheet').isHidden() && st.row(id).dirs.length === 3);

  // ---------- in-mode, in-bypass: Claude Code's five modes; 完全放开 asked once, then warm ----------
  const modeMenu = () => p.evaluate(() => [...document.querySelectorAll('.pop.on button[data-k="mode"]')].map(b => ({ v: b.dataset.v, name: b.firstChild.textContent, sub: b.querySelector('small')?.textContent ?? '', on: b.classList.contains('on'), warm: b.classList.contains('in-warm') })));
  const warmChip = () => p.evaluate(() => { const c = document.querySelector('.tb.mode'); return getComputedStyle(c).color === 'rgb(255, 201, 143)'; });
  await p.evaluate(() => document.querySelectorAll('.conv').forEach(c => { c.scrollTop = c.scrollHeight; }));
  await p.click('.tb.mode'); await wait(300);
  let modes = await modeMenu();
  check('in-mode: Claude Code\'s five modes by their Chinese names, each saying what it means', modes.map(m => m.name).join(' · ') === '自动 · 改之前问我 · 自动接受修改 · 计划模式 · 完全放开'
    && modes.map(m => m.v).join() === 'auto,default,acceptEdits,plan,bypassPermissions' && modes.every(m => m.sub) && modes[0].on && modes[4].warm && modes.filter(m => m.warm).length === 1, modes);
  await st.shot('in-mode');
  await p.click('.pop.on [data-v="bypassPermissions"]'); await wait(400);
  const askText = () => p.locator('.c-rows .in-ask').innerText().catch(() => '');
  check('in-bypass: 完全放开 asks first, above the composer, and nothing has switched yet', (await askText()).includes('完全放开？它不再问你就改文件、跑命令。只在这个会话里。') && !await p.locator('.pop.on').count()
    && st.row(id).mode === 'auto' && !await warmChip(), await askText());
  await st.shot('in-bypass');
  await p.keyboard.press('Escape'); await wait(300);
  check('in-bypass: esc says 算了 and the mode stays', !await p.locator('.c-rows .in-ask').count() && st.row(id).mode === 'auto');
  await p.click('.tb.mode'); await wait(250); await p.click('.pop.on [data-v="bypassPermissions"]'); await wait(300);
  await p.locator('#msg').focus(); await p.keyboard.press('Enter');
  await st.until('bypass on', () => st.row(id).mode === 'bypassPermissions');
  await wait(500);
  const told = { chip: await warmChip(), ask: await p.locator('.c-rows .in-ask').count(), note: (await items(id)).filter(i => i.k === 'note').map(i => i.text) };
  check('in-bypass: ⏎ lets it go: the session switches, the conversation says so, and the mode chip turns warm', told.chip && !told.ask && told.note.includes('模式换成 完全放开'), told);
  // The folders added above let its Claude Code go, so the mode goes with the next start.
  await st.send(id, 'go on');
  check('in-bypass: its Claude Code runs with permissions bypassed from then on', st.claude().filter(e => e.ev === 'start').at(-1).args.join(' ').includes('bypassPermissions'), st.claude().filter(e => e.ev === 'start').at(-1).args);
  await st.shot('in-bypass-on', { x: 0, y: 700, width: 1280, height: 120 });
  await p.click('.tb.mode'); await wait(250);
  modes = await modeMenu();
  await p.click('.pop.on [data-v="default"]');
  await st.until('back to asking', () => st.row(id).mode === 'default');
  await wait(300);
  check('in-mode: any other mode switches at once, and the chip is plain again', modes[4].on && !await warmChip() && !await p.locator('.c-rows .in-ask').count());
  await type('/permissions'); await p.keyboard.press('Enter'); await p.keyboard.press('Enter'); await wait(400);
  check('in-mode: /permissions opens the same menu', (await modeMenu()).length === 5);
  await p.keyboard.press('Escape');

  // ---------- in-mcp-login: a server that wants a sign-in gets one line under the step that ran into it ----------
  const mcpLines = sel => p.evaluate(sel => [...document.querySelectorAll(sel)].filter(e => e.checkVisibility()).map(e => ({ text: e.querySelector('span').textContent,
    tone: /in-t-(\w+)/.exec(e.className)?.[1], acts: [...e.querySelectorAll('button')].map(b => b.textContent) })), sel);
  const lineAt = (what, sel, pred = l => l.length) => st.until(what, async () => { const l = await mcpLines(sel); return pred(l) && l; });
  const ml = await st.session('MCP tracker SLOW', { wait: false });
  await st.open(ml);
  let mq = await lineAt('the line under the live step', '.steps.live .step .in-mq');
  check('in-mcp-login: while it works, a server that wants a sign-in gets one line under the step that ran into it: 登录 · 这次不用', mq.length === 1 && mq[0].text === 'tracker 要登录才能用'
    && mq[0].tone === 'warm' && mq[0].acts.join() === '登录,这次不用' && await p.locator('.steps.live .step:has(> .in-mq) .a').innerText() === 'tracker · authenticate', mq);
  await p.mouse.move(5, 5);
  await st.shot('in-mcp-login');
  await st.until('the sign-in turn ends', () => st.row(ml).st === 'done');
  mq = await lineAt('the line over the answer', '.it > .in-mqs .in-mq');
  check('in-mcp-login: once the steps fold, the same line stands right under them, above the answer', mq.length === 1 && mq[0].text === 'tracker 要登录才能用'
    && await p.locator('.it > .in-mqs:first-child').count() === 1, mq);
  await p.click('.steps:has(.in-mq) .s-sum'); await wait(500);
  check('in-mcp-login: with the steps open it is under its step again, and only there', (await mcpLines('.steps.open .step .in-mq')).length === 1 && !(await mcpLines('.it > .in-mqs .in-mq')).length);
  await p.click('.steps.open .s-sum'); await wait(500);
  await p.click('.it > .in-mqs button[data-v="login"]');
  const opened = await st.until('the sign-in page opened', async () => (await calls('openUrl')).find(c => c[1].startsWith('https://tracker.example.com/')));
  const during = await mcpLines('.it > .in-mqs .in-mq');
  check('in-mcp-login: 登录 has the session\'s Claude Code give its sign-in page, opens it in the browser, and the line waits', opened[1] === 'https://tracker.example.com/authorize?client_id=fake'
    && st.claude().some(e => e.subtype === 'mcp_authenticate' && e.request.serverName === 'tracker') && during[0]?.text === '在浏览器里登录 tracker…' && during[0].tone === 'wait', { opened, during });
  mq = await lineAt('signed in', '.it > .in-mqs .in-mq', l => l[0]?.tone === 'mint');
  check('in-mcp-login: signed in, Claude Code connects it, and the line says so', mq[0].text === 'tracker 连上了 · 3 个工具' && !mq[0].acts.length, mq);
  await st.shot('in-mcp-login-done');

  // ---------- in-mcp-down: a server that failed: 重试 · 这次不用 ----------
  const mdn = await st.session('MCP flaky');
  await st.open(mdn);
  mq = await lineAt('the failed line', '.it > .in-mqs .in-mq');
  check('in-mcp-down: a server that failed gets one line saying why: 重试 · 这次不用', mq.length === 1 && mq[0].text === 'flaky connect ECONNREFUSED 127.0.0.1:9' && mq[0].tone === 'red'
    && mq[0].acts.join() === '重试,这次不用', mq);
  await p.mouse.move(5, 5);
  await st.shot('in-mcp-down');
  await p.click('.it > .in-mqs button[data-v="retry"]');
  mq = await lineAt('connected again', '.it > .in-mqs .in-mq', l => l[0]?.tone === 'mint');
  check('in-mcp-down: 重试 connects it again, and the line turns to 连上了', mq[0].text === 'flaky 连上了 · 1 个工具' && st.claude().some(e => e.subtype === 'mcp_reconnect' && e.request.serverName === 'flaky'), mq);
  await st.send(mdn, 'MCP tracker');
  const two = await lineAt('a second line', '.it > .in-mqs .in-mq', l => l.length === 2);
  await p.click('.it > .in-mqs .in-t-warm button[data-v="skip"]'); await wait(300);
  mq = await mcpLines('.in-mq');
  check('in-mcp-down: 这次不用 hides the line for this session', two[1].text === 'tracker 要登录才能用' && mq.length === 1 && mq[0].text === 'flaky 连上了 · 1 个工具', { two, mq });

  // ---------- in-mcp-list: /mcp, every server with its state, its tools and a switch ----------
  const panel = () => p.evaluate(() => {
    const el = document.querySelector('.in-mcp');
    return el && !el.hidden ? { head: el.querySelector('.in-mh b')?.textContent, foot: el.querySelector('.in-mf')?.textContent, rows: [...el.querySelectorAll('.in-mr')].map(r => ({
      name: r.querySelector('b').textContent, scope: r.querySelector('small')?.textContent ?? '', text: r.querySelector('.in-mt > span').textContent, st: /in-s-(\w+)/.exec(r.className)[1],
      act: r.querySelector('.in-ma')?.textContent ?? '', on: r.querySelector('.in-sw').getAttribute('aria-checked') === 'true', grey: r.querySelector('.in-sw').getAttribute('aria-disabled') === 'true' })) } : null;
  });
  const row = (pl, n) => pl.rows.find(r => r.name === n);
  await p.locator('#msg').focus();
  await type('/mcp'); await p.keyboard.press('Enter'); await p.keyboard.press('Enter');
  let pl = await st.until('the list', async () => { const x = await panel(); return x?.rows.length && x; });
  check('in-mcp-list: /mcp lists every server of the session: name, where it is set, state, tools, and a switch', pl.head === '这个会话的 MCP' && pl.rows.map(r => r.name).join() === 'docs,tracker,flaky'
    && row(pl, 'docs').text === '连上了 · 2 个工具' && row(pl, 'docs').scope === '用户' && row(pl, 'tracker').text === '要登录' && row(pl, 'tracker').act === '登录' && row(pl, 'flaky').text === '连上了 · 1 个工具'
    && pl.rows.every(r => r.on && !r.grey) && pl.foot.includes('这个文件夹'), pl);
  await p.mouse.move(5, 5);
  await st.shot('in-mcp-list-claude');
  await p.click('.in-mcp .in-mr:has(b:text-is("docs")) .in-sw');
  pl = await st.until('docs off', async () => { const x = await panel(); return row(x, 'docs')?.st === 'off' && x; });
  await st.shot('in-mcp-list-off');
  check('in-mcp-list: a switch turns a Claude Code server off', !row(pl, 'docs').on && row(pl, 'docs').text === '关着' && st.claude().some(e => e.subtype === 'mcp_toggle' && e.request.serverName === 'docs' && e.request.enabled === false), pl);
  await p.click('.in-mcp .in-mr:has(b:text-is("docs")) .in-sw');
  pl = await st.until('docs on', async () => { const x = await panel(); return row(x, 'docs')?.st === 'on' && x; });
  check('in-mcp-list: and on again, connected', row(pl, 'docs').on && row(pl, 'docs').text === '连上了 · 2 个工具' && st.claude().some(e => e.subtype === 'mcp_toggle' && e.request.serverName === 'docs' && e.request.enabled === true), pl);
  await p.keyboard.press('Escape'); await wait(200);
  check('in-mcp-list: esc closes it', !await panel());

  // Codex: the same place for /add-dir, which it takes each turn.
  // SLOW: the stand-in answers after a moment, as Codex does; one that ends its turn in the same breath as it starts it
  // can outrun the host learning the new thread.
  const cx = await st.session('hello SLOW', { agent: 'codex' });
  await st.open(cx);
  await p.locator('#msg').focus();
  await type('/');
  m = await menu();
  check('in-slash: a Codex session\'s menu has /add-dir as the window\'s and Codex\'s own below', m.groups[0].head === '窗口里的' && m.groups[0].cmds.includes('/add-dir') && m.groups[1].head === 'Codex 的' && m.groups[1].cmds.includes('/compact'), m);
  await type(`/add-dir ${other}`); await p.keyboard.press('Enter');
  await st.until('codex folder added', () => st.row(cx).dirs?.includes(other));
  check('in-adddir: a Codex session takes a folder too', (await items(cx)).some(i => i.k === 'note' && i.text.includes('elsewhere')));
  // ---------- in-mode-codex: Codex's four ----------
  await p.click('.tb.mode'); await wait(300);
  modes = await modeMenu();
  check('in-mode-codex: a Codex session has Codex\'s four modes', modes.map(m => m.name).join(' · ') === '自动 · 只读 · 完全放开 · 计划模式' && modes.map(m => m.v).join() === 'auto,read,full,plan'
    && modes.every(m => m.sub) && modes[2].warm, modes);
  await st.shot('in-mode-codex');
  await p.click('.pop.on [data-v="full"]'); await wait(300);
  const cxAsk = await askText();
  await p.click('.c-rows .in-ask [data-ok]');
  await st.until('codex full', () => st.row(cx).mode === 'full');
  await wait(300);
  check('in-mode-codex: 完全放开 asks in Codex\'s words, and 放开 switches it', cxAsk.includes('不限在这个文件夹里') && await warmChip(), cxAsk);
  // ---------- in-mcp-list, in-mcp-login: Codex's list, its switches grey; its sign-in from the line ----------
  await p.locator('#msg').focus();
  await type('/mcp'); await p.keyboard.press('Enter'); await p.keyboard.press('Enter');
  pl = await st.until('the Codex list', async () => { const x = await panel(); return x?.rows.length && x; });
  check('in-mcp-list: a Codex session lists its servers too, the switches grey', pl.rows.map(r => `${r.name}:${r.st}`).join() === 'broken:fail,docs:on,off:off,remote:auth' && row(pl, 'broken').act === '重连'
    && row(pl, 'remote').act === '登录' && row(pl, 'docs').text === '连上了 · 2 个工具' && pl.rows.every(r => r.grey) && pl.foot === 'Codex 的 MCP 在它的 config.toml 里开关', pl);
  await p.mouse.move(5, 5);
  await st.shot('in-mcp-list');
  await p.click('.in-mcp .in-mr:has(b:text-is("docs")) .in-sw', { force: true }); await wait(250);
  check('in-mcp-list: a grey switch says why', (await p.locator('.toast').innerText()).includes('config.toml') && row(await panel(), 'docs').on);
  await p.mouse.click(640, 200); await wait(200);
  check('in-mcp-list: a click anywhere else closes it', !await panel());
  await st.send(cx, 'MCP remote');
  mq = await lineAt('the Codex line', '.it > .in-mqs .in-mq');
  const cxLine = mq[0];
  await p.click('.it > .in-mqs button[data-v="login"]');
  mq = await lineAt('Codex signed in', '.it > .in-mqs .in-mq', l => l[0]?.tone === 'mint');
  check('in-mcp-login: a Codex server that wants a sign-in, the same line; 登录 opens its page, then it is connected', cxLine.text === 'remote 要登录才能用' && cxLine.acts.join() === '登录,这次不用'
    && (await calls('openUrl')).some(c => c[1] === 'https://remote.example.com/authorize?client_id=fake') && mq[0].text === 'remote 连上了 · 1 个工具', { cxLine, mq });
  await p.keyboard.press('Control+n');
  await wait(400);
  await p.locator('#msg').focus();
  await type('/add-dir'); await p.keyboard.press('Enter'); await p.keyboard.press('Enter');
  await wait(300);
  check('in-adddir: before there is a session it says to start one first', (await p.locator('.toast').innerText()).includes('开了会话再给它加文件夹'), await p.locator('.toast').innerText());
  await type('/mcp'); await p.keyboard.press('Enter'); await p.keyboard.press('Enter');
  await wait(300);
  check('in-mcp-list: before there is a session /mcp says to start one first', (await p.locator('.toast').innerText()).includes('开了会话再看它的 MCP') && !await panel());
  await p.fill('#msg', '');
  await p.click('.tb.mode'); await wait(250);
  await p.click('.pop.on [data-k="mode"].in-warm'); await wait(300);
  const newAsk = await p.locator('.pop.on.in-askpop').innerText().catch(() => '');
  await p.evaluate(() => { document.querySelector('.toast').hidden = true; });
  await st.shot('in-bypass-new');
  await p.click('.pop.on.in-askpop [data-ok]'); await wait(300);
  check('in-bypass: before there is a session the question stands where the menu was, and 放开 picks it for the new one', newAsk.includes('完全放开？') && await warmChip()
    && ['bypassPermissions', 'full'].includes(await p.locator('.tb.mode').getAttribute('data-m')), newAsk);

  check('no errors on the page', !st.errors.length, st.errors);
  console.log(`\n${checks.length} checks passed`);
} catch (e) {
  console.error(e, '\n--- host ---\n', st.log().slice(-3000));
  process.exitCode = 1;
} finally { await st.close(); }
