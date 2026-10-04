import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode, type RefObject } from 'react';
import { CaretRight, Check, Cpu, Globe, House, Key, Lightbulb, LockSimple, Microphone, Planet, Robot, SlidersHorizontal, SpeakerHigh, Waveform, Bell } from '@phosphor-icons/react';
import { tr, useCompanionSettings, type L, type Lang } from './companionSettings';
import { postRoute, useRoute } from './homeData';
import { SKIN_KEYS, SKINS, type Skin } from './starCore';
import type { HomeLook } from './CompanionBall';
import type { HomeFinish } from './homeFinish';
import type { MarkLook } from './AgentMarks';
import type { NightLook } from './NightCard';

// Settings as a page in her panel: quick switches on top, then one list per category. Her own settings save
// in this profile and apply at once; Jarvis's own save through the daemon and are greyed out until it serves them.
export type Look = { skin: Skin; auto: boolean; home: HomeLook; homeFinish: HomeFinish; marks: MarkLook; night: NightLook };
export type Cues = { on: boolean; volume: number };
export type Controls = {
  micMuted: boolean; speechMuted: boolean; handsFree: boolean;
  setMic: (muted: boolean) => void; setSpeech: (muted: boolean) => void; setHandsFree: (on: boolean) => void;
  look: Look; setLook: (change: Partial<Look>) => void; playFaces: () => void;
  cues: Cues; setCues: (change: Partial<Cues>) => void;
};
export type Account = { name: string; ok: boolean; text: string };

// Jarvis's own settings: GET /inherent/settings answers { values, options?, restart_pending? }; POST the same
// route with { changes: { key: value } } saves and answers the same shape. POST /inherent/restart restarts Jarvis.
// defaults: what "System default" is right now for each device setting (null: unknown).
type Daemon = { values: Record<string, unknown>; options?: Record<string, string[]>; defaults?: Record<string, string | null>; restart_pending?: boolean };
const DEMO: Daemon = {
  values: { reply_language: 'follow', wake_threshold: .95, tts_voice: 'Warm Bestie', tts_volume: 1, output_device: 'System default', input_device: 'System default',
    gpt_live: true, timesink: true, keep_audio: true, repos: ['jarvis', 'typlus', 'timesink', 'guard-mode', 'drum-machine-pro', 'simple-wiki'],
    model_conversation: 'gpt-5.6-luna', model_background: 'GPT-6 luna', model_report: 'GPT-6 sol · flex',
    board_light: 'direction', board_brightness: .4, board_speed: 8, board_color: '#002040', board_direction_colors: ['#002040', '#00c066'],
    board_ring_colors: Array<string>(12).fill('#002040'), board_headphone: 8, board_lineout: 8 },
  options: { tts_voice: ['Warm Bestie', 'Explorative Girl'], output_device: ['System default', 'Multi-Output Device 2', 'MacBook Pro Speakers'], input_device: ['System default', 'reSpeaker XVF3800', 'MacBook Pro Microphone'] },
  defaults: { output_device: 'MacBook Pro Speakers', input_device: 'MacBook Pro Microphone' },
};
// The reSpeaker board: GET /inherent/board answers whether it is plugged in and what it hears; its settings live in /inherent/settings and apply at once.
type Board = { present: boolean; firmware: string | null; direction: number | null; speech: boolean };
const DEMO_BOARD: Board = { present: true, firmware: '2.1.1', direction: 251, speech: false };
const STALE: Record<'hour' | 'day' | 'never', L> = { hour: ['1 h', '1 小时'], day: ['1 day', '1 天'], never: ['never', '不收'] };
const SKIN_EN: Record<Skin, string> = { glass: 'Glass', nebula: 'Nebula', galaxy: 'Galaxy', frost: 'Frost', aurora: 'Aurora', codex: 'Icon' };
const SKIN_BG: Record<Skin, string> = {
  glass: 'radial-gradient(circle at 35% 30%,#7f95ff,#121634 68%)', nebula: 'radial-gradient(circle at 35% 30%,#d58cff,#2a1246 70%)',
  galaxy: 'conic-gradient(from 40deg,#1b2350,#8fa6ff,#1b2350,#e6c7ff,#1b2350)', frost: 'radial-gradient(circle at 35% 30%,#dfe6ff,#56618f 75%)',
  aurora: 'linear-gradient(160deg,#3fe0b5,#5a7bff 55%,#b26bff)',
  codex: 'radial-gradient(circle at 40% 45%,#7fe0ff,#5b3fd0 45%,#b04fd8 62%,#0d1030 80%)',
};

// The API keys Jarvis runs on: GET /inherent/setup says whether each is ok, missing or refused; POST
// /inherent/setup/key tests a pasted one and keeps it in the Keychain only when it works. Jarvis reads keys at boot.
type Provider = 'openai' | 'minimax' | 'tavily';
export type AccountKeyDrafts = Partial<Record<Provider, string>>;
type Setup = { keys: Record<Provider, 'ok' | 'missing' | 'bad'> };
const WHY: Record<string, L> = {
  unauthorized: ['The service rejected this key (401). Check it was copied in full.', '服务拒绝了这个密钥（401），看看是不是少复制了一截。'],
  model_denied: ['This key can’t use the model Jarvis needs. Allow it in the provider’s dashboard.', '这个密钥用不了 Jarvis 要的模型，去服务商后台打开模型权限。'],
  quota: ['This account is out of credit.', '这个账户没有额度了。'],
  rate_limited: ['Too many requests. Wait a minute and try again.', '请求太多了，等一分钟再试。'],
  network: ['Couldn’t reach the service. Check the network.', '连不上服务，看看网络。'],
  timeout: ['The service took too long to answer. Try again.', '服务太久没回应，再试一次。'],
  away: ['Jarvis isn’t answering. Try again in a moment.', 'Jarvis 没有回应，等一下再试。'],
  error: ['The key didn’t pass its test.', '这个密钥没测通。'],
};
// Only the installed app can quit for good: in a checkout launchd starts her again at once.
const packaged = new URLSearchParams(location.search).has('packaged');

type Ctl =
  | { k: 'switch'; on: boolean; set: (on: boolean) => void }
  | { k: 'seg'; value: string; opts: [string, L][]; set: (value: string) => void }
  | { k: 'range'; value: number; min: number; max: number; step: number; pct?: boolean; set: (value: number) => void }
  | { k: 'colors'; values: string[]; labels?: L[]; set: (values: string[], now?: boolean) => void }
  | { k: 'pick'; value: string; opts: string[]; set: (value: string) => void; device?: { fallback: string | null } }
  | { k: 'info'; text: string; tone?: 'ok' | 'warn' }
  | { k: 'act'; label: L; run: () => void }
  | { k: 'key'; provider: Provider; text: string; tone?: 'ok' | 'warn' }
  | { k: 'skins' };
type Item = { id: string; name: L; note?: L; ctl: Ctl; off?: boolean };
type Cat = { id: string; icon: ReactNode; name: L; sum: string; warm?: boolean; daemon?: boolean; items: Item[] };

export function SettingsPage({ lang, port, open, cat, onCat, ctl, accounts, keyDrafts, onKeyDraft, hiddenAgents, onUnhideAgents, onArrange, onPlugins, onResetHome, notify, head }: {
  lang: Lang; port: string | null; open: boolean; cat: string | null; onCat: (id: string | null) => void; ctl: Controls; accounts: Account[];
  keyDrafts: AccountKeyDrafts; onKeyDraft: (provider: Provider, value: string) => void;
  hiddenAgents: number; onUnhideAgents: () => void; onArrange: () => void; onPlugins: () => void; onResetHome: () => void;
  notify: (text: string) => void; head: (title: string, meta?: ReactNode) => ReactNode;
}) {
  const t = (l: L) => tr(lang, l);
  const [s, update] = useCompanionSettings();
  const route = useRoute<Daemon>(port, '/inherent/settings', open, 60_000);
  const setup = useRoute<Setup>(port, '/inherent/setup', open && cat === 'accounts', 30_000);
  const boardRoute = useRoute<Board>(port, '/inherent/board', open && cat === 'board', 1000), board = port ? boardRoute.data : DEMO_BOARD;
  // A microphone or speaker plugged in since the last fetch: ask again whenever the Voice category opens.
  useEffect(() => { if (cat === 'voice') route.reload(); }, [cat]);
  const [demo, setDemo] = useState(DEMO), [draft, setDraft] = useState<Record<string, number>>({}), [picks, setPicks] = useState<Record<string, string[]>>({});
  // The restart banner: changes waiting, going down and coming back, back (then gone), or too slow to come back.
  const [phase, setPhase] = useState<'wait' | 'going' | 'back' | 'slow'>('wait');
  const daemon = port ? route.data : demo, ready = !!daemon;
  const v = (key: string) => daemon?.values[key];
  const save = async (key: string, value: unknown) => {
    // The board's settings apply live: only the others wait for a restart.
    if (!port) { setDemo(d => ({ ...d, values: { ...d.values, [key]: value }, ...key.startsWith('board_') ? {} : { restart_pending: true } })); return; }
    try { await postRoute(port, '/inherent/settings', { changes: { [key]: value } }); route.reload(); }
    catch { notify(t(['Jarvis didn’t save that.', 'Jarvis 没存上。'])); }
  };
  const pause = (ms: number) => new Promise(go => setTimeout(go, ms));
  const restart = async () => {
    setPhase('going');
    if (!port) { await pause(1600); setDemo(d => ({ ...d, restart_pending: false })); setPhase('back'); await pause(2200); setPhase('wait'); return; }
    try { await postRoute(port, '/inherent/restart', {}); }
    catch { setPhase('wait'); notify(t(['Jarvis can’t restart itself yet.', 'Jarvis 还不能自己重启。'])); return; }
    // The answer leaves before he goes: wait for him to go, then for him to answer again.
    const up = () => fetch(`http://127.0.0.1:${port}/inherent/settings`, { signal: AbortSignal.timeout(1500) }).then(r => r.ok, () => false);
    let gone = false, back = false;
    for (let i = 0; i < 60 && !back; i++) { await pause(1000); if (!(await up())) gone = true; else back = gone || i >= 8; }
    route.reload();
    if (!back) { setPhase('slow'); return; }
    setPhase('back'); await pause(2200); setPhase('wait');
  };
  // Daemon items: a value, its options, and how to save it; greyed out while the daemon does not serve settings.
  const dSwitch = (key: string): Ctl => ({ k: 'switch', on: v(key) === true, set: on => void save(key, on) });
  const dPick = (key: string): Ctl => ({ k: 'pick', value: String(v(key) ?? '—'), opts: daemon?.options?.[key] ?? [], set: value => void save(key, value) });
  // A speaker or microphone: the first row names what the system default is now; a saved pick that is unplugged stays selected.
  const dDevice = (key: string): Ctl => ({ ...dPick(key) as Extract<Ctl, { k: 'pick' }>, device: { fallback: daemon?.defaults?.[key] ?? null } });
  const dRange = (key: string, min: number, max: number, step: number, pct?: boolean): Ctl =>
    ({ k: 'range', value: draft[key] ?? (typeof v(key) === 'number' ? v(key) as number : min), min, max, step, pct, set: value => { setDraft(d => ({ ...d, [key]: value })); } });
  const commit = (key: string) => { if (draft[key] !== undefined) { void save(key, draft[key]); setDraft(({ [key]: _, ...rest }) => rest); } };
  // The board's colors: the swatch follows the pointer from a local pick; the daemon hears it at most every 100 ms, and at once when the picker lets go.
  // A pick is dropped once the daemon reports it back.
  const lag = useRef<{ t?: ReturnType<typeof setTimeout>; go: () => void }>({ go() {} });
  const dColors = (key: string, n: number, labels?: L[]): Ctl => {
    const saved = [v(key)].flat() as unknown[];
    return { k: 'colors', labels, values: picks[key] ?? Array.from({ length: n }, (_, i) => String(saved[i] ?? '#000000')), set: (next, now) => {
      setPicks(p => ({ ...p, [key]: next }));
      lag.current.go = () => { clearTimeout(lag.current.t); lag.current.t = undefined; void save(key, n === 1 ? next[0] : next); };
      if (now) lag.current.go(); else lag.current.t ??= setTimeout(() => lag.current.go(), 100);
    } };
  };
  useEffect(() => setPicks(p => { const keep = Object.entries(p).filter(([k, c]) => `${c}`.toLowerCase() !== `${v(k)}`.toLowerCase()); return keep.length === Object.keys(p).length ? p : Object.fromEntries(keep); }), [daemon]);
  const dInfo = (key: string): Ctl => ({ k: 'info', text: Array.isArray(v(key)) ? t([`${(v(key) as unknown[]).length} folders`, `${(v(key) as unknown[]).length} 个`]) : String(v(key) ?? '—') });
  const off = !ready;
  const keyCtl = (provider: Provider): Ctl => {
    const state = setup.data?.keys[provider];
    return { k: 'key', provider, text: t(state === 'ok' ? ['Working', '能用'] : state === 'bad' ? ['Refused', '用不了'] : state === 'missing' ? ['Not set', '没填'] : ['—', '—']),
      tone: state === 'ok' ? 'ok' : state === 'bad' || (state === 'missing' && provider === 'openai') ? 'warn' : undefined };
  };
  const pct = (n: number) => `${Math.round(n * 100)}%`;

  const reply: [string, L][] = [['follow', ['Follow me', '跟着我']], ['zh', ['中文', '中文']], ['en', ['English', 'English']]];
  const replyName = t(reply.find(([k]) => k === v('reply_language'))?.[1] ?? ['—', '—']);
  // The board's lights: a short name for the category's line, and what each does for the list.
  const lights: [string, L, L][] = [['off', ['Off', '关'], ['Off', '关']], ['breath', ['Breathe', '呼吸'], ['Breathe: one color fades in and out', '呼吸：一种颜色慢慢亮了又暗']],
    ['rainbow', ['Rainbow', '彩虹'], ['Rainbow: colors go round', '彩虹：几种颜色轮流转']], ['solid', ['Solid', '单色'], ['Solid: one color stays on', '单色：一种颜色常亮']],
    ['direction', ['Direction', '方向'], ['Direction: lights up toward the voice', '方向：朝说话的人那边亮']], ['ring', ['Ring', '环形'], ['Ring: a color for each light', '环形：每颗灯各一种颜色']]];
  const light = String(v('board_light') ?? 'off'), lit = lights.find(([k]) => k === light), lightName = t(lit?.[1] ?? ['—', '—']);
  const cats: Cat[] = [
    { id: 'general', icon: <Globe/>, name: ['General', '通用'], sum: `${lang === 'zh' ? '中文' : 'English'} · ${t(['answers', '回答'])} ${replyName}`, items: [
      { id: 'lang', name: ['Interface language', '界面语言'], note: ['Her panel, and what Jarvis says on its own: the time, confirmations, reports', '她的面板，和 Jarvis 自己说的固定句子：报时、确认、日报'], ctl: { k: 'seg', value: lang, opts: [['en', ['English', 'English']], ['zh', ['中文', '中文']]], set: value => {
        update({ lang: value as Lang });
        if (port) postRoute(port, '/inherent/language', { language: value }).catch(() => notify(t(['Jarvis’s own phrases didn’t switch.', 'Jarvis 的固定句子没切换过去。'])));
      } } },
      { id: 'reply', name: ['Jarvis answers in', 'Jarvis 用什么语言回答'], note: ['Follow me = the language you spoke in', '跟着我 = 你用什么语言说，它就用什么回答'], ctl: { k: 'seg', value: String(v('reply_language') ?? ''), opts: reply, set: value => void save('reply_language', value) }, off },
      { id: 'asr', name: ['Speech recognition', '语音识别'], ctl: { k: 'info', text: t(['Chinese + English', '中英文自动']), tone: 'ok' } },
      { id: 'dictation', name: ['Dictation', '听写'], note: ['Tap the right ⌥ to start and again to finish; the words go where you type', '轻点右 ⌥ 开始，再点一下结束，字贴到你打字的地方'], ctl: { k: 'switch', on: s.dictation, set: on => update({ dictation: on }) } },
      { id: 'open-by', name: ['Open the Dashboard', '打开面板'], ctl: { k: 'seg', value: s.openBy, opts: [['both', ['Both', '都行']], ['click', ['Click notch', '点击刘海']], ['hover', ['Hover', '悬停刘海']]], set: value => update({ openBy: value as typeof s.openBy }) } },
      { id: 'screen', name: ['Which screen she lives on', '她在哪个屏幕'], ctl: { k: 'seg', value: s.screen, opts: [['follow', ['Follow the cursor', '跟着光标']], ['main', ['Main screen', '主屏幕']]], set: value => update({ screen: value as typeof s.screen }) } },
    ] },
    { id: 'home', icon: <House/>, name: ['Home', '首页'], sum: s.hidden.length ? t([`${s.hidden.length} hidden`, `隐藏了 ${s.hidden.length} 块`]) : t(['Nothing hidden', '没有隐藏']), items: [
      { id: 'arrange', name: ['Arrange the home', '编辑首页'], note: ['Or hold any block on the home', '也可以在首页长按任意一块'], ctl: { k: 'act', label: ['Edit', '编辑'], run: onArrange } },
      { id: 'talk', name: ['Conversation on top', '对话放在顶部'], note: ['After I talk = until 10 min after the last turn', '我开口后 = 最后一句之后 10 分钟内'], ctl: { k: 'seg', value: s.talk, opts: [['after', ['After I talk', '我开口后']], ['always', ['Always', '一直']], ['never', ['Never', '不放']]], set: value => update({ talk: value as typeof s.talk }) } },
      { id: 'foryou', name: ['Things for you show up', '找你的事自己出现'], note: ['Sign-ins and reminders from Jarvis', 'Jarvis 要你登录、提醒你的事'], ctl: { k: 'switch', on: s.foryou, set: on => update({ foryou: on }) } },
      { id: 'brief', name: ['Morning brief shows up', '早报自己出现'], note: ['Once each morning, the first time you open', '每天早上第一次打开时出现'], ctl: { k: 'switch', on: s.brief, set: on => update({ brief: on }) } },
      { id: 'mail', name: ['Unread mail shows up', '未读邮件自己出现'], note: ['Only mail from people', '只算人发来的'], ctl: { k: 'switch', on: s.mail, set: on => update({ mail: on }) } },
      { id: 'forecast', name: ['Forecast in the morning', '早上显示天气预报'], note: ['The next hours on Today, before 11 AM', '11 点前在“今天”里显示接下来几个小时'], ctl: { k: 'switch', on: s.forecast, set: on => update({ forecast: on }) } },
      { id: 'reset', name: ['Reset the home', '恢复默认首页'], ctl: { k: 'act', label: ['Reset', '恢复'], run: onResetHome } },
    ] },
    { id: 'look', icon: <Planet/>, name: ['Her look', '她的样子'], sum: `${lang === 'zh' ? SKINS[ctl.look.skin].name : SKIN_EN[ctl.look.skin]}${ctl.look.auto ? t([' · changes by herself', ' · 自己换装']) : ''}`, items: [
      { id: 'skin', name: ['Skin', '皮肤'], ctl: { k: 'skins' } },
      { id: 'auto', name: ['Change outfit by herself', '自己换装'], note: ['Every 6–14 min while resting', '在家时每 6–14 分钟一次'], ctl: { k: 'switch', on: ctl.look.auto, set: on => ctl.setLook({ auto: on }) } },
      { id: 'home', name: ['In the island', '在家的样子'], ctl: { k: 'seg', value: ctl.look.home, opts: [['dark', ['Dark glass', '暗玻璃']], ['eyes', ['Just her eyes', '只有两只眼']]], set: value => ctl.setLook({ home: value as HomeLook }) } },
      { id: 'home-finish', name: ['Notch home', '刘海里的家'], ctl: { k: 'seg', value: ctl.look.homeFinish, opts: [['original', ['Original', '原设计']], ['refined', ['Refined notch', '精修刘海']]], set: value => ctl.setLook({ homeFinish: value as HomeFinish }) } },
      { id: 'marks', name: ['Agent marks', '状态点'], note: ['The session marks beside the notch', '刘海旁边的会话标记'], ctl: { k: 'seg', value: ctl.look.marks, opts: [['spark', ['Spark', '星芒']], ['pixel', ['Pixel', '像素']]], set: value => ctl.setLook({ marks: value as MarkLook }) } },
      { id: 'night', name: ['Night run cards', '挂机卡片'], note: ['Bedtime, in the night and the morning after', '睡前、半夜和早上的卡片'], ctl: { k: 'seg', value: ctl.look.night, opts: [['list', ['List', '列表']], ['trail', ['Star trail', '星轨']]], set: value => ctl.setLook({ night: value as NightLook }) } },
      { id: 'faces', name: ['Her expressions', '她的表情'], ctl: { k: 'act', label: ['Play all', '全部看一遍'], run: ctl.playFaces } },
    ] },
    { id: 'voice', icon: <Waveform/>, name: ['Voice', '语音'], daemon: true, sum: ready ? `${t(['Wake word', '唤醒'])} ${Number(v('wake_threshold') ?? 0).toFixed(2)} · ${String(v('tts_voice') ?? '—')}` : t(['Not connected yet', '还没接上']), items: [
      { id: 'wave', name: ['Talk without the wake word', '免唤醒词对话'], note: ['Until you stop it. Only on headphones or the reSpeaker: on the Mac speakers she hears herself', '直到你停下。只在耳机或 reSpeaker 上用：Mac 自带喇叭她会听到自己'], ctl: { k: 'switch', on: ctl.handsFree, set: ctl.setHandsFree } },
      { id: 'captions', name: ['Captions', '字幕'], note: ['What she says shows as text under her. Only what to read: her spoken lines stay off, and times, lists, links and errors show. With her voice muted, everything shows', '她念的话在她身下出字。只显示要看的：她念的话不出字，有时间、列表、链接，或出错时才出。关掉她的声音时按“全部显示”'],
        ctl: { k: 'seg', value: s.captions, opts: [['all', ['Show all', '全部显示']], ['brief', ['Only what to read', '只显示要看的']], ['none', ['None', '不显示']]], set: value => update({ captions: value as typeof s.captions }) } },
      { id: 'wake', name: ['Wake word sensitivity', '唤醒词灵敏度'], note: ['Higher means fewer false wakes', '越高越少误唤醒'], ctl: dRange('wake_threshold', .8, .99, .01), off },
      { id: 'voice', name: ['Jarvis’s voice', 'Jarvis 的声音'], ctl: dPick('tts_voice'), off },
      { id: 'vol', name: ['Voice volume', '说话音量'], note: ['Above 100% it crackles', '超过 100% 会破音'], ctl: dRange('tts_volume', .3, 1, .05, true), off },
      { id: 'out', name: ['Speaker', '扬声器'], ctl: dDevice('output_device'), off },
      { id: 'in', name: ['Microphone', '麦克风'], ctl: dDevice('input_device'), off },
      { id: 'live', name: ['GPT-Live', 'GPT-Live'], ctl: dSwitch('gpt_live'), off },
    ] },
    { id: 'board', icon: <Lightbulb/>, name: ['Mic board', '麦克风'], daemon: true, sum: !ready ? t(['Not connected yet', '还没接上']) : board?.present === false ? t(['Not plugged in', '没接上']) : `${lightName} · ${pct(Number(v('board_brightness') ?? 0))}`, items: [
      { id: 'board', name: ['Board', '板子'], ctl: { k: 'info', tone: board ? board.present ? 'ok' : 'warn' : undefined,
        text: !board ? '—' : board.present ? `XVF3800 · ${t(['fw', '固件'])} ${board.firmware ?? '—'}` : t(['Not plugged in', '没接上']) }, off },
      { id: 'dir', name: ['Sound from', '声音方向'], note: ['In the board’s own degrees', '用的是板子自己的角度'], ctl: { k: 'info',
        text: board?.present && board.direction != null ? `${board.direction}°${t(board.speech ? [' · someone talking', ' · 有人在说话'] : [' · quiet', ' · 安静'])}` : '—' }, off },
      { id: 'light', name: ['Lights', '灯'], ctl: { k: 'pick', value: t(lit?.[2] ?? ['—', '—']), opts: lights.map(l => t(l[2])),
        set: label => { const key = lights.find(l => t(l[2]) === label)?.[0]; if (key) void save('board_light', key); } }, off },
      ...light === 'breath' || light === 'solid' ? [{ id: 'colors', name: ['Color', '颜色'] as L, ctl: dColors('board_color', 1), off }] : [],
      ...light === 'direction' ? [{ id: 'colors', name: ['Colors', '颜色'] as L, note: ['The base lights the whole ring; the second color points at whoever is talking', '底色是整圈常亮的颜色，方向色只点亮有人说话的那个方向'] as L,
        ctl: dColors('board_direction_colors', 2, [['Base', '底色'], ['Toward the voice', '方向色']]), off }] : [],
      ...light === 'ring' ? [{ id: 'colors', name: ['Color', '颜色'] as L, note: ['One color per light', '每颗灯一种颜色'] as L,
        ctl: dColors('board_ring_colors', 12, Array.from({ length: 12 }, (_, i): L => [`${i + 1}`, `${i + 1}`])), off }] : [],
      ...light !== 'off' ? [{ id: 'bright', name: ['Brightness', '亮度'] as L, ctl: dRange('board_brightness', 0, 1, .05, true), off,
        ...light === 'solid' || light === 'direction' || light === 'ring' ? { note: ['Dims the colors themselves', '把颜色本身调暗'] as L } : {} }] : [],
      ...light === 'breath' || light === 'rainbow' ? [{ id: 'speed', name: ['Speed', '速度'] as L, note: ['Higher is faster', '数字越大越快'] as L, ctl: dRange('board_speed', 1, 32, 1), off }] : [],
      { id: 'hp', name: ['Headphone jack volume', '耳机口音量'], note: ['Only matters with something plugged into the board', '板子上插了耳机或音箱才有用'], ctl: dRange('board_headphone', 0, 9, 1), off },
      { id: 'line', name: ['Line-out volume', '线路输出音量'], ctl: dRange('board_lineout', 0, 9, 1), off },
    ] },
    { id: 'sounds', icon: <Bell/>, name: ['Sounds', '提示音'], sum: ctl.cues.on ? `${t(['On', '开'])} · ${pct(ctl.cues.volume)}` : t(['Off', '关']), items: [
      { id: 'cues', name: ['Sound cues', '提示音'], note: ['Her own cues. The mute button silences them too', '她自己的提示音。静音键也会关掉它们'], ctl: { k: 'switch', on: ctl.cues.on, set: on => ctl.setCues({ on }) } },
      { id: 'cue-vol', name: ['Cue volume', '提示音音量'], ctl: { k: 'range', value: ctl.cues.volume, min: 0, max: 1, step: .05, pct: true, set: volume => ctl.setCues({ volume }) } },
    ] },
    { id: 'agents', icon: <Robot/>, name: ['Agents', 'Agents'], sum: `${[s.claude && 'Claude', s.codex && 'Codex'].filter(Boolean).join(' + ') || '—'} · ${t(STALE[s.stale])}`, items: [
      { id: 'claude', name: ['Claude Code sessions', 'Claude Code 会话'], ctl: { k: 'switch', on: s.claude, set: on => update({ claude: on }) } },
      { id: 'codex', name: ['Codex sessions', 'Codex 会话'], ctl: { k: 'switch', on: s.codex, set: on => update({ codex: on }) } },
      { id: 'stale', name: ['“Needs you” moves to Earlier after', '“等你”多久后收进 Earlier'], note: ['A session left waiting stops counting as waiting', '一直没理的会话不再算作在等你'], ctl: { k: 'seg', value: s.stale, opts: [['hour', ['1 hour', '1 小时']], ['day', ['1 day', '1 天']], ['never', ['Never', '不收']]], set: value => update({ stale: value as typeof s.stale }) } },
      { id: 'hidden', name: ['Hidden sessions', '隐藏的会话'], ctl: hiddenAgents ? { k: 'act', label: [`Show ${hiddenAgents}`, `显示 ${hiddenAgents} 个`], run: onUnhideAgents } : { k: 'info', text: t(['None', '没有']) } },
    ] },
    { id: 'privacy', icon: <LockSimple/>, name: ['Privacy & data', '隐私与数据'], daemon: true, sum: ready ? `TimeSink ${v('timesink') ? t(['on', '开']) : t(['off', '关'])}` : t(['Not connected yet', '还没接上']), items: [
      { id: 'timesink', name: ['Read TimeSink activity', '读取 TimeSink 记录'], note: ['Feeds Now and Projects', '给“现在”和项目用'], ctl: dSwitch('timesink'), off },
      { id: 'audio', name: ['Keep voice recordings', '保留语音录音'], ctl: dSwitch('keep_audio'), off },
      { id: 'repos', name: ['Watched code folders', '关注的代码仓库'], ctl: dInfo('repos'), off },
      { id: 'screen-look', name: ['Looking at your screen', '看你的屏幕'], ctl: { k: 'info', text: t(['Only when you ask', '只在你要求时']), tone: 'ok' } },
    ] },
    { id: 'accounts', icon: <Key/>, name: ['Accounts', '账户'], warm: accounts.some(a => !a.ok), sum: accounts.every(a => a.ok) ? t(['All connected', '都已连接']) : t([`${accounts.filter(a => !a.ok).length} need attention`, `${accounts.filter(a => !a.ok).length} 个要处理`]), items: [
      { id: 'key-openai', name: ['OpenAI API key', 'OpenAI API 密钥'], note: ['Required: every answer uses it', '必填，所有回答都靠它'], ctl: keyCtl('openai') },
      { id: 'key-minimax', name: ['MiniMax API key', 'MiniMax API 密钥'], note: ['Her speaking voice; without it she only types', '她说话的声音；不填就只打字'], ctl: keyCtl('minimax') },
      { id: 'key-tavily', name: ['Tavily API key', 'Tavily API 密钥'], note: ['Web search', '上网搜索'], ctl: keyCtl('tavily') },
      ...accounts.map(a => ({ id: a.name, name: [a.name, a.name] as L, ctl: { k: 'info', text: a.text, tone: a.ok ? 'ok' : 'warn' } as Ctl })),
      { id: 'plugins', name: ['Plugins', '插件'], ctl: { k: 'act', label: ['Open', '打开'], run: onPlugins } },
    ] },
    { id: 'models', icon: <Cpu/>, name: ['Models', '模型'], daemon: true, sum: ready ? String(v('model_conversation') ?? '—') : t(['Not connected yet', '还没接上']), items: [
      { id: 'm-conv', name: ['Conversation', '对话'], ctl: dInfo('model_conversation'), off },
      { id: 'm-bg', name: ['Background analysis', '后台分析'], ctl: dInfo('model_background'), off },
      { id: 'm-report', name: ['Daily report', '日报'], ctl: dInfo('model_report'), off },
    ] },
    { id: 'advanced', icon: <SlidersHorizontal/>, name: ['Advanced', '高级'], sum: port ? t([`Port ${port}`, `端口 ${port}`]) : t(['Demo data', '演示数据']), items: [
      { id: 'conn', name: ['Connection', '连接'], ctl: { k: 'info', text: port ? t([`Port ${port}`, `端口 ${port}`]) : t(['Demo data', '演示数据']), tone: 'ok' } },
      { id: 'restart', name: ['Restart Jarvis', '重启 Jarvis'], ctl: { k: 'act', label: ['Restart', '重启'], run: () => void restart() } },
      ...packaged ? [{ id: 'quit', name: ['Quit Jarvis', '退出 Jarvis'], note: ['She and Jarvis’s background service stop until you open Jarvis again', '她和后台都会停下，直到你再打开 Jarvis'],
        ctl: { k: 'act', label: ['Quit', '退出'], run: () => window.jarvis?.quit?.() } } satisfies Item] : [],
    ] },
  ];

  // A category slides in from the right; going back slides the list in from the left.
  const box = useRef<HTMLDivElement>(null), shown = useRef(cat);
  useLayoutEffect(() => {
    if (shown.current === cat) return;
    const into = !!cat; shown.current = cat;
    if (!matchMedia('(prefers-reduced-motion: reduce)').matches) box.current?.animate([{ transform: `translateX(${into ? 36 : -36}px)`, opacity: 0 }, { transform: 'none', opacity: 1 }], { duration: 380, easing: 'cubic-bezier(.2,.8,.2,1)' });
    box.current?.closest('.pg-body')?.scrollTo({ top: 0 });
  }, [cat]);

  const c = cats.find(x => x.id === cat);
  const quick = (on: boolean, icon: ReactNode, name: L, state: L, flip: () => void) =>
    <button className={`st-q ${on ? 'is-on' : ''}`} aria-pressed={on} onClick={flip}>{icon}<b>{t(name)}</b><span>{t(state)}</span></button>;
  const row = (i: Item) => <div className={`st ${i.off ? 'is-off' : ''}`} key={i.id} data-item={i.id}>
    <div className="st-top"><span className="st-name">{t(i.name)}</span>{control(i)}</div>
    {i.note && <p className="st-note">{t(i.note)}</p>}{below(i)}
  </div>;
  const control = (i: Item) => {
    const x = i.ctl;
    if (x.k === 'switch') return <button className="sw" role="switch" aria-checked={x.on} aria-label={t(i.name)} disabled={i.off} onClick={() => x.set(!x.on)}/>;
    if (x.k === 'info' || x.k === 'key') return <span className={`st-val ${x.tone ? `is-${x.tone}` : ''}`}>{x.text}</span>;
    if (x.k === 'act') return <button className="btn btn-ghost st-act" disabled={i.off} onClick={x.run}>{t(x.label)}</button>;
    if (x.k === 'range') return <span className="st-val">{x.pct ? pct(x.value) : x.step >= 1 ? String(x.value) : x.value.toFixed(2)}</span>;
    return null;
  };
  const below = (i: Item) => {
    const x = i.ctl;
    if (x.k === 'seg') return <div className="st-seg" role="radiogroup" aria-label={t(i.name)}>{x.opts.map(([key, name]) =>
      <button key={key} role="radio" aria-checked={x.value === key} disabled={i.off} onClick={() => x.set(key)}>{t(name)}</button>)}</div>;
    if (x.k === 'pick' && x.device) {
      const { fallback } = x.device, first = x.opts[0] ?? x.value, gone = x.value !== first && !x.opts.includes(x.value);
      const label = (o: string) => o !== first ? o : t(['System default', '跟随系统']) + (fallback ? (lang === 'zh' ? `（${fallback}）` : ` (${fallback})`) : '');
      return <div className="st-opts" role="radiogroup" aria-label={t(i.name)}>{(gone ? [first, x.value, ...x.opts.slice(1)] : x.opts).map(o =>
        <button key={o} role="radio" aria-checked={x.value === o} disabled={i.off} onClick={() => x.value !== o && x.set(o)}>
          {label(o)}{gone && o === x.value && <small className="st-sub">{t(['(not connected)', '（未连接）'])}</small>}</button>)}</div>;
    }
    if (x.k === 'pick') return <div className="st-opts" role="radiogroup" aria-label={t(i.name)}>{(x.opts.length ? x.opts : [x.value]).map(o =>
      <button key={o} role="radio" aria-checked={x.value === o} disabled={i.off} onClick={() => x.set(o)}>{o}</button>)}</div>;
    if (x.k === 'range') return <input className="st-range" type="range" aria-label={t(i.name)} min={x.min} max={x.max} step={x.step} value={x.value} disabled={i.off}
      onChange={e => x.set(Number(e.target.value))} onPointerUp={() => commit(keyOf(i))} onKeyUp={() => commit(keyOf(i))}/>;
    if (x.k === 'colors') return <Swatches ctl={x} name={t(i.name)} off={i.off} lang={lang}/>;
    if (x.k === 'skins') return <div className="st-skins" role="radiogroup" aria-label={t(i.name)}>{SKIN_KEYS.map(k =>
      <button key={k} role="radio" aria-checked={ctl.look.skin === k} onClick={() => ctl.setLook({ skin: k })}><i style={{ background: SKIN_BG[k] }}/>{lang === 'zh' ? SKINS[k].name : SKIN_EN[k]}</button>)}</div>;
    if (x.k === 'key') return <KeyField provider={x.provider} name={t(i.name)} port={port} lang={lang} value={keyDrafts[x.provider] ?? ''}
      onChange={value => onKeyDraft(x.provider, value)} onSaved={() => void restart()}/>;
    return null;
  };
  // Only the daemon's ranges wait for the release to save; hers apply as they move.
  const keyOf = (i: Item) => ({ wake: 'wake_threshold', vol: 'tts_volume', bright: 'board_brightness', speed: 'board_speed', hp: 'board_headphone', line: 'board_lineout' } as Record<string, string>)[i.id] ?? '';

  return <>
    {head(c ? t(c.name) : t(['Settings', '设置']), !c ? <span className={port && !route.data ? '' : 'is-ok'}>● {port ? route.data ? t(['Jarvis connected', 'Jarvis 已连接']) : t(['Her settings only', '只有她的设置']) : t(['Demo', '演示'])}</span> : undefined)}
    <div className="pg-body st-body"><div ref={box} className="st-box">{c ? <>
      {c.daemon && !ready && <div className="pg-sec st-list">{c.items.filter(i => i.off === undefined).map(row)}</div>}
      {c.daemon && !ready && <p className="pg-sec st-warn">{t(route.missing || !port ? ['Jarvis doesn’t let the panel change these yet. They still live in jarvis.yaml.', 'Jarvis 还不让面板改这些，它们还在 jarvis.yaml 里。'] : ['Checking with Jarvis…', '正在问 Jarvis…'])}</p>}
      <div className="pg-sec st-list">{(c.daemon && !ready ? c.items.filter(i => i.off !== undefined) : c.items).map(row)}</div>
    </> : <>
      <div className="pg-sec st-quick">
        {quick(!ctl.micMuted, <Microphone/>, ['Mic', '麦克风'], ctl.micMuted ? ['Off', '关'] : ['On', '开'], () => ctl.setMic(!ctl.micMuted))}
        {quick(!ctl.speechMuted, <SpeakerHigh/>, ['Voice', '声音'], ctl.speechMuted ? ['Muted', '静音'] : ['On', '开'], () => ctl.setSpeech(!ctl.speechMuted))}
        {quick(ctl.handsFree, <Waveform/>, ['Hands-free', '免唤醒'], ctl.handsFree ? ['On', '开'] : ['Off', '关'], () => ctl.setHandsFree(!ctl.handsFree))}
      </div>
      <div className="pg-sec st-cats">{cats.map(x => <button key={x.id} className="st-cat" data-cat={x.id} onClick={() => onCat(x.id)}>
        <span className="st-ic">{x.icon}</span><span className="st-tx"><b>{t(x.name)}</b><small className={x.warm ? 'is-warm' : ''}>{x.sum}</small></span><CaretRight size={12}/>
      </button>)}</div>
    </>}</div></div>
    {(daemon?.restart_pending || phase !== 'wait') && <div className={`st-restart is-${phase}`} role="status">
      <span key={phase}>{phase === 'going' ? t(['Restarting Jarvis…', '正在重启 Jarvis…']) : phase === 'back' ? t(['Restarted. Jarvis is back.', '重启好了，Jarvis 回来了。'])
        : phase === 'slow' ? t(['Jarvis is taking longer than usual to come back.', 'Jarvis 回来得比平时慢。']) : t(['Some changes apply after a restart', '有改动要重启 Jarvis 才生效'])}</span>
      {phase === 'going' ? <i className="st-spin" aria-hidden="true"/> : phase === 'back' ? <i className="st-done" aria-hidden="true"><Check weight="bold"/></i>
        : <button onClick={() => void restart()}>{t(['Restart', '重启'])}</button>}
    </div>}
  </>;
}

// The board's colors. The companion is a panel that never becomes the key window, so the
// system color panel behind <input type="color"> never opens there: the picker lives in the page.
const PRESETS = ['#002040', '#00c066', '#ff3b30', '#ff9500', '#ffd60a', '#0a84ff', '#bf5af2', '#ffffff'];
const toHsv = (hex: string) => {
  const n = parseInt(hex.slice(1), 16), r = (n >> 16 & 255) / 255, g = (n >> 8 & 255) / 255, b = (n & 255) / 255;
  const max = Math.max(r, g, b), d = max - Math.min(r, g, b);
  const h = !d ? 0 : max === r ? ((g - b) / d + 6) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return { h: h * 60, s: max ? d / max : 0, v: max };
};
const toHex = ({ h, s, v }: { h: number; s: number; v: number }) =>
  '#' + [5, 3, 1].map(n => { const k = (n + h / 60) % 6; return Math.round(255 * (v - v * s * Math.max(0, Math.min(k, 4 - k, 1)))).toString(16).padStart(2, '0'); }).join('');

function Swatches({ ctl: x, name, off, lang }: { ctl: Extract<Ctl, { k: 'colors' }>; name: string; off?: boolean; lang: Lang }) {
  const t = (l: L) => tr(lang, l);
  const [open, setOpen] = useState<number | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const label = (j: number) => x.labels?.[j] ? `${name} ${t(x.labels[j])}` : name;
  return <div className="st-colors" ref={box} role="group" aria-label={name}>
    {x.values.map((c, j) => <label key={j}>
      <button className="st-sw" style={{ background: c }} disabled={off} aria-label={label(j)} aria-expanded={open === j} onClick={() => setOpen(open === j ? null : j)}/>
      {x.labels?.[j] && <small>{t(x.labels[j])}</small>}</label>)}
    {open !== null && !off && <ColorPick key={open} value={x.values[open]} name={label(open)} box={box} onClose={() => setOpen(null)}
      set={(c, now) => x.set(x.values.map((o, k) => k === open ? c : o), now)}/>}
  </div>;
}

// Dragging sends at most every 100 ms (the caller's throttle); letting go, a preset or a typed hex sends at once.
function ColorPick({ value, name, box, set, onClose }: { value: string; name: string; box: RefObject<HTMLDivElement | null>; set: (c: string, now?: boolean) => void; onClose: () => void }) {
  const [hsv, setHsv] = useState(() => toHsv(value)), [typed, setTyped] = useState<string | null>(null);
  const pop = useRef<HTMLDivElement>(null);
  const pick = (next: typeof hsv, now?: boolean) => { setHsv(next); set(toHex(next), now); };
  // Esc needs key focus, which the companion panel takes only when asked; an outside click closes it too.
  useEffect(() => {
    pop.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    void Promise.resolve(window.jarvis?.focus(true)).then(() => pop.current?.focus({ preventScroll: true }));
    const outside = (e: PointerEvent) => { if (!box.current?.contains(e.target as Node)) onClose(); };
    document.addEventListener('pointerdown', outside);
    return () => { document.removeEventListener('pointerdown', outside); void window.jarvis?.focus(false); };
  }, []);
  const sv = (e: ReactPointerEvent<HTMLDivElement>, now?: boolean) => {
    const r = e.currentTarget.getBoundingClientRect(), clamp = (n: number) => Math.min(1, Math.max(0, n));
    pick({ ...hsv, s: clamp((e.clientX - r.left) / r.width), v: clamp(1 - (e.clientY - r.top) / r.height) }, now);
  };
  return <div className="st-pick" ref={pop} tabIndex={-1} role="dialog" aria-label={name}
    onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); onClose(); } }}>
    <div className="st-sv" style={{ background: `linear-gradient(to top,#000,transparent),linear-gradient(to right,#fff,hsl(${hsv.h} 100% 50%))` }}
      onPointerDown={e => { e.currentTarget.setPointerCapture(e.pointerId); sv(e); }} onPointerMove={e => { if (e.buttons) sv(e); }} onPointerUp={e => sv(e, true)}>
      <i style={{ left: `${hsv.s * 100}%`, top: `${(1 - hsv.v) * 100}%`, background: toHex(hsv) }}/>
    </div>
    <input className="st-hue" type="range" min={0} max={359} value={Math.round(hsv.h)} aria-label="Hue"
      onChange={e => pick({ ...hsv, h: Number(e.target.value) })} onPointerUp={() => set(toHex(hsv), true)} onKeyUp={() => set(toHex(hsv), true)}/>
    <div className="st-pre">
      {PRESETS.map(c => <button key={c} style={{ background: c }} aria-label={c} onClick={() => pick(toHsv(c), true)}/>)}
      <input value={typed ?? toHex(hsv)} aria-label="Hex" spellCheck={false} maxLength={7} onPointerDown={() => void window.jarvis?.focus(true)} onBlur={() => setTyped(null)}
        onChange={e => { const c = e.target.value.trim(); setTyped(c); if (/^#?[0-9a-f]{6}$/i.test(c)) pick(toHsv(`#${c.replace('#', '').toLowerCase()}`), true); }}/>
    </div>
  </div>;
}

// A kept key restarts Jarvis, which reads keys only at boot. The OpenAI test makes real calls, so it gets 90 s.
function KeyField({ provider, name, port, lang, value: key, onChange: setKey, onSaved }: { provider: Provider; name: string; port: string | null; lang: Lang; value: string; onChange: (value: string) => void; onSaved: () => void }) {
  const t = (l: L) => tr(lang, l);
  const [busy, setBusy] = useState(false), [why, setWhy] = useState<string | null>(null);
  const test = async () => {
    setBusy(true); setWhy(null);
    try {
      const r = await fetch(`http://127.0.0.1:${port}/inherent/setup/key`, { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ provider, key: key.trim() }), signal: AbortSignal.timeout(90_000) });
      const answer = await r.json() as { ok?: boolean; checks?: { ok: boolean; reason?: string }[] };
      if (answer.ok) { setKey(''); onSaved(); }
      else setWhy(t(WHY[answer.checks?.find(c => !c.ok)?.reason ?? ''] ?? WHY.error));
    } catch { setWhy(t(WHY.away)); }
    setBusy(false);
  };
  return <>
    <form className="st-key" onSubmit={e => { e.preventDefault(); if (key.trim()) void test(); }}>
      <input type="password" value={key} onChange={e => setKey(e.target.value)} placeholder={t(['Paste a new key', '贴一个新的密钥'])} aria-label={name}
        autoComplete="off" spellCheck={false} disabled={!port || busy}/>
      <button className="btn btn-ghost" disabled={!port || busy || !key.trim()}>{busy ? t(['Testing…', '测试中…']) : t(['Test and save', '测试并保存'])}</button>
    </form>
    {why && <p className="st-note st-why" role="alert">{why}</p>}
  </>;
}
