// Under each message, the way the Claude app has it (WP-A · 消息): an answer has its time, 复制 and 表情; what you said has
// its versions ‹ n/m ›, 表情, 修改, 复制 and its time. 修改 opens it in place and sends it again: the host stops a running
// turn, goes back to before that message (Claude's files too) in a new session that reads as the same conversation, and
// archives this one as the version before (m-edit, m-ver). Reactions never wake the agent: they wait on the message,
// dashed, until the next message you send carries them (m-rx). The agent's 👀 says it took a message you sent while it
// worked, and ↑ in an empty composer takes the newest one still queued back to change it (m-eyes).
import type { Item, Sess } from '../../electron/agents/types';
import type { Feature, PageCtx } from './ctx';
import './messages.css';

type Msg = Item & { k: 'you' | 'it' };
const RXS = ['👍', '❤️', '😂', '🎉', '🤔', '👀', '🙏', '👎'];
const NAME = { claude: 'Claude', codex: 'Codex' } as const;
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const svg = (d: string, w = 1.4) => `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const I = {
  smile: svg('<circle cx="8" cy="8" r="5.6"/><path d="M5.7 9.4c.5.8 1.3 1.2 2.3 1.2s1.8-.4 2.3-1.2"/><path d="M6.1 6.4v.3M9.9 6.4v.3" stroke-width="1.6"/>', 1.3),
  edit: svg('<path d="M10.8 2.7a1.6 1.6 0 0 1 2.3 2.3L5.8 12.3l-3 .8.8-3z"/><path d="M9.6 3.9l2.3 2.3"/>', 1.3),
  copy: svg('<rect x="5.5" y="5.5" width="8" height="8" rx="2"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/>', 1.5),
  x: svg('<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>'),
  prev: svg('<path d="M10 3.5 5.5 8l4.5 4.5"/>', 1.6),
  next: svg('<path d="M6 3.5 10.5 8 6 12.5"/>', 1.6),
};
// When a message was sent: the clock today, the date in front on other days (as the page says it).
function clock(at: number) {
  const d = new Date(at), t = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  return d.toDateString() === new Date().toDateString() ? t : `${d.getMonth() + 1}月${d.getDate()}日 ${t}`;
}
const busy = (s: Sess) => s.st === 'work' || s.st === 'pack' || s.st === 'wait';
const nextFrame = () => new Promise<void>(r => requestAnimationFrame(() => r()));

export function mountMessages(ctx: PageCtx): Feature {
  // The message being changed: its words when it opened, what the box holds now, how many files would go back (-1: none
  // can, Claude has no checkpoint for it), and whether it is on its way.
  let editing: { id: string; at: string; text: string; value: string; sel: [number, number]; files?: number; sending?: boolean } | null = null;
  // The message the reactions popover is open for.
  let rxAt: { id: string; key: string } | null = null, rxWasOpen = false, box: HTMLTextAreaElement | null = null;
  ctx.win.addEventListener('pointerdown', () => { rxWasOpen = !!ctx.win.querySelector('.pop.rxp.on'); }, true);

  // A message's key for its reactions and versions: its kind and id, for the first message of that kind with that id (a
  // Codex message steered into a running turn carries the turn's id too, and takes neither).
  const firsts = new WeakMap<Item[], Map<string, number>>();
  function keyOf(s: Sess, it: Msg, i: number) {
    if (!it.id || i < 0) return '';
    const items = ctx.items(s.id) ?? [], key = `${it.k}:${it.id}`;
    let m = firsts.get(items);
    const j = m?.get(key), hit = j !== undefined ? items[j] : undefined;
    if (!m || !hit || hit.k !== it.k || (hit as Msg).id !== it.id) {
      m = new Map();
      items.forEach((x, n) => { if ((x.k === 'you' || x.k === 'it') && x.id && !m!.has(`${x.k}:${x.id}`)) m!.set(`${x.k}:${x.id}`, n); });
      firsts.set(items, m);
    }
    return m.get(key) === i ? key : '';
  }
  // The latest thing you said and the latest answer keep their row showing.
  function latest(s: Sess, k: 'you' | 'it') {
    const items = ctx.items(s.id) ?? [];
    for (let j = items.length - 1; j >= 0; j--) if (items[j].k === k) return j;
    return -1;
  }

  // ---------- reactions ----------
  function chips(s: Sess, key: string) {
    const r = key ? s.rx?.[key] : undefined;
    if (!r?.by && !r?.mine?.length) return '';
    const by = r.by ? `<span class="rx-c by" data-tip="${NAME[s.agent]} 看到了">${r.by}</span>` : '';
    return `<div class="rxs">${by}${(r.mine ?? []).map(e => { const sent = !!r.sent?.includes(e); return `<button type="button" class="rx-c${sent ? '' : ' wait'}" data-act="m-rxt" data-m="${key}" data-e="${e}" data-tip="${sent ? '带给它了 · 点一下去掉' : '跟你下一句一起带给它'}" aria-label="${e}">${e}</button>`; }).join('')}</div>`;
  }
  const smile = (key: string) => `<button type="button" class="ia" data-act="m-rx" data-m="${key}" data-tip="表情" aria-label="表情">${I.smile}</button>`;
  // On at once here; the host's row follows. It is kept there and never sent by itself.
  async function toggle(s: Sess, key: string, e: string) {
    const r = s.rx?.[key] ?? {}, on = !r.mine?.includes(e), k = key.slice(0, key.indexOf(':'));
    s.rx = { ...s.rx, [key]: { ...r, mine: on ? [...r.mine ?? [], e] : (r.mine ?? []).filter(y => y !== e), sent: (r.sent ?? []).filter(y => y !== e) } };
    if (on) ctx.cue('on', .5); else ctx.tick();
    ctx.draw('main');
    await ctx.tryCall(`/sessions/${s.id}/rx`, { k, at: key.slice(k.length + 1), e });
  }

  // ---------- versions: each is a session; the others stay archived ----------
  // The versions of the message `key` names here, one session for each number: this one for its own, else the one in
  // the list, else the latest.
  function versions(s: Sess, key: string) {
    const v = s.vers?.at[key];
    if (!v) return null;
    const by = new Map<number, Sess>();
    for (const o of ctx.sessions()) for (const [fam, n] of Object.values(o.vers?.at ?? {})) {
      if (fam !== v[0] || o.vers?.root !== s.vers?.root) continue;
      const had = by.get(n);
      if (!had || (had.archived && !o.archived) || (had.archived === o.archived && o.updated > had.updated)) by.set(n, o);
    }
    by.set(v[1], s);
    const ns = [...by.keys()].sort((a, b) => a - b);
    return { fam: v[0], ns, by, at: ns.indexOf(v[1]) };
  }
  function verHTML(s: Sess, key: string) {
    const v = versions(s, key);
    if (!v || v.ns.length < 2) return '';
    const off = busy(s);
    return `<span class="m-ver"><button type="button" class="ia" data-act="m-ver" data-m="${key}" data-d="-1" data-tip="上一版" aria-label="上一版"${off || v.at === 0 ? ' disabled' : ''}>${I.prev}</button>`
      + `<em>${v.at + 1}/${v.ns.length}</em><button type="button" class="ia" data-act="m-ver" data-m="${key}" data-d="1" data-tip="下一版" aria-label="下一版"${off || v.at === v.ns.length - 1 ? ' disabled' : ''}>${I.next}</button></span>`;
  }
  // Another version, with the message stepped under where it was.
  async function step(s: Sess, key: string, d: number, el: HTMLElement) {
    const v = versions(s, key), t = v?.by.get(v.ns[v.at + d]);
    if (!v || !t || busy(s)) return;
    const top = el.closest('.item')?.getBoundingClientRect().top ?? 0;
    ctx.tick();
    ctx.open(t.id, 'key');
    const tk = Object.entries(t.vers?.at ?? {}).find(([, x]) => x[0] === v.fam)?.[0] ?? '', id = tk.slice(4);
    for (let n = 0; n < 60; n++) {
      await nextFrame();
      const items = ctx.items(t.id), i = items?.findIndex(x => x.k === 'you' && x.id === id) ?? -1;
      const root = ctx.win.querySelector<HTMLElement>('.host > .conv'), row = root?.querySelector('.c-items')?.children[i];
      if (ctx.current()?.id !== t.id || i < 0 || !root || !row) continue;
      root.scrollTop += row.getBoundingClientRect().top - top;
      if (n > 4) return;
    }
  }

  // ---------- 修改 ----------
  function note(s: Sess) {
    const e = editing!;
    return [busy(s) && '这一轮会停下', s.agent === 'codex' ? 'Codex 只回退对话，文件不动' : e.files === undefined ? '' : e.files < 0 ? '这一句没有检查点，文件不动'
      : e.files ? `之后改的 ${e.files} 个文件也回去` : ''].filter(Boolean).join(' · ');
  }
  function editHTML(s: Sess) {
    const e = editing!, n = note(s);
    // The words it opened with: what is typed since stays in the box, and a redraw puts it back (keep()).
    requestAnimationFrame(keep);
    return `<div class="m-edit${e.sending ? ' sending' : ''}"><textarea class="m-eta" rows="2" aria-label="修改这一句"${e.sending ? ' disabled' : ''}>${esc(e.text)}</textarea>`
      + `<div class="m-erow">${n ? `<small>${esc(n)}</small>` : ''}<span class="sp"></span><button type="button" class="btn sm" data-act="m-ex" data-tip="不改了" data-key="esc">取消</button>`
      + `<button type="button" class="btn sm warm${e.sending ? ' is-busy' : ''}" data-act="m-ego"${e.sending ? ' disabled' : ''}>重发 <kbd>⌘⏎</kbd></button></div></div>`;
  }
  // The box as it is typed in, whatever redraws the conversation.
  function keep() {
    const t = ctx.win.querySelector<HTMLTextAreaElement>('.m-eta');
    if (!editing || !t || t === box) return;
    const had = !!box;
    box = t;
    t.value = editing.value; fit(t);
    if (!editing.sending) { t.focus({ preventScroll: had }); t.setSelectionRange(...editing.sel); }
    if (!had) t.closest('.item')?.scrollIntoView({ block: 'nearest' });
  }
  const fit = (t: HTMLTextAreaElement) => { t.style.height = 'auto'; t.style.height = `${Math.min(220, t.scrollHeight)}px`; };
  ctx.win.addEventListener('input', e => { const t = e.target as HTMLTextAreaElement; if (editing && t.classList.contains('m-eta')) { editing.value = t.value; editing.sel = [t.selectionStart, t.selectionEnd]; fit(t); } });
  document.addEventListener('selectionchange', () => { if (editing && box && document.activeElement === box) editing.sel = [box.selectionStart, box.selectionEnd]; });
  function startEdit(s: Sess, it: Msg) {
    if (!it.id) return;
    const e = editing = { id: s.id, at: it.id, text: it.text, value: it.text, sel: [it.text.length, it.text.length] as [number, number], files: undefined as number | undefined };
    box = null; ctx.closeMenu(); ctx.tick(); ctx.draw('main');
    // Claude's checkpoints say which files go back with it.
    if (s.agent === 'claude') void ctx.call<{ can: boolean; files: string[] }>(`/sessions/${s.id}/rewind?at=${encodeURIComponent(it.id)}`)
      .then(r => { if (editing === e) { e.files = r.can ? r.files.length : -1; ctx.draw('main'); } }, () => {});
  }
  function endEdit() { if (!editing) return; editing = null; box = null; ctx.draw('main'); requestAnimationFrame(() => ctx.ta.focus({ preventScroll: true })); }
  // Sent again: the page goes to the new version, which the list shows in this one's place, without gliding.
  async function commit() {
    const e = editing, s = e && ctx.byId(e.id);
    if (!e || !s || e.sending) return;
    const text = e.value.trim();
    if (!text) return;
    if (text === e.text.trim()) { endEdit(); return; }
    e.sending = true; box = null; ctx.draw('main');
    ctx.still(20000);
    const r = await ctx.tryCall(`/sessions/${s.id}/edit`, { at: e.at, text });
    if (!r) { e.sending = false; box = null; ctx.still(0); ctx.draw('main'); return; }
    const id = String(r.id);
    editing = null; box = null;
    ctx.cue('send', .8);
    for (let n = 0; n < 100 && !ctx.byId(id); n++) await new Promise(ok => setTimeout(ok, 30));
    await ctx.load(id);
    ctx.still(1500);
    ctx.open(id, 'key');
  }

  // ---------- what you sent while it worked ----------
  // Taken back from the queue (Claude only: Codex takes it into the running turn at once), into the composer to change.
  async function recall(s: Sess, text: string, back: boolean) {
    if (!await ctx.tryCall(`/sessions/${s.id}/queue`, { action: 'cancel', text })) return;
    ctx.tick();
    if (!back) return;
    const ta = ctx.ta;
    ta.value = text + (ta.value.trim() ? `\n${ta.value}` : '');
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
  }
  function queuedHTML(s: Sess, text: string) {
    const claude = s.agent === 'claude';
    return `<div class="you queued">${esc(text)}</div><div class="m-acts on">${claude ? `<button type="button" class="ia" data-act="m-qedit" data-q="${esc(text)}" data-tip="拿回来改" data-key="↑" aria-label="拿回来改">${I.edit}</button>`
      + `<button type="button" class="ia" data-act="m-qdrop" data-q="${esc(text)}" data-tip="撤回" aria-label="撤回">${I.x}</button>` : ''}`
      + `<time class="q" data-tip="${claude ? '这一步做完它就会看到' : 'Codex 收下就放进这一轮了，撤不回来'}">排着</time></div>`;
  }

  return {
    message(s, it, i, html) {
      if (it.k === 'you' && it.queued) return queuedHTML(s, it.text);
      const key = keyOf(s, it, i), on = i === latest(s, it.k) ? ' on' : '';
      if (it.k === 'it') {
        // The page's row (复制, the time) gets 表情, and the reactions sit above it.
        const at = html.lastIndexOf('<div class="it-acts">');
        if (at < 0 || !key) return html;
        const row = html.slice(at, -'</div></div>'.length);
        return `${html.slice(0, at)}${chips(s, key)}${row.replace('<div class="it-acts">', `<div class="it-acts${on}">`)}${smile(key)}</div></div>`;
      }
      if (editing?.id === s.id && editing.at === it.id && key) return editHTML(s);
      const edit = key && !s.term && !s.gone ? `<button type="button" class="ia" data-act="m-edit" data-m="${key}" data-tip="修改" aria-label="修改">${I.edit}</button>` : '';
      return `${html}${it.ride?.length ? `<p class="m-ride">带上了 ${it.ride.join(' ')}</p>` : ''}${chips(s, key)}<div class="m-acts${on}">${key ? verHTML(s, key) + smile(key) : ''}${edit}`
        + `<button type="button" class="ia" data-act="copy" data-tip="复制" aria-label="复制">${I.copy}</button>${it.at ? `<time>${clock(it.at)}</time>` : ''}</div>`;
    },
    act(a, el) {
      const s = ctx.current(), key = el.dataset.m ?? '';
      if (!a.startsWith('m-') || !s) return false;
      if (a === 'm-rx') {
        if (rxWasOpen && rxAt?.key === key) { rxAt = null; return true; }
        const mine = s.rx?.[key]?.mine ?? [];
        rxAt = { id: s.id, key };
        ctx.menu(`<div class="rx-row">${RXS.map(e => `<button type="button" class="rx-e${mine.includes(e) ? ' on' : ''}" data-act="m-rxe" data-e="${e}" aria-label="${e}">${e}</button>`).join('')}</div>`
          + '<small>不叫醒它 · 跟你下一句一起带过去</small>', el, { right: key.startsWith('you:'), cls: 'rxp' });
      } else if (a === 'm-rxe') {
        const t = rxAt && ctx.byId(rxAt.id);
        ctx.closeMenu();
        if (t && rxAt) void toggle(t, rxAt.key, el.dataset.e!);
        rxAt = null;
      } else if (a === 'm-rxt') void toggle(s, key, el.dataset.e!);
      else if (a === 'm-edit') {
        const it = (ctx.items(s.id) ?? []).find((x): x is Msg => x.k === 'you' && `you:${x.id}` === key);
        if (it) startEdit(s, it);
      } else if (a === 'm-ex') endEdit();
      else if (a === 'm-ego') void commit();
      else if (a === 'm-ver') void step(s, key, Number(el.dataset.d), el);
      else if (a === 'm-qedit' || a === 'm-qdrop') void recall(s, el.dataset.q ?? '', a === 'm-qedit');
      else return false;
      return true;
    },
    esc() { if (!editing) return false; endEdit(); return true; },
    key(e) {
      const t = e.target as HTMLElement;
      // The box's own keys: esc and ⌘⏎ are its, and nothing else in the window takes one typed there.
      if (t.classList?.contains('m-eta')) {
        if (!e.isComposing && e.key === 'Escape') { e.preventDefault(); endEdit(); }
        else if (!e.isComposing && e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void commit(); }
        e.stopImmediatePropagation();
        return true;
      }
      // ↑ in an empty composer: the newest message still queued comes back to change.
      const s = ctx.current(), q = s?.queue?.at(-1);
      if (e.key !== 'ArrowUp' || t !== ctx.ta || ctx.ta.value || e.shiftKey || e.metaKey || e.ctrlKey || e.altKey || e.isComposing || !s || !q) return false;
      e.preventDefault(); e.stopImmediatePropagation();
      void recall(s, q, true);
      return true;
    },
    // Reading an older version: the files are as they are now, and writing here makes it the one in the list.
    rows(s) {
      return s.archived && s.vers ? '<p class="m-old">另一版 · 文件还是现在的样子 · 在这里写一句，就换回这一版</p>' : '';
    },
    hidden(s) {
      if (!s.archived || !s.vers) return false;
      return ctx.sessions().some(o => o !== s && o.vers?.root === s.vers!.root && (!o.archived || o.updated > s.updated));
    },
  };
}
