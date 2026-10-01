// Run after npm run build. Startrail in both languages on the stage (scripts/agents-stage.mjs): with the stand-in daemon
// answering 'en' on /inherent/language, the page and the host speak English and no Chinese is left in the Long Exposure
// (its words and its canvas), a conversation with steps and a question to answer, the settings pages and the preview
// sheet; with the stand-in saying nothing, the same page is Chinese. What the stand-in agents write (好的，做完了：…) is
// theirs and the owner's, and never translated. SHOTS=<folder> keeps a screenshot of each state.
import assert from 'node:assert/strict';
import { stage } from './agents-stage.mjs';

const CJK = /[　-〿㐀-鿿＀-￯]/;
// What the stand-in agents say themselves.
const THEIRS = /好的，做完了：|第二段在这里|侧答：|。/g;
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const viewport = { width: 1290, height: 890 };

// The sky's canvas text, kept as it is drawn; the DOM's text and attributes (labels, tips, placeholders) as the page holds them.
const spy = () => {
  const draw = []; window.__texts = draw;
  const fill = CanvasRenderingContext2D.prototype.fillText;
  CanvasRenderingContext2D.prototype.fillText = function (t, ...a) { draw.push(String(t)); return fill.call(this, t, ...a); };
};
const words = page => page.evaluate(() => {
  const root = document.querySelector('#win'), out = [root.innerText];
  for (const el of root.querySelectorAll('[aria-label],[data-tip],[placeholder],[title],[alt]')) for (const a of ['aria-label', 'data-tip', 'placeholder', 'title', 'alt']) if (el.hasAttribute(a)) out.push(el.getAttribute(a));
  return [...out, ...window.__texts].join('\n');
});
const chinese = async page => (await words(page)).replace(THEIRS, '').split('\n').filter(l => CJK.test(l));
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---------- English ----------
let st = await stage({ viewport, language: 'en' });
try {
  await st.context.addInitScript(spy);
  const p = st.page;
  const id = await st.session('hello PLAN');
  await st.send(id, 'EDIT notes.txt');
  await st.send(id, 'SUB README.md');
  const ask = await st.session('ASK run it');
  await st.send(id, 'see `README.md`');
  await st.open(id);

  check('the page is English: its language, the static words of agents.html, and the host\'s words in the header',
    await p.evaluate(() => document.documentElement.lang) === 'en' && await p.locator('.new').innerText() === 'New session' && /Archived/.test(await p.locator('.arch-link').innerText())
    && await p.locator('#find').getAttribute('placeholder') === 'Search' && /Open in Terminal/.test(await p.locator('.h-btn[data-act="terminal"]').textContent()));
  check('the steps and the state are English, and the answers stay as the agent wrote them', /Edited 1 file/.test(await p.locator('.conv').first().innerText()) && /好的，做完了/.test(await p.locator('.conv').first().innerText()));
  check('the conversation, its composer and the side list have no Chinese', (await chinese(p)).length === 0, await chinese(p));
  await st.shot('en-conversation');

  // The preview sheet: a file the answer names.
  await p.locator('.chat code.ref[data-ref="README.md"]').last().click();
  await p.waitForFunction(() => !document.querySelector('.pv').classList.contains('off') && document.querySelector('.pv-view')?.innerText.trim());
  await sleep(500);
  check('the preview sheet has no Chinese', (await chinese(p)).length === 0, await chinese(p));
  await p.locator('.pv [data-act="pvclose"]').click();
  await p.waitForFunction(() => document.querySelector('.pv').classList.contains('off'));

  // A question to answer: the card and its buttons.
  await st.open(ask);
  check('an approval card is English', /Allow/.test(await p.locator('.req').last().innerText()) && (await chinese(p)).length === 0, await chinese(p));

  // The Long Exposure, the sessions beside the one that is open.
  await p.click('.bw-pull');
  await p.waitForFunction(() => document.querySelector('#win').classList.contains('sky-on'));
  await sleep(1400);
  check('the Long Exposure says its words in English: waiting, the horizon, the names', /Waiting/.test(await words(p)) && (await p.evaluate(() => window.__texts)).some(t => /ago|Now|min/.test(t)));
  check('the Long Exposure, its canvas and its rows have no Chinese', (await chinese(p)).length === 0, await chinese(p));
  await st.shot('en-long-exposure');
  await p.keyboard.press('Escape');
  await sleep(500);

  // Settings: every page.
  await st.open(id);
  await p.keyboard.press('Control+,');
  await p.waitForSelector('.fr-set:not([hidden])');
  const pages = await p.locator('.fr-nav .fr-ni').count();
  check('the settings sheet lists its pages', pages >= 3, pages);
  for (let i = 0; i < pages; i++) {
    await p.locator('.fr-nav .fr-ni').nth(i).click();
    await sleep(500);
    const here = (await p.locator('.fr-ph h3').innerText().catch(() => '')) || `page ${i}`;
    check(`settings page "${here}" has no Chinese`, (await chinese(p)).length === 0, await chinese(p));
    if (i === 0) await st.shot('en-settings');
  }
  await p.keyboard.press('Escape');

  // The host's own words: the exported conversation, a refusal.
  const exported = await st.call(`/sessions/${id}/export`);
  check('the host writes the export and its refusals in English', /^## You/m.test(exported.text ?? '') && !/^## 你/m.test(exported.text ?? '')
    && /No such session/.test((await st.call('/sessions/nope/export')).error ?? ''), exported);
  check('no errors on the page', !st.errors.length, st.errors);
} catch (e) {
  console.error(e, '\n--- host ---\n', st.log().slice(-3000));
  process.exitCode = 1;
} finally { await st.close(); }

// ---------- and Chinese, as before, when the daemon says nothing ----------
if (!process.exitCode) {
  st = await stage({ viewport });
  try {
    const p = st.page;
    const id = await st.session('hello PLAN');
    await st.send(id, 'EDIT notes.txt');
    await st.open(id);
    check('without a language from the daemon the page is Chinese', await p.evaluate(() => document.documentElement.lang) === 'zh-CN' && await p.locator('.new').innerText() === '新会话'
      && /在终端打开/.test(await p.locator('.h-btn[data-act="terminal"]').textContent()) && /改了 1 个/.test(await p.locator('.conv').first().innerText()));
    await st.shot('zh-conversation');
    check('no errors on the page', !st.errors.length, st.errors);
  } catch (e) {
    console.error(e, '\n--- host ---\n', st.log().slice(-3000));
    process.exitCode = 1;
  } finally { await st.close(); }
}
console.log(`\n${checks.length} checks passed`);
