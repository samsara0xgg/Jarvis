import { useEffect, useRef } from 'react';

export type Presence = 'standby' | 'listening' | 'thinking' | 'speaking' | 'muted';
export const presenceLabels: Record<Presence, string> = { standby: '呼吸', listening: '聆听', thinking: '思考', speaking: '回应', muted: '静音' };
// Width, amplitude, gathering, flow speed, luminance. Geometry is continuous across states.
const poses: Record<Presence, number[]> = {
  standby: [36, 5.6, .12, .48, .58], listening: [34, 14.5, .3, .85, .85],
  thinking: [25.2, 15.5, .95, 1.1, .78], speaking: [36.7, 16, .2, 1.5, 1],
  muted: [31.2, 1.6, 0, .16, .27],
};

export function VoicePresence({ state, color }: { state: Presence; color: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const target = useRef({ state, color });
  const redraw = useRef<() => void>(() => {});
  useEffect(() => { target.current = { state, color }; redraw.current(); }, [state, color]);
  useEffect(() => {
    const el = canvas.current!;
    const ctx = el.getContext('2d');
    if (!ctx) return;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const pose = [...poses[target.current.state]], velocity = pose.map(() => 0);
    let frame = 0, last = 0, time = 0, flow = 0;
    const draw = (now: number) => {
      frame = 0;
      if (document.hidden) { last = 0; return; }
      const dt = last ? Math.min((now - last) / 1000, .035) : 1 / 60;
      last = now;
      const still = reduced.matches;
      if (!still) time += dt;
      const { state: current, color: tint } = target.current;
      const goal = [...poses[current]];
      // Phrase-sized swells leave room to settle; these are demonstration signals, not microphone input.
      const phrase = Math.pow(.5 + .5 * Math.sin(time * 1.65 - .8), 2);
      const syllable = .55 + .45 * Math.sin(time * 7.1) ** 2;
      if (!still) {
        goal[1] *= current === 'speaking' ? .48 + .62 * phrase * syllable
          : current === 'listening' ? .55 + .5 * phrase : 1 + .1 * Math.sin(time * 1.35);
        goal[0] -= (current === 'speaking' ? 1.1 : .45) * phrase;
      }
      for (let i = 0; i < pose.length; i++) {
        if (still) { pose[i] = goal[i]; velocity[i] = 0; }
        else {
          // Damped tension: a responsive pull followed by a slower, soft release.
          const stiffness = i === 1 && goal[i] < pose[i] ? 38 : 64;
          velocity[i] += ((goal[i] - pose[i]) * stiffness - velocity[i] * 13) * dt;
          pose[i] += velocity[i] * dt;
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
      const rgb = [1, 3, 5].map(i => parseInt(tint.slice(i, i + 2), 16));
      const light = rgb.map(v => Math.round(v + (255 - v) * .48));
      const gradient = ctx.createLinearGradient(7, 0, 83, 0);
      gradient.addColorStop(0, `rgba(${rgb},0)`);
      gradient.addColorStop(.2, `rgba(${rgb},.65)`);
      gradient.addColorStop(.5, `rgba(${light},1)`);
      gradient.addColorStop(.8, `rgba(${rgb},.65)`);
      gradient.addColorStop(1, `rgba(${rgb},0)`);
      ctx.strokeStyle = gradient;
      ctx.lineCap = 'round';
      ctx.globalCompositeOperation = 'screen';
      for (let strand = 0; strand < 7; strand++) {
        ctx.beginPath();
        for (let step = 0; step <= 64; step++) {
          const u = step / 32 - 1;
          const envelope = Math.pow(Math.max(0, 1 - u * u), 1.4);
          const offset = strand * Math.PI * 2 / 7;
          const wave = Math.sin(u * (2.7 + pose[2] * 1.7) + t + offset) * .68
            + Math.sin(u * 5.2 - t * .7 + offset * .55) * .22;
          const x = 45 + u * pose[0];
          const y = 18 + envelope * pose[1] * wave;
          if (step === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.globalAlpha = pose[4] * (strand % 3 === 0 ? .88 : .42);
        ctx.shadowColor = tint; ctx.shadowBlur = 3.4;
        ctx.lineWidth = strand % 3 === 0 ? .62 : .38;
        ctx.stroke();
      }
      ctx.shadowBlur = 0;
      el.dataset.motion = still ? 'reduced' : 'running';
      if (!still) frame = requestAnimationFrame(draw);
    };
    const wake = () => { cancelAnimationFrame(frame); last = 0; frame = requestAnimationFrame(draw); };
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
