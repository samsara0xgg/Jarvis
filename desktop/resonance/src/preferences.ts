import { useEffect, useState } from 'react';

const defaultPreferences = { feedbackEnabled: true, feedbackVolume: .35 };
type Preferences = typeof defaultPreferences;
const key = 'resonance-appearance-v1';
const validNumber = (value: unknown, min: number, max: number, fallback: number) =>
  typeof value === 'number' && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;

function loadPreferences(): Preferences {
    try {
      const value = JSON.parse(localStorage.getItem(key) ?? '{}');
      return {
        feedbackVolume: validNumber(value.feedbackVolume, 0, 1, defaultPreferences.feedbackVolume),
        feedbackEnabled: typeof value.feedbackEnabled === 'boolean' ? value.feedbackEnabled : true,
      };
    } catch { return defaultPreferences; }
}
export function usePreferences() {
  const [preferences, setPreferences] = useState<Preferences>(loadPreferences);
  useEffect(() => {
    const receive = (event: Event) => setPreferences((event as CustomEvent<Preferences>).detail);
    const shared = (event: StorageEvent) => { if (event.key === key) setPreferences(loadPreferences()); };
    window.addEventListener(key, receive); window.addEventListener('storage', shared);
    return () => { window.removeEventListener(key, receive); window.removeEventListener('storage', shared); };
  }, []);
  const update = (change: Partial<Preferences>) => {
    const next = { ...preferences, ...change };
    setPreferences(next);
    try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* Keep in-memory settings when storage is unavailable. */ }
    window.dispatchEvent(new CustomEvent(key, { detail: next }));
  };
  return [preferences, update] as const;
}
