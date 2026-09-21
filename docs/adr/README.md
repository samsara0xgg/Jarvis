# ADR Standard

One decision per file. One page per decision. Anything that has state, or that
another document already owns, belongs somewhere else.

`scripts/check_adrs.py` enforces the mechanical half of this file. A standard
with no check grows a new format era; this directory already has four.

## Template

```markdown
# ADR NNNN — <Title>

**Status:** Proposed
**Date:** YYYY-MM-DD
**Supersedes:** none

## Context

<The forces in tension: a latency ceiling, a cost, a hardware limit, a
provider behaviour, an existing contract that cannot break. Constraints only,
not background. Test: a reader who knows nothing should be able to derive the
Decision from this section alone.>

## Decision

<One imperative sentence, plus its limits. No implementation steps.>

## Alternatives rejected

- **<name>** — <why it lost, in terms someone could refute with a number or a
  reproducible observation.>

## Consequences

<What this decision makes harder. Benefits are why you chose it and are
already implied by the Decision; do not restate them.>
```

## Rules

1. **Status is one token from a closed set**, on line 3, with nothing else on
   the line: `Proposed`, `Accepted`, `Superseded-by-NNNN`, `Rejected`,
   `Frozen`. Approval dates, approver names and quoted directives go in
   Context. `Approved` is not a value.

2. **Numbers are mechanical**: highest number in this directory plus one,
   assigned when the file is written. Never reserve a number for future work.
   Prose reservations inside other ADRs are not a registry, and three of them
   have been cited by production code for months while pointing at files that
   were never written.

3. **One page.** Hard cap 200 lines, target under 150.

4. **`## Alternatives rejected` is mandatory** with at least one entry whose
   losing reason is falsifiable. This is the only section whose content exists
   nowhere else in the repository: the decision itself is in the code and the
   contract is in `docs/spec.html`, but why the other option lost is only ever
   written here. No ADR written before this standard has the section.

5. **Four kinds of content never go in an ADR:**

   | Content | Owner |
   |---|---|
   | Wire protocol, schema, field list, registry, event-type table | `docs/spec.html` |
   | Build order, file-level change map, module map | the commit, or a scratch plan that dies at merge |
   | Acceptance checklist, Definition of Done, verification steps | wherever current pass/fail state lives, never a document |
   | Implementation errata, post-hoc audit of a build pass | nowhere durable: fix the code, or write a new ADR |

6. **No "Spec deviations" section.** A decision that departs from the spec
   edits the spec in the same commit. A permanent record of disagreement with
   the spec is how a spec stops being trusted.

7. **No amendment paragraphs.** To change an accepted decision, write a new
   ADR and set the old one's Status to `Superseded-by-NNNN`. Never ask a
   reader to apply a patch note mentally while reading the body.

8. **A file that is not `Accepted` does not live in this directory.** Drafts
   live outside it until accepted, so nothing stale reads as binding.

## Frozen ADRs

`Frozen` marks a historical record: read it for background, never as contract.
The fourteen ADRs written before this standard are not retrofitted. They are
frozen in place, and the contracts buried in their bodies move to
`docs/spec.html` once. Until a file is frozen or rewritten, the check reports
it as legacy without failing.

A frozen file keeps its number even where a number is shared, because
production code and other ADRs cite these numbers by hand.

## What the check asserts

Per file that declares a conforming Status: filename shape, unique number,
Status enum on line 3, the four required headings spelled exactly, at least
one rejected alternative, the line cap, no forbidden heading, and that any
`Superseded-by-NNNN` target exists.

Repository-wide: every `ADR-NNNN` reference resolves to a file. Three numbers
are cited but were never written; they are allowlisted in the script so the
check is green on arrival and goes red the moment a new dangling reference
appears.
