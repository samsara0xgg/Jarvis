# ADR 0150 — Hybrid Whisper passes hear a short word list

**Status:** Proposed
**Date:** 2026-10-03
**Supersedes:** none

## Context

- ADR 0132 gave the hybrid's Chinese and English Whisper passes no word list, on Allen's decision
  (2026-10-02), because the 61-term `~/.typlus/vocab.yaml` list changed 10 of 53 turns without
  fixing a term. 言文 puts a `Common terms: a, b.` line in Whisper's prompt.
- Live, the passes mis-hear his own words: 「多伦多」 as 「多人多」 (T7a9d9911), "Jarvis" as "Javis".
- 2026-10-03 replay of the 303 newest kept turns through the real recognizer, three ways
  (A as live, B a 12-term list, C the list plus his last utterance and her last spoken reply,
  150 characters): B changed 65 turns, C 102, against A.
  - B: "Javis" to "Jarvis" in 9 turns, 「多人多」 and "Torrento" to 「多伦多」, no fullwidth artifact.
  - C: eight turns with a fullwidth "Ｂ" where punctuation belongs, carried-over words from her
    reply (「再决定这段关系」), and a copied misheard line; it also fixed "Eva" in two turns.
  - Median per pass 446 ms (A), 472 ms (B), 477 ms (C); p90 542, 584, 622 ms.
- Allen decided to ship the list alone (2026-10-03).

## Decision

When `realtime.final_asr` is `hybrid`, put `realtime.final_asr_terms` as one `Common terms: a, b.`
line after the simplified-Chinese hint in the prompt of the Chinese and English Whisper passes
only. The list stays a dozen or so words.

- **Not the command pass.** The ADR 0137 pass keeps its own prompt and 16-token cap.
- **Retries.** A looped transcript is heard as nothing (`retry_loops` is off in hybrid), so
  SenseVoice's words stand.

## Alternatives rejected

- **The 61-term Typlus list** — the replay of 2026-10-02 changed 10 of 53 turns and fixed no term.
- **The list plus the last two conversation lines** — tried in the replay above and rejected: it
  wrote fullwidth artifacts and carried words over from her reply, for two more fixes ("Eva").

## Consequences

- "Jarvis", 「多伦多」 and the other listed words are heard right more often; a word missing from the
  list is not helped, and the list is Allen's to keep short.
- A pass's prompt is about 25 ms slower at the median, 40 ms at p90.
