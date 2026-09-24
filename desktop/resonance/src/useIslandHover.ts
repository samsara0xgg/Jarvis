import { useCallback, useEffect, useRef, useState } from 'react';

// Open Island's 150ms entry and 100ms edge grace, with the requested 300ms
// leave delay. Visibility is independent of panels, drafts and live sessions.
export function useIslandHover(enabled: boolean, onOpen: () => void) {
  const [open, setOpen] = useState(false);
  const latest = useRef(onOpen); latest.current = onOpen;
  const inside = useRef(false), opened = useRef(false);
  const entry = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const exit = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const grace = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const pointer = useRef<(value: boolean) => void>(() => {});
  const close = useCallback(() => {
    opened.current = false; setOpen(false);
    void window.jarvis?.focus(false);
  }, []);
  const reveal = useCallback(() => {
    clearTimeout(entry.current); entry.current = undefined;
    clearTimeout(exit.current);
    opened.current = true; setOpen(true);
    if (!inside.current) exit.current = setTimeout(close, 300);
  }, [close]);
  useEffect(() => {
    if (!enabled) { opened.current = false; setOpen(false); return; }
    const receive = (value: boolean) => {
      if (value === inside.current) return;
      inside.current = value;
      if (value) {
        clearTimeout(grace.current); clearTimeout(exit.current);
        if (!opened.current && entry.current === undefined) entry.current = setTimeout(() => {
          entry.current = undefined;
          if (!inside.current) return;
          latest.current(); reveal();
        }, 150);
      } else {
        clearTimeout(grace.current);
        grace.current = setTimeout(() => { clearTimeout(entry.current); entry.current = undefined; }, 100);
        clearTimeout(exit.current);
        if (opened.current) exit.current = setTimeout(close, 300);
      }
    };
    pointer.current = receive;
    const unsubscribe = window.jarvis?.onIslandHover(receive);
    return () => {
      unsubscribe?.(); clearTimeout(entry.current); clearTimeout(exit.current); clearTimeout(grace.current);
      entry.current = undefined; inside.current = false; pointer.current = () => {};
    };
  }, [enabled, close, reveal]);
  return { open, reveal, close, enter: () => pointer.current(true), leave: () => pointer.current(false) };
}
