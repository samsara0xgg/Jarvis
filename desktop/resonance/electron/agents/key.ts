// The agent host's own key (ADR 0095): every request to the host carries it, so the Agents window needs nothing from the
// daemon to connect. Whichever side needs it first (the host starting, or the companion asking) makes it: 32 random
// bytes in <agents dir>/host-key, readable only by this account. Imported by the companion's main process too, so it
// stays free of anything but Node.
import { randomBytes } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

export const agentsDir = () => process.env.JARVIS_AGENTS_DIR ?? path.join(process.env.JARVIS_RUNTIME_ROOT ?? path.join(homedir(), '.jarvis'), 'agents');
const KEY = /^[0-9a-f]{64}$/;
export function hostKey(dir = agentsDir()) {
  const file = path.join(dir, 'host-key');
  const read = () => { try { return readFileSync(file, 'utf8').trim(); } catch { return ''; } };
  const had = read();
  if (KEY.test(had)) return had;
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const key = randomBytes(32).toString('hex');
  // Two sides starting at once: the one that loses reads what the other wrote. A file that is not a key is replaced.
  try { writeFileSync(file, key, { mode: 0o600, flag: existsSync(file) ? 'w' : 'wx' }); return key; }
  catch (e) { if ((e as NodeJS.ErrnoException).code !== 'EEXIST') throw e; }
  const won = read();
  if (!KEY.test(won)) throw new Error(`${file} is not a key`);
  return won;
}
