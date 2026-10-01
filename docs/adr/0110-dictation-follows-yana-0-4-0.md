# ADR 0110 — Dictation Follows 言字 0.4.0

**Status:** Accepted
**Date:** 2026-10-01
**Supersedes:** 0077

## Context

- ADR 0077 built Jarvis's right-⌥ dictation on Typlus's engine as it stood on
  2026-09-26: local Whisper large-v3-turbo forced to Chinese with the
  simplified-Chinese prompt and no word list, Typlus's polish prompt of that
  day (5f43b0a) on gpt-5.4-mini, and ⌘V into whatever app was in front.
  The daemon owns the mic, the hearing and the polish; the companion owns
  the key, the caret and the paste.
- Typlus became 言字 (Yana) and shipped 0.4.0 on 2026-10-01 (typeless-local
  5fddcba). On 2026-10-01 Allen asked for its improvements in Jarvis's
  version: "synchronize that to the Jarvis version".
- 言字 050d27f puts the user's word list back in Whisper's prompt with
  `condition_on_previous_text` off, only for audio within one 30 s window,
  and hears a looped result again without the list. ADR 0077 rejected the
  list on runs that had neither guard.
- 言字 ships with Whisper telling the language (`language: ""`); Allen's own
  copy was switched to English on 2026-10-01 and he dictates in both. A
  forced language garbles the other one: forced Chinese turned 1 of 3 pure
  English sentences into Chinese on 2026-09-26. Telling costs one more
  encoder pass, ~0.5 s per stretch.
- 言字's silence gate catches only a dead or muted mic (loudest 0.2 s at
  0.003 RMS); Jarvis's dictation used SenseVoice's 0.01, which quiet speech
  on the reSpeaker falls under. It drops a short fragment heard as another
  language unless it is confident English, and never drops Chinese for
  length; Jarvis dropped anything under two characters, a lone 「好」 too.
- 言字's prompt (f98557b, 5fddcba) adds his profile, a Chinese filler list,
  formatting rules and six real examples; at ~1,500 tokens with the word
  list it is past OpenAI's 1,024-token cache minimum, and 言字 sends a fixed
  `prompt_cache_key` with 24 h retention: 1,525 of 1,568 prompt tokens came
  from cache on 2026-10-01. It polishes on gpt-5.6-terra without reasoning,
  and may send the 300 characters before the caret as spelling context
  (Allen has it on).
- 言字 0.4.0: Claude's desktop app can move focus off its composer while the
  words are polished, so the ⌘V landed nowhere while the capsule said
  "Inserted". 言字 now checks the app in front, puts the caret back in the
  field the dictation started in, and copies the words when it cannot.

## Decision

Jarvis's dictation hears and polishes as 言字 0.4.0 does: Whisper tells the
language unless `dictation.language` names one (the simplified-Chinese
prompt only for Chinese), the word list goes into Whisper's prompt for
stretches within one 30 s window, only a dead mic is cut before the model,
a short fragment in another language is noise unless confidently English,
Chinese clauses get full-width punctuation, a session runs up to fifteen
minutes, and the polish uses 言字's prompt, the transcript in tags, the text
before the caret, gpt-5.6-terra and a cacheable request; the companion
pastes only into the app and text field the dictation started in, putting
the caret back there first, and otherwise copies the words and shows them.

## Alternatives rejected

- **Keep ADR 0077's forced Chinese** — his dictation is now English as well
  as Chinese, and forced Chinese garbled 1 of 3 English sentences.
- **Keep the word list out of Whisper** — 0077's loops came from decodes
  without `condition_on_previous_text` off and across 30 s windows; 言字 runs
  the list with both guards and re-hears any loop without it.
- **Read the language from 言字's own config** — 言字 layers a bundled file
  under the user's, so Jarvis would have to copy that merge, and a change
  in 言字's format would silently change what Jarvis hears.
- **Paste after a failed polish instead of copying** — 言字 pastes the raw
  words; Jarvis copies them and says the polish failed, so nothing is lost
  either way and the raw words do not land as if polished.
- **Check after the paste whether the field changed** (言字's
  "might not have gone in") — the refocus covers the reported case; the
  check needs a second Accessibility read loop for a message only.

## Consequences

- Each stretch costs ~0.5 s more while the language is told; setting
  `dictation.language` to `zh` or `en` buys it back and garbles the other.
- gpt-5.6-terra costs about 2.7 times gpt-5.4-mini per uncached token;
  the cached prompt is billed at a tenth.
- The text before the caret leaves the Mac with every polish; password
  fields are never read.
- A dictation that started in one app and ends with another in front is
  copied, not pasted, even when the move was deliberate.
- Fresh venvs and the packaged app still hear with SenseVoice, with none of
  the Whisper rules above.
