# Logging protocol

Three layers, three volumes:

    transcript      Claude Code keeps it; never loaded, forensic only
    events.jsonl    a few lines per session: feedback, decisions, drift, failures
    sessions.jsonl  one line per generation

The logs exist to answer, per generation: was it efficient, did attention
drift, where is the room to improve. Measured numbers and estimates are
kept apart and named as such.

## Events (log immediately, not at the end)

    python3 .claude/skills/horizon/scripts/log-event.py <type> "<summary>"

| type | when |
|---|---|
| `user_feedback` | Allen corrects or judges architecture, workflow, priority, contract, or a behavior; record the rule in one English sentence, not the quote |
| `decision` | a default applied without Allen, or a manifest/reality disagreement that changed your next action |
| `drift` | you notice you did work the card did not ask for; say what and why |
| `failure` | a spawn, merge, gate, or verifier run failed |
| `rotation` | written by log-session.py, do not log by hand |

Not events: "continue", "run the tests", "look at this". Ordinary prompts
stay in the transcript.

## Session log (before planned exit)

`log-session.py` merges three sources into one line:

    measured   context/<session>.json  used_pct, token counts, cost
    mechanical sessions/<session>.meta.json  role, generation, predecessor
    semantic   your --outcome, --successor, --mix, --drift, plus every
               user_feedback event this session logged

`--mix` is `context_mix_estimate`; it is your guess of what filled the
context. Keep it a guess in the field name.

`--end` values: `planned_rotation`, `complete` (role finished, no
successor), `blocked`.

## Safety net

The SessionEnd hook runs `session-end.py`. If a harness session ends
without a session log it appends `end: unexpected` with the last measured
context and the transcript path. It does no summarizing; that is why you
write the log before you exit.
