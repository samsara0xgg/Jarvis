import type { Lang } from './companionSettings';

// How long a limit has left before it resets, by the whole minute rounded up; null when the time is unknown.
function left(iso: string | null | undefined, now: Date, lang: Lang): { text: string; due: boolean } | null {
  if (!iso) return null;
  const reset = new Date(iso).getTime();
  if (!Number.isFinite(reset)) return null;
  const minutes = Math.ceil((reset - now.getTime()) / 60_000);
  if (minutes <= 0) return { text: lang === 'zh' ? '正在重置' : 'resetting', due: true };
  const hours = Math.floor(minutes / 60), days = Math.floor(hours / 24);
  return { due: false, text: lang === 'zh'
    ? hours >= 24 ? `${days} 天 ${hours % 24} 小时` : hours > 0 ? `${hours} 小时 ${minutes % 60} 分` : `${minutes} 分钟`
    : hours >= 24 ? `${days}d ${hours % 24}h` : hours > 0 ? `${hours}h ${minutes % 60}m` : `${minutes}m` };
}

// "3h 42m": the countdown on its own, for the small rings on the home page.
export const fmtLeft = (iso: string | null | undefined, now = new Date(), lang: Lang = 'en') => left(iso, now, lang)?.text ?? '—';

// "resets in 3h 42m": the countdown as a sentence.
export function fmtReset(iso: string | null | undefined, now = new Date(), lang: Lang = 'en'): string {
  const l = left(iso, now, lang);
  return !l ? '—' : l.due ? l.text : lang === 'zh' ? `${l.text}后重置` : `resets in ${l.text}`;
}
