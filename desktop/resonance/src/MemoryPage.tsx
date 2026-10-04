import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type ComponentProps, type ReactNode } from 'react';
import { ArrowCounterClockwise, CaretRight, MagnifyingGlass, PencilSimple, PushPin, Trash, X } from '@phosphor-icons/react';
import { useCompanionSettings, type L, type Lang } from './companionSettings';
import type { Route } from './homeData';
import { MorphText } from './MorphText';
import { MOTION } from './motion';
import './memory-page.css';

// The Dashboard's memory page (ADR 0154): what Jarvis keeps about you, where each line came from, and the means to correct it.
// Six screens: the kept notes (with last night's changes on top), one note opened, one note edited, a search over every word said,
// the day summaries, and the change log with its undo. Every write is a row in the daemon's core memory versions; the page only asks.
export type Tab = 'items' | 'days' | 'changes';
export type Who = 'all' | 'user' | 'jarvis';
export type MemView = { k: 'item'; id: string; morph?: { from: string; to: string } } | { k: 'edit'; id: string } | { k: 'convo'; day: string; around?: string };
export type MemNav = { tab: Tab; query: string; who: Who; stack: MemView[] };
export const MEM_HOME: MemNav = { tab: 'items', query: '', who: 'all', stack: [] };

export type Quote = { who: 'user' | 'jarvis' | 'other'; text: string };
export type Entry = { kind: 'add' | 'rewrite' | 'stale' | 'suggest_stale'; id: string; section: string; text: string; before?: string; quote: Quote | null };
export type MemItem = { id: string; text: string; pinned: boolean };
export type MemoryOverview = {
  version: { id: string; ts: string; origin: string }; items: number; days: number; chars: number; max_chars: number; booted_max_chars: number;
  sections: { name: string; items: MemItem[] }[]; new: { day: string | null; ts: string | null; version: string | null; entries: Entry[] };
};
type Source = { id: string; who: Quote['who']; ts: string; day: string; text: string };
type When = { ts: string; origin: string; day: string };
type ItemDetail = { id: string; section: string; text: string; pinned: boolean; number: number; chars: number; sources: Source[]; born: When | null; edits: When[]; edit_count: number; reminder: boolean };
type Hit = { id: string; ts: string; who: Quote['who']; text: string };
type Found = { words: string[]; days: { day: string; hits: Hit[] }[]; total: number; truncated: boolean; since: string };
type Card = { day: string; ts: string; kind: 'model' | 'verbatim' | 'user'; records: number; lines: string[] };
type DayDetail = { day: string; ts: string; kind: Card['kind']; records: number; sections?: Record<'topics' | 'decisions' | 'unfinished', string[]>; lines?: string[] };
type Version = { id: string; ts: string; origin: string; kind: string; day: string | null; current: boolean; undoable: boolean; chars: number; edited: number; moved: number;
  counts: { add: number; chg: number; old: number }; lines: { tag: 'add' | 'chg' | 'old'; text: string }[]; more: number };
type DayRecords = { day: string; total: number; offset: number; records: { id: string; ts: string; who: Quote['who']; text: string }[] };

const SECTION_EN: Record<string, string> = { 关于你: 'About you', 偏好: 'Preferences', 常提到的人: 'People', 正在做的事: 'Working on', 定下来的规矩: 'Rules', 承诺和待办: 'Commitments' };
const sectionName = (lang: Lang, zh: string) => lang === 'zh' ? zh : SECTION_EN[zh] ?? zh;
const DAY_HEADS: [keyof NonNullable<DayDetail['sections']>, L][] = [['topics', ['What we talked about', '聊了什么']], ['decisions', ['What you decided', '你说定的']], ['unfinished', ['Not finished', '没做完']]];
const ITEM_MAX = 400;

const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const dur = (ms: number) => reduced.matches ? 0 : ms;
const EASE = 'cubic-bezier(.16,1,.3,1)';
const pad = (n: number) => String(n).padStart(2, '0');
const hhmm = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const md = (iso: string) => { const d = new Date(iso); return `${d.getMonth() + 1}/${d.getDate()}`; };
const sameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString();
const focusWindow = (event: { currentTarget: HTMLElement }) => { const el = event.currentTarget; void window.jarvis?.focus(true).then(() => el.focus({ preventScroll: true })); };
const grabFocus = (el: HTMLElement | null) => { if (el) void window.jarvis?.focus(true).then(() => el.focus({ preventScroll: true })); };

class Refused extends Error { constructor(readonly status: number, message: string) { super(message); } }
async function api<T>(port: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const timeout = AbortSignal.timeout(8000);
  const r = await fetch(`http://127.0.0.1:${port}/inherent/memory${path}`, { method: body === undefined ? 'GET' : 'POST', signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    ...(body === undefined ? {} : { headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }) });
  if (!r.ok) { const d = await r.json().catch(() => ({})) as { detail?: unknown }; throw new Refused(r.status, typeof d.detail === 'string' ? d.detail : String(r.status)); }
  return r.json() as Promise<T>;
}
// The last answer stays on screen while the next one loads, so a search or a reload never blanks the list.
function useGet<T>(port: string, path: string | null, rev: number) {
  const [s, set] = useState<{ data: T | null; error: boolean; loading: boolean }>({ data: null, error: false, loading: path !== null });
  useEffect(() => {
    if (path === null) return;
    const ctl = new AbortController();
    set(v => ({ ...v, loading: true }));
    api<T>(port, path, undefined, ctl.signal).then(data => set({ data, error: false, loading: false }), () => { if (!ctl.signal.aborted) set(v => ({ ...v, error: true, loading: false })); });
    return () => ctl.abort();
  }, [port, path, rev]);
  return s;
}

// Collapses its child to nothing (and fades it) while `gone`: a deleted or confirmed line leaves instead of vanishing.
function Fold({ gone = false, open = true, children, className }: { gone?: boolean; open?: boolean; children: ReactNode; className?: string }) {
  return <div className={`mem-fold ${className ?? ''}`} data-shut={gone || !open ? '' : undefined} inert={gone || !open}><div>{children}</div></div>;
}

type Acts = {
  port: string; rev: number; lang: Lang; t: (l: L) => string; busy: boolean;
  run: <T>(path: string, body: unknown) => Promise<T | null>; push: (view: MemView) => void;
  tab: (tab: Tab) => void; undo: (version: string) => Promise<void>; toast: (text: string, version?: string) => void;
};

export function MemoryPage({ port, overview, nav, setNav, notify }: {
  port: string; overview: Route<MemoryOverview>; nav: MemNav; setNav: (change: (n: MemNav) => MemNav) => void; notify: (text: string, undo?: () => void) => void;
}) {
  const [{ lang }] = useCompanionSettings();
  const t = (l: L) => l[lang === 'zh' ? 1 : 0];
  const [rev, setRev] = useState(0), [busy, setBusy] = useState(false);
  const top = nav.stack.at(-1) ?? null;
  const root = useRef<HTMLDivElement>(null);

  const refresh = () => { setRev(n => n + 1); overview.reload(); };
  const undo = async (version: string) => {
    try { await api(port, '/undo', { version }); notify(t(['Taken back', '撤回了'])); }
    catch (e) { notify(e instanceof Refused ? e.message : t(['Couldn’t take that back.', '没能撤回。'])); }
    refresh();
  };
  const toast = (text: string, version?: string) => notify(text, version ? () => void undo(version) : undefined);
  const run = async <T,>(path: string, body: unknown): Promise<T | null> => {
    setBusy(true);
    try { const r = await api<T>(port, path, body); refresh(); return r; }
    catch (e) { notify(e instanceof Refused && e.status !== 502 ? e.message : t(['That didn’t go through. Try again.', '没成功，请再试一次。'])); return null; }
    finally { setBusy(false); }
  };
  const acts: Acts = { port, rev, lang, t, busy, run, undo, toast,
    push: view => setNav(n => ({ ...n, stack: [...n.stack, view] })), tab: tab => setNav(n => ({ ...n, tab, stack: [], query: '' })) };

  // A screen pushed on top comes in from the right, a screen revealed by going back from the left; the page is still while it happens.
  const shown = useRef(`${nav.stack.length}`), key = top ? `${nav.stack.length}:${top.k}` : '0', before = useRef<MemView[]>(nav.stack);
  useLayoutEffect(() => {
    const prev = shown.current, was = before.current; shown.current = key; before.current = nav.stack;
    if (prev === key) return;
    const deeper = nav.stack.length > Number(prev.split(':')[0]);
    // Coming back to the list, the row you opened has the focus again.
    const left = deeper ? null : was[nav.stack.length];
    if (left?.k === 'item' && !top) { const row = root.current?.querySelector<HTMLElement>(`.mem-row[data-id="${left.id}"]`); row?.focus({ preventScroll: true }); }
    root.current?.querySelector('.mem-view')?.animate([{ transform: `translateX(${deeper ? 36 : -36}px)`, opacity: 0 }, { transform: 'none', opacity: 1 }], { duration: dur(deeper ? MOTION.medium : MOTION.medium * MOTION.exit), easing: EASE });
    root.current?.closest('.pg-body')?.scrollTo({ top: 0 });
  }, [key]);

  const o = overview.data;
  return <div className="mem" data-lang={lang} ref={root}>
    {!o ? <p className="pg-sec muted">{overview.missing ? t(['This Jarvis can’t show its memory yet.', '这个 Jarvis 还不能显示记忆。']) : t(['Reading what she remembers…', '正在读她记着的…'])}</p>
    : <div className="mem-view" key={key}>
      {!top ? <Main o={o} nav={nav} setNav={setNav} a={acts}/>
        : top.k === 'item' ? <ItemView key={top.id} id={top.id} morph={top.morph} onMorphed={() => setNav(n => ({ ...n, stack: n.stack.map((v, i) => i === n.stack.length - 1 && v.k === 'item' ? { k: 'item', id: v.id } : v) }))} a={acts} pop={() => setNav(n => ({ ...n, stack: n.stack.slice(0, -1) }))}/>
        : top.k === 'edit' ? <EditView key={top.id} id={top.id} a={acts} done={(id, morph) => setNav(n => ({ ...n, stack: [...n.stack.slice(0, -1).filter(v => !(v.k === 'item' && v.id === id)), { k: 'item', id, morph }] }))} cancel={() => setNav(n => ({ ...n, stack: n.stack.slice(0, -1) }))}/>
        : <Convo key={`${top.day}${top.around ?? ''}`} day={top.day} around={top.around} a={acts}/>}
    </div>}
  </div>;
}

// ——— 1 · the list, with the search on top ———
function Main({ o, nav, setNav, a }: { o: MemoryOverview; nav: MemNav; setNav: (change: (n: MemNav) => MemNav) => void; a: Acts }) {
  const { t } = a;
  const [draft, setDraft] = useState(nav.query), input = useRef<HTMLInputElement>(null);
  useEffect(() => { const id = setTimeout(() => { const q = draft.trim(); setNav(n => n.query === q ? n : { ...n, query: q }); }, 220); return () => clearTimeout(id); }, [draft]);
  useEffect(() => { if (nav.query === '') setDraft(d => d.trim() === '' ? d : ''); }, [nav.query]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') { e.preventDefault(); grabFocus(input.current); } };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, []);
  const searching = nav.query !== '';
  return <>
    <div className="mem-search" onClick={() => grabFocus(input.current)}>
      <MagnifyingGlass size={14} weight="bold"/>
      <input ref={input} aria-label={t(['Search everything you said', '搜你说过的每一句'])} placeholder={t(['Search every line you’ve said', '搜你说过的每一句'])} autoComplete="off" spellCheck={false} value={draft}
        onPointerDown={focusWindow} onChange={e => setDraft(e.target.value)}
        onKeyDown={e => { if (e.key === 'Escape' && draft) { e.stopPropagation(); setDraft(''); } else if (e.key === 'Enter' && e.nativeEvent.isComposing) e.preventDefault(); }}/>
      {draft ? <button className="mem-clear" aria-label={t(['Clear the search', '清空搜索'])} onClick={e => { e.stopPropagation(); setDraft(''); grabFocus(input.current); }}><X size={10} weight="bold"/></button> : <kbd>⌘F</kbd>}
    </div>
    {searching ? <Search q={nav.query} who={nav.who} setWho={who => setNav(n => ({ ...n, who }))} a={a}/> : <>
      <div className="mem-chips" role="group" aria-label={t(['Show', '显示'])}>
        <button className="mem-chip" aria-pressed={nav.tab === 'items'} onClick={() => a.tab('items')}>{t(['Kept', '记着的'])}<em>{o.items}</em></button>
        <button className="mem-chip" aria-pressed={nav.tab === 'days'} onClick={() => a.tab('days')}>{t(['Days', '日摘要'])}<em>{o.days}</em></button>
        <button className="mem-chip" aria-pressed={nav.tab === 'changes'} onClick={() => a.tab('changes')}>{t(['Changes', '改动'])}</button>
      </div>
      <div className="mem-pane" key={nav.tab}>{nav.tab === 'items' ? <Items o={o} a={a}/> : nav.tab === 'days' ? <Days a={a}/> : <Changes o={o} a={a}/>}</div>
    </>}
  </>;
}

// ——— the kept notes ———
const TAG: Record<Entry['kind'], [string, L]> = { add: ['add', ['New', '新']], rewrite: ['chg', ['Edited', '改']], stale: ['old', ['Stale', '过时']], suggest_stale: ['old', ['Stale?', '过时?']] };

function Items({ o, a }: { o: MemoryOverview; a: Acts }) {
  const { t, lang } = a;
  const [gone, setGone] = useState<string[]>([]);
  useEffect(() => setGone([]), [o.version.id]); // a new version carries the truth; the optimistic hide has done its job
  const entries = o.new.entries.filter(e => !gone.includes(e.id + e.kind));
  // An entry leaves with its own animation; the write runs meanwhile and puts it back if the daemon refuses.
  const leave = async (e: Entry, path: string, body: unknown, say?: L) => {
    const k = e.id + e.kind;
    setGone(g => [...g, k]);
    const r = await a.run<{ version: string }>(path, body);
    if (!r) setGone(g => g.filter(x => x !== k));
    else if (say) a.toast(t(say), r.version);
  };
  const today = o.new.ts ? sameDay(new Date(o.new.ts), new Date()) : false;
  return <>
    {entries.length > 0 && <section className="mem-sec" aria-label={t(['Added last night', '昨晚新记的'])}>
      <div className="mem-sh"><h4>{today ? t(['Added last night', '昨晚新记的']) : t(['Added at the last pass', '上次整理新记的'])} · {entries.length}</h4>{o.new.day && <small>{t([`from ${md(o.new.day)}`, `从 ${md(o.new.day)} 的话里`])}</small>}</div>
      <div className="mem-new">{entries.map(e => <Fold key={e.id + e.kind} gone={gone.includes(e.id + e.kind)}><article className="mem-ent" data-kind={e.kind}>
        <span className={`mem-tag is-${TAG[e.kind][0]}`}>{t(TAG[e.kind][1])}</span>
        <div className="mem-ent-b">
          {e.kind === 'stale' ? <p className="mem-ent-t is-struck">{e.text}</p>
            : <button className="mem-ent-t is-link" aria-label={`${t(['Open', '打开'])}: ${e.text}`} onClick={() => a.push({ k: 'item', id: e.id })}>{e.text}</button>}
          {e.kind === 'rewrite' && e.before && <p className="mem-was">{t(['was', '原来'])}: {e.before}</p>}
          {e.quote ? <p className="mem-q"><b>{e.quote.who === 'user' ? t(['You said', '你说']) : t(['Jarvis said', '她说'])}</b>「{e.quote.text}」</p>
            : e.kind === 'suggest_stale' ? <p className="mem-q">{t(['It looks out of date, but you set this one, so it stayed.', '夜里觉得可能过时了，但这条是你定的，没动。'])}</p> : null}
          <div className="mem-acts">
            {e.kind === 'stale' ? <>
              <button className="mem-act is-ok" onClick={() => void leave(e, '/item/confirm', { id: e.id }, ['Marked right', '记下了'])}>{t(['Right', '对'])}</button>
              <button className="mem-act" onClick={() => void leave(e, '/item/keep', { version: o.new.version, id: e.id }, ['Kept it', '留着了'])}>{t(['Keep it', '留着'])}</button></>
            : e.kind === 'suggest_stale' ? <>
              <button className="mem-act is-ok" onClick={() => void leave(e, '/item/delete', { id: e.id }, ['Deleted', '删掉了'])}>{t(['Right, drop it', '对，删掉'])}</button>
              <button className="mem-act" onClick={() => void leave(e, '/item/confirm', { id: e.id }, ['Kept it', '留着了'])}>{t(['Keep it', '留着'])}</button></>
            : <>
              <button className="mem-act is-ok" onClick={() => void leave(e, '/item/confirm', { id: e.id }, ['Marked right', '记下了'])}>{t(['Right', '对'])}</button>
              <button className="mem-act" onClick={() => a.push({ k: 'edit', id: e.id })}>{t(['Edit', '改'])}</button>
              <button className="mem-act is-del" onClick={() => void leave(e, '/item/delete', { id: e.id }, ['Deleted', '删掉了'])}>{t(['Delete', '删'])}</button></>}
          </div>
        </div>
      </article></Fold>)}</div>
    </section>}
    {o.sections.map(s => <section className="mem-sec" key={s.name} aria-label={sectionName(lang, s.name)}>
      <div className="mem-sh"><h4>{sectionName(lang, s.name)} · {s.items.length}</h4></div>
      {s.items.length ? <div className="mem-rows">{s.items.map(i => <button className="mem-row" key={i.id} data-id={i.id} onClick={() => a.push({ k: 'item', id: i.id })}>
        <span className="mem-row-t">{i.text}</span>{i.pinned && <em className="mem-tag is-mine"><PushPin size={9} weight="fill"/>{t(['Yours', '你改的'])}</em>}<CaretRight size={11} weight="bold"/></button>)}</div>
        : <p className="mem-empty">{t(['Nothing yet.', '还没有'])}</p>}
    </section>)}
  </>;
}

// ——— 2 · one note opened ———
const whenText = (lang: Lang, w: When, verb: 'born' | 'edit') => {
  const t = (l: L) => l[lang === 'zh' ? 1 : 0], d = md(w.origin === 'nightly' ? w.day : w.ts);
  if (verb === 'born') return w.origin === 'nightly' ? t([`Noted from ${d}`, `${d} 的话里记下`]) : w.origin === 'remember' ? t([`You asked her to keep it · ${d}`, `${d} 你让她记的`])
    : w.origin === 'setup' ? t(['Written at first setup', '首次设置时写的']) : w.origin === 'migration' ? t(['Carried over from the old notes', '从旧记录搬来的']) : t([`You kept it · ${d}`, `${d} 你留着的`]);
  return w.origin === 'user' ? t([`you edited it ${d}`, `${d} 你改过`]) : t([`rewritten ${d}`, `${d} 夜里改过`]);
};
const stamp = (iso: string, lang: Lang) => { const d = new Date(iso); return sameDay(d, new Date()) ? `${lang === 'zh' ? '今天' : 'Today'} ${hhmm(d)}` : `${d.getMonth() + 1}/${d.getDate()} ${hhmm(d)}`; };
const dayLabel = (day: string, lang: Lang) => { const [y, m, d] = day.split('-').map(Number); return `${m}/${d} ${new Date(y!, m! - 1, d).toLocaleDateString(lang === 'zh' ? 'zh-CN' : 'en-US', { weekday: 'short' })}`; };

function SourceCard({ s, a }: { s: Source; a: Acts }) {
  const { t } = a;
  return <button className="mem-src" onClick={() => a.push({ k: 'convo', day: s.day, around: s.id })}>
    <span className="mem-src-w"><b>{s.who === 'user' ? t(['You said', '你说']) : t(['Jarvis said', '她说'])}</b><time>{md(s.ts)} {hhmm(new Date(s.ts))}</time><span>{t(['That day ›', '那天的对话 ›'])}</span></span>
    <span className="mem-src-t">{s.text}</span></button>;
}

function ItemView({ id, morph, onMorphed, a, pop }: { id: string; morph?: { from: string; to: string }; onMorphed: () => void; a: Acts; pop: () => void }) {
  const { t, lang } = a;
  const d = useGet<ItemDetail>(a.port, `/item/${encodeURIComponent(id)}`, a.rev), [moving, setMoving] = useState(false), [gone, setGone] = useState(false);
  const it = d.data;
  if (d.error && !it) return <p className="pg-sec muted">{t(['That line is gone. It may have been taken back.', '这一条已经不在了，可能被撤回了。'])}</p>;
  if (!it) return <p className="pg-sec muted">{t(['Loading…', '正在读…'])}</p>;
  const remove = async () => { setGone(true); const r = await a.run<{ version: string }>('/item/delete', { id }); if (r) { a.toast(t(['Deleted', '删掉了']), r.version); pop(); } else setGone(false); };
  const move = async (section: string) => {
    setMoving(false);
    const r = await a.run<{ version: string }>('/item/edit', { id, text: it.text, section });
    if (r) a.toast(t([`Moved to ${sectionName(lang, section)} · yours now, the night leaves it`, `挪到「${section}」了 · 夜里整理不会再动它`]), r.version);
  };
  const edits = it.edits.at(-1);
  return <Fold gone={gone}><div className="mem-item">
    {it.reminder && <div className="mem-remind" role="note"><p>{t(['Overnight she thought this might be out of date. You set it yourself, so it stayed.', '夜里觉得这条可能过时了。这条是你定的，所以没动。'])}</p>
      <div className="mem-acts"><button className="mem-act is-ok" onClick={() => void remove()}>{t(['Yes, drop it', '对，删掉']) }</button>
        <button className="mem-act" onClick={() => void a.run('/item/confirm', { id })}>{t(['Keep it', '留着'])}</button></div></div>}
    <section className="pg-sec mem-big">
      {morph ? <MorphText className="mem-big-t" from={morph.from} to={morph.to} onDone={onMorphed}/> : <p className="mem-big-t">{it.text}</p>}
      <p className="mem-where">{sectionName(lang, it.section)} · {t([`#${it.number}`, `第 ${it.number} 条`])} · {t([`${it.chars} chars`, `${it.chars} 字`])}{it.pinned && <em className="mem-tag is-mine"><PushPin size={9} weight="fill"/>{t(['Yours', '你改的'])}</em>}</p>
    </section>
    <section className="mem-sec" aria-label={t(['Where it came from', '出自这几句'])}>
      <div className="mem-sh"><h4>{t(['Where it came from', '出自这几句'])}</h4>{it.sources.length > 0 && <small>{t([`${it.sources.length} line${it.sources.length > 1 ? 's' : ''}`, `${it.sources.length} 句`])}</small>}</div>
      {it.sources.length ? <div className="mem-srcs">{it.sources.map(s => <SourceCard key={s.id} s={s} a={a}/>)}</div>
        : <p className="mem-empty">{t(['No source line: you asked her to keep it, or it came from the old notes.', '没有出处：是你让她记的，或者从旧记录搬来的。'])}</p>}
    </section>
    <p className="mem-tl">{it.born && <span>{whenText(lang, it.born, 'born')}</span>}
      {edits && <button onClick={() => a.tab('changes')}>{whenText(lang, edits, 'edit')}{it.edit_count > 1 ? t([` and ${it.edit_count - 1} more`, ` 等 ${it.edit_count} 次`]) : ''} · {t(['see changes ›', '看改动 ›'])}</button>}</p>
    <div className="mem-foot">
      {moving ? <div className="mem-move"><div className="mem-secpick" role="group" aria-label={t(['Move to', '挪到'])}>{SECTION_ORDER.map(s => <button key={s} className="mem-chip is-small" aria-pressed={s === it.section} onClick={() => s === it.section ? setMoving(false) : void move(s)}>{sectionName(lang, s)}</button>)}</div>
        <button className="mem-act" onClick={() => setMoving(false)}>{t(['Never mind', '不挪了'])}</button></div>
      : <><div className="mem-bar3">
        <button className="mem-act" onClick={() => a.push({ k: 'edit', id })}><PencilSimple size={12} weight="bold"/>{t(['Edit', '改'])}</button>
        <button className="mem-act" onClick={() => setMoving(true)}>{t(['Move to…', '挪到别栏'])}</button>
        <button className="mem-act is-del" onClick={() => void remove()}><Trash size={12} weight="bold"/>{t(['Delete', '删'])}</button></div>
        <p className="mem-note">{t(['A deleted line can be taken back from Changes.', '删了也能在「改动」里撤回。'])}</p></>}
    </div>
  </div></Fold>;
}
const SECTION_ORDER = Object.keys(SECTION_EN);

// ——— 3 · one note edited ———
function EditView({ id, a, done, cancel }: { id: string; a: Acts; done: (id: string, morph?: { from: string; to: string }) => void; cancel: () => void }) {
  const { t, lang } = a;
  const d = useGet<ItemDetail>(a.port, `/item/${encodeURIComponent(id)}`, 0), it = d.data;
  const [text, setText] = useState<string | null>(null), [section, setSection] = useState<string | null>(null), box = useRef<HTMLTextAreaElement>(null);
  const value = text ?? it?.text ?? '', where = section ?? it?.section ?? '';
  useEffect(() => { if (it && box.current) { grabFocus(box.current); const n = box.current.value.length; box.current.setSelectionRange(n, n); } }, [!!it]);
  useLayoutEffect(() => { const el = box.current; if (el) { el.style.height = 'auto'; el.style.height = `${el.scrollHeight}px`; } }, [value, !!it]);
  if (d.error && !it) return <p className="pg-sec muted">{t(['That line is gone. It may have been taken back.', '这一条已经不在了，可能被撤回了。'])}</p>;
  if (!it) return <p className="pg-sec muted">{t(['Loading…', '正在读…'])}</p>;
  const clean = value.split(/\s+/).filter(Boolean).join(' '), over = clean.length > ITEM_MAX, ok = clean !== '' && !over && !a.busy;
  const save = async () => {
    if (!ok) return;
    const r = await a.run<{ version: string }>('/item/edit', { id, text: clean, section: where });
    if (!r) return;
    a.toast(t(['Saved · she won’t change this one overnight', '存好了 · 夜里整理不会再改它']), r.version);
    done(id, clean !== it.text ? { from: it.text, to: clean } : undefined);
  };
  return <div className="mem-edit">
    <section className="pg-sec mem-field"><div className="mem-sh"><label htmlFor="mem-text"><h4>{t(['Write it as', '写成'])}</h4></label><small className={`mono ${over ? 'is-warm' : ''}`}>{clean.length} / {ITEM_MAX}</small></div>
      <textarea id="mem-text" ref={box} className="mem-ed" rows={3} value={value} aria-invalid={over} onPointerDown={focusWindow} onChange={e => setText(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'Escape') { e.stopPropagation(); cancel(); }
          else if (e.key === 'Enter' && e.metaKey) { e.preventDefault(); void save(); }
          else if (e.key === 'Enter' && e.nativeEvent.isComposing) e.preventDefault();
        }}/></section>
    <section className="pg-sec mem-field"><div className="mem-sh"><h4>{t(['Which section', '放在哪栏'])}</h4></div>
      <div className="mem-secpick" role="group" aria-label={t(['Section', '栏目'])}>{SECTION_ORDER.map(s => <button key={s} className="mem-chip is-small" aria-pressed={s === where} onClick={() => setSection(s)}>{sectionName(lang, s)}</button>)}</div></section>
    <p className="mem-lock"><PushPin size={12} weight="fill"/>{t(['Once saved it is pinned: the night won’t change it, and if it goes out of date she only reminds you.', '存了以后钉住：夜里整理不会改它，过时了也只会提醒你。'])}</p>
    <section className="mem-sec"><div className="mem-sh"><h4>{t(['It was', '原来是'])}</h4></div><p className="mem-was is-block">{it.text}<span> · {t(['in', '在'])} {sectionName(lang, it.section)}</span></p></section>
    <div className="mem-foot is-row"><button className="mem-act is-wide" onClick={cancel}>{t(['Cancel', '不改了'])}</button>
      <button className="mem-act is-ok is-wide" disabled={!ok} onClick={() => void save()}>{t(['Save', '存'])}<kbd>⌘↵</kbd></button></div>
  </div>;
}

// ——— 4 · search ———
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
function Marked({ text, words }: { text: string; words: string[] }) {
  const parts = useMemo(() => words.length ? text.split(new RegExp(`(${words.map(esc).join('|')})`, 'gi')) : [text], [text, words]);
  return <>{parts.map((p, i) => i % 2 ? <mark key={i}>{p}</mark> : <Fragment key={i}>{p}</Fragment>)}</>;
}
const WHO: [Who, L][] = [['all', ['All', '全部']], ['user', ['You', '你说的']], ['jarvis', ['Jarvis', '她说的']]];
function Search({ q, who, setWho, a }: { q: string; who: Who; setWho: (who: Who) => void; a: Acts }) {
  const { t, lang } = a;
  const r = useGet<Found>(a.port, `/search?q=${encodeURIComponent(q)}&who=${who}`, 0), found = r.data;
  const since = found?.since ? new Date(found.since) : null;
  return <>
    <div className="mem-chips" role="group" aria-label={t(['Who said it', '谁说的'])}>
      {WHO.map(([id, name]) => <button key={id} className="mem-chip" aria-pressed={who === id} onClick={() => setWho(id)}>{t(name)}</button>)}
      {found && <span className="mem-count" aria-live="polite">{found.total ? t([`${found.total} lines · ${found.days.length} day${found.days.length > 1 ? 's' : ''}`, `${found.total} 句 · ${found.days.length} 天`]) : ''}</span>}
    </div>
    <div className={`mem-results ${r.loading ? 'is-loading' : ''}`} aria-busy={r.loading}>
      {!found ? <p className="mem-empty">{r.error ? t(['Search didn’t answer. Try again.', '搜索没有回应，再试一次。']) : t(['Searching…', '在找…'])}</p>
        : !found.days.length ? <p className="mem-empty">{t([`Nothing says “${q}”${since ? ` since ${since.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}` : ''}.`, `没有找到「${q}」${since ? `（从 ${since.getMonth() + 1} 月 ${since.getDate()} 日起）` : ''}。`])}</p>
        : found.days.map(day => <section className="mem-day" key={day.day}>
          <div className="mem-hday"><span>{dayLabel(day.day, lang)}</span><button onClick={() => a.push({ k: 'convo', day: day.day })}>{t(['That day ›', '那天的对话 ›'])}</button></div>
          {day.hits.map(h => <button className="mem-hit" key={h.id} onClick={() => a.push({ k: 'convo', day: day.day, around: h.id })}>
            <time>{hhmm(new Date(h.ts))}</time><span className={`mem-who ${h.who === 'user' ? 'is-you' : ''}`}>{h.who === 'user' ? t(['You', '你']) : t(['Jarvis', '她'])}</span><span><Marked text={h.text} words={found.words}/></span></button>)}
        </section>)}
      {found && since && <p className="mem-note">{found.truncated ? t(['Showing the newest 200. ', '只显示最新的 200 句。']) : ''}{t([`Every line since ${since.toLocaleDateString('en-US', { month: 'long', day: 'numeric' })} is searchable here.`, `从 ${since.getMonth() + 1} 月 ${since.getDate()} 日起的每一句都搜得到。`])}</p>}
    </div>
  </>;
}

// ——— that day's conversation ———
function Convo({ day, around, a }: { day: string; around?: string; a: Acts }) {
  const { t, lang } = a;
  const [pages, setPages] = useState<DayRecords | null>(null), [err, setErr] = useState(false), [more, setMore] = useState(false), box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const ctl = new AbortController();
    api<DayRecords>(a.port, `/day/${day}/records?limit=120${around ? `&around=${encodeURIComponent(around)}` : ''}`, undefined, ctl.signal).then(setPages, () => setErr(true));
    return () => ctl.abort();
  }, [day, around]);
  useEffect(() => {
    const hit = around ? box.current?.querySelector<HTMLElement>(`[data-id="${around}"]`) : null;
    hit?.scrollIntoView({ block: 'center' });
    hit?.animate([{ background: 'rgb(var(--glow) / .28)' }, { background: 'transparent' }], { duration: dur(1400), easing: 'ease-out' });
  }, [!!pages]);
  const page = async (dir: -1 | 1) => {
    if (!pages || more) return;
    setMore(true);
    try {
      const offset = dir < 0 ? Math.max(0, pages.offset - 120) : pages.offset + pages.records.length;
      const next = await api<DayRecords>(a.port, `/day/${day}/records?limit=${dir < 0 ? pages.offset - offset : 120}&offset=${offset}`);
      setPages({ ...next, offset: dir < 0 ? next.offset : pages.offset, records: dir < 0 ? [...next.records, ...pages.records] : [...pages.records, ...next.records] });
    } catch { a.toast(t(['That didn’t load. Try again.', '没读出来，请再试一次。'])); }
    finally { setMore(false); }
  };
  if (err) return <p className="pg-sec muted">{t(['Can’t read that day yet.', '这一天暂时读不出来。'])}</p>;
  if (!pages) return <p className="pg-sec muted">{t(['Loading that day…', '正在读那天的对话…'])}</p>;
  return <div className="mem-convo" ref={box}>
    <div className="mem-sh"><h4>{dayLabel(day, lang)}</h4><small>{t([`${pages.total} lines`, `${pages.total} 句`])}</small></div>
    {pages.offset > 0 && <button className="mem-more" disabled={more} onClick={() => void page(-1)}>{t(['Earlier ↑', '更早 ↑'])}</button>}
    {pages.records.map(r => <div className="mem-line" key={r.id} data-id={r.id}><time>{hhmm(new Date(r.ts))}</time><span className={`mem-who ${r.who === 'user' ? 'is-you' : ''}`}>{r.who === 'user' ? t(['You', '你']) : t(['Jarvis', '她'])}</span><span>{r.text}</span></div>)}
    {pages.offset + pages.records.length < pages.total && <button className="mem-more" disabled={more} onClick={() => void page(1)}>{t(['Later ↓', '更晚 ↓'])}</button>}
    {!pages.total && <p className="mem-empty">{t(['Nothing was said that day.', '那天没有对话。'])}</p>}
  </div>;
}

// A textarea that is always as tall as its text.
function Grow(props: ComponentProps<'textarea'>) {
  const box = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => { const el = box.current; if (el) { el.style.height = 'auto'; el.style.height = `${el.scrollHeight}px`; } }, [props.value]);
  return <textarea rows={1} {...props} ref={box}/>;
}

// ——— 5 · day summaries ———
// The first clause of the first topic names the day: up to the first colon or full stop, short.
const headline = (line: string) => { const head = line.split(/[：:；;。]/)[0]!.trim() || line; return head.length > 26 ? `${head.slice(0, 25)}…` : head; };
function Days({ a }: { a: Acts }) {
  const { t, lang } = a;
  const r = useGet<{ days: Card[] }>(a.port, '/days', a.rev), [open, setOpen] = useState<string | null>(null);
  if (!r.data) return <p className="mem-empty">{r.error ? t(['Couldn’t read the days.', '日摘要读不出来。']) : t(['Reading the days…', '正在读日摘要…'])}</p>;
  if (!r.data.days.length) return <p className="mem-empty">{t(['No day has been written up yet. The first one lands at 05:00.', '还没有日摘要，第一份在早上 5 点写。'])}</p>;
  return <div className="mem-cards">{r.data.days.map(c => <DayCard key={c.day} c={c} open={open === c.day} toggle={() => setOpen(o => o === c.day ? null : c.day)} a={a}/>)}</div>;
}
function DayCard({ c, open, toggle, a }: { c: Card; open: boolean; toggle: () => void; a: Acts }) {
  const { t, lang } = a;
  const word = c.kind === 'verbatim', first = c.lines[0] ?? '', [seen, setSeen] = useState(false);
  useEffect(() => { if (open) setSeen(true); }, [open]);
  const k = c.kind === 'verbatim' ? t(['kept as said', '存的原话']) : c.kind === 'user' ? t(['you edited it', '你改过']) : t([`written ${hhmm(new Date(c.ts))}`, `${hhmm(new Date(c.ts))} 写的`]);
  return <article className="mem-card" data-open={open ? '' : undefined} data-day={c.day}>
    <button className="mem-card-h" aria-expanded={open} onClick={toggle}>
      <span className="mem-card-top"><time>{dayLabel(c.day, lang)}</time><span className="mem-card-t">{word ? t([`${c.records} line${c.records > 1 ? 's' : ''}`, `${c.records} 句话`]) : headline(first)}</span><small>{k}</small></span>
      <span className="mem-card-s">{word ? c.lines.map(l => `「${l}」`).join(' ') : c.lines.join(t(['; ', '；']))}</span>
    </button>
    <Fold open={open}>{(open || seen) && <DayBody c={c} a={a}/>}</Fold>
  </article>;
}
function DayBody({ c, a }: { c: Card; a: Acts }) {
  const { t } = a;
  const d = useGet<DayDetail>(a.port, `/day/${c.day}`, a.rev), [editing, setEditing] = useState(false), [draft, setDraft] = useState<Record<string, string>>({});
  const body = d.data;
  const begin = () => { setDraft(Object.fromEntries(DAY_HEADS.map(([key]) => [key, (body?.sections?.[key] ?? []).join('\n')]))); setEditing(true); };
  const save = async () => {
    const r = await a.run('/day/edit', { day: c.day, sections: Object.fromEntries(DAY_HEADS.map(([key]) => [key, (draft[key] ?? '').split('\n')])) });
    if (r) { setEditing(false); a.toast(t(['Saved · she won’t rewrite this day', '存好了 · 夜里不会重写这一天'])); }
  };
  if (!body) return <p className="mem-empty">{d.error ? t(['Couldn’t read this day.', '这一天读不出来。']) : t(['Reading…', '正在读…'])}</p>;
  return <div className="mem-day-b">
    {body.lines ? <ul className="mem-lines">{body.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>
      : DAY_HEADS.map(([key, name]) => <div className="mem-dsec" key={key}><h5>{t(name)}</h5>
        {editing ? <Grow className="mem-ed" aria-label={t(name)} value={draft[key] ?? ''} onPointerDown={focusWindow} onChange={e => setDraft(v => ({ ...v, [key]: e.target.value }))}
          onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); setEditing(false); } else if (e.key === 'Enter' && e.metaKey) { e.preventDefault(); void save(); } }}/>
          : body.sections![key].length ? <ul>{body.sections![key].map((l, i) => <li key={i}>{l}</li>)}</ul> : <p className="mem-empty is-tight">{t(['none', '无'])}</p>}</div>)}
    <div className="mem-dfoot">
      {editing ? <><button className="mem-act" onClick={() => setEditing(false)}>{t(['Cancel', '不改了'])}</button><button className="mem-act is-ok" disabled={a.busy} onClick={() => void save()}>{t(['Save', '存'])}<kbd>⌘↵</kbd></button></>
        : <>{!body.lines && <button className="mem-act" onClick={begin}><PencilSimple size={12} weight="bold"/>{t(['Edit', '改'])}</button>}
          <button className="mem-act" onClick={() => a.push({ k: 'convo', day: c.day })}>{t(['That day, word for word ›', '那天的原话 ›'])}</button>
          <small>{t([`${body.records} lines`, `${body.records} 句`])}</small></>}
    </div>
    {editing && <p className="mem-note">{t(['One line per bullet. Once saved this day is yours: she won’t write it again.', '一行一条。存了以后这一天归你，夜里不会再重写。'])}</p>}
  </div>;
}

// ——— 6 · changes ———
function title(v: Version, t: (l: L) => string): string {
  const n = (c: number, en: string, zh: string) => c ? [t([`${en} ${c}`, `${zh} ${c} 条`])] : [];
  if (v.kind === 'nightly') return v.day ? t([`Night pass · ${md(v.day)}`, `夜里整理 ${md(v.day)}`]) : t(['Night pass', '夜里整理']);
  if (v.kind === 'migration') return t(['First version, from the old notes', '第一版 · 从旧记录搬来']);
  if (v.kind === 'setup') return t(['First-run setup', '首次设置']);
  if (v.kind === 'remember') return t(['You asked her to remember', '你让她记了']);
  if (v.kind === 'confirm') return t(['You confirmed a line', '你确认了一条']);
  if (v.kind === 'undo') return t(['You took a change back', '你撤回了一次改动']);
  const parts = [...n(v.edited, 'edited', '改了'), ...n(v.moved, 'moved', '挪了'), ...(v.edited || v.moved ? [] : n(v.counts.chg, 'changed', '改了')), ...n(v.counts.old, 'deleted', '删了'), ...(v.kind === 'user' ? n(v.counts.add, 'kept', '留着了') : [])];
  return parts.length ? t(['You ', '你']) + parts.join(t([', ', '，'])) : t(['You changed something', '你改了一处']);
}
const LINE_TAG: Record<'add' | 'chg' | 'old', [string, L]> = { add: ['add', ['New', '新']], chg: ['chg', ['Edited', '改']], old: ['old', ['Gone', '过时']] };

function Changes({ o, a }: { o: MemoryOverview; a: Acts }) {
  const { t, lang } = a;
  const r = useGet<{ versions: Version[]; total: number }>(a.port, '/versions', a.rev), [pending, setPending] = useState<string | null>(null);
  return <>
    {!r.data ? <p className="mem-empty">{r.error ? t(['Couldn’t read the changes.', '改动读不出来。']) : t(['Reading the changes…', '正在读改动…'])}</p>
      : <ol className="mem-vers">{r.data.versions.map(v => <li className="mem-ver" key={v.id} data-kind={v.kind} data-now={v.current ? '' : undefined}>
        <i className={`mem-dot ${v.origin === 'user' ? 'is-you' : ''}`}/>
        <div className="mem-ver-b"><span className="mem-ver-t">{title(v, t)}{v.current && <em className="mem-tag is-now">{t(['Now', '现在'])}</em>}</span>
          <span className="mem-ver-s"><time>{stamp(v.ts, lang)}</time></span>
          {v.lines.length > 0 && <div className="mem-ver-l">{v.lines.map((l, i) => <div className="mem-vl" key={i}><span className={`mem-tag is-${l.tag}`}>{t(LINE_TAG[l.tag][1])}</span><span>{l.text}</span></div>)}
            {v.more > 0 && <small>{t([`and ${v.more} more`, `还有 ${v.more} 条`])}</small>}</div>}</div>
        {v.undoable && <button className="mem-act" disabled={pending !== null || a.busy} aria-label={`${t(['Undo', '撤回'])}: ${title(v, t)}`} onClick={async () => { setPending(v.id); try { await a.undo(v.id); } finally { setPending(null); } }}><ArrowCounterClockwise size={11} weight="bold"/>{t(['Undo', '撤回'])}</button>}
      </li>)}</ol>}
    <Cap o={o} a={a}/>
  </>;
}

function Cap({ o, a }: { o: MemoryOverview; a: Acts }) {
  const { t } = a;
  const [editing, setEditing] = useState(false), [value, setValue] = useState(String(o.max_chars));
  const n = Number(value.replace(/[,\s]/g, '')), valid = Number.isInteger(n) && n >= 1000 && n <= 20000;
  const pct = Math.min(100, o.chars / o.max_chars * 100), fmt = (x: number) => x.toLocaleString('en-US');
  const save = async () => {
    if (!valid) return;
    if (await a.run('/cap', { max_chars: n })) { setEditing(false); a.toast(t(['Limit saved · it applies after Jarvis restarts', '上限存好了 · Jarvis 重启后生效'])); }
  };
  return <div className="mem-meter">
    <div className="mem-meter-r"><span>{t([`Holding ${fmt(o.chars)} characters`, `记着 ${fmt(o.chars)} 字`])}</span>
      {editing ? <span className="mem-cap-ed"><input aria-label={t(['Character limit', '字数上限'])} inputMode="numeric" value={value} ref={el => { if (el && document.activeElement !== el && !el.dataset.f) { el.dataset.f = '1'; grabFocus(el); } }}
          onPointerDown={focusWindow} onChange={e => setValue(e.target.value)}
          onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); setEditing(false); } else if (e.key === 'Enter') { e.preventDefault(); void save(); } }}/>
        <button className="mem-act is-ok" disabled={!valid} onClick={() => void save()}>{t(['Save', '存'])}</button></span>
        : <button className="mem-cap" onClick={() => { setValue(String(o.max_chars)); setEditing(true); }} aria-label={t(['Change the limit', '改上限'])}>{t(['limit', '上限'])} <b>{fmt(o.max_chars)}</b><CaretRight size={10} weight="bold"/></button>}</div>
    <div className="mem-bar" role="progressbar" aria-valuemin={0} aria-valuemax={o.max_chars} aria-valuenow={o.chars} aria-label={t(['Used of the limit', '已用字数'])}><i data-warm={pct > 90 ? '' : undefined} style={{ width: `${pct}%` }}/></div>
    {editing && <small>{t(['1,000 to 20,000. The night’s check uses it from the next start.', '1,000 到 20,000。夜里整理从下次启动起按它来。'])}</small>}
    {!editing && o.max_chars !== o.booted_max_chars && <small>{t([`Applies after a restart (now ${fmt(o.booted_max_chars)})`, `重启后生效（现在是 ${fmt(o.booted_max_chars)}）`])}</small>}
    <small>{t(['Tidied every night at 05:00 · every line has its source', '每晚 05:00 整理 · 每条都带出处'])}</small>
  </div>;
}

