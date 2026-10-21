"""ADR 0207: a Mac running alone (`runtime.role: all`) accepts paired devices on its own address.

One acceptance check against a real `python -m jarvis serve` in role all, listening on the
machine's own private address besides loopback. A code is minted over loopback with the local
key and claimed from the private address, where the connection's peer is not loopback; then,
holding only the device token, that peer posts a phone event and reads the day, and cannot open
`/terminal/ws`, which is a brain's alone. The peer without a token is refused everywhere else.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from jarvis.state.plugin_settings import local_key
from jarvis.surface.phone_events import PHONE_EVENTS_PATH
from tests.support.phone import LOCATION, alone_daemon, bearer, event_frame, own_private_address

if TYPE_CHECKING:
    from pathlib import Path


def test_a_mac_running_alone_pairs_a_phone_hears_its_events_and_serves_its_day(
    tmp_path: Path,
) -> None:
    """Mint, claim, event and day work for a device on the private address; no terminal link."""
    address = own_private_address()
    if address is None:
        pytest.skip("this machine has no private non-loopback IPv4 address")
    with alone_daemon(tmp_path, address) as (root, port):
        local, remote = f"http://127.0.0.1:{port}", f"http://{address}:{port}"
        owner = bearer(local_key(root))

        # Without a token the private address is closed, the local key included, but for the claim.
        assert httpx.get(f"{remote}/inherent/day", timeout=5).status_code == 401
        assert httpx.get(f"{remote}/inherent/day", headers=owner, timeout=5).status_code == 401

        minted = httpx.post(
            f"{local}/inherent/devices/pairing", json={"name": "iphone"}, headers=owner, timeout=5,
        )
        assert minted.status_code == 200, minted.text
        assert minted.json()["brain"] == [remote]
        claimed = httpx.post(
            f"{remote}/inherent/devices/claim", json={"code": minted.json()["code"]}, timeout=5,
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["device"] == "iphone"
        auth = bearer(claimed.json()["token"])

        listed = httpx.get(f"{remote}/inherent/devices", headers=auth, timeout=5)
        assert [row["name"] for row in listed.json()["devices"]] == ["iphone"]

        frame = {**event_frame(1, LOCATION), "ts_epoch_ms": int(time.time() * 1000)}
        posted = httpx.post(
            f"{remote}{PHONE_EVENTS_PATH}", json={"events": [frame]}, headers=auth, timeout=5,
        )
        assert posted.status_code == 200, posted.text
        assert [(ack["ok"], ack["event_uid"]) for ack in posted.json()["acks"]] == [
            (True, frame["event_uid"]),
        ]
        # The route is the phone's own: the local key is not a device token.
        refused = httpx.post(
            f"{local}{PHONE_EVENTS_PATH}", json={"events": []}, headers=owner, timeout=5,
        )
        assert refused.status_code == 403

        day = httpx.get(f"{remote}/inherent/day", headers=auth, timeout=10)
        assert day.status_code == 200, day.text
        assert {"date", "tz", "items", "missing"} <= set(day.json())

        # No terminal hub: the socket is refused, to a device token as to any other caller.
        for header in (auth, owner):
            with pytest.raises(InvalidStatus):
                connect(
                    f"ws://{address}:{port}/terminal/ws", additional_headers=header,
                    open_timeout=5,
                )

        unpaired = httpx.delete(f"{remote}/inherent/devices/iphone", headers=auth, timeout=5)
        assert unpaired.status_code == 200
        assert httpx.get(f"{remote}/inherent/day", headers=auth, timeout=5).status_code == 401
