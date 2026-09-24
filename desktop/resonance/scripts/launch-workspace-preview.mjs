import { cpSync, existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { execFileSync, spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
let executable = path.join(root, 'node_modules', '.bin', 'electron');
let args = [root, '--workspace-preview'];
if (process.platform === 'darwin') {
  // A distinct bundle lets macOS distinguish the preview from the live capsule.
  const destination = path.join(root, 'build', 'Resonance Workspace Preview.app');
  if (!existsSync(destination)) {
    cpSync(path.join(root, 'node_modules/electron/dist/Electron.app'), destination, { recursive: true, verbatimSymlinks: true });
  }
  const target = path.join(destination, 'Contents/Resources/app');
  mkdirSync(target, { recursive: true });
  writeFileSync(path.join(target, 'package.json'), JSON.stringify({ name: 'resonance-workspace-preview', version: '0.1.0', type: 'module', main: 'main.js' }));
  writeFileSync(path.join(target, 'main.js'), `process.argv.push('--workspace-preview');\nawait import(${JSON.stringify(pathToFileURL(path.join(root, 'dist-electron/main.js')).href)});\n`);
  const plist = path.join(destination, 'Contents/Info.plist');
  for (const [key, value] of Object.entries({ CFBundleIdentifier: 'dev.jarvis.resonance.workspace-preview', CFBundleName: 'Resonance Workspace Preview', CFBundleDisplayName: 'Resonance Workspace Preview' })) {
    execFileSync('/usr/libexec/PlistBuddy', ['-c', `Set :${key} ${value}`, plist]);
  }
  execFileSync('codesign', ['--force', '--deep', '--sign', '-', destination], { stdio: 'inherit' });
  executable = path.join(destination, 'Contents/MacOS/Electron');
  args = [];
}
const child = spawn(executable, args, { cwd: root, stdio: 'inherit' });
child.on('error', error => { console.error(error.message); process.exitCode = 1; });
child.on('exit', code => { process.exitCode = code ?? 1; });
