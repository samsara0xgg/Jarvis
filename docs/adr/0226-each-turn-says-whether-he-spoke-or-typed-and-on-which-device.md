# ADR 0226 — Each turn says whether he spoke or typed, and on which device

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** none

## Context

- One brain now serves the Mac and the phone, and he switches between speaking and typing on
  either within one conversation. The model has to know, for every turn, how the words reached
  it and how its answer will reach him.
- Before this, a typed turn's state block said "Channel: voice (typed, so these are his exact
  words)", because a typed turn takes the spoken route and the spoken reply shape (ADR 0181) but
  is never played. "Voice" was false for its delivery. A second line, from ADR 0217, said which
  device the turn came from. The two facts were in two lines that could read as contradicting.
- The system prompt said only that spoken words come through speech recognition. Nothing said
  what typed words are or where an answer goes.
- Products that serve several surfaces (ChatGPT, Claude apps, Gemini, Alexa) give the model one
  surface per system prompt, as a fact with its consequence. None of them switches surface turn by
  turn inside one conversation, which Jarvis does.
- The owner approved both texts below on 2026-10-11.

## Decision

Say what spoken and typed words are once, in the system prompt, and give each turn one state line
naming how it came in, from which device, and how the answer goes out.

- The system prompt (`prompts/jarvis_v1.md`) says: he reaches her in two ways and each turn's
  state says which; spoken words come through speech recognition; typed words are exactly what he
  wrote; the answer goes back to the device he used, read aloud when he spoke and shown as text
  when he typed.
- The state line for a spoken turn is "This turn: spoken to his phone (iphone); your answer is
  read aloud there.", and for a typed turn "This turn: typed on his phone (iphone); your answer is
  shown there, not read aloud." A computer is named "macbook, a computer", the Mac she runs on
  alone "this Mac". With no device known (a brain's own `mac` row, a turn with no opening row) the
  line names none.
- The device comes from the turn's opening row (ADR 0217). A device that has declared terminal
  tools (ADR 0225) counts as a computer even while disconnected. ADR 0217's separate origin line
  is removed; its device and place lines stay.
- The spoken reply note still follows the line when the system prompt does not carry it. Live
  turns say "spoken, relayed by Live". Card taps, plugin resumes and other channels keep
  "Channel: text".

## Alternatives rejected

- **Keep "Channel: voice" and add "typed"** — the word voice stays false for a typed turn's
  delivery, and the device sits in another line.
- **Repeat the full rules for spoken and typed words every turn** — the state block repeats on
  every turn and in replayed history; rules belong in the cached system prompt, facts per turn.
- **One system prompt per device** — one conversation spans devices, so the prefix would change
  on every switch and the cache would break.

## Consequences

- The model reads one consistent fact per turn and one rule in the prompt; a typed phone turn no
  longer says voice.
- Replayed history (`session.replay_sent`) carries each past turn's line, so she can see that he
  moved from speaking on the Mac to typing on the phone.
- A new input kind needs a new line here and a sentence in the system prompt, not a new channel
  label.
