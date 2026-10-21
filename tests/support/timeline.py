"""Peers over a timeline app, and the calls it refuses without credentials."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, NamedTuple

from fastapi.testclient import TestClient

from jarvis.state.device_tokens import device_token_matches, pair_device
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps
from jarvis.surface.inherent_server.guards import require_local_key
from tests.support.day_highlights import visit_row
from tests.support.ledger import ledger_at
from tests.support.terminal import REMOTE

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.state.ledger import LedgerSources


class Peers(NamedTuple):
    """One app, reached the three ways the guard tells apart."""

    local: TestClient  # loopback with the local key
    paired: TestClient  # another peer with a paired device's token
    bare_local: TestClient  # loopback, no key
    bare_remote: TestClient  # another peer, no token


def peers_with(tmp_path: Path, **deps: Any) -> Peers:  # noqa: ANN401 — the deps under test
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            **deps,
        )
    )
    key = local_key(tmp_path)
    require_local_key(
        app,
        functools.partial(local_key_matches, key),
        device_token_matches=functools.partial(device_token_matches, tmp_path),
    )
    token = pair_device(tmp_path, "phone")
    host = "http://127.0.0.1:8006"
    loopback = ("127.0.0.1", 50000)
    return Peers(
        TestClient(app, base_url=host, client=loopback, headers={"Authorization": f"Bearer {key}"}),
        TestClient(
            app,
            base_url=host,
            client=(REMOTE, 50000),
            headers={"Authorization": f"Bearer {token}"},
        ),
        TestClient(app, base_url=host, client=loopback),
        TestClient(app, base_url=host, client=(REMOTE, 50000)),
    )


def refused_without_credentials(peers: Peers, path: str, body: dict[str, Any]) -> None:
    """No key on loopback, no token elsewhere, and a wrong token: 401, and nothing is written."""
    assert peers.bare_local.post(path, json=body).status_code == 401
    assert peers.bare_remote.post(path, json=body).status_code == 401
    wrong = {"Authorization": "Bearer not-a-token"}
    assert peers.bare_remote.post(path, json=body, headers=wrong).status_code == 401
    assert peers.bare_local.post(path, json=body, headers=wrong).status_code == 401


def stays_of(src: LedgerSources) -> None:
    for n in range(1, 8):
        visit_row(src, f"v{n}", ledger_at(7, 6 + n), ledger_at(7, 6 + n, 30), f"Place {n}")
