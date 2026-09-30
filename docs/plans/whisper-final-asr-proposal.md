# ADR NNNN — A Voice Turn Can Be Heard by Whisper as 言文 Hears It

**Status:** Proposed
**Date:** 2026-09-30
**Supersedes:** none

## Context

- 2026-09-30 Allen: 言文 (ex-Typlus, samsara0xgg/typeless-local, app 言字)
  now hears "非常非常准确" with its latency cut down; he asked the voice path
  to align its recognition model and thresholds with it.
- A voice turn's words come from SenseVoice-small int8 (ADR-0005): about
  0.1 s after the endpoint, text normalization that closes every sentence,
  a language and emotion tag. The same SenseVoice decodes the snapshots the
  semantic endpoint reads (ADR-0006 D7, 240 ms budget) and the words ADR
  0100's barge-in verdict reads.
- 言文 at 9b11024 hears with mlx-whisper large-v3-turbo, fp16, temperature
  0, language fixed to zh (c5c905c: identical text on 40 recordings, ASR
  p50 1.10 → 0.60 s), `condition_on_previous_text=False`, the prompt
  「以下是普通话的简体中文转录。」 plus `Common terms: a, b.` from the hand-kept
  `user` list of `~/.typlus/vocab.yaml` (≤ 600 characters); a transcript
  with a 2-16 character unit three times in a row, or one character eight
  times, is decoded again without the list and with temperature fallback
  0.0-1.0 (e94d055); a transcript that is only a subtitle credit is
  silence. Before the model it drops only a clip under 0.15 s or with no
  0.2 s at 0.003 RMS, a dead or muted mic (5231742). After 20 s idle a pass
  took 1.0-1.2 s against 0.37 s warm, so it warms when recording starts
  (c257562).
- ADR 0077 measured the two on Allen's dictation stretches: Whisper's CER
  0.116 / 0.302 / 0.390 (2-10 / 10-40 / 40+ s) against SenseVoice's
  0.149 / 0.322 / 0.431, most of SenseVoice's extra misses English and
  product names, and 0.47-0.61 s after the stop against ~0.1 s. With the
  word list in the prompt Whisper looped in 2 of 60 and did worse on the
  longer clips; 言文 has since added the loop re-decode. A voice turn is
  mostly under 10 s.
- SenseVoice empties a transcript whose whole clip averages under 0.01
  RMS; 言文 keeps anything with some 0.2 s at 0.003. Quiet speech, or a
  short answer inside a long quiet clip, is cut as silence today.
- mlx-whisper is on Allen's Mac outside `pyproject.toml` (ADR 0077); the
  packaged app does not ship it.

## Decision

`realtime.final_asr` chooses which model writes a voice turn's words:
`sensevoice` (default) or `whisper`. With `whisper` and mlx-whisper
installed, the authoritative transcript of a wake or PTT turn comes from
local Whisper decoded as 言文 decodes it (language zh, the simplified
prompt plus the `user` terms of `dictation.vocab_path`, no conditioning on
the previous window, the loop re-decode, subtitle credits dropped), gated
as 言文 gates (≥ 0.15 s, some 0.2 s ≥ 0.003 RMS), and closed with 「。」 or
"." when it ends without punctuation, as SenseVoice's normalization would
have closed it. SenseVoice keeps the endpoint snapshots and the barge-in
words. When Allen starts talking after 20 s without a Whisper pass, one
silent pass warms it in the background. Without mlx-whisper the switch
logs a warning and SenseVoice hears.

Dictation keeps its own prompt without the list (ADR 0077); it shares the
decode rules: no conditioning, the loop re-decode and the dropped credits.

## Alternatives rejected

- **Keep SenseVoice and lower its floor to 0.003** — the floor only
  decides which quiet clips reach the model; SenseVoice's extra misses are
  names and English (ADR 0077), which it does not change.
- **Whisper for the endpoint snapshots too** — a snapshot has 240 ms; a
  warm Whisper pass is ~0.4 s.
- **Hear each stretch at a pause, as dictation does (ADR 0076)** — the
  semantic endpoint already commits within ~0.8 s of a pause, so there is
  little tail to hide; decoding a candidate turn at the first pause and
  keeping it when no word follows is the follow-up if the 0.3-0.5 s shows.
- **Auto-detect the language** — a second encoder pass (言文 c5c905c,
  +0.5 s) for a speaker who talks Chinese with English words; an English
  speaker keeps SenseVoice.
- **Use 言文 itself as a service** — a second process on the mic and a
  second copy of the 1.6 GB model; the daemon is the single owner of the
  mic.

## Consequences

- Each voice turn's words arrive about 0.3-0.5 s later than SenseVoice's
  (warm), up to ~1 s on the first turn after a long quiet spell if the
  warm-up has not finished.
- The emotion tag is gone from Whisper turns; the language tag is always
  zh.
- ADR 0100's barge-in verdict and the short-answer rules (`is_unclear_sound`,
  backchannels) were tuned on SenseVoice's text; the turn's committed text
  now comes from another model and needs the live check before this is
  Accepted.
- The daemon's Whisper model is shared with dictation (mlx-whisper holds
  one per process); a turn and a dictation stretch wait on one lock.
- Accepted, this amends ADR-0005's recognizer choice and, for voice turns,
  ADR 0077's "the word list stays with the polish". The recognition row
  of `docs/spec.html#one-key` (local models, no key) holds either way.
