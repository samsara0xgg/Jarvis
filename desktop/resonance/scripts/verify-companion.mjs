// Companion prototype: scenes 01-07 in headless Chrome against the built page.
// The native bridge is stubbed; a fake hardware cutout sits on top like the real notch.
// Run after `npm run build`. Screenshots land in evidence/companion/.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.COMPANION_EVIDENCE_DIR ?? path.join(root, 'evidence/companion');
mkdirSync(dir, { recursive: true });
const port = Number(process.env.COMPANION_PORT ?? 5191); // another checkout may be previewing on the default
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(port), '--strictPort'], { cwd: root, stdio: 'ignore' });
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); };
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${port}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const context = await browser.newContext({ viewport: { width: 640, height: 722 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.__state = { passthrough: true, glass: [], ready: 0 };
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: callback => { window.__placement = callback; return () => {}; },
      onDisplayLeave: callback => { window.__leave = callback; return () => {}; },
      onTuck: callback => { window.__tuck = callback; return () => {}; },
      displayReady: () => { window.__state.ready++; },
      companionMenu: menu => { window.__state.menu = menu; },
      onCursor: callback => { window.__cursor = callback; return () => {}; },
      onCommand: callback => { window.__command = callback; return () => {}; },
      passthrough: value => { window.__state.passthrough = value; },
      focus: async () => {},
      material: rects => { window.__state.glass = rects; },
    };
  });
  await page.goto(`http://127.0.0.1:${port}/?companion=1`);
  // Desktop stand-in: a quiet blue-gray wallpaper, the menu bar, and the opaque camera cutout.
  await page.addStyleTag({ content: `html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}
    body::before{content:'';position:fixed;inset:0 0 auto;height:32px;background:rgb(255 255 255/.18);backdrop-filter:blur(20px)}
    body::after{content:'';position:fixed;z-index:10;pointer-events:none;top:0;left:227.5px;width:185px;height:32px;background:#000;border-radius:0 0 10px 10px}
    body.external::after{display:none}` });
  const hit = page.locator('.companion-hit');
  const place = () => hit.getAttribute('data-place');
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  const shot = (name, clip = { x: 120, y: 0, width: 400, height: 210 }) => page.screenshot({ path: path.join(dir, `${name}.png`), clip });
  const waitPlace = async value => { await page.waitForFunction(v => document.querySelector('.companion-hit')?.dataset.place === v, value); await page.waitForTimeout(900); };
  const lobe = { x: 195.5, y: 16 }, out = { x: 195.5, y: 72 };

  await page.waitForTimeout(800);
  check('01 rests in the island', await place() === 'home');
  await shot('01-home');
  const alphaAt = (x, y) => page.evaluate(([x, y]) => { const c = document.querySelector('.companion-canvas'), r = c.getBoundingClientRect(), k = c.width / r.width;
    return c.getContext('2d').getImageData(Math.round((x - r.left) * k), Math.round((y - r.top) * k), 1, 1).data[3]; }, [x, y]);
  check('01 at home her glass ball shows in the island', await alphaAt(lobe.x, 24) > 200);
  await page.evaluate(() => window.__command('homeGlass'));
  await page.waitForTimeout(1500);
  check('01 the tray switch sinks her back so only her eyes show', await alphaAt(lobe.x, 24) < 30 && await page.evaluate(() => window.__state.menu?.homeGlass === false));
  await shot('01-home-eyes');
  await page.evaluate(() => window.__command('homeGlass'));
  await page.waitForTimeout(1500);
  await move(170, 10);
  await page.waitForTimeout(80);
  check('01 the island takes clicks, so menu bar items hidden behind it are never hit', await page.evaluate(() => window.__state.passthrough === false));
  await move(140, 10);
  await page.waitForTimeout(80);
  check('01 the menu bar beside the island still gets its clicks', await page.evaluate(() => window.__state.passthrough === true));
  await move(lobe.x - 20, 14);
  await waitPlace('peek');
  check('01 peeks when the cursor approaches the island', true);
  await shot('01-peek');
  await move(out.x, out.y - 6);
  await waitPlace('out');
  check('02 comes out under the island on hover', await page.evaluate(() => window.__state.passthrough === false));
  check('02 keyboard chip appears beside her', await page.locator('.companion-chip.is-open').count() === 1);
  await shot('02-out-chip');
  await move(460, 400);
  await page.waitForTimeout(80);
  check('02 empty space passes clicks through', await page.evaluate(() => window.__state.passthrough === true));
  await move(out.x, out.y);
  await page.waitForTimeout(400);
  await move(out.x + 26 + 12 + 16, out.y);
  await page.locator('.companion-chip button').click();
  check('03 composer opens beneath her', await page.locator('.companion-composer.is-open').count() === 1);
  await page.keyboard.type('帮我整理今天的任务', { delay: 60 });
  await page.waitForTimeout(700);
  check('03 draft is typed into the composer', await page.locator('.companion-composer input').inputValue() === '帮我整理今天的任务');
  check('03 composer has native glass behind it', await page.evaluate(() => window.__state.glass.some(r => Math.round(r.width) === 300 && r.opacity > .9)));
  await shot('03-composer', { x: 20, y: 0, width: 400, height: 210 });
  await page.keyboard.press('Enter');
  await page.locator('.companion-bubble.is-open').waitFor();
  await page.waitForTimeout(500);
  await shot('03-reply');
  await page.waitForFunction(() => !document.querySelector('.companion-bubble.is-open'), null, { timeout: 6000 });
  check('03 text reply types out and clears', true);

  // Poke: press and hold briefly, then release into listening.
  await move(out.x, out.y);
  const box = await hit.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(160);
  await shot('02-press');
  await page.mouse.up();
  await page.locator('.companion-strip.is-open').waitFor();
  // Her face follows on the next animation frame.
  const face = (...ids) => page.waitForFunction(v => v.includes(document.querySelector('.companion-canvas')?.dataset.face), ids, { timeout: 1500 }).then(() => ids[0], () => null);
  check('04 poke starts listening with a live caption strip and one of her two listening faces', await face('35', '35b') === '35');
  await page.waitForFunction(() => document.querySelector('.strip-text')?.textContent === '把今天的任务整理一下', null, { timeout: 5000 });
  await page.waitForTimeout(250);
  check('04 once the caption ends she takes the task in, one of four takes', await face('31', '31b', '31c', '31d') === '31');
  check('04 then she thinks before answering', await face('30') === '30');
  await shot('04-thinking');
  await page.locator('.companion-bubble.is-open').waitFor({ timeout: 5000 });
  await page.waitForTimeout(500);
  check('05 she answers in a bubble beneath her, with her replying face', (await page.locator('.companion-bubble').textContent()).includes('好，我来整理。') && await face('39', '39b', '39c') === '39');
  await shot('05-speaking');
  await hit.click({ force: true });
  await page.waitForTimeout(600);
  check('05 poke while speaking interrupts and keeps listening', await page.locator('.companion-strip.is-open').count() === 1 && await page.locator('.companion-bubble.is-open').count() === 0);
  await hit.click({ force: true });
  await page.waitForTimeout(600);
  check('05 poke while listening ends voice', await page.locator('.companion-strip.is-open').count() === 0);

  await move(320, 14);
  await page.locator('.companion-dashboard.is-open').waitFor();
  await waitPlace('dock');
  check('06 dashboard opens from the notch and she docks on its edge', true);
  await shot('06-dock', { x: 120, y: 0, width: 400, height: 300 });
  await move(320, 200);
  await page.waitForTimeout(200);
  await move(600, 560);
  await page.waitForFunction(() => !document.querySelector('.companion-dashboard.is-open'), null, { timeout: 3000 });
  await waitPlace('home');
  check('06 closing the dashboard sends her home', true);
  await shot('06-home-again');
  await move(out.x, out.y);
  await waitPlace('out');
  await hit.dblclick({ force: true });
  await page.locator('.companion-dashboard.is-open').waitFor();
  await waitPlace('dock');
  await move(600, 560);
  await page.waitForTimeout(1200);
  check('06 a double click on her opens the Dashboard, and it stays when the cursor leaves',
    await page.locator('.companion-dashboard.is-open').count() === 1 && await page.locator('.companion-strip.is-open').count() === 0);
  await hit.dblclick({ force: true });
  await page.waitForFunction(() => !document.querySelector('.companion-dashboard.is-open'), null, { timeout: 3000 });
  await waitPlace('home');
  await page.waitForTimeout(400);
  check('06 another double click on her closes it, and neither double click starts voice', await page.locator('.companion-strip.is-open').count() === 0);

  // 09: the Dashboard around her. The home's blocks in their default order; each row grows into its page
  // at the same panel height, her face follows the page, and ‹ or Esc goes back one level.
  const panel = page.locator('.companion-dashboard');
  const panelShot = name => shot(name, { x: 150, y: 0, width: 340, height: 722 });
  const openRow = name => page.locator(`.ad [data-row="${name}"]`).evaluate(el => (el.matches('button') ? el : el.querySelector('button')).click());
  const title = () => page.locator('.ad .pg-head h3').textContent();
  const settle = () => page.waitForTimeout(700);
  const overlaps = {};
  await move(320, 14);
  await panel.locator('.ad').waitFor();
  await move(320, 200);
  await page.waitForTimeout(900);
  const homeHeight = (await panel.boundingBox()).height;
  const blocks = () => page.locator('.ad .home-inner > [data-block]').evaluateAll(els => els.map(e => e.dataset.block).join());
  check(`09 home: For you, the brief, Today, Mail, Agents, Now, Usage and Plugins | Projects in order, no conversation before you talk (${await blocks()})`,
    await blocks() === 'foryou,brief,today,mail,agents,now,usage,tiles');
  check('09 the panel grows with its blocks to 600 px, and the rest scrolls inside it',
    await page.locator('.ad .view').evaluate(e => Math.round(e.getBoundingClientRect().height)) === 600 && await page.locator('.ad .home-list').evaluate(e => e.scrollHeight > e.clientHeight + 40));
  check('09 Today has the weather, the next event and the to-dos, with no heads-up lines and no Duolingo',
    /°/.test(await page.locator('.ad .r-today .wx').textContent()) && await page.locator('.ad .r-today .ev').count() >= 1 && await page.locator('.ad .r-today .td').count() === 2
    && await page.locator('.ad .hu, .ad .duo').count() === 0);
  check('09 usage on the home page is four rings', await page.locator('.ad .r-usage .dial').count() === 4);
  await panelShot('09-home');
  for (const [name, heading, want] of [['conversation', 'Conversation', ['39', '39b', '39c']], ['now', 'Right now', '37'], ['agents', 'Agents', null], ['usage', 'Usage', null], ['plugins', 'Plugins', null], ['projects', 'Projects', '40']]) {
    await openRow(name);
    await settle();
    const faceNow = await page.locator('.companion-canvas').getAttribute('data-face');
    check(`09 ${name} opens in place at the same panel height${want ? ` and she wears ${[want].flat().join(' or ')}` : ''} (face ${faceNow})`,
      await title() === heading && Math.abs((await panel.boundingBox()).height - homeHeight) < 1 && (!want || [want].flat().includes(faceNow)));
    await panelShot(`09-${name}`);
    if (name === 'usage') check('09 usage shows how many limit resets Claude and Codex have left',
      (await page.locator('.ad .us-plan .meta').allTextContents()).filter(t => /resets? left/.test(t)).length === 2);
    if (name === 'projects') {
      await page.locator('.ad .pj').first().locator('.cols > span').last().hover();
      await page.waitForTimeout(250);
      const tip = page.locator('.ad .pj').first().locator('.cols > span:last-child .tip');
      check('09 hovering a project day shows its day and hours', await tip.textContent() === 'Today · 2.0 h' && await tip.evaluate(e => getComputedStyle(e).opacity === '1'));
      await panelShot('09-projects-hover');
    }
    // Going back, the page's words must be gone before any home row shows again: never text on text.
    overlaps[name] = await page.evaluate(() => new Promise(done => {
      const body = document.querySelector('.ad .pg-body'), home = document.querySelector('.ad .overview');
      const seen = el => Number(getComputedStyle(el).opacity);
      document.querySelector('.ad .pg-back').click();
      let worst = 0; const t0 = performance.now();
      const tick = () => {
        const words = body.isConnected ? seen(body) : 0, rows = seen(home) * Math.max(...[...home.querySelectorAll(':scope > .corner, .home-inner > *')].map(seen));
        worst = Math.max(worst, Math.min(words, rows));
        if (performance.now() - t0 < 500) requestAnimationFrame(tick); else done(Math.round(worst * 100) / 100);
      };
      requestAnimationFrame(tick);
    }));
    await page.waitForFunction(() => !document.querySelector('.ad .page'));
  }
  check('09 ‹ goes back home', await page.locator('.ad .overview').evaluate(e => !e.inert));
  check(`09 going back, the page's words leave before the home rows return (overlap ${JSON.stringify(overlaps)})`, Object.values(overlaps).every(v => v < .15));

  await openRow('agents');
  await settle();
  check('09 Agents groups Claude and Codex sessions into Needs you, Working and Earlier today',
    (await page.locator('.ad .pg-sec h4').allTextContents()).join('|') === 'Needs you|Working · 3|Earlier today · 3' && await page.locator('.ad .ag .tagc.claude').count() === 4);
  const detailHeights = () => page.locator('.ad .ag-more').evaluateAll(els => els.map(e => Math.round(e.getBoundingClientRect().height)));
  check('09 every card starts folded to its name, state and tags (agent, project, terminal, subagent)',
    (await detailHeights()).every(h => h === 0) && (await page.locator('.ad .ag[data-id="review"] .tagc').allTextContents()).join('|') === 'Claude|jarvis|Ghostty|Subagent');
  await page.locator('.ad .ag[data-id="inner"] .ag-head').click();
  await page.waitForTimeout(400);
  await page.locator('.ad .ag.is-wait .ag-head').click();
  await page.waitForTimeout(400);
  check('09 a click opens one card and folds the one before', (await detailHeights()).filter(h => h > 20).length === 1
    && await page.locator('.ad .ag.is-wait.is-open .ag-last').textContent() === 'Wants to run npm run build');
  await panelShot('09-agents-open');
  await page.locator('.ad .ag.is-wait').getByRole('button', { name: 'Approve' }).click();
  check('09 approving moves the session to the top of Working and she is pleased',
    await face('33') === '33' && await page.locator('.ad .ag.is-work .ag-title').first().textContent() === 'Adjust the usage page');
  await page.locator('.ad .ag.is-done').first().hover();
  await page.locator('.ad .ag.is-done .ag-x').first().click();
  await page.waitForTimeout(200);
  check('09 × hides a session and offers undo', await page.locator('.ad .ag.is-done').count() === 2 && await page.locator('.ad .toast.is-on button').count() === 1);
  await page.locator('.ad .toast button').click();
  check('09 undo brings it back', await page.locator('.ad .ag.is-done').count() === 3);
  await page.locator('.ad .pg-back').click();
  await page.waitForFunction(() => !document.querySelector('.ad .page'));
  check('09 the home Agents row follows the page', (await page.locator('.ad .r-agents .pill').textContent()) === '4 working');

  await openRow('plugins');
  await settle();
  await page.locator('.ad [data-plugin="notion"]').click();
  await settle();
  check('09 a plugin opens its own page, titled with its name', await title() === 'Notion' && await page.locator('.ad .ask-card').count() === 1);
  await page.getByRole('button', { name: 'Sign in and continue' }).click();
  check('09 while sign-in waits she wears her loading face', await face('36') === '36');
  await page.locator('.ad .ask-card.is-ok').waitFor({ timeout: 4000 });
  check('09 once signed in, Notion is connected and Jarvis picks the task back up',
    await face('10') === '10' && (await page.locator('.ad .say').textContent()).startsWith('Signed in to Notion'));
  await panelShot('09-plugin-connected');
  await page.locator('.ad .pg-back').focus();
  await page.keyboard.press('Escape');
  await page.waitForTimeout(450);
  check('09 Esc in a plugin goes back to the plugin list', await title() === 'Plugins' && (await page.locator('.ad .pl-row[data-plugin="notion"] small').textContent()) === 'Connected');
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.ad .page'));
  check('09 Esc again goes home', await page.locator('.ad .pl-mini .on').count() === 2);

  await page.locator('.ad .cmp-hit').hover();
  await page.waitForTimeout(500);
  await page.locator('.ad .cmp input').fill('Move the voice test to five');
  await page.keyboard.press('Enter');
  check('09 the bottom bar sends: her words turn to Thinking and she thinks', await face('30') === '30' && await page.locator('.ad .say').textContent() === 'Thinking…');
  await page.waitForFunction(() => document.querySelector('.ad .say')?.textContent.startsWith('Got it'), null, { timeout: 3000 });
  check('09 then she answers with her speaking face', await face('39', '39b', '39c') === '39');
  await openRow('conversation');
  await settle();
  check('09 the new turn is in the conversation, which has its own text box', (await page.locator('.ad .tr-you p').last().textContent()) === 'Move the voice test to five' && await page.locator('.ad .pg-input input').count() === 1);
  await move(600, 560);
  await page.waitForFunction(() => !document.querySelector('.companion-dashboard.is-open'), null, { timeout: 3000 });
  check('09 closing the panel returns it to the home page', await page.locator('.ad .page').count() === 0);
  await waitPlace('home');

  // 11: the home you arrange. The corner beside her, muting Jarvis, Settings, the language, holding a block
  // to arrange the home, dragging, hiding, the pop-ups' ×, and putting it all back.
  await move(320, 14);
  await page.locator('.companion-dashboard.is-open').waitFor();
  await move(320, 200);
  await page.waitForTimeout(900);
  const corner = page.locator('.ad .corner .cb');
  check('11 beside her: the time, then Conversation, Mute and Settings', await corner.count() === 3 && /\d:\d\d/.test(await page.locator('.ad .clock').textContent()));
  check('11 the conversation you just had sits on top, your words over her answer',
    (await blocks()).startsWith('talk,') && (await page.locator('.ad .r-talk .you').textContent()).endsWith('Move the voice test to five'));
  await page.evaluate(() => { window.__cues = []; window.addEventListener('jarvis:feedback', e => window.__cues.push(e.detail.cue)); });
  await page.locator('.ad .corner [data-row="settings"]').click();
  await settle();
  const tile = name => page.locator('.ad .st-q', { hasText: name });
  check('11 the gear opens Settings: Mic, Voice, Hands-free, then ten categories', await title() === 'Settings' && await page.locator('.ad .st-q').count() === 3 && await page.locator('.ad .st-cat').count() === 10);
  await tile('Hands-free').click(); await page.waitForTimeout(250); await tile('Hands-free').click(); await page.waitForTimeout(400);
  const loud = await page.evaluate(() => window.__cues.splice(0));
  check(`11 unmuted, hands-free on and off plays her cues (${loud.join()})`, loud.join() === 'voice-enter,voice-exit');
  await page.locator('.ad .pg-back').click();
  await page.waitForFunction(() => !document.querySelector('.ad .page'));
  await corner.nth(1).click();
  check('11 the corner mute mutes Jarvis and says so', await page.locator('.ad .cb.is-muted[aria-pressed="true"]').count() === 1
    && (await page.locator('.ad .toast').textContent()).startsWith('Jarvis is muted'));
  await page.locator('.ad .corner [data-row="settings"]').click();
  await settle();
  check('11 Settings shows the same switch: Voice muted', (await tile('Voice').textContent()).includes('Muted'));
  await tile('Hands-free').click(); await page.waitForTimeout(250); await tile('Hands-free').click(); await page.waitForTimeout(400);
  const quiet = await page.evaluate(() => window.__cues.splice(0));
  check(`11 muted, the same switches make no sound (${quiet.join() || 'none'})`, quiet.length === 0);
  await tile('Voice').click(); await page.waitForTimeout(400);
  check('11 unmuting brings the sound back', (await page.evaluate(() => window.__cues.splice(0))).join() === 'speaker-on' && await page.locator('.ad .cb.is-muted').count() === 0);
  await page.locator('.ad [data-cat="general"]').click();
  await page.waitForTimeout(500);
  await page.locator('.ad .st-seg button', { hasText: '中文' }).first().click();
  check('11 the interface language switches the panel at once', await title() === '通用');
  await page.locator('.ad .pg-back').click(); await page.waitForTimeout(450);
  check('11 …Settings too', await title() === '设置' && (await page.locator('.ad .st-cat b').allTextContents()).includes('隐私与数据'));
  await page.locator('.ad .pg-back').click();
  await page.waitForFunction(() => !document.querySelector('.ad .page'));
  check('11 …and the home', await page.locator('.ad [data-block="today"] .label').textContent() === '今天' && await page.evaluate(() => window.__state.menu?.lang === 'zh'));
  await panelShot('11-home-zh');
  await page.locator('.ad .corner [data-row="settings"]').click(); await settle();
  await page.locator('.ad [data-cat="general"]').click(); await page.waitForTimeout(500);
  await page.locator('.ad .st-seg button', { hasText: 'English' }).first().click();
  await page.locator('.ad .pg-back').click(); await page.waitForTimeout(450);
  await page.locator('.ad .pg-back').click();
  await page.waitForFunction(() => !document.querySelector('.ad .page'));

  // Hold a block past the hold time: the home opens for arranging, and the release clicks nothing.
  const held10 = await page.locator('.ad [data-block="agents"]').boundingBox();
  await page.mouse.move(held10.x + 150, held10.y + 30);
  await page.mouse.down(); await page.waitForTimeout(750); await page.mouse.up();
  await settle();
  check('11 holding a block opens Arrange the home, and letting go opens nothing else', await title() === 'Arrange the home');
  const arranged = () => page.locator('.ad .ar-body .ar-list').first().locator('.ar-row').evaluateAll(els => els.map(e => e.dataset.block).join());
  await page.locator('.ad .ar-row[data-block="now"] .ar-b').click();
  check('11 − hides a block that is always there, into Hidden', !(await arranged()).includes('now') && await page.locator('.ad .ar-row.is-off[data-block="now"] .ar-b.is-add').count() === 1);
  await page.locator('.ad .ar-row[data-block="mail"] .sw').click();
  check('11 a pop-up has a switch instead', await page.locator('.ad .ar-row[data-block="mail"] .sw').getAttribute('aria-checked') === 'false');
  await page.locator('.ad [data-grip="agents"]').focus();
  await page.keyboard.press('Alt+ArrowUp'); await page.keyboard.press('Alt+ArrowUp');
  check(`11 Alt + ↑ moves a block up (${await arranged()})`, await arranged() === 'talk,foryou,brief,agents,today,mail,usage,tiles');
  const grip = await page.locator('.ad [data-grip="tiles"]').boundingBox(), step = (await page.locator('.ad .ar-row[data-block="tiles"]').boundingBox()).height;
  await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
  await page.mouse.down();
  for (let i = 1; i <= 8; i++) { await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2 - i * step * 2 / 8); await page.waitForTimeout(20); }
  await panelShot('11-arrange-drag');
  await page.mouse.up(); await page.waitForTimeout(300);
  check(`11 dragging the dots moves a block two rows up (${await arranged()})`, await arranged() === 'talk,foryou,brief,agents,today,tiles,mail,usage');
  await panelShot('11-arrange');
  await page.locator('.ad .pg-back').click();
  await page.waitForFunction(() => !document.querySelector('.ad .page'));
  check(`11 the home follows: hidden and switched-off blocks gone, the rest in the new order (${await blocks()})`, await blocks() === 'talk,foryou,brief,agents,today,tiles,usage');

  // A pop-up closes with ×; undo brings it back; the always-there blocks have no ×.
  check('11 only pop-ups have a ×', await page.locator('.ad [data-block] > .mx').count() === 3 && await page.locator('.ad [data-block="today"] > .mx, .ad [data-block="agents"] > .mx').count() === 0);
  await page.locator('.ad [data-block="brief"]').hover();
  await page.locator('.ad [data-block="brief"] > .mx').click();
  await page.waitForTimeout(450);
  check('11 × closes the brief and offers undo', !(await blocks()).includes('brief') && (await page.locator('.ad .toast.is-on').textContent()).includes('Undo'));
  await page.locator('.ad .toast button').click(); await page.waitForTimeout(300);
  check('11 undo brings it back', (await blocks()).includes('brief'));
  const fits = await page.evaluate(() => { const v = document.querySelector('.ad .view').getBoundingClientRect().height, h = 30 + document.querySelector('.ad .home-inner').getBoundingClientRect().height;
    return [Math.round(v), Math.round(Math.min(600, Math.max(466, h)))]; });
  check(`11 the panel is as tall as its blocks, between 466 and 600 px (${fits.join(' = ')})`, fits[0] === fits[1]);
  await page.locator('.ad .corner [data-row="settings"]').click(); await settle();
  await page.locator('.ad [data-cat="home"]').click(); await page.waitForTimeout(500);
  await page.locator('.ad .st[data-item="reset"] button').click(); await page.waitForTimeout(300);
  await page.locator('.ad .pg-back').click(); await page.waitForTimeout(450);
  await page.locator('.ad .pg-back').click();
  await page.waitForFunction(() => !document.querySelector('.ad .page'));
  check(`11 Settings › Home › Reset puts the home back (${await blocks()})`, await blocks() === 'talk,foryou,brief,today,mail,agents,now,usage,tiles');
  await move(600, 560);
  await page.waitForFunction(() => !document.querySelector('.companion-dashboard.is-open'), null, { timeout: 3000 });
  await waitPlace('home');

  // 08: skins. She starts in deep-space glass; holding her past a poke changes her into the next skin,
  // the tray picks any skin, and on her own she comes out of the island, changes, and goes home.
  const skinOn = () => page.evaluate(() => window.__state.menu?.skins.find(s => s.on)?.key);
  const wearing = () => page.locator('.companion-canvas').getAttribute('data-skin');
  const wearsSoon = key => page.waitForFunction(k => document.querySelector('.companion-canvas')?.dataset.skin === k, key, { timeout: 4000 });
  check('08 she starts in deep-space glass', await skinOn() === 'glass' && await wearing() === 'glass');
  await move(out.x, out.y);
  await waitPlace('out');
  // Her glass body is opaque at the centre-left of the ball, where the eyes are not.
  const body = await page.evaluate(({ x, y }) => { const c = document.querySelector('.companion-canvas'), k = c.width / c.clientWidth;
    return c.getContext('2d').getImageData(Math.round((x - 16) * k), Math.round(y * k), 1, 1).data[3]; }, out);
  check('08 her glass body renders', body > 200);
  const face0 = await page.locator('.companion-canvas').getAttribute('data-face');
  check(`08 out and idle she wears the idle face (saw ${face0})`, face0 === '02');
  await shot('08-start');
  const held = await hit.boundingBox();
  await page.mouse.move(held.x + held.width / 2, held.y + held.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(800);
  await shot('08-charged');
  await page.mouse.up();
  await page.waitForTimeout(520);
  await shot('08-flash');
  await wearsSoon('nebula');
  check('08 holding her changes her into the next skin instead of starting voice', await page.locator('.companion-strip.is-open').count() === 0 && await skinOn() === 'nebula');
  await page.waitForTimeout(1400);
  await shot('08-nebula');
  for (const key of ['galaxy', 'frost', 'glass', 'aurora']) {
    await page.evaluate(k => window.__command(`skin:${k}`), key);
    await wearsSoon(key);
    await page.waitForTimeout(1400);
    await shot(`08-${key}`);
  }
  check('08 the tray picks any skin and she wears it', await skinOn() === 'aurora');
  await move(600, 560);
  await waitPlace('home');
  await page.evaluate(() => window.__command('outing'));
  await waitPlace('out');
  await page.waitForFunction(() => document.querySelector('.companion-canvas')?.dataset.skin !== 'aurora', null, { timeout: 4000 });
  await shot('08-own-change');
  await waitPlace('home');
  check('08 on her own she comes out, changes skin and goes home, keeping your pick', await skinOn() === 'aurora');
  await page.evaluate(() => window.__command('outing'));
  await wearsSoon('aurora');
  await waitPlace('home');
  check('08 her next change on her own returns to your pick', true);
  await page.evaluate(() => window.__command('expr:30'));
  await waitPlace('out');
  await page.waitForTimeout(1200);
  await shot('08-thinking');
  await waitPlace('home');
  check('08 the tray plays an expression out of the island and she goes home', true);

  // 10: the agent marks. A black wing out of the notch's right edge carries one mark per live session, in the
  // look picked in the tray (星芒 or 像素); resting on it lists them, and a click opens Agents in the same marks.
  const wingEl = page.locator('.agent-wing');
  const wingAlpha = (x, y) => page.evaluate(([x, y]) => { const c = document.querySelector('.agent-wing'), r = c.getBoundingClientRect(), k = c.width / r.width;
    return c.getContext('2d').getImageData(Math.round((x - r.left) * k), Math.round((y - r.top) * k), 1, 1).data[3]; }, [x, y]);
  const looks = sel => page.locator(sel).evaluateAll(els => [...new Set(els.map(e => e.dataset.look))].join());
  check('10 a black wing right of the notch carries one star per live session (09 approved the one that waited)',
    await wingEl.getAttribute('data-look') === 'spark' && await wingEl.getAttribute('data-marks') === 'work work work work' && await wingAlpha(416, 30) > 200 && await wingAlpha(500, 16) === 0);
  await move(440, 14);
  await page.waitForTimeout(500);
  check('10 resting on the marks lists the live sessions under them', await page.locator('.agent-wing-tip.is-open .wt-row').count() === 4
    && (await page.locator('.agent-wing-tip .wt-row b').first().textContent()) === 'Adjust the usage page');
  await shot('10-wing-spark', { x: 320, y: 0, width: 320, height: 220 });
  await page.evaluate(() => window.__command('marks:pixel'));
  await page.waitForTimeout(500);
  check('10 the tray turns every mark into pixels', await wingEl.getAttribute('data-look') === 'pixel' && await looks('.agent-wing-tip canvas, .ad .r-agents canvas') === 'pixel'
    && await page.evaluate(() => window.__state.menu?.marks === 'pixel'));
  await shot('10-wing-pixel', { x: 320, y: 0, width: 320, height: 220 });
  await page.locator('.agent-wing-hit').click();
  await page.locator('.companion-dashboard.is-open').waitFor();
  await page.waitForTimeout(900);
  check('10 a click on the marks opens Agents, whose rows wear the same marks',
    await title() === 'Agents' && await looks('.ad .ag canvas') === 'pixel' && (await page.locator('.ad .ag canvas').evaluateAll(els => els.map(e => e.dataset.state))).join() === 'work,work,work,work,seen,seen,seen');
  await panelShot('10-agents-pixel');
  await move(600, 560);
  await page.waitForTimeout(600);
  check('10 opened from the marks, the Dashboard stays when the cursor leaves', await page.locator('.companion-dashboard.is-open').count() === 1);
  await page.evaluate(() => window.__command('marks:spark'));
  await hit.dblclick({ force: true });
  await page.waitForFunction(() => !document.querySelector('.companion-dashboard.is-open'), null, { timeout: 3000 });
  await waitPlace('home');

  // 12: ⌘ in the menu bar row tucks the side of the camera the cursor is on, so what sits under it can be clicked.
  const islandBottom = () => page.locator('.companion-island').evaluate(e => e.getBoundingClientRect().bottom);
  const starBottom = async () => { const b = await hit.boundingBox(); return b.y + b.height; };
  await move(lobe.x, out.y);
  await waitPlace('out');
  await page.evaluate(() => window.__tuck({ left: true, right: false }));
  await page.waitForTimeout(120);
  const along = await page.evaluate(() => ['.companion-island', '.companion-canvas'].map(s => getComputedStyle(document.querySelector(s)).transform));
  check(`12 out under the notch, she goes up with the island in one motion, nothing of her trailing (${along.join(' | ')})`, along[0] !== 'none' && along[0] === along[1]);
  await page.waitForTimeout(1380);
  check('12 left of the camera, the island and she slide up off the screen; the marks stay',
    await islandBottom() <= 0 && await starBottom() <= 0 && await alphaAt(lobe.x, 24) === 0 && await wingAlpha(416, 30) > 200);
  await shot('12-tucked-left');
  await move(170, 10);
  await page.waitForTimeout(80);
  check('12 the menu bar under the tucked island gets its clicks', await page.evaluate(() => window.__state.passthrough === true));
  await move(lobe.x - 20, 14);
  await page.waitForTimeout(900);
  check('12 tucked, the cursor where the island was does not bring her out', await starBottom() <= 0);
  await move(lobe.x, out.y);
  await page.waitForTimeout(900);
  check('12 tucked, the cursor where she comes out does not bring her down either', await starBottom() <= 0);
  await move(600, 560);
  await page.evaluate(() => window.__tuck({ left: false, right: true }));
  await page.waitForTimeout(1500);
  check('12 right of the camera, the marks go under the notch and she is back in the island',
    await wingAlpha(416, 30) === 0 && await islandBottom() > 0 && await alphaAt(lobe.x, 24) > 200);
  await move(440, 14);
  await page.waitForTimeout(500);
  check('12 the menu bar under the tucked marks gets its clicks, and no list opens',
    await page.evaluate(() => window.__state.passthrough === true) && await page.locator('.agent-wing-tip.is-open').count() === 0);
  await move(600, 560);
  await page.evaluate(() => window.__tuck({ left: false, right: false }));
  await page.waitForTimeout(1500);
  check('12 untucked, the marks come back', await wingAlpha(416, 30) > 200);

  // 07: the cursor rests on an external screen with no notch. She sinks into this island,
  // Electron moves the window only after display-ready, and she comes up dead centre there.
  const centre = async () => { const b = await hit.boundingBox(); return b.x + b.width / 2; };
  await page.evaluate(() => window.__leave());
  await page.waitForFunction(() => window.__state.ready === 1, null, { timeout: 2000 });
  check('07 leaving a screen waits for her to go home before the window moves', await place() === 'home');
  await page.evaluate(() => { document.body.classList.add('external'); window.__placement({ docked: false, topInset: 32, notchWidth: 0, surfaceWidth: 640, compactWidth: 0, displayId: 2 }); });
  await page.waitForTimeout(900);
  check('07 on a screen without a notch she lives dead centre', await place() === 'home' && Math.abs(await centre() - 320) < 2);
  await shot('07-external-home', { x: 120, y: 0, width: 400, height: 210 });
  await move(262, 10);
  await page.waitForTimeout(80);
  check('07 the pill takes clicks too', await page.evaluate(() => window.__state.passthrough === false));
  await move(320, 14);
  await waitPlace('peek');
  check('07 the pill centre makes her peek', true);
  await move(320, out.y);
  await waitPlace('out');
  check('07 she comes out straight below the pill', Math.abs(await centre() - 320) < 2);
  await shot('07-external-out', { x: 120, y: 0, width: 400, height: 210 });
  await move(600, 560);
  await waitPlace('home');
  await move(268, 14);
  await page.locator('.companion-dashboard.is-open').waitFor();
  await waitPlace('dock');
  check('07 a wing of the pill opens the Dashboard', true);
  await shot('07-external-dock', { x: 120, y: 0, width: 400, height: 300 });
  await move(600, 560);
  await waitPlace('home');
  await page.evaluate(() => window.__tuck({ left: true, right: true }));
  await page.waitForTimeout(120);
  const rides = await page.evaluate(() => ['.companion-island', '.companion-canvas', '.agent-wing'].map(s => getComputedStyle(document.querySelector(s)).transform));
  check(`12 without a notch the pill, she and the marks go up as one piece (${rides.join(' | ')})`, rides[0] !== 'none' && rides.every(t => t === rides[0]));
  await page.waitForTimeout(1400);
  check('12 without a notch the whole pill goes, its marks too', await islandBottom() <= 0 && await starBottom() <= 0 && await wingAlpha(400, 16) === 0);
  await shot('12-external-tucked', { x: 120, y: 0, width: 400, height: 210 });
  await move(268, 14);
  await page.waitForTimeout(600);
  check('12 the menu bar under the tucked pill gets its clicks and opens no Dashboard',
    await page.evaluate(() => window.__state.passthrough === true) && await page.locator('.companion-dashboard.is-open').count() === 0);
  const gooBottom = () => page.locator('.companion-stage circle').evaluate(e => getComputedStyle(e).display === 'none' ? -1 : e.getBoundingClientRect().bottom);
  await move(320, out.y);
  await page.waitForTimeout(900);
  check('12 tucked without a notch, the cursor under the pill brings neither her nor a dark blob down', await starBottom() <= 0 && await gooBottom() <= 0);
  await move(600, 560);
  await page.evaluate(() => window.__tuck({ left: false, right: false }));
  await page.waitForTimeout(1500);
  check('12 untucked, the pill comes back', await islandBottom() > 0 && await starBottom() > 0);
  await page.reload();
  await page.waitForTimeout(800);
  check('08 her skin survives a restart', await skinOn() === 'aurora' && await wearing() === 'aurora');
  check('no page errors', errors.length === 0);
  await context.close();
  writeFileSync(path.join(dir, 'verification.json'), JSON.stringify({ checks, errors }, null, 2));
  console.log(`${checks.length} checks passed; evidence in ${dir}`);
} finally {
  await browser.close();
  server.kill();
}
