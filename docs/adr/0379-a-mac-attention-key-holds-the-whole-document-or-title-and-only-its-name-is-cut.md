# ADR 0379 — A Mac attention key holds the whole document or title and only its name is cut

**Status:** Accepted
**Date:** 2026-10-22
**Supersedes:** none

## Context

- ADR 0314 keys a Mac span that is not a site by app and document, else window title. The code
  cut that value to 30 characters before keying, so the cut applied to the key, not only to the
  name shown.
- TimeSink stores a document as a raw `file://` URL. `file:///Users/<name>/` alone is 26
  characters, so different files under `~/Projects` or `~/Documents` shared one key and, once
  told, one thread (ADR 0315).
- Measured on TimeSink's spans from 2026-10-09 to 2026-10-22: 11 keys split into 64, covering
  0.75 h of 102.6 h at the Mac. The time is small; the definition was wrong.
- TimeSink already normalizes a document to a path in `DocumentIdentity.path(of:)`: a `file://`
  URL percent-decoded, one trailing `/` dropped.

## Decision

Key a Mac span's document or title whole, and cut only the name shown.

- A document that denotes a path (a `file://` URL, or a rooted path) is keyed by that path:
  percent-decoded, trailing `/` dropped, the home directory as `~` (the home is the first two
  segments of `/Users/<name>`, other than the shared folder `/Users/Shared`, or of `/home/<name>`,
  since the Mac's home is not the brain's). A path
  is never folded into a project name.
- Any other document, and a title, is keyed as before: whitespace folded, nothing cut.
- The name shown is cut to 30 characters: a title at its end, a path at its start, so that the
  end of the path stays visible.

## Alternatives rejected

- **Raise `TITLE_MAX_CHARS`** — any ceiling still merges two paths that share that many leading
  characters, and deep paths under one folder do.
- **Key by the file's project or folder** — the project is what the model is asked to tell
  (ADR 0315); deriving it in code would pre-empt his word and guess for paths outside a repo.

## Consequences

- Every key whose string changes is a new key, and a key nobody has told is asked of the model
  once more (`untold`, at most 150 a call, those that held him at least a minute). On the data
  above 80 keys change (5.5 h at the Mac), 23 of them held a minute or more. Keys that did not
  change keep their threads. No code path writes `origin = 'him'` (only tests do), so no word of
  his is orphaned.
- Model-told rows for the old strings stay in `attention_threads`. No window piece carries those
  keys any more, so they sum nothing, but the threads brief still lists them as examples of the
  threads they were told for, named as `held()` names a key (cut to 30 characters) by
  `attention.key_name`.
- After deploy the doing writer rewrites the final text of the last `RECENT_DAYS` (3) days once:
  names and groupings change, so `inputs_digest` changes. Each of those days also costs one extra
  ask of the threads model for its untold keys. These are live model calls, and doing text he has
  already seen may read differently.
- A long window title is now a long key; the model sees only its cut name.
- A title that starts with `/` or `~` is cut like a path, at its start, because a key alone cannot
  tell it from a path.
