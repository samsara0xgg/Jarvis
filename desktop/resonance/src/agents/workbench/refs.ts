// What an answer points at, for the workbench (ADR 0085-0087): web addresses, markdown links and file paths become
// things to open. Pages, artifacts, PDFs and images want width and open on the stage; documents, code and diffs open
// beside the conversation.
import type { Item, Step } from '../../../electron/agents/types';
import { plural, tr } from '../lang';

export type Kind = 'art' | 'web' | 'pdf' | 'img' | 'file';
export type Ref = { key: string; ref: string; kind: Kind; target: 'side' | 'stage'; label: string; url: boolean };
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const IMG = /^(png|jpe?g|gif|webp|avif|svg|bmp|ico)$/;
const URL_RE = /https?:\/\/[^\s<>()"'`　-〿＀-￯]+[^\s<>()"'`.,;:!?　-〿＀-￯]/g;
// A path in code: something/with.ext, ./x.ext, ~/x.ext, or a bare name with a document or code extension; a line
// number after a colon is allowed. Names may be in any script (docs/发布清单.pdf); a file name under a folder may hold
// spaces (design/Startrail 发布.key) and a folder's may not, so a command with a path in it stays a command.
const C = '\\p{L}\\p{N}_@+\\-', EXT = '\\.[A-Za-z][A-Za-z0-9]{0,7}', DIRS = `(~\\/|\\.{1,2}\\/|\\/)?[${C}][${C}.]*(\\/[${C}.]+)*`;
const PATH = new RegExp(`^${DIRS}${EXT}(:\\d+(:\\d+)?(-\\d+)?)?$`, 'u'), SPACED = new RegExp(`^${DIRS}\\/[${C}][${C}. ]*[${C}]${EXT}$`, 'u');
const KNOWN = /\.(md|mdx|markdown|html?|tsx?|jsx?|mjs|cjs|cts|py|json|jsonl|css|pdf|png|jpe?g|gif|svg|webp|avif|ya?ml|toml|txt|log|csv|tsv|sql|sh|swift|rs|go|rb|java|kt|mov|mp4|m4v|webm|mp3|m4a|wav|aac|flac|ogg|key|numbers|pages|docx|xlsx|pptx|zip|dmg|pkg)(:|$)/i;
export const isPath = (c: string) => !/^https?:/i.test(c) && (PATH.test(c) || SPACED.test(c)) && (c.includes('/') || KNOWN.test(c));
export const stripLine = (p: string) => p.replace(/(#L\d.*|:\d+(:\d+)?(-\d+)?)$/, '');

export function classify(ref: string, label = ''): Ref {
  const url = /^https?:\/\//i.test(ref);
  let pathPart = stripLine(ref), host = '';
  if (url) { try { const u = new URL(ref); pathPart = u.pathname; host = u.host; } catch { /* keep it as written */ } }
  const ext = /\.([a-z0-9]+)$/i.exec(pathPart)?.[1]?.toLowerCase() ?? '';
  const kind: Kind = url && /(^|\.)claude\.ai$/.test(host) && /\/(artifact|public\/artifacts)\//.test(pathPart) ? 'art'
    : ext === 'pdf' ? 'pdf' : IMG.test(ext) ? 'img' : url ? 'web' : 'file';
  return { key: url ? ref : stripLine(ref).replace(/^\.\//, ''), ref, kind, target: kind === 'file' ? 'side' : 'stage', label, url };
}

// ---------- inline: links, addresses and paths in an answer become clickable, nothing else changes ----------
const bold = (x: string) => esc(x).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
export function inline(x: string) {
  const keep: string[] = [], hold = (h: string) => `\u0000${keep.push(h) - 1}\u0000`;
  const t = x
    .replace(/`([^`]+)`/g, (_, c: string) => hold(isPath(c) ? `<code class="ref" data-act="peek" data-ref="${esc(c)}">${esc(c)}</code>` : `<code>${esc(c)}</code>`))
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, l: string, u: string) => hold(`<a class="ref" data-act="peek" data-ref="${esc(u)}" data-label="${esc(l.replace(/\u0000\d+\u0000/g, ''))}" title="${esc(u)}">${bold(l)}</a>`))
    .replace(URL_RE, u => hold(`<a class="ref" data-act="peek" data-ref="${esc(u)}" title="${esc(u)}">${esc(u)}</a>`));
  let out = bold(t);
  for (let n = 0; n < 3 && out.includes('\u0000'); n++) out = out.replace(/\u0000(\d+)\u0000/g, (_, i: string) => keep[Number(i)]);
  return out;
}

// ---------- the cards under an answer: what it made or changed that is worth opening, at most three ----------
// Addresses first (an artifact before other pages), then files this turn edited; a path it only mentions stays a link
// in the text.
export function refsOf(text: string): { ref: string; label: string }[] {
  const out: { ref: string; label: string; at: number }[] = [];
  const noCode = text.replace(/```[\s\S]*?```/g, m => ' '.repeat(m.length));
  for (const m of noCode.matchAll(/\[([^\]]+)\]\(([^)\s]+)\)/g)) out.push({ ref: m[2], label: m[1].replace(/`/g, ''), at: m.index });
  const linked = noCode.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, m => ' '.repeat(m.length));
  for (const m of linked.matchAll(URL_RE)) out.push({ ref: m[0], label: '', at: m.index });
  for (const m of linked.matchAll(/`([^`]+)`/g)) if (isPath(m[1])) out.push({ ref: m[1], label: '', at: m.index });
  return out.sort((a, b) => a.at - b.at);
}
// The edit steps of the turn an answer ends: from the item after the last thing Allen said, up to the answer.
export function editsBefore(items: Item[], i: number) {
  const edits: Step[] = [];
  for (let k = i - 1; k >= 0 && items[k].k !== 'you'; k--) { const it = items[k]; if (it.k === 'steps') edits.push(...it.steps.filter(s => s.k === 'edit')); }
  return edits;
}
const same = (a: string, b: string) => { const x = stripLine(a).replace(/^\.\//, ''), y = b.replace(/^\.\//, ''); return x === y || y.endsWith(`/${x}`) || x.endsWith(`/${y}`); };
const shorten = (s: string, n = 34) => s.length > n ? `${s.slice(0, n - 1)}…` : s;
export function cardsHTML(text: string, edits: Step[]) {
  const seen = new Set<string>(), cards: { r: Ref; b: string; sub: string; ic: string; fi: boolean; rank: number; at: number }[] = [];
  refsOf(text).forEach(({ ref, label }, at) => {
    const r = classify(ref, label);
    if (seen.has(r.key)) return;
    if (r.url) {
      let host = '', rest = '';
      try { const u = new URL(ref); host = u.host.replace(/^www\./, ''); rest = u.pathname.replace(/\/$/, ''); } catch { host = ref; }
      const b = label || (r.kind === 'art' ? 'Artifact' : host);
      seen.add(r.key);
      cards.push({ r, b, sub: shorten(host + rest), ic: [...b.trim()][0]?.toUpperCase() ?? '·', fi: false, rank: r.kind === 'art' ? 0 : 1, at });
      return;
    }
    const mine = edits.filter(s => same(ref, s.t));
    if (!mine.length) return;
    seen.add(r.key);
    const lines = mine.reduce((n, s) => n + (s.add ?? 0) + (s.del ?? 0), 0), p = stripLine(ref), dir = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '.';
    const ext = /\.[a-z0-9]+$/i.exec(p)?.[0].toLowerCase() ?? '';
    cards.push({ r, b: label || p, sub: tr(`${dir}${lines ? ` · 改了 ${lines} 行` : ''}`, `${dir}${lines ? ` · ${plural(lines, 'line')} changed` : ''}`), ic: ext.length <= 6 ? ext : '.txt', fi: true, rank: 2, at });
  });
  cards.sort((a, b) => a.rank - b.rank || a.at - b.at);
  return cards.slice(0, 3).map(c => `<button type="button" class="lnk" data-act="peek" data-ref="${esc(c.r.ref)}" data-label="${esc(c.r.label)}"><span class="ic${c.fi ? ' fi' : ''}">${esc(c.ic)}</span><span><b>${esc(c.b)}</b><small>${esc(c.sub)}</small></span><em>${c.r.target === 'stage' ? tr('舞台', 'Stage') : tr('并排', 'Side')}</em></button>`).join('');
}
