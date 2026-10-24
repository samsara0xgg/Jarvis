"""Jarvis — state-centric personal runtime."""

import zoneinfo

# ADR 0386: every zone is read from the pinned tzdata package, never from the host's database,
# so the Pi, the Mac and CI read one version, and it moves with uv.lock.
zoneinfo.reset_tzpath(to=())
zoneinfo.ZoneInfo.clear_cache()

__version__ = "0.1.0"
