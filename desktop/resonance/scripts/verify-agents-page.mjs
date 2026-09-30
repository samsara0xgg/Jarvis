// Run after npm run build. Startrail's page itself, on the stage (scripts/agents-stage.mjs): a real host on stand-in
// agents, the built page in Chromium. SHOTS=<folder> keeps a screenshot of each state.
import assert from 'node:assert/strict';
import { stage } from './agents-stage.mjs';

const st = await stage();
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
try {
  const id = await st.session('hello PLAN');
  await st.open(id);
  const p = st.page;
  check('the page boots against the host and shows the session\'s answer', await p.locator('.conv .it').filter({ hasText: '好的，做完了：hello PLAN' }).count() > 0);
  check('the window was told which sessions it shows', (await p.evaluate(() => window.__agentsCalls.filter(c => c[0] === 'presence').length)) > 0);
  await st.shot('page-session');
  await st.send(id, 'EDIT notes.txt');
  await p.waitForTimeout(500);
  check('a later turn arrives over the event stream', await p.locator('.conv .it').filter({ hasText: 'EDIT notes.txt' }).count() > 0);
  await st.shot('page-edit');
  check('no errors on the page', !st.errors.length, st.errors);
  console.log(`\n${checks.length} checks passed`);
} catch (e) {
  console.error(e, '\n--- host ---\n', st.log().slice(-3000));
  process.exitCode = 1;
} finally { await st.close(); }
