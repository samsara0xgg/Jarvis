import { useEffect, useState } from 'react';

// What the home's new blocks read from the daemon. None of these routes exists yet: until one does, a 404 marks it
// `missing` and its block says so (Today) or stays away (brief, mail, notices). Without a port every block is a demo.
export type WxKind = 'sun' | 'cloud' | 'rain' | 'snow' | 'fog' | 'storm';
export type Weather = { now_c: number; high_c?: number; summary?: string; hours?: { at: string; kind: WxKind; temp_c: number }[] };
export type TodayEvent = { id: string; title: string; start: string; end?: string; all_day?: boolean };
export type Todo = { id: string; title: string; due?: string };
// GET /inherent/today. POST /inherent/today/todo { id, done } checks one off in Microsoft To Do.
export type Today = { weather?: Weather | null; events: TodayEvent[]; todos: Todo[] };
// GET /inherent/brief: today's brief, 404 before it is written. `summary` is the day's main line cut short (card), `lead` more of it (page).
// `sections` are what the page lists, in order; a work item carries `tag` (its kind), `label` (that kind in words) and its `note`.
export type BriefTag = 'done' | 'check' | 'part' | 'going' | 'discussed' | 'browsed';
export type BriefRow = { text: string; note?: string; status?: string; tag?: BriefTag; label?: string };
export type BriefSection = { key: 'items' | 'open' | 'next' | 'decisions' | 'suggestions'; title: string; rows: BriefRow[] };
export type Brief = { date: string; summary: string; lead?: string; items?: number; sections: BriefSection[] };
// POST /inherent/mail/archive { ids } takes letters the last answer called junk out of the inbox; /unarchive { ids } puts them back.
// GET /inherent/mail: unread mail from people (not newsletters or notifications), newest first.
// `reply` (ADR 0123): 'yes' = Jev is very sure it needs Allen's reply, 'fyi' = very sure it does not, null = no mark.
// `junk` (ADR 0124): Jev is very sure it is junk Allen did not ask for; the home offers to archive it, never does by itself.
// `importance`: Jev's 0-3 score when it rated the letter (ADR 0141); it only orders the list.
// `thread_id` ties a letter to the reply card; `address` is the sender's; `category` is Jev's topic ('job_search' feeds the page's filter).
// GET /inherent/mail/{id}: the letter itself, { id, thread_id, from, address, to, subject, received, text } with the body as plain text.
// POST /inherent/mail/{trash,untrash,read,unread} { ids } move letters out of and back into the unread list, like archive above.
export type Mail = { id: string; from: string; subject: string; received: string; reply?: 'yes' | 'fyi' | null; junk?: boolean; importance?: number; thread_id?: string; address?: string; category?: string };
// GET /inherent/notices: what Jarvis itself wants from you (reminders, its questions); agents are not in it.
export type Notice = { id: string; text: string; at: string };

export type Route<T> = { data: T | null; missing: boolean; reload: () => void };
const RETRY_MS = 10_000;
// Polled while the panel is open. A 404 means the daemon does not serve the route; any other failure keeps what was shown.
export function useRoute<T>(port: string | null, path: string, open: boolean, everyMs: number): Route<T> {
  const [data, setData] = useState<T | null>(null), [missing, setMissing] = useState(false);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!port || !open) return;
    let stop = false, timer: ReturnType<typeof setTimeout>;
    // A read that failed (daemon restarting, a slow first upstream call) is tried again in seconds, not at the next tick.
    const load = async () => {
      let ok = false;
      try {
        const r = await fetch(`http://127.0.0.1:${port}${path}`, { signal: AbortSignal.timeout(5000) });
        ok = r.ok || r.status === 404;
        if (!stop) { setMissing(r.status === 404); if (r.ok) setData(await r.json() as T); else if (r.status === 404) setData(null); }
      } catch { /* daemon away; retried below */ }
      if (!stop) timer = setTimeout(load, ok ? everyMs : Math.min(everyMs, RETRY_MS));
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
export const demoBrief = (): Brief => ({ date: new Date().toLocaleDateString('en-CA'), items: 3,
  summary: 'Yesterday the companion went live with the Claude sessions. Today: A3 is due at midnight.',
  sections: [
    { key: 'items', title: 'Yesterday', rows: [
      { text: 'The companion goes live with Claude sessions', tag: 'done', label: 'Done', status: 'committed', note: 'Sessions show on the home and answer from there.' },
      { text: 'Plugin panels show each plugin’s own logo', tag: 'done', label: 'Done', status: 'committed', note: 'The generic icon is gone.' },
      { text: 'CSC370 A3', tag: 'going', label: 'In progress', status: 'attempted / in progress', note: 'The write-up is half done.' }] },
    { key: 'open', title: 'Still open', rows: [{ text: 'Two agents are waiting for you.' }] }] });
export const demoMail = (): Mail[] => [
  { id: 'lee', from: 'Prof. Lee', address: 'lee@uni.example', thread_id: 't-lee', subject: 'Office hours move to Thursday', received: at(-40), reply: 'fyi', importance: 1 },
  { id: 'mom', from: 'Mom', address: 'mom@home.example', thread_id: 't-mom', subject: 'Still on for tonight?', received: at(-95), reply: 'yes', importance: 2.7 },
  { id: 'hire', from: 'Northwind Recruiting', address: 'jobs@northwind.example', thread_id: 't-hire', subject: 'Interview slots for next week', received: at(-300), reply: 'yes', importance: 1.6, category: 'job_search' },
  { id: 'deals', from: 'Shop Deals', address: 'hi@shop.example', thread_id: 't-deals', subject: '50% off everything this weekend', received: at(-130), reply: 'fyi', junk: true, importance: .2 },
];
export const demoMailText: Record<string, string> = {
  lee: 'Hi all,\n\nOffice hours move to Thursday, 3 to 5 PM, in room 214.\nThe A3 questions are still welcome by email: https://courses.example/csc370/a3\n\nProf. Lee',
  mom: 'Are you still coming for dinner tonight?\n\nI am making the soup you like. Call me when you leave.\n\nMom',
  hire: 'Hello,\n\nWe would like to talk about the role. Could you do Tuesday 10:00 or Wednesday 14:00?\nThe job post: https://jobs.northwind.example/roles/4821\n\nBest,\nNorthwind Recruiting',
  deals: 'Everything is half price this weekend only. https://shop.example/sale',
};
export const demoNotices = (): Notice[] => [{ id: 'mic', text: 'Reminder: test the mic at 4 PM', at: at(-5) }];

// Refreshes a clock-driven view every `ms`.
export function useNow(ms: number) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(id); }, [ms]);
  return now;
}
