import { useEffect, useState } from 'react';

export type QuotaLayout = 'category' | 'provider' | 'accordion';
export type DashboardStyle = 'unified' | 'cards';
export const defaultPreferences = { opacity: .4, glassStrength: 1, feedbackEnabled: true, feedbackVolume: .35, themeColor: '#8be4bc', quotaLayout: 'accordion' as QuotaLayout, quotaLayoutVersion: 2, dashboardStyle: 'unified' as DashboardStyle };
type Preferences = typeof defaultPreferences;
const key = `resonance-appearance-v1${new URLSearchParams(location.search).has('lab') ? '-lab' : ''}`;
const validNumber = (value: unknown, min: number, max: number, fallback: number) =>
  typeof value === 'number' && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;

export function usePreferences() {
  const [preferences, setPreferences] = useState<Preferences>(() => {
    try {
      const value = JSON.parse(localStorage.getItem(key) ?? '{}');
      // Adopt the approved B2 default once; subsequent layout choices stay saved.
      if (value.quotaLayoutVersion !== 2) {
        value.quotaLayout = 'accordion';
        value.quotaLayoutVersion = 2;
        localStorage.setItem(key, JSON.stringify(value));
      }
      return {
        dashboardStyle: value.dashboardStyle === 'cards' ? 'cards' as const : 'unified' as const,
        quotaLayoutVersion: 2,
        quotaLayout: value.quotaLayout === 'accordion' ? 'accordion' as const : value.quotaLayout === 'provider' ? 'provider' as const : 'category' as const,
        themeColor: typeof value.themeColor === 'string' && /^#[0-9a-f]{6}$/i.test(value.themeColor) ? value.themeColor : defaultPreferences.themeColor,
        opacity: validNumber(value.opacity, .08, .65, defaultPreferences.opacity),
        glassStrength: validNumber(value.glassStrength, 0, 1, defaultPreferences.glassStrength),
        feedbackVolume: validNumber(value.feedbackVolume, 0, 1, defaultPreferences.feedbackVolume),
        feedbackEnabled: typeof value.feedbackEnabled === 'boolean' ? value.feedbackEnabled : true,
      };
    } catch { return defaultPreferences; }
  });
  useEffect(() => {
    const receive = (event: Event) => setPreferences((event as CustomEvent<Preferences>).detail);
    window.addEventListener(key, receive);
    return () => window.removeEventListener(key, receive);
  }, []);
  const update = (change: Partial<Preferences>) => {
    const next = { ...preferences, ...change };
    setPreferences(next);
    try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* Keep in-memory settings when storage is unavailable. */ }
    window.dispatchEvent(new CustomEvent(key, { detail: next }));
  };
  return [preferences, update] as const;
}
