# ADR 0259 — Exports he hands over are kept raw on the brain and read into one UTC timeline

**Status:** Accepted
**Date:** 2026-10-15
**Supersedes:** none

## Context

- Allen wants her to understand him from every source he can give her (2026-10-12),
  in this order: raw data kept forever, one timeline, then the understanding layer of core memory
  (ADR 0258). On 2026-10-14 he exported his Claude.ai account, a WeChat Pay statement, an Alipay
  statement and a Bank of China debit statement. More sources follow (credit card, Health,
  ChatGPT later); each arrives as a file he downloads, not as a live feed.
- The sources disagree on time. WeChat Pay, Alipay and the bank print local China time with no
  zone; the Claude export is UTC with an offset; the brain's own events are epoch milliseconds and
  its days are his local days in the work-state timezone (ADR 0199).
- Statements overlap: the next WeChat export of a year will repeat most rows of this one. An
  importer that adds every row twice makes every sum wrong.
- Parsing is code's job: an amount, a date and a counterparty are facts, not judgement.
  Reading the rows to say what they mean is the model's, through tools; it sees only the
  rows it asks for and the facts code computes from them.
- The repository is public. No row of his, not even as a test fixture, may enter it.
- The brain is the Pi (ADR 0170); the raw lifelog archive already sits under its runtime root,
  outside retention, clearing and export (ADR 0238).

## Decision

Every export Allen hands over is kept on the brain byte for byte and for good, and code reads its
rows into one table of dated entries that the day timeline and the model's tools read.

- **Raw.** The file is stored under the runtime root by source and digest, and is outside the
  hourly sweep, "clear recordings" and the export, like the lifelog archive. Only erasing all
  data removes it. A file the archive already holds is not stored twice.
- **Rows.** A parser per source turns the file into entries. Each entry carries its source, the
  digest of the file it came from and its place in that file, so it always points back to raw.
  An entry's identity is the source's own key where it has one (an order number, a message id),
  otherwise a digest of its fields. An entry already held is not added again; a held entry whose
  fields differ is kept as it was and the difference is logged, never overwritten.
- **One clock.** Every entry stores its instant in UTC and the zone its source wrote it in. The
  three Chinese statements are read as Asia/Shanghai; the Claude export keeps its own offset.
  Days are then cut in his local zone like every other item on the timeline.
- **Timeline.** The day timeline (ADR 0199) gains one fold over these entries, in his lane:
  a payment is a point with its amount and counterparty, a conversation with Claude is a span
  from its first to its last message of that day.
- **What a model sees.** Entries reach a model only through a read tool that filters them by
  source, time and words and returns the matching rows with their totals, or as facts code has
  computed. Conversations with Claude are the exception: they already live at Anthropic, so a
  Claude model may read them whole to distil core memory.
- **Sources with a boundary.** A statement that covers someone else's spending keeps only his
  own card's rows; the rest is not read into entries. Login history in an export is not read.
- **No private data in the repository.** Parsers are tested on synthetic files only.

## Alternatives rejected

- **Keep only the parsed rows** — a parser bug or a field it ignores would be lost for
  good; the raw file costs a few megabytes.
- **Store local times as written** — the brain's day cut, the ledger and the timeline all run on
  UTC instants; a naive China time read as Pacific is off by 15 or 16 hours and lands on the
  wrong day.
- **One table per source** — the timeline fold, the read tool and the meaning index (ADR 0260)
  would each need a branch per source; the fields that differ ride in one structured column.
- **Let the model read whole statements** — a year of payments is thousands of rows of other
  people's names and amounts sent to a provider for a question a sum answers.

## Consequences

- Each new source needs a parser and a synthetic test before its rows count; until then its file
  is only kept raw.
- Re-importing an export after a parser fix adds only rows not held; correcting rows already held
  needs a new decision, because held rows are never rewritten.
- Raw files sit on the SD card until the SSD is fitted, and nothing backs them up yet.
