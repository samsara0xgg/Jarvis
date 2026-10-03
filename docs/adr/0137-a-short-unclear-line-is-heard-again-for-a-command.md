# ADR 0137 — A Short Unclear Line Is Heard Again for a Command

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- ADR 0132's final ASR sends a clip with under 1 s of speech to SenseVoice
  alone, and a longer one to Whisper large-v3-turbo with the plain
  simplified-Chinese prompt. Neither knows the spoken commands.
- 2026-10-02, hands-free, Allen said 「退下」 about 15 times. 15 turns heard
  it as 对下, 陛下, 配下, 背下吧, 推萨吧, 被上班, 对一下 or 对象, and each
  became a wrong answer. The command classifiers (`is_whole_dismissal`,
  `is_stop_request`, `is_wait_request`) match the words as heard, so a
  mishearing is a turn.
- Offline, on 265 real short recordings and 344 synthesized ones: hearing the
  same audio again with Whisper, language zh, and the prompt 「以下是普通话的简体中文转录，
  常用口令：退下，停，等我一下，继续说。」 turned 10 of the 15 misheard
  dismissals into 退下 or 退下吧, and none of the synthesized non-command
  phrases into a command.
- The false hit seen: a clip SenseVoice heard as 「你是谁？」 came back from the
  second pass as 「再见。」. English outputs of the second pass ("Bye!",
  "Peace out!", "Anyway...") appear on noise and on ordinary words.
- mlx cannot run two passes at once; the existing Whisper recognizers share
  one model and one lock.

## Decision

`HybridFinalRecognizer` hears a short unclear line a second time with a
Chinese Whisper that carries the command prompt, and uses that transcript
only when it is a Chinese command.

Limits: short and unclear is SenseVoice language zh or yue, 2-4 characters
with punctuation removed, all Chinese characters, not ending in a question
mark, and none of `is_dismissal`, `is_stop_request`, `is_wait_request`,
`is_backchannel` already true. A command is all Chinese characters and a whole
dismissal, a wait request or a Chinese stop phrase. Audio under 0.15 s or
below the Whisper level floor is not heard again; a failing second pass keeps
the first transcript. The command Whisper is optional (`None` is the old
behavior), is built beside the other two where the hybrid is wired, and is not
prewarmed separately: it shares the model the zh pass already loads. The log
line carries only whether it found a command (ADR 0067).

## Alternatives rejected

- **The command prompt on every Whisper pass** — it biases ordinary
  sentences toward the listed words; the plain prompt exists for that reason
  (ADR 0132 tuned without a word list).
- **Pinyin fuzzy matching of the first transcript** — no pinyin dependency
  is installed, and 陛下 and 对象 are real words: the mishearings cannot be
  told from the speech without hearing the audio again.
- **Accepting English second-pass outputs** — "Bye!" came back from noise
  and "Peace out!" from non-commands; only Chinese commands are used.
- **Hearing every short line again** — a question or a line that is already
  a command gains nothing and the clip that returned 「再见。」 for
  「你是谁？」 was a question.

## Consequences

- A short unclear line costs one more Whisper pass, 0.4-0.5 s measured
  (median 416 ms, max 484 ms on the 265 recordings); lines that are
  already commands, questions, backchannels or longer than 4 characters pay
  nothing.
- A real short phrase can still be replaced when the second pass writes a
  command for it: of 265 recordings it changed 7: six became a dismissal
  (退下, 退下吧, 再见), one, 体育, became 停.
- A command misheard as something that already is a command, a question or
  a listening sound is not recovered.
