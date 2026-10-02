import { Fragment, type ReactNode } from 'react';

// The markdown Jarvis's answers actually use (memory.db, 2026-09-25): headings, bold, code ticks, lists (some nested), tables; links since they began to carry sources.
// Built as React elements, never innerHTML, so nothing in an answer can inject markup into the window.
// ponytail: no italics or quotes; none appear in the answers yet. Add them when they do.
// The one way a link opens: in the browser, through the shell, never by navigating this window.
export const openLink = (url: string) => { void window.jarvis?.openUrl?.(url); };
export const Lk = ({ url, children }: { url: string; children: ReactNode }) => <a className="lk" href={url} onClick={e => { e.preventDefault(); openLink(url); }}>{children}</a>;
// A bare address reads short: no scheme, no www., no trailing slash, cut at 32.
const short = (url: string) => { const s = url.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, ''); return s.length > 32 ? `${s.slice(0, 31)}…` : s; };
// `spell`: how plain text is set (the talk area lights it a character at a time); code and links stay whole.
// A bare address ends on a character that is not punctuation, and stops at the first space, bracket or CJK character.
export const inline = (text: string, spell?: (text: string) => ReactNode): ReactNode[] => text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^\s)]+\)|https?:\/\/[^\s<>()"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]*[^\s<>()"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef.,;:!?])/).map((part, i) => {
  if (i % 2 === 0) return spell ? <Fragment key={i}>{spell(part)}</Fragment> : part;
  if (part[0] === '`') return <code key={i}>{part.slice(1, -1)}</code>;
  if (part[0] === '[') { const at = part.indexOf(']('); return <Lk key={i} url={part.slice(at + 2, -1)}>{part.slice(1, at)}</Lk>; }
  if (part[0] === 'h') return <Lk key={i} url={part}>{short(part)}</Lk>;
  return <strong key={i}>{inline(part.slice(2, -2), spell)}</strong>;
});

type Item = { indent: number; start: number | null; text: string };
const ITEM = /^(\s*)(?:[-*•]|(\d+)[.)])\s+(.*)$/;
const nest = (items: Item[]): ReactNode => {
  const out: ReactNode[] = [];
  for (let i = 0; i < items.length;) {
    let j = i + 1;
    while (j < items.length && items[j].indent > items[i].indent) j++;
    out.push(<li key={i}>{inline(items[i].text)}{j > i + 1 && nest(items.slice(i + 1, j))}</li>);
    i = j;
  }
  return items[0].start === null ? <ul>{out}</ul> : <ol start={items[0].start}>{out}</ol>;
};
const cells = (line: string) => line.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());

export function Markdown({ text, spell }: { text: string; spell?: (text: string) => ReactNode }) {
  const lines = text.split('\n'), blocks: ReactNode[] = [];
  for (let i = 0; i < lines.length;) {
    const line = lines[i], key = blocks.length;
    if (!line.trim()) { i++; continue; }
    if (line.trimStart().startsWith('```')) {
      const body: string[] = [];
      for (i++; i < lines.length && !lines[i].trimStart().startsWith('```'); i++) body.push(lines[i]);
      i++; blocks.push(<pre key={key}>{body.join('\n')}</pre>);
    } else if (/^#{1,6}\s/.test(line)) {
      blocks.push(<h5 key={key}>{inline(line.replace(/^#+\s+/, ''))}</h5>); i++;
    } else if (line.trimStart().startsWith('|')) {
      const rows: string[][] = [];
      for (; i < lines.length && lines[i].trimStart().startsWith('|'); i++) if (!/^[\s|:-]+$/.test(lines[i])) rows.push(cells(lines[i]));
      const [head = [], ...body] = rows;
      blocks.push(<div className="md-table" key={key}><table>
        <thead><tr>{head.map((c, n) => <th key={n}>{inline(c)}</th>)}</tr></thead>
        <tbody>{body.map((r, m) => <tr key={m}>{r.map((c, n) => <td key={n}>{inline(c)}</td>)}</tr>)}</tbody>
      </table></div>);
    } else if (ITEM.test(line)) {
      const items: Item[] = [];
      for (; i < lines.length; i++) {
        const m = ITEM.exec(lines[i]);
        if (m) items.push({ indent: m[1].length, start: m[2] ? Number(m[2]) : null, text: m[3] });
        else if (/^\s+\S/.test(lines[i])) items[items.length - 1].text += `\n${lines[i].trim()}`; // an item's wrapped line
        else break;
      }
      blocks.push(<div key={key}>{nest(items)}</div>);
    } else {
      const para: string[] = [];
      for (; i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*\||\s*```)/.test(lines[i]) && !ITEM.test(lines[i]); i++) para.push(lines[i]);
      blocks.push(<p key={key}>{inline(para.join('\n'), spell)}</p>);
    }
  }
  return <div className="md">{blocks}</div>;
}
