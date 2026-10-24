# ADR 0400 — A voice terminal's words are heard by Qwen in the cloud, through the brain

**Status:** Accepted
**Date:** 2026-10-23
**Supersedes:** for `realtime.final_asr: cloud` only, the clause of ADR 0170 that a terminal which runs ASR sends text and that audio stays on the device that heard it, and the rejection in ADR 0170 and ADR 0172 of the brain taking a terminal's audio. Everything else in ADR 0170 and ADR 0172 stands.

## Context

- On 2026-10-23 the owner could not say his room number, 「135E」, to Jarvis on the Mac.
  SenseVoice's caption heard 「135E」. The hybrid's Whisper pass (ADRs 0132, 0270), forced to
  Chinese, wrote 「一三五一」 and 「英语字母的意义」; its log-probability gate (-1.5) did not trip,
  because those passes scored about -0.4 to -0.6.
- Replay of the 66 retained Whisper turns through cloud Qwen's streaming recognizer: 「135E」
  right in 3 of 4, and Qwen read better than the live Whisper text in about 16 turns against
  about 5. All 5 were the trailing "Jarvis" (Travis, 嘉宾, dropped). Local Qwen3-ASR-0.6B also
  wrote 「一三五一」 and 「一百三十五亿」.
- Qwen's voice-input model polishes: it drops fillers, and once dropped 「那个 Claude Code」. The
  owner, 2026-10-23: a voice conversation takes the streaming model's words as said, fillers and
  repeats included, with neither Qwen's polish nor a second LLM pass; the turn's model
  understands them. His dictation app (Yana) keeps its own polish.
- The owner wants every voice terminal (Mac, iPhone, iPad, watch) on one interface, with the key
  on the Pi. ADR 0170 keeps model keys on the brain.
- Model Studio's international endpoint is in Singapore. TCP round trip: about 200 ms from both
  the Mac and the Pi; Mac to Pi 8 ms at home. A new connection plus a task start took 0.88 s; a
  task on an open connection 0.2 s, still after 60 s idle.
- Measured on 16 recorded clips sent at capture speed: Qwen's running words lag the speech by a
  median 0.51 s (p90 0.98 s). Its final words come only after `finish-task`, 0.45 to 0.82 s
  later. The running words at the end of the audio equalled the final words in 12 of 30 clips
  (「135亿」 against 「135E.」).
- The hybrid's Whisper starts at the 192 ms pause hint and is usually done by the 768 ms
  endpoint (ADR 0132). Three ways to start Qwen's final words at that hint were replayed through
  the real link at capture speed, with Silero marking the pauses:
  - streams finished at each pause and joined wrote 「135E」 as 「135一」 and 「130 5E」 in 2 of
    the first 9 turns;
  - 10 whole utterances sent at once, as a re-send at a pause would be, came back 0.95 to
    2.96 s later, growing with their length;
  - on all 66 turns, finishing the stream at each pause and re-sending the whole utterance when
    he spoke again waited a median 0.35 s at the commit but a p90 of 1.66 s, and in 4 turns lost
    a soft trailing 「Jarvis」 that Silero did not mark as speech. Finishing only at the commit
    waited a median 0.59 s, p90 0.69 s, max 0.88 s.
- Qwen took seconds to answer a WebSocket close.
- After these measurements the owner approved building it, Pi and Mac first: 「可以，你先装一下，
  然后我试一下呗。」 (2026-10-23).

## Decision

With `realtime.final_asr: cloud`, a voice terminal streams each utterance's audio to its brain
over `/terminal/ws`, the brain streams it to Qwen's streaming recognizer with the owner's word
list as hot words, and Qwen's words, as written, are the utterance's transcript.

Its limits:

- **The key stays on the brain** (`BAILIAN_API_KEY`). The terminal keeps the microphone, wake,
  VAD and endpointing, and SenseVoice: the captions, the semantic endpoint's snapshots, the
  language, emotion and audio event, and the words whenever Qwen's are missing.
- **One stream per utterance, from speech onset to the commit.** The commit finishes it and
  waits for Qwen's last words.
- **The brain keeps a connection open** ahead of the next utterance, and never waits for a
  close.
- **SenseVoice's words stand, and the utterance says why,** when the brain does not hear, the
  stream fails, no words come within 4 s, or Qwen writes none.
- Hot words are the terms he wrote into his Yana list himself (its `user` section), without
  one- and two-letter Latin ones: Yana learned 「pi」 from his edits, and 「pi」 replaced
  「Jarvis」 in a replay.

## Alternatives rejected

- **A digit gate on the hybrid** (SenseVoice stands when a digit run it wrote is missing from
  Whisper's text). On the 66 turns it fires 6 times and fixes 3; the 16 turns Qwen reads better
  are mostly not digits.
- **Each terminal calls Qwen itself.** The key would live on four devices, which ADR 0170
  forbids. The words would come one terminal-to-brain round trip sooner: 8 ms at home.
- **Qwen's voice-input model.** It polishes away fillers and, once, a name the owner said.
- **Local Qwen3-ASR-0.6B.** It failed 「135E」 as Whisper did, and needs hot words for "Jarvis".
- **Streams split at pauses and joined.** 「135E」 came back as 「135一」 and 「130 5E」.
- **A re-send of the whole utterance at each pause, in parallel.** Its words came 0.95 to 2.96 s
  later, after the endpoint and after the live stream's own final words.
- **Finishing at each pause, re-sending when he speaks again.** It saved 0.24 s at the median,
  cost 0.97 s more at p90, and dropped words after the last pause that the VAD missed.
- **Qwen's running words at the endpoint as the transcript.** They equalled the final words in
  12 of 30 clips.

## Consequences

- Every utterance's audio leaves the house for Alibaba Cloud in Singapore, through the brain.
- While the brain or Model Studio is unreachable, turns are heard by SenseVoice alone, and a
  stream that does not answer holds the turn up to 4 s.
- Each turn waits for Qwen after the endpoint: a median 0.59 s on the 66 replayed turns, where
  the hybrid's Whisper words are usually ready at it.
- Qwen gives no score, so under `asr_engine: qwen` the `confidence` is SenseVoice's for the
  same audio.
- Captions stay SenseVoice's. Qwen's running words reach the terminal and are not shown.
