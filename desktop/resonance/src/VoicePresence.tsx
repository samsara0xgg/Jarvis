import { useEffect, useRef } from 'react';

export type Presence = 'standby' | 'listening' | 'thinking' | 'speaking' | 'muted';
export const presenceLabels: Record<Presence, string> = { standby: '呼吸', listening: '聆听', thinking: '思考', speaking: '回应', muted: '静音' };
// Width, amplitude, gathering, flow speed, luminance. Geometry is continuous across states.
const poses: Record<Presence, number[]> = {
  standby: [36, 5.6, .12, .48, .58], listening: [34, 14.5, .3, .85, .85],
  thinking: [25.2, 15.5, .95, 1.1, .78], speaking: [36.7, 16, .2, 1.5, 1],
  muted: [31.2, 1.6, 0, .16, .27],
};
const refinedPoses: Record<Presence, number[]> = {
  standby: [30.5, 5.6, .12, .48, .58], listening: [35.5, 14.5, .3, .85, .85],
  thinking: [23, 15.5, .95, 1.1, .78], speaking: [37, 16, .2, 1.5, 1],
  muted: [26, 1.6, 0, .16, .27],
};
export type MotionVariant = 'classic' | 'refined';
// Width, amplitude, gathering, speed, light each have their own arrival time.
const timing: Record<Presence, { duration: number; delay: number[]; span: number[] }> = {
  standby: { duration: .95, delay: [.12, 0, .12, .1, .1], span: [.85, .72, .82, .9, .76] },
  listening: { duration: .48, delay: [.15, .07, .12, .04, 0], span: [.85, .84, .88, .8, .38] },
  thinking: { duration: .74, delay: [0, .07, .05, 0, 0], span: [.88, .9, .95, .75, .55] },
  speaking: { duration: .64, delay: [.02, .12, .05, .1, 0], span: [.92, .88, .88, .85, .38] },
  muted: { duration: .56, delay: [.2, 0, .12, 0, .05], span: [.8, .45, .75, .55, .65] },
};
const interruption = { duration: .26, delay: [.1, 0, .05, 0, 0], span: [.9, .7, .9, .65, .6] };

export function VoicePresence({ state, color, durationMs = 900, playbackRate = 1, motionPolicy = 'system', variant = 'classic', active = true }: { state: Presence; color: string; durationMs?: number; playbackRate?: number; motionPolicy?: 'system' | 'animate'; variant?: MotionVariant; active?: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const target = useRef({ state, color, durationMs, playbackRate, motionPolicy, variant, active });
  const redraw = useRef<() => void>(() => {});
  useEffect(() => { target.current = { state, color, durationMs, playbackRate, motionPolicy, variant, active }; redraw.current(); }, [state, color, durationMs, playbackRate, motionPolicy, variant, active]);
  useEffect(() => {
    const el = canvas.current!;
    const ctx = el.getContext('2d');
    if (!ctx) return;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const pose = [...(variant === 'refined' ? refinedPoses : poses)[target.current.state]], velocity = pose.map(() => 0);
    const strandPoses = Array.from({ length: 7 }, () => [...pose]);
    const states = Object.keys(poses) as Presence[];
    const weights = pose.map(() => states.map(state => Number(state === target.current.state)));
    let fromWeights = weights.map(w => [...w]), transitionState = target.current.state, duration = durationMs / 1000, elapsed = duration;
    let plan = { duration, delay: pose.map(() => 0), span: pose.map(() => 1) };
    const rgb = [1, 3, 5].map(i => parseInt(target.current.color.slice(i, i + 2), 16));
    let frame = 0, last = 0, time = 0, flow = 0;
    const draw = (now: number) => {
      frame = 0;
      if (document.hidden || !target.current.active) { last = 0; el.dataset.motion = 'paused'; return; }
      const dt = (last ? Math.min((now - last) / 1000, .1) : 1 / 60) * target.current.playbackRate;
      last = now;
      const still = reduced.matches && target.current.motionPolicy === 'system';
      if (!still) time += dt;
      const { state: current, color: tint } = target.current;
      const refined = target.current.variant === 'refined';
      const goal = pose.map(() => 0);
      // Phrase-sized swells leave room to settle; these are demonstration signals, not microphone input.
      const phrase = Math.pow(.5 + .5 * Math.sin(time * 1.65 - .8), 2);
      const syllable = .55 + .45 * Math.sin(time * 7.1) ** 2;
      // Give every state pair a complete timed morph. The easing has zero
      // velocity and acceleration at both ends; interrupted morphs start from
      // the current mixture, while the geometry below retains its momentum.
      if (current !== transitionState) {
        fromWeights = weights.map(w => [...w]);
        plan = refined ? (transitionState === 'speaking' && current === 'listening' ? interruption : timing[current])
          : { duration: .9, delay: pose.map(() => 0), span: pose.map(() => 1) };
        transitionState = current; elapsed = 0;
        duration = Math.max(.1, plan.duration * target.current.durationMs / 900);
      } else elapsed = Math.min(duration, elapsed + dt);
      const progress = still ? 1 : elapsed / duration;
      const blend = still ? 1 : -Math.expm1(-dt / .18);
      weights.forEach((channel, i) => {
        const p = still ? 1 : Math.min(1, Math.max(0, (progress - plan.delay[i]) / plan.span[i]));
        const morph = p ** 3 * (10 + p * (-15 + 6 * p));
        channel.forEach((_, index) => {
          channel[index] = fromWeights[i][index] + (Number(states[index] === current) - fromWeights[i][index]) * morph;
        });
      });
      states.forEach((state, index) => {
        const next = [...(refined ? refinedPoses : poses)[state]];
        if (!still) {
          next[1] *= state === 'speaking' ? .48 + .62 * phrase * syllable
            : state === 'listening' ? .55 + .5 * phrase : 1 + .1 * Math.sin(time * 1.35);
          next[0] -= (state === 'speaking' ? 1.1 : .45) * phrase;
        }
        next.forEach((value, i) => { goal[i] += value * weights[i][index]; });
      });
      for (let i = 0; i < pose.length; i++) {
        if (still) { pose[i] = goal[i]; velocity[i] = 0; }
        else {
          // Exact critically damped step: retain position and momentum across
          // state changes, with a softer release and no frame-rate overshoot.
          const tension = refined ? (i === 1 && goal[i] < pose[i] ? 11 : [12, 14, 11, 13, 16][i])
            : i === 1 && goal[i] < pose[i] ? 6 : 8;
          const offset = pose[i] - goal[i];
          const momentum = velocity[i] + tension * offset;
          const decay = Math.exp(-tension * dt);
          pose[i] = goal[i] + (offset + momentum * dt) * decay;
          velocity[i] = (velocity[i] - tension * momentum * dt) * decay;
        }
      }
      if (!still) flow += dt * pose[3];
      const t = still ? 1.2 : flow;
      const dpr = Math.min(3, window.devicePixelRatio || 1);
      if (el.width !== Math.round(90 * dpr) || el.height !== Math.round(36 * dpr)) {
        el.width = Math.round(90 * dpr); el.height = Math.round(36 * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, 90, 36);
      rgb.forEach((value, i) => { rgb[i] += (parseInt(tint.slice(1 + i * 2, 3 + i * 2), 16) - value) * blend; });
      const shade = rgb.map(Math.round);
      const light = rgb.map(v => Math.round(v + (255 - v) * .48));
      const gradient = ctx.createLinearGradient(7, 0, 83, 0);
      gradient.addColorStop(0, `rgba(${shade},0)`);
      gradient.addColorStop(.2, `rgba(${shade},.65)`);
      gradient.addColorStop(.5, `rgba(${light},1)`);
      gradient.addColorStop(.8, `rgba(${shade},.65)`);
      gradient.addColorStop(1, `rgba(${shade},0)`);
      ctx.strokeStyle = gradient;
      ctx.lineCap = 'round';
      ctx.globalCompositeOperation = 'screen';
      for (let strand = 0; strand < 7; strand++) {
        // Supporting filaments follow the leading lines by a few frames.
        const follow = !refined || still || strand % 3 === 0 ? 1 : -Math.expm1(-dt / (.035 + strand * .006));
        strandPoses[strand].forEach((value, i) => { strandPoses[strand][i] += (pose[i] - value) * follow; });
        const filament = strandPoses[strand];
        ctx.beginPath();
        for (let step = 0; step <= 64; step++) {
          const u = step / 32 - 1;
          const envelope = Math.pow(Math.max(0, 1 - u * u), 1.4);
          const offset = strand * Math.PI * 2 / 7;
          const wave = Math.sin(u * (2.7 + filament[2] * 1.7) + t + offset) * .68
            + Math.sin(u * 5.2 - t * .7 + offset * .55) * .22;
          const x = 45 + u * filament[0];
          const y = 18 + envelope * filament[1] * wave;
          if (step === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.globalAlpha = filament[4] * (strand % 3 === 0 ? .88 : .42);
        ctx.shadowColor = `rgb(${shade})`; ctx.shadowBlur = 3.4;
        ctx.lineWidth = strand % 3 === 0 ? .62 : .38;
        ctx.stroke();
      }
      ctx.shadowBlur = 0;
      el.dataset.motion = still ? 'reduced' : 'running';
      el.dataset.transitionProgress = progress.toFixed(3);
      el.dataset.transitionDuration = String(Math.round(duration * 1000));
      if (!still) frame = requestAnimationFrame(draw);
    };
    const wake = () => { if (!frame) frame = requestAnimationFrame(draw); };
    redraw.current = wake;
    reduced.addEventListener('change', wake);
    document.addEventListener('visibilitychange', wake);
    window.addEventListener('resize', wake);
    wake();
    return () => {
      cancelAnimationFrame(frame); redraw.current = () => {};
      reduced.removeEventListener('change', wake);
      document.removeEventListener('visibilitychange', wake);
      window.removeEventListener('resize', wake);
    };
  }, []);
  return <canvas ref={canvas} className="voice-presence" data-state={state} aria-hidden="true"/>;
}
