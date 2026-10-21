"""ADR 0312: every fix the phone kept while it moved, and the car's motion while he rode.

Acceptance checks against the real route, the real event log and the real archive:

- a ride sent as track frames through ``POST /inherent/device/events`` reads back from
  ``GET /inherent/track`` fix by fix at its own UTC times, per device, clipped to the window,
  with a fix two frames both hold given once and the sampled fixes left out;
- a frame the brain stamped with its arrival time (sent more than a week late) is still found
  by the window its fixes fall in;
- a window that runs backwards or past 12 hours is a 400;
- a motion piece is kept byte for byte at ``motion/<device>/<UTC day>/…csv.gz`` behind the same
  guard as audio, a resend is answered from what is kept, and only gzip text that opens with
  the motion header is taken, on its own route and not on the audio one.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import time
from typing import TYPE_CHECKING, Any

from fastapi.testclient import TestClient

from jarvis.runtime.daemon import routes
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.lifelog import LIFELOG_DIRNAME, Lifelog
from jarvis.state.phone_track import COLUMNS
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps
from jarvis.surface.inherent_server.guards import require_local_key
from jarvis.surface.phone_events import PHONE_EVENTS_PATH
from jarvis.surface.terminal_events import BrainEvents
from tests.support.terminal import brain_log

if TYPE_CHECKING:
    from pathlib import Path

REMOTE = "100.87.250.92"
LOOPBACK = "127.0.0.1"
NOW = int(time.time() * 1000)
MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR
START_MS = 1_791_756_000_123  # 2026-10-11T22:00:00.123Z
PIECE = "6C1E2F3A-7B3D-4C55-8E21-0A1B2C3D4E5F"
MOTION = gzip.compress(
    b"# jarvis motion v1 frame=x_magnetic_north_z_vertical rate_hz=25\n"
    b"t_ms,ax,ay,az\n0,0.0012,-0.0300,0.0101\n40,0.0013,-0.0310,0.0099\n",
)


class Brain:
    """One app with the log, the track read and the archive wired, and a paired phone."""

    def __init__(self, tmp_path: Path) -> None:
        """A brain with phones ``iphone`` and ``ipad`` paired."""
        self.log = tmp_path / "events.db"
        self.store = Lifelog(tmp_path / LIFELOG_DIRNAME, free_bytes=lambda _path: 100 * 1024**3)
        app = create_app(InherentDeps(
            submit_callable=lambda _text: "T",
            broadcaster=InherentBroadcaster(),
            phone_events=BrainEvents(brain_log(self.log)),
            device_name=functools.partial(device_name_for_token, tmp_path),
            lifelog=self.store,
            track_read=functools.partial(routes.serve_track, self.log),
        ))
        self.key = local_key(tmp_path)
        require_local_key(
            app, functools.partial(local_key_matches, self.key), extra_hosts=[REMOTE],
            device_token_matches=functools.partial(device_token_matches, tmp_path),
        )
        self.tokens = {name: pair_device(tmp_path, name) for name in ("iphone", "ipad")}
        self.remote = TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000))
        self.loopback = TestClient(
            app, base_url=f"http://{LOOPBACK}:8006", client=(LOOPBACK, 50000),
        )

    def send(self, device: str, frames: list[dict[str, Any]]) -> list[bool]:
        """Each frame's ack, through the phone's route under ``device``'s token."""
        sent = self.remote.post(
            PHONE_EVENTS_PATH, json={"events": frames},
            headers={"Authorization": f"Bearer {self.tokens[device]}"},
        )
        assert sent.status_code == 200
        return [ack["ok"] for ack in sent.json()["acks"]]

    def track(self, from_ms: int, to_ms: int) -> Any:  # noqa: ANN401 — the JSON as it came
        """``GET /inherent/track`` with the local key, as the Mac reads it."""
        return self.loopback.get(
            "/inherent/track", params={"from_ms": from_ms, "to_ms": to_ms},
            headers={"Authorization": f"Bearer {self.key}"},
        )

    def put_piece(
        self, kind: str, data: bytes, *, device: str = "iphone", auth: str | None = None,
        via: str = "remote",
    ) -> Any:  # noqa: ANN401 — the response
        """One archive piece of ``kind`` from ``device``."""
        client = self.remote if via == "remote" else self.loopback
        return client.put(
            f"/inherent/lifelog/{kind}/{PIECE}",
            params={"started_ms": START_MS, "duration_ms": 600_000}, content=data,
            headers={"Authorization": f"Bearer {self.tokens[device] if auth is None else auth}"},
        )


def _fix(t_ms: int, lat: float) -> list[Any]:
    return [t_ms, lat, -123.36, 4.7, 21.0, 3.0, 13.4, 0.6, 270.0, 4.0, 0]


def _frame(uid: int, start: int, offsets: list[int], *, ts: int | None = None) -> dict[str, Any]:
    """A track frame of fixes at ``offsets`` from ``start``, latitude counting the fixes."""
    fixes = [_fix(t, 48.0 + t / 1e6) for t in offsets]
    return {
        "event_uid": f"{uid:032x}", "event_type": "phone.track_observed",
        "ts_epoch_ms": start + offsets[-1] if ts is None else ts,
        "payload": {"started_at_ms": start, "ended_at_ms": start + offsets[-1], "fixes": fixes},
    }


def test_a_ride_reads_back_fix_by_fix_per_device_within_the_window(tmp_path: Path) -> None:
    """Two minutes of one phone, one of another, and a sampled fix that is not in the track."""
    brain = Brain(tmp_path)
    ride = NOW - HOUR
    first = list(range(0, 60_000, 1000))
    assert brain.send("iphone", [
        _frame(1, ride, first),
        _frame(2, ride + 59_000, [0, 1000, 2000]),  # its first fix is the first frame's last
        {"event_uid": f"{3:032x}", "event_type": "phone.location_observed", "ts_epoch_ms": ride,
         "payload": {"lat": 1.0, "lng": 1.0, "accuracy_m": 5.0}},
    ]) == [True, True, True]
    assert brain.send("ipad", [_frame(4, ride + 30_000, [0, 500])]) == [True]

    whole = brain.track(ride - MINUTE, ride + 5 * MINUTE)
    clipped = brain.track(ride + 10_000, ride + 12_000).json()

    assert whole.status_code == 200
    body = whole.json()
    assert body["columns"] == list(COLUMNS)
    assert (body["from_ms"], body["to_ms"]) == (ride - MINUTE, ride + 5 * MINUTE)
    ipad, iphone = body["tracks"]
    assert [ipad["device"], iphone["device"]] == ["ipad", "iphone"]
    assert [f[0] for f in iphone["fixes"]] == [ride + t for t in range(0, 62_000, 1000)]
    assert iphone["fixes"][0] == [ride, 48.0, -123.36, 4.7, 21.0, 3.0, 13.4, 0.6, 270.0, 4.0, 0]
    assert [f[0] for f in ipad["fixes"]] == [ride + 30_000, ride + 30_500]
    assert [[f[0] for f in t["fixes"]] for t in clipped["tracks"]] == [
        [ride + 10_000, ride + 11_000, ride + 12_000],
    ]
    assert brain.track(ride + 3 * MINUTE, ride + 4 * MINUTE).json()["tracks"] == []


def test_a_frame_stamped_with_its_arrival_is_found_by_its_fixes(tmp_path: Path) -> None:
    """Sent eight days late, the row is stamped now; the window of its fixes still finds it."""
    brain = Brain(tmp_path)
    old = NOW - 8 * DAY

    assert brain.send("iphone", [_frame(1, old, [0, 1000])]) == [True]

    (track,) = brain.track(old - MINUTE, old + MINUTE).json()["tracks"]
    assert [f[0] for f in track["fixes"]] == [old, old + 1000]


def test_a_window_backwards_or_past_twelve_hours_is_a_400(tmp_path: Path) -> None:
    """The bounds are the caller's mistake, said in words."""
    brain = Brain(tmp_path)

    backwards = brain.track(NOW, NOW - 1)
    too_long = brain.track(NOW - 12 * HOUR - 1, NOW)

    assert (backwards.status_code, too_long.status_code) == (400, 400)
    assert brain.track(NOW - 12 * HOUR, NOW).status_code == 200


def test_a_motion_piece_is_kept_byte_for_byte_beside_the_audio(tmp_path: Path) -> None:
    """201 with the digest, filed under motion/; a resend is 200; the local key is a 403."""
    brain = Brain(tmp_path)

    sent = brain.put_piece("motion", MOTION)
    again = brain.put_piece("motion", MOTION)
    local = brain.put_piece("motion", MOTION, auth=brain.key, via="loopback")

    assert (sent.status_code, again.status_code, local.status_code) == (201, 200, 403)
    assert sent.json() == {
        "id": PIECE, "sha256": hashlib.sha256(MOTION).hexdigest(), "bytes": len(MOTION),
    }
    (path,) = (p for p in brain.store.directory.rglob("*") if p.is_file())
    assert path.relative_to(brain.store.directory).as_posix() == (
        f"motion/iphone/2026-10-11/20261011T220000.123Z_600000ms_{PIECE}.csv.gz"
    )
    assert path.read_bytes() == MOTION


def test_only_motion_text_in_gzip_is_taken_and_only_on_its_own_route(tmp_path: Path) -> None:
    """Plain text, gzip of other text, a broken stream and audio are 415; motion is not audio."""
    brain = Brain(tmp_path)
    ogg = b"OggS\x00\x02" + b"\x02" * 400

    refused = [
        brain.put_piece("motion", b"# jarvis motion v1\nt_ms\n0\n").status_code,
        brain.put_piece("motion", gzip.compress(b"t_ms,ax\n0,0.1\n")).status_code,
        brain.put_piece("motion", MOTION[:2] + b"\x00" * 40).status_code,
        brain.put_piece("motion", ogg).status_code,
        brain.put_piece("audio", MOTION).status_code,
    ]

    assert refused == [415] * 5
    assert not any(p.is_file() for p in brain.store.directory.rglob("*"))
