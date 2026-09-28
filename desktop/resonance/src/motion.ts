// Frequencies (Hz) and damping ratios shared by the companion surfaces.
export const SPRINGS = {
  control: { frequency: 4.6, damping: 1 },
  panel: { frequency: 3.2, damping: .82 },
  flight: { frequency: 2.4, damping: .75 },
} as const;
export const MOTION = { fast: 140, medium: 260, slow: 460, exit: .7, stagger: 30 } as const;
