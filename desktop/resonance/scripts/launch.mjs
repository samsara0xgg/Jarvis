// Residency entry for the com.allen.jarvis.resonance LaunchAgent (ADR-0015): install when the lockfile is newer
// than node_modules, rebuild when any source is newer than the build, then run Electron and stay attached so
// launchd owns its lifetime and respawns it. Runs under launchd's bare PATH, so node, npm and electron are
// resolved from this install, never from PATH.
import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
process.chdir(path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'));
const mtime = f => existsSync(f) ? statSync(f).mtimeMs : 0;
const newest = entries => Math.max(0, ...entries.flatMap(e => statSync(e, { throwIfNoEntry: false })?.isDirectory()
  ? readdirSync(e, { recursive: true }).map(f => mtime(path.join(e, f))) : [mtime(e)]));
const npm = path.join(path.dirname(process.execPath), 'npm');
// npm is a shell script that execs `node`, so put this node's bin dir first regardless of the caller's PATH.
const env = { ...process.env, PATH: `${path.dirname(process.execPath)}:${process.env.PATH ?? ''}` };
const run = args => execFileSync(existsSync(npm) ? npm : 'npm', args, { stdio: 'inherit', env });
if (mtime('package-lock.json') > mtime('node_modules/.package-lock.json')) {
  console.log('resonance: lockfile newer than node_modules, installing');
  run(['ci']);
}
const built = ['dist/index.html', 'dist-electron/main.js', 'dist-native/material.node'];
const sources = ['src', 'electron', 'native', 'public', 'index.html', 'package.json', 'vite.config.ts', 'tsconfig.json', 'tsconfig.electron.json'];
if (built.some(f => !existsSync(f)) || newest(sources) > Math.min(...built.map(mtime))) {
  console.log('resonance: sources newer than build, rebuilding');
  run(['run', 'build']);
}
const { default: electron } = await import('electron');
execFileSync(electron, ['.'], { stdio: 'inherit', env });
