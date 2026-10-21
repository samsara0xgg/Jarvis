import { useCallback, useEffect, useRef, useState } from 'react';
import { poll } from './poll';

export type Plugin = {
  id: string; name: string; description: string; capabilities: string[]; skill_count: number;
  supported: boolean; unavailable_reason: string | null; enabled: boolean; status: string; error: string | null;
  auth: string; credential_fields: string[]; credentials_saved: boolean; approval_mode: string;
  tools: { name: string; description: string; read_only: boolean; requires_confirmation: boolean }[];
};
export type PluginRequest = {
  id: string; plugin_id: string; presentation: number; purpose: string; continue_task: boolean;
  state: 'offered' | 'connecting' | 'authorizing' | 'ready' | 'error' | 'cancelled';
  error: string | null; resume_status: string; auth_url?: string; // the sign-in link, only while state is 'authorizing' (ADR 0202)
};
export type PluginSnapshot = { plugins: Plugin[]; request: PluginRequest | null };
export const cleanError = (error: unknown) => String(error instanceof Error ? error.message : error).replace(/^Error invoking remote method '[^']+': (?:Error: )?/, '');

export function usePlugins() {
  const [snapshot, setSnapshot] = useState<PluginSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const sequence = useRef(0), applied = useRef(0), mutation = useRef(false);
  const alive = useRef(true);
  const failed = useRef(false); // the last read failed: the poll slows down (the plugin service may refuse it for good)
  const load = useCallback(async () => {
    if (!window.jarvis?.plugins || mutation.current) return;
    const seq = ++sequence.current;
    try {
      const next = await window.jarvis.plugins('read') as PluginSnapshot;
      failed.current = false;
      if (alive.current && seq > applied.current) { applied.current = seq; setSnapshot(next); setError(null); }
    } catch (e) { failed.current = true; if (alive.current && seq > applied.current) { applied.current = seq; setError(cleanError(e)); } }
  }, []);
  useEffect(() => {
    alive.current = true;
    const stop = poll(async () => { await load(); return failed.current; });
    return () => { alive.current = false; stop(); };
  }, [load]);
  const action = useCallback(async (operation: string, data: Record<string, unknown> = {}) => {
    if (!window.jarvis?.plugins || mutation.current) return false;
    mutation.current = true; setBusy(true); setError(null);
    const seq = ++sequence.current; applied.current = seq;
    try {
      const next = await window.jarvis.plugins(operation, data) as PluginSnapshot;
      if (alive.current) setSnapshot(next);
      return true;
    } catch (e) { if (alive.current) setError(cleanError(e)); return false; }
    finally { mutation.current = false; if (alive.current) setBusy(false); }
  }, []);
  // ADR 0202: the sign-in link opens on this device the first time a request shows it (the main process keeps
  // the once-per-request count, so a second window or a hidden panel does not open it again).
  const request = snapshot?.request, opened = useRef('');
  useEffect(() => {
    if (request?.state !== 'authorizing' || !request.auth_url || opened.current === request.id) return;
    opened.current = request.id;
    void window.jarvis?.openLogin?.(request.auth_url, request.id);
  }, [request?.id, request?.state, request?.auth_url]);
  return { snapshot, error, busy, action, refresh: load };
}

// A plugin's own logo from its manifest, fetched once per session rather than on every poll.
const icons = new Map<string, Promise<string | null>>();
// Icons Allen picked for packages that ship no logo, and for connectors with no package, by plugin id.
const PICKED: Record<string, string> = Object.fromEntries(Object.entries(import.meta.glob<string>('./plugin-icons/*', { eager: true, import: 'default' }))
  .map(([file, url]) => [file.replace(/^.*\/|\.\w+$/g, ''), url]));
export function usePluginIcon(id: string) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    if (!window.jarvis?.plugins) return;
    let live = true;
    if (!icons.has(id)) icons.set(id, window.jarvis.plugins('icon', { plugin_id: id }).then(r => (r as unknown as { icon: string | null }).icon, () => { icons.delete(id); return null; }));
    void icons.get(id)!.then(icon => { if (live) setSrc(icon); });
    return () => { live = false; };
  }, [id]);
  return src ?? PICKED[id] ?? null;
}
