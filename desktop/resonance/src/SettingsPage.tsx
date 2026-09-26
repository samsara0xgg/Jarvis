import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { CaretRight, Cpu, Globe, House, Key, LockSimple, Microphone, Planet, Robot, SlidersHorizontal, SpeakerHigh, Waveform, Bell } from '@phosphor-icons/react';
import { tr, useCompanionSettings, type L, type Lang } from './companionSettings';
import { postRoute, useRoute } from './homeData';
import { SKIN_KEYS, SKINS, type Skin } from './starCore';

// Settings as a page in her panel: quick switches on top, then one list per category. Her own settings save
// in this profile and apply at once; Jarvis's own save through the daemon and are greyed out until it serves them.
export type Look = { skin: Skin; auto: boolean; homeGlass: boolean; layout: 'around' | 'grid' };
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
type Daemon = { values: Record<string, unknown>; options?: Record<string, string[]>; restart_pending?: boolean };
const DEMO: Daemon = {
  values: { reply_language: 'follow', wake_threshold: .95, tts_voice: 'Warm Bestie', tts_volume: 1, output_device: 'System default', input_device: 'System default',
    gpt_live: true, mac_aec: false, timesink: true, keep_audio: true, repos: ['jarvis', 'typlus', 'timesink', 'guard-mode', 'drum-machine-pro', 'simple-wiki'],
    model_conversation: 'gpt-5.6-luna', model_background: 'GPT-6 luna', model_report: 'GPT-6 sol · flex' },
  options: { tts_voice: ['Warm Bestie', 'Explorative Girl'], output_device: ['System default', 'Multi-Output Device 2', 'MacBook Pro Speakers'], input_device: ['System default', 'reSpeaker XVF3800', 'MacBook Pro Microphone'] },
};
const STALE: Record<'hour' | 'day' | 'never', L> = { hour: ['1 h', '1 小时'], day: ['1 day', '1 天'], never: ['never', '不收'] };
const SKIN_EN: Record<Skin, string> = { glass: 'Glass', nebula: 'Nebula', galaxy: 'Galaxy', frost: 'Frost', aurora: 'Aurora' };
const SKIN_BG: Record<Skin, string> = {
  glass: 'radial-gradient(circle at 35% 30%,#7f95ff,#121634 68%)', nebula: 'radial-gradient(circle at 35% 30%,#d58cff,#2a1246 70%)',
  galaxy: 'conic-gradient(from 40deg,#1b2350,#8fa6ff,#1b2350,#e6c7ff,#1b2350)', frost: 'radial-gradient(circle at 35% 30%,#dfe6ff,#56618f 75%)',
  aurora: 'linear-gradient(160deg,#3fe0b5,#5a7bff 55%,#b26bff)',
};

type Ctl =
  | { k: 'switch'; on: boolean; set: (on: boolean) => void }
  | { k: 'seg'; value: string; opts: [string, L][]; set: (value: string) => void }
  | { k: 'range'; value: number; min: number; max: number; step: number; pct?: boolean; set: (value: number) => void }
  | { k: 'pick'; value: string; opts: string[]; set: (value: string) => void }
  | { k: 'info'; text: string; tone?: 'ok' | 'warn' }
  | { k: 'act'; label: L; run: () => void }
  | { k: 'skins' };
type Item = { id: string; name: L; note?: L; ctl: Ctl; off?: boolean };
type Cat = { id: string; icon: ReactNode; name: L; sum: string; warm?: boolean; daemon?: boolean; items: Item[] };

export function SettingsPage({ lang, port, open, cat, onCat, ctl, accounts, hiddenAgents, onUnhideAgents, onArrange, onPlugins, onResetHome, notify, head }: {
  lang: Lang; port: string | null; open: boolean; cat: string | null; onCat: (id: string | null) => void; ctl: Controls; accounts: Account[];
  hiddenAgents: number; onUnhideAgents: () => void; onArrange: () => void; onPlugins: () => void; onResetHome: () => void;
  notify: (text: string) => void; head: (title: string, meta?: ReactNode) => ReactNode;
}) {
  const t = (l: L) => tr(lang, l);
  const [s, update] = useCompanionSettings();
  const route = useRoute<Daemon>(port, '/inherent/settings', open, 60_000);
  const [demo, setDemo] = useState(DEMO), [draft, setDraft] = useState<Record<string, number>>({});
  const daemon = port ? route.data : demo, ready = !!daemon;
  const v = (key: string) => daemon?.values[key];
  const save = async (key: string, value: unknown) => {
    if (!port) { setDemo(d => ({ ...d, values: { ...d.values, [key]: value }, restart_pending: true })); return; }
    try { await postRoute(port, '/inherent/settings', { changes: { [key]: value } }); route.reload(); }
    catch { notify(t(['Jarvis didn’t save that.', 'Jarvis 没存上。'])); }
  };
  const restart = async () => {
    if (!port) { setDemo(d => ({ ...d, restart_pending: false })); notify(t(['Restarted.', '已重启。'])); return; }
    try { await postRoute(port, '/inherent/restart', {}); notify(t(['Restarting Jarvis…', '正在重启 Jarvis…'])); }
    catch { notify(t(['Jarvis can’t restart itself yet.', 'Jarvis 还不能自己重启。'])); }
  };
  // Daemon items: a value, its options, and how to save it; greyed out while the daemon does not serve settings.
  const dSwitch = (key: string): Ctl => ({ k: 'switch', on: v(key) === true, set: on => void save(key, on) });
  const dPick = (key: string): Ctl => ({ k: 'pick', value: String(v(key) ?? '—'), opts: daemon?.options?.[key] ?? [], set: value => void save(key, value) });
  const dRange = (key: string, min: number, max: number, step: number, pct?: boolean): Ctl =>
    ({ k: 'range', value: draft[key] ?? (typeof v(key) === 'number' ? v(key) as number : min), min, max, step, pct, set: value => { setDraft(d => ({ ...d, [key]: value })); } });
  const commit = (key: string) => { if (draft[key] !== undefined) { void save(key, draft[key]); setDraft(({ [key]: _, ...rest }) => rest); } };
  const dInfo = (key: string): Ctl => ({ k: 'info', text: Array.isArray(v(key)) ? t([`${(v(key) as unknown[]).length} folders`, `${(v(key) as unknown[]).length} 个`]) : String(v(key) ?? '—') });
  const off = !ready;
  const pct = (n: number) => `${Math.round(n * 100)}%`;

  const reply: [string, L][] = [['follow', ['Follow me', '跟着我']], ['zh', ['中文', '中文']], ['en', ['English', 'English']]];
  const replyName = t(reply.find(([k]) => k === v('reply_language'))?.[1] ?? ['—', '—']);
  const cats: Cat[] = [
    { id: 'general', icon: <Globe/>, name: ['General', '通用'], sum: `${lang === 'zh' ? '中文' : 'English'} · ${t(['answers', '回答'])} ${replyName}`, items: [
      { id: 'lang', name: ['Interface language', '界面语言'], note: ['Her panel and menus', '她的面板和菜单'], ctl: { k: 'seg', value: lang, opts: [['en', ['English', 'English']], ['zh', ['中文', '中文']]], set: value => update({ lang: value as Lang }) } },
      { id: 'reply', name: ['Jarvis answers in', 'Jarvis 用什么语言回答'], note: ['Follow me = the language you spoke in', '跟着我 = 你用什么语言说，它就用什么回答'], ctl: { k: 'seg', value: String(v('reply_language') ?? ''), opts: reply, set: value => void save('reply_language', value) }, off },
      { id: 'asr', name: ['Speech recognition', '语音识别'], ctl: { k: 'info', text: t(['Chinese + English', '中英文自动']), tone: 'ok' } },
      { id: 'open-by', name: ['Open the Dashboard', '打开面板'], ctl: { k: 'seg', value: s.openBy, opts: [['both', ['Both', '都行']], ['click', ['Double-click', '双击她']], ['hover', ['Hover', '悬停刘海']]], set: value => update({ openBy: value as typeof s.openBy }) } },
      { id: 'screen', name: ['Which screen she lives on', '她在哪个屏幕'], ctl: { k: 'seg', value: s.screen, opts: [['follow', ['Follow the cursor', '跟着光标']], ['main', ['Main screen', '主屏幕']]], set: value => update({ screen: value as typeof s.screen }) } },
    ] },
    { id: 'home', icon: <House/>, name: ['Home', '首页'], sum: s.hidden.length ? t([`${s.hidden.length} hidden`, `隐藏了 ${s.hidden.length} 块`]) : t(['Nothing hidden', '没有隐藏']), items: [
      { id: 'arrange', name: ['Arrange the home', '编辑首页'], note: ['Or hold any block on the home', '也可以在首页长按任意一块'], ctl: { k: 'act', label: ['Edit', '编辑'], run: onArrange } },
      { id: 'talk', name: ['Conversation on top', '对话放在顶部'], note: ['After I talk = until 10 min after the last turn', '我开口后 = 最后一句之后 10 分钟内'], ctl: { k: 'seg', value: s.talk, opts: [['after', ['After I talk', '我开口后']], ['always', ['Always', '一直']], ['never', ['Never', '不放']]], set: value => update({ talk: value as typeof s.talk }) } },
      { id: 'foryou', name: ['Things for you show up', '找你的事自己出现'], note: ['Sign-ins and reminders from Jarvis', 'Jarvis 要你登录、提醒你的事'], ctl: { k: 'switch', on: s.foryou, set: on => update({ foryou: on }) } },
      { id: 'brief', name: ['Morning brief shows up', '早报自己出现'], note: ['Once it is written, until you read it', '写好后出现，看过就收起'], ctl: { k: 'switch', on: s.brief, set: on => update({ brief: on }) } },
      { id: 'mail', name: ['Unread mail shows up', '未读邮件自己出现'], note: ['Only mail from people', '只算人发来的'], ctl: { k: 'switch', on: s.mail, set: on => update({ mail: on }) } },
      { id: 'forecast', name: ['Forecast in the morning', '早上显示天气预报'], note: ['The next hours on Today, before 11 AM', '11 点前在“今天”里显示接下来几个小时'], ctl: { k: 'switch', on: s.forecast, set: on => update({ forecast: on }) } },
      { id: 'reset', name: ['Reset the home', '恢复默认首页'], ctl: { k: 'act', label: ['Reset', '恢复'], run: onResetHome } },
    ] },
    { id: 'look', icon: <Planet/>, name: ['Her look', '她的样子'], sum: `${lang === 'zh' ? SKINS[ctl.look.skin].name : SKIN_EN[ctl.look.skin]}${ctl.look.auto ? t([' · changes by herself', ' · 自己换装']) : ''}`, items: [
      { id: 'skin', name: ['Skin', '皮肤'], ctl: { k: 'skins' } },
      { id: 'auto', name: ['Change outfit by herself', '自己换装'], note: ['Every 6–14 min while resting', '在家时每 6–14 分钟一次'], ctl: { k: 'switch', on: ctl.look.auto, set: on => ctl.setLook({ auto: on }) } },
      { id: 'glass', name: ['Show the glass ball at home', '在家露出玻璃球'], ctl: { k: 'switch', on: ctl.look.homeGlass, set: on => ctl.setLook({ homeGlass: on }) } },
      { id: 'layout', name: ['Dashboard layout', '面板布局'], note: ['Two columns has no Settings page; the tray switches back', '两栏没有设置页，用托盘菜单换回来'], ctl: { k: 'seg', value: ctl.look.layout, opts: [['around', ['Around her', '围着她']], ['grid', ['Two columns', '两栏']]], set: value => ctl.setLook({ layout: value as Look['layout'] }) } },
      { id: 'faces', name: ['Her expressions', '她的表情'], ctl: { k: 'act', label: ['Play all', '全部看一遍'], run: ctl.playFaces } },
    ] },
    { id: 'voice', icon: <Waveform/>, name: ['Voice', '语音'], daemon: true, sum: ready ? `${t(['Wake word', '唤醒'])} ${Number(v('wake_threshold') ?? 0).toFixed(2)} · ${String(v('tts_voice') ?? '—')}` : t(['Not connected yet', '还没接上']), items: [
      { id: 'wave', name: ['Talk without the wake word', '免唤醒词对话'], note: ['Until you stop it. Only on headphones or the reSpeaker: on the Mac speakers she hears herself', '直到你停下。只在耳机或 reSpeaker 上用：Mac 自带喇叭她会听到自己'], ctl: { k: 'switch', on: ctl.handsFree, set: ctl.setHandsFree } },
      { id: 'wake', name: ['Wake word sensitivity', '唤醒词灵敏度'], note: ['Higher means fewer false wakes', '越高越少误唤醒'], ctl: dRange('wake_threshold', .8, .99, .01), off },
      { id: 'voice', name: ['Jarvis’s voice', 'Jarvis 的声音'], ctl: dPick('tts_voice'), off },
      { id: 'vol', name: ['Voice volume', '说话音量'], note: ['Above 100% it crackles', '超过 100% 会破音'], ctl: dRange('tts_volume', .3, 1, .05, true), off },
      { id: 'out', name: ['Speaker', '扬声器'], ctl: dPick('output_device'), off },
      { id: 'in', name: ['Microphone', '麦克风'], ctl: dPick('input_device'), off },
      { id: 'live', name: ['GPT-Live', 'GPT-Live'], ctl: dSwitch('gpt_live'), off },
      { id: 'aec', name: ['Echo cancellation on the Mac', 'Mac 上的回声消除'], note: ['Off while the reSpeaker does it', 'reSpeaker 负责时关着'], ctl: dSwitch('mac_aec'), off },
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
    if (x.k === 'info') return <span className={`st-val ${x.tone ? `is-${x.tone}` : ''}`}>{x.text}</span>;
    if (x.k === 'act') return <button className="btn btn-ghost st-act" disabled={i.off} onClick={x.run}>{t(x.label)}</button>;
    if (x.k === 'range') return <span className="st-val">{x.pct ? pct(x.value) : x.value.toFixed(2)}</span>;
    return null;
  };
  const below = (i: Item) => {
    const x = i.ctl;
    if (x.k === 'seg') return <div className="st-seg" role="radiogroup" aria-label={t(i.name)}>{x.opts.map(([key, name]) =>
      <button key={key} role="radio" aria-checked={x.value === key} disabled={i.off} onClick={() => x.set(key)}>{t(name)}</button>)}</div>;
    if (x.k === 'pick') return <div className="st-opts" role="radiogroup" aria-label={t(i.name)}>{(x.opts.length ? x.opts : [x.value]).map(o =>
      <button key={o} role="radio" aria-checked={x.value === o} disabled={i.off} onClick={() => x.set(o)}>{o}</button>)}</div>;
    if (x.k === 'range') return <input className="st-range" type="range" aria-label={t(i.name)} min={x.min} max={x.max} step={x.step} value={x.value} disabled={i.off}
      onChange={e => x.set(Number(e.target.value))} onPointerUp={() => commit(keyOf(i))} onKeyUp={() => commit(keyOf(i))}/>;
    if (x.k === 'skins') return <div className="st-skins" role="radiogroup" aria-label={t(i.name)}>{SKIN_KEYS.map(k =>
      <button key={k} role="radio" aria-checked={ctl.look.skin === k} onClick={() => ctl.setLook({ skin: k })}><i style={{ background: SKIN_BG[k] }}/>{lang === 'zh' ? SKINS[k].name : SKIN_EN[k]}</button>)}</div>;
    return null;
  };
  // Only the daemon's ranges wait for the release to save; hers apply as they move.
  const keyOf = (i: Item) => ({ wake: 'wake_threshold', vol: 'tts_volume' } as Record<string, string>)[i.id] ?? '';

  return <>
    {head(c ? t(c.name) : t(['Settings', '设置']), !c ? <span className={port && !route.data ? '' : 'is-ok'}>● {port ? route.data ? t(['Jarvis connected', 'Jarvis 已连接']) : t(['Her settings only', '只有她的设置']) : t(['Demo', '演示'])}</span> : undefined)}
    <div className="pg-body st-body"><div ref={box} className="st-box">{c ? <>
      {c.daemon && !ready && <p className="pg-sec st-warn">{t(route.missing || !port ? ['Jarvis doesn’t let the panel change these yet. They still live in jarvis.yaml.', 'Jarvis 还不让面板改这些，它们还在 jarvis.yaml 里。'] : ['Checking with Jarvis…', '正在问 Jarvis…'])}</p>}
      <div className="pg-sec st-list">{c.items.map(row)}</div>
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
    {daemon?.restart_pending && <div className="st-restart"><span>{t(['Some changes apply after a restart', '有改动要重启 Jarvis 才生效'])}</span><button onClick={() => void restart()}>{t(['Restart', '重启'])}</button></div>}
  </>;
}
