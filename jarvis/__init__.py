"""Jarvis — state-centric personal runtime."""

import zoneinfo

# ADR 0398: every zone is read from the pinned tzdata package, never from the host's database,
# so the Pi, the Mac and CI read one version, and it moves with uv.lock. The C library's local
# time is pointed at the same package when the host's database disagrees with it.
zoneinfo.reset_tzpath(to=())
zoneinfo.ZoneInfo.clear_cache()

from jarvis.shared.clock import pin_local_time  # noqa: E402 - the search path is reset first

pin_local_time()

__version__ = "0.1.0"
