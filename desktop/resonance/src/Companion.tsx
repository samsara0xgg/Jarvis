import { useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react';
import { IconContext, Keyboard, Paperclip, ArrowUp, Microphone, Stop } from '@phosphor-icons/react';
import { CompanionBall, HOLD_MS, R, type BallHandle, type Lobe, type Place, type Point } from './CompanionBall';
import { EXPRESSIONS, PREVIEW, SKINS, SKIN_KEYS, TAKES, isSkin, pick, type ExprId, type Skin } from './starCore';
import { DashboardPreview } from './DashboardPreview';
import { AroundDashboard } from './AroundDashboard';
import { playFeedback, stopFeedback, warmFeedback, type FeedbackCue } from './feedback';
import { usePreferences } from './preferences';
import { initialState, plain, reducer, visible } from './model';
import { connect, type Runtime } from './runtime';
import { usePlugins } from './PluginPanel';
import { AgentWing, isMarkLook, wingSlots, type MarkLook } from './AgentMarks';
import { answerRequest, type ShownAgent } from './agents';
import { NoticeCard, noticeCue, useNotices, type Notice } from './Notices';
import { tr, useCompanionSettings, type L } from './companionSettings';
import type { Controls as DashControls, Look } from './SettingsPage';
import './companion.css';

type Placement = { topInset: number; notchWidth: number; surfaceWidth: number; displayId?: number };
type Rect = { x: number; y: number; w: number; h: number };
type Zone = 'none' | 'lobe' | 'ball';
const within = (p: Point, r: Rect) => p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h;
const PANEL = 300;
// With a `port` she is live: the daemon's turns drive her voice, faces and words. Without one (the design
// checks, `--demo`) the scripted demo below plays instead.
const port = new URLSearchParams(location.search).get('port');
// Her bubble carries what she says aloud: the spoken form when the answer has one (ADR 0040), else its text.
const spoken = (reply: string) => { const voice = /<voice>([\s\S]*?)(?:<\/voice>|$)/.exec(reply); return voice ? plain(voice[1]) : plain(reply); };
const DOUBLE_CLICK_MS = 300;
// around: one column under her, her words first (the default). grid: the main app's two columns of tiles.
type DashboardLayout = 'grid' | 'around';
// Prototype script: every transcript and reply below is simulated.
const HEARD = '把今天的任务整理一下';
// Her skin, whether she changes it herself, how the Dashboard is laid out and the look of the agent marks
// live in this companion's own profile.
const WARDROBE = 'companion-wardrobe-v1';
function loadWardrobe(): { skin: Skin; auto: boolean; layout: DashboardLayout; homeGlass: boolean; marks: MarkLook } {
  try {
    const value = JSON.parse(localStorage.getItem(WARDROBE) ?? '{}');
    return { skin: isSkin(value.skin) ? value.skin : 'glass', auto: value.auto !== false, layout: value.layout === 'grid' ? 'grid' : 'around', homeGlass: value.homeGlass !== false,
      marks: isMarkLook(value.marks) ? value.marks : 'spark' };
  } catch { return { skin: 'glass', auto: true, layout: 'around', homeGlass: true, marks: 'spark' }; }
}
const isPreview = (value: string): value is ExprId => (PREVIEW as string[]).includes(value);

function layout({ topInset, notchWidth, surfaceWidth: width }: Placement, tucked: boolean) {
  const center = width / 2, notchLeft = center - notchWidth / 2;
  // Without a notch she lives dead centre in a pill; its two wings open the Dashboard, as the camera does beside a notch.
  const lobe: Lobe = notchWidth ? { left: notchLeft - 64, right: notchLeft + 24, height: topInset, notched: true, tucked }
    : { left: center - 66, right: center + 66, height: topInset, notched: false, tucked };
  const x = notchWidth ? notchLeft - 32 : center, out = { x, y: topInset + R + 14 }, panelTop = topInset + 44;
  const anchors: Record<Place, Point> = { home: { x, y: topInset / 2 }, peek: { x, y: topInset + R * .1 }, out, dock: { x: center, y: panelTop - R * .5 } };
  // The agent marks' wing grows from the notch's right edge, or the pill's.
  const wingX = notchWidth ? notchLeft + notchWidth : lobe.right;
  return { width, lobe, anchors, out, center, panelTop, wingX, zones: {
    lobe: notchWidth ? { x: lobe.left - 26, y: 0, w: notchLeft - lobe.left + 26, h: topInset + 16 } : { x: center - 36, y: 0, w: 72, h: topInset + 16 },
    ball: { x: x - R - 12, y: topInset, w: 2 * R + 24, h: out.y + R + 12 - topInset },
    chip: { x: x + R + 4, y: out.y - 18, w: 44, h: 36 },
    dash: notchWidth ? [{ x: notchLeft, y: 0, w: notchWidth, h: topInset + 4 }]
      : tucked ? [] : [{ x: lobe.left, y: 0, w: 30, h: topInset + 4 }, { x: center + 36, y: 0, w: 30, h: topInset + 4 }],
    panel: { x: center - PANEL / 2 - 10, y: 0, w: PANEL + 20, h: panelTop + 660 },
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
  // ⌘ in the menu bar row tucks her away on that side of the camera for a moment (electron/companion.ts).
  const [tuck, setTuck] = useState({ left: false, right: false });
  useEffect(() => window.jarvis?.onTuck?.(setTuck), []);
  const geo = useMemo(() => layout(placement, tuck.left), [placement, tuck.left]);
  const [preferences, setPreferences] = usePreferences();
  const [companion] = useCompanionSettings();
  const t = (l: L) => tr(companion.lang, l);
  useEffect(() => { warmFeedback(); return stopFeedback; }, []);
  const [zone, setZone] = useState<Zone>('none');
  const [dashboard, setDashboard] = useState(false);
  const [composer, setComposer] = useState(false);
  const [draft, setDraft] = useState('');
  const [simVoice, setVoice] = useState<'off' | 'listening' | 'thinking' | 'speaking'>('off');
  const [simCaption, setCaption] = useState('');
  const [simHearing, setHearing] = useState(false);
  const [simReply, setReply] = useState({ text: '', shown: 0 });
  const [simTalking, setTalking] = useState(false);
  const [s, dispatch] = useReducer(reducer, initialState);
  // Muting Jarvis silences her cues as well as its voice.
  const feedback = (cue: FeedbackCue) => { if (preferences.feedbackEnabled && !s.soundMuted) void playFeedback(cue, preferences.feedbackVolume); };
  const link = useRef<Runtime | null>(null);
  useEffect(() => { if (!port) return; link.current = connect(port, dispatch); return () => { link.current?.close(); link.current = null; }; }, []);
  // Live, the daemon's phase is her voice; standby counts as listening only in wave mode (ADR 0041).
  // While your words are coming in she only listens: no answer starts then (ADR 0053), whatever text arrives.
  const inFlight = !!port && s.inFlight;
  const voice = !port ? simVoice : inFlight ? 'listening' : s.phase === 'speaking' ? 'speaking' : s.phase === 'processing' ? 'thinking'
    : s.phase === 'hearing' || (s.conversation && s.phase !== 'error') ? 'listening' : 'off';
  const caption = port ? s.heard : simCaption, hearing = port ? inFlight : simHearing, talking = port ? false : simTalking;
  const said = port ? spoken(s.reply) : '';
  // Her words on screen stay as they were while yours are still coming in: cut off, or none.
  const held = useRef('');
  if (!inFlight) held.current = said;
  const reply = port ? { text: held.current, shown: held.current.length } : simReply;
  const [pressed, setPressed] = useState(false);
  const [wardrobe, setWardrobe] = useState(loadWardrobe);
  // A skin change or an expression from the tray brings her out of the island for a moment.
  const [outing, setOuting] = useState(false);
  const [preview, setPreview] = useState<ExprId | null>(null);
  // The page open in the Dashboard sets her face while nothing else is going on.
  const [dashMood, setDashMood] = useState<ExprId | null>(null);
  const [receiving, setReceiving] = useState(false);
  const busy = composer || voice !== 'off' || !!reply.text || receiving;
  // Every session the Dashboard's Agents data knows: marks on the wing beside the notch, and the notices.
  const [agents, setAgents] = useState<ShownAgent[]>([]);
  const [seen, setSeen] = useState<{ ids: string[]; at: number }>({ ids: [], at: 0 });
  // No notice while she talks, while you type to her or while the Dashboard is open; they come up after.
  const notices = useNotices({ agents, hold: busy || dashboard || moving,
    cue: (name, gain) => { if (preferences.feedbackEnabled && !s.soundMuted) noticeCue(name, preferences.feedbackVolume, gain); },
    answer: (req, body) => port ? answerRequest(port, req.id, body) : Promise.resolve(true),
    onSeen: ids => setSeen({ ids, at: Date.now() }) });
  const notice = notices.current;
  // Tucked, she stays up with the island until it comes back; only the Dashboard and a notice card, below the menu bar, keep her out.
  const place: Place = moving ? 'home' : dashboard || notice ? 'dock' : tuck.left ? 'home' : busy || zone === 'ball' || outing ? 'out' : zone === 'lobe' || notices.peek ? 'peek' : 'home';
  // A finished text reply stays up briefly: that is her "done" face.
  const listenFace = useRef<ExprId>('35'), receiveFace = useRef<ExprId>('31'), replyFace = useRef<ExprId>('39');
  // Live turns pick her takes as they begin; the scripted demo picks its own in listen() and say().
  const lastVoice = useRef(voice);
  if (port && voice !== lastVoice.current) { if (voice === 'listening') listenFace.current = pick(TAKES.listen); else if (voice === 'speaking') replyFace.current = pick(TAKES.reply); }
  lastVoice.current = voice;
  // A notice sets her face: waiting on you, pleased it is done, a jolt on an error; after you answer, a moment of
  // pleasure or refusal.
  const moment = performance.now();
  const noticeFace: ExprId | null = !notice ? null : notices.over && moment < notices.over.until ? notices.over.face
    : notice.kind === 'done' || notice.kind === 'dones' ? 'fin' : notice.kind === 'err' ? moment - notices.openedAt < 1700 ? '34' : '02' : notices.card?.ok ? '02' : 'ask';
  useEffect(() => { if (notice?.kind !== 'err') return; const t = setTimeout(notices.bump, 1750); return () => clearTimeout(t); }, [notice?.key]);
  const expr: ExprId = preview ?? noticeFace ?? (receiving ? receiveFace.current : voice === 'listening' ? listenFace.current : voice === 'thinking' ? '30' : voice === 'speaking' || talking ? replyFace.current : dashboard && dashMood ? dashMood : reply.text ? '33' : '02');
  const chip = place === 'out' && zone === 'ball' && !busy;
  // Beside a notch the tucked marks go under it; without one they ride up with the pill.
  const wing = useMemo(() => { const all = wingSlots(agents, wardrobe.marks); return tuck.right && placement.notchWidth ? { ...all, width: 0 } : all; }, [agents, wardrobe.marks, tuck.right, placement.notchWidth]);
  // Tucked, the island and she slide up in one motion, far enough to clear her from where she comes out,
  // so nothing of her trails behind; the Dashboard and a notice card keep her (dock).
  const lift = tuck.left ? geo.out.y + 2 * R : 0;
  const [wingTip, setWingTip] = useState(false), [agentsFocus, setAgentsFocus] = useState(0);
  const wingRect = { x: geo.wingX, y: 0, w: tuck.right ? 0 : wing.width, h: placement.topInset };
  const live = useRef({ geo, dashboard, chip, composer, place, wardrobe, wingRect, openBy: companion.openBy });
  live.current = { geo, dashboard, chip, composer, place, wardrobe, wingRect, openBy: companion.openBy };
  const ball = useRef<BallHandle | null>(null), look = useRef<Point | null>(null), cursor = useRef<Point>({ x: -1e4, y: -1e4 });
  const input = useRef<HTMLInputElement>(null), root = useRef<HTMLElement>(null), pressing = useRef(false);

  const zoneTimer = useRef<ReturnType<typeof setTimeout>>(undefined), pending = useRef<Zone>('none');
  const tipTimer = useRef<ReturnType<typeof setTimeout>>(undefined), overWing = useRef(false);
  const noticeEl = useRef<HTMLDivElement>(null), overNotice = useRef(false), shownNotice = useRef<{ n: Notice; card: NonNullable<typeof notices.card> } | null>(null);
  if (notice && notices.card) shownNotice.current = { n: notice, card: notices.card };
  const dashTimer = useRef<ReturnType<typeof setTimeout>>(undefined), dashEntered = useRef(false), pinned = useRef(false), interactive = useRef(false);
  const script = useRef<ReturnType<typeof setTimeout>[]>([]);
  const after = (ms: number, run: () => void) => { script.current.push(setTimeout(run, ms)); };
  const stopScript = () => { script.current.forEach(clearTimeout); script.current = []; };
  useEffect(() => stopScript, []);
  const say = (text: string, done: () => void) => {
    replyFace.current = pick(TAKES.reply);
    setReply({ text, shown: 0 }); setTalking(true);
    for (let i = 1; i <= text.length; i++) after(i * 115, () => setReply({ text, shown: i }));
    after(text.length * 115 + 450, () => { setTalking(false); done(); });
  };
  // She takes the task in for a moment before she thinks or answers.
  const receive = () => { receiveFace.current = pick(TAKES.receive); setReceiving(true); after(700, () => setReceiving(false)); };
  const listen = (scripted: boolean) => {
    // Each turn she picks one of her takes for listening, receiving and replying.
    listenFace.current = pick(TAKES.listen);
    stopScript(); setReceiving(false); setVoice('listening'); setCaption(''); setHearing(false); setReply({ text: '', shown: 0 }); setTalking(false);
    if (!scripted) return;
    const end = 650 + HEARD.length * 115;
    after(650, () => setHearing(true));
    for (let i = 1; i <= HEARD.length; i++) after(650 + i * 115, () => setCaption(HEARD.slice(0, i)));
    after(end + 250, () => { setHearing(false); setVoice('thinking'); receive(); });
    after(end + 1700, () => { setVoice('speaking'); say('好，我来整理。', () => listen(false)); });
  };
  const endVoice = () => {
    if (port) {
      feedback('voice-exit');
      void link.current?.controls({ conversation: false }).catch(() => undefined);
      if (s.phase === 'speaking' || s.phase === 'processing') void link.current?.cancel(s.responseId).catch(() => undefined);
      return;
    }
    stopScript(); setReceiving(false); feedback('voice-exit'); setVoice('off'); setCaption(''); setHearing(false); setReply({ text: '', shown: 0 }); setTalking(false); };
  const closeComposer = () => { setComposer(false); void window.jarvis?.focus(false); };
  // Poke: start a voice turn, interrupt playback, or end the session.
  const poke = () => {
    if (port) {
      if (voice === 'off') { closeComposer(); feedback('voice-enter'); void link.current?.controls({ conversation: true }).catch(() => undefined); }
      else if (voice === 'speaking') void link.current?.cancel(s.responseId).catch(() => undefined);
      else endVoice();
      return;
    }
    if (voice === 'off') { closeComposer(); feedback('voice-enter'); listen(true); }
    else if (voice === 'speaking') listen(false);
    else endVoice();
  };
  const pressAt = useRef(0), firstClick = useRef<ReturnType<typeof setTimeout>>(undefined), latestPoke = useRef(poke);
  latestPoke.current = poke;
  const press = () => { pressing.current = true; pressAt.current = performance.now(); setPressed(true); };
  // A short poke talks to her once no second click follows; a double click opens or closes the Dashboard;
  // holding her until she shivers changes her into the next skin.
  const release = () => {
    if (!pressing.current) return;
    pressing.current = false; setPressed(false);
    if (performance.now() - pressAt.current >= HOLD_MS) choose(SKIN_KEYS[(SKIN_KEYS.indexOf(worn.current) + 1) % SKIN_KEYS.length]);
    else if (live.current.openBy === 'hover') latestPoke.current();
    else if (firstClick.current) { clearTimeout(firstClick.current); firstClick.current = undefined; toggleDashboard(); }
    else firstClick.current = setTimeout(() => { firstClick.current = undefined; latestPoke.current(); }, DOUBLE_CLICK_MS);
  };
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
    stopScript(); setReceiving(false); setReply({ text: '', shown: 0 }); setTalking(false); setComposer(true);
    void window.jarvis?.focus(true).then(() => requestAnimationFrame(() => { input.current?.focus(); aimAtCaret(); }));
  };
  const send = () => {
    const text = draft.trim();
    if (!text) return;
    setDraft(''); closeComposer(); stopScript();
    receive();
    if (port) { void submit(text); return; }
    after(700, () => say(text.includes('整理') ? '好，我来整理。' : '收到，我来处理。', () => after(1800, () => setReply({ text: '', shown: 0 }))));
  };
  // Typed text goes to the daemon like the capsule's; the answer comes back on the same link as a voice turn's.
  const submit = (text: string) => link.current?.submit(text).catch(() => dispatch({ type: 'phase', phase: 'error' }));
  // A heard utterance is a task she takes in, as a typed one is.
  useEffect(() => { if (port && s.heard) receive(); }, [s.heard]);
  // The conversation of record, polled while the Dashboard shows it. The streaming answer rides as a tail on the home row
  // until its row lands; the Conversation page waits for the row, since the stream's chunks lose the answer's line breaks.
  const tail = s.reply && !s.rows.some(row => row.seq > s.openSeq && row.source !== 'allen') ? visible(s.reply) : '';
  const lastSeq = useRef(0);
  lastSeq.current = s.rows.length ? s.rows[s.rows.length - 1].seq : 0;
  useEffect(() => {
    if (!port || !dashboard) return;
    let stop = false;
    const load = async () => { try { const rows = await link.current?.conversation(lastSeq.current); if (rows && !stop) dispatch({ type: 'rows', rows }); } catch { /* daemon away; the next tick retries */ } };
    void load();
    const id = setInterval(() => void load(), 2000);
    return () => { stop = true; clearInterval(id); };
  }, [dashboard, !!tail]); // and at once when an answer starts: its row is written before it streams
  // Older days for the Conversation page: a longer page of the newest rows; a short answer means the history's start.
  // ponytail: re-fetches the newest rows each time; a `before` cursor on the route if the history outgrows a few pages.
  const [floor, setFloor] = useState(false);
  const older = async () => {
    const want = s.rows.length + 200, first = s.rows[0]?.seq ?? Infinity;
    try {
      const rows = await link.current?.conversation(0, want);
      if (!rows) return false;
      if (rows.length < want) setFloor(true);
      dispatch({ type: 'older', rows });
      return rows.some(row => row.seq < first);
    } catch { return false; }
  };
  // When Jarvis asks for a plugin mid-conversation, the Dashboard opens on it.
  const plugins = usePlugins(), request = plugins.snapshot?.request, shownRequest = useRef('');
  const [pluginFocus, setPluginFocus] = useState<{ plugin: string; key: string } | null>(null);
  useEffect(() => {
    if (!port || !request) return;
    const key = `${request.id}:${request.presentation}`;
    if (key === shownRequest.current) return;
    shownRequest.current = key;
    if (request.purpose && (request.state === 'offered' || request.state === 'error')) { openDashboard(false); pinned.current = true; setPluginFocus({ plugin: request.plugin_id, key }); }
  }, [request?.id, request?.presentation]);
  const openDashboard = (hovered: boolean) => { pinned.current = false; dashEntered.current = hovered; setDashboard(true); setComposer(false); void window.jarvis?.focus(false); };
  // Opened by a double click, the Dashboard stays when the cursor leaves; another double click on her closes it.
  const toggleDashboard = () => { if (live.current.dashboard) setDashboard(false); else { openDashboard(false); pinned.current = true; } };
  // A click on the marks opens the Dashboard on Agents, and it stays like a double-clicked one.
  const openAgents = () => { setWingTip(false); if (!live.current.dashboard) openDashboard(false); pinned.current = true; setAgentsFocus(n => n + 1); };
  // Only a Codex thread can be opened from a card; the card goes, and the session counts as looked at.
  const openSession = (id: string) => { void window.jarvis?.openCodex?.(id); notices.next(true); };

  // What she wears now; it differs from the saved pick while she tries another skin on her own.
  const worn = useRef<Skin>(wardrobe.skin);
  const outingTimers = useRef<ReturnType<typeof setTimeout>[]>([]);
  // Out of the island first when she rests there, do the thing, and home again after `stay`.
  const appear = (run: () => void, stay: number) => {
    outingTimers.current.forEach(clearTimeout);
    const lead = live.current.place === 'home' || live.current.place === 'peek' ? 650 : 0;
    setOuting(true);
    outingTimers.current = [setTimeout(run, lead), setTimeout(() => { setOuting(false); setPreview(null); }, lead + stay)];
  };
  useEffect(() => () => outingTimers.current.forEach(clearTimeout), []);
  const wear = (skin: Skin) => { if (skin === worn.current) return; worn.current = skin; appear(() => ball.current?.change(skin), 2600); };
  const choose = (skin: Skin) => { setWardrobe(value => ({ ...value, skin })); wear(skin); };
  // On her own she tries another skin, and the next time changes back to yours.
  const selfChange = () => {
    const mine = live.current.wardrobe.skin, others = SKIN_KEYS.filter(key => key !== mine);
    wear(worn.current === mine ? others[Math.floor(Math.random() * others.length)] : mine);
  };
  useEffect(() => {
    try { localStorage.setItem(WARDROBE, JSON.stringify(wardrobe)); } catch { /* the pick just is not remembered */ }
    window.jarvis?.companionMenu({ skins: SKIN_KEYS.map(key => ({ key, name: SKINS[key].name, on: key === wardrobe.skin })), auto: wardrobe.auto, layout: wardrobe.layout, homeGlass: wardrobe.homeGlass, marks: wardrobe.marks,
      follow: companion.screen === 'follow', lang: companion.lang, exprs: PREVIEW.map(id => ({ id, name: EXPRESSIONS[id].name })) });
  }, [wardrobe, companion.screen, companion.lang]);
  useEffect(() => {
    if (!wardrobe.auto) return;
    let timer: ReturnType<typeof setTimeout>;
    // Every 6 to 14 minutes, while she rests in the island, she changes on her own.
    const plan = () => { timer = setTimeout(() => { if (live.current.place === 'home') selfChange(); plan(); }, (6 + Math.random() * 8) * 60_000); };
    plan();
    return () => clearTimeout(timer);
  }, [wardrobe.auto]);
  useEffect(() => window.jarvis?.onCommand(command => {
    const [name, value = ''] = command.split(':');
    if (command === 'dashboard') openDashboard(false);
    else if (command === 'settings') { openDashboard(false); pinned.current = true; setSettingsFocus(n => n + 1); }
    else if (name === 'skin' && isSkin(value)) choose(value);
    else if (command === 'outing') selfChange();
    else if (name === 'layout' && (value === 'grid' || value === 'around')) setWardrobe(current => ({ ...current, layout: value }));
    else if (name === 'expr' && isPreview(value)) appear(() => setPreview(value), 4200);
    else if (command === 'homeGlass') setWardrobe(current => ({ ...current, homeGlass: !current.homeGlass }));
    else if (name === 'marks' && isMarkLook(value)) setWardrobe(current => ({ ...current, marks: value }));
    else if (command === 'auto') {
      const auto = !live.current.wardrobe.auto;
      setWardrobe(current => ({ ...current, auto }));
      if (!auto) wear(live.current.wardrobe.skin);
    }
  }), []);
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
    const island = !lobe.tucked && p.y >= 0 && p.y <= lobe.height && p.x >= lobe.left - 6 && p.x <= lobe.right + (lobe.notched ? 0 : 6);
    const hit = island || !!document.elementFromPoint(p.x, p.y)?.closest('[data-hit]');
    if (hit !== interactive.current && !pressing.current) { interactive.current = hit; window.jarvis?.passthrough(!hit); }
  };
  useEffect(() => window.jarvis?.onCursor(point => {
    const { geo, dashboard, chip, composer } = live.current, z = geo.zones;
    cursor.current = point;
    if (!composer) look.current = point;
    refreshHit();
    const next: Zone = within(point, z.ball) || (chip && within(point, z.chip)) ? 'ball' : !geo.lobe.tucked && within(point, z.lobe) ? 'lobe' : 'none';
    // Resting on the marks lists the sessions under them.
    const wingNow = live.current.wingRect.w > 0 && within(point, live.current.wingRect);
    if (wingNow !== overWing.current) { overWing.current = wingNow; clearTimeout(tipTimer.current); tipTimer.current = setTimeout(() => setWingTip(wingNow), wingNow ? 120 : 200); }
    if (next !== pending.current) {
      pending.current = next; clearTimeout(zoneTimer.current);
      zoneTimer.current = setTimeout(() => setZone(next), next === 'ball' ? 0 : next === 'lobe' ? 90 : 600);
    }
    // A hand on a notice card holds its timers.
    const card = noticeEl.current?.classList.contains('is-open') ? noticeEl.current.getBoundingClientRect() : null;
    const onCard = !!card && point.x >= card.left && point.x <= card.right && point.y >= card.top && point.y <= card.bottom;
    if (onCard !== overNotice.current) { overNotice.current = onCard; notices.setHover(onCard); }
    const over = z.dash.some(r => within(point, r)) || (dashboard && within(point, z.panel));
    if (dashboard) {
      if (over || pinned.current) { dashEntered.current = true; clearTimeout(dashTimer.current); dashTimer.current = undefined; }
      else if (dashEntered.current && !dashTimer.current) dashTimer.current = setTimeout(() => { dashTimer.current = undefined; setDashboard(false); }, 450);
    } else if (over && !dashTimer.current && live.current.openBy !== 'click') {
      dashTimer.current = setTimeout(() => { dashTimer.current = undefined; if (live.current.geo.zones.dash.some(r => within(cursor.current, r))) openDashboard(true); }, 200);
    } else if (!over && dashTimer.current) { clearTimeout(dashTimer.current); dashTimer.current = undefined; }
  }), []);
  useEffect(() => () => { clearTimeout(zoneTimer.current); clearTimeout(dashTimer.current); clearTimeout(firstClick.current); clearTimeout(tipTimer.current); }, []);
  useEffect(refreshHit, [place, chip, composer, dashboard, voice, reply.text, wing.width, notice?.key, tuck.left]);

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
  useEffect(() => kickGlass.current(), [place, chip, composer, dashboard, voice, reply.text, caption, notice?.key]);

  // What Settings in the panel reads and changes here: the daemon's switches, her look, her cues.
  const [settingsFocus, setSettingsFocus] = useState(0);
  const control = (patch: { mic_muted?: boolean; speech_muted?: boolean; conversation?: boolean }) => void link.current?.controls(patch).catch(() => undefined);
  const handsFree = port ? s.conversation : voice !== 'off';
  const ctl: DashControls = {
    micMuted: s.micMuted, speechMuted: s.soundMuted, handsFree,
    setMic: muted => { if (muted === s.micMuted) return; feedback(muted ? 'mic-off' : 'mic-on'); if (port) control({ mic_muted: muted }); else dispatch({ type: 'mic' }); },
    setSpeech: muted => {
      if (muted === s.soundMuted) return;
      if (muted) stopFeedback(); else if (preferences.feedbackEnabled) void playFeedback('speaker-on', preferences.feedbackVolume);
      if (port) control({ speech_muted: muted }); else dispatch({ type: 'sound' });
    },
    setHandsFree: on => {
      if (on === handsFree) return;
      if (!on) { endVoice(); return; }
      closeComposer(); feedback('voice-enter');
      if (port) control({ conversation: true }); else listen(false);
    },
    look: wardrobe,
    setLook: (change: Partial<Look>) => {
      if (change.skin) choose(change.skin);
      if (change.auto === false) wear(wardrobe.skin);
      const { skin: _, ...rest } = change;
      if (Object.keys(rest).length) setWardrobe(current => ({ ...current, ...rest }));
    },
    playFaces: () => { stopScript(); PREVIEW.forEach((id, i) => after(i * 1100, () => setPreview(id))); after(PREVIEW.length * 1100, () => setPreview(null)); },
    cues: { on: preferences.feedbackEnabled, volume: preferences.feedbackVolume },
    setCues: change => setPreferences({ ...change.on !== undefined && { feedbackEnabled: change.on }, ...change.volume !== undefined && { feedbackVolume: change.volume } }),
  };

  const { out } = geo;
  // The strip and her bubble share one spot: her words keep it until yours are in, then the strip shows them whole.
  const yours = voice === 'thinking' && !!caption;
  const strip = place === 'out' && (yours || ((voice === 'listening' || voice === 'thinking') && !reply.text)), bubble = place === 'out' && !!reply.text && !yours;
  return <IconContext.Provider value={{ size: 16, weight: 'regular' }}>
    <main ref={root} className="companion" style={{ '--mint': preferences.themeColor } as React.CSSProperties}>
      <div className={`companion-chip ${chip ? 'is-open' : ''}`} data-hit={chip || undefined} data-glass="9" style={{ left: out.x + R + 12, top: out.y - 13 }}>
        <button aria-label={t(['Type to her', '文字输入'])} tabIndex={chip ? 0 : -1} onClick={openComposer}><Keyboard/></button>
      </div>
      <form className={`companion-composer ${composer && place === 'out' ? 'is-open' : ''}`} data-hit={composer || undefined} data-glass="17"
        style={{ left: out.x - PANEL / 2, top: out.y + R + 11 }} inert={!composer} onTransitionEnd={aimAtCaret}
        onSubmit={event => { event.preventDefault(); send(); }}>
        <button type="button" className="composer-attach" disabled aria-label={t(['Attach (not wired yet)', '添加附件（还没接）'])}><Paperclip/></button>
        <input ref={input} aria-label={t(['Type to her', '文字输入'])} placeholder={t(['Say something…', '和她说点什么…'])} value={draft}
          onChange={event => { setDraft(event.target.value); ball.current?.nudge(); requestAnimationFrame(aimAtCaret); }}
          onSelect={aimAtCaret} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); closeComposer(); } }}/>
        <button type="submit" className="composer-send" disabled={!draft.trim()} aria-label={t(['Send', '发送'])}><ArrowUp weight="bold"/></button>
      </form>
      <div className={`companion-strip ${strip ? 'is-open' : ''} ${hearing ? 'is-hearing' : ''}`} data-hit={strip || undefined} data-glass="19"
        style={{ left: out.x, top: out.y + R + 11 }} inert={!strip} role="status">
        <span className="strip-mic"><Microphone size={14} weight="fill"/></span>
        <span className={`strip-text ${caption ? '' : 'is-empty'}`}>{caption || t(['Listening…', '在听…'])}</span>
        <button className="strip-stop" aria-label={t(['End voice', '结束语音'])} onClick={endVoice}><Stop size={11} weight="fill"/></button>
      </div>
      <div className={`companion-bubble ${bubble ? 'is-open' : ''}`} data-glass="18" style={{ left: out.x, top: out.y + R + 11 }} role="status">
        <span className="bubble-text"><span className="bubble-ghost">{reply.text}</span><span>{reply.text.slice(0, reply.shown)}</span></span>
      </div>
      <div className={`companion-dashboard ${dashboard ? 'is-open' : ''}`} data-hit={dashboard || undefined} data-glass="24"
        style={{ left: geo.center - PANEL / 2, top: geo.panelTop }} inert={!dashboard}>
        {wardrobe.layout === 'around'
          ? <AroundDashboard open={dashboard} port={port} onClose={() => setDashboard(false)} onMood={setDashMood} onHop={height => ball.current?.hop(height)}
            talk={port ? { rows: s.rows, tail, busy: s.phase === 'processing', offline: s.phase === 'error', floor, submit, older } : undefined}
            plugins={port ? plugins : undefined} pluginFocus={pluginFocus} marks={wardrobe.marks} onAgents={setAgents} agentsFocus={agentsFocus} seen={seen}
            onAnswer={id => { setDashboard(false); notices.focus(id); }} ctl={ctl} settingsFocus={settingsFocus}/>
          : <DashboardPreview embedded port={port} visible={dashboard} shown={dashboard} onClose={() => setDashboard(false)}/>}
      </div>
      <div ref={noticeEl} className={`companion-notice ${notice ? 'is-open' : ''}`} data-hit={notice ? true : undefined} data-glass="24"
        style={{ left: geo.center - PANEL / 2, top: geo.panelTop }} inert={!notice} role="alertdialog" aria-label="Agent notice">
        {shownNotice.current && <NoticeCard key={shownNotice.current.n.key} n={shownNotice.current.n} card={shownNotice.current.card} agent={agents.find(a => a.id === shownNotice.current!.n.id)}
          count={notices.count} total={agents.length} look={wardrobe.marks} onClose={() => notices.next(true)} onLater={notices.fold} onOpen={openSession} onAll={openAgents}
          onChange={notices.bump} onResolve={(text, body) => {
            const n = shownNotice.current!.n;
            if (n.kind !== 'req') return;
            void notices.resolve(n, text, body);
            if (body.decision !== 'deny') ball.current?.hop(.14);
          }}/>}
      </div>
      <AgentWing look={wardrobe.marks} wing={wing} lift={placement.notchWidth ? 0 : lift} x={geo.wingX} height={placement.topInset} limit={geo.width} tip={wingTip && !dashboard} onOpen={openAgents}/>
      <CompanionBall width={geo.width} height={placement.topInset + 560} lobe={geo.lobe} lift={lift} look={look} handle={ball} skin={worn.current}
        target={{ place, expr, pressed, anchors: geo.anchors, homeGlass: wardrobe.homeGlass, lift: place === 'home' ? lift : 0 }}
        label={voice === 'off' ? t([`Poke to talk${port ? '' : ' (demo)'}`, `戳一下，开始语音${port ? '' : '（演示）'}`]) : voice === 'speaking' ? t(['Poke to interrupt', '戳一下，打断播报']) : t(['Poke to stop', '戳一下，结束语音'])}
        onPress={press} onRelease={release} onCancel={cancel} onMove={refreshHit}/>
    </main>
  </IconContext.Provider>;
}
