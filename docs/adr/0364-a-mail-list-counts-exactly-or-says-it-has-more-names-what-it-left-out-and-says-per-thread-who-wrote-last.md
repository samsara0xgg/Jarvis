# ADR 0364 — A mail list counts exactly or says it has more, names what it left out, and says per thread who wrote last

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0123, Jev's guess as the reply mark the Mac home shows; in ADR 0242, the overview of the 20 newest unread Primary letters with no-reply senders left out and its "N need a reply" line

## Context

- The overview reads at most 20 unread Primary letters (`Home.mail`) and ignores Gmail's next
  page.
- It drops no-reply and notification senders before counting, so "13 unread in Primary"
  understates. Banks and security alerts often send from no-reply addresses. `junk_left_out`
  counts only what Jev marked.
- "Needs a reply" is Jev's guess from the display name and the subject, at p ≥ 0.9 (ADR 0123):
  - a letter Jev has not answered within 1.5 s goes unmarked;
  - only unread Primary is covered, so a letter he read and never answered never counts;
  - the summary states "N need a reply" as fact.
- What he asks:
  - 「今天有什么新邮件」 needs everything since his local midnight, in every category, counted
    exactly;
  - 「哪些还没回」 needs the threads whose last message is someone else's, read or not.
- What the mail itself says:
  - the headers say whether a message is bulk or automated (`List-Unsubscribe`, `Precedence`,
    `Auto-Submitted`);
  - his sent mail says when he last wrote in a thread;
  - Gmail's search gives each message's thread, and pages on with a token.
- `resultSizeEstimate` is Gmail's estimate; the code already declines to state it.
- The spec's tool standard (`docs/spec.html#tools`):
  - a list says how many rows it did not give, and how to get them;
  - no field is left unknown for the model to guess.
- On 2026-10-21 Allen approved (「就按你说的去吧」):
  - a number is exact or says there are more, and names what was left out;
  - "needs a reply" is decided by thread, from whether the last message is his.

## Decision

Count a mail list exactly or say it has more, name what it left out, and mark per thread who
wrote last, with one rule for the tool and the Mac home.

- **Exact.** A list pages through Gmail's search until it holds every match or reaches a cap,
  and states the count, or "more than" the cap.
- **Nothing dropped silently.**
  - Rows are cut whole to the result's limit.
  - What is not shown is counted by Gmail category.
  - A no-reply sender is a row like any other.
- **Who wrote last.**
  - Each thread in a list says whether its last message is his. When it is not, the row says
    who wrote it, when, and whether its headers mark it bulk or automated.
  - A thread is waiting on him when its last message is someone else's, is neither bulk nor
    automated, and is addressed to him.
  - `waiting` lists only those threads.
  - Waiting is stated as a fact ("Sam wrote last, 2 days ago"). Whether it needs an answer is
    hers to judge from the mail.
- **Scope.** With no arguments, a list is his unread inbox in every category. `when` (ADR 0362)
  gives every mail received in that window, unread or not.
- **One rule.**
  - The Mac home keeps listing unread Primary mail. Its count is exact, and its reply mark is the
    waiting rule above.
  - Jev's reply guess is no longer shown. Jev's junk and importance marks still order the home
    (ADR 0124, 0141).

## Alternatives rejected

- **Keep Jev's guess and word it "likely".**
  - It sees two header fields.
  - It misses a letter he read and never answered, and 「哪些还没回」 needs exactly those.
  - The last sender of each thread answers it, from data already in the mail.
- **Primary only.** Banks, receipts and security notices land in Updates, so 「今天有什么新邮件」
  would miss them.
- **State Gmail's `resultSizeEstimate`.** Gmail calls it an estimate, and it differs from the
  paged count.

## Consequences

- Paging to an exact count costs more search calls in a full inbox, and the list waits for them.
- A person writing through a bulk sender, such as a recruiter on a mailing tool, is not marked
  waiting. The row still says who wrote last.
- The home's reply marks change for letters Jev had marked.
