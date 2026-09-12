export type FeedbackCue = 'voice-enter' | 'mic-on' | 'mic-off' | 'speaker-on' | 'speaker-off';
const cues: FeedbackCue[] = ['voice-enter', 'mic-on', 'mic-off', 'speaker-on', 'speaker-off'];
let context: AudioContext | undefined;
const buffers = new Map<FeedbackCue, Promise<AudioBuffer>>();
let playing: { source: AudioBufferSourceNode; gain: GainNode } | undefined;
let sequence = 0;
let idleTimer: ReturnType<typeof setTimeout> | undefined;

function prepare() {
  context ??= new AudioContext({ latencyHint: 'interactive' });
  for (const cue of cues) if (!buffers.has(cue)) buffers.set(cue,
    fetch(new URL(`audio/${cue}.wav`, document.baseURI)).then(response => {
      if (!response.ok) throw new Error(`Feedback asset unavailable: ${cue}`);
      return response.arrayBuffer();
    }).then(bytes => context!.decodeAudioData(bytes)));
  return context;
}

export function stopFeedback() {
  sequence++;
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { if (!playing) void context?.suspend().catch(() => {}); }, 35);
  if (!playing || !context) return;
  const { source, gain } = playing;
  gain.gain.cancelScheduledValues(context.currentTime);
  gain.gain.setTargetAtTime(0, context.currentTime, .004);
  source.stop(context.currentTime + .025);
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
    const buffer = await buffers.get(cue)!;
    if (current !== sequence) return;
    const source = audio.createBufferSource(), gain = audio.createGain();
    source.buffer = buffer;
    gain.gain.value = Math.min(1, Math.max(0, volume));
    source.connect(gain).connect(audio.destination);
    source.onended = () => {
      source.disconnect(); gain.disconnect();
      if (playing?.source === source) { playing = undefined; void audio.suspend().catch(() => {}); }
    };
    playing = { source, gain };
    source.start();
    window.dispatchEvent(new CustomEvent('jarvis:feedback', { detail: { cue, duration: buffer.duration, volume: gain.gain.value } }));
  } catch (error) {
    // Feedback failure must never prevent the control from changing state.
    console.warn('Feedback playback unavailable', error);
  }
}

export function warmFeedback() {
  // Decode before the first click, but never resume/play on launch.
  try { prepare(); for (const buffer of buffers.values()) void buffer.catch(() => {}); } catch { /* Audio is optional. */ }
}
