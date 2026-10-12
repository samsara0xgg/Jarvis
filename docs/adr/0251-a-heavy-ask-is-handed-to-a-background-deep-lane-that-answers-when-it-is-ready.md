# ADR 0251 — A heavy ask is handed to a background deep lane that answers when it is ready

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** one clause of ADR 0108: when `llm.deep` is configured, a sentence with an
`llm.think.on_words` word is handed to the deep lane instead of getting the think preset for its own
turn. Everything else in ADR 0061 and ADR 0108 stands, and the think preset still serves a sentence
when no deep lane is configured.

## Context

- The voice lane answers in one or two tool calls, with a per-turn tool cap, on the model that keeps
  a spoken reply fast. Asks that need many lookups put together or judged ("go through this week's
  job mail and say which need a reply", "plan the weekend around the calendar and the weather",
  "find out why spending went up") do not fit it: the cap ends the turn with half an answer, or the
  model guesses what it did not look up.
- ADR 0061 and ADR 0108 gave a think word a stronger model for that one turn. It is still a turn he
  waits inside, it has the same menu and the same cap, and it holds the voice lane's one line of
  talk until the model finishes.
- A turn is already a unit the runtime can run beside others: the intent pump claims an opening row,
  `drive_turn` runs it through `decide()`, and the closing row ends it. A result that
  arrives while he has moved on already has a spoken form (ADR 0107), a card with a push (ADR 0218,
  ADR 0210), and progress labels (ADR 0231). The turn guards (ADR 0240, ADR 0250) and the
  confirmation card (ADR 0062) hold for any turn. None of that has a way to start a turn that he did
  not speak.

## Decision

- **A deep task is a turn on its own channel.** `think_deeper(task)` is a voice-lane tool that
  writes a `surface.user_intent` on `DEEP_CHANNEL` (`deep`) and returns at once with
  `{started, spoken}`; the voice lane's reply is that `spoken`, word for word ("this needs a careful
  look, I will tell you when it is done"). The intent pump claims the row like any other, so the
  deep turn runs in a worker beside the voice lane. The task text is the voice lane's own
  rewriting of his ask plus what the conversation already established, up to 1500 characters.
- **One deep task at a time.** `think_deeper` while a deep intent has no closing row returns
  `{started: false, reason: "busy", spoken}` and starts nothing. Busy is read from the event log, not
  held in memory, so a restart does not forget a task; a task with no closing row for 30 minutes is
  taken as lost, because a crash writes none.
- **The deep turn runs on the `llm.deep.preset` preset.** The shipped one is `sonnet-deep`:
  `claude-sonnet-5-5`, `reasoning_effort: high`, 16384 output tokens, a 300 second request timeout
  (a `timeout_s` key on a preset overrides `llm.timeout_s`), and the unstructured spoken route of
  ADR 0241. The step cap is `llm.deep.max_tool_iterations` (40), where the voice lane keeps its own per-turn cap.
- **The deep turn sees the tools that read, or that ask first.** `DeepToolRegistry` wraps the
  registry for that turn and offers a tool only when it is read-only or requires his confirmation:
  nobody watches the turn, so a write that does not ask (`keep`, `save_*`, `forget_place`,
  `dismiss_card`, `set_voice`, `open_url`, `open_path`, `write_mail_draft`, the night runs, a
  plugin tool without an approval) is not on its menu and refuses to dispatch. `ask_user` (no one
  answers), `open_plugin` (its resume would run the task text again as a voice-lane turn) and
  `show_on_dashboard` (it flips the page on his screen while he is elsewhere) are hidden by name,
  as are `think_deeper` and `tool_search`; there is no allowlist of exceptions. The deferred tools
  are on the menu, since the turn has no `tool_search`, except those a menu tool supersedes
  (ADR 0242). `read_skill` stays. A tool that asks first still ends in its card (ADR 0062); the
  task stops at that card and the result says so.
- **The turn is told what it is.** `DEEP_RULES` is appended to its system prompt and the state
  block says the turn is a handed-over task. The rules ask for `<voice>` of at most three sentences
  that first say what the answer answers, and a `<document>` that opens with a line naming it and
  holds the full result with evidence. Only the document is kept in conversation history, so the
  naming line is what lets "the result from earlier" be found.
- **Delivery follows whether he is still talking.** When the answer is ready, `live_conversation`
  asks the log: his latest Mac voice words were within `llm.deep.live_window_s` (120) and no
  dismissal came after them. Live: the answer is spoken as a slow result that says what it answers
  (ADR 0107). Otherwise (typed words, a phone's voice, a dismissal, a quiet ten minutes): the
  `voice_notify` and `ask_confirm` attentions are lowered to `badge_card`, and the push rule
  (ADR 0218) sends a "your result is ready" push to the phone the task came from, or to a phone
  only when he has left the Mac. Nothing is spoken that he is not listening for.
- **A deep task is not a word of his.** It is excluded from `words_since`, from the think-word scan,
  from the utterance-count folds, and from the exit and barge-in cancellation (a spoken "stop" ends
  the voice lane's speech, not a running deep task). A later voice turn is told the task is running
  (the in-flight line, ADR 0107), with its text cut to 200 characters, and can say so.
- **A think word forces the deep lane without a model request.** When `llm.deep` is set,
  `ThinkMode` joins the sentence's transcripts and `forced_hit` returns a Tier 0 hit for
  `think_deeper` (`tier0.deep`, answered by the `spoken` of the hand-over). "想想" and the new
  on-words "仔细查" and "好好查" therefore always reach it, whatever the voice lane would have chosen.
- **A failed task says so.** `drive_turn` catches a deep run's failure and `DeepLane.failed` writes a
  second deep-channel intent that carries `deep_failure=<reason>`. `decide()` answers that notice with
  one fixed sentence built from `failure_reason` (missing key, unauthorized, model denied, quota,
  rate limited, network, timeout), which reaches him live or as a card like a result. A notice that
  itself fails writes nothing more.
- **Progress uses the step labels** of ADR 0231 (`think_deeper` is the `deep` kind, "started" and
  "done" lines in `shared/lang.py`); commentary treats `think_deeper` as a quiet tool.
- **The lane is off unless configured.** Without an `llm.deep` block there is no `think_deeper`
  tool and think words behave as in ADR 0108. A block naming a preset that is not in
  `llm.presets`, or a non-positive number, fails boot. The shipped `config/jarvis.yaml` sets it on,
  so it needs the Anthropic key the preset names; a missing key is the failure notice above, not a
  silent hang. `realtime.input.max_concurrent_turns` is 3 so a running deep task leaves two workers.
- **Wiring** lives in `runtime/`: `jarvis/decision/deep.py` holds the settings, rules, live check
  and forced hit; `jarvis/execution/deep_tool.py` the tool and the registry view;
  `jarvis/runtime/deep_lane.py` the hand-over, busy check and failure notice. The state layer adds
  `DEEP_CHANNEL`, `open_deep_turn`, and the exclusions above.

## Alternatives rejected

- **Raise the voice lane's tool cap and keep one turn.** Rejected: the voice turn then holds the
  speaker for a minute of lookups, and the voice model is chosen for latency, not for the
  judgement a mail triage needs. Falsified if a voice-lane run of the exam's heavy asks at a cap
  of 20 answers them as well as the deep lane within the spoken-reply latency budget.
- **A second daemon or subprocess for the deep agent.** Rejected: it would need its own copy of
  the registry, guards, confirmation cards, event log writes and push rules. The turn machinery
  already runs a turn beside others, and a deep turn through it gets every guard unchanged.
- **Queue further tasks behind the running one.** Not built: a queue needs an order, a
  cancellation and a way to say what is waiting, and a refusal that says so is true and cheap.
  Revisit when the log shows him asking for a second heavy task while one runs.
- **Speak the result whenever it is ready.** Rejected: a result that interrupts a conversation he
  has left, or plays at a Mac he is away from, is the late-answer problem ADR 0107 solved by saying
  what it answers and only while he is listening. The live window is the same test, from the log.
- **Keep the whole voice and document in history.** Rejected: ADR 0107's rule stands, only the
  document is the conversation record, so the rules put the naming line there instead of storing the
  spoken form twice.

## Consequences

- A heavy ask costs the voice lane one tool call and one sentence, then runs on the Sonnet preset
  for as long as it needs, up to 40 tool iterations and 300 seconds a request. Each deep run is
  billed to the Anthropic key, which is a new standing cost; `scripts/tool_exam.py` stubs
  `think_deeper` as "started, the result comes later" so the exam does not spend it.
- A deep run occupies one of the three turn workers for its whole duration. A fourth concurrent
  turn waits for a worker.
- A deep turn that has been claimed and is unfinished when the daemon restarts may be driven
  again after the restart, from its opening row, and repeat read-only lookups. Writes still stop at
  their cards. A task a crash left without a closing row stops counting as running after 30 minutes.
- A result for a task that came from a phone's voice is delivered as a card and push to that
  phone, not spoken: the Mac does not play a phone's conversation (ADR 0218).
- The UI shows the task text as a row of his on the deep channel, because the log shape is a user
  intent; it is not his sentence, and the state block and counts do not treat it as one.
- The Pi brain host runs the same code; whether its environment holds the Anthropic key was not
  checked, and without it the first task ends in the failure notice.
- `prompts/*.md` should tell the voice lane when to call `think_deeper` (needs several lookups put
  together or judged), when not to (one or two tools answer it), and to say the returned `spoken`
  word for word, including on `started: false`. Until that edit lands the tool description carries
  the same rule.
