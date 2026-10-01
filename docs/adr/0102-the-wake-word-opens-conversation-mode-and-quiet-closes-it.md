# ADR 0102 — The Wake Word Opens Conversation Mode, and Quiet Closes It

**Status:** Accepted
**Date:** 2026-09-30
**Supersedes:** none

## Context

- ADR 0041 let the wake word open wave mode through the surface: Resonance
  saw the `listening` phase and flipped the switch itself. The companion
  that replaced it does not, so a wake hit gives one utterance and then
  needs the wake word again; while Jarvis answers it, the wake word is
  suppressed and speech over her is ignored. ADR 0100's soft barge-in only
  runs in conversation mode.
- Live test 2026-09-30 (reSpeaker): three wakes at 0.988–0.991 answered
  their questions, and Allen could not stop or redirect any answer by
  voice; tapping the 星核 was the only way.
- Allen, the same day: after a round Jarvis should stay listening for a
  while; it should leave when told to, or after a stretch with nobody
  talking; the wake word should mean what tapping the 星核 means; it should
  not stay open long, but sometimes it should wait for him.
- Phone-class speakers keep listening 5–10 s after a reply (Alexa Follow-Up
  Mode about 5–7 s by press reports, Google Continued Conversation 8–10 s
  by its help pages); open-ended live modes (ChatGPT Voice, Gemini Live) stay
  until ended. All of them filter speech not meant for them with trained
  classifiers that still err about one time in ten.
- Measured 2026-09-30: two 20-minute reSpeaker recordings of Allen's room,
  replayed through a real `DuplexVoiceSession` in conversation mode and
  SenseVoice, committed 74 and 210 utterances: 40 and 183 of them words not
  meant for Jarvis (Allen talking to people or dictating), 15 and 8 lone
  words, 19 and 19 empty. In 108 minutes of past conversation mode, lone
  words became turns 2.5 times per 10 minutes.
- ADR 0041 rejected the daemon entering the mode by itself because nothing
  could leave it without a surface, and the Mac would keep sending every
  sentence in the room to the backend.

## Decision

A wake hit turns the daemon's conversation switch on, exactly as a tap does,
and the surface hears the change as a `controls` push. The mode ends, from
any start, on a tap, on a dismissal said in it, or
`conversation_idle_exit_s` (shipped 10 s) after Jarvis last spoke or one of
Allen's turns was last accepted.

Its limits:

- Only Jarvis's speech and accepted turns restart the clock. A listening
  sound or one syllable that says nothing, said while she is silent, is
  dropped as no turn; it and an utterance heard as nothing do not keep the
  mode open. The mode never ends while an utterance is still coming in, and
  an accepted turn holds it until her answer starts, for at most 30 s.
- A dismissal is an utterance that is only 退下, 没事了, 就这样吧, 先这样,
  拜拜, 再见, 结束对话, 去休息, bye, goodbye or that's all (with an optional
  wake phrase, 你/那, and 了/吧/啦), or a sentence of at most 16 characters,
  not a question, that holds 退下 or 退一下 or starts with 退出; Allen's
  first live dismissals were 「退出退出退下，暂停停一下等」 and 「我让你退一下」.
  It stops her if she is talking, ends the mode, and is no turn.
- A wait request is an utterance that is only 等我一下, 等等我, 稍等, 你等着,
  hold on, wait for me or give me a second. It stops her if she is talking,
  holds the mode `conversation_wait_s` (shipped 60 s) past it, and is no
  turn. A lone 「等一下」 stays ADR 0100's stop request.
- Both phrase lists are fixed patterns in `jarvis/surface/voice_asr.py`.
- Mute and GPT-Live still close the mode as before, and the last surface
  disconnecting still clears it.

## Alternatives rejected

- **Only the wake word interrupts her (no conversation mode).** 「停」 and a
  new question still would not reach her, and every follow-up needs the wake
  word again.
- **The surface opens the mode on `listening`, as Resonance did.** It needs a
  surface for the wake word to work at all, and the quiet timer would live in
  each surface.
- **30 s of no voice at all.** Room talk restarts it, so with people talking
  the mode never closes and answers them; and 30 s is three times what
  phone speakers wait.
- **A shorter window for the wake word, a longer one for a tap.** Two rules
  for one mode; 「等我一下」 covers waiting.
- **A model judging whether each utterance is for Jarvis.** Not decided
  here: an offline check on 50 utterances (2026-09-30, gpt-6-luna) kept all
  25 requests and dropped 21 of 25 room sentences only after its prompt was
  tuned on them, and one call takes about 2 s.

## Consequences

- Any speech in the room within 10 s of her last word, or of Allen's last
  accepted turn, still becomes a turn, as after a tap.
- An answer that has not started 30 s after its turn was accepted arrives
  after the mode has ended and cannot be interrupted by voice.
- A tap-opened mode now also ends after 10 s of quiet.
- A lone 「嗯」, 「啊」 or "The." said in the mode while she is silent is never
  answered; a lone card answer (好, 对, yes) still is.
- A phrasing outside the patterns is a turn; 「没事」 alone is a dismissal in
  the mode, even as an answer to her question.
- With no surface connected the mode still opens on a wake and ends on quiet
  or a dismissal.
