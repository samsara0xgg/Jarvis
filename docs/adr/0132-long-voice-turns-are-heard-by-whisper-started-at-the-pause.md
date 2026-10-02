# ADR 0132 — Long voice turns are heard by Whisper, started when Allen goes quiet

**Status:** Proposed
**Date:** 2026-10-02
**Supersedes:** none

## Context

- A voice turn's words come from SenseVoice-small int8 (ADR-0005): a median of 48 ms after the
  commit, and it also writes the live captions and the semantic endpoint's snapshots. On Allen's
  recorded turns it hears numbers and names wrongly: counting to ten came back as 「从一数到11」
  and 「从一数到1」, and "Jarvis" as "Javis" and "Gary".
- `realtime.final_asr: whisper` (proposal) lets local Whisper large-v3-turbo write every turn,
  but it starts after the commit: 0.4-0.9 s, and for very short clips it writes words that were
  never said ("Yeah" became "Thank you", 「不询问猫头鹰」 became nothing).
- The acoustic endpoint commits an utterance after 24 silent VAD frames (768 ms). A Whisper pass
  does not need that silence: started 192 ms into the pause it hears the same words, and can
  finish before the commit.
- mlx cannot be interrupted and passes share one model and one lock: a pass that is no longer
  wanted still finishes before the next starts.
- Replay of Allen's 80 newest recorded turns of 2026-10-02 through the real recognizer on its own
  clock (`tools/hybrid_asr_replay.py`: a pass started at the 6th silent frame of each pause,
  collected at the 24th; the live daemon running beside it):
  - 42 turns have 1 s of speech or more (22 zh, 20 en) and 38 have less.
  - A Whisper pass with the language fixed took a median of 362 ms (p90 385 ms); an earlier run
    of the same turns with the language auto-detected took 871 ms against 416 ms with it fixed.
  - 41 of the 42 long turns waited no longer than SenseVoice alone. The 42nd, 12.2 s of speech
    with five pauses inside it, waited 192 ms longer (209 ms in a first run): six passes of
    370-473 ms ran for it, one after another, the last behind the pass for an earlier pause.
  - The 42 long turns cost 63 Whisper passes: 21 were for a pause Allen then spoke through (11
    turns, up to 6 passes in one).
- Allen's decision (2026-10-02): no word list for these turns, and the language fixed per
  utterance from SenseVoice.

## Decision

When `realtime.final_asr` is `hybrid` and mlx-whisper is installed, keep SenseVoice for the
captions and for any utterance with under 1 s of speech, and let local Whisper large-v3-turbo
write the final transcript of every other one, in the language SenseVoice detected and with no
word list, starting its pass as soon as Allen has been quiet for 192 ms.

- **Language.** `zh` and `yue` (SenseVoice misreading Mandarin) go to Whisper fixed to zh with
  the simplified-Chinese prompt; `en` to Whisper fixed to en with no prompt; any other language
  keeps SenseVoice's words. The result keeps SenseVoice's language and emotion.
- **Fallback.** An empty Whisper transcript, a Whisper error, or a clip under 0.15 s or without
  0.2 s at 0.003 RMS keeps SenseVoice's words. Bare Whisper text gets a closing stop, as
  `whisper` mode does. Without mlx-whisper the daemon warns and uses SenseVoice.
- **Speculation is a hint.** At 6 silent frames the session hands the recognizer the audio so
  far and the speech span; speech afterwards discards it. The hand-over only enqueues, so
  capture never waits. At the commit the recognizer takes the prepared pass if it heard a prefix
  of the committed audio, waiting only for what is left of it, and otherwise hears the audio
  then, with the span the session measured. Under 1 s of speech nothing is prepared, so a short
  utterance is heard exactly as `sensevoice` hears it.
- **Other paths.** PTT and the legacy wake path supply no utterance id and speech span, so the
  length of the clip stands for its speech. The default stays `sensevoice`.

## Alternatives rejected

- **Whisper for every utterance** — on 79 of the same turns it rewrites short clips ("Yeah" to
  "Thank you") and, run after the pause, adds up to 205 ms of wait on one turn.
- **Whisper with the language auto-detected** — a second encoder pass, 871 ms median against
  416 ms with it fixed, and it hears a short Chinese phrase as English (「继续讲吧」 became
  "See you in the next one").
- **Whisper with the dictation word list in its prompt** — of 53 turns compared, 10 changed,
  mostly fillers kept, and no term was heard that the plain prompt missed; Allen declined it.
- **SenseVoice only** — keeps 「11」 for ten, "Javis" and "Gary" for Jarvis, all of which the
  replay has Whisper hearing correctly (「十」, "Jarvis").

## Consequences

- Each of Allen's mid-sentence pauses of 192 ms or more starts a pass that is thrown away. It
  cannot be cut short, so a pass still running when he resumes delays the next by what is left
  of it, and a long utterance with many pauses can wait past the endpoint (one turn of 42, 192 ms).
- The captions are SenseVoice's and the committed turn is Whisper's, so the text can change at
  the commit, and Whisper is not always right: the replay has it hearing 「说自己」 as 「数字戟」.
- With the semantic endpoint on, a `post_roll_ms` under 192 ms trims the committed audio to less
  than what was prepared; the pass is then not a prefix and the commit hears afresh, with the
  full Whisper wait.
- For PTT and the legacy wake path the clip's trailing silence counts as speech, so a short
  utterance with a long silence can still go to Whisper.
- Whisper runs on the GPU the daemon shares with every other local model.
