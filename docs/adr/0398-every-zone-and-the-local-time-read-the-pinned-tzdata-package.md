# ADR 0398 — Every zone, and the local time on a stale host, read the pinned tzdata package

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** ADR 0386

## Context

IANA tzdata 2026e moved BC, Alberta and Manitoba to permanent summer time. A tz database older
than that gives America/Vancouver UTC-8 and America/Winnipeg UTC-6 after 2026-11-01, so every
time of day read in those zones is an hour off.

Jarvis reads time of day through two providers. `zoneinfo` reads the first directory on `TZPATH`
and falls back to the `tzdata` PyPI package only when none holds the zone. The C library's local
time (`datetime.astimezone()` with no zone, `time.localtime`, about forty readers, and every
process Jarvis starts) reads the host's database through `TZ` or `/etc/localtime`.

ADR 0386 pinned the first: importing `jarvis` empties `TZPATH`, so every `ZoneInfo` is the
`tzdata` package's (2026.5 = 2026e, in `uv.lock`). It left the C library on the host. On
2026-10-24 the Pi's system tzdata was 2026c-0+deb13u1, and apt's candidate was the same, so the
host cannot be brought to 2026e by an upgrade. From 2026-11-01 the package says UTC-7 and the
Pi's C library UTC-8. ADR 0322's boot check sees the two disagree on his zone and refuses to
start; reproduced here with a 2026c-shaped Vancouver file under `TZDIR`.

Allen's choice on the decision card ("Jarvis 自带固定版时区数据") was that the Pi, the Mac and CI
all read one copy.

## Decision

Importing `jarvis` empties `zoneinfo`'s search path, so every `ZoneInfo` is read from the
`tzdata` package pinned in `uv.lock`; and when the C library's offset for the machine's zone
differs from the package's at now, half a year on or 2026-11-02 12:00, `TZ` is set to the
package's file for that zone, so local time reads the package too.

A host whose database agrees is left as it is. A machine on UTC, or on a zone the package does
not hold, is left as it is.

## Alternatives rejected

- **Upgrade the Pi's system tzdata** — apt's candidate on Debian 13 was 2026c on 2026-10-24, nine
  days before the rule takes effect; installing an unstable-suite package on the brain's host
  ties Jarvis' correctness to a manual step outside the repository on every new host.
- **Warn instead of refusing at boot** — the boot would go on with the forty C-library readers an
  hour off from the `ZoneInfo` readers in the same process; the check exists to stop exactly
  that.
- **Always point `TZ` at the package** — on a host that already agrees it changes nothing for
  Jarvis but rewrites `TZ` for every process it starts (macOS's own frameworks among them), for
  no gain.
- **Empty `TZPATH` in CI only (`PYTHONTZPATH=`)** — CI turns green and the Pi and the Mac keep
  their own databases; that is the failure CI caught, in his real mail.

## Consequences

A new IANA release reaches Jarvis only when `tzdata` is raised in `pyproject.toml` and `uv.lock`.
`scripts/init.sh` says so when the rule check fails, and says when local time was pointed at the
package.

On a stale host `TZ` holds a path (`:/…/tzdata/zoneinfo/America/Vancouver`), so `system_zone()`
reads a path as the zone its name ends in, and the processes Jarvis starts inherit it. Programs
on the host that Jarvis does not start (cron, the shell, the journal's local display) still read
the host's database.

Code that does not import `jarvis` reads the host's database for both providers.
