"""Passive read-only observer for the ``/inherent/ws`` push channel.

Attaching only adds one more client to ``InherentBroadcaster._clients``. The
route is outbound-only, so this never sends a frame, never owns an audio
device, and never restarts or otherwise disturbs a running daemon.

Canary for the listening-at-wake-arm change: while you are still speaking,
``{"op": "voice", "payload": {"phase": "listening", "turn_id": "T..."}}``
prints before the ``"phase": "transcribing"`` line bearing the same
``turn_id``. A wake followed by silence prints ``listening`` then
``"phase": "empty"``.

Usage::

    ./.venv/bin/python -m tools.inherent_ws_observer [ws://127.0.0.1:8009/inherent/ws]
"""

# Every line this tool emits is its operator-facing output; T201 is disabled
# file-wide rather than annotated on every print site.
# ruff: noqa: T201

from __future__ import annotations

import asyncio
import sys
import time

from websockets.asyncio.client import connect

DEFAULT_URL = "ws://127.0.0.1:8009/inherent/ws"


async def _observe(url: str) -> None:
    async with connect(url) as ws:
        print(f"observing {url} (read-only) — Ctrl-C to stop", flush=True)
        async for message in ws:
            stamp = time.strftime("%H:%M:%S")
            print(f"{stamp} {message!s}", flush=True)


def main() -> None:
    """Print every envelope the daemon pushes until interrupted."""
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    try:
        asyncio.run(_observe(url))
    except KeyboardInterrupt:
        print("observer stopped", flush=True)


if __name__ == "__main__":
    main()
