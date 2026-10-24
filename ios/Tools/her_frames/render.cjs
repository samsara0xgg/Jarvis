// Her's face on the watch, rendered from the Mac's own drawing code (desktop/resonance/src/starCore.ts + agents/exposure/her.ts)
// into transparent PNG loops. Rebuild whenever the Mac's her changes:
//
//   node ios/Tools/her_frames/render.cjs
//
// Needs `npm ci` in desktop/resonance (rolldown bundles the Mac code, playwright drives Chrome) and pngquant (brew). Writes
//   ios/JarvisWatch/Her.xcassets/her-<face>-<NN>.dataset       the loops, as data (2 px/pt; ball = BALL pt wide, image = FRAME pt, ball centred; 256-colour)
//   ios/JarvisWatch/Her/HerFrames.swift                        frame counts, fps and pixel sizes (generated)
//   ios/JarvisWatchWidgets/Assets.xcassets/her-comp*.imageset  complication stills
// Faces use the Mac's names: rest, 35 (listen), 30 (think), ask, 39 (talk), doze. Output is repeatable (seeded blinks, virtual time).
const fs = require('node:fs'), path = require('node:path'), { execFileSync } = require('node:child_process'), { createRequire } = require('node:module'), { pathToFileURL } = require('node:url');
const root = path.resolve(__dirname, '../../..'), res = path.join(root, 'desktop/resonance'), req = createRequire(path.join(res, 'package.json'));
const watch = path.join(root, 'ios/JarvisWatch'), widgets = path.join(root, 'ios/JarvisWatchWidgets');

// her biggest on the watch: the home sprite, 236 of the 208 pt frame, is a ~170 pt ball; every smaller size scales these down
const BALL = 170, FRAME = 272, PPP = 2;  // pt; her radius R = BALL/2 css px at dpr 2 (Her caps dpr at 2)
const FACES = [                          // [swift case, Mac face, look, fps, frames, blink after ms (null: none)]; frames ~ the face's own period:
  ['rest', 'rest', [0, .15], 8, 24, 1100],  // one blink per 3 s loop (the Mac blinks every 3-6 s)
  ['listen', '35', [0, 0], 30, 96, 1100],    // head sway 3.2 s; 30 fps while he talks to her
  ['think', '30', [0, 0], 8, 24, 1100],
  ['ask', 'ask', [0, .15], 8, 26, 1100],    // head sway 3.2 s
  ['talk', '39', [0, .3], 30, 96, 1100],     // 3.2 s: of the lengths 2-4.3 s, the one whose end best meets its start; 30 fps while she talks
  ['doze', 'doze', [0, .2], 8, 40, null],   // breath 5 s
];
// the loops are quantized to 256 colours (pngquant, repeatable) and kept as data assets, PNG as written: HerSprite decodes a frame
// when it is drawn, where an image set would be kept decoded by the system, every frame of a face. ~16 MB in the app, against
// ~26 MB as image sets compiled lossy (actool's jpeg-lzfse, the same for a quantized PNG) and ~33 MB lossless in full colour
const COMP = { R: 14, W: 56 };           // circular complication: a 28 pt ball

const imageset = (dir, name, png) => {
  const d = path.join(dir, `${name}.imageset`); fs.mkdirSync(d, { recursive: true });
  fs.writeFileSync(path.join(d, `${name}.png`), png);
  fs.writeFileSync(path.join(d, 'Contents.json'), JSON.stringify({ images: [{ filename: `${name}.png`, idiom: 'universal', scale: '2x' }], info: { author: 'xcode', version: 1 } }, null, 2) + '\n');
};
const dataset = (dir, name, png) => {
  const d = path.join(dir, `${name}.dataset`); fs.mkdirSync(d, { recursive: true });
  fs.writeFileSync(path.join(d, `${name}.png`), png);
  fs.writeFileSync(path.join(d, 'Contents.json'), JSON.stringify({ data: [{ filename: `${name}.png`, idiom: 'universal', 'universal-type-identifier': 'public.png' }], info: { author: 'xcode', version: 1 } }, null, 2) + '\n');
};
const catalog = dir => { fs.mkdirSync(dir, { recursive: true }); fs.writeFileSync(path.join(dir, 'Contents.json'), '{\n  "info" : { "author" : "xcode", "version" : 1 }\n}\n'); };
const png = url => Buffer.from(url.split(',')[1], 'base64');

// For the periodic loops (page.html renderPeriodic): every clock-driven motion in the core, `t * <rate>` (sin and cos of it, or `(t * r) % m`),
// gets its rate rounded to whole cycles of the loop while window.__herLoop holds the loop's length; unset, the rate is the core's own.
// A `% m` term's cycle is m, a sine's 2π; terms that share a rate on one line (the reading gaze's `% 1` and `% 3`) take the longer cycle.
// Event-driven motion (hops, nods, wince, bounce) is not clocked this way; renderPeriodic keeps those events from firing.
const RATES = 26;                        // the clocked terms in starCore.ts today: a change there should be looked at, not rounded blind
const loopable = {
  name: 'loopable',
  transform(code, id) {
    if (!id.endsWith('/starCore.ts')) return null;
    let n = 0;
    const lines = code.split('\n').map(line => {
      const re = /\b(st\.t|t) \* (TAU \/ [\w.[\]]+|\.?\d[\d.]*)(?=(\)\s*%\s*(\d+))?)/g, cyc = {};
      for (const m of line.matchAll(re)) cyc[m[2]] = Math.max(cyc[m[2]] ?? 0, m[4] ? +m[4] : 0);
      return line.replace(re, (all, t, rate, _, mod) => { n++; return `${t} * __hq(${rate}, ${cyc[rate] ? cyc[rate] : 'TAU'}, ${JSON.stringify(rate)})`; });
    });
    if (n !== RATES) throw new Error(`loopable: ${n} clocked terms in starCore.ts, expected ${RATES}`);
    return `const __hq = (w, c, src) => { const T = globalThis.__herLoop; if (!T) return w; const q = Math.round(w * T / c) * c / T;
  (globalThis.__herLoopLog ??= {})[src + (c === Math.PI * 2 ? '' : ' %' + c)] = [w, q]; return q; };\n` + lines.join('\n');
  },
};

(async () => {
  const { rolldown } = await import(pathToFileURL(req.resolve('rolldown')).href);
  const inline = { name: 'inline', resolveId(s, imp) { if (s.endsWith('?inline')) return '\0inline:' + path.resolve(path.dirname(imp), s.slice(0, -7)); },
    load(id) { if (id.startsWith('\0inline:')) return `export default "data:image/jpeg;base64,${fs.readFileSync(id.slice(8)).toString('base64')}";`; } };
  const out = path.join(__dirname, 'out'); fs.mkdirSync(out, { recursive: true });
  await (await rolldown({ input: path.join(__dirname, 'entry.ts'), plugins: [inline, loopable], platform: 'browser', logLevel: 'warn' }))
    .write({ file: path.join(out, 'her-main.js'), format: 'iife', minify: true });

  const { chromium } = req('playwright');
  const exe = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'].find(fs.existsSync);
  const b = await chromium.launch({ executablePath: exe, args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const p = await b.newPage({ viewport: { width: 600, height: 600 }, deviceScaleFactor: PPP });
  p.on('pageerror', e => console.log('pageerror:', e.message));
  await p.goto('file://' + path.join(__dirname, 'page.html'));
  await p.waitForFunction(() => document.title === 'ready');

  const faces = path.join(watch, 'Her.xcassets'); fs.rmSync(faces, { recursive: true, force: true }); catalog(faces);
  const rows = [];
  for (const [name, face, look, fps, n, blinkMs] of FACES) {
    const urls = await p.evaluate(s => window.renderLoop(s), { face, R: BALL / 2, W: FRAME, out: true, look, n, fps, blinkMs });
    const names = urls.map((u, i) => `her-${name}-${String(i).padStart(2, '0')}`);
    names.forEach((s, i) => dataset(faces, s, png(urls[i])));
    execFileSync('pngquant', ['--force', '--ext', '.png', '--strip', '256', ...names.map(s => path.join(faces, `${s}.dataset`, `${s}.png`))]);
    rows.push(`        case .${name}: HerClip(frames: ${n}, fps: ${fps}, pixels: ${FRAME * PPP}, ball: ${BALL * PPP})`);
    console.log(name, urls.length);
  }
  fs.writeFileSync(path.join(watch, 'Her/HerFrames.swift'), `// Generated by ios/Tools/her_frames/render.cjs from the Mac's starCore + her.ts. Do not edit; rerun the script.
// Each face is a loop of \`frames\` square PNGs (data assets) named her-<face>-<NN>, \`pixels\` wide, with the ball \`ball\` pixels wide at the centre.
import CoreGraphics

struct HerClip {
    let frames: Int
    let fps: Double
    let pixels: Int
    let ball: Int
    /// How much larger than the ball the image is.
    var margin: CGFloat { CGFloat(pixels) / CGFloat(ball) }
}

extension HerFace {
    var clip: HerClip {
        switch self {
${rows.join('\n')}
        }
    }
}
`);

  const comps = path.join(widgets, 'Assets.xcassets'); fs.rmSync(comps, { recursive: true, force: true }); catalog(comps);
  for (const [name, o] of [['her-comp', false], ['her-comp-out', true]]) {
    const [u] = await p.evaluate(s => window.renderLoop(s), { face: 'rest', R: COMP.R, W: COMP.W, out: o, look: [0, .15], n: 1, fps: 32, blinkMs: null });
    imageset(comps, name, png(u));
  }
  await b.close();
})();
