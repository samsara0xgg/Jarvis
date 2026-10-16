# ADR 0286 — A terminal sends a voiceprint with each utterance, and the brain turns away a long one that is not his

**Status:** Accepted
**Date:** 2026-10-16
**Supersedes:** none

## Context

- On 2026-10-16 the owner asked that she hear only him when music plays or other people talk
  near him ("我在放歌或者说在外面的时候，我会更希望他只听到我的声音"), and set the bar: almost
  every sentence he says must count as his, "不然我说话就等于他听不到". Who he is talking to is a
  separate judgment, not this one ("声纹就是识别我的声音，而不是识别我在跟谁说话"). He said
  "动手" at 17:19Z.
- Voice is split at text (ADR 0170, 0172, 0209): a terminal owns the microphone, endpointing and
  ASR; the phone sends words, never audio. The brain judges the words as they arrive and
  answers with a verdict (ADR 0216, 0277). A speaker embedding needs the audio.
- Offline study, 2026-10-01..10-16, `~/.jarvis/research/addressee-2026-10-01` and
  `voiceprint-2026-10-16` on the Mac: sherpa-onnx with 3D-Speaker CAM++ zh_en advanced (28 MB,
  192-d), about 30 ms for a 3 s utterance on the Mac CPU. Profile = mean of his own turns of 2 s
  or more, scored by cosine.
  - A profile from five lines in one sitting lost 17% of his lines; a mean of turns from
    several days lost none. Five, ten or thirty turns barely differ once they span days.
  - The same voice on another microphone scores much lower: his AirPods turns score a median
    0.39 against the reSpeaker profile and 0.72 against an AirPods profile; MacBook microphone
    turns 0.63 against reSpeaker, 0.70 against their own. Echo cancellation did not move his
    full-sentence scores (0.655 vs 0.652 median, 10-04).
  - reSpeaker turns of 2 s or more, each day held out of its own profile: 121 of 133 at 0.50
    or above, his own remaining four between 0.38 and 0.48; the six below 0.30 were song
    lyrics and credits, her own demo audio and garbled noise. Under 2 s, half his fragments
    fall below 0.30.
  - Never-enrolled recordings against the reSpeaker profile: phone video 27 lines, 26 below
    0.30, none at 0.50; a friend in the room 35 lines, 29 below 0.30, none at 0.50.
  - Of the 1139 recorded Mac turns, the 09-30 ones were mostly videos he played, recorded as
    his turns. A profile built from every turn, unfiltered, would learn them.
- On 10-04 the reply model was given the score and asked who spoke: it refused two clear lines
  of his at 0.71 and 0.62, and blocked no more strangers than a fixed rule.

## Decision

A terminal computes a voiceprint for each utterance and sends it with the words; the brain keeps
one profile per terminal microphone, learned from his own turns, and turns away an utterance of
2 s or more that scores below 0.30 against that microphone's profile.

- **The terminal extracts, the brain judges.** The utterance a terminal sends (the Mac's
  `utterance` ask, the phone's `say`) carries the embedding, the speech seconds and the name of
  the microphone that heard it. The Mac computes it beside the final ASR, so it adds nothing to
  the path. Scores, thresholds and profiles live only on the brain, beside the word judge.
- **One verdict, asked first.** `not_owner` is decided before `elsewhere` and the word judge,
  writes no turn, and is carried out like `elsewhere`: the terminal drops the words. The brain
  logs each one with its score, and a terminal that keeps audio keeps it.
- **Only a sure stranger is turned away.** Under 2 s, unsure (0.30 to 0.50), a microphone with
  no profile yet, the first three minutes after a microphone opens (the owner saw the reSpeaker
  misjudge right after power-on), or a missing or failed voiceprint: the words go on to the
  word judge as today. The score never reaches a model.
- **A profile per microphone, learned from his turns.** A profile is the mean of the last 300
  turns of 2 s or more on that microphone that scored 0.50 or above, so videos, songs and
  strangers never enter it. A new microphone starts from turns scoring 0.35 or more against his
  best other profile and turns nothing away until it holds 30 turns from two days.
  The offline profiles seed the brain once.
- **On by default, one switch.** A brain setting turns the gate off; scores are still logged.

## Alternatives rejected

- **Send the audio to the brain and embed there.** About 100 KB per utterance over the tailnet
  or a phone's cellular link to compute 192 numbers, and the phone would send audio where
  ADR 0209 sends words. The terminal already holds the audio; 30 ms of its CPU is the cost.
- **Judge on the terminal against a copied profile.** Two copies of the thresholds and the
  learning (Python and Swift) to keep in step, and the profile, which is the owner's state,
  would live where a reinstall loses it. ADR 0277 already moved every judgment to the brain.
- **One profile for all microphones.** His AirPods turns would score 0.39, half of them unsure
  and some below 0.30: the gate would turn him away on the headset he uses outside.
- **Give the model the score and let it decide.** Measured on 10-04: it lost two of his clear
  lines and caught no more strangers than the rule, at the price of a call.
- **Enroll by reading sentences once.** One sitting lost 17% of his lines; his everyday turns
  across days lost none, and cost him nothing.

## Consequences

- A stranger who speaks for under 2 s, or lands between 0.30 and 0.50, still reaches the word
  judge; a person whose voice is close to his is untested. Words said by him to someone else
  pass: this gate says who, not to whom.
- Every voice terminal carries a 28 MB model and the embedding code: Python on the Mac, Swift
  with sherpa-onnx on the phone.
- A profile drifts with what it keeps: a stranger who scores 0.50 or more on one microphone
  would enter it. The 300-turn window bounds how long a mistake stays.
