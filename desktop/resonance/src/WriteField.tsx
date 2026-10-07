import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode, type Ref, type RefObject } from 'react';
import { ArrowUp } from '@phosphor-icons/react';
import './write-field.css';

// The box you write a message in, one look and one set of keys for the talk area under her and both of the Dashboard's boxes: no pill,
// the words written on the surface under a hairline, a small arrow to send. Enter sends; Shift+Enter is a new line; the Enter that
// ends an IME composition does nothing. The field grows with its words to six lines, then scrolls.
const MIN = 36, MAX = 136; // one line with its 8 px above and below; six 20 px lines with them

// `on`: whether it is showing, so it is fitted when it comes up. `onFit` hears the height it settled at.
function useGrow(ta: RefObject<HTMLTextAreaElement | null>, value: string, on: boolean, onFit: (height: number) => void) {
  const hear = useRef(onFit); hear.current = onFit;
  // (Empty, it is one line, whatever its placeholder would take at a width it may not have yet.)
  const fit = () => {
    const el = ta.current; if (!el || !on) return;
    const top = el.scrollTop, atEnd = el.selectionStart === el.value.length;
    el.style.height = 'auto';
    const h = el.value ? Math.max(MIN, Math.min(el.scrollHeight, MAX)) : MIN;
    el.style.height = `${h}px`;
    el.scrollTop = atEnd ? el.scrollHeight : top; // writing at the end shows the last line whole; elsewhere it stays where it was
    el.toggleAttribute('data-long', el.scrollHeight > h + 1); // cut off at an edge: those words fade there
    hear.current(h);
  };
  useLayoutEffect(fit, [value, on]);
  // Its width may still be changing as the surface it sits on opens, and the words wrap to it.
  useEffect(() => {
    const el = ta.current; if (!el || !on) return;
    let width = el.offsetWidth;
    const watch = new ResizeObserver(() => { if (el.offsetWidth !== width) { width = el.offsetWidth; fit(); } });
    watch.observe(el);
    return () => watch.disconnect();
  }, [on]);
}

export function WriteField({ className, formRef, inputRef, hidden, on = true, lead, value, onChange, onSend, onEscape, onFit, onPointerDown, placeholder, label, sendLabel }: {
  className: string; formRef?: Ref<HTMLFormElement>; inputRef?: RefObject<HTMLTextAreaElement | null>; hidden?: boolean; on?: boolean;
  // Something in the row before the words (the talk area's way back to voice).
  lead?: ReactNode;
  value: string; onChange: (value: string) => void;
  // Enter or the arrow, with something to send. `onEscape`: Esc inside the field; without it Esc goes on up to whoever holds the field.
  onSend: () => void; onEscape?: () => void;
  onFit?: (height: number) => void; onPointerDown?: (event: PointerEvent<HTMLTextAreaElement>) => void;
  placeholder: string; label: string; sendLabel: string;
}) {
  const own = useRef<HTMLTextAreaElement>(null), ta = inputRef ?? own;
  const [tall, setTall] = useState(false), has = value.trim() !== '';
  useGrow(ta, value, on, h => { setTall(h > MIN); onFit?.(h); });
  const send = () => { if (has) onSend(); };
  return <form ref={formRef} className={`wf ${className}`} hidden={hidden} data-tall={tall || undefined} onSubmit={e => { e.preventDefault(); send(); }}>
    {lead}
    <textarea ref={ta} rows={1} aria-label={label} placeholder={placeholder} enterKeyHint="send" autoComplete="off" value={value}
      onChange={e => onChange(e.target.value)} onPointerDown={onPointerDown}
      onKeyDown={(e: KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Escape' && onEscape) { e.preventDefault(); onEscape(); }
        else if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); }
      }}/>
    <button type="submit" className={`send ${has ? '' : 'off'}`} disabled={!has} aria-label={sendLabel}><ArrowUp weight="bold"/></button>
  </form>;
}
