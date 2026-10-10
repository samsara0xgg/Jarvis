"""ADR 0206: a Mac running alone (`runtime.role: all`) accepts paired devices on its own address.

One acceptance check against a real `python -m jarvis serve` in role all, listening on the
machine's own private address besides loopback. A code is minted over loopback with the local
key and claimed from the private address, where the connection's peer is not loopback; then,
holding only the device token, that peer posts a phone event and reads the day, and cannot open
`/terminal/ws`, which is a brain's alone. The peer without a token is refused everywhere else.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

import httpx
import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from jarvis.state.plugin_settings import local_key
from jarvis.surface.phone_events import PHONE_EVENTS_PATH
from tests.integration.test_phone_events import LOCATION, _frame

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _own_private_address() -> str | None:
    """The machine's own private non-loopback IPv4 address, else ``None``.

    Connecting a UDP socket sends nothing; it only makes the kernel pick the outgoing address.
    A client that connects to that address from this machine is seen by the server as coming
    from it, a peer that is not on loopback.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
        except OSError:
            return None
        address: str = probe.getsockname()[0]
    parts = [int(part) for part in address.split(".")]
    private = (
        parts[0] == 10 or parts[:2] == [192, 168] or (parts[0] == 172 and 16 <= parts[1] < 32)
        or (parts[0] == 100 and 64 <= parts[1] < 128) or parts[:3] == [192, 0, 2]
    )
    return address if private else None


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


@contextmanager
def _daemon(tmp_path: Path, address: str) -> Iterator[tuple[Path, int]]:
    """A real role-all daemon listening on loopback and on ``address``: its root and port."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text(
        "runtime:\n  role: all\n"
        f"  listen_addresses: ['{address}']\n  listen_hosts: ['{address}']\n",
        encoding="utf-8",
    )
    # Voice models on disk (empty stand-ins), so the daemon does not fetch real ones; building
    # the voice pipeline from them fails and the daemon keeps serving text.
    sensevoice = root / "models" / "sensevoice-small-int8"
    sensevoice.mkdir(parents=True)
    (sensevoice / "model.int8.onnx").write_bytes(b"x")
    (sensevoice / "tokens.txt").write_text("", encoding="utf-8")
    (root / "models" / "silero_vad.onnx").write_bytes(b"x")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.endswith(("_API_KEY", "_ADMIN_KEY")) and name != "JARVIS_RUNTIME_ROOT"
    }
    env.update(HOME=str(tmp_path), JARVIS_LOG_LEVEL="INFO")
    log = tmp_path / "daemon.log"
    with log.open("w") as sink:
        daemon = subprocess.Popen(  # noqa: S603 — sys.executable and a fixed argv.
            [sys.executable, "-m", "jarvis", "serve", "--runtime-root", str(root),
             "--port", str(port)],
            stdout=sink, stderr=subprocess.STDOUT, env=env,
        )
    try:
        deadline = time.monotonic() + 40
        while True:
            try:
                if httpx.get(f"http://{address}:{port}/api/health", timeout=2).json() == {
                    "status": "ok",
                }:
                    break
            except httpx.HTTPError:
                pass
            assert daemon.poll() is None, log.read_text(encoding="utf-8")[-2000:]
            assert time.monotonic() < deadline, "daemon never answered on its private address"
            time.sleep(0.2)
        yield root, port
    finally:
        daemon.terminate()
        daemon.wait(timeout=20)


def test_a_mac_running_alone_pairs_a_phone_hears_its_events_and_serves_its_day(
    tmp_path: Path,
) -> None:
    """Mint, claim, event and day work for a device on the private address; no terminal link."""
    address = _own_private_address()
    if address is None:
        pytest.skip("this machine has no private non-loopback IPv4 address")
    with _daemon(tmp_path, address) as (root, port):
        local, remote = f"http://127.0.0.1:{port}", f"http://{address}:{port}"
        owner = _bearer(local_key(root))

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
        auth = _bearer(claimed.json()["token"])

        listed = httpx.get(f"{remote}/inherent/devices", headers=auth, timeout=5)
        assert [row["name"] for row in listed.json()["devices"]] == ["iphone"]

        frame = {**_frame(1, LOCATION), "ts_epoch_ms": int(time.time() * 1000)}
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
