import { useEffect, useRef } from 'react';
import { ArrowUp, Check, X } from '@phosphor-icons/react';
import type { L } from './companionSettings';
import type { Ask } from './dayOpen';
import { Ic } from './dayIcons';

// The input at the pane's foot (ADR 0296, spec-r3 section 8): what the turn is about as a chip (its kind's glyph, its title, its time; the
// × lets go of it and the turn is then asked alone), a field with the cursor in it, and send. Enter sends (not while an input method is
// still composing). The chip's × and the send button do not take the cursor from the field. `nonce` changes each time a row asks again, so
// the cursor comes back to the field when the chip is swapped for another.
export type DockState = 'idle' | 'busy' | 'sent' | 'failed';
export function AskDock({ about, draft, state, nonce, onDraft, onSend, onDrop, onField, t }: { about: Ask | null; draft: string; state: DockState; nonce: number; onDraft: (text: string) => void; onSend: () => void; onDrop: () => void; onField: (on: boolean) => void; t: (l: L) => string }) {
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const focus = () => field.current?.focus({ preventScroll: true });
    focus();
    const frame = requestAnimationFrame(focus);
    return () => cancelAnimationFrame(frame);
  }, [nonce, state === 'sent']);
  const keep = (e: { preventDefault: () => void }) => e.preventDefault();
  if (state === 'sent') return <div className="dt-dock is-sent" role="status"><Check size={13} weight="bold" aria-hidden="true"/>{t(['Sent to her', '已发给她'])}</div>;
  return <>
    <div className="dt-dock">
      {about && <span className="dt-about" title={about.about.title}><Ic icon={about.icon} size={12}/><b>{about.about.title}</b><time>{about.at}</time>
        <button type="button" aria-label={t(['Take it off', '去掉这一条'])} onMouseDown={keep} onClick={onDrop}><X size={9} weight="bold"/></button></span>}
      <input ref={field} className="dt-ask-in" type="text" autoComplete="off" spellCheck={false} aria-label={t(['Ask her', '问她'])} value={draft} readOnly={state === 'busy'}
        placeholder={about ? t(['Ask her about this…', '问她这一条…']) : t(['Ask her…', '问她…'])}
        onChange={e => onDraft(e.target.value)} onFocus={() => onField(true)} onBlur={() => onField(false)}
        onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); onSend(); } }}/>
      <button type="button" className="dt-send" aria-label={t(['Send', '发送'])} disabled={!draft.trim() || state === 'busy'} onMouseDown={keep} onClick={onSend}><ArrowUp size={13} weight="bold"/></button>
    </div>
    {state === 'failed' && <p className="dt-hint is-bad" role="alert">{t(["Didn't send. Press Enter to try again.", '没发出去，再按一次回车试试'])}</p>}
  </>;
}
