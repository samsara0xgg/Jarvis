# ADR 0130 — Jev judges the short words the word lists cannot

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- Words heard over her voice (barge-in, ADR 0100) or in hands-free mode (ADR 0102) are
  sorted by word lists in `voice_asr.py` into: a listening sound (she goes on, or the line is
  dropped), a stop, a wait, a dismissal, or a turn. A list cannot read intent: 「我让你退一下」
  and 「你先退下吧我跟他说」 dismiss her because 退下 sits inside a short sentence, and
  「Yeah」, 「好吧」, 「别跟我说话」 are turns because no list holds them.
- ADR 0122, 0123 and 0125 already call Jev (TypeSafe's hosted decision model, through
  OpenRouter, ADR 0128's dataset) with a median of 0.14 s per call.
- Offline, 2026-10-02, on 590 lines (456 real turns, 80 hand-written, 54 unsure excluded),
  one choice question (keep_going, stop, wait, dismiss, none) at 0.95, scored as correct,
  and as a real request swallowed (a labelled `none` that was acted on):

  | strategy | real 456: correct / swallowed | hand 80: correct / swallowed |
  |---|---|---|
  | A word lists only (today) | 429 / 1 | 40 / 6 |
  | B Jev only | 426 / 0 | 58 / 2 |
  | C word lists, Jev for what they miss | 437 / 1 | 59 / 7 |
  | D as C, but a loose dismissal needs Jev | 435 / 1 | 64 / 2 |

  The 590 calls cost $0.018; latency median 140 ms, p90 209 ms.
- The real set holds only lines that reached `utterance.received`: words the lists absorbed
  were never kept, so A's real error count is a lower bound and nothing measures the lists on
  the lines they absorb.
- Allen's bar for voice words is lossless over fast: a real request swallowed is worse than
  a listening sound taken for a turn.

## Decision

When `realtime.jev_words.enabled` is true and `OPENROUTER_API_KEY` is set, keep the word
lists first, with one exception, and ask Jev for what they leave: a dismissal the lists find
only inside a sentence (not the whole line) counts only if Jev answers `dismiss` at `at`
(0.95); and a line the lists call a turn, at most `max_chars` (24) long without punctuation,
heard over her voice or in conversation mode, is put to Jev, whose confident answer
(`keep_going`, `stop`, `wait`, `dismiss`) acts as the matching list verdict would have.

- **Turn on doubt.** `none`, below `at`, past `timeout_ms` (800, she is held meanwhile), an
  error or no key: the line is a turn, as any unmatched line is, so with no key a dismissal
  inside a sentence no longer dismisses. With the setting off, nothing changes.
- **Dismiss and wait need the mode.** Over her but outside conversation mode (it closed
  while she was held) they act as a stop: the lists judge neither word there, so today the
  line stops her and is a turn, and she is stopped either way. With her silent, a `stop` has
  nothing to stop and the line stays a turn.
- **One question, kept.** `jarvis/decision/voice_words.py` holds it, over ADR 0122's
  transport (`use` `voice_words`, zero data retention); the surface gets two plain callables
  from `runtime/`. The text is her latest words, a bracketed note saying whether she was
  speaking, then `User: <line>`, so a speaker tag can later become the line prefix.
- **No word is lost.** Every line the lists settle alone is noted in the dataset
  (`regex`, verdict, text, over her, conversation mode), because those lines were never kept
  before and a local model needs them.

## Alternatives rejected

- **B, Jev for every line** — on the real set it misses 30 actions against A's 23 (426
  correct vs 429), and it puts a network call on every barge-in.
- **C, Jev only for what the lists miss** — it keeps loose dismissals as they were:
  on the hand-written set it swallows 7 real requests against D's 2 (59 / 7 vs 64 / 2),
  6 of them a sentence that merely contains 退下.
- **Lower bar (0.9)** — D at 0.9 on real turns swallows 3 and takes 1 wrong action
  (439 correct), against 1 and 0 at 0.95; 0.98 loses 4 more correct real lines (431).
- **Fix the lists with more patterns** — each pattern fixes a phrase and breaks a sentence
  that contains it; A misses 34 of the 80 hand-written lines, and their phrasings differ more than their words.

## Consequences

- The two errors D keeps on the hand set are lines said to someone else, 「你先退下吧我跟他说」
  (dismiss, 0.96) and 「等一下我去拿个东西」 (wait, 0.99, to a friend); the text alone cannot
  tell them from an order. They need speaker information, which no stage produces yet.
- On the real set D still swallows one line (「五。」, absorbed by the unclear-sound list
  before Jev is asked) and swaps stop for wait three times (the list says stop).
- In conversation mode a short line pays Jev's latency (median 0.14 s, up to 0.8 s) before
  its turn starts; a long line pays nothing.
- Her latest words and the line leave the Mac for every short line while the setting is on,
  and the dataset grows by every such line plus every list-absorbed line.
