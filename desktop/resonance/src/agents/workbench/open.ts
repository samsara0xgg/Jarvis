// What the preview sheet draws for a file (review 12, 13, 15), each kind the way it reads best: a picture fits the sheet
// and a click shows it 1:1; a recording has its own play, pause and scrubber; a sound is its waveform and plays only
// when asked; a PDF opens in the window's own viewer, page after page with the page it is on; markdown is rendered,
// with its source a click away; code and text have line numbers and highlight.js's colours and stop at the line an
// answer named; a log keeps its times and levels apart, a CSV its header and its numbers. Pictures, sound, video and
// PDFs come as bytes from the host, and only from the session's own folders (files.ts sendFile). Keynote, disk images
// and the like never come here: Quick Look shows them.
import hljs from 'highlight.js/lib/core';
import bash from 'highlight.js/lib/languages/bash';
import c from 'highlight.js/lib/languages/c';
import cpp from 'highlight.js/lib/languages/cpp';
import css from 'highlight.js/lib/languages/css';
import diff from 'highlight.js/lib/languages/diff';
import go from 'highlight.js/lib/languages/go';
import ini from 'highlight.js/lib/languages/ini';
import java from 'highlight.js/lib/languages/java';
import javascript from 'highlight.js/lib/languages/javascript';
import json from 'highlight.js/lib/languages/json';
import kotlin from 'highlight.js/lib/languages/kotlin';
import markdown from 'highlight.js/lib/languages/markdown';
import objectivec from 'highlight.js/lib/languages/objectivec';
import python from 'highlight.js/lib/languages/python';
import ruby from 'highlight.js/lib/languages/ruby';
import rust from 'highlight.js/lib/languages/rust';
import scss from 'highlight.js/lib/languages/scss';
import shell from 'highlight.js/lib/languages/shell';
import sql from 'highlight.js/lib/languages/sql';
import swift from 'highlight.js/lib/languages/swift';
import typescript from 'highlight.js/lib/languages/typescript';
import xml from 'highlight.js/lib/languages/xml';
import yaml from 'highlight.js/lib/languages/yaml';
import type { Diff, Peek } from '../../../electron/agents/types';
import { inline } from './refs';
import './open.css';

for (const [n, l] of Object.entries({ bash, c, cpp, css, diff, go, ini, java, javascript, json, kotlin, markdown, objectivec, python, ruby, rust, scss, shell, sql, swift, typescript, xml, yaml })) hljs.registerLanguage(n, l);
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

// Files that go to Quick Look at once: documents of other apps, archives, disk images, what Chromium cannot draw.
export const QUICKLOOK = /\.(key|numbers|pages|docx?|xlsx?|pptx?|rtf|zip|dmg|pkg|sketch|fig|psd|ai|epub|heic|tiff?|mkv|avi)$/i;
const LANG: Record<string, string> = {
  ts: 'typescript', tsx: 'typescript', mts: 'typescript', cts: 'typescript', js: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript',
  py: 'python', pyi: 'python', json: 'json', jsonl: 'json', ndjson: 'json', css: 'css', scss: 'scss', html: 'xml', htm: 'xml', xml: 'xml', svg: 'xml', plist: 'xml', vue: 'xml',
  sh: 'bash', bash: 'bash', zsh: 'bash', swift: 'swift', go: 'go', rs: 'rust', rb: 'ruby', java: 'java', kt: 'kotlin', kts: 'kotlin', c: 'c', h: 'c', cc: 'cpp', cpp: 'cpp', hpp: 'cpp',
  m: 'objectivec', mm: 'objectivec', yml: 'yaml', yaml: 'yaml', toml: 'ini', ini: 'ini', cfg: 'ini', conf: 'ini', sql: 'sql', md: 'markdown', markdown: 'markdown', mdx: 'markdown',
  diff: 'diff', patch: 'diff',
};
const extOf = (p: string) => /\.([a-z0-9]+)$/i.exec(p)?.[1]?.toLowerCase() ?? '';
export const langOf = (p: string) => LANG[extOf(p)] ?? (/(^|\/)\.?(zshrc|bashrc|zprofile|profile)$/.test(p) ? 'bash' : null);
// The mark on the head: the extension, as the file cards write one.
export const badge = (p: string) => { const e = extOf(p).toUpperCase(); return e && e.length <= 4 ? e : 'TXT'; };
export const bytes = (n: number) => n < 1024 ? `${n} B` : n < 1 << 20 ? `${Math.round(n / 1024)} KB` : n < 1 << 30 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${(n / (1 << 30)).toFixed(1)} GB`;
const clock = (t: number) => { const s = Math.max(0, Math.floor(t || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };

// ---------- code: highlight.js line by line ----------
// It gives one string, and a span can run over several lines: each line closes what is open and the next opens it again.
function perLine(html: string) {
  const out: string[] = [], open: string[] = [];
  let line = '';
  for (const m of html.matchAll(/<span[^>]*>|<\/span>|\n|[^<\n]+/g)) {
    const t = m[0];
    if (t === '\n') { out.push(line + '</span>'.repeat(open.length)); line = open.join(''); }
    else if (t === '</span>') { open.pop(); line += t; }
    else if (t.startsWith('<span')) { open.push(t); line += t; }
    else line += t;
  }
  out.push(line + '</span>'.repeat(open.length));
  return out;
}
// Big files are drawn plain: colouring a few megabytes would hold the window up.
export function hl(text: string, lang?: string | null) {
  if (lang && text.length <= 300_000 && hljs.getLanguage(lang)) { try { return perLine(hljs.highlight(text, { language: lang, ignoreIllegals: true }).value); } catch { /* drawn plain */ } }
  return text.split('\n').map(esc);
}
// A row: the line's number as the file has it now (0 for one only the base had), its text, its mark; ⋯ between hunks.
export type Row = [number, string, ' ' | '+' | '-'] | '⋯';
export function rowsHTML(rows: Row[], lang: string | null, o: { at?: number; diff?: boolean; wrap?: boolean; nums?: boolean } = {}) {
  const real = rows.filter((r): r is Exclude<Row, '⋯'> => r !== '⋯'), h = hl(real.map(r => r[1]).join('\n'), lang), nums = o.nums !== false;
  const nw = nums ? String(real.reduce((a, r) => Math.max(a, r[0]), 1)).length : 0;
  let k = 0;
  return `<div class="op-code${o.diff ? ' op-dc' : ''}${o.wrap ? ' wrap' : ''}${nums ? '' : ' nonum'}" style="--nw:${nw}">${rows.map(r => r === '⋯' ? '<div class="op-hunk">⋯</div>'
    : `<div class="op-ln${r[2] === '+' ? ' add' : r[2] === '-' ? ' del' : ''}${o.at && r[0] === o.at && r[2] !== '-' ? ' at' : ''}"><b>${nums && r[0] ? r[0] : ''}</b><u>${o.diff && r[2] !== ' ' ? r[2] === '-' ? '−' : '+' : ''}</u><span>${h[k++] ?? ''}</span></div>`).join('')}</div>`;
}
// A diff as rows, numbered as the file is now: each hunk starts where `hunks` says.
export function diffRows(d: Diff, hunks: number[] = []): Row[] {
  const rows: Row[] = [];
  let h = 0, n = hunks[0] ?? 1;
  if (n > 1) rows.push('⋯');
  for (const [m, t] of d) {
    if (m === ' ' && t === '⋯') { rows.push('⋯'); n = hunks[++h] ?? n; continue; }
    rows.push([m === '-' ? 0 : n, t, m]);
    if (m !== '-') n++;
  }
  return rows;
}
const lines = (text: string) => { const l = text.split('\n'); if (l.length > 1 && l.at(-1) === '') l.pop(); return l; };

// ---------- markdown, rendered as a document ----------
// Headings, paragraphs, lists (with their boxes), tables, quotes, code in its colours; links and paths open in place.
const em = (h: string) => h.replace(/(?<![*\w])\*(?![\s*])([^*<>]+?)(?<!\s)\*(?![*\w])/g, '<em>$1</em>').replace(/~~([^~<>]+)~~/g, '<del>$1</del>');
const inl = (t: string) => em(inline(t));
export function mdDoc(src: string) {
  const L = src.replace(/\r/g, '').split('\n'), out: string[] = [];
  const cells = (r: string) => r.trim().replace(/^\||\|$/g, '').split('|').map(x => x.trim());
  for (let i = 0; i < L.length;) {
    const l = L[i];
    let m: RegExpExecArray | null;
    if (!l.trim()) { i++; continue; }
    if ((m = /^\s*(```|~~~)\s*([\w+-]*)/.exec(l))) {
      const fence = m[1], body: string[] = [];
      while (++i < L.length && !L[i].trimStart().startsWith(fence)) body.push(L[i]);
      i++;
      out.push(`<pre>${hl(body.join('\n'), LANG[m[2].toLowerCase()] ?? (hljs.getLanguage(m[2]) ? m[2] : null)).join('\n')}</pre>`);
      continue;
    }
    if ((m = /^(#{1,6})\s+(.*?)\s*#*$/.exec(l))) { const n = Math.min(m[1].length, 4); out.push(`<h${n}>${inl(m[2])}</h${n}>`); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { out.push('<hr>'); i++; continue; }
    if (/^\s*\|/.test(l) && i + 1 < L.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(L[i + 1])) {
      const head = cells(l), rows: string[][] = [];
      for (i += 2; i < L.length && /^\s*\|/.test(L[i]); i++) rows.push(cells(L[i]));
      out.push(`<table><thead><tr>${head.map(c => `<th>${inl(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${inl(c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
      continue;
    }
    if (/^\s*>/.test(l)) { const q: string[] = []; while (i < L.length && /^\s*>/.test(L[i])) q.push(inl(L[i++].replace(/^\s*>\s?/, ''))); out.push(`<blockquote>${q.join('<br>')}</blockquote>`); continue; }
    if ((m = /^\s*([-*+]|\d+[.)])\s+/.exec(l))) {
      const ol = /\d/.test(m[1]), items: string[] = [];
      while (i < L.length && (m = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(L[i])) && /\d/.test(m[1]) === ol) {
        const t = m[2], box = /^\[( |x|X)\]\s+(.*)$/.exec(t);
        items.push(box ? `<li class="task"><i${box[1] !== ' ' ? ' class="y"' : ''}>${box[1] !== ' ' ? '✓' : ''}</i><span>${inl(box[2])}</span></li>` : `<li>${inl(t)}</li>`);
        i++;
      }
      out.push(ol ? `<ol>${items.join('')}</ol>` : `<ul>${items.join('')}</ul>`);
      continue;
    }
    const p: string[] = [];
    while (i < L.length && L[i].trim() && !/^\s*(```|~~~|#{1,6}\s|>|[-*+]\s|\d+[.)]\s|\|)/.test(L[i])) p.push(inl(L[i++].trim()));
    if (!p.length) { p.push(inl(L[i++].trim())); }
    out.push(`<p>${p.join(' ')}</p>`);
  }
  return `<div class="op-md">${out.join('')}</div>`;
}

// ---------- a log and a CSV, read as they are meant ----------
const TIME = /^(\[?\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?\]?|\[?\d\d:\d\d:\d\d(?:[.,]\d+)?\]?)(\s+)(.*)$/;
const LEVEL = /^(\[?(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL)\]?:?)(\s+)(.*)$/i;
const isLog = (p: string, ls: string[]) => /\.log$/i.test(p) || ls.slice(0, 40).filter(l => l.trim()).filter(l => TIME.test(l)).length >= Math.max(3, ls.slice(0, 40).filter(l => l.trim()).length * .6);
function logHTML(ls: string[]) {
  let lv = '';
  return `<div class="op-log">${ls.map(l => {
    const t = TIME.exec(l);
    if (!t) return `<div class="op-lg${lv}">${esc(l) || ' '}</div>`;
    const v = LEVEL.exec(t[3]), k = v?.[2].toUpperCase() ?? '';
    lv = /^(ERROR|ERR|FATAL|CRITICAL)$/.test(k) ? ' error' : /^WARN/.test(k) ? ' warn' : '';
    return `<div class="op-lg${lv}"><span class="t">${esc(t[1])}</span>${t[2]}${v ? `<b>${esc(v[1])}</b>${v[3]}${esc(v[4])}` : esc(t[3])}</div>`;
  }).join('')}</div>`;
}
function csvCells(l: string) {
  const out: string[] = [];
  let cur = '', q = false;
  for (const ch of l) {
    if (ch === '"') q = !q;
    if (ch === ',' && !q) { out.push(cur); cur = ''; } else cur += ch;
  }
  out.push(cur);
  return out;
}
function csvHTML(ls: string[]) {
  const nw = String(ls.length).length;
  const cell = (c: string, head: boolean) => head ? `<span class="op-ch">${esc(c)}</span>` : /^\s*-?\d+(\.\d+)?%?\s*$/.test(c) ? `<span class="hljs-number">${esc(c)}</span>`
    : /^\s*"?\d{4}-\d\d-\d\d/.test(c) ? `<span class="hljs-string">${esc(c)}</span>` : esc(c);
  return `<div class="op-code op-csv" style="--nw:${nw}">${ls.map((l, i) => `<div class="op-ln"><b>${i + 1}</b><u></u><span>${csvCells(l).map(c => cell(c, i === 0)).join('<i class="op-cs">,</i>')}</span></div>`).join('')}</div>`;
}

// ---------- the recording and the sound: their own controls ----------
const PLAY = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M5 3.2v9.6L12.8 8z"/></svg>';
const PAUSE = '<svg viewBox="0 0 16 16" fill="currentColor"><rect x="4" y="3" width="2.8" height="10" rx="1"/><rect x="9.2" y="3" width="2.8" height="10" rx="1"/></svg>';
const ctlHTML = '<div class="op-ctl"><button type="button" class="op-pp" aria-label="播放">' + PLAY + '</button><span class="op-tm">0:00</span><div class="op-scrub" role="slider" aria-label="进度" tabindex="0" aria-valuemin="0" aria-valuenow="0"><i></i><b></b></div><span class="op-tm op-dur">0:00</span></div>';
// Play and pause, the time, and a scrubber that seeks while dragged; `drawn` hears every move of the playhead.
function wire(root: HTMLElement, m: HTMLMediaElement, drawn: () => void = () => {}) {
  const pp = root.querySelector<HTMLElement>('.op-pp')!, big = root.querySelector<HTMLElement>('.op-big'), now = root.querySelector<HTMLElement>('.op-tm')!, dur = root.querySelector<HTMLElement>('.op-dur')!;
  const sc = root.querySelector<HTMLElement>('.op-scrub')!, bar = sc.querySelector<HTMLElement>('i')!, knob = sc.querySelector<HTMLElement>('b')!;
  let raf = 0;
  const show = () => {
    const d = Number.isFinite(m.duration) ? m.duration : 0, f = d ? Math.min(1, m.currentTime / d) : 0;
    now.textContent = clock(m.currentTime); dur.textContent = clock(d);
    bar.style.width = `${f * 100}%`; knob.style.left = `${f * 100}%`; sc.setAttribute('aria-valuenow', String(Math.round(f * 100)));
    drawn();
  };
  const tick = () => { show(); if (!m.paused) raf = requestAnimationFrame(tick); };
  const state = () => {
    const on = !m.paused;
    pp.innerHTML = on ? PAUSE : PLAY; pp.setAttribute('aria-label', on ? '暂停' : '播放');
    root.classList.toggle('on', on);
    cancelAnimationFrame(raf); if (on) raf = requestAnimationFrame(tick); else show();
  };
  const toggle = () => { if (m.paused) void m.play().catch(() => {}); else m.pause(); };
  pp.addEventListener('click', toggle); big?.addEventListener('click', toggle);
  root.querySelector('video')?.addEventListener('click', toggle);
  for (const e of ['play', 'pause', 'ended']) m.addEventListener(e, state);
  for (const e of ['loadedmetadata', 'durationchange', 'timeupdate', 'seeked']) m.addEventListener(e, show);
  // A recording a browser made says no length until it is read to the end: a seek past it makes the length known.
  m.addEventListener('loadedmetadata', () => {
    if (m.duration !== Infinity) return;
    const known = () => { if (!Number.isFinite(m.duration)) return; m.removeEventListener('durationchange', known); m.currentTime = 0; };
    m.addEventListener('durationchange', known); m.currentTime = 1e101;
  }, { once: true });
  const seekAt = (el: HTMLElement, x: number) => { const r = el.getBoundingClientRect(), d = Number.isFinite(m.duration) ? m.duration : 0; if (d && r.width) { m.currentTime = Math.max(0, Math.min(1, (x - r.left) / r.width)) * d; show(); } };
  for (const el of [sc, root.querySelector<HTMLElement>('.op-wave')].filter((e): e is HTMLElement => !!e)) {
    el.addEventListener('pointerdown', e => {
      el.setPointerCapture(e.pointerId); seekAt(el, e.clientX);
      const move = (ev: PointerEvent) => seekAt(el, ev.clientX), up = () => { el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); };
      el.addEventListener('pointermove', move); el.addEventListener('pointerup', up);
    });
  }
  sc.addEventListener('keydown', e => { if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { e.preventDefault(); e.stopPropagation(); m.currentTime = Math.max(0, m.currentTime + (e.key === 'ArrowRight' ? 5 : -5)); } });
}
// The sound's shape, drawn from its own samples: what has played in the window's colour, the playhead warm.
async function wave(root: HTMLElement, m: HTMLAudioElement, src: string, size: number) {
  const c = root.querySelector('canvas')!, box = root.querySelector<HTMLElement>('.op-wave')!;
  let peaks: number[] = [];
  const draw = () => {
    const d = devicePixelRatio || 1, w = box.clientWidth, h = box.clientHeight;
    if (!w || !h) return;
    if (c.width !== Math.round(w * d)) { c.width = Math.round(w * d); c.height = Math.round(h * d); }
    const g = c.getContext('2d')!;
    g.setTransform(d, 0, 0, d, 0, 0); g.clearRect(0, 0, w, h);
    const n = Math.max(1, Math.floor(w / 3)), f = Number.isFinite(m.duration) && m.duration ? m.currentTime / m.duration : 0, mid = h / 2;
    for (let i = 0; i < n; i++) {
      const p = peaks.length ? peaks[Math.floor(i / n * peaks.length)] : 0, bh = Math.max(1, p * (h - 16));
      g.fillStyle = i / n < f ? 'rgb(157 180 255 / .95)' : 'rgb(196 204 238 / .34)';
      g.fillRect(i * 3 + 1, mid - bh / 2, 2, bh);
    }
    if (f > 0) { g.fillStyle = '#ffc98f'; g.fillRect(Math.round(f * w) - 1, 6, 2, h - 12); }
  };
  new ResizeObserver(draw).observe(box);
  if (size <= 40 << 20) {
    try {
      const buf = await (await fetch(src)).arrayBuffer(), a = await new OfflineAudioContext(1, 1, 44100).decodeAudioData(buf), ch = a.getChannelData(0);
      const n = 600, step = Math.max(1, Math.floor(ch.length / n)), out: number[] = [];
      for (let i = 0; i < n; i++) { let p = 0; for (let j = i * step, e = Math.min(ch.length, j + step); j < e; j++) p = Math.max(p, Math.abs(ch[j])); out.push(p); }
      const top = Math.max(...out, .01);
      peaks = out.map(p => p / top);
    } catch { /* drawn flat */ }
  }
  draw();
  return draw;
}

// ---------- the sheet ----------
export type FileView = {
  view: HTMLElement; k: Peek;
  // Where its bytes are (GET /sessions/{id}/file/…), where it is as the head says, and the head's line after that.
  src: string; dir: string; info(text: string): void;
  // A line to stop at instead of the one the reference named (coming back to it), and what it was looking at then.
  scroll?: number; mode?: string;
};
const modes = new Map<string, string>();
// The mode a file is looked at in: 'r' rendered or 's' source for markdown, 'all' or 'diff' for a changed file.
export const modeOf = (abs: string) => modes.get(abs);
export function stopFile(view: HTMLElement) { for (const m of view.querySelectorAll<HTMLMediaElement>('video,audio')) { m.pause(); m.removeAttribute('src'); m.load(); } }
export function drawFile(f: FileView) {
  const { view, k } = f, name = k.abs.slice(k.abs.lastIndexOf('/') + 1), size = k.size ?? 0, ext = extOf(k.abs);
  view.classList.remove('web');
  if (k.kind === 'dir') {
    const es = k.entries ?? [];
    f.info(`${es.length} 项`);
    view.innerHTML = `<div class="op-dir">${es.map(e => `<button type="button" data-act="peek" data-ref="${esc(`${k.abs}/${e.name}`)}"><i>${e.dir ? '▸' : ''}</i><span>${esc(e.name)}${e.dir ? '/' : ''}</span></button>`).join('') || '<p class="op-empty">空文件夹</p>'}</div>`;
    return;
  }
  if (k.bytes && /^(png|jpe?g|gif|webp|avif|svg|bmp|ico)$/.test(ext)) {
    f.info(bytes(size));
    view.innerHTML = `<div class="op-img"><img src="${esc(f.src)}" alt="${esc(name)}" draggable="false"></div>`;
    const box = view.firstElementChild as HTMLElement, img = box.querySelector('img')!;
    img.addEventListener('load', () => f.info(`${img.naturalWidth} × ${img.naturalHeight} · ${bytes(size)}`), { once: true });
    img.addEventListener('error', () => { view.innerHTML = '<p class="pv-err">这张图打不开</p>'; }, { once: true });
    // A click shows it 1:1 with the point clicked kept under the pointer; another fits it again.
    img.addEventListener('click', e => {
      const r = img.getBoundingClientRect(), fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height, vr = view.getBoundingClientRect();
      box.classList.toggle('z');
      if (box.classList.contains('z')) { const z = img.getBoundingClientRect(); view.scrollLeft += z.left - vr.left + fx * z.width - (e.clientX - vr.left); view.scrollTop += z.top - vr.top + fy * z.height - (e.clientY - vr.top); }
    });
    return;
  }
  if (k.bytes && k.kind === 'media' && /^(mp4|m4v|mov|webm|ogv)$/.test(ext)) {
    f.info(bytes(size));
    view.innerHTML = `<div class="op-vid"><div class="op-frame"><video preload="metadata" playsinline src="${esc(f.src)}"></video><button type="button" class="op-big" aria-label="播放">${PLAY}</button></div>${ctlHTML}</div>`;
    const root = view.firstElementChild as HTMLElement, v = root.querySelector('video')!;
    const said = () => { if (v.videoWidth) (root.querySelector('.op-frame') as HTMLElement).style.aspectRatio = `${v.videoWidth} / ${v.videoHeight}`; f.info(`${Number.isFinite(v.duration) ? `${clock(v.duration)} · ` : ''}${v.videoWidth ? `${v.videoWidth} × ${v.videoHeight} · ` : ''}${bytes(size)}`); };
    v.addEventListener('loadedmetadata', said); v.addEventListener('durationchange', said);
    v.addEventListener('error', () => { view.innerHTML = '<p class="pv-err">这段视频在这里放不了：按右上角在它自己的 App 里打开</p>'; }, { once: true });
    wire(root, v);
    return;
  }
  if (k.bytes && k.kind === 'media') {
    f.info(`${ext.toUpperCase()} · ${bytes(size)}`);
    view.innerHTML = `<div class="op-aud"><div class="op-wave" aria-label="波形，点一下跳到那里"><canvas></canvas></div>${ctlHTML}<audio preload="metadata" src="${esc(f.src)}"></audio></div>`;
    const root = view.firstElementChild as HTMLElement, a = root.querySelector('audio')!;
    const said = () => f.info(`${Number.isFinite(a.duration) ? `${clock(a.duration)} · ` : ''}${ext.toUpperCase()} · ${bytes(size)}`);
    a.addEventListener('loadedmetadata', said); a.addEventListener('durationchange', said);
    a.addEventListener('error', () => { view.innerHTML = '<p class="pv-err">这段声音在这里放不了：按右上角在它自己的 App 里打开</p>'; }, { once: true });
    let redraw = () => {};
    wire(root, a, () => redraw());
    void wave(root, a, f.src, size).then(d => { redraw = d; });
    return;
  }
  if (k.bytes && ext === 'pdf') {
    f.info(`${k.pages ? `${k.pages} 页 · ` : ''}${bytes(size)}`);
    view.classList.add('web');
    view.innerHTML = `<iframe class="op-pdf" src="${esc(f.src)}#navpanes=0&view=FitH" title="${esc(name)}"></iframe>`;
    return;
  }
  const text = k.text ?? '', ls = lines(text), lang = langOf(k.abs), sz = bytes(size);
  if (k.kind === 'md') {
    const mode = f.mode ?? modes.get(k.abs) ?? 'r';
    f.info('Markdown');
    view.innerHTML = `<div class="op-bar"><span class="op-seg" role="group" aria-label="怎么看"><button type="button" data-v="r" aria-pressed="${mode === 'r'}">渲染</button><button type="button" data-v="s" aria-pressed="${mode === 's'}">源码</button></span></div>`
      + (mode === 'r' ? mdDoc(text) : rowsHTML(ls.map((t, i) => [i + 1, t, ' ']), 'markdown', { wrap: true, at: k.line }));
    // A path a document links to is where the document is, not where the session is.
    const dir = k.abs.slice(0, k.abs.lastIndexOf('/'));
    for (const a of view.querySelectorAll<HTMLElement>('.op-md a.ref[data-ref]')) {
      const r = a.dataset.ref!;
      if (!/^(https?:|mailto:|\/|~|#)/i.test(r)) try { a.dataset.ref = decodeURIComponent(new URL(r, `file://${dir}/`).pathname); } catch { /* as written */ }
    }
    seg(view, v => { modes.set(k.abs, v); drawFile({ ...f, mode: v, scroll: undefined }); });
    at(view, f, mode === 's' ? k.line : undefined);
    return;
  }
  if (k.cut) {
    f.info(`${sz} · 只读了最后 1 MB`);
    const gap = `<div class="op-gap">⋯ 前面还有 ${bytes(Math.max(0, size - (1 << 20)))}</div>`;
    view.innerHTML = isLog(k.abs, ls) ? logHTML(ls).replace('<div class="op-log">', `<div class="op-log">${gap}`) : gap + rowsHTML(ls.map(t => [0, t, ' ']), lang, { nums: false });
    view.scrollTop = f.scroll ?? view.scrollHeight;
    return;
  }
  // Code and text: the whole file, the lines this session added marked in the gutter; its changes a click away.
  const changed = !!k.diff?.length, mode = changed ? f.mode ?? modes.get(k.abs) ?? 'all' : 'all';
  const added = new Set<number>();
  if (changed) for (const r of diffRows(k.diff!, k.hunks)) if (r !== '⋯' && r[2] === '+') added.add(r[0]);
  f.info(`${ls.length} 行${k.line ? ` · 第 ${k.line} 行` : ''}${changed ? ` · +${k.add ?? 0} −${k.del ?? 0}` : ''}`);
  const bar = changed ? `<div class="op-bar"><span class="op-seg" role="group" aria-label="看什么"><button type="button" data-v="all" aria-pressed="${mode === 'all'}">全文</button><button type="button" data-v="diff" aria-pressed="${mode === 'diff'}">改动</button></span></div>` : '';
  view.innerHTML = bar + (mode === 'diff' ? rowsHTML(diffRows(k.diff!, k.hunks), lang, { diff: true })
    : ext === 'csv' ? csvHTML(ls)
    : isLog(k.abs, ls) && !lang ? logHTML(ls)
    : rowsHTML(ls.map((t, i) => [i + 1, t, added.has(i + 1) ? '+' : ' ']), lang, { at: k.line }));
  seg(view, v => { modes.set(k.abs, v); drawFile({ ...f, mode: v, scroll: undefined }); });
  at(view, f, mode === 'all' ? k.line : undefined);
}
function seg(view: HTMLElement, pick: (v: string) => void) {
  for (const b of view.querySelectorAll<HTMLElement>('.op-seg button')) b.addEventListener('click', () => { if (b.getAttribute('aria-pressed') !== 'true') pick(b.dataset.v!); });
}
// Back where it was when it comes back; at the named line, a little above the middle, the first time.
function at(view: HTMLElement, f: FileView, line?: number) {
  if (f.scroll !== undefined) { view.scrollTop = f.scroll; return; }
  const row = line ? view.querySelector<HTMLElement>('.op-ln.at') : null;
  view.scrollTop = row ? Math.max(0, row.offsetTop - view.clientHeight * .38) : 0;
}
