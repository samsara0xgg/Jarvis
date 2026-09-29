#!/usr/bin/env node
// A stand-in for `codex app-server` in checks that must not reach OpenAI or spend anything: the JSON-RPC Codex's own
// desktop app uses, one object per line on stdin and stdout, as Codex 0.155 and 0.159 answer it, for what the agent
// host asks of it. Threads live in memory; a turn answers from what the message asks for:
//   FORM  an MCP server's form to fill in     LINK  an MCP server's page to open     VERIFY  an identity check
//   SLOW  three seconds of work, until interrupted
// A thread that has had a turn can be forked; a fork with instructions of its own, as a side question's, answers 侧答：<the text>.
// Its MCP servers are docs (connected, two tools), off (switched off in its config), remote (over HTTP, wants an OAuth
// sign-in) and broken (fails to start), reported as Codex reports them with and without a thread of the session's.
// Every request, and every answer to its own requests, is appended to FAKE_CODEX_LOG when it is set. The host runs it
// as `codex` on its PATH.
import { randomUUID } from 'node:crypto';
import { appendFileSync } from 'node:fs';
import { createInterface } from 'node:readline';

if (process.argv.includes('--version')) { console.log('codex-cli 9.9.9'); process.exit(0); }
const note = o => { if (process.env.FAKE_CODEX_LOG) appendFileSync(process.env.FAKE_CODEX_LOG, `${JSON.stringify({ at: Date.now(), pid: process.pid, ...o })}\n`); };
const out = m => process.stdout.write(`${JSON.stringify(m)}\n`);
const tell = (method, params) => out({ method, params });
const sleep = ms => new Promise(r => setTimeout(r, ms));
note({ ev: 'start', args: process.argv.slice(2) });

// ---------- threads and turns ----------
const threads = new Map();
const thread = (id, cwd, side = null) => ({ id, cwd, turns: 0, running: null, side });
let asked = 0;
const waiting = new Map();
function ask(method, params) {
  const id = `fake-${++asked}`;
  out({ id, method, params });
  return new Promise(resolve => waiting.set(id, resolve));
}
const FORM = { type: 'object', required: ['name'], properties: { name: { type: 'string', title: 'Name', minLength: 2 }, count: { type: 'integer', title: 'How many', minimum: 1, maximum: 5, default: 1 } } };
async function run(t, turnId, text) {
  const turn = t.running = { id: turnId, stop: false };
  tell('turn/started', { threadId: t.id, turn: { id: turnId, status: 'inProgress', items: [] } });
  tell('item/started', { threadId: t.id, turnId, item: { type: 'userMessage', id: randomUUID(), content: [{ type: 'text', text }] } });
  for (const [, what] of text.matchAll(/\b(FORM|LINK|VERIFY|SLOW)\b/g)) {
    if (what === 'SLOW') { for (let i = 0; i < 30 && !turn.stop; i++) await sleep(100); continue; }
    const base = { threadId: t.id, turnId, serverName: 'docs', _meta: null };
    const got = await ask('mcpServer/elicitation/request', what === 'FORM' ? { ...base, mode: 'form', message: 'Where should it go?', requestedSchema: FORM }
      : what === 'LINK' ? { ...base, mode: 'url', message: 'Sign in to Docs', url: 'https://docs.example.com/login', elicitationId: 'e1' }
      : { ...base, mode: 'openai/userVerification', title: 'Confirm it is you', description: 'Touch ID', challenge: 'c1' });
    note({ ev: 'elicitation', mode: what, response: got.result ?? null, error: got.error ?? null });
  }
  const item = randomUUID(), said = t.side ? `侧答：${text}` : `好的，做完了：${text}。`;
  tell('item/started', { threadId: t.id, turnId, item: { type: 'agentMessage', id: item, text: '', phase: null } });
  tell('item/agentMessage/delta', { threadId: t.id, turnId, itemId: item, delta: said });
  tell('item/completed', { threadId: t.id, turnId, item: { type: 'agentMessage', id: item, text: said, phase: 'final_answer' } });
  t.turns++; t.running = null;
  tell('turn/completed', { threadId: t.id, turn: { id: turnId, status: turn.stop ? 'interrupted' : 'completed', items: [], error: null } });
  note({ ev: 'turn end', thread: t.id, turn: turnId });
}

// ---------- MCP servers, as its config and a thread see them ----------
let signedIn = false;
function servers(threadId) {
  const live = !!threadId && threads.has(threadId), none = { tools: {}, resources: [], resourceTemplates: [], pluginId: null, httpOrigin: null, serverInfo: null, serverCapabilities: null };
  return [
    { ...none, name: 'broken', runtimeStatus: live ? 'failed' : null, toolsError: 'MCP startup failed: No such file or directory (os error 2)', authStatus: 'unsupported' },
    { ...none, name: 'docs', runtimeStatus: live ? 'connected' : null, serverInfo: { name: 'docs', version: '1.0.0' }, serverCapabilities: { tools: {} }, toolsError: null, authStatus: 'unsupported',
      tools: { publish: { name: 'publish', inputSchema: { type: 'object' } }, search: { name: 'search', inputSchema: { type: 'object' } } } },
    { ...none, name: 'off', runtimeStatus: live ? 'disabled' : null, toolsError: null, authStatus: 'unsupported' },
    signedIn ? { ...none, name: 'remote', runtimeStatus: live ? 'connected' : null, httpOrigin: 'https://remote.example.com', serverInfo: { name: 'remote', version: '2.0.0' }, toolsError: null, authStatus: 'oAuth',
      tools: { find: { name: 'find', inputSchema: { type: 'object' } } } }
      : { ...none, name: 'remote', runtimeStatus: live ? 'authenticationRequired' : null, httpOrigin: 'https://remote.example.com', toolsError: 'MCP startup failed: handshaking with MCP server failed: Auth required', authStatus: 'notLoggedIn' },
  ];
}

// ---------- requests ----------
function handle(m) {
  const p = m.params ?? {}, reply = result => out({ id: m.id, result }), refuse = (code, message) => out({ id: m.id, error: { code, message } });
  note({ ev: 'request', method: m.method, params: p });
  switch (m.method) {
    case 'initialize': return reply({ userAgent: 'fake-codex/9.9.9' });
    case 'model/list': return reply({ data: [{ id: 'fake-codex', model: 'fake-codex', displayName: 'Fake Codex', hidden: false, isDefault: true,
      supportedReasoningEfforts: [{ reasoningEffort: 'low' }, { reasoningEffort: 'high' }] }], nextCursor: null });
    case 'account/read': return reply({ account: { type: 'chatgpt', email: 'owner@example.com', planType: 'plus' } });
    case 'thread/start': { const t = thread(randomUUID(), p.cwd); threads.set(t.id, t); return reply({ thread: { id: t.id, cwd: t.cwd, turns: [] } }); }
    case 'thread/resume': { if (!threads.has(p.threadId)) threads.set(p.threadId, thread(p.threadId, p.cwd)); return reply({ thread: { id: p.threadId, turns: [] } }); }
    case 'thread/fork': {
      const from = threads.get(p.threadId);
      if (!from?.turns) return refuse(-32600, `no rollout found for thread id ${p.threadId}`);
      const t = thread(randomUUID(), from.cwd, p.developerInstructions ?? '');
      threads.set(t.id, t);
      return reply({ thread: { id: t.id, ephemeral: !!p.ephemeral, forkedFromId: from.id, turns: [] } });
    }
    case 'thread/unsubscribe': return reply({ status: 'unsubscribed' });
    case 'thread/delete': threads.delete(p.threadId); return reply({});
    case 'thread/name/set': return reply({});
    case 'thread/backgroundTerminals/list': return reply({ data: [] });
    case 'turn/start': {
      const t = threads.get(p.threadId);
      if (!t) return refuse(-32600, `thread not found: ${p.threadId}`);
      const id = randomUUID(), text = (p.input ?? []).filter(i => i.type === 'text').map(i => i.text).join('\n');
      reply({ turn: { id, status: 'inProgress', items: [] } });
      void run(t, id, text);
      return;
    }
    case 'turn/interrupt': { const t = threads.get(p.threadId); if (t?.running?.id === p.turnId) t.running.stop = true; return reply({}); }
    case 'mcpServerStatus/list': return reply({ data: servers(p.threadId), nextCursor: null });
    case 'config/mcpServer/reload': return reply({});
    case 'mcpServer/oauth/login':
      if (p.name !== 'remote') return refuse(-32600, 'OAuth login is only supported for streamable HTTP servers.');
      reply({ authorizationUrl: 'https://remote.example.com/authorize?client_id=fake' });
      void sleep(200).then(() => { signedIn = true; tell('mcpServer/oauthLogin/completed', { name: p.name, threadId: p.threadId ?? null, success: true }); });
      return;
    default: return refuse(-32601, `the stand-in does not do ${m.method}`);
  }
}
createInterface({ input: process.stdin }).on('line', l => {
  let m;
  try { m = JSON.parse(l); } catch { return; }
  if (m.method && m.id !== undefined) handle(m);
  else if (!m.method && m.id !== undefined) { note({ ev: 'answer', id: m.id, result: m.result ?? null, error: m.error ?? null }); waiting.get(m.id)?.(m); waiting.delete(m.id); }
});
process.stdin.on('end', () => { note({ ev: 'end' }); process.exit(0); });
