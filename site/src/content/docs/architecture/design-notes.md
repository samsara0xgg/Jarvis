---
title: "Design notes"
description: "Four decisions behind Jarvis, each with what was measured and the option that lost: interrupting her, long answers, staying fast as it remembers more, and the pops in her voice."
---

Every decision below has a record in [`docs/adr`](https://github.com/samsara0xgg/Jarvis/tree/main/docs/adr) with a mandatory "Alternatives rejected" section. These are four of them, told as stories. The numbers come from those records.

## Interrupting her

**The problem.** The first version, [jarvis-legacy](https://github.com/samsara0xgg/jarvis-legacy), had an interrupt feature that, measured later, had never fired once. Here you can talk over her at any point. But a microphone next to a speaker hears her own voice, and a cough as readily as a command.

**What was measured.** A "Hey Jarvis, stop" style barge-in raised 7.53 false candidates a minute on the MacBook speakers, against a target of 0.5. Stopping at the first sign of speech, as the first version of barge-in did, fired three times with nobody talking on the reSpeaker (2026-09-25), on short sounds near -40 dBFS.

**The decision.** When speech starts she gets quieter (to 0.2 gain). Once the sound holds 0.8 s of voice she is held where she is, and what the speech recognizer heard decides: a hum lets her go on, "stop" ends it, anything else stops her and becomes a turn. Echo is handled by whichever mic is open. A reSpeaker XVF3800 removes her voice on the board, so software echo cancellation stays out of its way; any other microphone goes through software echo cancellation (WebRTC AEC3).

**What lost.** Stopping at onset: a cough, a "嗯" or the TV ended her answer and cancelled its generation. Stopping on voiced time alone: a long "嗯嗯嗯" stopped her, and a quick "停" did not.

**The result.** Five live runs. In the last, all five "嗯" passed, "pause" and "可以啦" stopped her without an answer, and a stop no longer popped (it had, until a 20 ms fade). The price: a short "停" now takes 0.5 to 0.7 s from the first sound. Echo suppression on real speakers is unmeasured.

Decisions: [0100](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0100-jarvis-yields-to-speech-over-her-and-its-words-decide.md), [0041](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0041-wave-mode-listens-without-a-wake-word.md), [0143](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0143-echo-cancellation-follows-the-open-microphone.md)

## Long answers

**The problem.** A model writes an answer to be read. Read aloud word for word it drags: on 2026-09-24 a weather lookup came out as 729 characters of speech in English, and answers played for up to 49 s. In one test a 471-character answer played for about 39 s before the author cut in at 20.7 s.

**What was measured.** The first fix was a second model request that rewrote any long answer into one to three sentences. It cost 1.3 to 1.9 s per long turn, and the screen showed different words from the ones heard. Asking the model for voice and document tags failed too: in a test it left them out in 6 of 8 answers.

**The decision.** No second request: every request of the turn carries a strict JSON shape. `spoken` is one or two sentences, said as it streams, and `written` carries lists, times and links for the screen. In a probe on 20 real turns, all 20 came back valid and `spoken` always came first, with a median of 0.75 s to the first spoken character against 0.66 s for plain text.

**What lost.** Tightening the prompt (the weak model ignored it). Cutting the text by code, which the author rejected. Keeping the second request, at 1.3 to 1.9 s a turn.

**The result.** The words on screen are the words she says, and they light up as she says them; the written part arrives under them when she is done. The weak spots are named in the record: `written` can invent details (it once made up example commands), so the switch ships off until it has been watched on live turns.

Decisions: [0114](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0114-a-structured-spoken-answer-carries-spoken-then-written.md), [0040](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0040-a-spoken-answer-is-its-spoken-form.md)

## Staying fast as it remembers more

**The problem.** Every turn decoded the whole event log before calling the model. On the live trace the wait before the request grew from 0.16 s at 16k events to 1.36 s at 48.6k, and a log only grows.

**What was measured.** One read cost about 0.54 s on a copy of the real log (48,952 events), nearly all of it decoding. A turn reads at least twice before its first request.

**The decision.** Fold from a high-water mark: keep the folded state in memory, and read only the events added since. A reader uses it only if it can prove it is a prefix of the log it sees, and otherwise folds everything. When idle, a background check compares it with a whole-log fold every 100th read.

**What lost.** Caching the snapshot while the newest event id is unchanged: a turn appends its own input first, so it misses on exactly the costly reads. A recent window: projections depend on events of any age. Materialised tables: they make the reader write.

**The result.** The same answer as a full fold, by construction and by test, and a canary fails if a hot path folds the whole log. Two provider-side changes cover the rest. Asking OpenAI for its fast tier on spoken turns only (16 paid calls) cut the first token's median from 1.04 s to 0.68 s, at about twice the price. And the speech provider pads every segment with silence (median 182 ms at the start and 261 ms at the end over 878 recorded segments), about 440 ms of dead air at each junction; trimming only the quiet samples ends each answer about 340 ms sooner. Both are opt-in: the shipped config leaves them off.

Decisions: [0164](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0164-the-decision-snapshot-folds-the-log-from-a-high-water-mark.md), [0166](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0166-a-spoken-turn-asks-openai-for-its-fast-service-tier.md), [0165](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0165-the-spoken-sample-timeline-drops-provider-silence-at-start-and-junctions.md)

## Pops in the audio

**The problem.** Faint clicks in replies looked like a streaming bug: a seam between audio chunks, or the resampler.

**What was measured.** Neither. Across 99 real junctions between segments the largest sample step was 0.0000, with a per-segment or a shared resampler. Then the same sentences at MiniMax's volume setting 1, 2 and 3 (2026-09-07): loudness (RMS) rose linearly, but the peak reached only 2.8 times at 3. The provider's own limiter was squashing loud syllables, which is the crackle, and at 5 it hard-clips its 16-bit output.

**The decision.** Leave the provider's volume at its default of 1 and change loudness on our side. A volume below 1 does not lower the audio MiniMax returns (measured 2026-09-25), so her playback level is a gain in our own player.

**What lost.** A fix at the seam: a shared resampler changes nothing when the step is already 0.0000. A limiter or de-clicker of our own: the damage is done before the audio reaches us, so nothing on our side can undo it.

**The result.** At 1 the voice is clean; a louder one comes from the Mac's output volume. Later she could be asked to speak louder, in steps of 1.4 times or 2 times, and anything above 200% waits for a yes.

Decisions: [0165](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0165-the-spoken-sample-timeline-drops-provider-silence-at-start-and-junctions.md), [0052](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0052-the-settings-page-writes-a-file-laid-over-the-yaml-at-boot.md), [0174](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0174-her-voice-volume-and-speed-are-set-by-the-model-through-one-tool.md); the volume measurement is in the comments of [`config/jarvis.yaml`](https://github.com/samsara0xgg/Jarvis/blob/main/config/jarvis.yaml) (`tts_volume`).
