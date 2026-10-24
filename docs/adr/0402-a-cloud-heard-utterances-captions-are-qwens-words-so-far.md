# ADR 0402 — A cloud-heard utterance's captions are Qwen's words so far

**Status:** Accepted
**Date:** 2026-10-23
**Supersedes:** the consequence in ADR 0400 that captions stay SenseVoice's. Everything else in ADR 0400 stands.

## Context

- Under `realtime.final_asr: cloud` (ADR 0400) the turn takes Qwen's words, while the captions
  were SenseVoice's snapshots, decoded on the terminal every 240 ms
  (`partial_asr.interval_ms`). The two disagree where SenseVoice is weak: it writes "Jarvis" as
  「javis斯」 and 「jabi」, and 「从一数到十」 as 「从一数到11」, which the turn then does not say.
- Qwen's words so far already reach the terminal as `hear` `text` frames, each time they change.
- The owner, 2026-10-23, once Qwen hearing was live: the screen should show the words changing
  one by one, as Qwen's streaming recognizer writes them.
- Qwen's words trail the voice: on his 16 longest clips at speaking speed, a word appeared a
  median 0.51 s (p90 0.98 s) after it was said, the first one 0.48 s after he began. Through the
  brain at home this adds about 0.01 s.
- They carry no timestamps, so which audio a given update covers is not known.
- Qwen rewrites its last sentence after the audio ends: its words at the end of the audio matched
  the final ones in 12 of 30 clips.
- A stream can fail or never answer while the turn still falls back to SenseVoice's words.

## Decision

From the first words Qwen brings for an utterance, the terminal's captions are Qwen's words so
far and SenseVoice's snapshots stop for that utterance; before them, and for an utterance Qwen
never answers, the captions stay SenseVoice's.

## Alternatives rejected

- **Qwen's words as the settled part and SenseVoice's tail after them (ADR 0145's split)** —
  the split needs the byte where Qwen's words end. With no timestamps the only cut is the audio
  sent so far, which runs about 0.5 s past Qwen's words, so the words said in that gap would
  vanish until Qwen's next update; an earlier guessed cut would show them twice.
- **Qwen's words only, from speech onset** — the first caption would wait 0.48 s (median)
  instead of SenseVoice's first 240 ms snapshot, and an utterance whose stream fails would show
  no caption at all while its turn is still heard by SenseVoice.
- **Keep SenseVoice's captions (ADR 0400)** — the screen keeps showing words the turn will not
  use, such as 「javis斯」 for the Jarvis the turn hears.

## Consequences

- Captions trail his voice by about half a second, up to about one second, where SenseVoice's
  trailed by one snapshot interval and its decode.
- The first half second of an utterance can show SenseVoice's words that Qwen's then replace.
- The captions can still change when the turn's words land, since Qwen's final words differed
  from its words at the end of the audio in 18 of 30 clips.
- Qwen's captions are one `text`: the `settled` and `tail` split of ADR 0145 does not apply.
