# ADR 0081 — Agents attention through Long Exposure

**Status:** Accepted
**Date:** 2026-09-27
**Supersedes:** none

## Context

Allen selected the published Long Exposure artifact's B01 inline expansion
on September 27, 2026, including its attention and motion behavior, while
requiring the existing Agents view to remain available. The prototype has
simulated histories and decisions; the application owns real conversations,
pending approvals and background work. Recreating the prototype's examples
as live state would misrepresent both time and authority. Agents also appear
in the notch, so two surfaces can compete for the same attention.

## Decision

Make B01 an alternate presentation of the existing Agents host, deriving
its history from provider timestamps and observed host transitions, routing
every decision through the existing host, and yielding duplicate notch
attention while that presentation owns foreground focus.

Keep the original presentation selectable. Missing historical timestamps
remain unknown. A visual completion follows a successful host response;
opening, deferring or dismissing a card never answers an approval.

## Alternatives rejected

- **Replace the existing Agents view** — removes the explicitly requested
  fallback and forces session management into the attention view.
- **Use the prototype's simulated timeline and decisions** — would place
  events at times absent from provider records and animate approvals that
  never reached the running agent.
- **Let both surfaces announce the same sessions** — a foreground approval
  would still compete with a second notch card and notification sound.

## Consequences

The host retains a small transition history in addition to its session
metadata. Older conversations can have incomplete timing. Foreground
attention ownership must cross the trusted Electron boundary and be released
when focus or presentation changes. Both presentations require regression
coverage against the same live host contracts.
