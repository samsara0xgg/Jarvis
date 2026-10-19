# ADR 0298 — The item he has open reaches her with its record from the day line

**Status:** Accepted
**Date:** 2026-10-19
**Supersedes:** none

## Context

ADR 0214 tells her which item his phone has open: kind, id, title and start,
on one line of the turn. On a day-line row the title is often only a label
("睡眠"), so on 2026-10-19 he tapped 问她 on his sleep row and she could not
say how he slept: no tool reads a day-line item by id, and the line carried
no figures.

Constraints:

- The phone sends the item's id as `GET /inherent/day` served it, and that
  route folds the day on each call; nothing stores the items.
- Folding a day on the Pi takes about 0.4 s from local stores. Reading his
  Mac live through the terminal or his Outlook through Graph can wait
  seconds, and a turn must not.
- The rows his Mac shipped (ADR 0294) give the same work and call items when
  TimeSink is not read live.
- Item fields can hold third parties' words (a calendar title, a payment's
  counterparty, a picture's note).

## Decision

When a turn's `about` names an item of the day line, the runtime folds the day
of its `start_ms` (today without one) from local stores only, finds the item
by id and adds its record to the about line: its local times, a sleep's
minutes in each stage and the times he woke, then its other fields, at most
900 characters, marked as data and not requests. An id the fold does not find
leaves the about line as it was. `workout` and `code` join the about kinds.

## Alternatives rejected

- **A tool that reads a day-line item by id** — the model would spend a round
  trip on every 问她 to fetch what the turn already names.
- **The phone sends the item's fields** — the phone's copy can be stale and
  is a claim; the brain already holds the record it drew.
- **Fold with the terminal and calendar** — the turn would wait on another
  machine; an Outlook event already says to read it with the calendar tool.

## Consequences

A turn with `about` costs one day fold, about 0.4 s on the Pi, before the
request is sent. Work and call items come from the shipped rows, so an item
the Mac has not shipped yet is not found and she gets only the label.
