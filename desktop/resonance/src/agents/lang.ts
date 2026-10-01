// ADR 0109: Startrail speaks Jarvis's own language. The host reads it from the daemon (`language` in settings.yaml) and
// answers it on /lang; this page asks once, before any other module draws, so a string made at load is already in the
// right language. English when it says 'en', otherwise the Chinese everything was written in. `tr('中文', 'English')` picks.
// (A check that runs a module of this page in Node has no window, no host to ask, and reads Chinese.)
const port = typeof location === 'undefined' ? '' : new URLSearchParams(location.search).get('port') ?? '8016';
export const en = port ? await fetch(`http://127.0.0.1:${port}/lang`, { signal: AbortSignal.timeout(2000) }).then(r => r.json()).then(j => j.language === 'en', () => false) : false;
export const tr = <T>(zh: T, e: T): T => en ? e : zh;
// The static words of agents.html carry their English beside them: data-en for an element's text, data-en-<attribute> for
// an attribute (data-en-aria-label, data-en-data-tip, data-en-placeholder).
if (en) {
  document.documentElement.lang = 'en';
  for (const el of document.querySelectorAll('*')) for (const { name, value } of [...el.attributes]) {
    if (name === 'data-en') el.textContent = value; else if (name.startsWith('data-en-')) el.setAttribute(name.slice(8), value);
  }
}
// The wording helpers both languages share: a 24-hour clock ("16:11") and, on other days, the date in front ("Sep 29 16:11", "9月29日 16:11").
export const hhmm = (at: number) => new Date(at).toLocaleTimeString(en ? 'en-US' : 'zh-CN', { hour: '2-digit', minute: '2-digit', ...en ? { hourCycle: 'h23' } : { hour12: false } });
export const stamp = (at: number) => {
  const d = new Date(at), t = hhmm(at);
  return d.toDateString() === new Date().toDateString() ? t : `${en ? d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : `${d.getMonth() + 1}月${d.getDate()}日`} ${t}`;
};
// English plural: "1 file", "3 files". Chinese has none, so only an English string calls it.
export const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
