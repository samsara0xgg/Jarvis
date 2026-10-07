import { readdirSync, readFileSync } from 'node:fs';
import { posix } from 'node:path';
import { fileURLToPath } from 'node:url';
import picks from '../../adr-picks.json';

export const ADR_DIR = fileURLToPath(new URL('../../../docs/adr/', import.meta.url));
export const GITHUB_REPO = 'https://github.com/samsara0xgg/Jarvis';
export const GITHUB_ADR_TREE = `${GITHUB_REPO}/tree/main/docs/adr`;

export interface ParsedAdr {
  number: string;
  /** The full first line without the leading "# ", e.g. "ADR 0164 — Title". */
  title: string;
  /** Title without the "ADR NNNN — " prefix. */
  shortTitle: string;
  status: string;
  date: string;
  /** Markdown body without the H1 and the Status/Date/Supersedes lines. */
  body: string;
}

export function readPicks(): string[] {
  return picks as string[];
}

/** Rewrite repo-relative markdown links to GitHub URLs so they do not 404 on the site. */
export function rewriteRelativeLinks(markdown: string): string {
  return markdown.replace(/\]\((?!https?:|mailto:|#)([^)\s]+)\)/g, (_m, target: string) => {
    const [path, hash] = target.split('#');
    const resolved = posix.normalize(posix.join('docs/adr', path ?? ''));
    return `](${GITHUB_REPO}/blob/main/${resolved}${hash ? `#${hash}` : ''})`;
  });
}

export function parseAdr(number: string, text: string): ParsedAdr {
  const lines = text.split('\n');
  const h1 = lines.findIndex((l) => /^# ADR \d+ — /.test(l));
  if (h1 === -1) throw new Error(`ADR ${number}: no "# ADR NNNN — Title" line`);
  const title = lines[h1]!.slice(2).trim();
  const shortTitle = title.replace(/^ADR \d+ — /, '');
  const meta = (key: string) =>
    lines.find((l) => l.startsWith(`**${key}:**`))?.replace(`**${key}:**`, '').trim() ?? '';
  const body = lines
    .slice(h1 + 1)
    .filter((l) => !/^\*\*(Status|Date|Supersedes):\*\*/.test(l))
    .join('\n')
    .trim();
  return {
    number,
    title,
    shortTitle,
    status: meta('Status'),
    date: meta('Date'),
    body: rewriteRelativeLinks(body),
  };
}

/** Synchronous read of the picked ADR files (used by astro.config.mjs for the sidebar). */
export function loadPickedAdrs(): ParsedAdr[] {
  const files = readdirSync(ADR_DIR);
  return readPicks().map((number) => {
    const file = files.find((f) => f.startsWith(`${number}-`) && f.endsWith('.md'));
    if (!file) throw new Error(`adr-picks.json lists ${number} but docs/adr has no such file`);
    return parseAdr(number, readFileSync(ADR_DIR + file, 'utf-8'));
  });
}
