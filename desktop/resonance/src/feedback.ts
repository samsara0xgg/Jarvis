export type FeedbackCue = 'voice-enter' | 'mic-on' | 'mic-off' | 'speaker-on' | 'speaker-off';
// Only voice-enter still ships as a rendered asset; the toggles are synthesized below.
const sampled: FeedbackCue[] = ['voice-enter'];

// Hermes desktop makes no tone for its mic and speaker toggles — it fires haptic
// click patterns, one 4 ms noise burst per pulse through a bandpass whose centre
// frequency and gain both track the pulse intensity, so a harder pulse reads
// brighter as well as louder. Engine ported from web-haptics 0.0.6 (MIT); the
// three patterns are Hermes' own `selection` / `open` / `close` intents.
type Pulse = { delay?: number; duration: number; intensity: number };
const patterns: Partial<Record<FeedbackCue, Pulse[]>> = {
  // `selection`: one click, and Hermes deliberately uses it for both directions.
  'mic-on': [{ duration: 16, intensity: .52 }],
  'mic-off': [{ duration: 16, intensity: .52 }],
  // `open` / `close`: a pair that rises into the second click or falls away from it.
  'speaker-on': [{ duration: 18, intensity: .42 }, { delay: 36, duration: 22, intensity: .66 }],
  'speaker-off': [{ duration: 22, intensity: .58 }, { delay: 32, duration: 16, intensity: .34 }]
};

let context: AudioContext | undefined;
const buffers = new Map<FeedbackCue, Promise<AudioBuffer>>();
let playing: { sources: AudioBufferSourceNode[]; gain?: GainNode } | undefined;
let sequence = 0;
let idleTimer: ReturnType<typeof setTimeout> | undefined;

function prepare() {
  context ??= new AudioContext({ latencyHint: 'interactive' });
  for (const cue of sampled) if (!buffers.has(cue)) buffers.set(cue,
    fetch(new URL(`audio/${cue}.wav`, document.baseURI)).then(response => {
      if (!response.ok) throw new Error(`Feedback asset unavailable: ${cue}`);
      return response.arrayBuffer();
    }).then(bytes => context!.decodeAudioData(bytes)));
  return context;
}

// A burst decays over 25 samples, not 25 ms — web-haptics hard-codes the sample
// count, and that is what gives the click its character at any sample rate.
function burst(audio: AudioContext) {
  const buffer = audio.createBuffer(1, Math.max(1, Math.round(audio.sampleRate * .004)), audio.sampleRate);
  const samples = buffer.getChannelData(0);
  for (let i = 0; i < samples.length; i++) samples[i] = (Math.random() * 2 - 1) * Math.exp(-i / 25);
  return buffer;
}

// Schedule every click of a pattern up front, so stopFeedback cancels the unplayed
// ones by stopping their sources. web-haptics repeats a click every
// 16 + (1-intensity)*184 ms inside a pulse; at these durations that lands exactly
// one per pulse, and the formula is kept so a longer pulse still ports correctly.
// Each click gets its own filter because their centre frequencies differ.
function schedule(audio: AudioContext, pattern: Pulse[], volume: number) {
  const sources: AudioBufferSourceNode[] = [];
  const start = audio.currentTime;
  let at = 0;
  for (const pulse of pattern) {
    at += pulse.delay ?? 0;
    const step = 16 + (1 - pulse.intensity) * 184;
    for (let offset = 0; offset <= pulse.duration; offset += step) {
      const source = audio.createBufferSource(), filter = audio.createBiquadFilter(), gain = audio.createGain();
      source.buffer = burst(audio);
      filter.type = 'bandpass';
      filter.Q.value = 8;
      filter.frequency.value = (2000 + pulse.intensity * 2000) * (1 + (Math.random() - .5) * .3);
      gain.gain.value = .5 * pulse.intensity * volume;
      source.connect(filter).connect(gain).connect(audio.destination);
      source.start(start + (at + offset) / 1000);
      sources.push(source);
    }
    at += pulse.duration;
  }
  return { sources, duration: at / 1000 };
}

export function stopFeedback() {
  sequence++;
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { if (!playing) void context?.suspend().catch(() => {}); }, 35);
  if (!playing || !context) return;
  const { sources, gain } = playing;
  if (gain) {
    gain.gain.cancelScheduledValues(context.currentTime);
    gain.gain.setTargetAtTime(0, context.currentTime, .004);
  }
  for (const source of sources) source.stop(context.currentTime + .025);
  playing = undefined;
}

export async function playFeedback(cue: FeedbackCue, volume: number) {
  stopFeedback();
  if (volume <= 0) return;
  clearTimeout(idleTimer);
  const current = sequence;
  try {
    const audio = prepare();
    await audio.resume();
    if (current !== sequence) return;
    const level = Math.min(1, Math.max(0, volume));
    const pattern = patterns[cue];
    let sources: AudioBufferSourceNode[], gain: GainNode | undefined, duration: number;
    if (pattern) {
      ({ sources, duration } = schedule(audio, pattern, level));
    } else {
      const buffer = await buffers.get(cue)!;
      if (current !== sequence) return;
      const source = audio.createBufferSource();
      gain = audio.createGain();
      source.buffer = buffer;
      gain.gain.value = level;
      source.connect(gain).connect(audio.destination);
      source.start();
      sources = [source];
      duration = buffer.duration;
    }
    let live = sources.length;
    for (const source of sources) source.onended = () => {
      source.disconnect();
      if (--live > 0) return;
      gain?.disconnect();
      if (playing?.sources === sources) { playing = undefined; void audio.suspend().catch(() => {}); }
    };
    playing = { sources, gain };
    window.dispatchEvent(new CustomEvent('jarvis:feedback', { detail: { cue, duration, volume: level } }));
  } catch (error) {
    // Feedback failure must never prevent the control from changing state.
    console.warn('Feedback playback unavailable', error);
  }
}

export function warmFeedback() {
  // Decode before the first click, but never resume/play on launch.
  try { prepare(); for (const buffer of buffers.values()) void buffer.catch(() => {}); } catch { /* Audio is optional. */ }
}
