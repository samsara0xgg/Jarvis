import { useEffect, useState } from 'react';

// What the home's new blocks read from the daemon. None of these routes exists yet: until one does, a 404 marks it
// `missing` and its block says so (Today) or stays away (brief, mail, notices). Without a port every block is a demo.
export type WxKind = 'sun' | 'cloud' | 'rain' | 'snow' | 'fog' | 'storm';
export type Weather = { now_c: number; high_c?: number; summary?: string; hours?: { at: string; kind: WxKind; temp_c: number }[] };
export type TodayEvent = { id: string; title: string; start: string; end?: string; all_day?: boolean };
export type Todo = { id: string; title: string; due?: string };
// GET /inherent/today. POST /inherent/today/todo { id, done } checks one off in Microsoft To Do.
export type Today = { weather?: Weather | null; events: TodayEvent[]; todos: Todo[] };
// GET /inherent/brief: today's brief, 404 before it is written.
export type Brief = { date: string; summary: string; body: string; items?: number };
// GET /inherent/mail: unread mail from people (not newsletters or notifications), newest first.
// `reply` (ADR 0123): 'yes' = Jev is very sure it needs Allen's reply, 'fyi' = very sure it does not, null = no mark.
export type Mail = { id: string; from: string; subject: string; received: string; reply?: 'yes' | 'fyi' | null };
// GET /inherent/notices: what Jarvis itself wants from you (reminders, its questions); agents are not in it.
export type Notice = { id: string; text: string; at: string };

export type Route<T> = { data: T | null; missing: boolean; reload: () => void };
// Polled while the panel is open. A 404 means the daemon does not serve the route; any other failure keeps what was shown.
export function useRoute<T>(port: string | null, path: string, open: boolean, everyMs: number): Route<T> {
  const [data, setData] = useState<T | null>(null), [missing, setMissing] = useState(false);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!port || !open) return;
    let stop = false, timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const r = await fetch(`http://127.0.0.1:${port}${path}`, { signal: AbortSignal.timeout(5000) });
        if (!stop) { setMissing(r.status === 404); if (r.ok) setData(await r.json() as T); else if (r.status === 404) setData(null); }
      } catch { /* daemon away; the next tick retries */ }
      if (!stop) timer = setTimeout(load, everyMs);
    };
    void load();
    return () => { stop = true; clearTimeout(timer); };
  }, [port, path, open, tick]);
  return { data, missing, reload: () => setTick(n => n + 1) };
}

export async function postRoute(port: string, path: string, body: unknown) {
  const r = await fetch(`http://127.0.0.1:${port}${path}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body), signal: AbortSignal.timeout(8000) });
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json().catch(() => ({}));
}

// Demo data, dated from now so the demo never shows a call that already happened.
const at = (minutes: number) => new Date(Date.now() + minutes * 60_000).toISOString();
const tonight = () => { const d = new Date(); d.setHours(23, 59, 0, 0); return d.toISOString(); };
export function demoToday(): Today {
  const hour = new Date().getHours();
  const kinds: WxKind[] = hour < 12 ? ['sun', 'cloud', 'cloud', 'rain'] : ['cloud', 'rain', 'rain', 'rain'];
  return {
    weather: { now_c: 14, high_c: 16, summary: 'Rain from 10 PM', hours: kinds.map((kind, i) => ({ at: at(i * 180 + 60), kind, temp_c: 14 - i })) },
    events: [{ id: 'mom', title: 'Call with Mom', start: at(76) }, { id: 'review', title: 'Review the Dashboard', start: at(200) }],
    todos: [{ id: 'a3', title: 'CSC370 A3 write-up', due: tonight() }, { id: 'lee', title: 'Reply to Prof. Lee' }],
  };
}
export const demoBrief = (): Brief => ({ date: new Date().toLocaleDateString('en-CA'), items: 5,
  summary: 'Yesterday the companion went live with the Claude sessions. Today: A3 is due at midnight.',
  body: '**Yesterday**\n\n- The companion went live with the Claude sessions.\n- Plugin panels show each plugin’s own logo.\n\n**Today**\n\n- CSC370 A3 is due at midnight.\n- Call with Mom this evening.\n- Two agents are waiting for you.' });
export const demoMail = (): Mail[] => [
  { id: 'lee', from: 'Prof. Lee', subject: 'Office hours move to Thursday', received: at(-40), reply: 'fyi' },
  { id: 'mom', from: 'Mom', subject: 'Still on for tonight?', received: at(-95), reply: 'yes' },
];
export const demoNotices = (): Notice[] => [{ id: 'mic', text: 'Reminder: test the mic at 4 PM', at: at(-5) }];

// Refreshes a clock-driven view every `ms`.
export function useNow(ms: number) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(id); }, [ms]);
  return now;
}
