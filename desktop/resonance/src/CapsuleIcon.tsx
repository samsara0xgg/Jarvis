import type { SVGProps } from 'react';
import { bellPath } from './bellPath';
import { capsulePaths } from './capsulePaths';
type Name = 'compose' | 'microphone' | 'microphone-off' | 'speaker' | 'speaker-off' | 'bell' | 'collapse' | 'close' | 'plus' | 'send' | 'reply';
export function CapsuleIcon({ name, ...props }: SVGProps<SVGSVGElement> & { name: Name }) {
  const path = name === 'bell' ? bellPath : capsulePaths[name];
  return <svg width="20" height="20" viewBox="0 0 40 40" fill="currentColor" aria-hidden="true" data-capsule-icon={name} {...props}>
    {path ? <path d={path} fillRule="evenodd"/> : <g fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
      {name === 'close' && <path d="m12 12 16 16M28 12 12 28"/>}
      {name === 'reply' && <path d="M32 10v8c0 4-2 6-6 6H10m7-7-7 7 7 7"/>}
      {name === 'send' && <path d="M20 31V9m-8 8 8-8 8 8"/>}
    </g>}
  </svg>;
}
