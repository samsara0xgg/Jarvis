// Star-core sound kit. Every cue is synthesized at play time, nothing is loaded from a file.
// The score (which notes, when) is shared by all palettes; a palette only decides the instrument,
// so a cue keeps its shape in every timbre and switching palettes never changes what a sound means.

export const NOTE = {
  A4: 440, B4: 493.88, Cs5: 554.37, E5: 659.26, Fs5: 739.99, A5: 880, B5: 987.77, Cs6: 1108.73,
  E6: 1318.51, Fs6: 1479.98, A6: 1760, B6: 1975.53, Cs7: 2217.46, E7: 2637.02, Fs7: 2959.96, A7: 3520,
  // outside the pentatonic, only for the chord melody (major sevenths and a suspended fourth)
  D5: 587.33, Gs5: 830.61, D6: 1174.66, Gs6: 1661.22,
} as const;
export type Note = keyof typeof NOTE;
// A major pentatonic, low to high. Every cue is written in it (the shipped Live-enter cue is C#4 + E4,
// the same key), so two cues that overlap still sound like one chord.
export const SCALE: Note[] = ['A4', 'B4', 'Cs5', 'E5', 'Fs5', 'A5', 'B5', 'Cs6', 'E6', 'Fs6', 'A6', 'B6', 'Cs7', 'E7', 'Fs7', 'A7'];

// [ms from start, note, velocity 0..1, 1 = damped short hit]
export type Hit = [number, Note, number, 1?];
export type Cue = { hits: Hit[]; len?: number; dark?: boolean; gain?: number; fixed?: 'click' };

type Voice = (a: BaseAudioContext, out: AudioNode, t: number, f: number, v: number, len: number, short: boolean, dark: boolean) => number;
// oct moves the whole score by octaves (.5 = one down, still in key); lp softens everything above it.
export type Palette = { id: string; name: string; tempo: number; wet: number; gain: number; oct?: number; lp?: number; echo?: [number, number, number]; voice: Voice };

const clamp = (x: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, x));

// Exponential attack/decay; returns when the sound is gone.
function shape(p: AudioParam, t: number, peak: number, attack: number, decay: number) {
  p.setValueAtTime(.0001, t);
  p.exponentialRampToValueAtTime(Math.max(peak, .0002), t + attack);
  p.exponentialRampToValueAtTime(.0001, t + attack + decay);
  return t + attack + decay;
}
function run(nodes: (OscillatorNode | AudioBufferSourceNode)[], t: number, end: number) {
  for (const n of nodes) { n.start(t); n.stop(end + .03); }
  return end;
}

// Glass: an FM ping. The modulator at 3.5x (inharmonic) makes the bright "tink" and dies in
// ~60 ms, leaving a near-pure sine; a faint 2.76x partial fades first. Higher notes ring shorter.
const glass: Voice = (a, out, t, f, v, len, short) => {
  const d = (short ? .075 : .85 * len) * clamp(1100 / f, .55, 1.25);
  const car = a.createOscillator(), mod = a.createOscillator(), depth = a.createGain(), amp = a.createGain();
  car.frequency.value = f; mod.frequency.value = f * 3.5;
  depth.gain.setValueAtTime(f * (short ? .9 : 1.5) * v, t);
  depth.gain.exponentialRampToValueAtTime(f * .01, t + (short ? .025 : .06));
  mod.connect(depth).connect(car.frequency);
  const end = shape(amp.gain, t, .3 * v, .002, d);
  car.connect(amp).connect(out);
  const p = a.createOscillator(), pa = a.createGain();
  p.frequency.value = f * 2.76; shape(pa.gain, t, .05 * v, .002, d * .3);
  p.connect(pa).connect(out);
  return run([car, mod, p], t, end);
};

// Droplet: a sine that chirps up an octave in 30 ms, like a drop landing in water. Short, no ring.
const drop: Voice = (a, out, t, f, v, len, short) => {
  const d = short ? .05 : .2 * Math.max(.6, len);
  const o = a.createOscillator(), g = a.createGain();
  o.frequency.setValueAtTime(f * .5, t);
  o.frequency.exponentialRampToValueAtTime(f, t + .03);
  o.frequency.exponentialRampToValueAtTime(f * 1.06, t + .03 + d);
  const end = shape(g.gain, t, .34 * v, .003, d);
  o.connect(g).connect(out);
  return run([o], t, end);
};

// Kalimba: a triangle behind a lowpass that closes as the pluck settles (bright, then woody),
// plus the tine's own high mode (about 5.4x) for the first 50 ms.
const tine: Voice = (a, out, t, f, v, len, short) => {
  const d = (short ? .09 : 1.0 * len) * clamp(900 / f, .6, 1.3);
  const o = a.createOscillator(), lp = a.createBiquadFilter(), g = a.createGain();
  o.type = 'triangle'; o.frequency.value = f;
  lp.type = 'lowpass'; lp.Q.value = .7;
  lp.frequency.setValueAtTime(f * 4, t); lp.frequency.exponentialRampToValueAtTime(f * 1.3, t + .12);
  const end = shape(g.gain, t, .36 * v, .004, d);
  o.connect(lp).connect(g).connect(out);
  const nodes: OscillatorNode[] = [o];
  if (f * 5.4 < 9000) {
    const h = a.createOscillator(), hg = a.createGain();
    h.frequency.value = f * 5.4; shape(hg.gain, t, .035 * v, .001, .05);
    h.connect(hg).connect(out); nodes.push(h);
  }
  return run(nodes, t, end);
};

// Hologram: a square blip through a resonant lowpass that sweeps shut, a sine an octave down for
// weight; the palette adds a short digital echo. Fast, dry, a heads-up display.
const holo: Voice = (a, out, t, f, v, len, short) => {
  const d = short ? .03 : .11 * Math.max(.7, len);
  const o = a.createOscillator(), lp = a.createBiquadFilter(), g = a.createGain();
  o.type = 'square'; o.frequency.value = f;
  lp.type = 'lowpass'; lp.Q.value = 5;
  lp.frequency.setValueAtTime(Math.min(16000, f * 7), t); lp.frequency.exponentialRampToValueAtTime(f * 1.6, t + d);
  shape(g.gain, t, .09 * v, .001, d);
  o.connect(lp).connect(g).connect(out);
  const s = a.createOscillator(), sg = a.createGain();
  s.frequency.value = f / 2;
  const end = shape(sg.gain, t, .1 * v, .002, d * 1.3);
  s.connect(sg).connect(out);
  return run([o, s], t, end);
};

// Haptic: no pitch at all, the click engine already in the app (a 8 ms noise burst through a
// bandpass). The note only moves the bandpass, so a cue's contour survives as brightness.
function burst(a: BaseAudioContext) {
  const buffer = a.createBuffer(1, Math.round(a.sampleRate * .008), a.sampleRate), s = buffer.getChannelData(0), k = a.sampleRate * .0015;
  for (let i = 0; i < s.length; i++) s[i] = (Math.random() * 2 - 1) * Math.exp(-i / k);
  return buffer;
}
const click: Voice = (a, out, t, f, v, _len, _short, dark) => {
  const src = a.createBufferSource(), bp = a.createBiquadFilter(), g = a.createGain();
  src.buffer = burst(a);
  bp.type = 'bandpass'; bp.Q.value = 8; bp.frequency.value = clamp(f * 2.2, 1500, 6000) * (dark ? .55 : 1) * (1 + (Math.random() - .5) * .2);
  g.gain.value = 1.8 * v;
  src.connect(bp).connect(g).connect(out);
  src.start(t);
  return t + .01;
};

// ---------- round two: rounder and lower, most of them grown from the drop ----------
function noise(a: BaseAudioContext, seconds: number) {
  const buffer = a.createBuffer(1, Math.round(a.sampleRate * seconds), a.sampleRate), s = buffer.getChannelData(0);
  for (let i = 0; i < s.length; i++) s[i] = Math.random() * 2 - 1;
  const src = a.createBufferSource(); src.buffer = buffer; return src;
}

// Bubble: the drop's chirp made gentler (from .7x over 45 ms, 6 ms attack) with a fast wobble that
// dies in 80 ms, the "blub" of a bubble rising under water.
const bubble: Voice = (a, out, t, f, v, len, short) => {
  const d = short ? .06 : .18 * Math.max(.6, len);
  const o = a.createOscillator(), g = a.createGain(), lfo = a.createOscillator(), depth = a.createGain();
  o.frequency.setValueAtTime(f * .7, t); o.frequency.exponentialRampToValueAtTime(f, t + .045);
  lfo.frequency.value = 26;
  depth.gain.setValueAtTime(f * .035, t); depth.gain.exponentialRampToValueAtTime(f * .001, t + .08);
  lfo.connect(depth).connect(o.frequency);
  const end = shape(g.gain, t, .34 * v, .006, d);
  o.connect(g).connect(out);
  return run([o, lfo], t, end);
};

// Pop: a sine that falls into its note (1.6x down to 1x in 22 ms), the soft "boop" of a button.
const pop: Voice = (a, out, t, f, v, len, short) => {
  const d = short ? .06 : .14 * Math.max(.6, len);
  const o = a.createOscillator(), g = a.createGain();
  o.frequency.setValueAtTime(f * 1.6, t); o.frequency.exponentialRampToValueAtTime(f, t + .022);
  const end = shape(g.gain, t, .36 * v, .002, d);
  o.connect(g).connect(out);
  return run([o], t, end);
};

// Marimba: a soft mallet on a wooden bar. Sine fundamental, the bar's 4x mode for 50 ms, a low thump.
const marimba: Voice = (a, out, t, f, v, len, short) => {
  const d = (short ? .08 : .5 * len) * clamp(700 / f, .6, 1.4);
  const o = a.createOscillator(), g = a.createGain(), h = a.createOscillator(), hg = a.createGain();
  o.frequency.value = f; h.frequency.value = f * 4;
  const end = shape(g.gain, t, .34 * v, .002, d);
  shape(hg.gain, t, .07 * v, .001, .05);
  o.connect(g).connect(out); h.connect(hg).connect(out);
  const n = noise(a, .02), lp = a.createBiquadFilter(), ng = a.createGain();
  lp.type = 'lowpass'; lp.frequency.value = 1000; shape(ng.gain, t, .08 * v, .001, .015);
  n.connect(lp).connect(ng).connect(out);
  return run([o, h, n], t, end);
};

// Electric piano: a gentle 1:1 FM that starts a little bright and settles into a round tone.
const epiano: Voice = (a, out, t, f, v, len, short) => {
  const d = (short ? .1 : 1.1 * len) * clamp(700 / f, .6, 1.4);
  const car = a.createOscillator(), mod = a.createOscillator(), depth = a.createGain(), amp = a.createGain();
  car.frequency.value = f; mod.frequency.value = f;
  depth.gain.setValueAtTime(f * 1.1 * v, t); depth.gain.exponentialRampToValueAtTime(f * .12, t + (short ? .05 : .3));
  mod.connect(depth).connect(car.frequency);
  const end = shape(amp.gain, t, .3 * v, .004, d);
  car.connect(amp).connect(out);
  return run([car, mod], t, end);
};

// Bamboo: a hollow knock. A sine that drops into its note in 12 ms and a short burst of noise
// ringing in the tube (bandpass at 2.4x).
const bamboo: Voice = (a, out, t, f, v, len, short) => {
  const d = short ? .06 : .16 * Math.max(.7, len);
  const o = a.createOscillator(), g = a.createGain();
  o.frequency.setValueAtTime(f * 1.12, t); o.frequency.exponentialRampToValueAtTime(f, t + .012);
  const end = shape(g.gain, t, .3 * v, .001, d);
  o.connect(g).connect(out);
  const n = noise(a, .05), bp = a.createBiquadFilter(), ng = a.createGain();
  bp.type = 'bandpass'; bp.frequency.value = f * 2.4; bp.Q.value = 9; shape(ng.gain, t, .5 * v, .001, .035);
  n.connect(bp).connect(ng).connect(out);
  return run([o, n], t, end);
};

// Hum: her own small voice. Slides up into each note like a voice does, holds, a little vibrato,
// then lets go. The octave above is quiet, for the "mm" colour.
const hum: Voice = (a, out, t, f, v, len, short) => {
  const d = short ? .09 : .36 * Math.max(.6, len), peak = .3 * v;
  const o = a.createOscillator(), o2 = a.createOscillator(), g = a.createGain(), g2 = a.createGain();
  const vib = a.createOscillator(), vd = a.createGain(), vd2 = a.createGain();
  for (const [osc, k] of [[o, 1], [o2, 2]] as const) {
    osc.frequency.setValueAtTime(f * k * .94, t); osc.frequency.exponentialRampToValueAtTime(f * k, t + .045);
  }
  vib.frequency.value = 5.5;
  vd.gain.setValueAtTime(0, t); vd.gain.linearRampToValueAtTime(f * .008, t + .12);
  vd2.gain.setValueAtTime(0, t); vd2.gain.linearRampToValueAtTime(f * .016, t + .12);
  vib.connect(vd).connect(o.frequency); vib.connect(vd2).connect(o2.frequency);
  g.gain.setValueAtTime(.0001, t);
  g.gain.exponentialRampToValueAtTime(peak, t + .018);
  g.gain.exponentialRampToValueAtTime(peak * .8, t + .018 + d * .45);
  g.gain.exponentialRampToValueAtTime(.0001, t + .018 + d);
  g2.gain.value = .18;
  o.connect(g).connect(out); o2.connect(g2).connect(g);
  return run([o, o2, vib], t, t + .018 + d);
};

// ---------- round three: crisp, dry and short, no long tails ----------
function sine(a: BaseAudioContext, out: AudioNode, t: number, f: number, peak: number, attack: number, decay: number) {
  const o = a.createOscillator(), g = a.createGain();
  o.frequency.value = f;
  const end = shape(g.gain, t, peak, attack, decay);
  o.connect(g).connect(out);
  return run([o], t, end);
}

// Crisp drop: the drop, faster (chirps up in 15 ms) and half as long, with a whisper of the octave for edge.
const dropCrisp: Voice = (a, out, t, f, v, len, short) => {
  const d = short ? .04 : .11 * Math.max(.6, len);
  let end = t;
  for (const [k, peak, dk] of [[1, .36, 1], [2, .04, .5]] as const) {
    const o = a.createOscillator(), g = a.createGain();
    o.frequency.setValueAtTime(f * k * .55, t); o.frequency.exponentialRampToValueAtTime(f * k, t + .015);
    o.frequency.exponentialRampToValueAtTime(f * k * 1.03, t + .015 + d);
    end = Math.max(end, shape(g.gain, t, peak * v, .0015, d * dk));
    o.connect(g).connect(out); run([o], t, t + .0015 + d * dk);
  }
  return end;
};

// Pure: a plain sine ping. No chirp, no ring, the simplest sound there is.
const pure: Voice = (a, out, t, f, v, len, short) => {
  const d = short ? .05 : .16 * Math.max(.6, len);
  sine(a, out, t, f * 2, .03 * v, .001, d * .4);
  return sine(a, out, t, f, .32 * v, .0015, d);
};

// Crisp pop: a cork. Falls from 2.2x into the note in 8 ms and is gone in 75 ms.
const popCrisp: Voice = (a, out, t, f, v, len, short) => {
  const d = short ? .035 : .075 * Math.max(.7, len);
  const o = a.createOscillator(), g = a.createGain();
  o.frequency.setValueAtTime(f * 2.2, t); o.frequency.exponentialRampToValueAtTime(f, t + .008);
  const end = shape(g.gain, t, .4 * v, .001, d);
  o.connect(g).connect(out);
  return run([o], t, end);
};

// Xylophone: a hard mallet on rosewood. The bar's 3x mode for 30 ms and a click on top of the note.
const xylo: Voice = (a, out, t, f, v, len, short) => {
  const d = (short ? .05 : .2 * len) * clamp(900 / f, .6, 1.3);
  sine(a, out, t, f * 3, .08 * v, .001, .03);
  const n = noise(a, .01), bp = a.createBiquadFilter(), ng = a.createGain();
  bp.type = 'bandpass'; bp.frequency.value = 4000; bp.Q.value = 1.2; shape(ng.gain, t, .06 * v, .0005, .006);
  n.connect(bp).connect(ng).connect(out); run([n], t, t + .01);
  return sine(a, out, t, f, .32 * v, .001, d);
};

// Steel drum: the round, bright note of a pan. Octave and twelfth partials fade faster than the note.
const pan: Voice = (a, out, t, f, v, len, short) => {
  const d = (short ? .07 : .32 * len) * clamp(700 / f, .6, 1.4);
  sine(a, out, t, f * 2, .13 * v, .002, d * .6);
  sine(a, out, t, f * 3.01, .05 * v, .002, d * .35);
  return sine(a, out, t, f, .28 * v, .002, d);
};

// Pizzicato: a plucked string. A saw through a lowpass that snaps shut in 60 ms.
const pizz: Voice = (a, out, t, f, v, len, short) => {
  const d = short ? .06 : .2 * Math.max(.6, len);
  const o = a.createOscillator(), lp = a.createBiquadFilter(), g = a.createGain();
  o.type = 'sawtooth'; o.frequency.value = f;
  lp.type = 'lowpass'; lp.Q.value = 1;
  lp.frequency.setValueAtTime(f * 8, t); lp.frequency.exponentialRampToValueAtTime(f * 1.4, t + .06);
  const end = shape(g.gain, t, .22 * v, .002, d);
  o.connect(lp).connect(g).connect(out);
  return run([o], t, end);
};

// Marble: two glass beads touching. Two sines 0.7% apart (the beat is the clink) and a 2.7x glint
// for 15 ms. The glass without its ring.
const marble: Voice = (a, out, t, f, v, len, short) => {
  const d = short ? .04 : .12 * Math.max(.6, len);
  sine(a, out, t, f * 2.7, .05 * v, .0008, .015);
  sine(a, out, t, f * 1.007, .17 * v, .001, d);
  return sine(a, out, t, f, .17 * v, .001, d);
};

// Gains level-match the palettes: one offline render of every cue, short-term RMS for the tonal
// ones, peak for the clicks (a 8 ms click reads louder than its RMS says), +1 dB for the palettes
// an octave down, which the ear hears as quieter at the same level. Round three is short, where a
// 50 ms window underreads loudness, so its gains stop short of the RMS match to keep peaks level.
export const PALETTES: Palette[] = [
  { id: 'glass', name: '玻璃', tempo: 1, wet: .22, gain: 1, voice: glass },
  { id: 'drop', name: '水滴', tempo: .85, wet: .1, gain: 1.1, voice: drop },
  { id: 'tine', name: '拇指琴', tempo: 1.1, wet: .28, gain: .95, voice: tine },
  { id: 'holo', name: '全息', tempo: .6, wet: .08, gain: 2.5, echo: [.095, .28, .32], voice: holo },
  { id: 'click', name: '触感', tempo: .75, wet: 0, gain: 2, voice: click },
  // round two: each drop variant changes one thing about the drop
  { id: 'dropLow', name: '水滴·低', tempo: .9, wet: .12, gain: 1.24, oct: .5, voice: drop },
  { id: 'bubble', name: '气泡', tempo: .95, wet: .14, gain: 1.1, lp: 2600, voice: bubble },
  { id: 'dropRoom', name: '水滴·回声', tempo: .9, wet: .34, gain: 1.1, echo: [.16, .2, .22], voice: drop },
  { id: 'pop', name: '啵', tempo: .9, wet: .1, gain: 1.4, oct: .5, voice: pop },
  { id: 'marimba', name: '马林巴', tempo: 1, wet: .2, gain: 1.1, oct: .5, voice: marimba },
  { id: 'epiano', name: '电钢琴', tempo: 1, wet: .24, gain: .81, oct: .5, lp: 2600, voice: epiano },
  { id: 'bamboo', name: '竹筒', tempo: .9, wet: .16, gain: 1.48, oct: .5, voice: bamboo },
  // round three: crisp, dry, short
  { id: 'dropCrisp', name: '水滴·脆', tempo: .85, wet: .06, gain: 1.26, voice: dropCrisp },
  { id: 'dropMid', name: '水滴·中', tempo: .85, wet: .08, gain: 1.17, oct: .75, voice: drop },
  { id: 'pure', name: '纯音', tempo: .9, wet: .06, gain: 1.26, oct: .75, voice: pure },
  { id: 'popCrisp', name: '脆啵', tempo: .85, wet: .05, gain: 1.19, voice: popCrisp },
  { id: 'xylo', name: '木琴', tempo: .9, wet: .06, gain: 1.41, oct: .75, voice: xylo },
  { id: 'pan', name: '钢鼓', tempo: .95, wet: .08, gain: 1.15, oct: .5, voice: pan },
  { id: 'pizz', name: '拨弦', tempo: .95, wet: .08, gain: 1.88, oct: .5, voice: pizz },
  { id: 'marble', name: '玻璃珠', tempo: .85, wet: .05, gain: 1.26, oct: .75, voice: marble },
  { id: 'hum', name: '小哼声', tempo: 1.1, wet: .16, gain: .54, oct: .5, lp: 2000, voice: hum },
];
export const palette = (id: string) => PALETTES.find(p => p.id === id) ?? PALETTES[0];

// ---------- the score ----------
const riser: Hit[] = (['A5', 'B5', 'Cs6', 'E6', 'Fs6', 'A6', 'B6', 'Cs7'] as Note[])
  .map((n, i) => [Math.round(430 * (1 - (1 - i / 7) ** 1.7)), n, .16 + .04 * i]);

export const CUES: Record<string, Cue> = {
  // voice turn
  listen: { hits: [[0, 'A5', .75], [75, 'E6', .9]], len: .6 },
  heard: { hits: [[0, 'Cs6', .5]], len: .4 },
  think: { hits: [[0, 'Fs6', .24], [150, 'E6', .2]], len: .5, gain: .8 },
  turn: { hits: [[0, 'E6', .42]], len: .5 },
  interrupt: { hits: [[0, 'E6', .6, 1], [45, 'A5', .65, 1]] },
  miss: { hits: [[0, 'A5', .6], [120, 'Fs5', .65]], len: .7 },
  end: { hits: [[0, 'E6', .75], [95, 'A5', .85]] },
  // notifications
  done: { hits: [[0, 'Cs6', .6], [85, 'E6', .7], [170, 'A6', .85]] },
  ask: { hits: [[0, 'B5', .6], [95, 'E6', .7], [190, 'B6', .85]] },
  error: { hits: [[0, 'Fs5', .75], [160, 'B4', .85]], len: 1.1, dark: true },
  remind: { hits: [[0, 'A5', .55], [0, 'E6', .45], [420, 'A5', .55], [420, 'E6', .45]], len: 1.2 },
  msg: { hits: [[0, 'E6', .5], [60, 'A6', .35]], len: .8 },
  // her own
  hello: { hits: [[0, 'A4', .35], [0, 'A5', .6], [120, 'E6', .65], [240, 'A6', .7], [360, 'Cs7', .6]], len: 1.3 },
  bye: { hits: [[0, 'Cs7', .45], [120, 'A6', .5], [240, 'E6', .55], [360, 'A5', .65], [360, 'A4', .3]], len: 1.3 },
  charge: { hits: riser, len: .3 },
  transform: { hits: [[0, 'E7', .34, 1], [25, 'Cs7', .3, 1], [50, 'Fs7', .3, 1], [75, 'B6', .3, 1], [100, 'E7', .28, 1], [125, 'A6', .3, 1],
    [360, 'A5', .55], [360, 'E6', .5], [360, 'A6', .45]], len: 1.2 },
  happy: { hits: [[0, 'E6', .45], [70, 'A6', .55]], len: .5 },
  surprise: { hits: [[0, 'B5', .4], [45, 'Fs6', .55]], len: .4 },
  sleepy: { hits: [[0, 'E6', .3], [260, 'Cs6', .26], [560, 'A5', .22]], len: .9 },
  // dashboard
  open: { hits: [[0, 'E6', .25, 1], [40, 'A6', .3, 1]] },
  close: { hits: [[0, 'A6', .25, 1], [40, 'E6', .22, 1]] },
  module: { hits: [[0, 'A6', .34, 1]] },
  back: { hits: [[0, 'E6', .32, 1]] },
  send: { hits: [[0, 'A5', .35, 1], [45, 'E6', .45, 1], [90, 'A6', .55]], len: .5 },
  on: { hits: [[0, 'Cs6', .32, 1], [45, 'A6', .36, 1]] },
  off: { hits: [[0, 'A6', .32, 1], [45, 'Cs6', .28, 1]] },
  refresh: { hits: [[0, 'Cs7', .22, 1]] },
  fail: { hits: [[0, 'Fs5', .45, 1], [70, 'Fs5', .4, 1]], dark: true },
  // the mic and speaker toggles already in the app: Hermes' selection / open / close clicks, in every palette
  mic: { hits: [[0, 'A5', .52]], fixed: 'click' },
  speakerOn: { hits: [[0, 'Fs5', .42], [36, 'A5', .66]], fixed: 'click' },
  speakerOff: { hits: [[0, 'A5', .58], [32, 'Fs5', .34]], fixed: 'click' },
};
// ---------- melodies ----------
// The score above is one melody set (open fifths and arpeggios). Each set below rewrites the cues that
// carry a tune; taps, the charge and the costume change stay shared. Any set plays in any palette.
type Melody = { id: string; name: string; cues: Record<string, Cue> };
const soft = (gain: number, cues: Record<string, Cue>) => Object.fromEntries(Object.entries(cues).map(([k, c]) => [k, { ...c, gain }]));
export const MELODIES: Melody[] = [
  { id: 'fifths', name: '五度', cues: {} },
  // Adjacent scale steps only, two notes at most: the rise and fall of a voice saying "mm".
  { id: 'steps', name: '小步', cues: {
    listen: { hits: [[0, 'A5', .7], [70, 'B5', .85]], len: .6 },
    heard: { hits: [[0, 'B5', .5]], len: .4 },
    turn: { hits: [[0, 'B5', .4]], len: .5 },
    interrupt: { hits: [[0, 'B5', .6, 1], [40, 'A5', .6, 1]] },
    miss: { hits: [[0, 'Cs6', .55], [110, 'B5', .6]], len: .7 },
    end: { hits: [[0, 'B5', .7], [90, 'A5', .85]] },
    done: { hits: [[0, 'A5', .6], [80, 'B5', .7], [160, 'Cs6', .85]] },
    ask: { hits: [[0, 'A5', .6], [90, 'B5', .75], [260, 'B5', .7]] },
    error: { hits: [[0, 'B4', .75], [150, 'A4', .85]], len: 1.1, dark: true },
    remind: { hits: [[0, 'Cs6', .6], [140, 'B5', .55], [280, 'Cs6', .6], [420, 'B5', .55]] },
    msg: { hits: [[0, 'B5', .45], [60, 'Cs6', .4]], len: .8 },
    hello: { hits: [[0, 'A5', .5], [100, 'B5', .55], [200, 'Cs6', .6], [300, 'E6', .65]], len: 1.2 },
    bye: { hits: [[0, 'E6', .5], [100, 'Cs6', .5], [200, 'B5', .55], [300, 'A5', .65]], len: 1.2 },
    send: { hits: [[0, 'B5', .4, 1], [45, 'Cs6', .5]], len: .5 },
  } },
  // One pitch, meaning by count and rhythm; only start and end move, by an octave.
  { id: 'rhythm', name: '节奏', cues: {
    listen: { hits: [[0, 'E5', .6], [80, 'E6', .8]], len: .6 },
    heard: { hits: [[0, 'E6', .35, 1]] },
    turn: { hits: [[0, 'E6', .4]], len: .5 },
    interrupt: { hits: [[0, 'E6', .6, 1], [45, 'E6', .5, 1]] },
    miss: { hits: [[0, 'E6', .5], [240, 'E5', .5]], len: .7 },
    end: { hits: [[0, 'E6', .6], [100, 'E5', .7]] },
    done: { hits: [[0, 'E6', .7], [110, 'E6', .85]] },
    ask: { hits: [[0, 'E6', .7], [110, 'E6', .7], [220, 'E6', .8]] },
    error: { hits: [[0, 'E5', .8], [260, 'E5', .8]], len: 1.1, dark: true },
    remind: { hits: [[0, 'E6', .6], [90, 'E6', .6], [400, 'E6', .6], [490, 'E6', .6]] },
    msg: { hits: [[0, 'E6', .55]], len: .8 },
    hello: { hits: [[0, 'E5', .5], [140, 'E6', .6], [220, 'E6', .7]], len: 1.2 },
    bye: { hits: [[0, 'E6', .6], [140, 'E5', .6], [220, 'E5', .5]], len: 1.2 },
    send: { hits: [[0, 'E6', .5, 1], [50, 'E6', .6]], len: .5 },
  } },
  // A ball settling: after the tune, the last note bounces with shrinking gaps and falling loudness.
  { id: 'bounce', name: '弹跳', cues: {
    listen: { hits: [[0, 'A5', .8], [120, 'E6', .6], [190, 'E6', .35]], len: .6 },
    heard: { hits: [[0, 'Cs6', .5], [90, 'Cs6', .25]], len: .4 },
    turn: { hits: [[0, 'E6', .4], [80, 'E6', .2]], len: .5 },
    interrupt: { hits: [[0, 'E6', .6, 1], [50, 'A5', .5, 1]] },
    miss: { hits: [[0, 'A5', .6], [130, 'Fs5', .5], [200, 'Fs5', .3], [245, 'Fs5', .18]], len: .7 },
    end: { hits: [[0, 'E6', .75], [140, 'A5', .6], [230, 'A5', .35], [285, 'A5', .2], [320, 'A5', .1]] },
    done: { hits: [[0, 'Cs6', .6], [100, 'E6', .7], [180, 'A6', .85], [300, 'A6', .4], [370, 'A6', .22], [410, 'A6', .12]] },
    ask: { hits: [[0, 'B5', .6], [140, 'E6', .7], [260, 'B6', .8], [400, 'B6', .5]] },
    error: { hits: [[0, 'Fs5', .75], [180, 'B4', .8], [300, 'B4', .45], [370, 'B4', .25]], len: 1.1, dark: true },
    remind: { hits: [[0, 'A5', .55], [0, 'E6', .45], [160, 'A5', .35], [160, 'E6', .3], [260, 'A5', .2], [260, 'E6', .18]], len: 1.2 },
    msg: { hits: [[0, 'E6', .5], [90, 'A6', .35], [150, 'A6', .18]], len: .8 },
    hello: { hits: [[0, 'A4', .35], [0, 'A5', .6], [200, 'E6', .65], [340, 'A6', .7], [440, 'Cs7', .6], [510, 'Cs7', .3]], len: 1.3 },
    bye: { hits: [[0, 'Cs7', .45], [180, 'A6', .5], [310, 'E6', .55], [400, 'A5', .65], [460, 'A5', .35], [500, 'A5', .18]], len: 1.3 },
  } },
  // Mandarin tones played on the instrument: rising "嗯？", falling "嗯。", "好啦！", "拜拜", "哎呀".
  // A tone 3 dips (F#5, E5, A5), tone 4 falls through a grace note, tone 2 rises through one.
  { id: 'speech', name: '说话腔', cues: {
    listen: { hits: [[0, 'B5', .55], [50, 'E6', .85]], len: .6 },
    heard: { hits: [[0, 'Cs6', .55], [35, 'A5', .45, 1]], len: .4 },
    turn: { hits: [[0, 'B5', .3], [50, 'E6', .45]], len: .5 },
    interrupt: { hits: [[0, 'E6', .6, 1], [35, 'Cs6', .5, 1]] },
    miss: { hits: [[0, 'A5', .5], [60, 'Cs6', .6], [120, 'Fs6', .7]], len: .7 },
    end: { hits: [[0, 'E6', .75], [40, 'B5', .6, 1], [170, 'A5', .5, 1]] },
    done: { hits: [[0, 'Fs5', .6], [50, 'E5', .5], [110, 'A5', .7], [230, 'E6', .8]] },
    ask: { hits: [[0, 'B5', .55], [45, 'E6', .7], [200, 'B5', .55], [245, 'Fs6', .8]] },
    error: { hits: [[0, 'E6', .7], [160, 'Cs6', .6], [210, 'A5', .5]], len: 1.1, dark: true },
    remind: { hits: [[0, 'E6', .6], [40, 'B5', .5, 1], [150, 'Fs5', .55], [190, 'E5', .45], [240, 'A5', .55], [360, 'Cs6', .6]], len: 1.2 },
    msg: { hits: [[0, 'B5', .5], [45, 'E6', .6], [170, 'Cs6', .4, 1]], len: .8 },
    hello: { hits: [[0, 'Fs5', .55], [50, 'E5', .45], [110, 'A5', .6], [250, 'A6', .8], [290, 'E6', .65]], len: 1.3 },
    bye: { hits: [[0, 'Fs5', .5], [50, 'E5', .4], [110, 'A5', .55], [260, 'E6', .6]], len: 1.3 },
  } },
  // Chords instead of tunes, rolled 20-25 ms apart: major sevenths, a suspended fourth that waits.
  // Stacked notes measure ~2 dB over the other sets, so every chord is played at .8.
  { id: 'chords', name: '和弦', cues: soft(.8, {
    listen: { hits: [[0, 'A5', .6], [18, 'Cs6', .6], [36, 'E6', .7]], len: .6 },
    heard: { hits: [[0, 'Cs6', .45], [0, 'E6', .4]], len: .4 },
    turn: { hits: [[0, 'A5', .35], [0, 'E6', .35]], len: .5 },
    interrupt: { hits: [[0, 'A5', .5, 1], [0, 'E6', .5, 1]] },
    miss: { hits: [[0, 'Fs5', .5], [20, 'A5', .5], [40, 'Cs6', .5], [60, 'E6', .5]], len: .7 },
    end: { hits: [[0, 'E6', .55], [20, 'Cs6', .55], [40, 'A5', .65]] },
    done: { hits: [[0, 'A5', .6], [25, 'Cs6', .6], [50, 'E6', .65], [75, 'Gs6', .6]] },
    ask: { hits: [[0, 'A5', .6], [25, 'D6', .6], [50, 'E6', .65]] },
    error: { hits: [[0, 'Fs5', .7], [0, 'A5', .55], [200, 'D5', .7], [200, 'Fs5', .55]], len: 1.1, dark: true },
    remind: { hits: [[0, 'E6', .5], [0, 'Gs6', .45], [400, 'E6', .5], [400, 'Gs6', .45]], len: 1.2 },
    msg: { hits: [[0, 'Cs6', .45], [0, 'E6', .4]], len: .8 },
    hello: { hits: [[0, 'A4', .45], [60, 'E5', .5], [120, 'Gs5', .55], [180, 'B5', .6], [240, 'Cs6', .6]], len: 1.3 },
    bye: { hits: [[0, 'Cs6', .5], [60, 'B5', .5], [120, 'Gs5', .5], [180, 'E5', .55], [240, 'A4', .5]], len: 1.3 },
    send: { hits: [[0, 'E6', .4, 1], [40, 'A6', .5]], len: .5 },
  }) },
];
export const scoreOf = (id: string): Record<string, Cue> => ({ ...CUES, ...(MELODIES.find(m => m.id === id)?.cues ?? {}) });

// Slider ticks walk the scale with the value, so dragging plays a little run.
export const tickCue = (k: number): Cue => ({ hits: [[0, SCALE[5 + Math.round(clamp(k, 0, 1) * 9)], .24, 1]] });

// ---------- playback ----------
const rooms = new WeakMap<AudioNode, ConvolverNode>();
function room(a: BaseAudioContext, dest: AudioNode) {
  let c = rooms.get(dest);
  if (!c) {
    const n = Math.round(a.sampleRate * 1.4), ir = a.createBuffer(2, n, a.sampleRate);
    for (let ch = 0; ch < 2; ch++) { const d = ir.getChannelData(ch); for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / n) ** 3.2; }
    c = a.createConvolver(); c.buffer = ir; c.connect(dest); rooms.set(dest, c);
  }
  return c;
}

export type Handle = { end: number; stop(): void };
// Allen's own tuning on top of any palette: semitones up or down, length, and brightness (1 = untouched).
export type Tune = { semi: number; len: number; bright: number };
export const FLAT: Tune = { semi: 0, len: 1, bright: 1 };
export function play(a: BaseAudioContext, dest: AudioNode, cue: Cue, pal: Palette, gain = 1, at = a.currentTime + .005, tune = FLAT): Handle {
  const p = cue.fixed ? palette(cue.fixed) : pal;
  const bus = a.createGain();
  bus.gain.value = p.gain * (cue.gain ?? 1) * gain;
  let tail: AudioNode = bus;
  const fixed = !!cue.fixed, bright = fixed || tune.bright >= 1 ? 2e4 : 1500 * 2 ** (tune.bright * 3.5);
  if (cue.dark || p.lp || bright < 2e4) {
    const lp = a.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = Math.min(cue.dark ? 1500 : 2e4, p.lp ?? 2e4, bright); lp.Q.value = .5;
    bus.connect(lp); tail = lp;
  }
  tail.connect(dest);
  if (p.wet) { const send = a.createGain(); send.gain.value = p.wet; tail.connect(send).connect(room(a, dest)); }
  if (p.echo) {
    const [time, fb, mix] = p.echo, delay = a.createDelay(1), loop = a.createGain(), wet = a.createGain();
    delay.delayTime.value = time; loop.gain.value = fb; wet.gain.value = mix;
    tail.connect(delay).connect(loop).connect(delay); delay.connect(wet).connect(dest);
  }
  let end = at;
  const shift = (p.oct ?? 1) * (fixed ? 1 : 2 ** (tune.semi / 12)), len = (cue.len ?? 1) * (fixed ? 1 : tune.len);
  for (const [ms, n, v, s] of cue.hits) end = Math.max(end, p.voice(a, bus, at + ms * p.tempo / 1000, NOTE[n] * shift, v, len, !!s, !!cue.dark));
  return {
    end: end + (p.wet ? .6 : p.echo ? .5 : 0),
    stop() {
      if (!('suspend' in a)) return;
      bus.gain.cancelScheduledValues(a.currentTime);
      bus.gain.setTargetAtTime(0, a.currentTime, .012);
      setTimeout(() => bus.disconnect(), 250);
    },
  };
}
