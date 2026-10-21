"""ADR 0238: a paired phone's recordings of his day are kept on the brain as the raw archive.

Acceptance checks against the real store, the real route and the real device-token guard:

- only a paired device's own token from the tailnet opens the route; the local key, no
  credential, a wrong one and a revoked one are refused;
- a new piece is answered 201 with the SHA-256 of what was kept, and lands, byte for byte and
  0600, at ``audio/<device>/<UTC day>/<UTC start>_<length>ms_<id>.<ext>``;
- sending the same piece again is answered 200 from what is kept; the same id with other bytes
  or another start is refused 409 and the kept file is untouched;
- the container is read from the bytes (CAF, Ogg, MPEG-4 taken; WAV, text and empty refused);
  a body without a length, over the cap, or with a malformed id or time is refused, nothing kept;
- a piece that would take the disk below the floor is refused 507 and nothing is written;
- the runtime puts the archive at ``lifelog.dir`` or in the runtime root, keeps it out of the
  hourly sweep, the clear and the export, and a daemon without it has no route.
"""

from __future__ import annotations

import functools
import hashlib
import stat
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from jarvis.runtime.daemon import retention, routes
from jarvis.state.device_tokens import (
    device_name_for_token,
    device_token_matches,
    pair_device,
    unpair_device,
)
from jarvis.state.lifelog import (
    DEFAULT_MIN_FREE_BYTES,
    LIFELOG_DIRNAME,
    MAX_PIECE_BYTES,
    Lifelog,
)
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from tests.support.runtime_config import runtime_config

if TYPE_CHECKING:
    from pathlib import Path

    from httpx2 import Response  # what the test client answers with

    from jarvis.runtime import JarvisRuntime

REMOTE = "100.87.250.92"
LOOPBACK = "127.0.0.1"
START_MS = 1_791_756_000_123  # 2026-10-11T22:00:00.123Z
CAF = b"caff\x00\x01\x00\x00" + b"\x01" * 4000
OGG = b"OggS\x00\x02" + b"\x02" * 4000
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x03" * 4000
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 4000
PIECE = "9F2C1E6A-7B3D-4C55-8E21-0A1B2C3D4E5F"


@dataclass
class Daemon:
    """One app with the archive wired, a paired phone, and a client on each side."""

    root: Path
    store: Lifelog
    key: str
    token: str
    loopback: TestClient
    remote: TestClient
    free: list[int]

    def put(  # noqa: PLR0913 — the request's every part, each defaulted
        self, data: bytes, piece: str = PIECE, *, started_ms: int = START_MS,
        duration_ms: int = 600_000, auth: str | None = None, via: str = "remote",
    ) -> Response:
        """Send one piece with the phone's token unless ``auth`` says otherwise."""
        client = self.remote if via == "remote" else self.loopback
        return client.put(
            f"/inherent/lifelog/audio/{piece}",
            params={"started_ms": started_ms, "duration_ms": duration_ms},
            content=data,
            headers={"Authorization": f"Bearer {self.token if auth is None else auth}"},
        )

    def kept(self) -> list[Path]:
        """Every file in the archive, temporary ones included."""
        return sorted(p for p in self.store.directory.rglob("*") if p.is_file())


def _daemon(tmp_path: Path, *, wired: bool = True) -> Daemon:
    root = tmp_path / "root"
    root.mkdir()
    free = [100 * 1024**3]
    store = Lifelog(root / LIFELOG_DIRNAME, free_bytes=lambda _path: free[0])
    deps = InherentDeps(
        submit_callable=lambda _text: "T",
        broadcaster=InherentBroadcaster(),
        device_name=functools.partial(device_name_for_token, root),
        lifelog=store if wired else None,
    )
    app = create_app(deps)
    key = local_key(root)
    require_local_key(
        app,
        functools.partial(local_key_matches, key),
        extra_hosts=[REMOTE],
        device_token_matches=functools.partial(device_token_matches, root),
    )
    token = pair_device(root, "iphone")
    return Daemon(
        root=root, store=store, key=key, token=token, free=free,
        loopback=TestClient(app, base_url=f"http://{LOOPBACK}:8006", client=(LOOPBACK, 50000)),
        remote=TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000)),
    )


def test_only_a_paired_devices_own_token_opens_the_route(tmp_path: Path) -> None:
    """The phone's token from the tailnet opens it; the local key is 403, the rest 401."""
    d = _daemon(tmp_path)

    assert d.put(CAF).status_code == 201
    assert d.put(OGG, "other", auth=d.key, via="loopback").status_code == 403
    assert [
        d.put(OGG, "other", auth="nope").status_code,
        d.put(OGG, "other", auth=d.key).status_code,
        d.put(OGG, "other", auth="nope", via="loopback").status_code,
    ] == [401, 401, 401]
    unpair_device(d.root, "iphone")
    assert d.put(OGG, "other").status_code == 401
    assert len(d.kept()) == 1


def test_a_new_piece_is_kept_whole_where_its_start_says(tmp_path: Path) -> None:
    """201 with the digest of what was kept; the bytes, the name and the mode on disk."""
    d = _daemon(tmp_path)

    sent = d.put(CAF)

    assert sent.status_code == 201
    assert sent.json() == {
        "id": PIECE, "sha256": hashlib.sha256(CAF).hexdigest(), "bytes": len(CAF),
    }
    (path,) = d.kept()
    assert path.relative_to(d.store.directory).as_posix() == (
        f"audio/iphone/2026-10-11/20261011T220000.123Z_600000ms_{PIECE}.caf"
    )
    assert path.read_bytes() == CAF
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_sending_a_piece_again_is_answered_from_what_is_kept(tmp_path: Path) -> None:
    """Same piece: 200 and the same digest. Same id, other bytes or start: 409, file untouched."""
    d = _daemon(tmp_path)
    first = d.put(OGG)

    again = d.put(OGG)
    other_bytes = d.put(OGG + b"x")
    other_start = d.put(OGG, started_ms=START_MS + 1000)

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert (other_bytes.status_code, other_start.status_code) == (409, 409)
    (path,) = d.kept()
    assert path.read_bytes() == OGG


@pytest.mark.parametrize(
    ("data", "status", "ext"),
    [(CAF, 201, "caf"), (OGG, 201, "ogg"), (M4A, 201, "m4a"),
     (WAV, 415, None), (b"just some words", 415, None), (b"", 400, None)],
)
def test_the_container_is_read_from_the_bytes(
    tmp_path: Path, data: bytes, status: int, ext: str | None,
) -> None:
    """CAF, Ogg and MPEG-4 are kept under their own extension; anything else is refused."""
    d = _daemon(tmp_path)

    assert d.put(data).status_code == status
    assert [p.suffix for p in d.kept()] == ([] if ext is None else [f".{ext}"])


def test_malformed_requests_keep_nothing(tmp_path: Path) -> None:
    """No length, over the cap, a bad id, a start out of range, a zero length: each refused."""
    d = _daemon(tmp_path)
    auth = {"Authorization": f"Bearer {d.token}"}
    params = {"started_ms": START_MS, "duration_ms": 600_000}

    chunked = d.remote.put(
        f"/inherent/lifelog/audio/{PIECE}", params=params, headers=auth,
        content=iter([CAF[:10], CAF[10:]]),
    )
    over = d.remote.put(
        f"/inherent/lifelog/audio/{PIECE}", params=params,
        headers={**auth, "Content-Length": str(MAX_PIECE_BYTES + 1)}, content=CAF,
    )

    assert chunked.status_code == 411
    assert over.status_code == 413
    assert d.put(CAF, "has a space").status_code in (404, 422)
    assert d.put(CAF, "x" * 65).status_code == 422
    assert d.put(CAF, started_ms=0).status_code == 422
    assert d.put(CAF, duration_ms=0).status_code == 422
    assert d.remote.put(
        f"/inherent/lifelog/audio/{PIECE}", params={"started_ms": START_MS},
        headers=auth, content=CAF,
    ).status_code == 422
    assert d.kept() == []


def test_a_piece_that_would_fill_the_disk_is_refused(tmp_path: Path) -> None:
    """Below the floor after the write: 507 and nothing written; with room again it is kept."""
    d = _daemon(tmp_path)
    d.free[0] = DEFAULT_MIN_FREE_BYTES + len(CAF) - 1

    assert d.put(CAF).status_code == 507
    assert d.kept() == []
    d.free[0] = DEFAULT_MIN_FREE_BYTES + len(CAF)
    assert d.put(CAF).status_code == 201


def test_a_daemon_without_the_archive_has_no_route(tmp_path: Path) -> None:
    """Not wired (a host that takes no device tokens): 404, nothing kept."""
    d = _daemon(tmp_path, wired=False)

    assert d.put(CAF).status_code == 404
    assert not (d.root / LIFELOG_DIRNAME).exists()


def _runtime(root: Path, config: dict[str, object]) -> JarvisRuntime:
    """Just what the archive's wiring and the media sweep read."""
    return cast("JarvisRuntime", SimpleNamespace(
        config=runtime_config(config, root=root),
        runtime_paths=SimpleNamespace(root=root, artifacts_root=root / "artifacts"),
        attachments=None,
        memory=None,
    ))


def test_the_runtime_places_the_archive_and_keeps_it_out_of_sweep_clear_and_export(
    tmp_path: Path,
) -> None:
    """``lifelog.dir`` and ``min_free_gb`` are read; the archive is never a media directory."""
    root = tmp_path / "root"
    default = routes.open_lifelog(_runtime(root, {}))
    moved = routes.open_lifelog(
        _runtime(root, {"lifelog": {"dir": str(tmp_path / "ssd" / "lifelog"), "min_free_gb": 1}}),
    )

    assert (default.directory, default.min_free_bytes) == (
        root / LIFELOG_DIRNAME, DEFAULT_MIN_FREE_BYTES,
    )
    assert (moved.directory, moved.min_free_bytes) == (tmp_path / "ssd" / "lifelog", 1024**3)
    media = retention.media_dirs(_runtime(root, {}))
    assert not any(
        d == default.directory or default.directory in d.parents for d in media
    )


def test_each_piece_and_each_refusal_is_one_log_line_without_its_bytes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Kept, kept again and refused each leave one line naming the device, never the audio."""
    d = _daemon(tmp_path)

    with caplog.at_level("INFO", logger="jarvis.surface.inherent_server"):
        d.put(CAF)
        d.put(CAF)
        d.put(WAV, "other")

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("lifelog:")]
    assert lines == [
        f"lifelog: kept 600 s of audio from iphone ({len(CAF)} bytes)",
        f"lifelog: already had 600 s of audio from iphone ({len(CAF)} bytes)",
        "lifelog: refused audio from iphone with 415: "
        "send audio in a CAF, Ogg or MPEG-4 container",
    ]
