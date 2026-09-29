# ADR proposal — Jarvis Yields to Speech Over Her, and Its Words Decide

**Status:** Proposed
**Date:** 2026-09-28
**Supersedes:** 0041 (on acceptance; it then gets the next ADR number)

## Context

- ADR 0041 stops Jarvis at Silero speech onset whenever Allen talks over her
  in conversation mode, and cancels the answer still being written. Its own
  Consequences name the cost: a cough or a door stops her, with no transcript
  behind the stop.
- On 2026-09-25 the reSpeaker captures held three stops with nobody talking,
  on short sounds near -40 dBFS; the `tts` VAD profile (-36 dB) was the fix
  for those three, not for a cough, a 「嗯」 or a TV at speech level.
- A listening sound is not a request for the turn. The GPT-Live prompting
  guide (`docs/gpt-live/live-prompting.md`) says it plainly: "A brief
  listening sound is different from taking over the user's turn."
- A lone 「对」「好」「是」 or "yes" may be Allen answering a card that waits
  for its words (ADR 0062); it must still reach the turn.
- Final ASR of a short sound is estimated at about 0.1 s (not measured);
  the acoustic endpoint of a short sound waits 0.77 s of VAD silence (24
  frames of 32 ms, both shipped profiles) plus the 0.4 s short-sound grace.
  Partial ASR (ADR-0006 D7) is off.
- ADR-0006 D8's keyword barge-in raised 7.53 false candidates a minute on the
  MacBook speakers against a 0.5 target.
- The player's gain is also the speech mute (ADR-0015 D2); nothing may lift
  a mute.
- The first live run (2026-09-28, 17 turns) at 0.4 s: eight short listening
  sounds passed as intended, but a 「嗯」 of 0.61 s voiced stopped her
  mid-answer and final ASR heard it as "And."; another 「嗯」 came back as a
  Cantonese 「五」 and two 「停」 as 「停立」 and 「顶」, and all three were
  answered as questions. 「对对对」 held 0.64 s of voice.
- The second live run (2026-09-29) at 0.8 s: two short 「嗯」 passed, but a
  drawn-out 「嗯——」 held enough voice to stop her; final ASR then judged it a
  listening sound, and she stayed silent with the answer cut off.
- The second and third live runs (2026-09-29): every 「pause」 over her came
  back as some other lone English word of two to six characters, taken as
  saying nothing, and 「可以啦」 was a turn she answered.

## Decision

In conversation mode, speech that starts while Jarvis speaks first lowers her
to `barge_in_yield_gain`; once it holds `barge_in_confirm_voiced_s` of voice
she is held where she is (her answer stops playing and keeps its place), and
a shorter sound ends after `barge_in_pause_ms` of silence. Final ASR judges
both: nothing, a listening sound or one Chinese syllable that is no answer
lets her go on
from where she was with the gain back and is no turn, a stop request or her
wake phrase alone stops her and is no turn, anything else stops her and is a
turn. A stop request includes the ting/ding syllables final ASR makes of a
lone 「停」 over her voice, and any lone English word that is no listening
sound, no card's answer and no question.

Limits: the gain returns only after a stop has landed; the yield scales the
mute and never replaces it; words that held her by their length are judged
the same way (a slow 「别说了」 is no turn); held words that final ASR fails on
stop her; a lone 对/好/是/yes and anything ending in a question mark are never
a listening sound; `0` for the voiced time is ADR 0041 as it shipped.

## Alternatives rejected

- **Keep stopping at onset (ADR 0041).** A cough, a 「嗯」 or the TV ends her
  answer and cancels its generation; the three stops of 2026-09-25 show it
  happens on the reSpeaker with the stricter profile already in place.
- **Duck only, and restore when the sound ends.** A short 「停」 then never
  stops her: it is shorter than any voiced-time rule that lets a cough pass.
- **Stop on voiced time alone, without the transcript.** A long 「嗯嗯嗯」 stops
  her and becomes a turn she answers; a quick 「停」 does not stop her.
- **Stop once the voiced time is reached, and let the transcript only decide
  the turn** (as first shipped). A drawn-out 「嗯——」 or a cough ends her
  answer for good (second live run).
- **Stop, then say the unheard rest again when the words say nothing.** The
  answer may still be generating when it is cancelled, and saying it again
  costs a new synthesis; holding costs neither.
- **Keyword spotting on partial ASR while she talks (ADR-0006 D8).** Measured
  at 15 times its false-candidate target on the MacBook speakers, and partial
  ASR is off.

## Consequences

- A short 「停」 stops her about `barge_in_pause_ms` plus final ASR after it
  ends (about 0.45 s at the defaults), where ADR 0041 stopped her at onset.
- Until a sound is judged she keeps talking at 0.2: a word of hers may be
  missed under a cough.
- A long sound holds her silent until it is judged: its ordinary endpoint
  (0.77 s of silence) plus final ASR after it ends. A real question still
  silences her at 0.8 s, but her answer is dropped, and the one still being
  written cancelled, only once it is judged. While she is held her speech
  keeps streaming into the player's ring and its writer waits.
- The listening-sound and stop-request lists are fixed Chinese and English
  word lists in `voice_asr.py`; a phrasing outside them is a turn, except a
  lone English word, which stops her: a hum heard as an English word other
  than "And.", or a lone "wow" said over her, stops her too.
- 0.8 s comes from the first live run's hums and 「对对对」; 0.2 and 350 ms
  are still chosen, not measured. A second live run on the Mac with the
  reSpeaker and with the built-in speakers is owed before acceptance.
- A question with less voice than 0.8 s (「你是谁」, 0.6 s) stops her only after
  its pause and final ASR, about 0.45 s after it ends.
- One Chinese syllable that is no answer is dropped over her voice: a lone
  「五」 said on purpose is lost, and Allen says it again.
- A sound split by a pause longer than `barge_in_pause_ms` is judged in two
  parts; ADR 0074's merge still joins the second part to the first when both
  are turns.
