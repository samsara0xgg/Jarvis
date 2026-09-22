import type { CSSProperties } from 'react';

// Preserve the approved green treatment without adding another settings UI.
// Change this selection to reuse the complete treatment; accent remains shared.
export const surfaceThemes = {
  black: {
    '--surface-ink': '#e7eaee', '--surface-muted': '#a1a8b6',
    '--surface-line': 'rgb(255 255 255 / .075)', '--surface-hover': 'rgb(255 255 255 / .045)',
    '--surface-rgb': '0 0 0', '--surface-drag': '#17191c',
  },
  inkGreen: {
    '--surface-ink': '#e2ebe7', '--surface-muted': '#9daea6',
    '--surface-line': 'color-mix(in srgb,var(--surface-accent) 13%,transparent)',
    '--surface-hover': 'color-mix(in srgb,var(--surface-accent) 9%,transparent)',
    '--surface-rgb': '0 0 0', '--surface-drag': '#17231d',
  },
} satisfies Record<string, CSSProperties & Record<`--${string}`, string>>;
export const activeSurfaceTheme: keyof typeof surfaceThemes = 'black';
