import { useEffect, useRef } from 'react';
import type { Presence } from './VoicePresence';

export type OrbitState = Presence | 'notification';
export const orbitStates: { value: OrbitState; label: string; detail: string }[] = [
  { value: 'standby', label: '待机', detail: '缓慢呼吸，安静地保持在线。' },
  { value: 'listening', label: '聆听', detail: '光点驻留，圆环轻轻起伏，表示正在接收。' },
  { value: 'thinking', label: '思考', detail: '光点沿轨道持续前行，无需语音也能表达处理中。' },
  { value: 'speaking', label: '回应', detail: '圆环随示意节奏起伏，亮弧随之增强。' },
  { value: 'notification', label: '有消息', detail: '两次柔和的亮度提示，随后留出安静间隔。' },
  { value: 'muted', label: '静音', detail: '光点隐去，留下低亮度的完整圆环。' },
];
// Brightness, angular speed, breathing depth, arc length, orbit-dot opacity.
const poses: Record<OrbitState, number[]> = {
  standby: [.52, 0, .025, 1.05, .6],
  listening: [.9, .04, .06, 2.3, 1],
  thinking: [.85, 1.35, .015, 3.6, 1],
  speaking: [1, .32, .105, 2.85, .9],
  notification: [.9, .12, .04, 1.6, 1],
  muted: [.23, 0, 0, .1, 0],
};

/** Presentation only: state and color are supplied by the caller. */
export function OrbitPresence({ state, color = '#a8b5ff', size = 32, playbackRate = 1 }: {
  state: OrbitState; color?: string; size?: number; playbackRate?: number;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const target = useRef({ state, color, playbackRate });
  useEffect(() => { target.current = { state, color, playbackRate }; }, [state, color, playbackRate]);
  useEffect(() => {
    const el = canvas.current!;
    const ctx = el.getContext('2d');
    if (!ctx) return;
    const pose = [...poses[target.current.state]];
    const rgb = [1, 3, 5].map(i => parseInt(target.current.color.slice(i, i + 2), 16));
    let frame = 0, last = 0, time = 0, angle = -.8;
    let speaking = Number(target.current.state === 'speaking');
    let message = Number(target.current.state === 'notification');
    const draw = (now: number) => {
      frame = 0;
      if (document.hidden) { last = 0; return; }
      const dt = Math.min(last ? (now - last) / 1000 : 1 / 60, .05) * target.current.playbackRate;
      last = now; time += dt;
      const blend = -Math.expm1(-dt / .24);
      pose.forEach((v, i) => { pose[i] = v + (poses[target.current.state][i] - v) * blend; });
      speaking += (Number(target.current.state === 'speaking') - speaking) * blend;
      message += (Number(target.current.state === 'notification') - message) * blend;
      rgb.forEach((v, i) => { rgb[i] = v + (parseInt(target.current.color.slice(1 + i * 2, 3 + i * 2), 16) - v) * blend; });
      angle += dt * pose[1];
      const dpr = Math.min(3, window.devicePixelRatio || 1);
      const px = Math.round(size * dpr);
      if (el.width !== px) { el.width = px; el.height = px; }
      ctx.setTransform(px / 32, 0, 0, px / 32, 0, 0);
      ctx.clearRect(0, 0, 32, 32);
      const tint = rgb.map(Math.round);
      const pale = rgb.map(v => Math.round(v + (255 - v) * .58));
      const phase = time % 4.8;
      const ping = Math.exp(-(((phase - .65) / .22) ** 2)) + .8 * Math.exp(-(((phase - 1.3) / .22) ** 2));
      const rhythm = Math.sin(time * 4.8) * .55 + Math.sin(time * 7.3) * .25;
      const breath = Math.sin(time * 1.45);
      const wave = breath * (1 - speaking) + rhythm * speaking;
      const radius = 10.6 * (1 + pose[2] * wave);
      const light = pose[0] * (1 + pose[2] * 2 * wave) * (1 - message * .32 + message * ping * .45);
      const stroke = (r: number, start: number, end: number, opacity: number, width: number, bright = false) => {
        ctx.beginPath(); ctx.arc(16, 16, r, start, end);
        ctx.strokeStyle = `rgba(${bright ? pale : tint},${Math.min(1, opacity)})`;
        ctx.lineWidth = width; ctx.stroke();
      };
      const glow = ctx.createRadialGradient(16, 16, 6, 16, 16, 15.5);
      glow.addColorStop(0, `rgba(${tint},0)`);
      glow.addColorStop(.58, `rgba(${tint},${light * .07})`);
      glow.addColorStop(1, `rgba(${tint},0)`);
      ctx.fillStyle = glow; ctx.fillRect(0, 0, 32, 32);
      ctx.lineCap = 'round';
      stroke(13, 0, Math.PI * 2, .24 * light, .4);
      stroke(radius, 0, Math.PI * 2, .32 * light, .4);
      // A tapered luminous trail is drawn on the same continuous orbit in every state.
      for (let i = 0; i < 56; i++) {
        const p = i / 56;
        stroke(radius, angle - pose[3] * (1 - p), angle - pose[3] * (1 - (i + 1) / 56) + .005,
          light * (.08 + .76 * p * p), .46);
      }
      stroke(13, angle + 1.1, angle + 2.6 + pose[3] * .12, .46 * light, .44, true);
      const x = 16 + Math.cos(angle) * radius, y = 16 + Math.sin(angle) * radius;
      const dotGlow = ctx.createRadialGradient(x, y, 0, x, y, 3.2);
      dotGlow.addColorStop(0, `rgba(${tint},${.6 * pose[4]})`);
      dotGlow.addColorStop(1, `rgba(${tint},0)`);
      ctx.fillStyle = dotGlow; ctx.fillRect(x - 3.2, y - 3.2, 6.4, 6.4);
      ctx.beginPath(); ctx.arc(x, y, .85, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${pale},${pose[4]})`; ctx.fill();
      el.dataset.motion = 'running';
      frame = requestAnimationFrame(draw);
    };
    const wake = () => { if (!frame) frame = requestAnimationFrame(draw); };
    document.addEventListener('visibilitychange', wake); wake();
    return () => { cancelAnimationFrame(frame); document.removeEventListener('visibilitychange', wake); };
  }, [size]);
  return <canvas ref={canvas} className="orbit-presence" data-state={state} style={{ width: size, height: size }} role="img" aria-label={`${orbitStates.find(s => s.value === state)?.label}圆环`}/>;
}
