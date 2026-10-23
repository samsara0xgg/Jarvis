import { useMemo, useRef } from 'react';
import { Briefcase, ChatCircleDots, FolderSimple, GearSix, Notebook, Plugs, SunHorizon, type Icon } from '@phosphor-icons/react';
import { useRoute, type Brief } from '../homeData';
import type { MemoryOverview } from '../MemoryPage';
import type { Page } from '../AroundDashboard';
import type { L } from '../companionSettings';
import type { DoorCtx, DoorPage, DoorRow } from '../doors';

// 更多: the old Dashboard's other pages, one row each. A row opens that page of the Dashboard alone, in the pane (`ctx.tool`, AroundDashboard `only`: its own ‹, its state kept
// while the pane stays up); the page is not about the shown day, so the rows have no time and the bar has no marks. The Dashboard's 现在 and 编辑首页 are not doors, nor is its home.
const TOOLS: readonly { page: Page; icon: Icon; title: L }[] = [
  { page: 'conversation', icon: ChatCircleDots, title: ['Conversation', '聊天记录'] },
  { page: 'memory', icon: Notebook, title: ['Memory', '记忆'] },
  { page: 'projects', icon: FolderSimple, title: ['Projects', '项目'] },
  { page: 'plugins', icon: Plugs, title: ['Plugins', '插件'] },
  { page: 'settings', icon: GearSix, title: ['Settings', '设置'] },
  { page: 'brief', icon: SunHorizon, title: ['Morning brief', '早报'] },
  { page: 'jobs', icon: Briefcase, title: ['Job mail', '求职邮件'] },
];

export function useMoreDoor(ctx: DoorCtx): DoorPage | null {
  const { port, open, t, lang, day, tool } = ctx;
  // What the Dashboard's home already says of two of its pages (the same routes it reads): how many notes are kept and how many are new, and today's brief in its first line.
  const memory = useRoute<MemoryOverview>(port, '/inherent/memory', open, 60_000), brief = useRoute<Brief>(port, '/inherent/brief', open, 10 * 60_000);
  // The renderer is read when a page opens, so a new one each time the companion draws never makes this page a new one.
  const render = useRef(tool); render.current = tool;
  const today = new Date(day.now).toLocaleDateString('en-CA');
  return useMemo((): DoorPage => {
    const kept = Array.isArray(memory.data?.sections) ? memory.data : null, fresh = kept?.new.entries.filter(e => e.kind === 'add' || e.kind === 'rewrite').length ?? 0;
    const sub: Partial<Record<Page, string>> = {};
    if (kept) sub.memory = t([`${kept.items} kept${fresh > 0 ? ` · ${fresh} new` : ''}`, `记着 ${kept.items} 条${fresh > 0 ? ` · ${fresh} 条新的` : ''}`]);
    if (brief.data?.date === today && brief.data.summary) sub.brief = brief.data.summary;
    return {
      count: null, line: t(['Your conversation, memory, projects, plugins and settings live here.', '聊天记录、记忆、项目、插件和设置都在这里。']),
      chips: [], marks: [], nowAt: null,
      // V2: a narrow menu at the left (day-doors.css), one line each: the words under a title are the page's own to say, so they show only as a number at the title (memory: how many are new).
      rows: TOOLS.map(({ page, icon: Glyph, title }): DoorRow => ({ key: page, at: null, icon: <Glyph size={13} aria-hidden="true"/>, title: t(title), sub: sub[page], tag: page === 'memory' && fresh > 0 ? { text: t([`${fresh} new`, `${fresh} 新`]) } : null, opens: !!tool })),
      // The right column opens on 记忆: the one that has something new most days.
      rest: tool ? 'memory' : null,
      detail: (key, back) => {
        const one = TOOLS.find(x => x.page === key);
        return one && render.current ? { title: t(one.title), body: render.current(one.page, back), bare: true } : null;
      },
    };
  }, [memory.data, brief.data, today, !!tool, lang]);
}
