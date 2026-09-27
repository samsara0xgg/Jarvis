// Builds build/Jarvis.app: the companion carrying its own Python, the locked runtime dependencies and
// the daemon's files as committed at HEAD, signed with Developer ID and the hardened runtime.
// `npm run package -- --release` also notarizes and staples it and makes build/Jarvis-<version>.dmg.
// Lessons from Typlus's docs/RELEASING.md: sign inside out, never --deep; find Mach-O files by their
// first bytes, not the execute bit; nothing may write into the signed bundle, so bytecode is built here.
import { cpSync, mkdirSync, writeFileSync, rmSync, readdirSync, lstatSync, renameSync, openSync, readSync, closeSync, realpathSync, symlinkSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
if (process.platform !== 'darwin' || process.arch !== 'arm64') throw new Error('Jarvis.app is built on an Apple Silicon Mac.');
const VERSION = '0.1.0', ID = 'com.alllllenshi.jarvis';
const IDENTITY = 'Developer ID Application: yilun shi (3MEBVQ3N3U)';
const NOTARY = 'Typlus'; // the notarytool keychain profile; same Apple team
const release = process.argv.includes('--release');
const run = (command, args, options = {}) => execFileSync(command, args, { stdio: 'inherit', ...options });
const repo = path.resolve('../..'), build = path.resolve('build'), app = path.join(build, 'Jarvis.app');
const contents = path.join(app, 'Contents'), resources = path.join(contents, 'Resources');

rmSync(app, { recursive: true, force: true });
cpSync('node_modules/electron/dist/Electron.app', app, { recursive: true, verbatimSymlinks: true });
rmSync(path.join(resources, 'default_app.asar'));
// Electron's and Chromium's licenses must travel with the app; the notices name the models too.
cpSync('node_modules/electron/dist/LICENSE', path.join(resources, 'LICENSE.electron'));
cpSync('node_modules/electron/dist/LICENSES.chromium.html', path.join(resources, 'LICENSES.chromium.html'));
cpSync('THIRD-PARTY-NOTICES.md', path.join(resources, 'THIRD-PARTY-NOTICES.md'));
renameSync(path.join(contents, 'MacOS/Electron'), path.join(contents, 'MacOS/Jarvis'));
const plist = path.join(contents, 'Info.plist'), set = (key, type, value) => run('plutil', ['-replace', key, `-${type}`, value, plist]);
for (const [key, value] of Object.entries({
  CFBundleExecutable: 'Jarvis', CFBundleIdentifier: ID, CFBundleName: 'Jarvis', CFBundleDisplayName: 'Jarvis',
  CFBundleShortVersionString: VERSION, CFBundleVersion: VERSION,
  NSMicrophoneUsageDescription: 'Jarvis listens for “Hey Jarvis” and to what you say to it.',
  NSAppleEventsUsageDescription: 'Jarvis works other apps for you when you ask, such as bringing a terminal session to the front.',
})) set(key, 'string', value);
set('LSUIElement', 'bool', 'YES');
// The same two prompts in Chinese; macOS picks by the user's language.
writeFileSync(path.join(resources, 'zh_CN.lproj/InfoPlist.strings'), Buffer.from('﻿'
  + '"NSMicrophoneUsageDescription" = "Jarvis 用麦克风听“Hey Jarvis”和你对它说的话。";\n'
  + '"NSAppleEventsUsageDescription" = "Jarvis 在你要求时操作其他 App，比如把某个终端会话切到前面。";\n', 'utf16le'));

const companion = path.join(resources, 'app');
for (const directory of ['dist', 'dist-electron', 'dist-native']) cpSync(directory, path.join(companion, directory), { recursive: true });
writeFileSync(path.join(companion, 'package.json'), JSON.stringify({ name: 'jarvis', productName: 'Jarvis', version: VERSION, type: 'module', main: 'dist-electron/companion.js' }));

// CPython from uv's standalone builds: relocatable, with its own libpython. Tcl/Tk and pip are not needed.
const python = path.join(resources, 'python'), py = path.join(python, 'bin/python3.12');
const found = realpathSync(execFileSync('uv', ['python', 'find', '--managed-python', '--system', '3.12'],{ encoding: 'utf8' }).trim());
cpSync(path.dirname(path.dirname(found)), python, { recursive: true, verbatimSymlinks: true });
for (const part of ['include', 'share', 'lib/python3.12/test', 'lib/python3.12/idlelib', 'lib/python3.12/tkinter', 'lib/python3.12/turtledemo', 'lib/python3.12/ensurepip', 'lib/python3.12/site-packages/pip'])
  rmSync(path.join(python, part), { recursive: true, force: true });
for (const dir of ['lib', 'lib/python3.12/lib-dynload'])
  for (const name of readdirSync(path.join(python, dir))) if (/^(tcl|tk|itcl|thread|libtcl|_tkinter)/.test(name)) rmSync(path.join(python, dir, name), { recursive: true });
const requirements = path.join(build, 'requirements.txt');
run('uv', ['export', '--frozen', '--no-dev', '--no-emit-project', '--quiet', '-o', requirements], { cwd: repo });
run('uv', ['pip', 'install', '--python', py, '--break-system-packages', '--no-deps', '--require-hashes', '--compile-bytecode', '--quiet', '-r', requirements]);

// The daemon's files: the package and what it finds beside it (config, prompt, plugins, prices).
const runtime = path.join(resources, 'runtime');
mkdirSync(runtime);
execFileSync('tar', ['-x', '-C', runtime], { input: execFileSync('git', ['-C', repo, 'archive', 'HEAD', 'jarvis', 'config', 'prompts', 'plugins', 'data/pricing.json'], { maxBuffer: 1 << 30 }) });
// Jarvis is closed source: only its bytecode ships. `-b` puts each .pyc where its .py was, the one
// place Python imports a module without its source. The stdlib ships compiled, uv compiled the rest.
run(py, ['-m', 'compileall', '-q', '-b', path.join(runtime, 'jarvis')]);
for (const file of readdirSync(path.join(runtime, 'jarvis'), { recursive: true })) if (file.endsWith('.py')) rmSync(path.join(runtime, 'jarvis', file));
// A C extension that cannot load only shows when imported; import the native ones now, outside any venv.
run(path.join(python, 'bin/python3'), ['-c', 'import jarvis.runtime, jarvis.surface.voice_tts, sherpa_onnx, pymicro_wakeword, onnxruntime, sounddevice, soxr, uvloop, lxml, cryptography'],
  { cwd: '/', env: { PATH: '/usr/bin:/bin', HOME: process.env.HOME, PYTHONPATH: runtime, PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1' } });

// V8 and CPython's ctypes need JIT and unsigned executable memory; the mic and Apple Events need their own.
const entitlements = path.join(build, 'entitlements.plist');
writeFileSync(entitlements, '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>\n'
  + ['cs.allow-jit', 'cs.allow-unsigned-executable-memory', 'cs.disable-library-validation', 'device.audio-input', 'automation.apple-events']
    .map(key => `<key>com.apple.security.${key}</key><true/>\n`).join('') + '</dict></plist>\n');
const MAGIC = new Set([0xfeedface, 0xfeedfacf, 0xcefaedfe, 0xcffaedfe, 0xcafebabe, 0xbebafeca]);
const machO = file => { const fd = openSync(file, 'r'), head = Buffer.alloc(4), n = readSync(fd, head, 0, 4, 0); closeSync(fd); return n === 4 && MAGIC.has(head.readUInt32BE(0)); };
const targets = [];
const walk = directory => {
  for (const name of readdirSync(directory)) {
    const file = path.join(directory, name), stat = lstatSync(file);
    if (stat.isSymbolicLink()) continue;
    if (stat.isDirectory()) { walk(file); if (/\.(app|framework)$/.test(name)) targets.push(file); } else if (machO(file)) targets.push(file);
  }
};
walk(contents);
// Deepest first, so every bundle is signed after what it holds; one codesign call per depth.
const depth = file => file.split(path.sep).length;
targets.sort((a, b) => depth(b) - depth(a));
const sign = files => execFileSync('codesign', ['--force', '--timestamp', '--options', 'runtime', '--entitlements', entitlements, '--sign', IDENTITY, ...files]);
for (let i = 0, j = 0; i < targets.length; i = j) {
  while (j < targets.length && j - i < 200 && depth(targets[j]) === depth(targets[i])) j++;
  sign(targets.slice(i, j));
}
sign([app]);
run('codesign', ['--verify', '--deep', '--strict', app]);
console.log(`signed ${targets.length + 1} binaries and bundles`);

if (release) {
  const notarize = (file, staple) => {
    const notary = args => JSON.parse(execFileSync('xcrun', ['notarytool', ...args, '--keychain-profile', NOTARY, '--output-format', 'json'], { encoding: 'utf8' }));
    const { id } = notary(['submit', file]);
    // Apple takes minutes; a connection dropped while waiting must not lose the submission.
    let answer;
    for (let tries = 1; !answer; tries++) {
      try { answer = notary(['wait', id]); } catch (error) { if (tries === 5) throw new Error(`notarization ${id}: ${error.message}`); }
    }
    if (answer.status !== 'Accepted') throw new Error(`notarization ${answer.status}; see: xcrun notarytool log ${id} --keychain-profile ${NOTARY}`);
    run('xcrun', ['stapler', 'staple', staple]);
  };
  const zip = path.join(build, 'Jarvis.zip'), stage = path.join(build, 'dmg'), dmg = path.join(build, `Jarvis-${VERSION}.dmg`);
  run('ditto', ['-c', '-k', '--keepParent', app, zip]);
  notarize(zip, app);
  rmSync(stage, { recursive: true, force: true });
  mkdirSync(stage);
  run('ditto', [app, path.join(stage, 'Jarvis.app')]);
  symlinkSync('/Applications', path.join(stage, 'Applications'));
  run('hdiutil', ['create', '-volname', 'Jarvis', '-srcfolder', stage, '-ov', '-format', 'UDZO', dmg]);
  run('codesign', ['--timestamp', '--sign', IDENTITY, dmg]);
  notarize(dmg, dmg);
  rmSync(zip);
  rmSync(stage, { recursive: true });
  console.log(dmg);
} else console.log(app);
