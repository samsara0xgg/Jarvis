// Her, drawn by the desktop's own starCore. At home she rests inside dark glass: only her nebula and her eyes show,
// as in the notch. She surfaces to say something (a ring of her light over the glass, she clears, swells a little
// past her size and settles) and sinks back when it is said. Ported from the Agents lab, round 11.
import { Core, B as GLB, type ExprId } from '../../starCore';
import { dpr, reduced, spring, step, type Spring } from './motion';

// The resting face the character review settled on (short round eyes tipped 9°), so a small her never reads as a
// pause button. Both pages draw it the same way.
// The app owns the shared expression definitions.

// Her nebula at home, as light only: the darker a pixel, the more transparent, so the dark patches of her glass never
// show as a grainy dark crescent over the horizon. An SVG filter the canvas draws through.
const LIGHT_ONLY = 'her-light-only';
function lightOnly() {
  if (!document.getElementById(LIGHT_ONLY)) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '0'); svg.setAttribute('height', '0'); svg.setAttribute('aria-hidden', 'true');
    svg.style.position = 'absolute';
    svg.innerHTML = `<filter id="${LIGHT_ONLY}" color-interpolation-filters="sRGB"><feColorMatrix type="matrix" values="1 0 0 0 0 0 1 0 0 0 0 0 1 0 0 3 3.4 2.6 0 0"/></filter>`;
    document.body.append(svg);
  }
  return `url(#${LIGHT_ONLY})`;
}

export class Her {
  core = new Core('glass');
  shine: Spring = spring(0);
  sc: Spring = spring(.9);
  out = false; hover = false; pressed = false; rippleAt = -1;
  face: ExprId = 'rest'; faceUntil = 0;
  look: [number, number] | null = null;
  private ctx: CanvasRenderingContext2D;
  private eyeCv = document.createElement('canvas');
  private ectx = this.eyeCv.getContext('2d')!;
  private neb = document.createElement('canvas');
  private nctx = this.neb.getContext('2d')!;
  // cv: her canvas, W×W css px; R: her radius in css px; home: whether she rests inside the glass
  constructor(public cv: HTMLCanvasElement, public W: number, public R: number, public home = true) {
    this.ctx = cv.getContext('2d')!;
    cv.style.width = cv.style.height = `${W}px`;
  }
  surface(on: boolean) {
    if (on && !this.out && this.home && !reduced.matches) { this.rippleAt = performance.now(); this.core.effect('ripple', this.rippleAt); this.sc.value = .86; this.sc.velocity = 0; }
    this.out = on;
  }
  say(face: ExprId, ms: number) { this.face = face; this.faceUntil = performance.now() + ms; }
  hop(k = .12) { this.core.hop(performance.now(), k); }
  shake() { this.core.effect('shake', performance.now()); }
  // Her light, "r g b", for her own pool of light only; the window's accent never follows it.
  light() { return this.core.light.glow.map(v => Math.round(v * 255)).join(' '); }
  frame(now: number, dt: number) {
    const { R, W, core } = this;
    const want = !this.home ? 1 : reduced.matches ? (this.out || this.hover ? 1 : 0) : this.out || this.pressed ? 1 : this.hover ? .5 : 0;
    step(this.shine, want, want > this.shine.value ? 1.5 : .7, 1, dt);
    step(this.sc, 1, 2.1, .45, dt);
    const a = Math.max(0, Math.min(1, this.shine.value)), k = this.sc.value;
    core.update(now, dt, { expr: now < this.faceUntil ? this.face : 'rest', look: this.look, still: reduced.matches, pressed: this.pressed });
    const d = dpr(), N = Math.round(W * d), c = this.ctx, m = W / 2;
    if (this.cv.width !== N) this.cv.width = this.cv.height = N;
    c.setTransform(d, 0, 0, d, 0, 0); c.clearRect(0, 0, W, W);
    c.translate(m, m); c.scale(k, k); c.translate(-m, -m);
    const S = Math.round(2 * GLB * R * d), [jx, jy, bx, by] = core.pose(1);
    if (core.render(S, R * d < 20 ? 2 : 3)) {
      // Inside the glass only her nebula glows through, soft-edged; on navy it mostly adds light.
      if (a < .99) {
        const n = Math.min(S, 256);
        if (this.neb.width !== n) this.neb.width = this.neb.height = n;
        const nc = this.nctx;
        nc.setTransform(1, 0, 0, 1, 0, 0); nc.clearRect(0, 0, n, n);
        nc.setTransform(1, 0, 0, 1, n / 2, n / 2); core.inside(nc, n / 2 / GLB, S);
        nc.globalCompositeOperation = 'destination-in';
        const g = nc.createRadialGradient(0, 0, 0, 0, 0, n / 2);
        g.addColorStop(0, '#000'); g.addColorStop(.4, 'rgba(0,0,0,.8)'); g.addColorStop(.72, 'rgba(0,0,0,0)');
        nc.fillStyle = g; nc.fillRect(-n / 2, -n / 2, n, n);
        // on black, so its soft edge is dark too and the filter below fades it out with the rest of the dark
        nc.globalCompositeOperation = 'destination-over'; nc.fillStyle = '#000'; nc.fillRect(-n / 2, -n / 2, n, n); nc.globalCompositeOperation = 'source-over';
        // It moves with her (a hop, a lean toward where she looks) so her eyes stay on it, and its soft edge ends
        // inside her canvas, never cut off by it.
        const r = GLB * R * 1.6, nx = m + jx * R - r, ny = m + jy * R - r;
        c.save(); c.filter = lightOnly(); c.globalCompositeOperation = 'lighter'; c.globalAlpha = 1 - a; c.drawImage(this.neb, nx, ny, 2 * r, 2 * r);
        c.globalAlpha = .9 * (1 - a); c.drawImage(this.neb, nx, ny, 2 * r, 2 * r); c.restore();
      }
      if (a > .01) {
        c.save(); c.globalAlpha = a;
        c.save(); c.translate(m, m); core.orbit(c, R, -1); c.restore();
        c.save(); c.translate(m + jx * R, m + jy * R); c.scale(bx, by); core.inside(c, R, S); core.glass(c, R, S); c.restore();
        c.restore();
      }
    }
    // Where she surfaces, a ring of her light runs out over the glass, gone before it reaches her canvas's edge.
    const rk = this.rippleAt < 0 ? 1 : (now - this.rippleAt) / 900;
    if (rk < 1) {
      const e = 1 - (1 - rk) ** 3, rgb = core.light.glow.map(v => Math.round(v * 255)).join(',');
      c.save(); c.strokeStyle = `rgba(${rgb},${.45 * (1 - rk) ** 2})`; c.lineWidth = 1.2 * (1 - rk * .6);
      c.beginPath(); c.arc(m, m, Math.min(m - 3, R * (1 + .5 * e)) / k, 0, Math.PI * 2); c.stroke(); c.restore();
    } else this.rippleAt = -1;
    const E = Math.ceil(3.8 * R * d * 1.1);
    if (this.eyeCv.width !== E) this.eyeCv.width = this.eyeCv.height = E;
    const ec = this.ectx;
    ec.setTransform(1, 0, 0, 1, 0, 0); ec.clearRect(0, 0, E, E);
    ec.setTransform(d * k, 0, 0, d * k, E / 2, E / 2); ec.translate(jx * R, jy * R); ec.scale(bx, by); core.eyes(ec, R);
    const [glow, blur] = core.glow();
    c.save(); c.setTransform(1, 0, 0, 1, 0, 0); c.shadowColor = glow; c.shadowBlur = blur * R * d; c.drawImage(this.eyeCv, m * d - E / 2, m * d - E / 2); c.restore();
    c.save(); c.translate(m, m); c.globalAlpha = this.home ? a : 1; core.orbit(c, R, 1); c.globalAlpha = 1; core.particles(c, R, d); c.restore();
  }
}
