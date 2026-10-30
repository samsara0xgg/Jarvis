# ADR Standard

One decision per file. One page per decision. Anything that has state, or that
another document already owns, belongs somewhere else.

Start at [INDEX.md](INDEX.md): every ADR in force, by area, with the later ADRs
that replace part of each. It is generated from the headers, so it is never
behind them. Before changing code, `scripts/adrs_for.py --diff` lists the ADRs
the changed files cite (rule 12).

`scripts/check_adrs.py` enforces the mechanical half of this file. A standard
with no check grows a new format era; this directory already has four.

## Template

```markdown
# ADR NNNN — <Title>

**Status:** Proposed
**Date:** YYYY-MM-DD
**Supersedes:** none
**Area:** <one or more areas from INDEX.md, comma-separated>

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
   | Wire protocol, schema, field list, registry, event-type table | the code that defines it; `docs/spec.html` only on ADR 0089's terms |
   | Build order, file-level change map, module map | the commit, or a scratch plan that dies at merge |
   | Acceptance checklist, Definition of Done, verification steps | wherever current pass/fail state lives, never a document |
   | Implementation errata, post-hoc audit of a build pass | nowhere durable: fix the code, or write a new ADR |

6. **No "Spec deviations" section.** A decision that departs from the spec
   edits the spec in the same commit. A permanent record of disagreement with
   the spec is how a spec stops being trusted.

7. **No amendment paragraphs.** To change an accepted decision, write a new
   ADR and mark the old one as rule 10 says. Never ask a reader to apply a
   patch note mentally while reading the body. Four ADRs written before this
   rule still carry such paragraphs (`AMENDED_IN_BODY` in the check); that set
   only shrinks. `scripts/check_adrs.py --since` fails an edit to an accepted
   ADR's body; its header may change.

8. **A file that is not `Accepted` does not live in this directory.** Drafts
   live outside it until accepted, so nothing stale reads as binding. Accepted
   means built, or being built on the same branch: a decision approved for
   later work waits outside too. The check lists every Accepted ADR that no
   code cites, so one that was never built shows up instead of reading as
   binding (ADR 0350 to 0354 sat that way for days).

9. **Names, numbers and thresholds stay out of the title and the decision
   sentence.** A model, a threshold, a timeout, a codec or a width is a value
   that moves; config and `docs/spec.html` hold the current one. The ADR says
   why that kind of value, and gives the value of its day only as an example.
   Most of the 54 ADRs the 10-25 audit found partly outdated went stale
   through such details. For ADRs numbered 0419 and up, the check fails a
   title with a number in it.

10. **A relation is written on both sides, in the same commit.** An ADR that
    replaces another names it on its Supersedes line, never only in its body:
    the number alone for the whole (`0302`), or the clauses for a part (`in
    ADR 0302, the clauses that … Everything else in ADR 0302 stands.`). The
    old ADR then says so in its header: Status `Superseded-by-NNNN` when the
    whole is replaced, or the new number on its `**Amended-by:**` line when a
    part is. That line is a pointer, not an amendment; the old body is never
    edited. The check fails a relation only one side states, and for ADRs
    numbered 0419 and up, a Decision that says it replaces an ADR its
    Supersedes line does not name. (Before 10-25, over a hundred relations
    were stated on one side only or in a body; those the audit added are
    marked "named by the 10-25 audit".)

11. **Write one only for a decision others must respect.** A boundary between
    layers or devices, what is kept and for how long, what leaves which device
    and to whom, a contract between the brain and a terminal, what the product
    is: those are costly to reverse, and code elsewhere relies on them. A
    feature's behaviour, its tunables and its screens belong in
    `docs/spec.html`. A decision still moving within the day is written once
    it settles: ADR 0406 was stale within hours of its commit.

12. **Before a change lands, read what governs the code it touches.**
    `scripts/adrs_for.py --diff` (or with paths) lists each ADR the changed
    files cite, with its status and the ADRs that amend it. Confirm each still
    holds, or write the ADR that replaces the part that does not. Code cites
    ADRs by number on purpose: that citation is how a change finds them.

## Frozen ADRs

`Frozen` marks a historical record: read it for background, never as contract.
The thirteen ADRs written before this standard are not retrofitted. They are
frozen in place, and the contracts buried in their bodies move out once: to
`docs/spec.html` on ADR 0089's terms, otherwise to the code that defines
them. Until a file is frozen or rewritten, the check reports it as legacy
without failing. ADR 0001, 0002 and 0014 were frozen on 10-25, when the audit
found nothing of them binding that later ADRs and the code do not carry.

A frozen file keeps its number even where a number is shared, because
production code and other ADRs cite these numbers by hand.

## What the check asserts

Per file that declares a conforming Status: filename shape, unique number,
Status enum on line 3, only the template's header lines, a Supersedes line,
an Area line from the closed set (`AREAS` in the script; a new area is added
there), the four required headings spelled exactly, at least one rejected
alternative, the line cap, no forbidden heading, and no amendment paragraph.
For ADRs numbered 0419 and up, also rules 9 and 10 against the title and the
Decision.

Between files: every relation is stated on both sides (rule 10), and every
`Superseded-by-NNNN` and `Amended-by` target exists.

Repository-wide: every `ADR-NNNN` reference resolves to a file; no code cites
a Proposed ADR; `README.md` and `docs/spec.html` cite no ADR that was wholly
replaced; and `INDEX.md` matches the headers. Three
numbers are cited but were never written; they are allowlisted in the script
so the check is green on arrival and goes red the moment a new dangling
reference appears.

It also prints, without failing, the Accepted ADRs that no code cites.
