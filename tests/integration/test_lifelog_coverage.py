"""ADR 0292: the brain tells the phone whether the Mac is recording him, and where home is.

Acceptance checks against the real archive, the real route and the real device-token guard:

- only a paired device's own token from the tailnet opens ``GET /inherent/lifelog/coverage``;
- the Mac counts as recording only when its terminal is connected AND its newest kept piece
  ended within 15 minutes of the brain's clock; a connected terminal with an old piece, a
  recent piece of a terminal that is no longer connected, and no terminal at all are not;
- the answer names that terminal and the end of its newest piece, whichever day folder holds it;
- home is the kept place with the role ``home`` (the newer when two share it), else ``null``.
"""

from __future__ import annotations

import functools
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi.testclient import TestClient

from jarvis.runtime.daemon import routes
from jarvis.state import places
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.lifelog import COVERAGE_FRESH_MS, LIFELOG_DIRNAME, Lifelog
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime

REMOTE = "100.87.250.92"
MIN_MS = 60_000
PIECE_MS = 10 * MIN_MS


class World:
    """An app with the archive, a hub whose terminals the test chooses, and a paired phone."""

    def __init__(self, tmp_path: Path) -> None:
        """Build the app under ``tmp_path``; no terminal is connected and no place is kept."""
        self.root = tmp_path / "root"
        self.root.mkdir()
        self.store = Lifelog(self.root / LIFELOG_DIRNAME, free_bytes=lambda _path: 10**15)
        self.db = tmp_path / "memory.db"
        self.connected: list[str] = []
        runtime = cast("JarvisRuntime", SimpleNamespace(
            terminal_hub=SimpleNamespace(
                connected=lambda: [(name, frozenset[str]()) for name in self.connected],
            ),
            memory=SimpleNamespace(db_path=self.db),
        ))
        deps = InherentDeps(
            submit_callable=lambda _text: "T",
            broadcaster=InherentBroadcaster(),
            device_name=functools.partial(device_name_for_token, self.root),
            lifelog=self.store,
            lifelog_coverage=routes.lifelog_coverage(runtime, self.store),
        )
        app = create_app(deps)
        self.key = local_key(self.root)
        require_local_key(
            app,
            functools.partial(local_key_matches, self.key),
            extra_hosts=[REMOTE],
            device_token_matches=functools.partial(device_token_matches, self.root),
        )
        self.token = pair_device(self.root, "iphone")
        self.client = TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000))
        self.loopback = TestClient(
            app, base_url="http://127.0.0.1:8006", client=("127.0.0.1", 50000),
        )

    def keep(self, device: str, *, ended_ms_ago: int) -> int:
        """Keep a ten-minute piece of ``device`` that ended ``ended_ms_ago`` ago; return its end."""
        end = int(time.time() * 1000) - ended_ms_ago
        start = end - PIECE_MS
        self.store.save_audio(device, f"p{start}", start, PIECE_MS, b"OggS\x00\x02" + b"\x01" * 64)
        return end

    def ask(self) -> dict[str, Any]:
        """The phone's ask, answered."""
        reply = self.client.get(
            "/inherent/lifelog/coverage", headers={"Authorization": f"Bearer {self.token}"},
        )
        assert reply.status_code == 200
        got: dict[str, Any] = reply.json()
        return got


def test_only_a_paired_devices_own_token_opens_the_route(tmp_path: Path) -> None:
    """The phone's token opens it; the local key, a wrong token and none are refused."""
    w = World(tmp_path)

    assert w.client.get(
        "/inherent/lifelog/coverage", headers={"Authorization": f"Bearer {w.token}"},
    ).status_code == 200
    assert w.client.get(
        "/inherent/lifelog/coverage", headers={"Authorization": f"Bearer {w.key}"},
    ).status_code == 401
    assert w.loopback.get(  # the local key is not a device token
        "/inherent/lifelog/coverage", headers={"Authorization": f"Bearer {w.key}"},
    ).status_code == 403
    assert w.client.get("/inherent/lifelog/coverage").status_code == 401
    assert w.client.get(
        "/inherent/lifelog/coverage", headers={"Authorization": "Bearer nope"},
    ).status_code == 401


def test_the_mac_records_only_with_a_connected_terminal_and_a_fresh_piece(tmp_path: Path) -> None:
    """Fresh piece and link: true. Old piece, dropped link, or no piece: false."""
    w = World(tmp_path)
    assert w.ask()["mac_recording"] is False

    end = w.keep("mac", ended_ms_ago=3 * MIN_MS)
    w.connected = ["mac"]
    got = w.ask()
    assert (got["mac_recording"], got["terminal"], got["newest_piece_end_ms"]) == (True, "mac", end)

    w.connected = []  # the piece is fresh but the terminal is gone
    got = w.ask()
    assert (got["mac_recording"], got["terminal"], got["newest_piece_end_ms"]) == (
        False, None, None,
    )


def test_a_piece_older_than_fifteen_minutes_is_not_recording(tmp_path: Path) -> None:
    """Past the limit it is not recording, but the terminal and its piece are still named."""
    w = World(tmp_path)
    w.connected = ["mac"]
    w.keep("mac", ended_ms_ago=COVERAGE_FRESH_MS + 5_000)
    got = w.ask()
    assert got["mac_recording"] is False
    assert got["terminal"] == "mac"  # still named: facts only
    assert isinstance(got["newest_piece_end_ms"], int)


def test_the_newest_piece_is_found_across_day_folders_and_terminals(tmp_path: Path) -> None:
    """The connected terminal with the newest piece is named; a phone's pieces do not count."""
    w = World(tmp_path)
    w.connected = ["mac", "studio"]
    w.keep("mac", ended_ms_ago=40 * MIN_MS)
    w.keep("mac", ended_ms_ago=30 * 3600 * 1000)  # another day folder
    end = w.keep("studio", ended_ms_ago=2 * MIN_MS)
    w.keep("iphone", ended_ms_ago=1 * MIN_MS)  # the phone's own pieces are not a terminal's

    got = w.ask()
    assert (got["mac_recording"], got["terminal"], got["newest_piece_end_ms"]) == (
        True, "studio", end,
    )


@pytest.mark.parametrize("roles", [[], ["other"], ["home"], ["home", "home"]])
def test_home_is_the_kept_place_with_the_home_role(tmp_path: Path, roles: list[str]) -> None:
    """No home place is ``null``; the newer of two homes wins; radius is the place's own."""
    w = World(tmp_path)
    for i, role in enumerate(roles):
        places.save(
            w.db, name=f"p{i}", lat=48.4 + i * 0.1, lng=-123.3, role=role,
            radius_m=100 + i, origin="user",
        )
    home = w.ask()["home"]
    homes = [i for i, role in enumerate(roles) if role == "home"]
    if not homes:
        assert home is None
    else:
        i = homes[-1]
        assert home == {"lat": 48.4 + i * 0.1, "lng": -123.3, "radius_m": 100 + i}
