# ADR 0090 — One Owner per macOS Account

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

- Constitution C1 reads "Allen-only personal runtime": Jarvis serves Allen
  alone and never trades personalisation for multi-user use, generic
  onboarding or legibility to strangers (spec v1 §3.2.1, §1.1 and §1.5
  principle 1; `_C1` in `jarvis/constitution/__init__.py`). The first build
  anyone else installs breaks it.
- On 2026-09-29 Allen asked for Jarvis to become software anyone can use with
  one API key each, and for its multi-user security and privacy to be thought
  through. Given two readings of "multi-user" (each person installs their own
  copy, or several people share one Jarvis), he chose "各装各的": one owner
  per macOS account.
- Much of Jarvis is already per account. The runtime root `~/.jarvis` sits in
  the home folder and is set to 0700 at every boot
  (`jarvis/deployment/__init__.py:249`); API keys sit in the account's login
  Keychain; the daemon and the companion run as the account's own processes
  (LaunchAgents in its `gui/<uid>` domain, or children of the app it opened);
  first-run setup asks the owner's name and keeps it in memory.db.
- Loopback ports are not per account. The daemon listens on 127.0.0.1:8006
  and the agent host on 127.0.0.1:8016, where any account's process can
  connect or listen first. `scripts/claude_hook.py` sends Jarvis's key to
  whatever holds 8006 and hands its answer to Claude Code as a permission
  decision.
- A confirmation card also takes a spoken yes: the first utterance within
  `confirmation.ttl_ms` (10 minutes) of the ask answers it (ADR 0062).
  Nothing in `jarvis/` tells one voice from another.
- The review counted 25 strings in the code that spell "Allen", 14 of them in
  text the model reads (release inventory 改2).

## Decision

Replace C1 with "one owner per install": a Jarvis install serves the person
whose macOS account it runs in, and its memory, keys, confirmations and
attention are that person's. It may lean as far into the owner's projects,
habits and speech as it likes, but no code names a particular person, and a
stranger has to be able to understand it on their first run. Two people on one
Mac are two accounts with two installs that share only the hardware.

This does not make Jarvis tell voices apart: whoever speaks near the owner's
Mac is heard as the owner.

## Alternatives rejected

- **Several people share one Jarvis** — the three memory.db tables (records,
  profile, summaries), every event and every pending card would need a person
  on them, and voice turns would need speaker identification, which nothing in
  `jarvis/` does; the option card put it as a large change to both the spec
  and the code.
- **Keep C1 and let others run an Allen-only build** — C1 would forbid the
  onboarding, first-run clarity and cross-account isolation a public build
  needs, and a principle that every other install breaks stops constraining
  anything.

## Consequences

- When two accounts on one Mac run Jarvis, the fixed ports collide, and one
  account's process can take the other's hook key and answer its Claude Code
  permission prompts. The local endpoints have to move inside the 0700 runtime
  root (a unix socket, as `codex.sock` already is) before a public build
  ships.
- Every "Allen" that the model or the owner can read becomes a defect: it has
  to be the owner's name or "the user". The `allen` source marker on stored
  records is an internal value and can wait for a data migration.
- A guest's spoken "好" still confirms the owner's action; limiting spoken
  confirmation is a separate decision.
- A household that wants one shared assistant is not served.
