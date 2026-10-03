# ADR 0145 — The live captions show Whisper's words for the audio it has heard

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- Allen, 2026-10-03: with `realtime.final_asr: hybrid` (ADR 0132) the words of a
  line on screen while he speaks are SenseVoice's (ADR 0111), but the final
  text of any line with 1 s of speech or more is Whisper's, so at the end the
  whole line jumps to different words, which he finds odd.
- The hybrid already runs a Whisper pass at every pause of 192 ms, on the audio
  up to that pause. Replayed in real time on one 6.8 s Chinese recording
  (T7a9d9911, 5.5 s of speech) the passes took 466, 889 and 544 ms and
  finished 466 to 889 ms after the pause began.
- SenseVoice decodes about 15 ms per second of audio (ADR 0111), so a decode of
  only the audio after a pause is never longer than today's whole-utterance
  one. On the same recording the 25 caption decodes took 20-79 ms (median 39)
  against the 240 ms cadence.
- A Whisper pass runs on the hybrid's worker thread; the caption state
  belongs to the capture thread, which owns the utterance.

## Decision

While he speaks, the caption is the words of the newest finished Whisper pass
for this utterance (`settled`) followed by SenseVoice's decode of only the
audio after that pass (`tail`).

Its limits:

- A finished pass counts even if he kept speaking and its entry was discarded
  for the final; a newer finished pass replaces an older one, and one for an
  utterance that is no longer current is ignored. Before any pass finishes the
  whole caption is tail, and a line under 1 s of speech never has a pass.
- The `partial` voice phase keeps `text`, now `settled` plus `tail`, and adds
  `settled` and `tail`, sent only when the final recognizer prepares passes. A
  surface shows the tail dimmer; the final words replace the line as before.
- It applies only with `partial_asr.captions` and without `partial_asr.enabled`:
  the semantic endpoint needs hypotheses of the whole utterance, so it keeps
  them, and `sensevoice` and `whisper` final ASR caption exactly as before.

## Alternatives rejected

- **Keep decoding the whole utterance and show Whisper's words only at the
  final** — that is today's jump, which is the complaint.
- **Run Whisper on a fixed cadence for the captions** — a pass is 0.5-0.9 s
  and mlx cannot be interrupted, so passes every 240 ms queue behind each
  other and delay the final's own pass past the endpoint.
- **Show SenseVoice's whole-utterance decode after the settled words** — it
  repeats the words Whisper has settled, so the line shows them twice or has
  to be spliced by text matching across two recognizers.

## Consequences

- The caption can show a Whisper misreading until the next pass or the final:
  on the recording above the second pass wrote 「更多的简单的简单的简单」 for
  what was said and the line kept it for 2.6 s.
- The tail is empty for up to one decode (about 40 ms) when a pass is taken,
  then grows again from the audio after the cut.
- A pass dropped before it starts (he resumed while another pass was running)
  never settles anything; the earlier settled words stay.
