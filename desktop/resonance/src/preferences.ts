import { useEffect, useState } from 'react';

export const defaultPreferences = { opacity: .4, glassStrength: 1, feedbackEnabled: true, feedbackVolume: .35, themeColor: '#8be4bc' };
type Preferences = typeof defaultPreferences;
const key = `resonance-appearance-v1${new URLSearchParams(location.search).has('lab') ? '-lab' : ''}`;
const validNumber = (value: unknown, min: number, max: number, fallback: number) =>
  typeof value === 'number' && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;

export function usePreferences() {
  const [preferences, setPreferences] = useState<Preferences>(() => {
    try {
      const value = JSON.parse(localStorage.getItem(key) ?? '{}');
      return {
        themeColor: typeof value.themeColor === 'string' && /^#[0-9a-f]{6}$/i.test(value.themeColor) ? value.themeColor : defaultPreferences.themeColor,
        opacity: validNumber(value.opacity, .08, .65, defaultPreferences.opacity),
        glassStrength: validNumber(value.glassStrength, 0, 1, defaultPreferences.glassStrength),
        feedbackVolume: validNumber(value.feedbackVolume, 0, 1, defaultPreferences.feedbackVolume),
        feedbackEnabled: typeof value.feedbackEnabled === 'boolean' ? value.feedbackEnabled : true,
      };
    } catch { return defaultPreferences; }
  });
  useEffect(() => { try { localStorage.setItem(key, JSON.stringify(preferences)); } catch { /* Keep in-memory settings when storage is unavailable. */ } }, [preferences]);
  const update = (change: Partial<Preferences>) => setPreferences(p => ({ ...p, ...change }));
  return [preferences, update] as const;
}
