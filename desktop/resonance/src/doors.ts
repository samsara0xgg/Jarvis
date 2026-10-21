import type { ReactNode } from 'react';
import { CalendarBlank, CheckCircle, DotsThree, EnvelopeSimple, Gauge, TerminalWindow, type Icon } from '@phosphor-icons/react';
import type { L, Lang } from './companionSettings';
import type { DayItem } from './dayView';
import type { About } from './dayOpen';
import type { Page as DashboardPage } from './AroundDashboard';
import { useAgentsDoor } from './doors/agents';
import { useMailDoor } from './doors/mail';
import { useMoreDoor } from './doors/more';
import { useQuotaDoor } from './doors/quota';
import { useTodoDoor } from './doors/todo';

// The doors at the foot of the day's pane (DayTimeline.tsx): the old Dashboard's tools, each a page that swaps only what is under the bar. A door's hook
// (src/doors/<key>.tsx) reads what its page needs and hands the frame a `DoorPage`, nothing drawn; the frame draws every page the same way (DayDoors.tsx).
export type DoorKey = 'today' | 'mail' | 'todo' | 'agents' | 'quota' | 'more';
export type Tone = 'on' | 'dim' | 'amber';
export type BarMark =
  | { kind: 'point'; at: number; tone: Tone; key?: string }
  | { kind: 'span'; from: number; to: number; lane: 0 | 1 | 2; tone: Tone; key?: string }
  | { kind: 'level'; points: { at: number; value: number }[]; tone: Tone; until?: number; key?: string };
export type DoorChip = { id: string; icon: ReactNode; label: string; value?: string; amber?: boolean; only?: ReadonlySet<string> };
export type DoorRow = {
  key: string;
  at: number | null;            // when (ms), for the order and the bar; null = untimed, listed after the timed rows
  time?: string;                // what the time column says (default: the clock of `at` in the day's zone)
  icon: ReactNode;              // inside the row's 28 px circle: an icon, a letter, a logo
  who?: boolean;                // the circle holds a letter (11.5 px 600, ink .82, border white .12, fill white .04)
  title: ReactNode;
  tag?: { text: string; amber?: boolean } | null;   // the small tag after the title (dt-wait look when amber)
  sub?: ReactNode;
  side?: ReactNode;             // the right column (dt-tm)
  quick?: ReactNode;            // buttons shown while the row is under the pointer, in place of `side` (dt-quick); a button with `data-opens` opens the row like a click on it
  tone?: 'read' | 'future' | 'live';  // read: opacity .55; future: the day's future look; live: icon ring ink .55
  opens?: boolean;              // a click opens its detail
};
export type DoorPage = {
  count: string | null;         // the door's number
  countAmber?: boolean;
  line: string | null;          // one line under the date, written by code from the facts (zh/en)
  chips: DoorChip[];
  link?: { label: string; open: () => void } | null;   // right end of the numbers row, e.g. 邮件 ↗
  marks: BarMark[];
  rows: DoorRow[];              // timed rows oldest first, then untimed
  untimed?: string | null;      // small heading before the untimed rows
  nowAt: number | null;         // where the now line goes among timed rows (null: none)
  empty?: string;               // what an empty page says
  loading?: boolean;
  failed?: boolean;
  // A row's page; `back` closes it, as the frame's ‹ and Esc do. `bare`: the body is a whole page with a ‹ and a title of its own (a Dashboard page from `ctx.tool`), so the frame draws no head for it and gives it the whole
  // stage; Esc presses that ‹ (the page goes back by its own steps before it closes), and the frame's arrow keys leave it alone.
  detail?: (key: string, back: () => void) => { title: string; body: ReactNode; bare?: true } | null;
};
// One page of the old Dashboard shown alone (AroundDashboard `only`), for a row's detail; `back` is what its ‹ calls at the page's root (Companion.tsx makes it, Notch.tsx and DayTimeline.tsx pass it down).
export type ToolRenderer = (page: DashboardPage, back: () => void) => ReactNode;
export type DoorCtx = {
  port: string | null;
  open: boolean;                // poll only while the pane is open
  t: (l: L) => string;
  lang: Lang;
  day: { date: string; tz: string; start: number; end: number; now: number; today: boolean };
  items: DayItem[];             // the shown day's items (the day reply), e.g. `code` sessions
  ask?: (text: string, about?: About) => Promise<void>;
  tool?: ToolRenderer;          // the Dashboard's own pages for a row's detail (更多, 额度): `body: ctx.tool('settings', back)`
};
// One hook per door in src/doors/<key>.tsx: useMailDoor(ctx), useTodoDoor(ctx), useAgentsDoor(ctx),
// useQuotaDoor(ctx), useMoreDoor(ctx): DoorPage | null. All run while the pane is open (the doors
// show counts), in a fixed order; the active one is drawn.

// The row of doors, left to right: what each is called and its glyph; the last has no label (its words are for a screen reader).
export const DOORS: readonly { key: DoorKey; label: L; icon: Icon; bare?: boolean }[] = [
  { key: 'today', label: ['Today', '今天'], icon: CalendarBlank },
  { key: 'mail', label: ['Mail', '信'], icon: EnvelopeSimple },
  { key: 'todo', label: ['To-dos', '待办'], icon: CheckCircle },
  { key: 'agents', label: ['Agents', 'Agents'], icon: TerminalWindow },
  { key: 'quota', label: ['Usage', '额度'], icon: Gauge },
  { key: 'more', label: ['More', '更多'], icon: DotsThree, bare: true },
];
// Every door's hook, in this order every time (hooks keep their place); a door whose hook has nothing to say yet is null.
export function useDoors(ctx: DoorCtx): Record<Exclude<DoorKey, 'today'>, DoorPage | null> {
  return { mail: useMailDoor(ctx), todo: useTodoDoor(ctx), agents: useAgentsDoor(ctx), quota: useQuotaDoor(ctx), more: useMoreDoor(ctx) };
}
