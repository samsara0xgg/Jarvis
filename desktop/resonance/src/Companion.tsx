import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { IconContext, Keyboard, Paperclip, ArrowUp, Microphone, Stop } from '@phosphor-icons/react';
import { CompanionBall, R, type BallHandle, type Lobe, type Mood, type Place, type Point } from './CompanionBall';
import { DashboardPreview } from './DashboardPreview';
import { playFeedback, stopFeedback, warmFeedback, type FeedbackCue } from './feedback';
import { usePreferences } from './preferences';
import './companion.css';

type Placement = { topInset: number; notchWidth: number; surfaceWidth: number; displayId?: number };
type Rect = { x: number; y: number; w: number; h: number };
type Zone = 'none' | 'lobe' | 'ball';
const within = (p: Point, r: Rect) => p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h;
const PANEL = 300;
// Prototype script: every transcript and reply below is simulated.
const HEARD = '把今天的任务整理一下';

function layout({ topInset, notchWidth, surfaceWidth: width }: Placement) {
  const center = width / 2, notchLeft = center - notchWidth / 2;
  // Without a notch she lives dead centre in a pill; its two wings open the Dashboard, as the camera does beside a notch.
  const lobe: Lobe = notchWidth ? { left: notchLeft - 64, right: notchLeft + 24, height: topInset, notched: true }
    : { left: center - 66, right: center + 66, height: topInset, notched: false };
  const x = notchWidth ? notchLeft - 32 : center, out = { x, y: topInset + R + 14 }, panelTop = topInset + 44;
  const anchors: Record<Place, Point> = { home: { x, y: topInset / 2 }, peek: { x, y: topInset + R * .1 }, out, dock: { x: center, y: panelTop - R * .5 } };
  return { width, lobe, anchors, out, center, panelTop, zones: {
    lobe: notchWidth ? { x: lobe.left - 26, y: 0, w: notchLeft - lobe.left + 26, h: topInset + 16 } : { x: center - 36, y: 0, w: 72, h: topInset + 16 },
    ball: { x: x - R - 12, y: topInset, w: 2 * R + 24, h: out.y + R + 12 - topInset },
    chip: { x: x + R + 4, y: out.y - 18, w: 44, h: 36 },
    dash: notchWidth ? [{ x: notchLeft, y: 0, w: notchWidth, h: topInset + 4 }]
      : [{ x: lobe.left, y: 0, w: 30, h: topInset + 4 }, { x: center + 36, y: 0, w: 30, h: topInset + 4 }],
    panel: { x: center - PANEL / 2 - 10, y: 0, w: PANEL + 20, h: panelTop + 470 },
  } };
}

export function Companion() {
  const [placement, setPlacement] = useState<Placement>({ topInset: 32, notchWidth: 185, surfaceWidth: 640 });
  // Moving to another screen: she sinks into this island, then the window moves and she comes up in the new one.
  const [moving, setMoving] = useState(false);
  const shownDisplay = useRef<number | undefined>(undefined), arriving = useRef(false);
  useEffect(() => {
    if (!window.jarvis) return;
    const receive = (value: Placement | null) => {
      if (!value) return;
      if (shownDisplay.current !== undefined && value.displayId !== shownDisplay.current) arriving.current = true;
      shownDisplay.current = value.displayId;
      setPlacement(value); setMoving(false);
    };
    void window.jarvis.placement().then(receive);
    return window.jarvis.onPlacement(receive);
  }, []);
  const geo = useMemo(() => layout(placement), [placement]);
  const [preferences] = usePreferences();
  const feedback = (cue: FeedbackCue) => { if (preferences.feedbackEnabled) void playFeedback(cue, preferences.feedbackVolume); };
  useEffect(() => { warmFeedback(); return stopFeedback; }, []);
  const [zone, setZone] = useState<Zone>('none');
  const [dashboard, setDashboard] = useState(false);
  const [composer, setComposer] = useState(false);
  const [draft, setDraft] = useState('');
  const [voice, setVoice] = useState<'off' | 'listening' | 'speaking'>('off');
  const [caption, setCaption] = useState('');
  const [hearing, setHearing] = useState(false);
  const [reply, setReply] = useState({ text: '', shown: 0 });
  const [talking, setTalking] = useState(false);
  const [pressed, setPressed] = useState(false);
  const busy = composer || voice !== 'off' || !!reply.text;
  const place: Place = moving ? 'home' : dashboard ? 'dock' : busy || zone === 'ball' ? 'out' : zone === 'lobe' ? 'peek' : 'home';
  const mood: Mood = voice === 'listening' ? 'listening' : voice === 'speaking' || talking ? 'speaking' : 'idle';
  const chip = place === 'out' && zone === 'ball' && !busy;
  const live = useRef({ geo, dashboard, chip, composer });
  live.current = { geo, dashboard, chip, composer };
  const ball = useRef<BallHandle | null>(null), look = useRef<Point | null>(null), cursor = useRef<Point>({ x: -1e4, y: -1e4 });
  const input = useRef<HTMLInputElement>(null), root = useRef<HTMLElement>(null), pressing = useRef(false);

  const zoneTimer = useRef<ReturnType<typeof setTimeout>>(undefined), pending = useRef<Zone>('none');
  const dashTimer = useRef<ReturnType<typeof setTimeout>>(undefined), dashEntered = useRef(false), interactive = useRef(false);
  const script = useRef<ReturnType<typeof setTimeout>[]>([]);
  const after = (ms: number, run: () => void) => { script.current.push(setTimeout(run, ms)); };
  const stopScript = () => { script.current.forEach(clearTimeout); script.current = []; };
  useEffect(() => stopScript, []);
  const say = (text: string, done: () => void) => {
    setReply({ text, shown: 0 }); setTalking(true);
    for (let i = 1; i <= text.length; i++) after(i * 115, () => setReply({ text, shown: i }));
    after(text.length * 115 + 450, () => { setTalking(false); done(); });
  };
  const listen = (scripted: boolean) => {
    stopScript(); setVoice('listening'); setCaption(''); setHearing(false); setReply({ text: '', shown: 0 }); setTalking(false);
    if (!scripted) return;
    const end = 650 + HEARD.length * 115;
    after(650, () => setHearing(true));
    for (let i = 1; i <= HEARD.length; i++) after(650 + i * 115, () => setCaption(HEARD.slice(0, i)));
    after(end + 250, () => setHearing(false));
    after(end + 800, () => { setVoice('speaking'); say('好，我来整理。', () => listen(false)); });
  };
  const endVoice = () => { stopScript(); feedback('voice-exit'); setVoice('off'); setCaption(''); setHearing(false); setReply({ text: '', shown: 0 }); setTalking(false); };
  const closeComposer = () => { setComposer(false); void window.jarvis?.focus(false); };
  // Poke: start a voice turn, interrupt playback, or end the session.
  const poke = () => {
    if (voice === 'off') { closeComposer(); feedback('voice-enter'); listen(true); }
    else if (voice === 'speaking') listen(false);
    else endVoice();
  };
  const press = () => { pressing.current = true; setPressed(true); };
  const release = () => { if (!pressing.current) return; pressing.current = false; setPressed(false); poke(); };
  const cancel = () => { pressing.current = false; setPressed(false); };

  const measure = useRef<CanvasRenderingContext2D | null>(null);
  // She watches the caret while you type.
  const aimAtCaret = () => {
    const el = input.current, ctx = measure.current ??= document.createElement('canvas').getContext('2d');
    if (!el || !ctx) return;
    const style = getComputedStyle(el), r = el.getBoundingClientRect(), pad = parseFloat(style.paddingLeft);
    ctx.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
    const width = ctx.measureText(el.value.slice(0, el.selectionStart ?? el.value.length)).width;
    look.current = { x: Math.min(r.right - pad, r.left + pad + width - el.scrollLeft), y: r.top + r.height / 2 };
  };
  const openComposer = () => {
    stopScript(); setReply({ text: '', shown: 0 }); setTalking(false); setComposer(true);
    void window.jarvis?.focus(true).then(() => requestAnimationFrame(() => { input.current?.focus(); aimAtCaret(); }));
  };
  const send = () => {
    const text = draft.trim();
    if (!text) return;
    setDraft(''); closeComposer(); stopScript();
    after(280, () => say(text.includes('整理') ? '好，我来整理。' : '收到，我来处理。', () => after(1800, () => setReply({ text: '', shown: 0 }))));
  };
  const openDashboard = (hovered: boolean) => { dashEntered.current = hovered; setDashboard(true); setComposer(false); void window.jarvis?.focus(false); };
  useEffect(() => window.jarvis?.onCommand(command => { if (command === 'dashboard') openDashboard(false); }), []);
  useEffect(() => window.jarvis?.onDisplayLeave(() => {
    closeComposer(); setDashboard(false); clearTimeout(zoneTimer.current); pending.current = 'none'; setZone('none'); setMoving(true);
    // Long enough to look up, fly home and merge before the window leaves this screen.
    setTimeout(() => window.jarvis?.displayReady(), 520);
  }), []);
  // The ball reads the new anchors in its own effect, which runs before this one.
  useEffect(() => { if (arriving.current) { arriving.current = false; ball.current?.arrive(); } }, [geo]);
  useEffect(() => {
    const blur = () => { if (live.current.composer) closeComposer(); };
    window.addEventListener('blur', blur);
    return () => window.removeEventListener('blur', blur);
  }, []);

  // Approach, peek, hover and the dashboard all come from the native cursor feed.
  // Only our own shapes take clicks; everything else passes through to the desktop.
  // The island counts: it is opaque, and a click on it must not reach a menu bar item hidden behind it.
  const refreshHit = () => {
    const p = cursor.current, { lobe } = live.current.geo;
    const island = p.y >= 0 && p.y <= lobe.height && p.x >= lobe.left - 6 && p.x <= lobe.right + (lobe.notched ? 0 : 6);
    const hit = island || !!document.elementFromPoint(p.x, p.y)?.closest('[data-hit]');
    if (hit !== interactive.current && !pressing.current) { interactive.current = hit; window.jarvis?.passthrough(!hit); }
  };
  useEffect(() => window.jarvis?.onCursor(point => {
    const { geo, dashboard, chip, composer } = live.current, z = geo.zones;
    cursor.current = point;
    if (!composer) look.current = point;
    refreshHit();
    const next: Zone = within(point, z.ball) || (chip && within(point, z.chip)) ? 'ball' : within(point, z.lobe) ? 'lobe' : 'none';
    if (next !== pending.current) {
      pending.current = next; clearTimeout(zoneTimer.current);
      zoneTimer.current = setTimeout(() => setZone(next), next === 'ball' ? 0 : next === 'lobe' ? 90 : 600);
    }
    const over = z.dash.some(r => within(point, r)) || (dashboard && within(point, z.panel));
    if (dashboard) {
      if (over) { dashEntered.current = true; clearTimeout(dashTimer.current); dashTimer.current = undefined; }
      else if (dashEntered.current && !dashTimer.current) dashTimer.current = setTimeout(() => { dashTimer.current = undefined; setDashboard(false); }, 450);
    } else if (over && !dashTimer.current) {
      dashTimer.current = setTimeout(() => { dashTimer.current = undefined; if (live.current.geo.zones.dash.some(r => within(cursor.current, r))) openDashboard(true); }, 200);
    } else if (!over && dashTimer.current) { clearTimeout(dashTimer.current); dashTimer.current = undefined; }
  }), []);
  useEffect(() => () => { clearTimeout(zoneTimer.current); clearTimeout(dashTimer.current); }, []);
  useEffect(refreshHit, [place, chip, composer, dashboard, voice, reply.text]);

  // Native frosted glass behind every visible panel, following its transitions.
  const kickGlass = useRef(() => {});
  useLayoutEffect(() => {
    const el = root.current!;
    let frame = 0, deadline = 0, sent = '';
    const update = () => {
      const rects = [...el.querySelectorAll<HTMLElement>('[data-glass]')].map(node => {
        const r = node.getBoundingClientRect();
        let opacity = 1;
        for (let n: HTMLElement | null = node; n && n !== el; n = n.parentElement) { const cs = getComputedStyle(n); opacity *= cs.visibility === 'hidden' ? 0 : Number(cs.opacity); }
        return { x: r.x, y: r.y, width: r.width, height: r.height, radius: Number(node.dataset.glass) * r.width / (node.offsetWidth || 1), opacity };
      }).filter(r => r.opacity > .01 && r.width > 0 && r.height > 0);
      const key = JSON.stringify(rects);
      if (key !== sent) { sent = key; window.jarvis?.material(rects, 1); }
    };
    const tick = () => { update(); frame = performance.now() < deadline ? requestAnimationFrame(tick) : 0; };
    const kick = () => { deadline = performance.now() + 700; if (!frame) frame = requestAnimationFrame(tick); };
    kickGlass.current = kick;
    const observer = new ResizeObserver(kick);
    el.querySelectorAll('[data-glass]').forEach(node => observer.observe(node));
    el.addEventListener('transitionrun', kick);
    kick();
    return () => { cancelAnimationFrame(frame); observer.disconnect(); el.removeEventListener('transitionrun', kick); };
  }, []);
  useEffect(() => kickGlass.current(), [place, chip, composer, dashboard, voice, reply.text, caption]);

  const { out } = geo;
  const strip = place === 'out' && voice === 'listening', bubble = place === 'out' && !!reply.text;
  return <IconContext.Provider value={{ size: 16, weight: 'regular' }}>
    <main ref={root} className="companion" style={{ '--mint': preferences.themeColor } as React.CSSProperties}>
      <div className={`companion-chip ${chip ? 'is-open' : ''}`} data-hit={chip || undefined} data-glass="9" style={{ left: out.x + R + 12, top: out.y - 13 }}>
        <button aria-label="文字输入" tabIndex={chip ? 0 : -1} onClick={openComposer}><Keyboard/></button>
      </div>
      <form className={`companion-composer ${composer && place === 'out' ? 'is-open' : ''}`} data-hit={composer || undefined} data-glass="17"
        style={{ left: out.x - PANEL / 2, top: out.y + R + 11 }} inert={!composer} onTransitionEnd={aimAtCaret}
        onSubmit={event => { event.preventDefault(); send(); }}>
        <button type="button" className="composer-attach" disabled aria-label="添加附件（原型未接入）"><Paperclip/></button>
        <input ref={input} aria-label="文字输入" placeholder="和她说点什么…" value={draft}
          onChange={event => { setDraft(event.target.value); ball.current?.nudge(); requestAnimationFrame(aimAtCaret); }}
          onSelect={aimAtCaret} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); closeComposer(); } }}/>
        <button type="submit" className="composer-send" disabled={!draft.trim()} aria-label="发送"><ArrowUp weight="bold"/></button>
      </form>
      <div className={`companion-strip ${strip ? 'is-open' : ''} ${hearing ? 'is-hearing' : ''}`} data-hit={strip || undefined} data-glass="19"
        style={{ left: out.x, top: out.y + R + 11 }} inert={!strip} role="status">
        <span className="strip-mic"><Microphone size={14} weight="fill"/></span>
        <span className={`strip-text ${caption ? '' : 'is-empty'}`}>{caption || '在听…'}</span>
        <button className="strip-stop" aria-label="结束语音" onClick={endVoice}><Stop size={11} weight="fill"/></button>
      </div>
      <div className={`companion-bubble ${bubble ? 'is-open' : ''}`} data-glass="18" style={{ left: out.x, top: out.y + R + 11 }} role="status">
        <span className="bubble-text"><span className="bubble-ghost">{reply.text}</span><span>{reply.text.slice(0, reply.shown)}</span></span>
      </div>
      <div className={`companion-dashboard ${dashboard ? 'is-open' : ''}`} data-hit={dashboard || undefined} data-glass="24"
        style={{ left: geo.center - PANEL / 2, top: geo.panelTop }} inert={!dashboard}>
        <DashboardPreview embedded visible={dashboard} shown={dashboard} onClose={() => setDashboard(false)}/>
      </div>
      <CompanionBall width={geo.width} height={placement.topInset + 560} lobe={geo.lobe} look={look} handle={ball}
        target={{ place, mood, pressed, hearing, anchors: geo.anchors }}
        label={voice === 'off' ? '戳一下，开始语音（演示）' : voice === 'speaking' ? '戳一下，打断播报' : '戳一下，结束语音'}
        onPress={press} onRelease={release} onCancel={cancel} onMove={refreshHit}/>
    </main>
  </IconContext.Provider>;
}
