// An MCP server's form (C3), for both agents: the fields of an elicitation's requested schema as the window shows them,
// and what the owner filled in, checked against them before it goes back. MCP keeps forms flat: text (with a format and
// a length), numbers, yes or no, and options to pick one or several of.
import type { Field } from './types.js';

type Prop = Record<string, any>;
const str = (v: unknown) => typeof v === 'string' ? v : '';
const num = (v: unknown) => typeof v === 'number' && Number.isFinite(v) ? v : undefined;

// Options as [value, label]: consts with titles (oneOf or anyOf), or an enum with the older enumNames beside it.
function opts(p: Prop): [string, string][] | undefined {
  const list = Array.isArray(p.oneOf) ? p.oneOf : Array.isArray(p.anyOf) ? p.anyOf : null;
  if (list) return list.filter((o: Prop) => o && typeof o === 'object' && o.const !== undefined).map((o: Prop) => [String(o.const), str(o.title) || String(o.const)]);
  if (Array.isArray(p.enum)) return p.enum.map((v: unknown, i: number) => [String(v), str(p.enumNames?.[i]) || String(v)]);
  return undefined;
}

export function fieldsOf(schema: unknown): Field[] {
  const props = (schema as Prop | null)?.properties;
  if (!props || typeof props !== 'object') return [];
  const need = new Set<unknown>(Array.isArray((schema as Prop).required) ? (schema as Prop).required : []);
  return Object.entries(props as Record<string, Prop>).filter(([, p]) => p && typeof p === 'object').slice(0, 40).map(([key, p]) => {
    const f: Field = { key, title: str(p.title) || key, kind: 'text' };
    const pick = p.type === 'array' ? opts(p.items ?? {}) : opts(p);
    if (p.type === 'array') Object.assign(f, { kind: 'many', opts: pick ?? [], min: num(p.minItems), max: num(p.maxItems) });
    else if (pick) Object.assign(f, { kind: 'one', opts: pick });
    else if (p.type === 'boolean') f.kind = 'bool';
    else if (p.type === 'integer' || p.type === 'number') Object.assign(f, { kind: p.type === 'integer' ? 'int' : 'number', min: num(p.minimum), max: num(p.maximum) });
    else Object.assign(f, { min: num(p.minLength), max: num(p.maxLength), format: str(p.format) || undefined });
    if (str(p.description)) f.about = str(p.description);
    if (need.has(key)) f.need = true;
    if (p.default !== undefined) f.def = p.default;
    for (const k of ['min', 'max', 'format'] as const) if (f[k] === undefined) delete f[k];
    return f;
  });
}

// What was filled in, as the form's schema wants it, a field left out holding what it held to start with; a string
// instead says what is missing or wrong.
export function contentOf(fields: Field[], values: Record<string, unknown>): Record<string, unknown> | string {
  const out: Record<string, unknown> = {};
  const range = (f: Field, n: number, what: string) => (f.min !== undefined && n < f.min) || (f.max !== undefined && n > f.max)
    ? `「${f.title}」${what}要在 ${f.min ?? 0} 到 ${f.max ?? '∞'} 之间` : '';
  for (const f of fields) {
    const v = values[f.key] ?? f.def;
    if (v === undefined || v === null || v === '' || (Array.isArray(v) && !v.length)) {
      if (f.need) return `「${f.title}」要填`;
      continue;
    }
    if (f.kind === 'bool') {
      if (typeof v !== 'boolean') return `「${f.title}」只能是或否`;
      out[f.key] = v;
    } else if (f.kind === 'number' || f.kind === 'int') {
      const n = typeof v === 'number' ? v : Number(v);
      if (!Number.isFinite(n) || (f.kind === 'int' && !Number.isInteger(n))) return `「${f.title}」要${f.kind === 'int' ? '一个整数' : '一个数'}`;
      const bad = range(f, n, '');
      if (bad) return bad;
      out[f.key] = n;
    } else if (f.kind === 'one') {
      if (!f.opts?.some(o => o[0] === String(v))) return `「${f.title}」只能从选项里选`;
      out[f.key] = String(v);
    } else if (f.kind === 'many') {
      const vs = [...new Set((Array.isArray(v) ? v : [v]).map(String))];
      if (vs.some(x => !f.opts?.some(o => o[0] === x))) return `「${f.title}」只能从选项里选`;
      const bad = range(f, vs.length, '选的个数');
      if (bad) return bad;
      out[f.key] = vs;
    } else {
      const t = String(v);
      const bad = range(f, [...t].length, '的长度');
      if (bad) return bad;
      out[f.key] = t;
    }
  }
  return out;
}
