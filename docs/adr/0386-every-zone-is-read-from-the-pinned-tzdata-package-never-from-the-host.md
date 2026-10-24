# ADR 0386 — Every zone is read from the pinned tzdata package, never from the host

**Status:** Superseded-by-0398
**Date:** 2026-10-24
**Supersedes:** none

## Context

IANA tzdata 2026e moved BC, Alberta and Manitoba to permanent summer time.
A tz database older than that gives America/Winnipeg UTC-6 and America/Vancouver UTC-8 after
2026-11-01, so every time of day read in those zones is an hour off.

`zoneinfo` reads the first directory on `TZPATH` that holds a zone, and falls back to the
`tzdata` PyPI package only when none does. The zones Jarvis read were therefore the host's: the
GitHub runner's (older than 2026e, so `test_zone_label` has been red on main since a0a2bc7), the
Pi's and the Mac's, each updated by a different system on its own schedule. The boot and
`scripts/init.sh` could detect an old host database (commit 7df1d56) but not fix it, and the same
code gave different answers on the three machines.

The `tzdata` package 2026.5 carries IANA 2026e. A dependency in `uv.lock` moves on every host in
one step, through the deploy script's `uv sync`.

On 2026-10-24 Allen chose this option on the decision card ("Jarvis 自带固定版时区数据").

## Decision

Importing `jarvis` empties `zoneinfo`'s search path, so every `ZoneInfo` on every host is read
from the `tzdata` package pinned in `uv.lock`; the boot's and `init.sh`'s zone-data check name
that package's version.

This covers `zoneinfo` only. The C library's local time (`datetime.astimezone()` with no zone,
`time.localtime`) still follows the host's database.

## Alternatives rejected

- **Empty `TZPATH` in CI only (`PYTHONTZPATH=`)** — CI turns green, but the Pi and the Mac keep
  reading their own databases. A Debian or macOS host that has not yet shipped 2026e gives
  Winnipeg UTC-6 on 2026-11-12, which is the failure CI caught, now in his real mail.
- **Upgrade the runner's system tzdata in the workflow (`apt-get install tzdata`)** — it depends
  on Ubuntu's mirror having shipped 2026e, which it had not when main went red, and it still
  leaves three hosts on three versions.
- **Point `TZ` at the package's zone files so the C library reads them too** — it changes what
  `system_zone()` and ADR 0322's witness see (a path, not a zone name), and the witness exists to
  catch a host whose clock disagrees with the zone data; hiding the host behind the package
  would remove that check.

## Consequences

A new IANA release reaches Jarvis only when `tzdata` is raised in `pyproject.toml` and `uv.lock`;
the host's own updates no longer help. `scripts/init.sh` says so when the rule check fails.

The host still matters for the C library. On a host whose database predates 2026e and whose zone
is his zone, ADR 0322's boot check sees the package and the machine disagree after 2026-11-01 and
refuses to boot, instead of reading an hour off quietly. Such a host needs its system tzdata
updated.

Code that builds a `ZoneInfo` before `jarvis` is imported (a script that does not import it) reads
the host's database.
