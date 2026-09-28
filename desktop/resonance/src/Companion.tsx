import { useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react';
import { IconContext, Keyboard, Paperclip, ArrowUp, Microphone, Stop } from '@phosphor-icons/react';
import { CompanionBall, HOLD_MS, R, type BallHandle, type Lobe, type Place, type Point } from './CompanionBall';
import { PREVIEW, SKIN_KEYS, TAKES, isSkin, pick, type ExprId, type Skin } from './starCore';
import { AroundDashboard, type Think } from './AroundDashboard';
import { playFeedback, stopFeedback, warmFeedback, type FeedbackCue } from './feedback';
import { usePreferences } from './preferences';
import { initialState, plain, reducer, visible } from './model';
import { connect, type Runtime } from './runtime';
import { usePlugins } from './PluginPanel';
import { isMarkLook } from './AgentMarks';
import { answerRequest, type Agent, type ShownAgent } from './agents';
import { NoticeCard, ended, noticeCue, useNotices } from './Notices';
import { ActionCard, QuestionCard, type Answer, type Card, type Decide, type Question } from './ActionCard';
import { Notch, type NotchNote } from './Notch';
import { tr, useCompanionSettings, type L, type Lang } from './companionSettings';
import { useNow, useRoute } from './homeData';
import type { Controls as DashControls, Look } from './SettingsPage';
import { HOVER_DWELL_MS, HOVER_EXIT_MS, HOVER_SPEED, PointerIntent } from './pointerIntent';
import './design-tokens.css';
import './companion.css';

type Placement = { topInset: number; notchWidth: number; surfaceWidth: number; displayId?: number };
type Rect = { x: number; y: number; w: number; h: number };
type Zone = 'none' | 'lobe' | 'ball';
const within = (p: Point, r: Rect) => p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h;
const PANEL = 360;
// With a `port` she is live: the daemon's turns drive her voice, faces and words. Without one (the design
// checks, `--demo`) the scripted demo below plays instead.
const port = new URLSearchParams(location.search).get('port');
// Her bubble carries what she says aloud: the spoken form when the answer has one (ADR 0040), else its text.
const spoken = (reply: string) => { const voice = /<voice>([\s\S]*?)(?:<\/voice>|$)/.exec(reply); return voice ? plain(voice[1]) : plain(reply); };
// Prototype script: every transcript and reply below is simulated.
const HEARD = '把今天的任务整理一下';
// Her skin, whether she changes it herself, how she looks in the island and the look of the agent marks
// live in this companion's own profile.
const WARDROBE = 'companion-wardrobe-v1';
const SKIN_NAMES: Record<Skin, L> = { glass: ['Glass', '深空玻璃'], nebula: ['Nebula', '星云'], galaxy: ['Galaxy', '银河'], frost: ['Frost', '磨砂'], aurora: ['Aurora', '极光'], codex: ['Icon', '图标同款'] };
function loadWardrobe(): Look {
  try {
    const value = JSON.parse(localStorage.getItem(WARDROBE) ?? '{}');
    return { skin: isSkin(value.skin) ? value.skin : 'glass', auto: value.auto !== false, home: value.home === 'eyes' ? 'eyes' : 'dark',
      marks: isMarkLook(value.marks) ? value.marks : 'spark' };
  } catch { return { skin: 'glass', auto: true, home: 'dark', marks: 'spark' }; }
}
// Ghostty's title for a session is its name, sometimes behind a status mark; the board folds long names with "…".
const bare = (text: string) => text.replace(/\s+/g, ' ').replace(/^[^\p{L}\p{N}]+/u, '').trim();
const sameTitle = (name: string, title: string) => { const a = bare(name), b = bare(title); return !!a && (a === b || (a.endsWith('…') && b.startsWith(a.slice(0, -1)))); };
// Think mode's words come as the daemon's Python patterns; one JavaScript cannot read switches nothing on early.
const pattern = (source: string) => { try { return new RegExp(source, 'i'); } catch { return null; } };

function layout({ topInset, notchWidth, surfaceWidth: width }: Placement) {
  const center = width / 2, notchLeft = center - notchWidth / 2;
  // Without a notch she lives dead centre in a pill; its two wings open the Dashboard, as the camera does beside a notch.
  const lobe: Lobe = notchWidth ? { left: notchLeft - 64, right: notchLeft + 24, height: topInset, notched: true }
    : { left: center - 66, right: center + 66, height: topInset, notched: false };
  const x = notchWidth ? notchLeft - 32 : center, out = { x, y: topInset + R + 14 }, panelTop = topInset + 44;
  const anchors: Record<Place, Point> = { home: { x, y: topInset / 2 }, peek: { x, y: topInset + R * .1 }, out, dock: { x: center, y: panelTop - R * .5 } };
  // The agent marks' wing grows from the notch's right edge, or the pill's. Beside the pill it starts 30 pt inside it,
  // so neither shape's rounded corner shows where they meet and the bottom edge runs straight across.
  const wingX = notchWidth ? notchLeft + notchWidth : lobe.right;
  return { width, lobe, anchors, out, center, panelTop, wingX, zones: {
    lobe: notchWidth ? { x: lobe.left, y: 0, w: notchLeft - lobe.left, h: topInset } : { x: center - 36, y: 0, w: 72, h: topInset },
    ball: { x: x - R - 12, y: topInset, w: 2 * R + 24, h: out.y + R + 12 - topInset },
    chip: { x: x + R + 4, y: out.y - 18, w: 44, h: 36 },
    dash: notchWidth ? [{ x: notchLeft, y: 0, w: notchWidth, h: topInset + 4 }]
      : [{ x: lobe.left, y: 0, w: 30, h: topInset + 4 }, { x: center + 36, y: 0, w: 30, h: topInset + 4 }],
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
  // ADR 0058: out at the text caret for dictation, drawn by its own window; she comes back happy when the words went in.
  const [trip, setTrip] = useState('home');
  useEffect(() => window.jarvis?.onDictation?.(setTrip), []);
  const geo = useMemo(() => layout(placement), [placement]);
  const [preferences, setPreferences] = usePreferences();
  const [companion, updateCompanion] = useCompanionSettings();
  // One language switch (Settings → Interface language): her panel follows the language Jarvis speaks in.
  const jarvisLang = useRoute<{ language: Lang }>(port, '/inherent/language', true, 3_600_000).data?.language;
  useEffect(() => { if (jarvisLang === 'en' || jarvisLang === 'zh') updateCompanion({ lang: jarvisLang }); }, [jarvisLang]);
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
  // ADR 0062: the card waiting for Allen's button, read every 1.5 s whether or not the Dashboard is open: closed, it
  // grows from the notch. The same card object stays while its id does, so a letter being edited keeps its text.
  // ADR 0066: the ask card rides the same tick; it hangs from the notch too, after a waiting confirmation.
  const [card, setCard] = useState<Card | null>(null);
  const [question, setQuestion] = useState<Question | null>(null);
  useEffect(() => {
    if (!port) return;
    let stop = false;
    const load = async () => { try {
      const [next, asked] = await Promise.all([link.current?.card(), link.current?.question()]);
      if (stop) return;
      setCard(current => current?.id === next?.id ? current : next ?? null);
      setQuestion(current => current?.id === asked?.id ? current : asked ?? null);
    } catch { /* daemon away; the next tick retries */ } };
    void load();
    const id = setInterval(() => void load(), 1500);
    return () => { stop = true; clearInterval(id); };
  }, []);
  const decideCard: Decide = (decision, edits) => {
    if (!card) return;
    const id = card.id;
    setCard(null);
    if (decision === 'accept') ball.current?.hop(.14);
    void link.current?.decide(id, decision, edits).catch(() => undefined); // a stale card: the next read shows what waits now
  };
  const answerQuestion: Answer = answers => {
    if (!question) return;
    const id = question.id;
    setQuestion(null);
    if (answers) ball.current?.hop(.14);
    void link.current?.answer(id, answers).catch(() => undefined); // a stale card: the next read shows what waits now
  };
  // With the Dashboard closed the card hangs from the notch, ahead of the agents' notices, and she watches it from home.
  const carded = (!!card || !!question) && !dashboard && !moving;
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
  // ADR 0064: think mode as the daemon reads it from Allen's words (ADR 0061), read again as soon as his words go in
  // or an answer opens; the poll catches the ten quiet minutes that end it.
  const think = useRoute<{ on: boolean; on_words: string; off_words: string }>(port, '/inherent/think', true, 30_000);
  const deep = !!port && think.data?.on === true;
  const words = useMemo((): Think['words'] => [pattern(think.data?.on_words ?? '(?!)'), pattern(think.data?.off_words ?? '(?!)')], [think.data?.on_words, think.data?.off_words]);
  useEffect(() => { if (s.waiting) think.reload(); }, [s.waiting, s.turnId]);
  const deepThinking = deep && (voice === 'thinking' || s.askedAt !== null);
  const clock = useNow(deepThinking ? 1000 : 3_600_000);
  const deepSecs = deepThinking ? Math.max(1, Math.ceil((clock - (s.askedAt ?? clock)) / 1000)) : 0;
  // Each deep answer's wait, pinned to the log position its row lands after (the streaming tail's rule).
  const [thoughts, setThoughts] = useState<{ turn: string; after: number; secs: number }[]>([]);
  useEffect(() => { if (deep && s.thoughtS && s.turnId) setThoughts(v => [...v.slice(-50), { turn: s.turnId!, after: s.openSeq, secs: s.thoughtS }]); }, [s.turnId]);
  const answerSecs = thoughts.at(-1)?.turn === s.turnId ? thoughts.at(-1)!.secs : 0;
  const [pressed, setPressed] = useState(false);
  const [wardrobe, setWardrobe] = useState(loadWardrobe);
  // A skin change brings her out of the island for a moment.
  const [outing, setOuting] = useState(false);
  const [menu, setMenu] = useState<Point | null>(null), [settingsFocus, setSettingsFocus] = useState(0);
  const menuRef = useRef<HTMLDivElement>(null);
  const [preview, setPreview] = useState<ExprId | null>(null);
  // The page open in the Dashboard sets her face while nothing else is going on.
  const [dashMood, setDashMood] = useState<ExprId | null>(null);
  const [receiving, setReceiving] = useState(false);
  const busy = composer || voice !== 'off' || !!reply.text || receiving || deepThinking;
  // Every session the Dashboard's Agents data knows: the stars beside the notch, and the notices.
  const [agents, setAgents] = useState<ShownAgent[]>([]);
  // The Claude session Allen has been looking at in Ghostty for 1.5 s (ADR 0057): read, and nothing pops for it.
  const [ghostty, setGhostty] = useState({ front: false, title: '' }), [dwelled, setDwelled] = useState(false);
  useEffect(() => window.jarvis?.onGhostty?.(seen => setGhostty(g => g.front === seen.front && g.title === seen.title ? g : seen)), []);
  useEffect(() => { setDwelled(false); if (!ghostty.front || !ghostty.title) return; const t = setTimeout(() => setDwelled(true), 1500); return () => clearTimeout(t); }, [ghostty]);
  const anyClaude = agents.some(a => a.agent === 'claude');
  useEffect(() => window.jarvis?.watchGhostty?.(anyClaude), [anyClaude]);
  const watched = dwelled ? agents.find(a => a.agent === 'claude' && sameTitle(a.title, ghostty.title))?.id ?? null : null;
  // ⌥Tab (spec §15.3): each press toggles the island's list for the keys; while it holds them the window takes key
  // focus without activating the app. `viewing` is the session whose page is open in the island.
  const [keysPress, setKeysPress] = useState(0), [keysOn, setKeysOn] = useState(false), [viewing, setViewing] = useState<string | null>(null);
  // No notice while she talks, while you type to her, while the Dashboard is open or while the keys hold the island;
  // they come up after.
  const notices = useNotices({ port, agents, hold: busy || dashboard || moving || carded || keysOn || !!menu, watched, viewing,
    cue: (name, gain) => { if (preferences.feedbackEnabled && !s.soundMuted) noticeCue(name, preferences.feedbackVolume, gain); },
    answer: (req, body) => port ? answerRequest(port, req.id, body) : Promise.resolve(true) });
  const notice = notices.current;
  // Going to a session reads it: its Codex thread, or its Ghostty terminal (a new tab attaches a background one).
  const jump = (a: Agent) => {
    if (ended(a.state)) notices.read([a.id]);
    if (a.agent === 'codex') void window.jarvis?.openCodex?.(a.id);
    else void window.jarvis?.jumpGhostty?.(a.title, a.job ?? '');
  };
  // A notice hangs from the notch and she watches it from home.
  const place: Place = moving ? 'home' : dashboard ? 'dock' : carded ? 'home' : notice ? notices.peek ? 'peek' : 'home' : busy || zone === 'ball' || outing || menu ? 'out' : zone === 'lobe' || notices.peek ? 'peek' : 'home';
  // A finished text reply stays up briefly: that is her "done" face.
  const listenFace = useRef<ExprId>('35'), receiveFace = useRef<ExprId>('31'), replyFace = useRef<ExprId>('39');
  // Live turns pick her takes as they begin; the scripted demo picks its own in listen() and say().
  const lastVoice = useRef(voice);
  if (port && voice !== lastVoice.current) { if (voice === 'listening') listenFace.current = pick(TAKES.listen); else if (voice === 'speaking') replyFace.current = pick(TAKES.reply); }
  lastVoice.current = voice;
  // A notice sets her face: waiting on you, pleased it is done, a jolt on an error; after you answer, a moment of
  // pleasure or refusal.
  const moment = performance.now();
  const stopped = notice?.kind === 'pop' && notice.ids.every(id => agents.find(a => a.id === id)?.state === 'err');
  const noticeFace: ExprId | null = carded ? 'ask' : !notice ? null : notices.over && moment < notices.over.until ? notices.over.face
    : notice.kind === 'pop' ? stopped ? moment - notices.openedAt < 1700 ? '34' : '02' : 'fin' : notices.card?.ok ? '02' : 'ask';
  useEffect(() => { if (!stopped) return; const t = setTimeout(notices.bump, 1750); return () => clearTimeout(t); }, [notice?.key]);
  const expr: ExprId = preview ?? noticeFace ?? (receiving ? receiveFace.current : inFlight ? listenFace.current : deepThinking ? 'deep' : voice === 'listening' ? listenFace.current : voice === 'thinking' ? '30' : voice === 'speaking' || talking ? replyFace.current : dashboard && dashMood ? dashMood : reply.text ? port && s.failed ? '38' : '33' : '02');
  const chip = place === 'out' && zone === 'ball' && !busy;
  // During a notice she looks down at it from the island.
  const noticeLook = carded ? { x: geo.center, y: placement.topInset + 90 } : notice ? { x: notice.kind === 'pop' ? geo.wingX + 80 : geo.center, y: placement.topInset + 90 } : null;
  const live = useRef({ geo, dashboard, chip, composer, place, wardrobe, noticeLook, openBy: companion.openBy });
  live.current = { geo, dashboard, chip, composer, place, wardrobe, noticeLook, openBy: companion.openBy };
  const ball = useRef<BallHandle | null>(null), look = useRef<Point | null>(null), cursor = useRef<Point>({ x: -1e4, y: -1e4 });
  const input = useRef<HTMLInputElement>(null), root = useRef<HTMLElement>(null), pressing = useRef(false);

  const zoneTimer = useRef<ReturnType<typeof setTimeout>>(undefined), pending = useRef<Zone>('none');
  const dashTimer = useRef<ReturnType<typeof setTimeout>>(undefined), dashEntered = useRef(false), pinned = useRef(false), dashClosedHere = useRef(false), interactive = useRef(false);
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
  const pressAt = useRef(0), latestPoke = useRef(poke);
  latestPoke.current = poke;
  const press = () => { pressing.current = true; pressAt.current = performance.now(); setPressed(true); };
  // Release a short click immediately. Once the charge ring appears, releasing early cancels the hold.
  const release = () => {
    if (!pressing.current) return;
    pressing.current = false; setPressed(false);
    const held = performance.now() - pressAt.current;
    if (held >= HOLD_MS) choose(SKIN_KEYS[(SKIN_KEYS.indexOf(worn.current) + 1) % SKIN_KEYS.length]);
    else if (held < 200) latestPoke.current();
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
  // Clicking the island pins the Dashboard; clicking it again closes it.
  const toggleDashboard = () => {
    clearTimeout(dashTimer.current); dashTimer.current = undefined;
    if (live.current.dashboard && pinned.current) { setDashboard(false); pinned.current = false; dashClosedHere.current = true; }
    else { if (!live.current.dashboard) openDashboard(false); pinned.current = true; }
  };

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
  const wear = (skin: Skin) => { if (skin === worn.current) return; worn.current = skin; window.jarvis?.wearing?.(skin); appear(() => ball.current?.change(skin), 2600); };
  // At the caret she wears what she wears here.
  useEffect(() => window.jarvis?.wearing?.(worn.current), []);
  const choose = (skin: Skin) => { setWardrobe(value => ({ ...value, skin })); wear(skin); };
  // On her own she tries another skin, and the next time changes back to yours.
  const selfChange = () => {
    const mine = live.current.wardrobe.skin, others = SKIN_KEYS.filter(key => key !== mine);
    wear(worn.current === mine ? others[Math.floor(Math.random() * others.length)] : mine);
  };
  useEffect(() => {
    try { localStorage.setItem(WARDROBE, JSON.stringify(wardrobe)); } catch { /* the pick just is not remembered */ }
  }, [wardrobe]);
  useEffect(() => window.jarvis?.companionSettings({ follow: companion.screen === 'follow', lang: companion.lang, dictation: companion.dictation }),
    [companion.screen, companion.lang, companion.dictation]);
  useEffect(() => {
    if (!wardrobe.auto) return;
    let timer: ReturnType<typeof setTimeout>;
    // Every 6 to 14 minutes, while she rests in the island, she changes on her own.
    const plan = () => { timer = setTimeout(() => { if (live.current.place === 'home') selfChange(); plan(); }, (6 + Math.random() * 8) * 60_000); };
    plan();
    return () => clearTimeout(timer);
  }, [wardrobe.auto]);
  // ⌥Tab, from the main process.
  useEffect(() => window.jarvis?.onCommand(command => {
    if (command === 'agent-keys') { setDashboard(false); closeComposer(); setKeysPress(n => n + 1); }
  }), []);
  useEffect(() => window.jarvis?.onDisplayLeave(() => {
    closeComposer(); setMenu(null); setDashboard(false); clearTimeout(zoneTimer.current); clearTimeout(dashTimer.current); dashTimer.current = undefined; pending.current = 'none'; setZone('none'); setMoving(true);
    // Long enough to look up, fly home and merge before the window leaves this screen.
    setTimeout(() => window.jarvis?.displayReady(), 520);
  }), []);
  // The ball reads the new anchors in its own effect, which runs before this one.
  useEffect(() => { if (arriving.current) { arriving.current = false; ball.current?.arrive(); } }, [geo]);
  useEffect(() => {
    const blur = () => { setMenu(null); cancel(); if (live.current.composer) closeComposer(); };
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
  useEffect(() => {
    const intent = new PointerIntent();
    const slow = () => intent.sample(cursor.current, performance.now()) < HOVER_SPEED;
    const receive = (point: Point) => {
      const { geo, dashboard, chip, composer } = live.current, z = geo.zones;
      cursor.current = point;
      intent.sample(point, performance.now());
      if (!composer) look.current = live.current.noticeLook ?? point;
      refreshHit();
      const next: Zone = within(point, z.ball) || (chip && within(point, z.chip)) ? 'ball' : within(point, z.lobe) ? 'lobe' : 'none';
      if (next !== pending.current) {
        pending.current = next; clearTimeout(zoneTimer.current);
        const reveal = () => {
          if (next !== pending.current) return;
          if (next === 'none' || slow()) { setZone(next); zoneTimer.current = undefined; }
          else zoneTimer.current = setTimeout(reveal, 20);
        };
        zoneTimer.current = setTimeout(reveal, next === 'none' ? HOVER_EXIT_MS : HOVER_DWELL_MS);
      }
      const panel = root.current?.querySelector('.companion-dashboard')?.getBoundingClientRect();
      const over = z.dash.some(r => within(point, r)) || (dashboard && !!panel && point.x >= panel.left && point.x <= panel.right && point.y >= 0 && point.y <= panel.bottom);
      const toward = dashboard && !!panel && intent.headingTo(panel);
      if (!over) dashClosedHere.current = false;
      if (dashboard) {
        if (over || pinned.current) { dashEntered.current = true; clearTimeout(dashTimer.current); dashTimer.current = undefined; }
        else if (dashEntered.current && (toward || !dashTimer.current)) {
          clearTimeout(dashTimer.current);
          dashTimer.current = setTimeout(() => { dashTimer.current = undefined; setDashboard(false); }, HOVER_EXIT_MS);
        }
      } else if (over && !dashTimer.current && !dashClosedHere.current && live.current.openBy !== 'click') {
        const reveal = () => {
          if (live.current.dashboard || !live.current.geo.zones.dash.some(r => within(cursor.current, r))) { dashTimer.current = undefined; return; }
          if (slow()) { dashTimer.current = undefined; openDashboard(true); }
          else dashTimer.current = setTimeout(reveal, 20);
        };
        dashTimer.current = setTimeout(reveal, HOVER_DWELL_MS);
      } else if (!over && dashTimer.current) { clearTimeout(dashTimer.current); dashTimer.current = undefined; }
    };
    const unsubscribe = window.jarvis?.onCursor(receive);
    return () => { unsubscribe?.(); clearTimeout(zoneTimer.current); clearTimeout(dashTimer.current); };
  }, []);
  useEffect(() => {
    if (!menu) return;
    void Promise.resolve(window.jarvis?.focus(true)).then(() => menuRef.current?.querySelector('button')?.focus());
    const dismiss = (event: PointerEvent) => { if (!menuRef.current?.contains(event.target as Node)) setMenu(null); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.stopPropagation(); setMenu(null); } };
    document.addEventListener('pointerdown', dismiss); document.addEventListener('keydown', escape, true);
    return () => { document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', escape, true); void window.jarvis?.focus(false); };
  }, [menu]);
  useEffect(refreshHit, [place, chip, composer, dashboard, menu, voice, reply.text, notice?.key, card?.id]);

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
  useEffect(() => kickGlass.current(), [place, chip, composer, dashboard, voice, reply.text, caption, notice?.key, card?.id]);

  // What Settings in the panel reads and changes here: the daemon's switches, her look, her cues.
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
  const note: NotchNote | null = carded && card ? { key: `card:${card.id}`, onClose: () => undefined, card: <ActionCard key={card.id} card={card} lang={companion.lang} onDecide={decideCard}/> }
    : carded && question ? { key: `question:${question.id}`, onClose: () => undefined, card: <QuestionCard key={question.id} question={question} lang={companion.lang} onAnswer={answerQuestion}/> }
    : !notice ? null : notice.kind === 'pop' ? { key: notice.key, pop: notice.ids, onClose: notices.next } : { key: notice.key, id: notice.id, onClose: notices.fold,
    card: <NoticeCard key={notice.key} n={notice} card={notices.card!} agent={agents.find(a => a.id === notice.id)} count={notices.count} look={wardrobe.marks}
      onPark={() => notices.park([notice.id])} onOpen={jump} onChange={notices.bump} onResolve={(text, body) => {
        if (notice.kind !== 'req') return;
        void notices.resolve(notice, text, body);
        if (body.decision !== 'deny') ball.current?.hop(.14);
      }}/> };
  // The strip and her bubble share one spot: her words keep it until yours are in, then the strip shows them whole.
  const yours = voice === 'thinking' && !!caption;
  const strip = place === 'out' && (yours || ((voice === 'listening' || voice === 'thinking') && !reply.text)), bubble = place === 'out' && !!reply.text && !yours;
  return <IconContext.Provider value={{ size: 16, weight: 'regular' }}>
    <main ref={root} className="companion" onContextMenu={event => {
      if (!(event.target as Element).closest('.companion-hit')) return;
      event.preventDefault(); cancel();
      setMenu({ x: Math.min(geo.width - 230, Math.max(8, event.clientX)), y: Math.max(placement.topInset + 12, event.clientY) });
    }}>
      <button className="companion-island-target" data-hit aria-label={t(['Open Dashboard', '打开主页'])} title={t(['Click the notch to open Dashboard', '点击刘海打开主页'])}
        style={{ left: geo.lobe.left, width: geo.lobe.notched ? geo.wingX - geo.lobe.left : geo.lobe.right - geo.lobe.left, height: placement.topInset }}
        onClick={toggleDashboard}/>
      {menu && <div ref={menuRef} className="companion-menu" data-hit role="menu" aria-label={t(['Her menu', '她的菜单'])} style={{ left: menu.x, top: menu.y }}
        onKeyDown={event => {
          const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button')], at = items.indexOf(document.activeElement as HTMLButtonElement);
          if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
            event.preventDefault();
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (at + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
            items[next]?.focus();
          }
        }}>
        {SKIN_KEYS.map(skin => <button key={skin} role="menuitemradio" aria-checked={wardrobe.skin === skin} onClick={() => { choose(skin); setMenu(null); }}>{t(SKIN_NAMES[skin])}</button>)}
        <hr/>
        <button role="menuitem" onClick={() => { setMenu(null); appear(ctl.playFaces, PREVIEW.length * 1100); }}>{t(['Preview expressions', '看一遍表情'])}</button>
        <button role="menuitem" onClick={() => { setMenu(null); openDashboard(false); pinned.current = true; setSettingsFocus(n => n + 1); }}>{t(['Settings…', '设置…'])}</button>
      </div>}
      <div className={`companion-chip ${chip ? 'is-open' : ''}`} data-hit={chip || undefined} data-glass="10" style={{ left: out.x + R + 12, top: out.y - 13 }}>
        <button aria-label={t(['Type to her', '文字输入'])} tabIndex={chip ? 0 : -1} onClick={openComposer}><Keyboard/></button>
      </div>
      <form className={`companion-composer ${composer && place === 'out' ? 'is-open' : ''}`} data-hit={composer || undefined} data-glass="14"
        style={{ left: out.x - PANEL / 2, top: out.y + R + 11 }} inert={!composer} onTransitionEnd={aimAtCaret}
        onSubmit={event => { event.preventDefault(); send(); }}>
        <button type="button" className="composer-attach" disabled aria-label={t(['Attach (not wired yet)', '添加附件（还没接）'])}><Paperclip/></button>
        <input ref={input} aria-label={t(['Type to her', '文字输入'])} placeholder={t(['Say something…', '和她说点什么…'])} value={draft}
          onChange={event => { setDraft(event.target.value); ball.current?.nudge(); requestAnimationFrame(aimAtCaret); }}
          onSelect={aimAtCaret} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); closeComposer(); } }}/>
        <button type="submit" className="composer-send" disabled={!draft.trim()} aria-label={t(['Send', '发送'])}><ArrowUp weight="bold"/></button>
      </form>
      <div className={`companion-strip ${strip ? 'is-open' : ''} ${hearing ? 'is-hearing' : ''} ${deep ? 'is-deep' : ''}`} data-hit={strip || undefined} data-glass="14"
        style={{ left: out.x, top: out.y + R + 11 }} inert={!strip} role="status">
        <span className="strip-mic"><Microphone size={14} weight="fill"/></span>
        <span className={`strip-text ${caption ? '' : 'is-empty'}`}>{caption || t(['Listening…', '在听…'])}</span>
        {deepSecs > 0 && <span className="strip-think">{t([`Thinking ${deepSecs} s`, `深想 ${deepSecs} 秒`])}</span>}
        <button className="strip-stop" aria-label={t(['End voice', '结束语音'])} onClick={endVoice}><Stop size={11} weight="fill"/></button>
      </div>
      <div className={`companion-bubble ${bubble ? 'is-open' : ''} ${answerSecs ? 'is-deep' : ''}`} data-glass="14" style={{ left: out.x, top: out.y + R + 11 }} role="status">
        {answerSecs > 0 && <small className="bubble-think">{t([`Thought for ${answerSecs.toFixed(1)} s`, `想了 ${answerSecs.toFixed(1)} 秒`])}</small>}
        <span className="bubble-text"><span className="bubble-ghost">{reply.text}</span><span>{reply.text.slice(0, reply.shown)}</span></span>
      </div>
      <div className={`companion-dashboard ${dashboard ? 'is-open' : ''}`} data-hit={dashboard || undefined} data-glass="24"
        style={{ left: geo.center - PANEL / 2, top: geo.panelTop }} inert={!dashboard}>
        <AroundDashboard open={dashboard} port={port} onClose={() => setDashboard(false)} onMood={setDashMood} settingFocus={settingsFocus} onHop={height => ball.current?.hop(height)}
          talk={port ? { rows: s.rows, tail, busy: s.phase === 'processing', offline: s.phase === 'error', floor, submit, older, card, decide: decideCard, question, answer: answerQuestion,
            think: { on: deep, secs: deepSecs, words, thoughts, exit: () => void submit(t(['stop thinking', '不用想了'])) } } : undefined}
          plugins={port ? plugins : undefined} pluginFocus={pluginFocus} marks={wardrobe.marks} onAgents={setAgents} unread={notices.unread}
          onAnswer={id => { setDashboard(false); notices.focus(id); }} ctl={ctl}/>
      </div>
      <Notch look={wardrobe.marks} agents={agents} unread={notices.unread} parked={notices.parked} archived={notices.archived} cursor={cursor} quiet={dashboard || moving}
        onNoteHover={notices.setHover} geo={{ width: geo.width, top: placement.topInset, notchR: geo.wingX, lobeL: geo.lobe.left }} note={note}
        act={{ jump, answer: notices.focus, read: notices.read, back: notices.back, archive: notices.archive, park: notices.park, unpark: notices.unpark }}
        port={port} keys={keysPress} onViewing={setViewing} onKeys={on => { setKeysOn(on); void window.jarvis?.focus(on); }}/>
      <CompanionBall width={geo.width} height={placement.topInset + 560} lobe={geo.lobe} look={look} handle={ball} skin={worn.current}
        target={{ place, expr, pressed, anchors: geo.anchors, home: wardrobe.home, homeFace: !!notice || carded,
          attention: noticeLook ? { id: carded ? `card:${card?.id ?? question?.id}` : notice!.key, point: noticeLook } : undefined,
          away: trip === 'out', happy: trip === 'happy', deep: deep && expr === '02' }}
        label={voice === 'off' ? t([`Poke to talk${port ? '' : ' (demo)'}`, `戳一下，开始语音${port ? '' : '（演示）'}`]) : voice === 'speaking' ? t(['Poke to interrupt', '戳一下，打断播报']) : t(['Poke to stop', '戳一下，结束语音'])}
        onPress={press} onRelease={release} onCancel={cancel} onMove={refreshHit}/>
    </main>
  </IconContext.Provider>;
}
