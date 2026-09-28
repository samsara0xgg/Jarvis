// Her, drawn by the desktop's own starCore. At home she rests inside dark glass: only her nebula and her eyes show,
// as in the notch. She surfaces to say something (a ring of her light over the glass, she clears, swells a little
// past her size and settles) and sinks back when it is said. Ported from the Agents lab, round 11.
import { Core, B as GLB, type ExprId } from '../../starCore';
import { dpr, reduced, spring, step, type Spring } from './motion';

// The resting face the character review settled on (short round eyes tipped 9°), so a small her never reads as a
// pause button. Both pages draw it the same way.
// The app owns the shared expression definitions.

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
    core.update(now, dt, { expr: now < this.faceUntil ? this.face : 'rest', look: this.look, still: reduced.matches, pressed: this.pressed, charge: 0 });
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
        nc.fillStyle = g; nc.fillRect(-n / 2, -n / 2, n, n); nc.globalCompositeOperation = 'source-over';
        const r = GLB * R * 1.9;
        c.save(); c.globalAlpha = .3 * (1 - a); c.drawImage(this.neb, m - r, m - r, 2 * r, 2 * r);
        c.globalCompositeOperation = 'lighter'; c.globalAlpha = 1 - a; c.drawImage(this.neb, m - r, m - r, 2 * r, 2 * r);
        c.globalAlpha = .9 * (1 - a); c.drawImage(this.neb, m - r, m - r, 2 * r, 2 * r); c.restore();
      }
      if (a > .01) {
        c.save(); c.globalAlpha = a;
        c.save(); c.translate(m, m); core.orbit(c, R, -1); c.restore();
        c.save(); c.translate(m + jx * R, m + jy * R); c.scale(bx, by); core.inside(c, R, S); core.glass(c, R, S); c.restore();
        c.restore();
      }
    }
    // Where she surfaces, a ring of her light runs out over the glass.
    const rk = this.rippleAt < 0 ? 1 : (now - this.rippleAt) / 900;
    if (rk < 1) {
      const e = 1 - (1 - rk) ** 3, rgb = core.light.glow.map(v => Math.round(v * 255)).join(',');
      c.save(); c.strokeStyle = `rgba(${rgb},${.5 * (1 - rk) ** 2})`; c.lineWidth = 1.3 * (1 - rk * .6);
      c.beginPath(); c.arc(m, m, R * (1 + .85 * e) / k, 0, Math.PI * 2); c.stroke(); c.restore();
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
