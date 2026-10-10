# ADR 0216 — The phone asks the host to judge words said over her

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- When Allen talks over her, a phone that waits for the host to stop her hears her go on for a
  full round trip over the tailnet. The phone therefore holds her audio itself the moment it hears
  him, and only then asks what the words were.
- Holding is not stopping. A listening sound ("嗯"), a syllable that says nothing, or her own voice
  in the microphone must let her go on from where she was held; a stop request, 「等我一下」, a
  dismissal or a quiet phrase must not.
- The Mac already decides this (ADR 0053, 0102, 0130, 0153): regexes first, then Jev for a short
  line the regexes call a turn. Its Jev key, the regexes' vocabulary and her recent words (to tell
  her echo from his words) are on the host. The phone has no Jev key and holds none of them.
- Jev waits at most `words_timeout_ms` (800 as shipped) for a line, so a verdict is bounded.
- ADR 0209's socket must stay backward compatible: a phone that sends `barge` and unflagged
  `say` frames keeps working.

## Decision

Let the phone send the words it heard over her, or in its conversation mode, as a spoken `say`
that says so, and have the host judge them with the same function the Mac uses and answer with
the verdict, acting on it for the phone: a turn is recorded only for `turn`; `stop` and `wait`
stop her as `barge` does; `dismissed` and a quiet phrase end her answers as `cancel` does; a
listening sound, an unclear sound and her own echo do nothing, so the phone resumes its audio.

Its limits:

- **One judge.** The Mac's session and the phone's socket both call `jarvis.surface.word_judge`;
  neither keeps a copy of the regexes or of the Jev mapping.
- **The host's conversation mode is never touched.** A dismissal from a phone ends the phone's
  own conversation mode, which the phone does when it reads the verdict.
- **Quiet is heard only where it can be set.** A host with no `set_quiet` does not recognize
  the quiet phrases, as on the Mac.
- **The flags mean nothing for typed words**, and an unflagged `say` is a turn.
- **No cache.** A resent flagged `say` is judged again; a phone that stopped waiting resends it
  unflagged, which records the turn once.

## Alternatives rejected

- **Judge on the phone.** It would copy the regexes and the Jev prompt into a second language
  and ship two vocabularies to keep equal, and the phone has no Jev key and cannot read her recent
  words for the echo check. A line the Mac calls a backchannel only because Jev said so would be a
  turn on the phone.
- **The phone sends `cancel` for a stop.** It costs a second round trip (the `say`, its verdict,
  then `cancel`) after the one already spent judging, and `cancel` cancels every recent turn
  where a stop over her should reach only the open answer's generation.
- **Send `barge` first and judge afterwards.** Her audio would stop on every listening sound
  and the phone could not tell a held voice from a stopped one; the Mac's soft barge-in exists
  to avoid exactly that.

## Consequences

- The phone speaks no line back to a dismissal, 「等我一下」 or a quiet phrase. The Mac says a
  fixed one (`_say_conversation_line`); the phone gets none in this change.
- Her audio stays held for as long as the verdict takes, up to Jev's `words_timeout_ms` plus a
  round trip. A phone that gives up waiting falls back to an unflagged `say`, which is a turn, so
  a listening sound said over her in that case becomes one.
- The host answers a flagged `say` after it judges, not after it records; a phone that does not
  read `verdict` sees `turn_id: null` and no turn, and must send the words again unflagged.
- A quiet phrase said to a phone changes the one quiet level the Mac, the phone and every
  other device share.
