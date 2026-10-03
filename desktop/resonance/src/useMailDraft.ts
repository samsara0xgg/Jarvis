import { useEffect, useRef, useState } from 'react';
import { postRoute, type Mail } from './homeData';
import type { Card } from './ActionCard';

// One letter's reply draft over the daemon's routes, keyed by the Gmail letter id (404 on the draft route = the feature is off):
//   GET  /inherent/mail/{id}/draft          -> { draft: Draft | null }, polled
//   POST /inherent/mail/{id}/draft          { subject, body } saves hand edits -> { draft }
//   POST /inherent/mail/{id}/draft/send     { subject, body } -> the daemon raises the gmail_send card, polled from /inherent/confirmation
//   POST /inherent/mail/{id}/draft/discard
// Jarvis writes through his own turns, so asking is a typed turn. Revisions are numbered; a revision by Jarvis morphs in over the old text.
export type Draft = { revision: number; to: string; subject: string; body: string; by: 'jarvis' | 'owner' };
export type Fields = { subject: string; body: string };
export const ASK = '给这封邮件起草一封回复';
const FAST_MS = 60_000, SLOW_MS = 3000, NEAR_MS = 700, SAVE_MS = 600, CARD_MS = 1000;
const DEMO = ['Hi,\n\nThanks for your note. Thursday works for me, and I will bring my questions.\n\nAllen', 'Hi,\n\nThanks for the note. Thursday at 3 works well for me. I will bring my A3 questions and see you there.\n\nBest,\nAllen'];

export function useMailDraft(port: string | null, letter: Mail, thread?: string) {
  const id = encodeURIComponent(letter.id), base = `/inherent/mail/${id}/draft`;
  const [draft, setDraft] = useState<Draft | null>(null), [fields, setFields] = useState<Fields>({ subject: '', body: '' });
  const [morph, setMorph] = useState<{ from: string; to: string } | null>(null), [off, setOff] = useState(false);
  const [writing, setWriting] = useState(false), [watch, setWatch] = useState(0), [card, setCard] = useState<Card | null>(null);
  const [done, setDone] = useState(false), [failed, setFailed] = useState(false), [kick, setKick] = useState(0);
  const rev = useRef(0), seq = useRef(0), dirty = useRef(false), fast = useRef(0), saver = useRef<ReturnType<typeof setTimeout>>(undefined);
  const mine = useRef(fields); mine.current = fields;
  const post = (path: string, body: unknown) => port ? postRoute(port, path, body) : Promise.resolve({});

  // A revision newer than the one held. Jarvis's always wins, even over unsaved hand edits; any other waits for them to be saved.
  const adopt = (d: Draft | null) => {
    if (!d) { if (!dirty.current) { rev.current = 0; setDraft(null); } return; }
    const jarvis = d.by === 'jarvis';
    if (d.revision <= rev.current || !jarvis && dirty.current) return;
    clearTimeout(saver.current); dirty.current = false;
    if (jarvis && rev.current && mine.current.body !== d.body) setMorph({ from: mine.current.body, to: d.body });
    rev.current = d.revision; setDraft(d); setFields({ subject: d.subject, body: d.body });
    if (jarvis) setWriting(false);
  };
  // Every 3 s while the letter is open; every 0.7 s for a minute after something was asked of Jarvis from this page.
  useEffect(() => {
    if (!port || done) return;
    let stop = false, timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const r = await fetch(`http://127.0.0.1:${port}${base}`, { signal: AbortSignal.timeout(5000) });
        if (!stop) { setOff(r.status === 404); if (r.ok) adopt((await r.json() as { draft: Draft | null }).draft); }
      } catch { /* daemon away; the next tick retries */ }
      if (!stop) timer = setTimeout(load, Date.now() < fast.current ? NEAR_MS : SLOW_MS);
    };
    void load();
    return () => { stop = true; clearTimeout(timer); };
  }, [port, id, done, kick]);
  useEffect(() => { if (!writing) return; const t = setTimeout(() => setWriting(false), FAST_MS); return () => clearTimeout(t); }, [writing]);
  // Without a daemon a draft turns up a moment after asking, so the page can be tried.
  useEffect(() => {
    if (port || !writing) return;
    const t = setTimeout(() => adopt({ revision: rev.current + 1, to: letter.address ?? letter.from, subject: `Re: ${letter.subject}`, body: DEMO[rev.current ? 1 : 0], by: 'jarvis' }), 1200);
    return () => clearTimeout(t);
  }, [port, writing]);

  const save = async (value: Fields, n: number) => {
    try {
      const r = await post(base, value) as { draft?: Draft | null };
      if (r.draft && r.draft.revision > rev.current) rev.current = r.draft.revision; // his own revision: held, never animated
    } catch { return; } // stays unsaved; the next edit tries again
    if (seq.current === n) dirty.current = false;
  };
  const edit = (change: Partial<Fields>) => {
    const next = { ...mine.current, ...change }, n = ++seq.current;
    mine.current = next; setFields(next); dirty.current = true;
    clearTimeout(saver.current); saver.current = setTimeout(() => void save(next, n), SAVE_MS);
  };
  // Leaving the letter sends the edits still waiting out their delay.
  useEffect(() => () => { clearTimeout(saver.current); if (dirty.current) void post(base, mine.current).catch(() => {}); }, [port, id]);

  const ask = async () => {
    setFailed(false); setWriting(true); fast.current = Date.now() + FAST_MS; setKick(k => k + 1);
    try { await post('/inherent/submit', { text: ASK }); } catch { setWriting(false); setFailed(true); }
  };
  const discard = async () => {
    setFailed(false);
    try { await post(`${base}/discard`, {}); } catch { setFailed(true); return; }
    clearTimeout(saver.current); dirty.current = false; rev.current = 0; setDraft(null); setMorph(null);
  };
  const send = async () => {
    setFailed(false);
    try { await post(`${base}/send`, mine.current); } catch { setFailed(true); return; }
    setWatch(Date.now());
  };
  // After sending, the daemon raises the gmail_send confirmation; the notch hides while the Dashboard is open, so it is asked for here.
  useEffect(() => {
    if (!watch) return;
    let stop = false, timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const c = port ? (await (await fetch(`http://127.0.0.1:${port}/inherent/confirmation`, { signal: AbortSignal.timeout(5000) })).json() as { card: Card | null }).card
          : { id: 'demo', tool: 'gmail_send', action: 'Send email', source: 'gmail', letter: true, args: { threadId: thread, to: letter.address, ...mine.current } };
        if (!stop && c && (c.tool === 'gmail_send' || c.letter) && thread && c.args?.threadId === thread) { setCard(c); setWatch(0); return; }
      } catch { /* tried again until the minute is up */ }
      if (stop) return;
      if (Date.now() < watch + FAST_MS) timer = setTimeout(poll, CARD_MS); else setWatch(0);
    };
    void poll();
    return () => { stop = true; clearTimeout(timer); };
  }, [port, watch, thread]);
  const decide = async (decision: 'accept' | 'reject', edits?: Fields) => {
    setFailed(false);
    try { await post('/inherent/confirmation', { confirmation_id: card!.id, decision, edits }); } catch { setFailed(true); return; }
    setCard(null);
    if (decision === 'accept') { setDone(true); setDraft(null); }
  };
  return { draft, fields, edit, morph, endMorph: () => setMorph(null), off, writing, sending: watch > 0, card, done, failed, ask, send, discard, decide };
}
