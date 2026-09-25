# ADR 0042 — Wake on microWakeWord

**Status:** Accepted
**Date:** 2026-09-24
**Supersedes:** none

## Context

- Allen's complaint, 2026-09-12: Jarvis wakes when he says a bare "Jarvis"
  (he pronounces it "Javis", r-less) or nothing wake-like at all. Recall was
  never the problem.
- The engine ADR-0005 shipped, openwakeword `hey_jarvis_v0.1` at 0.5, scores a
  bare "Javis" 0.98-0.999, as high as his cleanest "Hey Javis". Its threshold
  has no leverage: on 31 labelled clips of his voice, a gate of 0.9 still
  admits 5 of 17 negatives while dropping 3 of 14 positives.
- microWakeWord `hey_jarvis` v2 (pymicro-wakeword, INT8 tflite, macOS wheel)
  is near-binary on his voice: every bare name scores <= 0.03, "Oh Javis"
  0.31, "Hey you you" 0.10, a connected "Hey Javis" 0.82-0.996.
- Its one failure mode is measured and causal: it tolerates about 90 ms of
  silence between "Hey" and "Javis"; 110 ms scores 0.11-0.20, 170 ms and
  beyond 0. Allen ruled that a long pause should not wake.
- Replay of the 31 clips through the daemon's own frames: microWakeWord@0.95
  wakes 6/14 positives with 0/17 false accepts (5 misses are paused takes, 3
  quiet takes at 0.82-0.95); openwakeword@0.5 wakes 13/14 with 6/17.
- Detection lands later: +150 to +320 ms after the last syllable versus +30
  to +170 ms for openwakeword@0.5. Compute is 1.2 ms per audio second versus
  22.9 ms.
- Only the INT8 artefact is published; the model cannot be fine-tuned on
  Allen's voice.
- Allen, 2026-09-13: "不用测了就这么决定了 只用mww", then "就用0.95吧 确定了";
  and on keeping the old engine switchable, "把之前 openWakeWord 包装起来，
  这样到时候我可以随意切换". On 2026-09-24 he confirmed 0.95 had held on the
  reSpeaker XVF3800 with its echo cancellation fed by the Multi-Output Device.

## Decision

Wake on microWakeWord `hey_jarvis` at 0.95, with openwakeword kept as a
config-selectable fallback behind the same engine contract; no second stage
sits in the wake path.

## Alternatives rejected

- **openwakeword with a higher threshold** — at 0.9 it still false-accepts
  5/17 of Allen's labelled negatives and loses 3/14 positives.
- **openwakeword plus an EfficientWord-Net second stage** — enrolled on
  Allen's own recordings, positives scored 0.67-0.84 and negatives 0.67-0.78;
  two thresholds fit on about ten clips each broke on the next session.
- **openwakeword's custom verifier trained on Allen's clips** — worse than
  no verifier; "Hey, you you" scored 0.896.
- **openwakeword@0.9 gating microWakeWord@0.6** — only cost recall: takes
  032 and 036 scored 0.99 on microWakeWord and were blocked by openwakeword
  at 0.79 and 0.87.
- **SenseVoice transcript rule ("hey" then a j-word)** — separated the
  labelled clips 6/6 and 0/10, declined by Allen.
- **Picovoice Porcupine** — signup requires a company email and offers only
  a 7-day enterprise trial.
- **Retraining openwakeword** — its training stack pins torch 1.13.1 and does
  not install in 2026 (upstream issue #317).

## Consequences

- "Hey Jarvis" must be said as one phrase; a pause of about 100 ms or more
  between the words stays silent.
- Quiet or far takes scoring 0.80-0.95 do not wake; lowering the threshold
  is the only lever, since the model cannot be retrained.
- The command after the wake phrase starts 150-320 ms later relative to the
  phrase than it did, so a phrase run straight into a request leans harder
  on capture replay.
- `realtime.wake_threshold` means a different scale per engine; switching
  `realtime.wake_engine` without changing it gives a nonsense gate.
- pymicro-wakeword joins the locally installed voice wheels outside
  `[project].dependencies`; a machine without it runs text-only.
- Unmeasured: other people's near-miss phrases and a full day of ambient
  audio.
