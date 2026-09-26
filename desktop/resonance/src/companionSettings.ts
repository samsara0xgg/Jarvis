import { useEffect, useState } from 'react';

// Settings that belong to her and live in this companion's own profile: the language of her panel,
// how the Dashboard opens, which screen she lives on, how the home is arranged, and which Agents it shows.
// Jarvis's own settings (voice, models, data) are the daemon's, on /inherent/settings.
export type Lang = 'en' | 'zh';
export type L = readonly [string, string]; // [English, 中文]
export const tr = (lang: Lang, l: L) => l[lang === 'zh' ? 1 : 0];

// The home's blocks in their default order. Pop-ups show up only when there is something; the rest always.
export type BlockId = 'talk' | 'foryou' | 'brief' | 'today' | 'mail' | 'agents' | 'now' | 'usage' | 'tiles';
export const BLOCKS: BlockId[] = ['talk', 'foryou', 'brief', 'today', 'mail', 'agents', 'now', 'usage', 'tiles'];
export const POPS: BlockId[] = ['talk', 'foryou', 'brief', 'mail'];
export const isPop = (id: BlockId) => POPS.includes(id);

export type TalkMode = 'after' | 'always' | 'never';
export type Stale = 'hour' | 'day' | 'never';
export const defaultSettings = {
  lang: 'en' as Lang,
  openBy: 'both' as 'both' | 'click' | 'hover',
  screen: 'follow' as 'follow' | 'main',
  order: BLOCKS, hidden: [] as BlockId[],
  talk: 'after' as TalkMode, foryou: true, brief: true, mail: true, forecast: true,
  claude: true, codex: true, stale: 'day' as Stale,
};
export type CompanionSettings = typeof defaultSettings;
export const HOME_DEFAULTS: Partial<CompanionSettings> = { order: BLOCKS, hidden: [], talk: 'after', foryou: true, brief: true, mail: true, forecast: true };
const KEY = 'companion-settings-v1';
const one = <T extends string>(value: unknown, options: readonly T[], fallback: T) => options.includes(value as T) ? value as T : fallback;
const flag = (value: unknown, fallback: boolean) => typeof value === 'boolean' ? value : fallback;

function load(): CompanionSettings {
  const d = defaultSettings;
  try {
    const v = JSON.parse(localStorage.getItem(KEY) ?? '{}') ?? {};
    // A saved order keeps the blocks it knows; blocks added since land in their default place.
    const saved: BlockId[] = Array.isArray(v.order) ? v.order.filter((id: unknown, i: number, all: unknown[]) => BLOCKS.includes(id as BlockId) && all.indexOf(id) === i) : [];
    const order = [...saved];
    BLOCKS.forEach((id, i) => { if (!order.includes(id)) order.splice(Math.min(i, order.length), 0, id); });
    return {
      lang: one(v.lang, ['en', 'zh'], d.lang), openBy: one(v.openBy, ['both', 'click', 'hover'], d.openBy), screen: one(v.screen, ['follow', 'main'], d.screen),
      order, hidden: Array.isArray(v.hidden) ? v.hidden.filter((id: unknown) => BLOCKS.includes(id as BlockId) && !isPop(id as BlockId)) : [],
      talk: one(v.talk, ['after', 'always', 'never'], d.talk), foryou: flag(v.foryou, d.foryou), brief: flag(v.brief, d.brief), mail: flag(v.mail, d.mail), forecast: flag(v.forecast, d.forecast),
      claude: flag(v.claude, d.claude), codex: flag(v.codex, d.codex), stale: one(v.stale, ['hour', 'day', 'never'], d.stale),
    };
  } catch { return d; }
}

// The panel's words in the chosen language, for components that are not handed `lang`.
export function useT() { const [s] = useCompanionSettings(); return (l: L) => tr(s.lang, l); }

// Every hook instance in the window shares one value: a change anywhere reaches the others at once.
let current: CompanionSettings | null = null;
const update = (change: Partial<CompanionSettings>) => {
  current = { ...(current ??= load()), ...change };
  try { localStorage.setItem(KEY, JSON.stringify(current)); } catch { /* kept for this run only */ }
  window.dispatchEvent(new CustomEvent(KEY, { detail: current }));
};
export function useCompanionSettings() {
  const [settings, setSettings] = useState(() => current ??= load());
  useEffect(() => {
    const receive = (event: Event) => setSettings((event as CustomEvent<CompanionSettings>).detail);
    window.addEventListener(KEY, receive);
    return () => window.removeEventListener(KEY, receive);
  }, []);
  return [settings, update] as const;
}
