# ADR 0164 — The decision snapshot folds the log from a high-water mark

**Status:** Accepted
**Date:** 2026-10-05
**Supersedes:** none

## Context

- Every voice turn reads the decision snapshot (`read_decision_snapshot`) at
  least twice before its first model request: the runtime builds a packet to
  route the turn, then `decide()` builds its own; each tool-loop iteration
  reads again. Each read decodes the whole events table and folds every
  projection, plus the event-derived half of the authorization snapshot.
- Measured on the real log copy (48,952 events): one read costs about 0.54 s,
  nearly all of it the SELECT and JSON decode. On the live trace the gap
  before the model request grew from 0.16 s at 16k events (9/28) to 1.36 s at
  48.6k (10/4). The cost is linear in a log that only grows.
- `docs/spec.html#projections` says a projection is folded from events and
  read-only (I3); `docs/spec-v1.html` already allows it to be cached or folded
  incrementally. Nothing requires a fold to restart from event 1.
- A snapshot is one SQLite read transaction: `event_cursor` is `MAX(id)` in
  that view, and the operational tables are read in the same view. Turns run
  on worker threads, each with its own connection, so two readers can hold
  different views of the log at the same moment.
- Only `runtime/` may wire across layers. The reader is L2, the callers are L3
  and the runtime.

## Decision

Fold the projections incrementally from a high-water mark, and keep that state
in one shared holder in the runtime.

Its limits:

- **The log stays the only fact.** The fold state is a cache of the log's
  prefix. It is an immutable value (a cursor, the uid of the event at the
  cursor, and the folds); advancing it returns a new value. Nothing is written
  and no table is created. A reader without a state folds the whole log, as at
  startup. I3 is unchanged: every state is reproducible from the events.
- **Equal to the whole-log fold.** Every projection and the event-derived half
  of the authorization snapshot absorb an increment with the same result as
  one pass over the whole log, wherever the log is split. The whole-log fold
  stays as the reference the increments are tested against.
- **A reader proves its prior is a prefix.** It uses a prior only when its own
  view reaches the prior's cursor and the row at that cursor is the same
  event. A view older than the prior (a second connection that began earlier),
  or another log, folds from the start instead; a reader never gets a state
  from the future.
- **What cannot be folded in order.** The conversation window decides which
  turns it keeps from the whole sequence of inputs. A log shaped so that
  in-order folding would differ (an input arriving after outputs of its own
  turn, one response id across two turns) makes that read fold the whole log;
  the daemon never writes such a log.
- **One holder, in the runtime.** `DecisionStateCache` hands every reader the
  state and publishes a newer one by swapping one reference under a lock. It
  publishes only states read in a transaction it owned, never one that may hold
  a caller's uncommitted rows. It is passed to the turn as a reader function
  (`DecideContext.read_snapshot`), so L3 imports nothing from the runtime.
- **One read per turn, not two.** The runtime's packet and `decide()`'s packet
  read through the same holder, so the second costs only the events appended
  between them, and so do the tool-loop re-reads and the two projection reads
  after a confirmation answer. The daemon folds the log once in the background
  when it starts.
- **A self-check.** Every 100th read, a background thread opens its own
  connection and compares the incremental snapshot with a whole-log fold of the
  same view. A difference logs a warning and resets the holder, so the next
  read folds the whole log.

## Alternatives rejected

- **Pass the runtime's packet into `decide()`.** Removes one of the two reads
  but the packet is older than the one `decide()` takes by the events the run
  opening appends, and the tool loop still re-reads in three places, each a
  whole-log fold, because each must see what the tool dispatches just appended.
- **Cache the whole snapshot while `MAX(id)` is unchanged.** A turn appends its
  own input before the first read, so the first read of every turn sees a new
  cursor; the cache would miss on exactly the reads that cost the most.
- **Fold only a recent window of events.** Consumed lease ids, open actions
  and confirmation lineage depend on events of any age; a bounded read changes
  what the projections mean, not what they cost.
- **Materialised projection tables.** A reader that writes or creates tables
  breaks the snapshot's read-only transaction and needs a migration for a
  value that is cheap to recompute from the events.

## Consequences

- A fold bug now outlives the read that hit it, until the self-check or a
  restart: every new projection must ship an incremental form and an equality
  test against the whole-log fold, and the whole-log fold has to stay.
- The daemon holds the folded state in memory, and events decoded once are
  shared by reference between reads, so no reader may mutate an event payload.
- A log that makes the conversation window fold out of order is read in full on
  every call, slow but correct.
- The self-check costs one whole-log fold of CPU every 100 reads, on a thread
  that competes for the interpreter lock with a turn that may be running.
