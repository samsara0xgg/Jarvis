"""ADR 0378: the phone's raw accelerometer, kept all day as archive pieces.

Acceptance checks against the real route and the real archive:

- an accelerometer piece is kept byte for byte at ``accel/<device>/<UTC day>/…csv.gz`` behind the
  same guard as audio and ride motion, and a resend is answered from what is kept;
- only gzip text that opens with the accelerometer header is taken, on its own route: ride motion
  is not taken there, and an accelerometer piece is taken neither as motion nor as audio.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
from typing import TYPE_CHECKING, Any

from fastapi.testclient import TestClient

from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.lifelog import LIFELOG_DIRNAME, Lifelog
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps
from jarvis.surface.inherent_server.guards import require_local_key

if TYPE_CHECKING:
    from pathlib import Path

REMOTE = "100.87.250.92"
LOOPBACK = "127.0.0.1"
START_MS = 1_791_756_000_000  # 2026-10-11T22:00:00Z, a ten-minute boundary
PIECE = "accel-1791756000000"
ACCEL = gzip.compress(
    b"# jarvis accel v1 rate_hz=50\n"
    b"t_ms,x,y,z\n0,0.0123,-0.9981,0.0456\n20,0.0119,-0.9979,0.0461\n",
)
MOTION = gzip.compress(
    b"# jarvis motion v1 frame=x_magnetic_north_z_vertical rate_hz=25\n"
    b"t_ms,ax,ay,az\n0,0.0012,-0.0300,0.0101\n",
)


class Brain:
    """One app with the archive wired, and a paired phone."""

    def __init__(self, tmp_path: Path) -> None:
        """A brain with the phone ``iphone`` paired."""
        self.store = Lifelog(tmp_path / LIFELOG_DIRNAME, free_bytes=lambda _path: 100 * 1024**3)
        app = create_app(InherentDeps(
            submit_callable=lambda _text: "T",
            broadcaster=InherentBroadcaster(),
            device_name=functools.partial(device_name_for_token, tmp_path),
            lifelog=self.store,
        ))
        self.key = local_key(tmp_path)
        require_local_key(
            app, functools.partial(local_key_matches, self.key), extra_hosts=[REMOTE],
            device_token_matches=functools.partial(device_token_matches, tmp_path),
        )
        self.token = pair_device(tmp_path, "iphone")
        self.remote = TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000))
        self.loopback = TestClient(
            app, base_url=f"http://{LOOPBACK}:8006", client=(LOOPBACK, 50000),
        )

    def put_piece(
        self, kind: str, data: bytes, *, auth: str | None = None, via: str = "remote",
    ) -> Any:  # noqa: ANN401 — the response
        """One archive piece of ``kind`` from the phone."""
        client = self.remote if via == "remote" else self.loopback
        return client.put(
            f"/inherent/lifelog/{kind}/{PIECE}",
            params={"started_ms": START_MS, "duration_ms": 600_000}, content=data,
            headers={"Authorization": f"Bearer {self.token if auth is None else auth}"},
        )


def test_an_accel_piece_is_kept_byte_for_byte_beside_the_audio(tmp_path: Path) -> None:
    """201 with the digest, filed under accel/; a resend is 200; the local key is a 403."""
    brain = Brain(tmp_path)

    sent = brain.put_piece("accel", ACCEL)
    again = brain.put_piece("accel", ACCEL)
    local = brain.put_piece("accel", ACCEL, auth=brain.key, via="loopback")

    assert (sent.status_code, again.status_code, local.status_code) == (201, 200, 403)
    assert sent.json() == {
        "id": PIECE, "sha256": hashlib.sha256(ACCEL).hexdigest(), "bytes": len(ACCEL),
    }
    (path,) = (p for p in brain.store.directory.rglob("*") if p.is_file())
    assert path.relative_to(brain.store.directory).as_posix() == (
        f"accel/iphone/2026-10-11/20261011T220000.000Z_600000ms_{PIECE}.csv.gz"
    )
    assert path.read_bytes() == ACCEL


def test_only_accel_text_in_gzip_is_taken_and_only_on_its_own_route(tmp_path: Path) -> None:
    """Plain text, ride motion and a broken stream are 415 here; accel is not motion or audio."""
    brain = Brain(tmp_path)

    refused = [
        brain.put_piece("accel", b"# jarvis accel v1 rate_hz=50\nt_ms\n0\n").status_code,
        brain.put_piece("accel", MOTION).status_code,
        brain.put_piece("accel", ACCEL[:2] + b"\x00" * 40).status_code,
        brain.put_piece("motion", ACCEL).status_code,
        brain.put_piece("audio", ACCEL).status_code,
    ]

    assert refused == [415] * 5
    assert not any(p.is_file() for p in brain.store.directory.rglob("*"))
