# ADR 0260 — Search finds by meaning with a local embedding model, beside keywords

**Status:** Accepted
**Date:** 2026-10-15
**Supersedes:** none

## Context

- Her searches over what he said, what she said and what he hands over (ADR 0259) match words:
  any word of the query as a substring, weighted by rarity (ADR 0146 amendment, ADR 0243). A
  question in other words than the record's, or in the other language, finds nothing. He mixes
  Chinese and English in one sentence, and the Claude export is mostly English while he asks her
  in Chinese.
- Allen agreed on 2026-10-15 (「可以」, 02:40Z) that search by meaning ships with the first
  imports, alongside keyword recall.
- His text should stay on his machines unless a provider already holds it. The brain is a
  Raspberry Pi 5 with 4 GB of memory, about 2.4 GB of it free with the brain running
  (2026-10-15).
- Measured on that Pi on 2026-10-15, int8 ONNX models, two threads:
  multilingual-e5-small used 544 MB resident, 6 ms per short query, 10 ms per short record and
  278 ms for 400 characters; bge-m3 used 1,128 MB, 37 ms per query and 1,876 ms for 400
  characters. Both put the right record first on 7 of 8 mixed-language probes.
- Volumes are small: 2,754 conversation records on 2026-10-14, about 3,000 messages in the Claude
  export, a few hundred payments a year. 100,000 vectors of 384 numbers are 77 MB at half
  precision, and a full scan of them is milliseconds.

## Decision

The brain embeds every searchable text with a local multilingual model and the search tools return
hits by meaning merged with hits by words; no text leaves his machines to be embedded.

- **Model.** multilingual-e5-small, int8, run with ONNX Runtime in the brain's process and loaded
  on first use. Its files sit under the runtime root's models directory at a pinned revision.
- **What is indexed.** Conversation records, day summaries, imported entries (ADR 0259) and core
  memory items, including the earlier wordings a rewrite keeps (ADR 0258). A long text is cut into
  overlapping pieces the model can read whole.
- **Index.** One table of vectors keyed by what they point at, the model they came from and a
  digest of the text. A text whose digest is held is not embedded again; vectors of another model
  are ignored and rebuilt. New rows are embedded in the background as they arrive; nothing waits
  on it.
- **Search.** A scan of all vectors with cosine similarity. The keyword ranking and the meaning
  ranking are merged by reciprocal rank, so an exact name still comes first and a paraphrase is
  still found. The tools' parameters do not change; the model does not choose a mode.

## Alternatives rejected

- **bge-m3** — 1.1 GB resident on a brain with 2.4 GB free, and 1.9 s to read 400 characters;
  it tied e5-small on the probes.
- **A provider's embedding API** — every record and every payment would be sent to a third party
  to be found; the local model answers a query in 6 ms.
- **A vector extension (sqlite-vec) or a vector database** — a native dependency for a scan that
  takes milliseconds at a hundred times today's volume.
- **A separate tool for search by meaning** — the model would have to guess which search fits a
  question; merging inside one tool always tries both.

## Consequences

- The brain's resident memory grows by about half a gigabyte once anything is searched.
- A better model later means re-embedding everything; the text is all kept, so it is a background
  job, not a migration.
- Meaning hits can be near misses. The tools return the records themselves with their dates, and
  the model reads them before it relies on one.
