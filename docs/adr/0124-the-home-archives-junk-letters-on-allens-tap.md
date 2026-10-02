# ADR 0124 — The home archives junk letters, only on Allen's tap

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- Allen, 2026-10-02, approved a junk suggestion on the companion home's mail
  block, in his words: suggest first, archive only when he taps, never delete;
  archive means out of the inbox only, still in All Mail.
- ADR 0123 already asks Jev one question per unread letter over the sender's
  display name and the subject. Jev's request takes several questions at once,
  so a second one costs no extra call.
- Until now the home only read Gmail (ADR 0055); its only write was a To Do
  checkbox in Microsoft. Archiving is the first time the home changes mail.
- The `gmail` MCP server runs with `gmail.modify` already granted (ADR 0055)
  and exposes `gmail_batchModify`, which adds and removes labels on many
  messages in one call; removing the `INBOX` label is Gmail's archive.
- A wrong junk flag hides nothing by itself: the letter stays in the list until
  Allen taps, and a tap can be undone for five seconds.

## Decision

Ask Jev, in the same request as the reply question, whether a letter is junk
Allen would not want in his inbox, flag it junk at probability `junk_at` (0.9)
or above, and let Allen archive the flagged letters with one tap on the home,
undoable; the daemon never archives on its own and never deletes.

- **Question.** A second `noul` key, `junk`, over the same name and subject, no
  new data leaves the Mac. Unsure is not junk. A letter whose reply answer
  reaches `yes_at` is never flagged, whatever the junk answer says.
- **Cache.** Both probabilities are cached per Gmail message id like the reply
  probability; `junk_at` applies when read.
- **Write.** `POST /inherent/mail/archive {ids}` removes `INBOX` from exactly
  those ids with `gmail_batchModify`; `POST /inherent/mail/unarchive {ids}`
  adds it back. Both are behind the local key like every `/inherent` route.
  The archive route accepts only ids the last `GET /inherent/mail` answer
  flagged, the unarchive route only ids archived since boot; any other id
  rejects the whole request with 400. A letter put back is not flagged again.
  Each change is logged with ids only.
- **Mail list.** The unread search is now `in:inbox category:primary is:unread`,
  so an archived letter does not come back on the next poll.
- **Companion.** The mail block shows one line, "N look like junk", with an
  Archive button; the letters leave the list at once and a toast with Undo
  follows. The block pops up for the newest of the newest reply-needed letter
  and the newest junk letter; FYI-only mail still does not pop it.

## Alternatives rejected

- **Archive every flagged letter by itself.** Allen's rule is suggest first.
  Jev's junk answer is one number over a name and a subject: in the 14
  invented pairs of the 2026-10-02 live run, a cold-sales pitch scored 0.93 for
  junk and 0.85 for needing a reply, so a bar on junk alone can be wrong.
- **Trash or mark as spam instead of archive.** Both leave All Mail or train
  Gmail's filter on one probability; removing `INBOX` is undone by adding it
  back and loses nothing.
- **`gmail_modify` per letter.** It makes N calls where `gmail_batchModify`
  makes one; the home archives a handful of ids at a time, so the tool that
  takes a list is the same size in code and fewer round trips.
- **Let the route archive any id the page sends.** The route is reachable by
  anything holding the local key; limiting it to ids the daemon itself
  flagged keeps a leaked key from archiving mail Jev never called junk.

## Consequences

- Archiving needs the `gmail.modify` scope the server already holds, so the
  first run after this ADR asks nothing new, but a revoked scope turns the tap
  into a 502 and a toast.
- A letter Allen archived himself in Gmail while still unread no longer shows
  on the home, because the search now requires `INBOX`.
- Flag state is in memory: after a restart a letter put back is flagged again
  at the next poll.
- The Event Log has no record of an archive; the daemon log has the ids.
