// Her faces on the watch as 60 fps video loops (HerVideo.swift plays them), rendered from the Mac's own drawing code:
//
//   node ios/Tools/her_frames/render.cjs   # first: it bundles out/her-main.js
//   node ios/Tools/her_frames/video.cjs [face ...]
//
// Needs ffmpeg with libx265 (brew). Each face is rendered on black (the watch is black and the video is opaque) as an exactly periodic loop
// (page.html renderPeriodic): its length fits the face's own motion, every clocked motion rounded to whole cycles in it, so frame N-1
// leads into frame 0 as any frame into the next. Encoded HEVC (hvc1, 8-bit 4:2:0, BT.709), one closed GOP a loop, into
// ios/JarvisWatch/HerVideo/her-<face>.mp4. The source PNGs stay in out/v-<face>/, with seam.png (frames N-2, N-1, 0, 1 side by side)
// and steps.json (how far each frame is from the one before, mean |Δ| 0-255; the seam is N-1 to 0).
const fs = require('node:fs'), path = require('node:path'), { execFileSync } = require('node:child_process'), { createRequire } = require('node:module');
const root = path.resolve(__dirname, '../../..'), req = createRequire(path.join(root, 'desktop/resonance/package.json'));

const BALL = 170, FRAME = 272, PPP = 2, FPS = 60;     // as render.cjs: a 544 px frame, the ball 340 px wide
const KBPS = 2500;                                    // average bitrate; the lowest where the dark star field and glow show no banding or blocks
// [Mac face, look, frames at 60 fps, blink after ms (null: none)]: the loop length, from her face's periods (the periods that change are printed as the face renders)
const FACES = {
  talk: ['39', [0, .3], 655, 1100],     // 10.92 s: the reading gaze's 3 lines (5.45 s) twice; breath 4.2 s, bob 3.3 s and the voice rhythm round to it
  listen: ['35', [0, 0], 576, 1100],    // 9.6 s: head sway 3.2 s three times, breath 2.8 s at 3.2, bob 3.3 s at 3.2; the stars turn once (18 s at her speed)
  think: ['30', [0, 0], 509, 1100],     // 8.48 s: her motes go round twice (4.19 s), breath 4.2 s twice; her upward gaze (7.85 s, 4.83 s) rounds to 8.48 and 4.24
  rest: ['rest', [0, .15], 252, 1100],  // 4.2 s: one breath; bob 3.3 s runs at 4.2; the stars hold still (a turn takes 29 s at her speed)
  ask: ['ask', [0, .15], 576, 1100],    // 9.6 s: as listen, head sway 3.2 s three times; the stars turn once
  doze: ['doze', [0, .2], 312, null],   // 5.2 s: breath 5 s at 5.2, a z every 1.3 s; Always On still plays the PNG frames (1 Hz)
};
const ff = (...a) => execFileSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', ...a]);

(async () => {
  const { chromium } = req('playwright');
  const b = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const p = await b.newPage({ viewport: { width: 600, height: 600 }, deviceScaleFactor: PPP });
  p.on('pageerror', e => console.log('pageerror:', e.message));
  await p.goto('file://' + path.join(__dirname, 'page.html'));
  await p.waitForFunction(() => document.title === 'ready');
  const dst = path.join(root, 'ios/JarvisWatch/HerVideo'); fs.mkdirSync(dst, { recursive: true });

  for (const name of process.argv.slice(2).length ? process.argv.slice(2) : Object.keys(FACES)) {
    const [face, look, n, blinkMs] = FACES[name], T = n / FPS, dir = path.join(__dirname, 'out', `v-${name}`);
    fs.rmSync(dir, { recursive: true, force: true }); fs.mkdirSync(dir, { recursive: true });
    const r = await p.evaluate(s => window.renderPeriodic(s), { face, R: BALL / 2, W: FRAME, out: true, look, T, n, blinkMs, bg: '#000', check: true });
    r.frames.forEach((u, i) => fs.writeFileSync(path.join(dir, `${String(i).padStart(4, '0')}.png`), Buffer.from(u.split(',')[1], 'base64')));
    const sorted = [...r.steps].sort((a, b) => a - b), med = sorted[n >> 1], dup = r.steps.filter(d => d < .05).length;
    fs.writeFileSync(path.join(dir, 'steps.json'), JSON.stringify({ T, n, seam: r.steps[0], median: med, max: sorted[n - 1], dup, check: r.check, spin: r.spin, quantized: r.quantized, steps: r.steps }, null, 1));
    const f = i => path.join(dir, `${String((i + n) % n).padStart(4, '0')}.png`);
    ff('-i', f(-2), '-i', f(-1), '-i', f(0), '-i', f(1), '-filter_complex', 'hstack=inputs=4', path.join(dir, 'seam.png'));

    // one closed GOP per loop: every frame leans on the loop's own first frame, so the end does not step in quality into the start
    const out = path.join(dst, `her-${name}.mp4`);
    ff('-framerate', `${FPS}`, '-i', path.join(dir, '%04d.png'), '-frames:v', `${n}`,
      '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
      '-c:v', 'libx265', '-preset', 'slow', '-b:v', `${KBPS}k`, '-x265-params', `keyint=${n}:min-keyint=${n}:scenecut=0:open-gop=0:aq-mode=3:log-level=error`,
      '-tag:v', 'hvc1', '-an', '-movflags', '+faststart', out);
    const moved = Object.entries(r.quantized).filter(([, [w, q]]) => Math.abs(w - q) > 1e-9).map(([k, [w, q]]) => `${k} ${w.toFixed(3)}→${q.toFixed(3)}`);
    console.log(name, `${n} frames (${T.toFixed(2)} s), seam ${r.steps[0].toFixed(3)} / median step ${med.toFixed(3)} / max ${sorted[n - 1].toFixed(3)}, ${dup} still frames,`,
      `frame N against 0 ${r.check.toFixed(4)}, stars ${r.spin.turns} turn(s), ${(fs.statSync(out).size / 1024).toFixed(0)} KB`);
    console.log('  rates changed (rad/s or cycles/s):', moved.join(', ') || 'none');
  }
  await b.close();
})();
