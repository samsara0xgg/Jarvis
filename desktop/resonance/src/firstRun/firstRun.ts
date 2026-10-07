// Jarvis's first launch, from macOS's 打开 to her home beside the notch (the 初见 lab, artifact 9AXSt1fu v8).
// Her body, faces and light are the companion's own star core. The entrance is the 跃迁 take: a star chart that
// finds her in three hops, a flight that speeds up to light speed, a flash, and she is there. Then she moves into
// the notch and walks you through setup; what you pick is saved through the daemon, and 进入 hands over to the
// companion. The UI cues are the 星核 kit (fifths) in the bright drop, all through the entrance's master.
import { Core, TURN_FACE, spring, step, type ExprId } from '../starCore';
import { NOTE, play, scoreOf, SCALE, type Cue, type Note } from '../soundKit';
import { UI_PAL, bellSoft, tick as strike } from './voices';
import notionLogo from './logos/notion.png';
import './firstRun.css';

type V2 = [number, number];
type Info = { top: number; notch: number; cursor: V2; name: string; lang: Lang; port: string };
declare global { interface Window { firstRun: {
  info: () => Promise<Info>;
  permission: (kind: Perm, ask: boolean, note?: [string, string]) => Promise<PermState>;
  open: (page: 'openai' | 'minimax' | 'tavily') => void;
  passthrough: (on: boolean) => void;
  done: () => void;
  quit: () => void;
} } }
const $ = <T extends HTMLElement = HTMLElement>(s: string, r: ParentNode = document) => r.querySelector(s) as T;
const clamp = (v: number, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, v));
const lerp = (a: number, b: number, k: number) => a + (b - a) * k;
const seg = (e: number, a: number, b: number) => clamp((e - a) / (b - a));
const eOut = (k: number) => 1 - (1 - k) ** 3;
const eIn = (k: number) => k ** 3;
const eInOut = (k: number) => k < .5 ? 4 * k ** 3 : 1 - (-2 * k + 2) ** 3 / 2;
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const rgb = (c: number[], a = 1) => `rgba(${Math.round(c[0] * 255)},${Math.round(c[1] * 255)},${Math.round(c[2] * 255)},${a})`;
const TAU = Math.PI * 2;
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const bridge = window.firstRun;

// ---------- geometry (CSS px): the menu bar height and the camera's width come from the main process ----------
let TOP = 32, NOTCH = 0;
const R_HOME = 13, R_PANEL = 25;
// ---------- the acts, ms from each act's start ----------
// Act one (the entrance) is written in design ms and played STRETCH times slower; RING lengthens every bell.
// It starts when you click 打开 on macOS's own dialog. The star chart runs on its clock e; the flight on
// te = e - OFF: the stars start moving at 900, reach light speed (the flash) at 3250, and she is there at BORN.
const STRETCH = 2, RING = 1.6, BORN = 3330, R_START = .3;
// The flight's speed as the stars' depth speed: one smooth exponential climb (twice as fast every 0.85 s) from a
// soft start to VZ1 at the flash, returned as spd (the inverse of vzOf) so everything that follows spd works.
const VZ0 = .3, VZ1 = 14;
const jumpSpd = (te: number) => {
  if (te < 900) return 0;
  const u = seg(te, 900, 3250), k = seg(u, 0, .08), vz = VZ0 * (VZ1 / VZ0) ** u * k * k * (3 - 2 * k);
  return (Math.max(0, vz - .012) / 3.2) ** (1 / 2.2);
};
const JTOP = jumpSpd(3250);
// The star chart: three hops to find her, then the flight. Her star sits at V; `look` is the point of the sky
// at the screen's centre, measured from her star (V = centre - look). Each hop swings the view to the next
// bright star and a finder ring closes on it; the third star is her.
const HOPS: [number, number][] = [[400, 650], [1050, 1300], [1700, 1950]];   // swing start and end
const CLOSE = 125, VERDICT = [950, 1600, 2150], WARP = 2350, OFF = WARP - 900;
const LOOK0 = (): V2 => [-.08 * W, .26 * H];
// Real coordinates for the two wrong stars (the Orion Nebula, Vega); hers is today's date.
const two = (n: number) => String(n).padStart(2, '0'), today = new Date();
const CANDS = (): { o: V2; label: string }[] => [
  { o: [-.3 * W, -.06 * H], label: fmt(tx().radec, { ra: '05h 35m', dec: '−05° 23′' }) },
  { o: [.3 * W, .2 * H], label: fmt(tx().radec, { ra: '18h 36m', dec: '+38° 47′' }) },
  { o: [0, 0], label: fmt(tx().radec, { ra: `${two(today.getMonth() + 1)}h ${two(today.getDate())}m`, dec: '+25° 09′' }) },
];
function lookOf(e: number): V2 {
  let from = LOOK0();
  for (let i = 0; i < HOPS.length; i++) {
    const [a, b] = HOPS[i], to = CANDS()[i].o;
    if (e < a) return from;
    if (e < b) { const k = reduced.matches ? 1 : eInOut(seg(e, a, b)); return [lerp(from[0], to[0], k), lerp(from[1], to[1], k)]; }
    from = to;
  }
  return from;
}
const B = { crouch: 250, fly: 420, lobe: 800, land: 1120, edge: 1250, edgeEnd: 2500, open: 2600 };
const D = { retract: 200, reveal: 700, revealEnd: 1700, hi: 1500, bye: 6500 };
const RAINBOW = ['#8fa4ff', '#b98cff', '#ff9ec8', '#ffc98f', '#6fe0b4', '#8fe3ff'];

// ---------- copy ----------
type Lang = 'zh' | 'en';
type Perm = 'mic' | 'screen' | 'auto' | 'notify';
type PermState = '' | 'ok' | 'relaunch' | 'asked' | 'later';
type Conn = 'notion' | 'ms' | 'web';
type Reason = 'unauthorized' | 'model_denied' | 'quota' | 'rate_limited' | 'network' | 'timeout' | 'missing_key';
const TX = {
  zh: {
    tag: '住在你 Mac 上的助手', go: '开始', goHint: '或者按回车', skipIntro: '跳过动画', quit: '退出', radec: '赤经 {ra}   赤纬 {dec}',
    next: '继续', skip: '跳过', must: '这一步必填', enter: '进入 {a}', test: '测试', testing: '测试中',
    say1: '先认识一下。我该怎么称呼你？', ph1: '你的名字', src1: '来自 Mac 账户', note1: '只用来称呼你，随时能在设置里改。', hi1: '你好，{u}！',
    say2: '那我呢？给我起个名字吧。', rec: '推荐', nova: '新星', own: '自己起', ph2: '给她起个名字',
    note2: '唤醒词一直是 “Hey Jarvis”，起什么名字都能这样叫醒我。', hi2: '{a}……我喜欢这个名字。',
    say3: '我们用什么语言聊？', sys: '跟随系统', sysV: '现在是{l}', names: { zh: '简体中文', en: '英文' } as Record<Lang, string>, zhV: '界面和说话都用中文', enV: 'UI and voice in English',
    note3: '界面和我说话都会换成这个语言，之后也能改。',
    say4: '我要靠 OpenAI 思考和说话。贴一个你的 API key 给我吧。',
    rows4: ['连上 OpenAI', '对话', '深度任务', '实时语音'], first: '她用这把 key 说的第一句话', secs: '{s} 秒',
    why4: {
      unauthorized: '{p} 拒绝了这个 key（401）。看看是不是少复制了一截，或者新建一个再贴。',
      model_denied: '这个 key 用不了“{row}”要的模型。在 {p} 后台给这个项目打开模型权限，再测一次。',
      quota: '这个账户没有额度了。去 {p} 充值后再测一次。',
      rate_limited: '{p} 说请求太多了，等一分钟再测。',
      missing_key: '先贴一个 key 进来。',
      network: '连不上 {p}。看看网络，再测一次。',
      timeout: '{p} 太久没回应。再测一次。',
    } as Record<Reason, string>,
    failed: '没测通：{d}', away: '连不上 Jarvis 的后台，现在测不了。等一下再试。', old: 'Jarvis 的后台还不认识这一步，要更新后重开。',
    note4: '只存在这台 Mac 的钥匙串里，不经过任何服务器。还没有 key？去 {link} 新建一个。', hi4: '好，我能思考了。',
    say5: '想听我说话的话，再给我一把 MiniMax 的 key，然后挑一个声音。', ph5: 'MiniMax API key', lock5: '测通 key 之后才能试听',
    hear: '你好，我是 {a}。今天想先做点什么？', note5: '不填也能用，我就只打字、不出声。还没有 key？去 {link} 新建一个。', ok5: 'MiniMax 连上了，点一个声音听听', textOnly: '只打字',
    say6: '要帮上忙，我还需要几样权限。每一样都可以以后再给。', allow: '允许', allowed: '已允许', relaunch: '重开后生效', asked: '已请求',
    perms: {
      mic: ['麦克风', '听到 “Hey Jarvis” 才开始听，唤醒词在这台 Mac 上识别。'],
      screen: ['屏幕录制', '看你在用什么、做什么，用来写每天的日报。截图只留在本机。'],
      auto: ['控制终端', 'Claude Code 需要你的时候，把你带回那个终端窗口。'],
      notify: ['通知', '任务做完、早报到了，在右上角告诉你。'],
    } as Record<Perm, string[]>,
    ping: '以后我就这样提醒你。',
    say7: '最后，把常用的地方接上吗？我能替你看日程、查邮件、整理笔记。', connect: '连接', busy: '在浏览器里登录', connected: '已连接', na: '还不能连',
    conns: { notion: ['Notion', '读写你的笔记和页面'], ms: ['Microsoft', 'To Do 待办、Outlook 邮件和日历'], web: ['网页搜索', '上网查资料，要一个 Tavily key'] } as Record<Conn, string[]>,
    ph7: 'Tavily API key', note7: '去 {link} 免费申请一个。',
    say8: '都准备好了，{u}。', recap: ['称呼', '助手', '语言', 'OpenAI', '声音', '权限', '连接'], works: '能用', none: '以后再接', and: '、',
    tip8: '双击刘海旁边的我打开 Dashboard，或者说 “Hey Jarvis”。跳过的都能在设置里补。', saving: '保存中', unsaved: '没能保存：{e}。再按一次试试。',
    bubble: '{u}，我在这儿。双击我，随时找我。',
  },
  en: {
    tag: 'The assistant that lives on your Mac', go: 'Begin', goHint: 'or press Return', skipIntro: 'Skip animation', quit: 'Quit', radec: 'RA {ra}   Dec {dec}',
    next: 'Continue', skip: 'Skip', must: 'Required', enter: 'Enter {a}', test: 'Test', testing: 'Testing',
    say1: 'Let’s get acquainted. What should I call you?', ph1: 'Your name', src1: 'From your Mac account', note1: 'Only used to address you. Change it any time in Settings.', hi1: 'Hi, {u}!',
    say2: 'And me? Give me a name.', rec: 'Suggested', nova: 'New star', own: 'Your own', ph2: 'Her name',
    note2: 'The wake word is always “Hey Jarvis”, whatever you call me.', hi2: '{a}… I like it.',
    say3: 'Which language should we use?', sys: 'Match system', sysV: 'Currently {l}', names: { zh: 'Simplified Chinese', en: 'English' } as Record<Lang, string>, zhV: '界面和说话都用中文', enV: 'UI and voice in English',
    note3: 'The interface and my voice both switch. You can change it later.',
    say4: 'I think and talk through OpenAI. Paste an API key for me.',
    rows4: ['Reach OpenAI', 'Chat', 'Deep tasks', 'Live voice'], first: 'Her first words with this key', secs: '{s} s',
    why4: {
      unauthorized: '{p} rejected this key (401). Check that it was copied in full, or create a new one and paste it.',
      model_denied: 'This key can’t use the model “{row}” needs. Allow it for this project in the {p} dashboard, then test again.',
      quota: 'This account is out of credit. Top it up at {p}, then test again.',
      rate_limited: '{p} says too many requests. Wait a minute and test again.',
      missing_key: 'Paste a key first.',
      network: 'Couldn’t reach {p}. Check the network and test again.',
      timeout: '{p} took too long to answer. Test again.',
    } as Record<Reason, string>,
    failed: 'The test failed: {d}', away: 'Jarvis’s background service isn’t answering, so I can’t test yet. Try again in a moment.', old: 'Jarvis’s background service doesn’t know this step yet; it needs an update and a restart.',
    note4: 'Stored only in this Mac’s Keychain, never on a server. No key yet? Create one at {link}.', hi4: 'Good, I can think now.',
    say5: 'If you want to hear me, add a MiniMax key and pick a voice.', ph5: 'MiniMax API key', lock5: 'Test the key to preview voices',
    hear: 'Hi, I’m {a}. What shall we start with today?', note5: 'Works without it; I’ll just type. No key yet? Create one at {link}.', ok5: 'MiniMax connected. Tap a voice to hear it', textOnly: 'Text only',
    say6: 'To be useful I need a few permissions. Each one can wait.', allow: 'Allow', allowed: 'Allowed', relaunch: 'After relaunch', asked: 'Asked',
    perms: {
      mic: ['Microphone', 'I only listen after “Hey Jarvis”; the wake word is detected on this Mac.'],
      screen: ['Screen Recording', 'Lets me see what you are working on for your daily report. Screenshots stay on this Mac.'],
      auto: ['Control Terminal', 'When Claude Code needs you, I take you back to its terminal window.'],
      notify: ['Notifications', 'I tell you when a task finishes or the morning brief is ready.'],
    } as Record<Perm, string[]>,
    ping: 'This is how I’ll let you know.',
    say7: 'Last one: connect the places you use? I can read your calendar, mail and notes for you.', connect: 'Connect', busy: 'Signing in', connected: 'Connected', na: 'Not available yet',
    conns: { notion: ['Notion', 'Read and write your pages'], ms: ['Microsoft', 'To Do, Outlook mail and calendar'], web: ['Web search', 'Look things up; needs a Tavily key'] } as Record<Conn, string[]>,
    ph7: 'Tavily API key', note7: 'Get a free one at {link}.',
    say8: 'All set, {u}.', recap: ['Name', 'Assistant', 'Language', 'OpenAI', 'Voice', 'Permissions', 'Connections'], works: 'Working', none: 'Later', and: ', ',
    tip8: 'Double-click me next to the notch to open the Dashboard, or say “Hey Jarvis”. Anything skipped is in Settings.', saving: 'Saving', unsaved: 'Couldn’t save: {e}. Press it again to retry.',
    bubble: 'I’m right here, {u}. Double-click me any time.',
  },
};

// ---------- what the user has set (the account name and the system language come from the main process) ----------
type Voice = { id: string; label: string; note: string };
type Check = { id: string; ok: boolean; reason?: string; detail?: string };
const S = {
  lang: 'en' as Lang, sysLang: 'en' as Lang, langPick: 'system' as 'system' | Lang, account: '', user: '', asst: 'Jarvis', asstPick: 'Jarvis',
  keyState: 'idle' as 'idle' | 'testing' | 'ok' | 'bad', keyRows: [0, 0, 0, 0], keyMs: 0, keyWhy: '', firstLine: '',
  mmState: 'idle' as 'idle' | 'testing' | 'ok' | 'bad', mmWhy: '', voices: [] as Voice[], voice: -1, playing: -1,
  perms: { mic: '', screen: '', auto: '', notify: '' } as Record<Perm, PermState>,
  conns: { notion: '', ms: '', web: '' } as Record<Conn, '' | 'busy' | 'ok' | 'na'>, connWhy: '', webOpen: false,
  saving: false, saveWhy: '',
  done: new Set<number>(), skipped: new Set<number>(),
};
const tx = () => TX[S.lang];
const fmt = (s: string, v: Record<string, string> = {}) => s.replace(/\{(\w+)\}/g, (_, k: string) => v[k] ?? (k === 'u' ? S.user : k === 'a' ? S.asst : ''));

// ---------- the daemon: the renderer's requests get the local key from the main process ----------
let PORT = '8006';
async function api<T = Record<string, unknown>>(path: string, body?: unknown, ms = 60000): Promise<T> {
  let r: Response;
  try {
    r = await fetch(`http://127.0.0.1:${PORT}${path}`, body === undefined ? { signal: AbortSignal.timeout(ms) }
      : { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body), signal: AbortSignal.timeout(ms) });
  } catch { throw new Error(tx().away); }
  if (!r.ok) {
    const e = await r.json().catch(() => ({})) as { detail?: unknown };
    throw new Error(typeof e.detail === 'string' ? e.detail : r.status === 404 ? tx().old : `HTTP ${r.status}`);
  }
  return (r.headers.get('content-type') ?? '').includes('json') ? await r.json() as T : await r.blob() as T;
}

// ---------- sound ----------
// One bright master (the entrance's): rumble out, a little air on top for the original music, glue, a limiter;
// a wide room fed through a highpass and a ping-pong echo for the sparkles. Nothing rounds the highs off.
// The UI cues play through it on five fixed pan positions, in the palette picked in the toolbar.
let ac: AudioContext | null = null, out: GainNode | null = null, noise: AudioBuffer | null = null;
let inp: GainNode, revIn: GainNode, echoIn: GainNode, noiseR: AudioBuffer;
const SCORE = scoreOf('fifths');
const PAL = UI_PAL.dropBright;
// The entrance's music sits a fourth below the bright original (A major down to E major) and stops its wind lower.
const TR = 2 ** (-5 / 12), HI = .65;
const PANS = [-.7, -.35, 0, .35, .7], panBus: (StereoPannerNode | null)[] = PANS.map(() => null);
function whiteBuffer(a: BaseAudioContext, s: number) {
  const b = a.createBuffer(1, Math.round(a.sampleRate * s), a.sampleRate), d = b.getChannelData(0);
  for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
  return b;
}
function audio() {
  if (!ac) {
    const a = ac = new AudioContext();
    const filt = (type: BiquadFilterType, f: number, q = .7, gain = 0) => { const n = a.createBiquadFilter(); n.type = type; n.frequency.value = f; n.Q.value = q; n.gain.value = gain; return n; };
    const comp = (th: number, ratio: number, att: number, rel: number, knee: number) => {
      const c = a.createDynamicsCompressor(); c.threshold.value = th; c.ratio.value = ratio; c.attack.value = att; c.release.value = rel; c.knee.value = knee; return c;
    };
    const vol = a.createGain(), lim = comp(-2, 20, .001, .08, 0); vol.gain.value = .9;
    inp = a.createGain();
    inp.connect(filt('highpass', 32)).connect(comp(-18, 3.5, .003, .2, 8)).connect(vol).connect(lim).connect(a.destination);
    const n = Math.round(a.sampleRate * 3.4), ir = a.createBuffer(2, n, a.sampleRate);
    for (let ch = 0; ch < 2; ch++) { const d = ir.getChannelData(ch); for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / n) ** 2.4; }
    const conv = a.createConvolver(), ret = a.createGain(); conv.buffer = ir; ret.gain.value = .5;
    revIn = a.createGain(); revIn.connect(filt('highpass', 280)).connect(conv).connect(ret).connect(inp);
    const dl = a.createDelay(1), dr = a.createDelay(1), fl = a.createGain(), fr = a.createGain(), pl = a.createStereoPanner(), pr = a.createStereoPanner();
    dl.delayTime.value = dr.delayTime.value = .19; fl.gain.value = fr.gain.value = .4; pl.pan.value = -.85; pr.pan.value = .85;
    echoIn = a.createGain(); echoIn.connect(filt('highpass', 800)).connect(dl);
    dl.connect(pl).connect(inp); dl.connect(fl).connect(dr); dr.connect(pr).connect(inp); dr.connect(fr).connect(dl);
    out = a.createGain(); out.gain.value = .95; out.connect(inp);
    noise = whiteBuffer(a, 2); noiseR = whiteBuffer(a, 2);
  }
  void ac.resume();
}
const audible = () => !!ac && !!out;
// Where she is on screen decides where she is heard.
const panX = (x: number) => clamp((x / W - .5) * 1.6, -.8, .8);
function bus(pan: number) {
  const i = PANS.reduce((b, p, j) => Math.abs(p - pan) < Math.abs(PANS[b] - pan) ? j : b, 0);
  if (!panBus[i]) { const p = ac!.createStereoPanner(); p.pan.value = PANS[i]; p.connect(out!); panBus[i] = p; }
  return panBus[i]!;
}
function cue(c: string | Cue, gain = 1, low = false, pan = 0) {
  if (!audible()) return;
  const k = typeof c === 'string' ? SCORE[c] : c, p = PAL;
  if (k) play(ac!, bus(pan), k, low ? { ...p, oct: .5 } : p, gain);
}
// Filtered noise that swells and fades; `pan` may glide from one side to the other over its length.
function air(dur: number, f0: number, f1: number, peak: number, type: BiquadFilterType = 'lowpass', pan: number | V2 = 0) {
  if (!audible() || !noise) return;
  const a = ac!, t = a.currentTime + .01, src = a.createBufferSource(), f = a.createBiquadFilter(), g = a.createGain(), p = a.createStereoPanner();
  const [p0, p1] = typeof pan === 'number' ? [pan, pan] : pan;
  src.buffer = noise; src.loop = true; f.type = type; f.Q.value = type === 'bandpass' ? 1.1 : .6;
  f.frequency.setValueAtTime(f0, t); f.frequency.exponentialRampToValueAtTime(f1, t + dur);
  g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(peak, t + dur * .55); g.gain.exponentialRampToValueAtTime(.0001, t + dur);
  p.pan.setValueAtTime(p0, t); p.pan.linearRampToValueAtTime(p1, t + dur);
  src.connect(f).connect(g).connect(p).connect(out!); src.start(t); src.stop(t + dur + .05);
}
// A low sine hit that falls in pitch, with its octave so laptop speakers carry it.
function thump(f0: number, f1: number, dur: number, peak: number, pan = 0) {
  if (!audible()) return;
  const a = ac!, t = a.currentTime + .01;
  for (const [k, p] of [[1, peak], [2, peak * .35]]) {
    const o = a.createOscillator(), g = a.createGain();
    o.frequency.setValueAtTime(f0 * k, t); o.frequency.exponentialRampToValueAtTime(f1 * k, t + dur * .7);
    g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(p, t + .008); g.gain.exponentialRampToValueAtTime(.0001, t + dur);
    o.connect(g).connect(bus(pan)); o.start(t); o.stop(t + dur + .05);
  }
}
const tick = (i: number): Cue => ({ hits: [[0, SCALE[5 + i], .2, 1]] });

// The entrance's own voices: bells with their overtones, air, the flight bed, the hit.
const sec = (ms: number) => ms * STRETCH / 1000;         // design ms to real seconds
// One voice's exit: panned into the master, with sends to the room and the echo.
function outlet(pan: number, wet: number, echo = 0) {
  const a = ac!, g = a.createGain(), p = a.createStereoPanner();
  p.pan.value = clamp(pan, -1, 1); g.connect(p).connect(inp);
  if (wet) { const s = a.createGain(); s.gain.value = wet; p.connect(s).connect(revIn); }
  if (echo) { const s = a.createGain(); s.gain.value = echo; p.connect(s).connect(echoIn); }
  return g;
}
function noiseSrc(right: boolean) { const s = ac!.createBufferSource(); s.buffer = right ? noiseR : noise; s.loop = true; return s; }
function bell(n: Note | number, v: number, len = 1, pan = 0, wet = .3, echo = 0, delayMs = 0) {
  if (!audible()) return;
  const f = (typeof n === 'number' ? n : NOTE[n]) * TR;
  bellSoft(ac!, outlet(pan, wet, echo), ac!.currentTime + .005 + sec(delayMs), f, v * 1.15, len * RING, false, false);
}
function chord(notes: Note[], v: number, len: number, strumMs = 12, delayMs = 0) {
  notes.forEach((n, i) => bell(n, v * (1 - i * .05), len, (i / (notes.length - 1) - .5) * 1.1, .45, i >= notes.length - 2 ? .3 : 0, delayMs + i * strumMs));
}
function arp(notes: Note[], v: number, gapMs: number, len: number, pan0: number, pan1: number, echo = .25, delayMs = 0) {
  notes.forEach((n, i) => bell(n, v, len, lerp(pan0, pan1, i / Math.max(1, notes.length - 1)), .35, echo, delayMs + i * gapMs));
}
// Noise through a moving filter with a quick rise and a long fall, left and right decorrelated.
function sweep(ms: number, type: BiquadFilterType, f0: number, f1: number, peak: number, wet = .2, q = .8) {
  if (!audible()) return;
  const a = ac!, t = a.currentTime + .005, d = sec(ms);
  for (const right of [false, true]) {
    const s = noiseSrc(right), f = a.createBiquadFilter(), g = a.createGain();
    f.type = type; f.Q.value = q; f.frequency.setValueAtTime(f0 * HI, t); f.frequency.exponentialRampToValueAtTime(f1 * HI, t + d);
    g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(peak, t + d * .15); g.gain.exponentialRampToValueAtTime(.0001, t + d);
    s.connect(f).connect(g).connect(outlet(right ? .6 : -.6, wet)); s.start(t, Math.random()); s.stop(t + d + .05);
  }
}
// Air that swells into a hit and stops dead on it, brighter and louder all the way.
function swell(ms: number, peak: number, f0 = 500, f1 = 9000) {
  if (!audible()) return;
  const a = ac!, t = a.currentTime + .005, d = sec(ms);
  for (const right of [false, true]) {
    const s = noiseSrc(right), f = a.createBiquadFilter(), g = a.createGain();
    f.type = 'highpass'; f.frequency.setValueAtTime(f0 * HI, t); f.frequency.exponentialRampToValueAtTime(f1 * HI, t + d);
    g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(peak, t + d); g.gain.setValueAtTime(0, t + d + .004);
    s.connect(f).connect(g).connect(outlet(right ? .55 : -.55, .15)); s.start(t, Math.random()); s.stop(t + d + .05);
  }
}
// The view swinging across the chart: a band of air that rises and falls with the swing and crosses the stereo
// field the way the stars slide.
function swipe(ms: number, dir: number) {
  if (!audible()) return;
  const a = ac!, t = a.currentTime + .005, d = sec(ms);
  const s = noiseSrc(false), f = a.createBiquadFilter(), g = a.createGain(), p = a.createStereoPanner(), r = a.createGain();
  f.type = 'bandpass'; f.Q.value = 1.1;
  f.frequency.setValueAtTime(500 * HI, t); f.frequency.exponentialRampToValueAtTime(2600 * HI, t + d * .5); f.frequency.exponentialRampToValueAtTime(900 * HI, t + d);
  g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(.12, t + d * .5); g.gain.exponentialRampToValueAtTime(.0001, t + d * 1.1);
  p.pan.setValueAtTime(-.7 * dir, t); p.pan.linearRampToValueAtTime(.7 * dir, t + d); r.gain.value = .2;
  s.connect(f).connect(g).connect(p).connect(inp); p.connect(r).connect(revIn); s.start(t, Math.random()); s.stop(t + d * 1.2);
}
// A soft low knock: a sine that drops onto its note, its octave and twelfth for small speakers, a click on top.
function knock(f: number, v: number, len = .4) {
  if (!audible()) return;
  const a = ac!, t = a.currentTime + .005, o0 = outlet(0, .12);
  f *= TR;
  for (const [k, amp, d] of [[1, .3, len], [2, .2, len * .45], [3, .07, len * .25]] as const) {
    const o = a.createOscillator(), g = a.createGain();
    o.type = k === 1 ? 'sine' : 'triangle';
    o.frequency.setValueAtTime(f * k * 1.9, t); o.frequency.exponentialRampToValueAtTime(f * k, t + .035);
    g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(amp * v, t + .004); g.gain.exponentialRampToValueAtTime(.0001, t + d);
    o.connect(g).connect(o0); o.start(t); o.stop(t + d + .05);
  }
  strike(a, o0, t, .05 * v, 1500, 6);
}
// The flight's own beat: notes that come faster and climb higher as the stars speed up, so the ear hears the
// acceleration the eye sees, and a knock on every fourth note under them, a drum roll that tightens with the
// speed. Nothing plays while the stars stand still.
let pulsePh = 0, pulseN = 0, rollPh = 0;
function pulse(dtReal: number, s: number) {
  if (s <= 0) return;
  const rate = 1.6 + 15 * s ** 1.5;
  pulsePh += dtReal * rate;
  while (pulsePh >= 1) {
    pulsePh -= 1;
    const i = pulseN++;
    bell(SCALE[Math.min(15, i % 5 + Math.floor(s * 9))], .12 + .26 * s, lerp(.9, .3, s), i % 2 ? .35 : -.35, .25, .15);
  }
  rollPh += dtReal * rate / 4;
  while (rollPh >= 1) { rollPh -= 1; knock(110, .4 + .8 * Math.min(s, 1.25) ** 2, lerp(.45, .16, clamp(s))); }
}
// The hit: a bright snap, a body of noise closing from 12 kHz down, a sub drop with its octave, and a low saw
// chord (A1 to A3) whose lowpass closes over two seconds: the weight that rings on under the bells.
function boom(v = 1) {
  if (!audible()) return;
  const a = ac!, t = a.currentTime + .005;
  strike(a, outlet(0, .3), t, .45 * v, 1800, 30);
  sweep(900, 'lowpass', 12000, 300, .3 * v, .35);
  for (const [k, type, amp, d] of [[1, 'sine', .5, 1.4], [2, 'triangle', .15, .5]] as const) {
    const o = a.createOscillator(), g = a.createGain();
    o.type = type; o.frequency.setValueAtTime(84 * k, t); o.frequency.exponentialRampToValueAtTime(44 * k, t + .3);
    g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(amp * v, t + .006); g.gain.exponentialRampToValueAtTime(.0001, t + d);
    o.connect(g).connect(inp); o.start(t); o.stop(t + d + .05);
  }
  const lp = a.createBiquadFilter(), g = a.createGain();
  lp.type = 'lowpass'; lp.Q.value = 1.2; lp.frequency.setValueAtTime(2600, t); lp.frequency.exponentialRampToValueAtTime(160, t + 2.4);
  g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(.075 * v, t + .012); g.gain.exponentialRampToValueAtTime(.0001, t + 3.2);
  lp.connect(g).connect(outlet(0, .35));
  for (const f of [55, 110, 164.81, 220]) for (const c of [-9, 9]) {
    const o = a.createOscillator(); o.type = 'sawtooth'; o.frequency.value = f * TR; o.detune.value = c;
    o.connect(lp); o.start(t); o.stop(t + 3.3);
  }
}
const HIT: Note[] = ['A4', 'E5', 'A5', 'Cs6', 'E6', 'B6'];
const SHIMMER: Note[] = ['A6', 'Cs7', 'E7', 'A7'];
const HELLO: Note[] = ['A5', 'Cs6', 'E6', 'A6', 'Cs7'];
const TICKS: Note[] = ['A6', 'B6', 'Cs7', 'E7', 'Fs7', 'A7'];

// The flight bed, driven every frame by the picture's speed: an open A chord of detuned saws behind a lowpass
// that opens as she speeds up, an endlessly rising Shepard glide, and wind whose band climbs into the treble.
type Bed = { pad: GainNode; lp: BiquadFilterNode; sh: OscillatorNode[]; shg: GainNode[]; shl: GainNode; ph: number; last: number[];
  bps: BiquadFilterNode[]; wg: GainNode[]; srcs: AudioScheduledSourceNode[] };
let bed: Bed | null = null;
function bedStart() {
  if (!audible() || bed) return;
  const a = ac!, t = a.currentTime, srcs: AudioScheduledSourceNode[] = [];
  const pad = a.createGain(), lp = a.createBiquadFilter(), hp = a.createBiquadFilter(), ps = a.createGain();
  pad.gain.value = .0001; lp.type = 'lowpass'; lp.Q.value = .9; lp.frequency.value = 900; hp.type = 'highpass'; hp.frequency.value = 80; ps.gain.value = .35;
  hp.connect(lp).connect(pad); pad.connect(inp); pad.connect(ps).connect(revIn);
  [110, 164.81, 220, 329.63, 440, 493.88, 554.37, 659.26].map(f => f * TR).forEach((f, i) => [-1, 1].forEach(side => {
    const o = a.createOscillator(), p = a.createStereoPanner();
    o.type = 'sawtooth'; o.frequency.value = f; o.detune.value = side * (7 + i); p.pan.value = side * .5;
    o.connect(p).connect(hp); srcs.push(o);
  }));
  const shl = a.createGain(), ss = a.createGain(); shl.gain.value = .0001; ss.gain.value = .3; shl.connect(inp); shl.connect(ss).connect(revIn);
  const sh: OscillatorNode[] = [], shg: GainNode[] = [];
  for (let i = 0; i < 6; i++) {
    const o = a.createOscillator(), g = a.createGain(), p = a.createStereoPanner();
    o.type = 'triangle'; o.frequency.value = 110 * TR * 2 ** i; g.gain.value = 0; p.pan.value = i % 2 ? .3 : -.3;
    o.connect(g).connect(p).connect(shl); sh.push(o); shg.push(g); srcs.push(o);
  }
  const bps: BiquadFilterNode[] = [], wg: GainNode[] = [];
  for (const right of [false, true]) {
    const s = noiseSrc(right), bp = a.createBiquadFilter(), g1 = a.createGain(), p = a.createStereoPanner();
    bp.type = 'bandpass'; bp.Q.value = .7; bp.frequency.value = 400;
    g1.gain.value = .0001; p.pan.value = right ? .65 : -.65;
    s.connect(bp).connect(g1).connect(p); p.connect(inp);
    bps.push(bp); wg.push(g1); srcs.push(s);
  }
  srcs.forEach(s => s.start(t));
  bed = { pad, lp, sh, shg, shl, ph: 0, last: sh.map(() => 0), bps, wg, srcs };
}
function bedSet(dtReal: number, spdK: number, lvl = 1) {
  if (!bed || !ac) return;
  const t = ac.currentTime, b = bed, k = clamp(spdK, 0, 1.2), k1 = Math.min(k, 1);
  b.lp.frequency.setTargetAtTime(900 * 2 ** (3.1 * k1), t, .06);
  b.pad.gain.setTargetAtTime(.0001 + .036 * lvl * clamp(k * 1.5) ** 1.3, t, .12);
  b.ph = (b.ph + dtReal * (.06 + 1.25 * k ** 1.6)) % 6;
  b.sh.forEach((o, i) => {
    const pos = (i + b.ph) % 6, f = 110 * TR * 2 ** pos;
    if (pos < b.last[i]) { o.frequency.cancelScheduledValues(t); o.frequency.setValueAtTime(f, t); } else o.frequency.setTargetAtTime(f, t, .02);
    b.last[i] = pos; b.shg[i].gain.setTargetAtTime(Math.exp(-((pos - 3.3) ** 2) / 1.8), t, .02);
  });
  b.shl.gain.setTargetAtTime(.0001 + .055 * lvl * clamp(k * 1.4) ** 1.3, t, .08);
  b.bps.forEach(f => f.frequency.setTargetAtTime(380 * 2 ** (3.6 * Math.min(k, 1.1)), t, .05));
  b.wg.forEach(g => g.gain.setTargetAtTime(.0001 + .2 * lvl * k ** 1.7, t, .05));
}
function bedCut(fade = .025, padFade = fade) {
  if (!bed || !ac) return;
  const b = bed, t = ac.currentTime; bed = null;
  for (const g of [b.pad, b.shl, ...b.wg]) { g.gain.cancelScheduledValues(t); g.gain.setTargetAtTime(0, t, g === b.pad ? padFade : fade); }
  b.srcs.forEach(s => s.stop(t + Math.max(fade, padFade) * 8 + .05));
}

// ---------- DOM ----------
const screenEl = $('#screen'), intro = $('#intro'), word = $('#word'), halo = $('#halo');
const panel = $('#panel'), pin = $('#pin'), bubble = $('#bubble'), skipBtn = $('#skip-intro'), quitBtn = $('#quit');
const bg = $<HTMLCanvasElement>('#bg'), fg = $<HTMLCanvasElement>('#fg');
const sky = $<HTMLCanvasElement>('#sky'), glc = $<HTMLCanvasElement>('#gl');
const bctx = bg.getContext('2d')!, fctx = fg.getContext('2d')!, sctx = sky.getContext('2d')!;
const eyeCv = document.createElement('canvas'), ectx = eyeCv.getContext('2d')!;

let W = 800, H = 600, dpr = 1, pw = 560;
// sd: canvas pixels per CSS px for the moving stars, redrawn every frame; soft light that does not need the screen's full resolution
const sd = 1;
const center = (): V2 => [W / 2, H * .4];
const R0 = () => clamp(Math.min(W, H) * .085, 44, 80);
const home = (): V2 => [NOTCH ? W / 2 - NOTCH / 2 - 32 : W / 2, TOP / 2];

// ---------- clock and phases (ms since the page started; each act counts from its own start) ----------
type Phase = 'wait' | 'intro' | 'hello' | 'movein' | 'setup' | 'finale';
let vt = 0, phase: Phase = 'wait', t0 = 0;
let fired = new Set<string>(), timers: { at: number; fn: () => void }[] = [];
const el = () => vt - t0;
function enter(p: Phase) { phase = p; t0 = vt; fired = new Set(); screenEl.dataset.phase = p; }
function once(k: string, at: number, fn: () => void) { if (el() >= at && !fired.has(k)) { fired.add(k); fn(); } }
function later(ms: number, fn: () => void) { timers.push({ at: vt + ms, fn }); }

// ---------- her ----------
const core = new Core('glass');
const ball = { x: spring(0), y: spring(0), R: spring(0), mode: 'off' as 'off' | 'drive' | 'center' | 'home' | 'panel', v: [0, 0] as V2 };
let face: ExprId = '00', over: { f: ExprId; until: number } | null = null, lookForce: V2 | null = null;
// pointerLive: she only follows the pointer once the entrance is over and it has moved; until then she looks at you
let pointer: V2 | null = null, pointerLive = false, typedAt = -1e9;
let trail: { x: number; y: number; R: number; t: number }[] = [];
const moment = (f: ExprId, ms: number) => { over = { f, until: vt + ms }; };

// ---------- the island (notch, her lobe, or the setup panel) ----------
const isl = { x0: spring(0), x1: spring(0), h: spring(TOP) };
let islMode: 'notch' | 'lobe' | 'panel' = 'notch', panelH = 320;
function islTarget(): [number, number, number] {
  const c = W / 2;
  if (islMode === 'panel') return [c - pw / 2, c + pw / 2, panelH];
  if (islMode === 'lobe') return NOTCH ? [c - NOTCH / 2 - 64, c + NOTCH / 2, TOP] : [c - 66, c + 66, TOP];
  return [c - NOTCH / 2, c + NOTCH / 2, TOP];
}
function snapIsland() { const [a, b, h] = islTarget(); Object.assign(isl.x0, { value: a, velocity: 0 }); Object.assign(isl.x1, { value: b, velocity: 0 }); Object.assign(isl.h, { value: h, velocity: 0 }); }
function ballTarget(): [number, number, number] {
  if (ball.mode === 'home') return [...home(), R_HOME];
  if (ball.mode === 'panel') return [W / 2, TOP + 34, R_PANEL];
  return [...center(), R0()];
}
function snapBall() { const [x, y, r] = ballTarget(); Object.assign(ball.x, { value: x, velocity: 0 }); Object.assign(ball.y, { value: y, velocity: 0 }); Object.assign(ball.R, { value: r, velocity: 0 }); }

let hole = 0, haloK = 0, edgeAt = -1e9, panelShown = false, islKey = '';

const STAR_C = ['#9bb0ff', '#aabfff', '#cad7ff', '#f4f6ff', '#f4f6ff', '#fff4ea', '#ffe2c0', '#ffc98f', '#b98cff', '#8fe3ff'];
// A star with a soft glow and four thin spikes, painted once and stamped.
const SPRITE = (() => {
  const c = document.createElement('canvas'); c.width = c.height = 128;
  const x = c.getContext('2d')!, g = x.createRadialGradient(64, 64, 0, 64, 64, 64);
  g.addColorStop(0, 'rgba(255,255,255,1)'); g.addColorStop(.08, 'rgba(235,240,255,.9)'); g.addColorStop(.25, 'rgba(170,190,255,.25)'); g.addColorStop(1, 'rgba(160,180,255,0)');
  x.fillStyle = g; x.fillRect(0, 0, 128, 128);
  for (const [w, h] of [[128, 2], [2, 128]]) {
    const l = w > h ? x.createLinearGradient(0, 0, 128, 0) : x.createLinearGradient(0, 0, 0, 128);
    l.addColorStop(0, 'rgba(220,230,255,0)'); l.addColorStop(.5, 'rgba(240,244,255,.8)'); l.addColorStop(1, 'rgba(220,230,255,0)');
    x.fillStyle = l; x.fillRect(64 - w / 2, 64 - h / 2, w, h);
  }
  return c;
})();

// ---------- act one: her sky, the chart and the flight (stars on the GPU, one capsule per star) ----------
const hex = (h: string) => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16) / 255);
const STAR_RGB = STAR_C.map(hex), RAINBOW_RGB = RAINBOW.map(hex);
const anyOf = <T>(a: T[]) => a[Math.floor(Math.random() * a.length)];
const hd = () => Math.hypot(W, H) / 2;
let P: V2 = [0, 0], V: V2 = [0, 0], panV: V2 = [0, 0];        // P: where 打开 was clicked; V: her star, the point we fly toward
let openR = 0, skyK = 1, spd = 0, seedK = 0, flashK = 0, glowK = 0, backK = 1, gridK = 0, candK = 0, born = false;
let rings: { at: number; dur: number; max: number; w: number }[] = [];
const sgl = glc.getContext('webgl', { premultipliedAlpha: true, antialias: false, alpha: true })!;
const STAR_VS = `attribute vec2 aP, aA, aB, aH; attribute vec4 aC; uniform vec2 uRes;
varying vec2 vP, vA, vB, vH; varying vec4 vC;
void main(){ vP = aP; vA = aA; vB = aB; vH = aH; vC = aC; gl_Position = vec4(aP.x / uRes.x * 2. - 1., 1. - aP.y / uRes.y * 2., 0., 1.); }`;
const STAR_FS = `precision highp float;
varying vec2 vP, vA, vB, vH; varying vec4 vC; uniform vec3 uM, uH; uniform float uD;
void main(){
  vec2 ab = vB - vA; float L2 = dot(ab, ab);
  float h = L2 > .01 ? clamp(dot(vP - vA, ab) / L2, 0., 1.) : 1.;
  float d = length(vP - vA - ab * h), w = vC.a, s = mix(vH.x, vH.y, h);
  float I = (exp(-d * d / (w * w)) + .2 * exp(-d / (w * 1.8))) * (.04 + .96 * s * s);
  I *= 1. - smoothstep(uM.z - 45. * uD, uM.z, length(vP - uM.xy));
  if (uH.z > 0.) I *= smoothstep(uH.z - 90. * uD, uH.z, length(vP - uH.xy));
  vec3 c = vC.rgb * I;
  gl_FragColor = vec4(c, max(c.r, max(c.g, c.b)));
}`;
const suni: Record<string, WebGLUniformLocation | null> = {};
{
  const sh = (type: number, src: string) => { const s = sgl.createShader(type)!; sgl.shaderSource(s, src); sgl.compileShader(s); if (!sgl.getShaderParameter(s, sgl.COMPILE_STATUS)) console.error(sgl.getShaderInfoLog(s)); return s; };
  const prog = sgl.createProgram()!;
  sgl.attachShader(prog, sh(sgl.VERTEX_SHADER, STAR_VS)); sgl.attachShader(prog, sh(sgl.FRAGMENT_SHADER, STAR_FS)); sgl.linkProgram(prog); sgl.useProgram(prog);
  sgl.bindBuffer(sgl.ARRAY_BUFFER, sgl.createBuffer());
  ([['aP', 2, 0], ['aA', 2, 8], ['aB', 2, 16], ['aH', 2, 24], ['aC', 4, 32]] as const).forEach(([n, size, off]) => {
    const l = sgl.getAttribLocation(prog, n); sgl.enableVertexAttribArray(l); sgl.vertexAttribPointer(l, size, sgl.FLOAT, false, 48, off);
  });
  suni.uRes = sgl.getUniformLocation(prog, 'uRes'); suni.uM = sgl.getUniformLocation(prog, 'uM'); suni.uH = sgl.getUniformLocation(prog, 'uH'); suni.uD = sgl.getUniformLocation(prog, 'uD');
  sgl.enable(sgl.BLEND); sgl.blendFunc(sgl.ONE, sgl.ONE);
}
const MAXC = 7000, VB = new Float32Array(MAXC * 72);
let nC = 0;
// A capsule from tail (a) to head (b) in CSS px; h0/h1 is how far along the whole streak its two ends sit.
function cap(ax: number, ay: number, bx: number, by: number, w: number, r: number, g: number, b: number, h0 = 0, h1 = 1) {
  if (nC >= MAXC || r + g + b < .003) return;
  const d = sd; ax *= d; ay *= d; bx *= d; by *= d; w *= d;
  let ux = bx - ax, uy = by - ay; const L = Math.hypot(ux, uy);
  if (L > .01) { ux /= L; uy /= L; } else { ux = 1; uy = 0; }
  const e = w * 6 + 1; ux *= e; uy *= e;
  const px = [ax - ux + uy, ax - ux - uy, bx + ux - uy, bx + ux + uy], py = [ay - uy - ux, ay - uy + ux, by + uy + ux, by + uy - ux];
  let o = nC * 72;
  for (const q of [0, 1, 2, 0, 2, 3]) {
    VB[o++] = px[q]; VB[o++] = py[q]; VB[o++] = ax; VB[o++] = ay; VB[o++] = bx; VB[o++] = by;
    VB[o++] = h0; VB[o++] = h1; VB[o++] = r; VB[o++] = g; VB[o++] = b; VB[o++] = w;
  }
  nC++;
}
function glFlush() {
  sgl.viewport(0, 0, glc.width, glc.height); sgl.clearColor(0, 0, 0, 0); sgl.clear(sgl.COLOR_BUFFER_BIT);
  if (nC) {
    const m = openR > hd() * 2.3 ? 1e6 : openR * sd;
    sgl.uniform2f(suni.uRes, glc.width, glc.height); sgl.uniform3f(suni.uM, P[0] * sd, P[1] * sd, m); sgl.uniform1f(suni.uD, sd);
    sgl.uniform3f(suni.uH, home()[0] * sd, home()[1] * sd, hole * sd);
    sgl.bufferData(sgl.ARRAY_BUFFER, VB.subarray(0, nC * 72), sgl.DYNAMIC_DRAW);
    sgl.drawArrays(sgl.TRIANGLES, 0, nC * 6);
  }
  nC = 0;
}
// Flight: stars in a cylinder ahead, depth 0..1, projected toward V; the camera moves, the stars stay.
// fovK < 1 widens the view (the tunnel looks deeper); span caps how far back in depth a streak's tail reaches.
type WStar = { x: number; y: number; z: number; m: number; c: number[]; tw: number };
let wfield: WStar[] = [], fovK = 1, span = 1.6;
const F = () => hd() * .5 * fovK;
const vzOf = (s: number) => .012 + 3.2 * s ** 2.2;
function wstar(z = Math.random()): WStar {
  const r = 3 * Math.sqrt(Math.random()), a = Math.random() * TAU;
  return { x: Math.cos(a) * r, y: Math.sin(a) * r, z: .012 + z * .988, m: .25 + .75 * Math.random() ** 2.6, c: anyOf(STAR_RGB), tw: Math.random() * TAU };
}
const count = (k: number) => reduced.matches ? 500 : Math.round(clamp(W * H / 900, 900, 2000) * k);
function makeWField() { wfield = Array.from({ length: count(1.4) }, () => wstar()); }
function flyStep(dt: number) {
  const vz = vzOf(spd), f = F();
  for (const st of wfield) {
    st.z -= vz * dt;
    const off = st.z < .5 && (Math.abs(st.x / st.z * f) > W * 1.2 || Math.abs(st.y / st.z * f) > H * 1.2);
    if (st.z < .012 || off) Object.assign(st, wstar(1 - Math.random() * Math.max(.04, vz * dt)));   // spread over this frame's travel
  }
}
function flyDraw(gain = 1) {
  const vz = vzOf(spd), shut = Math.min(.016 + .2 * Math.min(spd, 1.3) ** 1.4, span / vz), f = F(), [vx, vy] = V, still = 1 - Math.min(spd * 3, 1);
  const blue = .45 * Math.min(spd, 1) + .25 * clamp(spd - 1), pan = Math.hypot(panV[0], panV[1]) > 200;
  for (const s of wfield) {
    const hx = vx + s.x / s.z * f, hy = vy + s.y / s.z * f;
    if (hx < -60 || hx > W + 60 || hy < -60 || hy > H + 60) continue;
    const near = 1 - s.z, fade = clamp(near / .12);
    const I = s.m * (.5 + 1.1 * near ** 1.6) * fade * gain * (1 + 1.2 * Math.min(spd, 1.3)) * (1 - .4 * still * (.5 + .5 * Math.sin(vt * .0023 + s.tw)));
    const w = (.45 + 1.25 * s.m) * (.75 + 1.3 * near ** 2) * (1 - .35 * Math.min(spd, 1));
    const r = lerp(s.c[0], .82, blue) * I, g = lerp(s.c[1], .9, blue) * I, b = lerp(s.c[2], 1, blue) * I;
    // the tail is where the star was `shut` seconds ago, further out in depth; while the chart swings, it trails the swing
    if (pan) { cap(hx - panV[0] * .035, hy - panV[1] * .035, hx, hy, w, r, g, b); continue; }
    const z = Math.min(s.z + vz * shut, 1.6);
    cap(vx + s.x / z * f, vy + s.y / z * f, hx, hy, w, r, g, b);
  }
}
// The far sky: stars too distant to move, painted once per size, with a faint band of denser stars and haze like
// the Milky Way. It has margins so the chart can swing without showing an edge, and dims while the near stars stream.
const back = document.createElement('canvas');
const BM: V2 = [.4, .32];
function paintBack() {
  const s = Math.min(dpr, 1.25), BW = W * (1 + 2 * BM[0]), BH = H * (1 + 2 * BM[1]);
  back.width = Math.round(BW * s); back.height = Math.round(BH * s);
  const c = back.getContext('2d')!; c.setTransform(s, 0, 0, s, 0, 0);
  const band = (x: number) => BH * .62 - (x / BW) * BH * .38, n = Math.round(BW * BH / 420);
  for (let i = 0; i < 8; i++) {
    const x = BW * (i + .5) / 8, y = band(x), g = c.createRadialGradient(x, y, 0, x, y, BH * .3);
    g.addColorStop(0, rgb(RAINBOW_RGB[i % RAINBOW_RGB.length], .05)); g.addColorStop(1, rgb(RAINBOW_RGB[i % RAINBOW_RGB.length], 0));
    c.fillStyle = g; c.fillRect(0, 0, BW, BH);
  }
  for (let i = 0; i < n; i++) {
    const x = Math.random() * BW, inBand = Math.random() < .45, y = inBand ? band(x) + (Math.random() + Math.random() + Math.random() - 1.5) * BH * .14 : Math.random() * BH;
    const m = Math.random() ** 3;
    c.fillStyle = rgb(anyOf(STAR_RGB), .16 + .6 * m); c.beginPath(); c.arc(x, y, .35 + .8 * m, 0, TAU); c.fill();
  }
  for (let i = 0; i < 90; i++) {
    const z = 5 + 11 * Math.random() ** 2; c.globalAlpha = .35 + .5 * Math.random();
    c.drawImage(SPRITE, Math.random() * BW - z / 2, Math.random() * BH - z / 2, z, z);
  }
  c.globalAlpha = 1;
}
function radial(c: CanvasRenderingContext2D, x: number, y: number, r: number, stops: [number, string][]) {
  const g = c.createRadialGradient(x, y, 0, x, y, Math.max(1, r));
  for (const [k, col] of stops) g.addColorStop(k, col);
  c.fillStyle = g; c.fillRect(0, 0, W, H);
}
// Her sky on its own canvas: a deep vignette round her star, the far sky and the chart's grid, opened from the click.
// It only changes while it opens, swings, streams or gives the desktop back; otherwise last frame's stays.
let skyKey = '';
function drawSky() {
  const c = sctx, [vx, vy] = V, [cx, cy] = center(), key = `${vx},${vy},${openR},${skyK},${backK},${gridK},${P},${hole}`;
  if (key === skyKey) return;
  skyKey = key;
  c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, sky.width, sky.height);
  if (openR <= 0 || skyK < .002) return;
  c.setTransform(dpr, 0, 0, dpr, 0, 0); c.globalAlpha = skyK;
  radial(c, vx, vy, hd() * 1.3, [[0, '#0a0f2a'], [.5, '#040618'], [1, '#010208']]);
  if (backK > .01) { c.globalAlpha = skyK * backK; c.drawImage(back, vx - cx - BM[0] * W, vy - cy - BM[1] * H, W * (1 + 2 * BM[0]), H * (1 + 2 * BM[1])); c.globalAlpha = skyK; }
  if (gridK > .01) {
    // circles of declination round a pole off screen and hour lines from it, moving with the sky
    const px = vx - .7 * W, py = vy - 1.3 * H, d0 = Math.hypot(vx - px, vy - py), base = Math.atan2(vy - py, vx - px);
    c.strokeStyle = `rgba(143,164,255,${.09 * gridK})`; c.lineWidth = 1;
    for (let k = -5; k <= 5; k++) { c.beginPath(); c.arc(px, py, d0 + k * .16 * W, 0, TAU); c.stroke(); }
    for (let k = -6; k <= 6; k++) {
      const a = base + k * .09, r0 = d0 - .9 * W, r1 = d0 + .9 * W;
      c.beginPath(); c.moveTo(px + Math.cos(a) * r0, py + Math.sin(a) * r0); c.lineTo(px + Math.cos(a) * r1, py + Math.sin(a) * r1); c.stroke();
    }
  }
  c.globalAlpha = 1;
  if (openR < hd() * 2.3) {
    c.globalCompositeOperation = 'destination-in';
    radial(c, P[0], P[1], openR, [[0, '#000'], [Math.max(0, 1 - 140 / openR), '#000'], [1, 'rgba(0,0,0,0)']]);
    c.globalCompositeOperation = 'source-over';
  }
  if (hole > 0) {
    // the finale gives the desktop back: a soft-edged circle growing from her home beside the notch
    const [hx, hy] = home();
    c.globalCompositeOperation = 'destination-out';
    radial(c, hx, hy, hole, [[0, '#000'], [Math.max(0, 1 - 180 / hole), '#000'], [1, 'rgba(0,0,0,0)']]);
    c.globalCompositeOperation = 'source-over';
  }
}
// Light on top of the stars: the glow ahead, her star, the two wrong stars, rings, the flash, the finder.
function drawLights(f: CanvasRenderingContext2D) {
  const [vx, vy] = V, e = el() / STRETCH;
  f.save(); f.globalCompositeOperation = 'lighter';
  if (glowK > .002) radial(f, vx, vy, hd() * .55, [[0, `rgba(200,215,255,${.32 * glowK})`], [.35, `rgba(143,164,255,${.12 * glowK})`], [1, 'rgba(143,164,255,0)']]);
  if (seedK > .01) {
    const s = (20 + 70 * seedK) * (1 + .08 * Math.sin(vt * .006));
    f.globalAlpha = Math.min(1, seedK * 1.6); f.drawImage(SPRITE, vx - s / 2, vy - s / 2, s, s); f.globalAlpha = 1;
  }
  if (candK > .01) {
    const cs = CANDS();
    f.globalAlpha = candK;
    for (let i = 0; i < 2; i++) f.drawImage(SPRITE, vx + cs[i].o[0] - 15, vy + cs[i].o[1] - 15, 30, 30);
    f.globalAlpha = 1;
  }
  for (const r of rings) {
    const k = (vt - r.at) / r.dur;
    if (k < 0 || k > 1) continue;
    const rad = r.max * eOut(k), al = (1 - k) ** 1.6, cg = f.createConicGradient(k * 2, vx, vy);
    RAINBOW.concat(RAINBOW[0]).forEach((col, i) => cg.addColorStop(i / RAINBOW.length, col));
    f.strokeStyle = cg;
    for (const [lw, a] of [[r.w * 2.4, .12], [r.w, .3], [2, .9]]) { f.lineWidth = lw * (1 - .5 * k); f.globalAlpha = a * al; f.beginPath(); f.arc(vx, vy, rad, 0, TAU); f.stroke(); }
    f.globalAlpha = 1;
  }
  if (flashK > .002) radial(f, vx, vy, hd() * 1.5, [[0, `rgba(255,255,255,${flashK})`], [.18, `rgba(235,242,255,${.85 * flashK})`], [.5, `rgba(180,200,255,${.3 * flashK})`], [1, `rgba(143,164,255,${.05 * flashK})`]]);
  f.restore();
  rings = rings.filter(r => vt - r.at < r.dur);
  if (phase === 'intro') drawFinder(f, e);
}
// The finder ring: it closes on each bright star the view swings to, shows where it is, and lets go; on the
// third it turns her colours, names her and closes in as the flight begins.
function drawFinder(f: CanvasRenderingContext2D, e: number) {
  const cs = CANDS();
  for (let i = 0; i < 3; i++) {
    const t0 = HOPS[i][1], t1 = VERDICT[i], lock = i === 2, end = lock ? WARP + 150 : t1 + 220;
    if (e < t0 || e > end) continue;
    const x = V[0] + cs[i].o[0], y = V[1] + cs[i].o[1], k = eOut(seg(e, t0, t0 + CLOSE)), on = lock && e > t1, turn = (e - t0) / 700;
    let r = lerp(70, 22, k), a = k;
    if (!lock && e > t1) { const q = seg(e, t1, end); r += 18 * q; a *= 1 - q; }
    if (on) { r = lerp(22, 12, eOut(seg(e, t1, WARP))); a *= 1 - seg(e, WARP - 100, end); }
    f.save(); f.globalAlpha = a; f.lineWidth = on ? 2 : 1.2;
    if (on) {
      const cg = f.createConicGradient(turn * 4, x, y);
      RAINBOW.concat(RAINBOW[0]).forEach((c, j) => cg.addColorStop(j / RAINBOW.length, c));
      f.strokeStyle = cg;
    } else f.strokeStyle = 'rgba(220,230,255,.85)';
    f.beginPath(); f.arc(x, y, r, 0, TAU); f.stroke();
    for (let q = 0; q < 4; q++) {
      const ang = q * Math.PI / 2 + (on ? turn : 0);
      f.beginPath(); f.moveTo(x + Math.cos(ang) * (r + 5), y + Math.sin(ang) * (r + 5)); f.lineTo(x + Math.cos(ang) * (r + 13), y + Math.sin(ang) * (r + 13)); f.stroke();
    }
    if (!on && k > .9 && e < t1) { f.beginPath(); f.arc(x, y, r + 9, turn * TAU, turn * TAU + 1); f.stroke(); }   // the scan
    f.font = '500 11px ui-monospace, "SF Mono", Menlo, monospace'; f.fillStyle = on ? '#fff' : 'rgba(210,220,255,.8)';
    f.fillText(on ? 'Jarvis' : cs[i].label, x + r + 18, y + r + 14);
    f.restore();
  }
}
// The chart's sounds, each on the frame its picture happens: the swing, the ring closing, a no, the lock.
function chart(at: (k: string, ms: number, fn: () => void) => void) {
  const cs = CANDS();
  at('open', 0, () => sweep(450, 'bandpass', 300, 2200, .05, .3));
  HOPS.forEach(([a, b], i) => {
    const from = i ? cs[i - 1].o : LOOK0();
    at(`swing${i}`, a, () => swipe(b - a, Math.sign(from[0] - cs[i].o[0]) || 1));   // the stars slide against the swing
    at(`close${i}`, b, () => { bell('E6', .22, .25, 0, .2); bell('A6', .24, .25, 0, .2, 0, 45); });
    if (i < 2) at(`no${i}`, VERDICT[i], () => { bell('Cs6', .18, .4, 0, .25); bell('A5', .16, .5, 0, .25, 0, 70); });
  });
  at('lock', VERDICT[2], () => {
    arp(['A5', 'Cs6', 'E6', 'A6'], .38, 60, 1.3, -.25, .25, .3); bell('E7', .2, 1.6, 0, .4, .5, 220);
    rings.push({ at: vt, dur: 700 * STRETCH, max: hd() * .3, w: 5 });
  });
}
// Act one, per frame: the chart swings, the flight speeds up to light speed, the flash, she arrives and celebrates.
function entrance(dt: number) {
  const e = el() / STRETCH, te = e - OFF, b = reduced.matches ? 900 : BORN, dtReal = dt, [cx, cy] = center();
  const at = (k: string, ms: number, fn: () => void) => once(k, ms * STRETCH, fn), T = (k: string, ms: number, fn: () => void) => at(k, ms + OFF, fn);
  openR = hd() * 2.4 * seg(e, 0, 550) ** 1.5;   // space opens from the click
  const L = lookOf(e), pv = V;
  V = [cx - L[0], cy - L[1]];
  panV = dtReal > 0 ? [(V[0] - pv[0]) / dtReal, (V[1] - pv[1]) / dtReal] : [0, 0];
  gridK = seg(e, 150, 450) * (1 - seg(te, 900, 1400));
  candK = seg(e, 150, 450) * (1 - seg(te, 900, 1250));
  seedK = e < VERDICT[2] ? .15 : .6;             // her star looks like any other until the ring locks on it
  spd = 0; flashK = 0; glowK = 0; backK = 1; fovK = 1; span = 1.6;
  chart(at);
  if (!reduced.matches) {
    spd = jumpSpd(te);
    if (te >= 3330) spd = .012 + JTOP * Math.exp(-(te - 3330) / 70);     // drop out: the lines snap back to points
    // The second half the way a hyperspace jump is staged: the view widens as the speed builds, streaks stay short
    // enough that their heads are seen racing, and in the last 0.3 s they stretch to the edges before the flash.
    if (te < 3250) fovK = 1 - .35 * eInOut(seg(te, 1900, 3250));
    span = te < 3100 ? .6 : te < 3250 ? lerp(.6, 1.6, eIn(seg(te, 3100, 3250))) : 1.6;
    seedK = te < 3250 ? lerp(seedK, 1, eIn(seg(te, 900, 3250))) : 0;
    flashK = te < 3250 ? eIn(seg(te, 3130, 3250)) : 1 - eOut(seg(te, 3250, 3750));
    glowK = te < 3250 ? Math.min(spd, 1) ** 2 : 0;
    backK = te < 3250 ? 1 - .7 * Math.min(spd, 1) : seg(te, 3330, 3950);
    flyStep(dt);
    T('bed', 900, bedStart);
    // the bed reaches its brightest only at the flash; it swells with time in flight too, and drops out 0.12 s before the hit
    if (te < 3190) { bedSet(dtReal, spd * 1.2 / JTOP, lerp(.5, 1.2, seg(te, 900, 3190) ** 1.3)); pulse(dtReal, Math.min(spd, 1.25)); }
    T('swell', 2650, () => swell(540, .22));
    T('suck', 3190, () => bedCut(.03));
    T('hit', 3250, () => { boom(1); chord(HIT, .85, 3.2); arp(SHIMMER, .3, 70, 1.4, -.5, .5, .45, 120); });
    T('drop', 3330, () => sweep(520, 'bandpass', 7000, 350, .2, .3));
  }
  // her: she wakes, celebrates with a full turn, a hop and sparks while the greeting climbs, then faces you
  T('born', b, () => {
    born = true; ball.mode = 'center'; snapBall(); ball.R.value = R0() * (reduced.matches ? .6 : R_START); face = '00'; seedK = 0;
    core.effect('ripple', vt);
    rings.push({ at: vt, dur: 1100 * STRETCH, max: hd() * 1.25, w: 14 }, { at: vt + 120 * STRETCH, dur: 1500 * STRETCH, max: hd() * 1.7, w: 7 });
  });
  T('wake', b + 650, () => { face = '13'; bell('B5', .6, .6, 0, .25); bell('Fs6', .65, .6, 0, .25, 0, 45); });
  T('happy', b + 900, () => { face = '02'; moment('10', 1500 * STRETCH); core.effect('spin', vt); core.effect('burst', vt); arp(HELLO, .75, 100, 1.2, -.5, .5, .2); swipe(550, 1); });
  letters.forEach((l, i) => T(`w${i}`, b + 1550 + i * 110, () => { l.start = vt; l.settle = vt + 300; (word.children[i] as HTMLElement).classList.add('on'); }));
  T('tag', b + 2450, () => intro.classList.add('tag-in'));
  T('go', b + 2700, toHello);
  haloK = born ? eOut(seg(te, b + 100, b + 900)) : 0;
}

// ---------- the word ----------
const WORD = 'Jarvis', GLYPH = 'AJRVSXZ#%&*+';
let letters: { ch: string; start: number; settle: number; done: boolean; scr: number }[] = [];
function resetWord() {
  word.innerHTML = [...WORD].map(() => '<span></span>').join('');
  letters = [...WORD].map(ch => ({ ch, start: -1, settle: 0, done: false, scr: 0 }));
  intro.className = ''; intro.hidden = false; $('#tag').textContent = tx().tag;
}
function wordAllIn() {
  [...word.children].forEach((s, i) => { s.textContent = WORD[i]; s.className = 'on in'; });
  letters.forEach(l => { l.done = true; l.start = 0; });
}
function letterStep() {
  letters.forEach((l, i) => {
    if (l.start < 0 || l.done) return;
    const s = word.children[i] as HTMLElement;
    if (vt >= l.settle) { l.done = true; s.textContent = l.ch; s.classList.add('in'); bell(TICKS[i], .42, .35, (i - 2.5) / 2.5 * .6, .3, .2); }
    else if (vt - l.scr > 50) { l.scr = vt; s.textContent = GLYPH[Math.floor(Math.random() * GLYPH.length)]; }
  });
}
function layoutIntro() { const [, cy] = center(); intro.style.top = `${cy + R0() + 46}px`; }

// ---------- setup pages ----------
let stepN = 0, line = '', lineAt = 0, shown = -1;
const CPS = () => S.lang === 'zh' ? 34 : 22;
const speaking = () => phase === 'setup' && vt - lineAt < line.length * CPS();
function setLine(text: string) { line = text; lineAt = vt; shown = -1; }
function drawLine() {
  const say = pin.querySelector<HTMLElement>('.say');
  if (!say) return;
  const n = Math.min(line.length, Math.floor((vt - lineAt) / CPS()));
  if (n === shown) return;
  shown = n; say.setAttribute('aria-label', line);
  say.innerHTML = `${esc(line.slice(0, n))}<span class="rest">${esc(line.slice(n))}</span>`;
}
const I = {
  check: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7"/></svg>',
  x: '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
  star: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 0C6.4 3.6 8.4 5.6 12 6 8.4 6.4 6.4 8.4 6 12 5.6 8.4 3.6 6.4 0 6 3.6 5.6 5.6 3.6 6 0Z" fill="currentColor"/></svg>',
};
const svg16 = (p: string) => `<svg viewBox="0 0 16 16" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
const PICON: Record<Perm, string> = {
  mic: svg16('<rect x="5.5" y="1.8" width="5" height="8.4" rx="2.5"/><path d="M3.2 8a4.8 4.8 0 0 0 9.6 0M8 12.8v1.6"/>'),
  screen: svg16('<rect x="1.5" y="2.5" width="13" height="9" rx="1.5"/><path d="M5.5 14h5M8 11.5V14"/><circle cx="8" cy="7" r="1.6" fill="currentColor" stroke="none"/>'),
  auto: svg16('<rect x="1.5" y="2.5" width="13" height="11" rx="2"/><path d="M4.5 6.5l2 1.8-2 1.7M8.5 10.5h3"/>'),
  notify: svg16('<path d="M4 11V7a4 4 0 0 1 8 0v4l1.2 1.5H2.8L4 11zM6.5 14a1.5 1.5 0 0 0 3 0"/>'),
};
const logo = (k: Conn) => k === 'notion' ? `<img src="${notionLogo}" alt="">`
  : k === 'ms' ? '<svg viewBox="0 0 22 22" width="18" height="18" aria-hidden="true"><rect width="10" height="10" fill="#f25022"/><rect x="12" width="10" height="10" fill="#7fba00"/><rect y="12" width="10" height="10" fill="#00a4ef"/><rect x="12" y="12" width="10" height="10" fill="#ffb900"/></svg>'
  : '<svg viewBox="0 0 16 16" width="19" height="19" fill="none" stroke="#1b2140" stroke-width="1.3" aria-hidden="true"><circle cx="8" cy="8" r="6.3"/><path d="M1.7 8h12.6M8 1.7c2 2 2 10.6 0 12.6M8 1.7c-2 2-2 10.6 0 12.6"/></svg>';
const link = (page: string, text: string) => `<a href="#" data-act="link" data-v="${page}">${text}</a>`;
const FACE: Record<number, ExprId> = { 1: '02', 2: '02', 3: '02', 4: '35b', 5: '02', 6: '02', 7: '02', 8: '02' };
const PERMS: Perm[] = ['mic', 'screen', 'auto', 'notify'];
const CONNS: Conn[] = ['notion', 'ms', 'web'];
const ROWS = ['connect', 'chat', 'deep', 'live'];
const sayOf = (n: number) => fmt((tx() as Record<string, unknown>)[`say${n}`] as string);
const canNext = (n: number) => n !== 4 || S.keyState === 'ok';
// A failed check in words: the reason code picks the sentence; anything else shows the daemon's own detail.
function whyOf(c: Check | undefined, p: string) {
  const t = tx(), w = (t.why4 as Record<string, string>)[c?.reason ?? ''];
  return w ? fmt(w, { p, row: t.rows4[ROWS.indexOf(c!.id)] ?? c!.id }) : fmt(t.failed, { d: c?.detail || c?.reason || '' });
}

function live(n: number) {
  const t = tx();
  if (n === 2) {
    const chip = (v: string, sub: string, label = v) => `<button type="button" class="chip" data-act="asst" data-v="${v}" aria-pressed="${S.asstPick === v}"><b>${label}</b><small>${sub}</small></button>`;
    return `<div class="chips">${chip('Jarvis', t.rec)}${chip('Nova', t.nova)}${chip('custom', '…', t.own)}</div>`
      + (S.asstPick === 'custom' ? `<div class="fld"><input id="f-asst" class="inp center" placeholder="${t.ph2}" value="${S.asst === 'Jarvis' || S.asst === 'Nova' ? '' : esc(S.asst)}" maxlength="16" autocomplete="off" spellcheck="false"></div>` : '');
  }
  if (n === 3) {
    const opt = (v: string, b: string, s: string) => `<button type="button" class="opt" data-act="lang" data-v="${v}" aria-pressed="${S.langPick === v}"><span><b>${b}</b><br><span>${s}</span></span><i></i></button>`;
    return `<div class="opts">${opt('system', t.sys, fmt(t.sysV, { l: t.names[S.sysLang] }))}${opt('zh', '简体中文', t.zhV)}${opt('en', 'English', t.enV)}</div>`;
  }
  if (n === 4) {
    if (S.keyState === 'idle') return '';
    const ST = ['wait', 'spin', 'ok', 'bad', 'wait'];
    const checks = `<div class="checks">${t.rows4.map((label, i) => { const s = S.keyRows[i]; return `<div class="ck${s === 4 ? ' off' : ''}"><span class="st ${ST[s]}">${s === 2 ? I.check : s === 3 ? I.x : ''}</span>${label}</div>`; }).join('')}</div>`;
    // a key that passed may still miss deep tasks or live voice: her first line shows, and the reason under it
    const reply = S.keyState === 'ok' ? `<div class="reply"><small><span>${t.first}</span><span>${fmt(t.secs, { s: (S.keyMs / 1000).toFixed(2) })}</span></small><p>“${esc(S.firstLine)}”</p></div>`
      + (S.keyWhy ? `<p class="note">${esc(S.keyWhy)}</p>` : '')
      : S.keyState === 'bad' ? `<div class="reply bad"><p>${esc(S.keyWhy)}</p></div>` : '';
    return checks + reply;
  }
  if (n === 5) {
    const locked = S.mmState !== 'ok';
    const state = S.mmState === 'testing' ? `<span class="st spin" style="display:inline-grid;vertical-align:-2px"></span> ${t.testing}…`
      : S.mmState === 'ok' ? `<span style="color:var(--ok)">${t.ok5}</span>` : S.mmState === 'bad' ? `<span style="color:var(--bad)">${esc(S.mmWhy)}</span>` : t.lock5;
    return `<p class="note">${state}</p><div class="voices${locked ? ' locked' : ''}">${(S.voices.length ? S.voices : Array.from({ length: 5 }, () => ({ id: '', label: '—', note: '' }))).map((v, i) =>
      `<button type="button" class="voice${S.playing === i ? ' playing' : ''}" data-act="voice" data-i="${i}" aria-pressed="${S.voice === i}"><b>${esc(v.label)}</b><span>${esc(v.note)}</span><span class="bars"><i></i><i></i><i></i><i></i></span></button>`).join('')}</div>`
      + `<p class="cap">${S.playing >= 0 ? `“${esc(fmt(t.hear))}”` : ''}</p>`;
  }
  if (n === 6) {
    return `<div class="rows">${PERMS.map(k => {
      const [name, why] = t.perms[k], s = S.perms[k];
      const btn = s === 'ok' || s === 'relaunch' || s === 'asked' ? `<span class="pill done">${I.check}${s === 'ok' ? t.allowed : s === 'relaunch' ? t.relaunch : t.asked}</span>`
        : `<button type="button" class="pill${s === 'later' ? ' later' : ''}" data-act="perm" data-k="${k}">${t.allow}</button>`;
      return `<div class="prow"><span class="pi">${PICON[k]}</span><div><b>${name}</b><span>${why}</span></div>${btn}</div>`;
    }).join('')}</div>`;
  }
  if (n === 7) {
    return `<div class="grid">${CONNS.map(k => {
      const [name, what] = t.conns[k], s = S.conns[k];
      const em = s === 'busy' ? `<span class="st spin"></span>${k === 'web' ? t.testing : t.busy}…` : s === 'ok' ? `${I.check}${t.connected}` : s === 'na' ? t.na : `${t.connect} →`;
      return `<button type="button" class="tile ${s}" data-act="conn" data-k="${k}"${s ? ' aria-disabled="true"' : ''}><span class="lg">${logo(k)}</span><b>${name}</b><span>${what}</span><em>${em}</em></button>`;
    }).join('')}</div>` + (S.connWhy ? `<p class="note" style="color:var(--bad)">${esc(S.connWhy)}</p>` : '');
  }
  if (n === 8) {
    // what she now knows, skipped items in grey
    const permsN = Object.values(S.perms).filter(v => v === 'ok' || v === 'relaunch' || v === 'asked').length;
    const conns = CONNS.filter(k => S.conns[k] === 'ok').map(k => t.conns[k][0]);
    const voiceOk = S.mmState === 'ok' && S.voice >= 0;
    const rows: [string, string, string][] = [
      [t.recap[0], esc(S.user) || '—', S.user ? '' : 'skip'], [t.recap[1], esc(S.asst), ''],
      [t.recap[2], S.langPick === 'system' ? `${t.sys} · ${t.names[S.lang]}` : t.names[S.lang], ''],
      [t.recap[3], S.keyState === 'ok' ? t.works : '—', S.keyState === 'ok' ? 'ok' : 'skip'],
      [t.recap[4], voiceOk ? esc(S.voices[S.voice].label) : t.textOnly, voiceOk ? '' : 'skip'],
      [t.recap[5], `${permsN} / 4`, permsN ? '' : 'skip'],
      [t.recap[6], conns.length ? conns.join(t.and) : t.none, conns.length ? '' : 'skip'],
    ];
    return `<ul class="recap">${rows.map(([k, v, c]) => `<li><span>${k}</span><b class="${c}">${v}</b></li>`).join('')}</ul><p class="note">${t.tip8}</p>`
      + (S.saveWhy ? `<p class="note" style="color:var(--bad)">${esc(S.saveWhy)}</p>` : '')
      + `<button type="button" class="cta" data-act="enter"${S.saving ? ' disabled' : ''}>${S.saving ? `${t.saving}…` : esc(fmt(t.enter))}</button>`;
  }
  return '';
}
function body(n: number) {
  const t = tx();
  if (n === 1) return `<div class="fld"><input id="f-user" class="inp center" value="${esc(S.user)}" placeholder="${t.ph1}" maxlength="24" autocomplete="off" spellcheck="false"><span class="src"${S.account && S.user === S.account ? '' : ' hidden'}>${t.src1}</span></div><p class="note">${t.note1}</p>`;
  if (n === 2) return `<div id="live">${live(2)}</div><p class="note">${t.note2}</p>`;
  if (n === 3) return `<div id="live">${live(3)}</div><p class="note">${t.note3}</p>`;
  if (n === 4) return `<div class="fld"><input id="f-key" class="inp mono" type="password" placeholder="sk-…" autocomplete="off" spellcheck="false"><button type="button" class="btn-sm" data-act="test">${t.test}</button></div>`
    + `<div id="live" class="body">${live(4)}</div><p class="note">${fmt(t.note4, { link: link('openai', 'platform.openai.com/api-keys') })}</p>`;
  if (n === 5) return `<div class="fld"><input id="f-mm" class="inp mono" type="password" placeholder="${t.ph5}" autocomplete="off" spellcheck="false"><button type="button" class="btn-sm" data-act="mmtest">${t.test}</button></div>`
    + `<div id="live" class="body">${live(5)}</div><p class="note">${fmt(t.note5, { link: link('minimax', 'platform.minimax.io') })}</p>`;
  // the web search key sits outside #live so a refresh never clears what is typed
  if (n === 7) return `<div id="live" class="body">${live(7)}</div><div class="fld" id="web-fld"${S.webOpen ? '' : ' hidden'}><input id="f-web" class="inp mono" type="password" placeholder="${t.ph7}" autocomplete="off" spellcheck="false"><button type="button" class="btn-sm" data-act="webtest">${t.test}</button></div>`
    + `<p class="note" id="web-note"${S.webOpen ? '' : ' hidden'}>${fmt(t.note7, { link: link('tavily', 'tavily.com') })}</p>`;
  return `<div id="live" class="body">${live(n)}</div>`;
}
function foot(n: number) {
  const t = tx(), past = (k: number) => S.done.has(k) || S.skipped.has(k);
  const stars = [1, 2, 3, 4, 5, 6, 7].map(k => {
    const cls = k === n ? 'cur' : S.done.has(k) ? 'done' : S.skipped.has(k) ? 'skip' : '';
    return `${k > 1 ? `<span class="ln${past(k - 1) && (past(k) || k === n) ? ' lit' : ''}"></span>` : ''}<button type="button" class="star ${cls}" data-act="star" data-n="${k}"${past(k) && k !== n ? '' : ' disabled'} aria-label="${k}">${I.star}</button>`;
  }).join('');
  if (n === 8) return `<div class="foot" style="--i:3;justify-content:center"><div class="stars">${stars}</div></div>`;
  const left = n === 4 ? `<span class="must">${t.must}</span>` : `<button type="button" class="btn ghost" data-act="skip">${n === 5 ? t.textOnly : t.skip}</button>`;
  return `<div class="foot" style="--i:2"><div class="stars">${stars}</div><div class="acts">${left}<button type="button" class="btn pri" data-act="next"${canNext(n) ? '' : ' disabled'}>${t.next}</button></div></div>`;
}
function refresh() { const l = pin.querySelector<HTMLElement>('#live'); if (l) l.innerHTML = live(stepN); refreshFoot(); }
function refreshFoot() { const b = pin.querySelector<HTMLButtonElement>('[data-act="next"]'); if (b) b.disabled = !canNext(stepN); }
function render(n: number) {
  stepN = n; pin.classList.remove('leave');
  pin.innerHTML = `<p class="say" style="--i:0"></p><div class="body" style="--i:1">${body(n)}</div>${foot(n)}`;
  setLine(sayOf(n)); drawLine(); face = FACE[n]; measure(); quitBtn.textContent = tx().quit;
  const f = pin.querySelector<HTMLInputElement>(n === 1 ? '#f-user' : n === 4 ? '#f-key' : '#none');
  if (f) { f.focus({ preventScroll: true }); if (n === 1) f.select(); }
  if (n === 6) for (const k of PERMS) void bridge.permission(k, false).then(s => { if (s && !S.perms[k]) { S.perms[k] = s; refresh(); } });
  if (n === 7) void connStatus();
  if (n === 8) { moment('fin', 1700); cue('done', .9, true); }
}
function goStep(n: number) {
  stopVoice();
  if (!pin.innerHTML) { render(n); return; }
  pin.classList.add('leave'); later(150, () => render(n));
}
function measure() { panelH = Math.min(pin.offsetHeight, H - 12); }
new ResizeObserver(measure).observe(pin);

// Leaving the language page tells the daemon at once, so the key test's first line and the voices come in it.
const sendLang = () => api('/inherent/language', { language: S.lang }).catch(() => undefined);
function next() {
  const n = stepN, t = tx();
  if (!canNext(n) || pin.classList.contains('leave')) return;
  if (n === 1) { S.user = pin.querySelector<HTMLInputElement>('#f-user')!.value.trim(); if (!S.user) { skip(); return; } }
  if (n === 2 && S.asstPick === 'custom') S.asst = pin.querySelector<HTMLInputElement>('#f-asst')?.value.trim() || 'Jarvis';
  if (n === 3) void sendLang();
  S.done.add(n); S.skipped.delete(n);
  const reply = n === 1 ? t.hi1 : n === 2 ? t.hi2 : n === 4 ? t.hi4 : '';
  if (reply) {
    setLine(fmt(reply)); moment(n === 2 ? '14' : '10', 1400); cue('happy', .9, true);
    pin.querySelectorAll<HTMLButtonElement>('.acts button').forEach(b => { b.disabled = true; });
    later(n === 2 ? 1300 : 1000, () => goStep(n + 1));
  } else { cue('module', .9); goStep(n + 1); }
}
function skip() {
  const n = stepN;
  if (n === 4 || pin.classList.contains('leave')) return;
  if (n === 3) void sendLang();
  if (n === 5) S.voice = -1;
  S.skipped.add(n); S.done.delete(n); cue('back', .8); goStep(n + 1);
}

// ---------- the key tests: one call to the daemon checks the key the way she will use it, and keeps it on a pass ----------
type KeyAnswer = { ok: boolean; checks: Check[]; first_line?: string; seconds?: number; voices?: Voice[] };
async function testKey() {
  const input = pin.querySelector<HTMLInputElement>('#f-key'), v = input?.value.trim() ?? '';
  if (!v || S.keyState === 'testing') { input?.focus(); return; }
  S.keyState = 'testing'; S.keyRows = [1, 0, 0, 0]; S.keyWhy = ''; refresh(); cue('think', .8);
  let r: KeyAnswer;
  try { r = await api<KeyAnswer>('/inherent/setup/key', { provider: 'openai', key: v }, 90000); }
  catch (e) { S.keyState = 'bad'; S.keyRows = [3, 4, 4, 4]; S.keyWhy = (e as Error).message; refresh(); moment('34', 800); cue('error', .8); return; }
  // the answer comes at once; the rows still light up one after another
  const rows = ROWS.map(id => r.checks.find(c => c.id === id));
  rows.forEach((c, i) => later(i * 160, () => {
    S.keyRows[i] = !c ? 4 : c.ok ? 2 : 3;
    if (i < 3 && rows[i + 1]) S.keyRows[i + 1] = 1;
    refresh(); if (c?.ok) cue(tick(1 + i * 2), .8, true);
  }));
  later(4 * 160 + 120, () => {
    S.keyState = r.ok ? 'ok' : 'bad'; S.firstLine = r.first_line ?? ''; S.keyMs = (r.seconds ?? 0) * 1000;
    S.keyWhy = rows.some(c => c && !c.ok) ? whyOf(rows.find(c => c && !c.ok), 'OpenAI') : '';
    refresh();
    if (r.ok) { moment('33', 1500); cue('done', 1, true); } else { moment('34', 800); later(800, () => moment('38', 1600)); cue('error', .8); }
  });
}
async function testMM() {
  const v = pin.querySelector<HTMLInputElement>('#f-mm')?.value.trim();
  if (!v || S.mmState === 'testing') return;
  S.mmState = 'testing'; S.mmWhy = ''; refresh();
  try {
    const r = await api<KeyAnswer>('/inherent/setup/key', { provider: 'minimax', key: v });
    if (r.ok) { S.mmState = 'ok'; S.voices = r.voices ?? []; S.voice = S.voices.length ? 0 : -1; cue('on', .9); moment('10', 1100); }
    else { S.mmState = 'bad'; S.mmWhy = whyOf(r.checks.find(c => !c.ok), 'MiniMax'); cue('error', .8); moment('34', 800); }
  } catch (e) { S.mmState = 'bad'; S.mmWhy = (e as Error).message; cue('error', .8); }
  refresh();
}
// A voice speaks one line through the daemon; she moves her mouth while it plays.
let clip: HTMLAudioElement | null = null;
function stopVoice() { clip?.pause(); clip = null; if (S.playing >= 0) { S.playing = -1; refresh(); } }
async function preview(i: number) {
  stopVoice(); S.voice = i; S.playing = i; refresh();
  try {
    const blob = await api<Blob>('/inherent/setup/voice-preview', { voice_id: S.voices[i].id, text: fmt(tx().hear) }, 30000);
    if (S.playing !== i) return;
    const a = clip = new Audio(URL.createObjectURL(blob));
    a.onended = a.onerror = () => { URL.revokeObjectURL(a.src); if (clip === a) { clip = null; S.playing = -1; refresh(); } };
    await a.play();
  } catch (e) { if (S.playing === i) { S.playing = -1; S.mmWhy = (e as Error).message; S.mmState = 'bad'; refresh(); } }
}

// ---------- permissions: macOS asks; the main process reports what came back ----------
async function ask(k: Perm) {
  cue('open', .7); lookForce = [0, .95];
  const s = await bridge.permission(k, true, [S.asst, tx().ping]).catch(() => 'later' as PermState);
  lookForce = null; S.perms[k] = s || 'later'; refresh();
  if (S.perms[k] === 'later') cue('off', .7); else { moment(TURN_FACE.receive, 900); cue('on', .9); }
}

// ---------- connections: Notion signs in through the browser like the plugin panel; web search takes a Tavily key ----------
type Snap = { plugins: { id: string; status: string; supported: boolean }[]; request: { id: string; plugin_id: string; state: string; error?: string } | null };
const PLUGIN: Record<'notion' | 'ms', string> = { notion: 'notion', ms: 'mcp-microsoft' };
async function connStatus() {
  try {
    const snap = await api<Snap>('/inherent/plugins');
    for (const k of ['notion', 'ms'] as const) {
      const p = snap.plugins.find(x => x.id === PLUGIN[k]);
      if (!p || !p.supported) S.conns[k] = 'na'; else if (p.status === 'ready') S.conns[k] = 'ok';
    }
  } catch (e) { S.connWhy = (e as Error).message; }
  refresh();
}
async function connect(k: 'notion' | 'ms') {
  S.conns[k] = 'busy'; S.connWhy = ''; refresh(); cue('think', .7);
  try {
    const id = (await api<Snap>('/inherent/plugins/action', { operation: 'open', data: { plugin_id: PLUGIN[k] } })).request?.id;
    await api('/inherent/plugins/action', { operation: 'connect', data: { request_id: id } });
    // the daemon opens the browser; poll the same snapshot the plugin panel reads until the sign-in ends
    while (phase === 'setup') {
      await new Promise(r => setTimeout(r, 1500));
      const r = (await api<Snap>('/inherent/plugins')).request;
      if (!r || r.id !== id) { S.conns[k] = ''; break; }
      if (r.state === 'ready') { S.conns[k] = 'ok'; moment('31c', 900); cue('on', .9); break; }
      if (r.state === 'error' || r.state === 'cancelled') { S.conns[k] = ''; S.connWhy = r.error ?? ''; cue('off', .7); break; }
    }
  } catch (e) { S.conns[k] = ''; S.connWhy = (e as Error).message; }
  refresh();
}
async function testWeb() {
  const v = pin.querySelector<HTMLInputElement>('#f-web')?.value.trim();
  if (!v || S.conns.web === 'busy') return;
  S.conns.web = 'busy'; S.connWhy = ''; refresh(); cue('think', .7);
  try {
    const r = await api<KeyAnswer>('/inherent/setup/key', { provider: 'tavily', key: v });
    if (r.ok) { S.conns.web = 'ok'; webField(false); moment('31c', 900); cue('on', .9); }
    else { S.conns.web = ''; S.connWhy = whyOf(r.checks.find(c => !c.ok), 'Tavily'); cue('error', .8); }
  } catch (e) { S.conns.web = ''; S.connWhy = (e as Error).message; }
  refresh();
}
function webField(open: boolean) {
  S.webOpen = open;
  for (const id of ['#web-fld', '#web-note']) { const e = pin.querySelector<HTMLElement>(id); if (e) e.hidden = !open; }
  if (open) pin.querySelector<HTMLInputElement>('#f-web')?.focus({ preventScroll: true });
}

// ---------- 进入: what was picked goes to the daemon, which marks setup done and restarts ----------
async function enterApp() {
  if (S.saving || phase !== 'setup') return;
  S.saving = true; S.saveWhy = ''; refresh();
  try {
    await api('/inherent/language', { language: S.lang });
    const who: Record<string, string> = {};
    if (S.done.has(1) && S.user) who.name = S.user;
    if (S.done.has(2)) who.assistant_name = S.asst;
    if (Object.keys(who).length) await api('/inherent/setup/name', who);
    if (S.mmState === 'ok' && S.voice >= 0) await api('/inherent/settings', { changes: { tts_voice: S.voices[S.voice].id } });
    await api('/inherent/setup/done', {});
  } catch (e) {
    S.saving = false; S.saveWhy = fmt(tx().unsaved, { e: (e as Error).message }); refresh(); moment('34', 800); cue('error', .8);
    return;
  }
  finale();
}

// ---------- acts ----------
// Space opens where 打开 was clicked: the one place the app knows at launch without asking for anything.
function start(click: V2) {
  audio();
  const [cx, cy] = center(), l0 = LOOK0();
  P = click; V = [cx - l0[0], cy - l0[1]]; panV = [0, 0];
  makeWField(); layoutIntro(); enter('intro'); skipBtn.classList.add('on');
}
function toHello() {
  enter('hello'); skipBtn.classList.remove('on');
  intro.classList.add('tag-in', 'go-in');
  $<HTMLButtonElement>('#go').focus({ preventScroll: true });
}
// Her sky at rest, from the end of act one to the finale: the far sky and slow stars round the middle.
function skyState() {
  skyK = 1; openR = hd() * 2.4; backK = 1; gridK = 0; candK = 0; seedK = 0; flashK = 0; glowK = 0; fovK = 1; span = 1.6; spd = .012; V = center(); P = center();
  born = true; if (!wfield.length) makeWField();
}
function skipIntro() {
  if (phase !== 'intro') return;
  bedCut(.05); skyState(); haloK = 1;
  face = '02'; over = null; lookForce = null; trail = []; rings = [];
  ball.mode = 'center'; snapBall(); wordAllIn(); toHello();
}
function moveIn() {
  if (phase !== 'hello') return;
  enter('movein'); intro.classList.add('out'); cue('send', 1, true); face = '02'; lookForce = [0, -.9];
}
function finale() {
  if (phase !== 'setup') return;
  enter('finale'); pin.classList.add('leave'); cue('send', 1, true);
}

// ---------- per-frame scene logic ----------
function update(dt: number) {
  const e = el();
  if (phase === 'intro') entrance(dt);
  else if (phase === 'movein') {
    const [cx, cy] = center(), [hx, hy] = home();
    once('crouch', B.crouch, () => { core.s.stretch.velocity -= 2.4; });
    once('fly', B.fly, () => { ball.mode = 'drive'; air(.7, 320, 1700, .16, 'bandpass', [panX(cx), panX(hx)]); });
    once('lobe', B.lobe, () => { islMode = 'lobe'; });
    haloK = 1 - seg(e, B.fly, B.fly + 450);
    if (ball.mode === 'drive') {
      const q = reduced.matches ? (e >= B.land ? 1 : 0) : eInOut(seg(e, B.fly, B.land)), p1: V2 = [cx - 14, TOP + 80];
      drive((1 - q) ** 2 * cx + 2 * (1 - q) * q * p1[0] + q * q * hx, (1 - q) ** 2 * cy + 2 * (1 - q) * q * p1[1] + q * q * hy, lerp(R0(), R_HOME, q ** .7), dt);
    }
    once('land', B.land, () => {
      ball.mode = 'home'; ball.v = [0, 0]; snapBall(); face = 'rest'; lookForce = null;
      core.s.stretch.velocity -= 2.6; core.effect('ripple', vt); cue('heard', 1, true, panX(hx)); thump(90, 50, .25, .3, panX(hx));
    });
    once('edge', B.edge, () => {
      edgeAt = vt;
      air(1.25, 300, 900, .05, 'bandpass', [-.15, -.85]); air(1.25, 300, 900, .05, 'bandpass', [.15, .85]);
    });
    once('edgeDone', B.edgeEnd - 250, () => { cue('done', .9, true); });
    once('open', B.open, () => {
      core.hop(vt, .22); ball.mode = 'panel'; islMode = 'panel'; enter('setup'); pin.innerHTML = ''; render(1); cue('open', 1);
    });
  } else if (phase === 'finale') {
    once('retract', D.retract, () => { panel.classList.remove('show'); panelShown = false; pin.innerHTML = ''; islMode = 'lobe'; ball.mode = 'home'; face = 'rest'; });
    // from here the desktop is yours again: clicks pass through while she says hello, then the companion takes over
    once('reveal', D.reveal, () => { bridge.passthrough(true); air(1.3, 900, 180, .07, 'lowpass', panX(home()[0])); });
    hole = Math.hypot(W, H) * 1.15 * eInOut(seg(e, D.reveal, D.revealEnd));
    if (e >= D.revealEnd) { skyK = 0; hole = 0; }
    once('hi', D.hi, () => { moment('10', 1400); showBubble(fmt(tx().bubble), D.bye - D.hi); cue('done', .9, true, panX(home()[0])); });
    once('bye', D.bye, () => bridge.done());
  }
  if (phase === 'setup') {
    drawLine();
    if (!panelShown && isl.h.value > panelH * .8) { panelShown = true; panel.classList.add('show'); }
  }
  // springs
  const [a, b, h] = islTarget();
  step(isl.x0, a, 3, .74, dt); step(isl.x1, b, 3, .74, dt); step(isl.h, h, islMode === 'panel' ? 2.7 : 3.4, .76, dt);
  panel.style.height = `${Math.max(0, isl.h.value)}px`;
  if (ball.mode === 'center' || ball.mode === 'home' || ball.mode === 'panel') {
    const [x, y, r] = ballTarget(), soft = ball.mode === 'center';
    step(ball.x, x, 3, .8, dt); step(ball.y, y, 3, .8, dt); step(ball.R, r, soft ? 2.4 : 3.2, soft ? .42 : .72, dt);
    ball.v = [ball.x.velocity, ball.y.velocity];
  }
  trail = trail.filter(p => vt - p.t < 260);
  // her sky keeps drifting after the entrance until it hands over
  if (phase !== 'intro' && skyK > .002 && wfield.length) { spd = .012; flyStep(dt); }
  // space
  letterStep();
  if (ball.mode !== 'off') {
    core.update(vt, dt, { expr: faceNow(), look: lookAt(ball.x.value, ball.y.value), still: false, pressed: false });
    const g = core.light.glow.map(v => Math.round(v * 255)).join(' ');
    if (g !== glowNow) { glowNow = g; document.documentElement.style.setProperty('--glow', g); }
  }
}
// Moves her by hand this frame, keeping her speed for the stretch and the trail.
function drive(x: number, y: number, R: number, dt: number) {
  if (dt > 0) ball.v = [(x - ball.x.value) / dt, (y - ball.y.value) / dt];
  ball.x.value = x; ball.y.value = y; ball.R.value = R;
  if (Math.hypot(ball.v[0], ball.v[1]) > 250) trail.push({ x, y, R, t: vt });
}
let glowNow = '';
function faceNow(): ExprId {
  if (over && vt < over.until) return over.f;
  if (phase === 'setup') {
    if (speaking() || (stepN === 5 && clip && !clip.paused)) return TURN_FACE.reply;
    if (stepN === 4 && S.keyState === 'testing') return '36';
    if (stepN === 5 && S.mmState === 'testing') return '36';
    if (stepN === 7 && Object.values(S.conns).includes('busy')) return '40';
    if (stepN === 1 && vt - typedAt < 1400) return '35';
  }
  return face;
}
function lookFrom(c: V2, reach = 1e9): V2 | null {
  if (!pointer) return null;
  const dx = pointer[0] - c[0], dy = pointer[1] - c[1], dist = Math.hypot(dx, dy) || 1, k = dist / (dist + 120);
  return dist > reach ? null : [dx / dist * k, dy / dist * k];
}
const lookAt = (x: number, y: number): V2 | null => lookForce ?? (pointerLive ? lookFrom([x, y], phase === 'finale' ? 320 : 1e9) : [0, 0]);
function showBubble(text: string, ms: number) {
  const [hx] = home();
  bubble.textContent = text; bubble.style.left = `${Math.max(16, hx - 22)}px`; bubble.style.top = `${TOP + 8}px`; bubble.classList.add('on');
  later(ms, () => bubble.classList.remove('on'));
}

// ---------- painting ----------
function drawBall(c: CanvasRenderingContext2D, k: Core, x: number, y: number, R: number, d: number, vel: V2 = [0, 0]) {
  const Rq = Math.ceil(R / 6) * 6, S2 = Math.round(2 * 1.3 * Rq * d), [jx, jy, bx, by] = k.pose(1);
  const sp = Math.hypot(vel[0], vel[1]), st = reduced.matches ? 0 : Math.min(.26, sp / 4200), ang = Math.atan2(vel[1], vel[0]);
  const pose = (cc: CanvasRenderingContext2D, cx: number, cy: number) => {
    cc.translate(cx, cy);
    if (st > .004) { cc.rotate(ang); cc.scale(1 + st, 1 - st * .6); cc.rotate(-ang); }
    cc.translate(jx * R, jy * R); cc.scale(bx, by);
  };
  if (k.render(S2, 3)) {
    c.save(); c.translate(x, y); k.orbit(c, R, -1); c.restore();
    c.save(); pose(c, x, y); k.inside(c, R, S2); k.glass(c, R, S2); c.restore();
  }
  const E = Math.ceil(3.4 * Rq * d);
  if (eyeCv.width !== E) eyeCv.width = eyeCv.height = E;
  ectx.setTransform(1, 0, 0, 1, 0, 0); ectx.clearRect(0, 0, E, E);
  ectx.setTransform(d, 0, 0, d, 0, 0); pose(ectx, E / 2 / d, E / 2 / d); k.eyes(ectx, R);
  const [glow, blur] = k.glow();
  c.save(); c.setTransform(1, 0, 0, 1, 0, 0); c.shadowColor = glow; c.shadowBlur = blur * R * d;
  c.drawImage(eyeCv, x * d - E / 2, y * d - E / 2); c.restore();
  c.save(); c.translate(x, y); k.orbit(c, R, 1); k.particles(c, R, d); c.restore();
}
function islandPath(c: CanvasRenderingContext2D, x0: number, x1: number, h: number) {
  const r = Math.min(lerp(10, 26, clamp((h - TOP) / 60)), h / 2, (x1 - x0) / 2), s = 8;
  c.beginPath(); c.moveTo(x0 - s, 0); c.quadraticCurveTo(x0, 0, x0, s);
  c.lineTo(x0, h - r); c.quadraticCurveTo(x0, h, x0 + r, h); c.lineTo(x1 - r, h); c.quadraticCurveTo(x1, h, x1, h - r);
  c.lineTo(x1, s); c.quadraticCurveTo(x1, 0, x1 + s, 0); c.closePath();
}
// Walks from (sx, 0) left along the top edge, down the left side, and along the bottom to the middle.
const RC = 14;
const edgeLen = (sx: number) => (sx - RC) + Math.PI * RC + (H - 2 * RC) + (W / 2 - RC);
function edgeLeft(s: number, sx: number): V2 {
  const q = Math.PI / 2 * RC, top = sx - RC, side = H - 2 * RC;
  if (s <= top) return [sx - s, 0];
  s -= top; if (s <= q) { const a = s / RC; return [RC - Math.sin(a) * RC, RC - Math.cos(a) * RC]; }
  s -= q; if (s <= side) return [0, RC + s];
  s -= side; if (s <= q) { const a = s / RC; return [RC - Math.cos(a) * RC, H - RC + Math.sin(a) * RC]; }
  s -= q; return [RC + s, H];
}
function edgeRun(c: CanvasRenderingContext2D, p: number, fadeTail: number, flash: number) {
  const t = vt / 1000, inset = (v: V2): V2 => [clamp(v[0], 3, W - 3), clamp(v[1], 3, H - 3)];
  c.save(); c.globalCompositeOperation = 'lighter'; c.lineCap = 'round';
  if (fadeTail > .01) for (const side of [-1, 1]) {
    const sx = side < 0 ? isl.x0.value : W - isl.x1.value, L = edgeLen(sx), head = p * L, tail = Math.min(head, L * .5 + 80), n = 40;
    const pts: V2[] = [];
    for (let i = 0; i <= n; i++) { const [x, y] = inset(edgeLeft(head - tail + tail * i / n, sx)); pts.push([side < 0 ? x : W - x, y]); }
    for (const [lw, al] of [[30, .07], [12, .2], [3.5, 1]]) {
      c.lineWidth = lw;
      for (let i = 1; i <= n; i++) {
        c.strokeStyle = RAINBOW[Math.floor(i / n * 5 + t * 3) % RAINBOW.length]; c.globalAlpha = al * (i / n) ** 1.6 * fadeTail;
        c.beginPath(); c.moveTo(...pts[i - 1]); c.lineTo(...pts[i]); c.stroke();
      }
    }
  }
  if (flash > .01) {
    const g = c.createLinearGradient(0, 0, W, H);
    RAINBOW.forEach((col, i) => g.addColorStop(i / (RAINBOW.length - 1), col));
    c.strokeStyle = g;
    for (const [lw, al] of [[32, .08], [12, .25], [3.5, .9]]) { c.lineWidth = lw; c.globalAlpha = al * flash; c.beginPath(); c.roundRect(3, 3, W - 6, H - 6, RC); c.stroke(); }
  }
  c.restore();
}
function draw() {
  const d = dpr;
  drawSky();
  if (wfield.length && skyK > .002) flyDraw(skyK);
  glFlush();
  // background canvas: the island, repainted only when its shape or her light changes
  const x0 = isl.x0.value, x1 = isl.x1.value, h = isl.h.value, islNow = `${x0},${x1},${h},${glowNow}`;
  if (islNow !== islKey) {
    islKey = islNow;
    const c = bctx, open = clamp((h - TOP) / 120);
    c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, bg.width, bg.height); c.setTransform(d, 0, 0, d, 0, 0);
    c.save();
    if (open > 0) { c.shadowColor = rgb(core.light.glow, .28 * open); c.shadowBlur = 50 * d; }
    islandPath(c, x0, x1, h); c.fillStyle = '#000'; c.fill();
    if (open > 0) { c.shadowColor = 'transparent'; c.strokeStyle = rgb(core.light.glow, .3 * open); c.lineWidth = 1; c.stroke(); }
    c.restore();
  }

  // front canvas: the trail, her (or her far-away light), the lens streak, the bloom, the light round the screen
  const f = fctx;
  f.setTransform(1, 0, 0, 1, 0, 0); f.clearRect(0, 0, fg.width, fg.height); f.setTransform(d, 0, 0, d, 0, 0);
  if (trail.length && !reduced.matches) {
    f.save(); f.globalCompositeOperation = 'lighter';
    trail.forEach((p, i) => {
      const a = (1 - (vt - p.t) / 260) * (i / trail.length), g = f.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.R * 1.4);
      g.addColorStop(0, rgb(core.light.glow, .45 * a)); g.addColorStop(1, rgb(core.light.glow, 0));
      f.fillStyle = g; f.beginPath(); f.arc(p.x, p.y, p.R * 1.4, 0, TAU); f.fill();
    });
    f.restore();
  }
  drawLights(f);
  if (ball.mode !== 'off' && ball.R.value >= 5) drawBall(f, core, ball.x.value, ball.y.value, ball.R.value, d, ball.v);
  const ee = vt - edgeAt + B.edge;
  if (ee < B.edgeEnd + 600) {
    const run = reduced.matches ? 1 : eInOut(seg(ee, B.edge, B.edgeEnd - 250));
    const flash = seg(ee, B.edgeEnd - 250, B.edgeEnd - 100) * (1 - seg(ee, B.edgeEnd - 100, B.edgeEnd + 600));
    edgeRun(f, run, 1 - seg(ee, B.edgeEnd - 250, B.edgeEnd), flash);
  }
  // halo (DOM, blurred conic light) follows her
  const hs = Math.max(1, ball.R.value) * 3.3;
  halo.style.opacity = String(haloK);
  if (haloK > .001) { halo.style.width = halo.style.height = `${hs}px`; halo.style.transform = `translate(${ball.x.value - hs / 2}px,${ball.y.value - hs / 2}px)`; }
}

// ---------- loop ----------
let last = 0;
function frame(ts: number) {
  const real = Math.min(50, last ? ts - last : 16); last = ts;
  const dms = real; vt += dms;
  const due = timers.filter(t => t.at <= vt); timers = timers.filter(t => t.at > vt); due.forEach(t => t.fn());
  update(dms / 1000); draw();
  requestAnimationFrame(frame);
}
function resize() {
  const r = screenEl.getBoundingClientRect(); W = r.width; H = r.height; dpr = Math.min(2, devicePixelRatio || 1);
  for (const cv of [bg, fg, sky]) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
  glc.width = Math.round(W * sd); glc.height = Math.round(H * sd);
  skyKey = islKey = '';
  paintBack();
  pw = Math.min(560, W - 24); document.documentElement.style.setProperty('--pw', `${pw}px`); document.documentElement.style.setProperty('--top', `${TOP}px`);
  layoutIntro(); if (islMode === 'notch') snapIsland(); if (phase === 'setup') measure();
}

// ---------- input ----------
screenEl.addEventListener('pointermove', e => { const r = screenEl.getBoundingClientRect(); pointer = [e.clientX - r.left, e.clientY - r.top]; if (phase !== 'wait' && phase !== 'intro') pointerLive = true; });
screenEl.addEventListener('pointerleave', () => { pointer = null; });
$('#go').addEventListener('click', moveIn);
skipBtn.addEventListener('click', skipIntro);
quitBtn.addEventListener('click', () => bridge.quit());
pin.addEventListener('click', e => {
  const b = (e.target as Element).closest<HTMLElement>('[data-act]');
  if (!b || (b as HTMLButtonElement).disabled || b.getAttribute('aria-disabled') === 'true') return;
  const a = b.dataset.act!, v = b.dataset.v ?? '';
  if (a === 'next') next();
  else if (a === 'skip') skip();
  else if (a === 'star') { cue('back', .7); goStep(Number(b.dataset.n)); }
  else if (a === 'asst') {
    S.asstPick = v; if (v !== 'custom') S.asst = v;
    refresh(); moment('14', 1300); cue('happy', .7, true);
    if (v === 'custom') pin.querySelector<HTMLInputElement>('#f-asst')?.focus({ preventScroll: true });
  } else if (a === 'lang') {
    const lang: Lang = v === 'system' ? S.sysLang : v === 'en' ? 'en' : 'zh', changed = lang !== S.lang;
    S.langPick = v as typeof S.langPick; S.lang = lang;
    if (changed) { moment('13', 450); later(450, () => moment('10', 1200)); cue('surprise', .8, true); goStep(3); } else { refresh(); cue('on', .7); }
  } else if (a === 'test') void testKey();
  else if (a === 'mmtest') void testMM();
  else if (a === 'voice') void preview(Number(b.dataset.i));
  else if (a === 'perm') void ask(b.dataset.k as Perm);
  else if (a === 'conn') { const k = b.dataset.k as Conn; if (k === 'web') webField(!S.webOpen); else void connect(k); }
  else if (a === 'webtest') void testWeb();
  else if (a === 'link') { e.preventDefault(); bridge.open(v as 'openai' | 'minimax' | 'tavily'); }
  else if (a === 'enter') void enterApp();
});
pin.addEventListener('input', e => {
  const i = e.target as HTMLInputElement;
  if (i.id === 'f-user') { typedAt = vt; const src = pin.querySelector<HTMLElement>('.src'); if (src) src.hidden = !S.account || i.value.trim() !== S.account; }
  if (i.id === 'f-asst') { S.asst = i.value.trim() || 'Jarvis'; typedAt = vt; }
  if (i.id === 'f-key' && S.keyState !== 'testing' && S.keyState !== 'idle') { S.keyState = 'idle'; refresh(); }
});
pin.addEventListener('paste', e => {
  const i = e.target as HTMLInputElement;
  if (i.id === 'f-key') later(200, testKey); else if (i.id === 'f-mm') later(200, testMM); else if (i.id === 'f-web') later(200, testWeb);
});
addEventListener('keydown', e => {
  const tgt = e.target as HTMLElement;
  if (e.metaKey && e.key.toLowerCase() === 'q') { bridge.quit(); return; }
  if (e.key === 'Escape') { if (phase === 'intro') skipIntro(); return; }
  if (e.key !== 'Enter' || tgt.tagName === 'BUTTON' || tgt.tagName === 'A') return;
  if (phase === 'intro') skipIntro();
  else if (phase === 'hello') moveIn();
  else if (phase === 'setup') {
    if (tgt.id === 'f-key' && S.keyState !== 'ok') void testKey();
    else if (tgt.id === 'f-mm' && S.mmState !== 'ok') void testMM();
    else if (tgt.id === 'f-web') void testWeb();
    else if (stepN === 8) void enterApp();
    else next();
  }
});

// ---------- boot: where the notch is, where 打开 was clicked, the account's name and the system's language ----------
void bridge.info().then(info => {
  TOP = info.top; NOTCH = info.notch; PORT = info.port;
  S.account = S.user = info.name; S.lang = S.sysLang = info.lang;
  $('#go').textContent = tx().go; $('#go-hint').textContent = tx().goHint; skipBtn.textContent = tx().skipIntro; quitBtn.textContent = tx().quit;
  resetWord(); new ResizeObserver(resize).observe(screenEl); resize(); snapIsland();
  start(info.cursor);
  requestAnimationFrame(frame);
});
