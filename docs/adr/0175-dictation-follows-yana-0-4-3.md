# ADR 0175 — Dictation Follows 言字 0.4.3

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** none

## Context

- ADR 0110 brought Jarvis's right-⌥ dictation to 言字 0.4.0 (typeless-local
  5fddcba). 言字 has since shipped 0.4.1 through 0.4.3 (621fa64); Allen asked
  for the same improvements in Jarvis's version.
- 言字 0.4.1 rewrote its polish prompt to write in the language spoken, never
  translating any part of a mixed sentence, and sends the recognizer's guess
  (`Recognizer's language guess: en`) after the transcript, which the prompt
  says the transcript outranks. Jarvis's prompt still said the user speaks
  Chinese mixed with English terms.
- 言字 0.4.1 also hears a short clip again: with the spoken language on
  Automatic, a clip under 4 s that Whisper heard in a language outside the
  user's own (a stray word drew Korean or Icelandic) gets Whisper's
  probabilities on the same audio and is decoded again as the likeliest allowed
  language. 言字 reads its allowed set from the Mac's languages and keyboards
  and from past long dictations.
- 言字 0.4.3: a plain Return while a dictation records is swallowed and
  finishes it; once the words are pasted it waits 0.1 s and presses Return in
  the front app, so a chat message goes out. Words that were copied instead
  of pasted send nothing.
- 言字 0.4.1 to 0.4.3 also added hot-word ranking, edit learning, English
  practice, a word book and screenshot translation.

## Decision

Jarvis's dictation polishes with 言字 621fa64's prompt and the language of the
stretch with the most text as the recognizer's guess, hears a clip under 4 s
that Whisper placed outside Chinese and English again as the likelier of the
two when `dictation.language` is empty, and sends the message when Return, not
the right ⌥, finished a dictation whose words were pasted straight in.

## Alternatives rejected

- **Hot-word ranking** — his whole `user` list fits in Whisper's 600-character
  prompt today, so there is nothing to rank away.
- **Edit learning** — the shared `~/.typlus/vocab.yaml` already carries the
  words 言字 learns from his edits, and Jarvis reads it on every dictation.
- **Reading the Mac's languages and keyboards for the allowed set** — he
  dictates only Chinese and English; a fixed pair costs no Carbon call and no
  second copy of 言字's merge rules.
- **English practice, the word book, screenshot translation** — separate
  features of 言字's own window; none is part of typing where the caret is.
- **Send after a box-fixed paste too** — words he read and corrected are a
  message he looked at; Return before the card means "send what I said".

## Consequences

- A clip under 4 s outside Chinese and English costs one more language
  detection and one more decode, on top of the pass ADR 0110 already costs;
  a third language he starts to dictate would be forced into Chinese or English
  until the pair in `jarvis/runtime/dictation.py` grows.
- While a dictation listens, Return belongs to Jarvis: no other app sees it,
  as Esc already is. Keypad Enter is not covered: Electron's accelerator names
  have no token for it.
- A dictation whose polish fails, or whose words are copied instead of pasted,
  presses nothing, so a Return he meant as "send" is lost with it.
