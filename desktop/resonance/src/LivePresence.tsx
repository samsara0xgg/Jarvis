import { useEffect, useRef } from 'react';
import type { Presence } from './VoicePresence';

export type RestState = 'standby' | 'thinking' | 'notification' | 'unavailable';
const wavePoses: Record<Presence, number[]> = {
  standby: [30.5, 5.6, .12, .48, .58], listening: [35.5, 14.5, .3, .85, .85],
  thinking: [23, 15.5, .95, 1.1, .78], speaking: [37, 16, .2, 1.5, 1], muted: [26, 1.6, 0, .16, .27],
};

// One persistent geometry and clock drive both the shell and the center shape.
export function LivePresence({ live, restState, presence, color, playbackRate, renderScale = 1, active = true, onProgress }: {
  live: boolean; restState: RestState; presence: Presence; color: string; playbackRate: number; renderScale?: number; active?: boolean;
  onProgress: (progress: number) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const target = useRef({ live, restState, presence, color, playbackRate, renderScale, active, onProgress });
  const redraw = useRef<() => void>(() => {});
  useEffect(() => { target.current = { live, restState, presence, color, playbackRate, renderScale, active, onProgress }; redraw.current(); }, [live, restState, presence, color, playbackRate, renderScale, active, onProgress]);
  useEffect(() => {
    const el = canvas.current!, ctx = el.getContext('2d');
    if (!ctx) return;
    let progress = Number(target.current.live), velocity = 0, last = 0, frame = 0;
    let time = 0, flow = 0;
    let speaking = Number(target.current.presence === 'speaking'), listening = Number(target.current.presence === 'listening');
    const wave = [...wavePoses[target.current.presence]];
    const rgb = [1, 3, 5].map(i => parseInt(target.current.color.slice(i, i + 2), 16));
    const draw = (now: number) => {
      frame = 0;
      if (document.hidden || !target.current.active) { last = 0; el.dataset.motion = 'paused'; return; }
      const dt = Math.min(last ? (now - last) / 1000 : 1 / 60, .05) * target.current.playbackRate;
      last = now; time += dt;
      const goal = Number(target.current.live), tension = target.current.live ? 10 : 12;
      const offset = progress - goal, momentum = velocity + tension * offset, decay = Math.exp(-tension * dt);
      progress = goal + (offset + momentum * dt) * decay;
      velocity = (velocity - tension * momentum * dt) * decay;
      if (Math.abs(progress - goal) < .0001 && Math.abs(velocity) < .001) { progress = goal; velocity = 0; }
      target.current.onProgress(progress);
      const blend = -Math.expm1(-dt / .22);
      wave.forEach((v, i) => { wave[i] += (wavePoses[target.current.presence][i] - v) * blend; });
      rgb.forEach((v, i) => { rgb[i] += (parseInt(target.current.color.slice(1 + 2 * i, 3 + 2 * i), 16) - v) * blend; });
      speaking += (Number(target.current.presence === 'speaking') - speaking) * blend;
      listening += (Number(target.current.presence === 'listening') - listening) * blend;
      flow += dt * wave[3];
      // Render for the final on-screen magnification, with 1.5× supersampling.
      // CSS enlargement alone would stretch a small bitmap and blur each filament.
      const dpr = Math.min(12, (window.devicePixelRatio || 1) * target.current.renderScale * 1.5);
      const segments = Math.max(256, Math.ceil(256 * target.current.renderScale));
      if (el.width !== Math.round(90 * dpr)) { el.width = Math.round(90 * dpr); el.height = Math.round(36 * dpr); }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, 90, 36);
      const tint = rgb.map(Math.round), pale = rgb.map(v => Math.round(v + (255 - v) * .48));
      const radius = 4.5;
      const phrase = Math.pow(.5 + .5 * Math.sin(time * 1.65 - .8), 2);
      const syllable = .55 + .45 * Math.sin(time * 7.1) ** 2;
      const amplitude = wave[1] * ((1 + .1 * Math.sin(time * 1.35)) * (1 - speaking - listening) + (.48 + .62 * phrase * syllable) * speaking + (.55 + .5 * phrase) * listening);
      const point = (u: number, strand: number, r: number) => {
        const theta = -Math.PI / 2 + (u + 1) * Math.PI;
        // Both halves of the ring converge onto the same waveform. Keeping
        // horizontal order avoids a knot or center pinch halfway through.
        const along = Math.cos(theta);
        const envelope = Math.pow(Math.max(0, 1 - along * along), 1.4);
        const o = strand * Math.PI * 2 / 7;
        const y = envelope * amplitude * (Math.sin(along * (2.7 + wave[2] * 1.7) + flow + o) * .68 + Math.sin(along * 5.2 - flow * .7 + o * .55) * .22);
        return [45 + along * (r * (1 - progress) + wave[0] * progress),
          18 + Math.sin(theta) * r * (1 - progress) + y * progress];
      };
      const trace = (strand: number) => {
        ctx.beginPath();
        for (let step = 0; step <= segments; step++) {
          const [x, y] = point(step / segments * 2 - 1, strand, radius);
          if (!step) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.closePath();
      };
      // Selected design 01: a static, flat, borderless nine-pixel disc.
      ctx.save(); trace(0);
      ctx.globalAlpha = (1 - progress) ** 1.6;
      ctx.fillStyle = `rgb(${tint})`; ctx.fill();
      ctx.restore();
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      const gradient = ctx.createLinearGradient(5, 0, 85, 0);
      gradient.addColorStop(0, `rgba(${tint},0)`); gradient.addColorStop(.18, `rgba(${tint},.65)`);
      gradient.addColorStop(.5, `rgba(${pale},1)`); gradient.addColorStop(.82, `rgba(${tint},.65)`); gradient.addColorStop(1, `rgba(${tint},0)`);
      for (let strand = 0; strand < 7; strand++) {
        trace(strand);
        const circleAlpha = 0;
        ctx.globalAlpha = circleAlpha * (1 - progress) + wave[4] * (strand % 3 === 0 ? .88 : .42) * progress;
        ctx.strokeStyle = gradient; ctx.lineWidth = .65 * (1 - progress) + (strand % 3 === 0 ? .62 : .38) * progress;
        ctx.shadowColor = `rgb(${tint})`; ctx.shadowBlur = 1.3 * dpr * progress; ctx.stroke();
      }
      ctx.shadowBlur = 0; ctx.globalAlpha = 1;
      el.dataset.progress = progress.toFixed(5);
      const settled = !target.current.live && progress === 0 && rgb.every((v, i) => Math.abs(v - parseInt(target.current.color.slice(1 + 2 * i, 3 + 2 * i), 16)) < .1);
      el.dataset.motion = settled ? 'still' : 'running';
      if (settled) last = 0; else frame = requestAnimationFrame(draw);
    };
    const wake = () => { if (!frame) frame = requestAnimationFrame(draw); };
    redraw.current = wake;
    document.addEventListener('visibilitychange', wake); window.addEventListener('resize', wake); wake();
    return () => { redraw.current = () => {}; cancelAnimationFrame(frame); document.removeEventListener('visibilitychange', wake); window.removeEventListener('resize', wake); };
  }, []);
  return <canvas ref={canvas} className="live-presence" aria-hidden="true"/>;
}
