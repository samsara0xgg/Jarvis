# ADR 0223 — A picture he sends is remembered as words, and can be looked at again

**Status:** Superseded-by-0255
**Date:** 2026-10-11
**Supersedes:** 0211

## Context

- ADR 0211 gives a picture to the conversation model only in the turn it arrives with. After
  that, history and the memory record hold one marker line (`[attached: image NAME.jpg]`)
  and nothing about what it showed. 0211 named a re-reading tool as "the next step".
- A live case on 2026-10-11: he sent a photo with no words. She read it, guessed the dish
  wrong and answered in English. He corrected her with the dish's name. About 20 minutes later
  he asked about that dish, and she said she could not see the photo he had sent. A question
  about a picture comes minutes or hours later as often as in the same turn.
- The English answer has a mechanical cause. An attachment-only turn gets the placeholder
  `t("attach.no_words")` as its transcript, in the process language. On an English system
  locale the placeholder is English, and `reply_language(transcript, "follow")` then
  pins the reply to English (`jarvis/decision/__init__.py:1280`, `jarvis/shared/lang.py:534`).
- On 2026-10-11 Allen asked that every decision be the long-term optimum, not a patch. He also
  wants the day line on the phone (the star chart, ADR 0199) and the know-you ledger (ADR 0201)
  to understand his day. A meal photo is exactly what both lack.
- Constraints carried over from 0211 and 0044:
  - History is words only, so its front stays byte-identical for the provider cache. A 4 MiB
    picture is about 5.6 MB of base64 on every later request.
  - Pictures are kept 30 days (`attachments.retention_days`). Records and core memory are kept
    for good.
  - The vision preset and the conversation preset are the same `gpt-6-luna`. Reading a picture
    with the vision preset takes about 5 s at $0.10 per 1M input tokens. One picture is well
    under $0.001.
- Allen approved this design on 2026-10-11 (「可以」) in the phone planning thread.
- What he says about a picture outranks what a model guessed from it. That is the same rule
  core memory follows for his own statements (ADR 0146, 0154).

## Decision

Every picture he sends gets a durable note in words, written after its turn. Later turns,
summaries, the day line and the ledger read that note. A tool lets a later turn look at the
stored picture again while it is kept. Its limits:

- **The turn itself is unchanged.** The picture still goes to the conversation model as image
  parts of that turn. The note is never on that turn's critical path.
- **The note.** When the turn ends, a background job sends the vision preset three things:
  - the picture;
  - his words and her answer from that turn;
  - the next turn of his, if it arrives within 10 minutes. This is so a correction (the dish's
    real name) lands in the note.

  It writes one short note in his conversation language: what the picture shows, any text
  in it, and anything he said about it, which is marked as his and overrides the model's guess.
  No guesses at who a person is.
- **Where the note lives.** It is appended to memory.db as its own record after that turn's
  records, linked to the attachment id. Appending keeps the history front stable. The marker
  line becomes the note, so recall, the nightly day summary, core-memory review, the ledger's
  "The day" line and the day line all read it with no special case. The day line gets a
  picture item at its time and place.
- **Looking again.** A tool `look_at_picture(picture, question)` re-reads the stored file with
  the vision preset and returns its answer as text. It works for the 30 days the file is kept;
  after that the note is all there is, and she says so.
- **He owns his pictures.** Erasing a picture erases its note. Export carries both. A note is
  owner data, under the same no-telemetry rule as records.
- **An attachment-only turn speaks his language.** It takes the reply language of his last
  turn with words, never the language of the placeholder. The placeholder is no longer a
  transcript the language check reads.
- **Carried over from 0211 unchanged:**
  - The kind is read from the first bytes.
  - Images are PNG, JPEG, WebP and GIF, up to 4 MiB each; text is UTF-8, up to 256 KiB, with
    20,000 characters given to the model. Everything else is refused, and says why.
  - At most 4 attachments and 8 MiB of images a turn, and the turn always goes to the model.
  - Images are accepted only while the default preset declares `images: true`.
  - Files are kept in `artifacts/attachments/` for `attachments.retention_days`, in a store of
    at most 512 MiB.
  - Upload is `POST /inherent/attachments`, with `attachments: [id]` on submit.
  - A share is `POST /inherent/share` (a turn when `ask`, else one `user.shared` event).
  - The picture goes only to the conversation provider, and now also to the vision preset for
    its note and for a second look. `docs/spec.html#egress-table` holds both rows.

## Alternatives rejected

- **Keep the pixels in later turns.** It breaks the cache-stable words-only history (0044), at
  about 5.6 MB of base64 per later request for a 4 MiB picture. It also ends at retention
  anyway, while the note lasts.
- **Only the look-again tool, no note.** The model must already know what a picture was in
  order to decide to look at it. With a bare marker it cannot tell one picture from another
  sent minutes later. Every reference would also cost about 5 s, and
  summaries, the ledger and the day line would still get nothing.
- **Describe it before the turn, as 0211 rejected.** That adds about 5 s before the first token
  and loses the question. Writing the note after the turn keeps the latency and gains his
  words and corrections.
- **Leave it to the nightly day summary.** That covers the day line, but not this case: the
  question came about 20 minutes later, the same evening.
- **Have the turn's own model write the note inside its answer.** That couples every image
  answer to a hidden output format. A failed parse would lose the note, and the note would
  miss a correction made in the next turn.

## Consequences

- Each picture costs one more vision request after its turn, about 5 s of background work and
  well under $0.001. A burst of pictures queues them.
- A note can be wrong, and it is read later as if true. His correction is folded in only when
  it comes within the 10-minute window; a later correction lives in the records beside the
  note, not inside it.
- The note goes to the provider like any other record. A picture he would not want described
  in words is still described. Erasing it is the only way out.
- Notes outlive pictures by design. After 30 days she knows what a picture showed, but she
  cannot look at it again.
- The day line gets a new item kind, which the phone must draw.
