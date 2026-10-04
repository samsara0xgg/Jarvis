// Every verify-*.mjs in turn, against the current build (`npm run verify` builds first). Skips the two that start a
// real Claude Code session (keeper makes paid Haiku calls). A script's output is shown only when it fails.
import { spawnSync } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';

// The agents stage needs a Chromium; without Playwright's own download, use this Mac's Chrome.
const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
if (!process.env.CHROMIUM && existsSync(chrome)) process.env.CHROMIUM = chrome;

const skip = new Set(['verify-all.mjs', 'verify-agents-keeper.mjs', 'verify-agents-clear.mjs']);
const scripts = readdirSync('scripts').filter(name => /^verify-.*\.mjs$/.test(name) && !skip.has(name)).sort();
const failed = [];
for (const name of scripts) {
  const run = spawnSync(process.execPath, [`scripts/${name}`], { encoding: 'utf8', timeout: 300_000 });
  const ok = run.status === 0;
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`);
  if (!ok) { failed.push(name); console.log(`${run.stdout}${run.stderr}`.trim().split('\n').slice(-15).join('\n')); }
}
console.log(`\n${scripts.length - failed.length}/${scripts.length} passed${failed.length ? `; failed: ${failed.join(', ')}` : ''}`);
process.exit(failed.length ? 1 : 0);
