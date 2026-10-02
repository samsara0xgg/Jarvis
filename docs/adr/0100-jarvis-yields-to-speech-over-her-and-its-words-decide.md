# ADR 0100 — Jarvis Yields to Speech Over Her, and Its Words Decide

**Status:** Accepted
**Date:** 2026-09-30
**Supersedes:** 0041

## Context

- ADR 0041 stops Jarvis at Silero speech onset whenever Allen talks over her
  in conversation mode, and cancels the answer still being written. Its own
  Consequences name the cost: a cough or a door stops her, with no transcript
  behind the stop.
- On 2026-09-25 the reSpeaker captures held three stops with nobody talking,
  on short sounds near -40 dBFS; the `tts` VAD profile (-36 dB) was the fix
  for those three, not for a cough, a 「嗯」 or a TV at speech level.
- A listening sound is not a request for the turn: OpenAI's GPT-Live
  prompting guide (https://developers.openai.com/api/docs/guides/live-prompting)
  tells a brief listening sound apart from taking over the user's turn.
- A lone 「对」「好」「是」 or "yes" may be Allen answering a card that waits
  for its words (ADR 0062); it must still reach the turn.
- An utterance ends after 0.77 s of VAD silence (24 frames of 32 ms), a
  short sound 0.4 s later still; partial ASR (ADR-0006 D7) is off.
- The player's gain is also the speech mute (ADR-0015 D2); nothing may lift
  a mute.
- Over her voice, final ASR heard a 「嗯」 as "And." or as Cantonese 「五」, and
  a drawn-out one, split by the `tts` profile, as Japanese 「うん」「うん」「う」;
  a lone 「停」 as 「停立」 or 「顶」; and every 「pause」 as some other lone
  English word of two to six characters. Replayed through Silero, a 「嗯」
  held 0.61 s of voice and 「对对对」 0.64 s.
- Proposed on 2026-09-28 and revised after the first four live runs below;
  Allen accepted it on 2026-09-30, after the fifth run passed.

## Decision

In conversation mode, when speech starts while Jarvis speaks, lower her
instead of stopping her, hold her where she is once the speech has enough
voice, and let final ASR's words decide whether she goes on, stops, or stops
and answers.

Its limits:

- She drops to `barge_in_yield_gain` at onset and is held once the speech
  has `barge_in_confirm_voiced_s` of voice: her answer stops playing and
  keeps its place. A shorter sound ends after `barge_in_pause_ms` of silence,
  not on the ordinary endpoint.
- Nothing, a listening sound, or one syllable or word that answers nothing:
  she goes on from where she was with the gain back, and it is no turn. A
  stop request, or her wake phrase alone: she stops, and it is no turn.
  Anything else: she stops, and it is a turn. A stop cancels the answer
  still being written, as ADR 0041 did; a hold cancels nothing.
- Words that held her are judged the same way (a slow 「别说了」 is no turn).
  Words lost before a verdict, to an ASR error or a capture failure, stop her
  if she was held and let her go on if she was only lowered.
- A stop request includes the ting/ding syllables final ASR makes of a lone
  「停」, 「等一下」, "wait", "pause", 「可以了」 and 「够啦」 style endings, and
  any lone English word that is no listening sound, no card's answer and no
  question. A listening sound includes Japanese and Korean hums.
- Stop phrases, wait phrases (ADR 0102) and listening sounds count in any
  repetition or mix, punctuation aside: 「停下来停下来停」, 「你别说话你别说话停」
  and 「嗯哼停」 are one stop request, 「嗯哼等我一下」 a wait. A stop phrase in
  the run makes it a stop request, else a wait phrase makes it a wait request,
  else it is a listening sound; any other word keeps it a turn (「停一下，帮我
  查天气」). A lone 嗯/呃 with only the hum's own letters after it (「嗯h」) is a
  listening sound.
- A lone 对/好/是/yes or any other one-word card answer, and a lone 「可以」,
  stay turns; a listening sound or a lone word asked as a question is a turn.
- A stop or a hold first fades her to silence over 20 ms; going on from a
  hold fades her back in over the declick's 128 samples. Her gain returns
  only after a stop has landed; the yield scales the speech mute and never
  lifts it.
- `barge_in_confirm_voiced_s: 0` is ADR 0041 as it shipped.
- The rest of ADR 0041 stands: wave mode, entered from the orb or by the
  wake word, listens without a wake word; mic mute and GPT-Live close it; the
  surface sets it in the daemon's memory only, and it drops with the last
  surface; ADR-0006 D9's per-profile gate does not apply to it.

## Alternatives rejected

- **Keep stopping at onset (ADR 0041).** A cough, a 「嗯」 or the TV ends her
  answer and cancels its generation; the three stops of 2026-09-25 show it
  happens on the reSpeaker with the stricter profile already in place.
- **Duck only, and restore when the sound ends.** A short 「停」 then never
  stops her: it is shorter than any voiced-time rule that lets a cough pass.
- **Stop on voiced time alone, without the transcript.** A long 「嗯嗯嗯」 stops
  her and becomes a turn she answers; a quick 「停」 does not stop her.
- **Stop once the voiced time is reached, and let the words decide only the
  turn** (as first shipped). A drawn-out 「嗯——」 or a cough ends her answer
  for good: in the second live run one reached 0.8 s, was judged a listening
  sound, and she stayed silent with the answer cut off.
- **Stop, then say the unheard rest again when the words say nothing.** The
  answer may still be generating when it is cancelled, and saying it again
  costs a new synthesis; holding costs neither.
- **Take a lone English word over her as saying nothing.** In the second and
  third live runs all ten 「pause」 came back as lone English words of two to
  six characters, none of them "pause"; each only lowered her.
- **Force Chinese on final ASR, to get 「嗯」 back from 「五」.** sherpa-onnx
  switches the language only for the whole recognizer, and switching back to
  automatic changed every later decode's language guess (measured); a second
  Chinese recognizer costs about 240 MB of memory.
- **Cut on the player's own declick** (ADR-0006 D11: 128 samples, about 2.7 ms
  at 48 kHz, from the last sample played). In the fourth live run Allen heard
  her stop from mid-word as a pop; after the 20 ms fade, none in the fifth.
- **Keyword spotting on partial ASR while she talks (ADR-0006 D8).** It raised
  7.53 false candidates a minute on the MacBook speakers against a 0.5
  target, and partial ASR is off.

## Consequences

- A stop or a hold lands 20 ms plus one output block after it is decided.
- A short 「停」, or a question with less voice than 0.8 s (「你是谁」, 0.6 s),
  stops her only after its pause and final ASR, where ADR 0041 stopped her at
  onset: 「停」 took 0.5 to 0.7 s from Allen's first sound in the second run.
- Until a sound is judged she keeps talking at 0.2: a word of hers may be
  missed under a cough.
- A long sound holds her silent through its ordinary endpoint (0.77 s of
  silence) and final ASR: a real question silences her at 0.8 s of voice, but
  her answer is dropped, and the one being written cancelled, only once it is
  judged. Held, her speech fills the player's ring, and then its writer waits.
- The listening-sound and stop-request lists are fixed Chinese, English,
  Japanese and Korean patterns in `jarvis/surface/voice_asr.py`; a phrasing
  outside them is a turn, except a lone English word, which stops her: a hum
  heard as an English word off the listening list, or a lone "wow" said over
  her, stops her too.
- 0.8 s comes from the first live run's hums and 「对对对」; 0.2 and 350 ms
  are chosen, not measured. Runs two to five used the reSpeaker; no run is
  recorded on the built-in microphone and speakers.
- One syllable that answers nothing is dropped over her voice: a lone 「五」
  said on purpose is lost, and Allen says it again.
- A sound split by a pause longer than `barge_in_pause_ms` is judged in
  parts; ADR 0074's merge still joins the second part to the first when both
  are turns.
- Words judged no turn leave no recording or transcript, only their length
  and verdict in the realtime trace.

## Evidence

Five Mac live runs; runs two to five with the trace on, the reSpeaker
XVF3800 in, the Multi-Output Device out and software AEC off.

- 2026-09-28, 17 turns, at 0.4 s: eight short listening sounds passed; a
  0.61 s 「嗯」 and a cough stopped her; 「五」「停立」「顶」 and "And." were
  answered.
- 2026-09-29 12:55, 10 turns: 「停」「等一下」 stopped her unanswered, short
  「嗯」 passed; a drawn-out 「嗯——」 cut her off for good, 「OK可以了」 was
  answered, seven 「pause」 only lowered her.
- 2026-09-29 15:06, 4 turns: three 「pause」 only lowered her and 「可以啦」 was
  answered; a 「嗯」 and a cough passed.
- 2026-09-29 15:21, 7 turns: 「pause」 and 「可以啦」 stopped her unanswered;
  the split 「うん」「うん」「う」 stopped her as a turn, and her stop popped.
- 2026-09-29 15:47, 6 turns, nine sounds over her: all five 「嗯」 passed, one
  of them held her at 0.8 s and she went on from where she stopped; 「pause」
  and 「可以啦」 stopped her unanswered; a held 「继续讲刚才的故事」 was a turn;
  no pop. Allen: 「可以很完美」.

The behaviour is pinned in `tests/integration/test_soft_barge_in.py`, the
fades in `tests/integration/test_wave2_streaming_media.py`.
