#!/usr/bin/env node
// A stand-in for Claude Code in checks that must not reach Anthropic or spend anything: it speaks the Agent SDK's
// stream-json protocol on stdin and stdout, keeps its transcript where Claude Code keeps one (so the SDK's own session
// functions read it), keeps file checkpoints for rewind, and answers from what the message asks for:
//   EDIT <file> · WRITE <file>  change a file in its folder (with a checkpoint)
//   ASK   a shell command that needs the owner's yes     TASK  a background shell that runs until stopped
//   SUB   a sub-agent that reads a file and reports      PLAN  a to-do list
//   AGENT a sub-agent that works for about six seconds and can be stopped on its own
//   SLOW  three seconds of work (to interrupt, queue behind)      FAIL  the turn ends in an error
//   FORM  an MCP server's form to fill in                          LINK  an MCP server's page to open
//   PICK  a question with three options to pick from (AskUserQuestion)
//   SHOT  a tool that gives back a picture (a screenshot)
//   THINK a thought that takes four seconds                        LATE  the session's name comes 1.5 s after the turn
// A question on the side (/btw) is answered on its own, after three seconds when it says SLOW, and can be cancelled.
// It has three MCP servers: docs (two tools, a moment to connect), tracker (wants a sign-in) and flaky (fails until it
// is connected again); one switched off stays off in that folder, kept in the config folder as Claude Code keeps it.
// Every start, request and turn is appended to FAKE_CLAUDE_LOG when it is set. The host runs it through
// JARVIS_AGENTS_CLAUDE.
import { randomUUID } from 'node:crypto';
import { appendFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

const argv = process.argv.slice(2);
if (argv.includes('--version')) { console.log('9.9.9 (Claude Code)'); process.exit(0); }
const arg = name => {
  const eq = argv.find(a => a.startsWith(`--${name}=`));
  if (eq) return eq.slice(name.length + 3);
  const i = argv.indexOf(`--${name}`);
  return i >= 0 ? argv[i + 1] : undefined;
};
const sid = arg('resume') ?? arg('session-id') ?? randomUUID(), cwd = process.cwd();
const CONFIG = process.env.CLAUDE_CONFIG_DIR ?? path.join(homedir(), '.claude');
const TRANSCRIPT = path.join(CONFIG, 'projects', cwd.replace(/[^a-zA-Z0-9]/g, '-'), `${sid}.jsonl`);
const HISTORY = path.join(CONFIG, 'fake-file-history', `${sid}.json`);
let model = arg('model') ?? 'fake-sonnet';

const note = o => { if (process.env.FAKE_CLAUDE_LOG) appendFileSync(process.env.FAKE_CLAUDE_LOG, `${JSON.stringify({ at: Date.now(), pid: process.pid, sid, ...o })}\n`); };
const env = process.env;
note({ ev: 'start', args: argv, cwd, key: env.ANTHROPIC_API_KEY ? `…${env.ANTHROPIC_API_KEY.slice(-4)}` : null, token: env.ANTHROPIC_AUTH_TOKEN ? 'set' : null,
  bedrock: env.CLAUDE_CODE_USE_BEDROCK ?? null, region: env.AWS_REGION ?? null, vertex: env.CLAUDE_CODE_USE_VERTEX ?? null, host: env.JARVIS_AGENTS_HOST ?? null });

const out = m => process.stdout.write(`${JSON.stringify(m)}\n`);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const text = c => typeof c === 'string' ? c : Array.isArray(c) ? c.filter(b => b?.type === 'text').map(b => b.text).join('\n') : '';

// ---------- the transcript, one entry per line, each pointing at the one before ----------
let last = null, first = true;
if (existsSync(TRANSCRIPT)) for (const l of readFileSync(TRANSCRIPT, 'utf8').split('\n')) { try { const e = JSON.parse(l); if (e.uuid && !e.isSidechain) last = e.uuid; } catch { /* not an entry */ } }
function record(e) {
  mkdirSync(path.dirname(TRANSCRIPT), { recursive: true });
  const entry = { parentUuid: last, isSidechain: false, userType: 'external', cwd, sessionId: sid, version: '9.9.9', gitBranch: 'main', timestamp: new Date().toISOString(), ...e };
  appendFileSync(TRANSCRIPT, `${JSON.stringify(entry)}\n`);
  if (entry.uuid) last = entry.uuid;
}

// ---------- file checkpoints: per message sent, each file as it was before that turn changed it ----------
let history = existsSync(HISTORY) ? JSON.parse(readFileSync(HISTORY, 'utf8')) : [];
const saveHistory = () => { mkdirSync(path.dirname(HISTORY), { recursive: true }); writeFileSync(HISTORY, JSON.stringify(history)); };
const read = f => existsSync(f) ? readFileSync(f, 'utf8') : null;
function change(file, next) {
  const turn = history.at(-1), was = read(file);
  if (turn && !(file in turn.files)) { turn.files[file] = was; saveHistory(); }
  writeFileSync(file, next);
  return was;
}
function rewind(uuid, dry) {
  const i = history.findIndex(t => t.uuid === uuid);
  if (i < 0) return { canRewind: false, error: 'No checkpoint for that message' };
  const back = {};
  for (const t of history.slice(i)) for (const [f, was] of Object.entries(t.files)) if (!(f in back)) back[f] = was;
  let insertions = 0, deletions = 0;
  const files = Object.entries(back).filter(([f, was]) => read(f) !== was);
  for (const [f, was] of files) {
    const now = (read(f) ?? '').split('\n'), then = (was ?? '').split('\n');
    insertions += then.filter(l => !now.includes(l)).length; deletions += now.filter(l => !then.includes(l)).length;
    if (!dry) { if (was === null) rmSync(f, { force: true }); else writeFileSync(f, was); }
  }
  return { canRewind: true, filesChanged: files.map(([f]) => f), insertions, deletions };
}

// ---------- talking to the SDK ----------
const waiting = new Map();
function ask(request) {
  const id = randomUUID();
  out({ type: 'control_request', request_id: id, request });
  return new Promise(resolve => waiting.set(id, resolve)).finally(() => waiting.delete(id));
}
const tasks = new Map(), sides = new Map();
const COMMANDS = [{ name: 'compact', description: 'Clear history but keep a summary', argumentHint: '<instructions>' }, { name: 'review', description: 'Review a pull request', argumentHint: '' },
  { name: 'fake-skill', description: 'A skill only the stand-in has', argumentHint: '' }];
const MODELS = [{ value: 'fake-sonnet', displayName: 'Fake Sonnet', description: 'the stand-in', supportedEffortLevels: ['low', 'medium', 'high'] },
  { value: 'fake-opus', displayName: 'Fake Opus', description: 'the other stand-in', supportedEffortLevels: ['low', 'medium', 'high', 'max'] }];
function context() {
  const cats = [['System prompt', 3000, 'used'], ['System tools', 12000, 'used'], ['Memory files', 800, 'used'], ['Messages', 4200, 'used'], ['Free space', 147000, 'free'], ['Autocompact buffer', 33000, 'buffer']];
  return { categories: cats.map(([name, tokens, kind]) => ({ name, tokens, color: 'gray', kind })), totalTokens: 20000, maxTokens: 200000, rawMaxTokens: 200000, percentage: 10, gridRows: [],
    model, memoryFiles: [{ path: path.join(cwd, 'CLAUDE.md'), type: 'Project', tokens: 800 }], mcpTools: [], agents: [{ agentType: 'Explore', source: 'built-in', tokens: 100 }],
    systemTools: [{ name: 'Bash', tokens: 3000 }, { name: 'Edit', tokens: 1500 }], isAutoCompactEnabled: true,
    messageBreakdown: { toolCallTokens: 600, toolResultTokens: 2000, attachmentTokens: 100, assistantMessageTokens: 1000, userMessageTokens: 500, redirectedContextTokens: 0, unattributedTokens: 0,
      toolCallsByType: [{ name: 'Read', callTokens: 100, resultTokens: 1500 }] } };
}
// ---------- MCP servers ----------
const MCP = path.join(CONFIG, 'fake-mcp.json'), born = Date.now(), SERVERS = { docs: 'user', tracker: 'project', flaky: 'local' };
const mcpOff = () => (existsSync(MCP) ? JSON.parse(readFileSync(MCP, 'utf8')) : {})[cwd] ?? [];
let fixed = false, since = born;
function mcpStatus() {
  const off = mcpOff(), wait = Date.now() - since < 400;
  return Object.entries(SERVERS).map(([name, scope]) => {
    if (off.includes(name)) return { name, status: 'disabled', scope, source: scope };
    if (name === 'tracker') return { name, status: 'needs-auth', scope, source: scope };
    if (name === 'flaky' && !fixed) return { name, status: 'failed', error: 'connect ECONNREFUSED 127.0.0.1:9', scope, source: scope };
    if (name === 'docs' && wait) return { name, status: 'pending', scope, source: scope };
    return { name, status: 'connected', scope, source: scope, serverInfo: { name, version: '1.0.0' },
      tools: name === 'docs' ? [{ name: 'publish' }, { name: 'search' }] : [{ name: 'retry' }] };
  });
}
function mcpToggle(name, on) {
  const all = existsSync(MCP) ? JSON.parse(readFileSync(MCP, 'utf8')) : {}, off = new Set(all[cwd] ?? []);
  if (on) { off.delete(name); since = Date.now(); } else off.add(name);
  all[cwd] = [...off];
  mkdirSync(CONFIG, { recursive: true }); writeFileSync(MCP, JSON.stringify(all));
}
const FORM = { type: 'object', required: ['name', 'count'], properties: {
  name: { type: 'string', title: 'Name', description: 'What to call it', minLength: 2, maxLength: 20 },
  count: { type: 'integer', title: 'How many', minimum: 1, maximum: 5 },
  public: { type: 'boolean', title: 'Public', default: false },
  color: { type: 'string', title: 'Color', oneOf: [{ const: 'red', title: 'Red' }, { const: 'blue', title: 'Blue' }], default: 'blue' },
  tags: { type: 'array', title: 'Tags', items: { anyOf: [{ const: 'a', title: 'A' }, { const: 'b', title: 'B' }, { const: 'c', title: 'C' }] }, maxItems: 2 } } };

function answer(m) {
  const r = m.request, reply = response => out({ type: 'control_response', response: { subtype: 'success', request_id: m.request_id, response } });
  const refuse = error => out({ type: 'control_response', response: { subtype: 'error', request_id: m.request_id, error } });
  note({ ev: 'control', subtype: r.subtype, ...r.subtype === 'initialize' ? { system: [r.systemPrompt ?? []].flat().filter(t => typeof t === 'string').join('\n') || null } : { request: r } });
  if (r.subtype === 'initialize') return reply({ commands: COMMANDS, agents: [{ name: 'Explore', description: 'Looks around' }], output_style: 'default', available_output_styles: ['default'],
    models: MODELS, account: { email: 'owner@example.com', subscriptionType: env.ANTHROPIC_API_KEY ? 'api' : 'fake', apiKeySource: env.ANTHROPIC_API_KEY ? 'ANTHROPIC_API_KEY' : 'none' }, pid: process.pid });
  // A question still out is withdrawn, as Claude Code does when a turn is interrupted.
  if (r.subtype === 'interrupt') {
    if (turn) { turn.stop = true; for (const [id, done] of waiting) { out({ type: 'control_cancel_request', request_id: id }); done(null); } }
    return reply({});
  }
  if (r.subtype === 'rewind_files') return reply(rewind(r.user_message_id, r.dry_run === true));
  if (r.subtype === 'cancel_async_message') {
    const i = queue.findIndex(q => q.uuid === r.message_uuid);
    if (i >= 0) queue.splice(i, 1);
    return reply({ cancelled: i >= 0 });
  }
  if (r.subtype === 'stop_task') {
    const t = tasks.get(r.task_id);
    if (!t) return refuse(`No task ${r.task_id}`);
    tasks.delete(r.task_id); t.stop?.();
    out({ type: 'system', subtype: 'task_notification', task_id: r.task_id, status: 'stopped', output_file: t.file, summary: 'Stopped', session_id: sid, uuid: randomUUID() });
    return reply({});
  }
  if (r.subtype === 'get_context_usage') return reply(context());
  if (r.subtype === 'mcp_status') return reply({ mcpServers: mcpStatus() });
  if (r.subtype === 'side_question') {
    out({ type: 'system', subtype: 'control_request_progress', request_id: m.request_id, status: 'started', uuid: randomUUID(), session_id: sid });
    sides.set(m.request_id, setTimeout(() => {
      sides.delete(m.request_id);
      note({ ev: 'side', question: r.question, history: r.history ?? null, during: !!turn });
      reply({ response: `侧答：${r.question}`, synthetic: false });
    }, /SLOW/.test(r.question) ? 3000 : 200));
    return;
  }
  if ((r.subtype === 'mcp_toggle' || r.subtype === 'mcp_reconnect') && !(r.serverName in SERVERS)) return refuse(`Server not found: ${r.serverName}`);
  if (r.subtype === 'mcp_toggle') { mcpToggle(r.serverName, r.enabled === true); return reply({}); }
  if (r.subtype === 'mcp_reconnect') {
    if (mcpOff().includes(r.serverName)) return refuse(`Server ${r.serverName} is disabled`);
    if (r.serverName === 'flaky') fixed = true;
    return reply({});
  }
  if (r.subtype === 'set_model') model = r.model ?? model;
  reply({});
}

// ---------- a turn ----------
let turn = null;
const queue = [];
const stream = (event, parent = null) => out({ type: 'stream_event', event, parent_tool_use_id: parent, session_id: sid, uuid: randomUUID() });
// One content block: streamed, then as an assistant message of its own; the main thread's go in the transcript. `ms`:
// how long a thought takes to stream.
async function block(b, parent = null, ms = 0) {
  if (turn.stop) throw new Error('stop');
  const uuid = randomUUID(), message = { id: `msg_${randomUUID().slice(0, 8)}`, type: 'message', role: 'assistant', model, content: [b], stop_reason: null,
    usage: { input_tokens: 1200, output_tokens: 40, cache_read_input_tokens: 18000, cache_creation_input_tokens: 800 } };
  stream({ type: 'content_block_start', index: 0, content_block: b.type === 'text' ? { type: 'text', text: '' } : b.type === 'thinking' ? { type: 'thinking', thinking: '' } : { ...b, input: {} } }, parent);
  for (let t = 0; t < ms && !turn.stop; t += 100) await sleep(100);
  if (b.type === 'text') for (const part of b.text.match(/.{1,12}/gs) ?? []) { stream({ type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: part } }, parent); await sleep(15); }
  stream({ type: 'content_block_stop', index: 0 }, parent);
  out({ type: 'assistant', message, parent_tool_use_id: parent, session_id: sid, uuid });
  if (!parent) record({ type: 'assistant', message, requestId: `req_${uuid.slice(0, 8)}`, uuid });
  await sleep(20);
}
async function result(id, content, extra = {}, parent = null, isError = false) {
  const uuid = randomUUID(), message = { role: 'user', content: [{ tool_use_id: id, type: 'tool_result', content, ...isError ? { is_error: true } : {} }] };
  out({ type: 'user', message, parent_tool_use_id: parent, session_id: sid, uuid, ...Object.keys(extra).length ? { tool_use_result: extra } : {} });
  if (!parent) record({ type: 'user', message, uuid, toolUseResult: extra });
  await sleep(20);
}
const tool = async (name, input, parent = null) => { const id = `toolu_${randomUUID().slice(0, 12)}`; await block({ type: 'tool_use', id, name, input }, parent); return id; };

// SHOT's picture: 48×32, a diagonal from warm to violet.
const SHOT = 'iVBORw0KGgoAAAANSUhEUgAAADAAAAAgCAIAAADbtmxLAAACnElEQVR4nM3U+TdUcRzG8c9/VolEq0ikRSQSiYTZ7sy9d/btLnPn3rmzGsswxjKGktIqaZHSIilJK2mRtEjbufNr53T6wdH3P3if1znPA7+6a3521XyPyZaissUO+dd2+Zc2xaeIYqFFMR9WfmhWzoVU7xpVbxtUs/XY6yA2U6t+FVC/9GueezXPPJopN/5ExCcFYoInHjmJcY58wJJjjHaU1t6jdHdsuttW3YhFf9OsHzYahgyG63rDNZ3xCmkcJEwDuOmSxtyPmS+ozOeVlrNyyxmZFX7EZEudsm9R2aKUIv/cqliIKD6GlfPNyrkm5fuQlPKmHputw2aC2LSUon7hk2qeuvEpEZ904Y8FYsJJPOTIcQc5xpL3ae0opb1rl2pGLPpbZv2wSX/DaBiKp1zVGgdJ42XcNKAx9avNFzEp5ZxCqumTWU9XWwERmL4a66lqa2+VDRCB6a2ynTxm66m0wd9hplcKpqfSduKovbvCDojAHK+wd5fbY0coQASmq5yKlVHRwxQgAtNZRkVL6fYSGv7LlP6E6Sil20ro1mIGEIFpO0RHipmWIgYQgYkcZMJFTHMhC4jAhAvZpgNsqIAFRGBCBWzjfkdDvgMQgWnId9TnOYL7OEAEpi6PC+Zygb0cIAJTm8sF9jh9u52wDDCKZYDxx2s8OTwgAuPd5XTn8GI2D4jAuHfyrmxeyBIAERhXlsDvEJyZAiACw2cK3HaXI8MFiMA4MlxsuovZJgIiMEy6SKeJ1FYREIGh0kT7Frd1sxtW4Hz/BcYWrzFv9AAiMJZNUo0x1QOIwJg2eAypHn2KFxCBMaR4deu92mQvIAKjS/aS63xEkg8QgSGTfHiiT7PWD4jAaBL96gQ/tsYPiMBgCX7V6oByVeA3K0OO46WieP4AAAAASUVORK5CYII=';
const THOUGHT = '**Weighing where the switch belongs**\n\nThe setting is written to settings.json, but the menu bar item reads it only once, at launch.\nWith the menu gone, nothing tells the companion the value changed.\n\nTwo ways: the companion watches settings.json, or the settings page sends it a message.\nThe settings module already has onDidChange, which costs the least and leaves the page alone.\n\nFirst check the key the page writes, then find who else reads it.\nThen change the companion to redraw her mark when the value moves.\nLast, run the companion check again.';
async function work(said) {
  for (const [, what, arg1] of said.matchAll(/\b(EDIT|WRITE|ASK|TASK|SUB|AGENT|PLAN|SLOW|FAIL|FORM|LINK|PICK|SHOT|THINK)\b(?:\s+([\w./-]+))?/g)) {
    if (what === 'THINK') { await block({ type: 'thinking', thinking: THOUGHT, signature: 'x' }, null, 4000); continue; }
    if (what === 'SLOW') { for (let i = 0; i < 30 && !turn.stop; i++) await sleep(100); if (turn.stop) throw new Error('stop'); continue; }
    if (what === 'FAIL') return 'fail';
    if (what === 'EDIT' || what === 'WRITE') {
      const file = path.resolve(cwd, arg1 ?? 'notes.txt'), before = read(file);
      if (what === 'WRITE' || before === null) {
        const content = `written by the stand-in\n`, id = await tool('Write', { file_path: file, content });
        change(file, content);
        await result(id, `File created successfully at: ${file}`, { type: 'create', filePath: file, content, structuredPatch: [] });
      } else {
        const old = before.trimEnd().split('\n').at(-1), next = `${old}\nedited by the stand-in`, id = await tool('Edit', { file_path: file, old_string: old, new_string: next });
        change(file, before.replace(old, next));
        await result(id, `The file ${file} has been updated.`, { filePath: file, oldString: old, newString: next, originalFile: before,
          structuredPatch: [{ oldStart: 1, oldLines: 1, newStart: 1, newLines: 2, lines: [` ${old}`, '+edited by the stand-in'] }] });
      }
    }
    if (what === 'ASK') {
      const input = { command: 'echo asked', description: 'Say asked' }, id = await tool('Bash', input);
      const got = await ask({ subtype: 'can_use_tool', tool_name: 'Bash', input, tool_use_id: id, title: 'Say asked',
        permission_suggestions: [{ type: 'addRules', rules: [{ toolName: 'Bash', ruleContent: 'echo asked' }], behavior: 'allow', destination: 'localSettings' }] });
      if (turn.stop) throw new Error('stop');
      const ok = got?.response?.behavior === 'allow';
      note({ ev: 'permission', behavior: got?.response?.behavior, updatedPermissions: got?.response?.updatedPermissions ?? null });
      await result(id, ok ? 'asked' : `Permission denied: ${got?.response?.message ?? ''}`, ok ? { stdout: 'asked', stderr: '', interrupted: false } : {}, null, !ok);
    }
    if (what === 'PICK') {
      const input = { questions: [{ question: 'Which one first?', header: 'Order', multiSelect: false,
        options: [{ label: 'Red', description: 'the warm one' }, { label: 'Green', description: 'the calm one' }, { label: 'Blue', description: 'the cool one' }] }] };
      const id = await tool('AskUserQuestion', input), got = await ask({ subtype: 'can_use_tool', tool_name: 'AskUserQuestion', input, tool_use_id: id });
      if (turn.stop) throw new Error('stop');
      const ok = got?.response?.behavior === 'allow';
      note({ ev: 'question', behavior: got?.response?.behavior, answers: got?.response?.updatedInput?.answers ?? null });
      await result(id, ok ? JSON.stringify(got.response.updatedInput?.answers ?? {}) : `Permission denied: ${got?.response?.message ?? ''}`, {}, null, !ok);
    }
    if (what === 'TASK') {
      const id = await tool('Bash', { command: 'sleep 100', description: 'Wait in the background', run_in_background: true }), task = `b${randomUUID().slice(0, 7)}`;
      const file = path.join(CONFIG, 'fake-tasks', `${task}.output`);
      mkdirSync(path.dirname(file), { recursive: true });
      writeFileSync(file, 'waiting in the background\nstill waiting\n');
      // It keeps writing while it runs, as a dev server or a long test would.
      let n = 0;
      const beat = setInterval(() => appendFileSync(file, `tick ${++n}\n`), 400);
      tasks.set(task, { file, stop: () => clearInterval(beat) });
      out({ type: 'system', subtype: 'task_started', task_id: task, tool_use_id: id, description: 'Wait in the background', task_type: 'local_bash', session_id: sid, uuid: randomUUID() });
      await result(id, `Command running in background with ID: ${task}. Output is being written to: ${file}.`, { stdout: '', stderr: '', backgroundTaskId: task });
    }
    if (what === 'AGENT') {
      // Claude Code keeps a sub-agent the turn waits on as a task of its own; stopping that task ends only the sub-agent.
      const id = await tool('Agent', { subagent_type: 'Explore', description: 'Find the readme', prompt: 'Find the readme and say what it holds' });
      const task = `a${randomUUID().slice(0, 7)}`, sub = { stop: false };
      tasks.set(task, { stop: () => { sub.stop = true; } });
      out({ type: 'system', subtype: 'task_started', task_id: task, tool_use_id: id, description: 'Find the readme', subagent_type: 'Explore', task_type: 'local_agent', is_backgrounded: false, session_id: sid, uuid: randomUUID() });
      out({ type: 'user', message: { role: 'user', content: [{ type: 'text', text: 'Find the readme and say what it holds' }] }, parent_tool_use_id: id, session_id: sid, uuid: randomUUID() });
      const readme = path.join(cwd, 'README.md'), code = path.join(cwd, 'src', 'a.ts');
      const steps = [
        () => block({ type: 'thinking', thinking: 'The readme should be at the top of the folder.', signature: 'x' }, id),
        async () => { const r = await tool('Glob', { pattern: '*.md' }, id); await result(r, 'README.md', {}, id); },
        async () => { const r = await tool('Read', { file_path: readme }, id); await result(r, read(readme) ?? 'no readme', {}, id); },
        async () => { const r = await tool('Read', { file_path: code }, id); await result(r, read(code) ?? '', {}, id); },
        () => block({ type: 'text', text: 'The readme is a short list, and src/a.ts holds two constants.' }, id),
      ];
      for (const step of steps) {
        for (let t = 0; t < 1200 && !sub.stop && !turn.stop; t += 100) await sleep(100);
        if (sub.stop || turn.stop) break;
        await step();
      }
      if (turn.stop) throw new Error('stop');
      if (sub.stop) await result(id, 'The sub-agent was stopped before it finished.', { status: 'stopped' }, null, true);
      else {
        tasks.delete(task);
        out({ type: 'system', subtype: 'task_notification', task_id: task, tool_use_id: id, status: 'completed', output_file: '', summary: 'Found the readme', session_id: sid, uuid: randomUUID() });
        await result(id, [{ type: 'text', text: 'The readme is a short list: one, two. `src/a.ts` holds two constants.' }], { status: 'completed' });
      }
    }
    if (what === 'SUB') {
      const id = await tool('Agent', { subagent_type: 'Explore', description: 'Look around', prompt: 'Look around this folder' });
      out({ type: 'user', message: { role: 'user', content: [{ type: 'text', text: 'Look around this folder' }] }, parent_tool_use_id: id, session_id: sid, uuid: randomUUID() });
      await block({ type: 'thinking', thinking: 'Reading the readme first.', signature: 'x' }, id);
      const r = await tool('Read', { file_path: path.join(cwd, 'README.md') }, id);
      await result(r, read(path.join(cwd, 'README.md')) ?? 'no readme', {}, id);
      await block({ type: 'text', text: 'The folder has a readme.' }, id);
      await result(id, [{ type: 'text', text: 'The folder has a readme.' }], { status: 'completed' });
    }
    if (what === 'FORM' || what === 'LINK') {
      const id = await tool('mcp__docs__publish', { title: 'x' });
      const got = await ask(what === 'FORM' ? { subtype: 'elicitation', mcp_server_name: 'docs', message: 'Where should it go?', mode: 'form', requested_schema: FORM }
        : { subtype: 'elicitation', mcp_server_name: 'docs', message: 'Sign in to Docs', mode: 'url', url: 'https://docs.example.com/login', elicitation_id: 'e1' });
      if (turn.stop) throw new Error('stop');
      note({ ev: 'elicitation', response: got?.response ?? null });
      await result(id, JSON.stringify(got?.response ?? null), {});
    }
    if (what === 'SHOT') {
      const id = await tool('mcp__browser__screenshot', {});
      await result(id, [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data: SHOT } }]);
    }
    if (what === 'PLAN') {
      const id = await tool('TodoWrite', { todos: [{ content: 'Read the code', status: 'completed', activeForm: 'Reading' }, { content: 'Change it', status: 'in_progress', activeForm: 'Changing' }] });
      await result(id, 'Todos have been modified successfully');
    }
  }
  return 'ok';
}
async function run(m) {
  // A one-shot query's message comes with no id of its own.
  m.uuid ??= randomUUID();
  turn = { uuid: m.uuid, stop: false };
  const t0 = Date.now(), said = text(m.message?.content);
  note({ ev: 'turn', uuid: m.uuid, text: said, model, blocks: Array.isArray(m.message?.content) ? m.message.content.map(b => b.type) : ['text'] });
  out({ type: 'command_lifecycle', state: 'started', command_uuid: m.uuid, session_id: sid, uuid: randomUUID() });
  if (first) {
    first = false;
    out({ type: 'system', subtype: 'init', cwd, session_id: sid, tools: ['Bash', 'Edit', 'Read', 'Write', 'Agent', 'TodoWrite'], mcp_servers: [], model, permissionMode: arg('permission-mode') ?? 'default',
      slash_commands: COMMANDS.map(c => c.name), apiKeySource: env.ANTHROPIC_API_KEY ? 'ANTHROPIC_API_KEY' : 'none', claude_code_version: '9.9.9', output_style: 'default', agents: ['Explore'], skills: [], plugins: [], uuid: randomUUID() });
  }
  record({ type: 'user', message: m.message, uuid: m.uuid });
  history.push({ uuid: m.uuid, files: {} }); saveHistory();
  let how = 'ok';
  try {
    stream({ type: 'message_start', message: { id: `msg_${m.uuid.slice(0, 8)}`, type: 'message', role: 'assistant', model, content: [], usage: { input_tokens: 1200, output_tokens: 0 } } });
    await block({ type: 'thinking', thinking: `**Reading the ask**\n\nThe owner wrote: ${said.slice(0, 200)}`, signature: 'x' });
    how = await work(said);
    if (how === 'ok') await block({ type: 'text', text: `好的，做完了：${said.slice(0, 80)}。\n\n第二段在这里。` });
  } catch (e) {
    if (String(e.message) !== 'stop') throw e;
    how = 'stop';
  }
  const common = { duration_ms: Date.now() - t0, duration_api_ms: Date.now() - t0, num_turns: 1, session_id: sid, total_cost_usd: 0, uuid: randomUUID(), permission_denials: [], user_message_uuid: m.uuid,
    usage: { input_tokens: 1200, output_tokens: 40, cache_read_input_tokens: 18000, cache_creation_input_tokens: 800 },
    modelUsage: { [model]: { inputTokens: 1200, outputTokens: 40, cacheReadInputTokens: 18000, cacheCreationInputTokens: 800, webSearchRequests: 0, costUSD: 0, contextWindow: 200000, maxOutputTokens: 32000 } } };
  if (how === 'stop') {
    record({ type: 'user', message: { role: 'user', content: [{ type: 'text', text: '[Request interrupted by user]' }] }, uuid: randomUUID() });
    out({ type: 'result', subtype: 'error_during_execution', is_error: false, stop_reason: null, terminal_reason: 'aborted_streaming', errors: [], ...common });
  } else if (how === 'fail') out({ type: 'result', subtype: 'error_during_execution', is_error: true, stop_reason: null, errors: ['the stand-in was told to fail'], ...common });
  else {
    out({ type: 'result', subtype: 'success', is_error: false, stop_reason: 'end_turn', terminal_reason: 'completed', result: `好的，做完了：${said.slice(0, 80)}。`, ...common });
    // Claude Code names the session after its first exchange, asking for the name on the side: with LATE it comes after
    // the turn has ended.
    const titled = path.join(CONFIG, 'fake-titled', sid);
    if (!existsSync(titled)) {
      const name = () => appendFileSync(TRANSCRIPT, `${JSON.stringify({ type: 'ai-title', aiTitle: `Stand-in: ${said.slice(0, 30)}`, sessionId: sid })}\n`);
      if (/\bLATE\b/.test(said)) setTimeout(name, 1500); else name();
      mkdirSync(path.dirname(titled), { recursive: true }); writeFileSync(titled, '');
    }
  }
  note({ ev: 'turn end', uuid: m.uuid, how });
  turn = null;
  if (queue.length) void run(queue.shift());
}

// ---------- stdin: control requests, answers to ours, and messages ----------
let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', d => {
  buf += d;
  for (let i; (i = buf.indexOf('\n')) >= 0;) {
    const l = buf.slice(0, i); buf = buf.slice(i + 1);
    if (!l.trim()) continue;
    let m;
    try { m = JSON.parse(l); } catch { continue; }
    if (m.type === 'control_request') answer(m);
    else if (m.type === 'control_response') waiting.get(m.response?.request_id)?.(m.response);
    else if (m.type === 'control_cancel_request' && sides.has(m.request_id)) { clearTimeout(sides.get(m.request_id)); sides.delete(m.request_id); note({ ev: 'side cancelled' }); }
    else if (m.type === 'user') { if (turn) { queue.push(m); note({ ev: 'queued', uuid: m.uuid }); } else void run(m); }
  }
});
process.stdin.on('end', () => { note({ ev: 'end' }); process.exit(0); });
