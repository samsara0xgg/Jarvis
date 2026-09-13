# ADR-0016 — Live Voice Provider and Delegation Bridge

**Status:** Approved (2026-09-12, Allen: "没问题，开始写吧" on the phase B plan; direct playback and "允许重新设计" granted earlier the same day, recorded in `docs/gpt-live-integration-planning.md` §1.1)
**Date:** 2026-09-12
**Depends on:** ADR-0005 (voice foundation, single audio ingress), ADR-0009 (resident daemon), ADR-0011 (tool surface and caller principals), ADR-0014 D21 (idempotent input inbox), ADR-0015 (Resonance surface and mute-as-gain).
**Supersedes:** the hosted speech-to-speech non-goals in ADR-0006 ("Does not use: OpenAI Realtime API or any hosted speech-to-speech runtime"), ADR-0008 §Non-goals ("Hosted speech-to-speech or OpenAI Realtime API integration") and ADR-0014 §Non-goals ("Integrating OpenAI Realtime API or any hosted speech-to-speech session"). Everything else in those ADRs stands; the local ASR → L3 → TTS chain remains the default and the fallback.
**Amends:** spec §3.6.5 Output: Live native audio plays without the pre-emit gate (see D1).
**Number note:** 0004, 0007, 0010 and 0013 stay reserved; this ADR takes the next free number.

## 1. Context

- GPT-Live (`gpt-live-1`, `/v1/live/sessions`) is a native full-duplex voice
  model: it listens, decides when to speak, and speaks. It is not the old
  Realtime API with a new model name. Its wire has no turn boundaries, no
  output-audio-done event and no remote stop; the application feeds it context
  through three append events (`instructions`, `thinking`, `commentary`), each
  a plain string of at most 500 tokens.
- Phase A (`74b83d2` … `58029a1`, live-tested 2026-09-12) put the session
  behind Resonance's live toggle: the daemon keeps the microphone and the
  speaker, tees the 16 kHz ingress to the socket, plays returned PCM through
  one `AudioStreamPlayer`, shows captions, and gates playback locally on
  "别说了". Nothing in phase A touches L3: a `session.delegation.created`
  is answered with a fixed "没接后台" commentary, and the Live conversation
  is invisible to memory.db and to the backend.
- The backend already has the whole text path: one `surface.user_intent` row
  drives `drive_turn` (L3 decide loop, L4 tools, ADR-0011 policy), the answer
  is the `surface.response_emitted` row, and ADR-0014 D21 gives an idempotent,
  persisted submission receipt keyed by `(principal, client_instance_id,
  request_id)`.
- Allen's stated goal: changing the voice provider must not lose memory,
  interrupt tasks, change permissions, re-run tools or desynchronize the UI.
  The change should show up as voice and feel, not as a second Jarvis.

## 2. Decisions

**D1. Live native audio plays directly.** Output audio from the Live session
goes to the local player as it arrives. It is not gated by the L3 pre-emit
gate and no ResponsePlan is minted for ordinary Live speech. The gate keeps
its job for everything Jarvis itself asserts: backend answers still pass
through L3 before they are handed to Live, and Live only paraphrases what the
backend returned. Consequence accepted: the words Allen hears are the model's
own; claims Live makes on its own are not Jarvis claims and are not recorded
as such (see D3).

**D2. Client delegation is the only bridge from Live to the backend.** The
session is created in client delegation mode. On `session.delegation.created`
the L5 session records a pending delegation keyed by
`(session_id, delegation_id)`, waits a bounded settle window for user
transcript fragments, builds the request from the user fragments in the
session-timeline window `(previous delegation offset, this offset + settle]`,
and submits it through the ADR-0014 D21 inbox with `client_instance_id =
Live session id` and `request_id = delegation_id`. The inbox is the durable
receipt: a redelivered event replays the same `turn_id` and starts no second
turn. The request text is frozen in the pending record so a replay can never
hit `PayloadConflictError`. Transcript fragments alone never start a turn.
Ordinary conversation never enters a ResponseRun.

**D3. The Live conversation is persisted to memory.db, with provenance.**
User fragments are merged on pauses and appended as `source=allen`; the
model's spoken output as `source=jarvis_live`, and nothing the model says
while playback is hushed is recorded, because Allen did not hear it. The
delegated request row is written by L5 before submission and its `record_id`
travels on the `surface.user_intent` payload; `drive_turn` then skips its own
`allen` write and passes that id as `exclude_id` to `context_note`, so the
request appears once in the prompt. The backend's full answer keeps its
existing `source=jarvis` row. memory.db remains the conversation of record;
the provider's own storage stays off (`store: false`).

**D4. Results go back as facts, sized for speech, only when they still
apply.** At submission the bridge sends one `session.thinking.append`
progress fact. The answer is sent as `session.commentary.append` with the
delegation id, using `voice_text` (falling back to `text`), budgeted to about
300 Chinese characters and cut at a sentence end; when no short form exists
the commentary states the status and that the full result is on the UI.
Before sending, the bridge checks that the pending record's `session_id` and
epoch match the current run; otherwise the result stays in memory.db and the
next session's brief. If playback is hushed, the result goes as
`thinking`, not `commentary`. The UI keeps the complete answer through the
existing response stream; Live never receives it.

**D5. Three-state outcome, one foreground query.** A `response.failed` or
`turn.failed` terminal yields a "查询失败" commentary; no answer within
`delegation_timeout_s` (default 90 s) yields "还没拿到结果"; an answer that
arrives after the timeout is appended as `thinking` only. When a new
delegation is registered, every older pending delegation is demoted to
`thinking` delivery: its turn still completes and the UI still shows it, but
only the newest query is spoken. Semantic task revision (planning §6) is
not attempted here.

**D6. Delegated turns see only read-only tools.** When
`surface.user_intent.channel == "gpt_live"`, `drive_turn` hands `decide()` a
registry view that exposes only `read_only=True` tools (and never
`cancel_action`), forwards `dispatch` for those and raises `UnknownToolError`
for anything else. The shared `runtime.tool_registry` is untouched. Mutating
tools and the ADR-0012 confirmation flow are phase C.

**D7. The session brief is the existing memory note, budgeted.** The
`session.start` `input` carries one `developer` message rendered by a
character-budgeted variant of `context_note`: the whole profile, then the
most recent records selected from the newest backwards and emitted in time
order, about 1 500 characters. No new schema, no separate memory service.

**D8. The local speech chain stays silent for Live turns, by response.**
A turn that originates from Live is marked silent for the TTS consumer at its
`surface.response_open` header, the same per-response mechanism ADR-0009 D4
uses for silent attention channels, so neither the answer nor the lifecycle
commentary of that turn is synthesized. Muting the player gain (phase A) is
kept as the last line, not the mechanism.

**D9. Layering is unchanged.** `jarvis/surface/voice_live.py` is L5 and
imports nothing from `state`, `decision` or `runtime`. The composition root
injects four callables: `delegate(text, delegation_id, session_id,
record_id) -> turn_id`, `record(source, text, record_id)`, `brief() -> str`
and `lookup_result(turn_id)`; it also subscribes to the committed-event bus
and wakes the pending delegation when a response terminal for its `turn_id`
commits. The bus callback only enqueues; the Live session task reads the
Event Log and owns the WebSocket. `lint-imports` remains the gate.

## 3. Consequences

- Allen can ask Live to look something up, keep talking while it runs, hear
  the newest answer, hush it, and find the full result on the UI and in
  memory.db. Reconnecting starts a session that already knows what happened
  through the brief; cross-session delivery of a pending result to a new Live
  session is not provided.
- Two "jarvis" voices exist in memory.db: what the backend found
  (`jarvis`) and what Live actually said (`jarvis_live`). Both are true;
  they differ on purpose.
- Live-side claims made without delegation are outside the pre-emit gate.
  The prompt confines Live to conversation and to delegating lookups; this
  is behavioural guidance, not a gate, and the ADR does not pretend
  otherwise.
- `context_days` full-text injection now includes Live conversation, which
  grows the L3 prompt; unchanged policy, larger input.

## 4. Non-goals

Semantic revision and stale-result suppression across independent queries,
side-effecting tools and confirmations from Live, resuming a pending result
in a new session, a provider abstraction or `LocalChainedVoiceProvider`,
WebRTC, provider storage and forks.

## 5. Definition of done

A live run recorded in `docs/live-burn-2026-09-12-gpt-live-phase-b.md`
with, per decision: the daemon log lines for claim, submit, `turn_id`, ACK
by `client_event_id` and delivery kind (D2, D4); a redelivered delegation
producing one `surface.user_intent` row (D2); memory.db rows for `allen`,
`jarvis_live` and the backend `jarvis` answer with no duplicated request
(D3); a hushed result acknowledged as `session.thinking.appended` (D4); a
late result logged as `thinking` only and two overlapping delegations
speaking only the newest (D5); a mutating tool request logged as
`UnknownToolError` with no `action.dispatched` (D6); `session.started`
showing the `developer` brief in `input` (D7); zero MiniMax requests during
the Live session (D8); `lint-imports` clean (D9).
