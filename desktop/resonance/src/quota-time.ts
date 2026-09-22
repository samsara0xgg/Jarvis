export function fmtReset(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return '—';
  const reset = new Date(iso).getTime();
  if (!Number.isFinite(reset)) return '—';
  const minutes = Math.ceil((reset - now.getTime()) / 60_000);
  if (minutes <= 0) return '等待额度更新';
  const hours = Math.floor(minutes / 60);
  return hours >= 24
    ? `resets in ${Math.floor(hours / 24)}d ${hours % 24}h`
    : hours > 0 ? `resets in ${hours}h ${minutes % 60}m` : `resets in ${minutes}m`;
}
