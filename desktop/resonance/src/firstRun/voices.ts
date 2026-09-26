// The first launch's bright voices: drop-in palettes for the app's sound kit (same Voice shape as ../soundKit.ts).
import { palette, type Palette } from '../soundKit';
const clamp = (v: number, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, v));

// A few ms of bright noise: the strike on top of a bell, or the snap of the hit.
export function tick(a: BaseAudioContext, out: AudioNode, t: number, v: number, hp = 5000, ms = 4) {
  const b = a.createBuffer(1, Math.max(8, Math.round(a.sampleRate * ms / 1000)), a.sampleRate), s = b.getChannelData(0);
  for (let i = 0; i < s.length; i++) s[i] = (Math.random() * 2 - 1) * (1 - i / s.length) ** 2;
  const n = a.createBufferSource(), f = a.createBiquadFilter(), g = a.createGain();
  n.buffer = b; f.type = 'highpass'; f.frequency.value = hp; g.gain.value = v;
  n.connect(f).connect(g).connect(out); n.start(t);
}

// A small struck bell: harmonic partials plus the slightly sharp 4.2x and 5.4x modes, each dying sooner than
// the one below, and a 4 ms strike of bright noise. Nothing is filtered away, so it reaches the treble.
const PARTS: [number, number, number][] = [[1, 1, 1], [2, .5, .6], [3, .24, .38], [4.2, .14, .24], [5.4, .08, .15], [6.8, .04, .09]];
// The softer bell for the lower music: the same strike without the two highest modes and with a quieter tick.
const SOFT: [number, number, number][] = [[1, 1, 1], [2, .42, .6], [3, .15, .36], [4.2, .05, .2]];
const makeBell = (parts: [number, number, number][], strike: number): Palette['voice'] => (a, out, t, f, v, len, short) => {
  const dur = (short ? .1 : .5 * len) * clamp(1000 / f, .5, 1.4);
  let end = t;
  for (const [k, amp, dk] of parts) {
    if (f * k > 15000) continue;
    const o = a.createOscillator(), g = a.createGain(), d = dur * dk;
    o.frequency.value = f * k; o.detune.value = (Math.random() - .5) * 6;
    g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(.16 * amp * v, t + .002); g.gain.exponentialRampToValueAtTime(.0001, t + .002 + d);
    o.connect(g).connect(out); o.start(t); o.stop(t + d + .05);
    end = Math.max(end, t + .002 + d);
  }
  tick(a, out, t, strike * v);
  return end;
};
export const bellVoice = makeBell(PARTS, .05), bellSoft = makeBell(SOFT, .018);
// The crisp drop the app uses now, with its overtones put back: octave, twelfth and double octave chirp up
// with it and die sooner, and a 4 ms strike. Same shape and length; it just reaches the treble.
export const dropBright: Palette['voice'] = (a, out, t, f, v, len, short) => {
  const d = short ? .045 : .12 * Math.max(.6, len);
  let end = t;
  for (const [k, peak, dk] of [[1, .32, 1], [2, .14, .6], [3, .07, .4], [4, .035, .28]] as const) {
    const o = a.createOscillator(), g = a.createGain();
    o.frequency.setValueAtTime(f * k * .55, t); o.frequency.exponentialRampToValueAtTime(f * k, t + .015);
    o.frequency.exponentialRampToValueAtTime(f * k * 1.03, t + .015 + d);
    g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(peak * v, t + .0015); g.gain.exponentialRampToValueAtTime(.0001, t + .0015 + d * dk);
    o.connect(g).connect(out); o.start(t); o.stop(t + d * dk + .05);
    end = Math.max(end, t + .0015 + d * dk);
  }
  tick(a, out, t, .08 * v, 4500);
  return end;
};
// Gains level-match the new palettes to the current one (measure.ts, median short RMS over every cue).
export const UI_PAL: Record<string, Palette> = {
  dropCrisp: palette('dropCrisp'),
  dropBright: { id: 'dropBright', name: '亮水滴', tempo: .85, wet: .08, gain: 1.28, voice: dropBright },
  bell: { id: 'bell', name: '小铃', tempo: .9, wet: .14, gain: 1.88, voice: bellVoice },
};
