# ADR 0363 — A mail is read from its raw message, with hidden text dropped, cells apart, quotes cut by structure, long bodies in parts and PDFs as text

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0242, a read being clean text of at most 4,000 characters; in ADR 0063, the mail record's body being capped at 4,000 characters

## Context

- The Gmail server's own read gives one body: the plain part when there is one, else the HTML.
  Jarvis strips the HTML's tags (`mail_text`). The server can also give the raw message, which
  holds every part, every header and every attachment's bytes.
- Hidden text is kept:
  - preheader padding (U+034F, U+200C, no-break spaces);
  - text the mail hides with `display:none`.
  A probe of his mail kept 450 such characters. On 2026-10-18 they took most of the Cloudflare
  mails' snippets.
- Quotes are cut by a text pattern:
  - `^On … wrote:` also matches newsletter prose ("On Wednesday, Wedbush analyst Dan Ives
    wrote:"), and everything after it goes;
  - a lone `--` line cuts what follows;
  - a note above "Sent from my iPhone" drops the forwarded statement under it.
- Table cells are glued (a label runs into its amount, "Statement balance$…"). Outlook's
  MSO-only copies show twice.
- A body stops at 4,000 characters with "(truncated)" and no way to read on.
- Attachments are names only. On 2026-10-16 a statement's amount was only in its PDF.
- The spec's tool standard (`docs/spec.html#tools`):
  - results of about 4,000 characters;
  - more by paging;
  - never cut in the middle.
  A long web page is read in parts or where his question is (ADR 0360).
- The brain already reads the PDF statements he hands over with pypdf (ADR 0259). The Pi has no
  poppler.
- On 2026-10-21 Allen approved (「就按你说的去吧」):
  - drop hidden content and padding;
  - keep cells apart;
  - cut quotes by the mail's structure, not its words;
  - page long bodies;
  - read PDF attachments as text.

## Decision

Read a mail from its raw message:

- drop what the mail hides;
- keep table cells apart;
- cut quotes only where the mail marks them;
- read a long body in parts;
- read a PDF attachment as text.

Its limits:

- **Hidden.** These go before anything is read, including a row's opening line:
  - elements the mail hides (`display:none`, `hidden`, zero size, MSO-only copies);
  - padding characters.
- **Cells and links.**
  - Table cells and rows stay apart.
  - A link reads as its text and its site.
- **Quotes by structure.**
  - An older message is cut only where the mail marks it as quoted:
    - an HTML quote block (Gmail's, Outlook's, Apple's);
    - `>` lines, with the attribution line directly above them.
  - A signature is cut only at the standard `-- ` line.
  - A forwarded message is content and stays.
  - The result says what was left out.
- **In parts.**
  - A body longer than the result's limit comes in parts cut at paragraphs, or at the paragraphs
    that match `looking_for`, as a web page is read (ADR 0360).
  - The result says which part of how many it is.
- **Attachments.**
  - A PDF is read as text and paged the same way.
  - A PDF with no text layer says it is a scan.
  - Other files are named with their type and size.
- **Large messages.** A message larger than a limit the brain sets is read in the server's text
  form, and the result says its attachments were not read.
- The mail record of ADR 0063 holds the same text.

## Alternatives rejected

- **Keep the server's one-string body.**
  - It gives the plain part whenever there is one, which carries no hidden marks, no quote blocks
    and no cells.
  - It never gives an attachment's contents.
  - The raw message brings all of it in the same single call.
- **Cut quotes by more text patterns.**
  - "On … wrote:" in newsletter prose is the failure seen.
  - Any wording can appear in content, while quote markup appears only around quotes.
- **Download attachments to the brain's disk with the server's download tool.**
  - It writes a file on the brain for every read, which then has to be cleaned up.
  - The raw message already holds the bytes, which are read in memory.

## Consequences

- A raw read carries the attachments' bytes, so a message with large attachments reads more
  slowly, up to the limit.
- A mail client that quotes with neither markup nor `>` lines keeps its quote in the text.
- Hidden text that carried meaning is dropped with the padding.
