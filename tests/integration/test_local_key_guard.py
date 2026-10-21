"""Every daemon route answers only local requests that carry the local key.

Builds the app with every optional route registered, guards it exactly as the
daemon does, and walks the whole route table: no key, a wrong key, and a
rebound Host are each refused on every route; the key on a local Host gets
through. Only the liveness probe is open. The runtime root that holds the
key, the conversations and the audio is closed to other accounts.
"""

from __future__ import annotations

from typing import Any

import pytest

from jarvis.deployment import bootstrap_runtime
from jarvis.state.plugin_settings import local_key
from tests.support.local_key_guard import KEYLESS, guard_client, status_of


def test_the_route_table_is_the_one_this_test_walks(tmp_path: Any) -> None:  # noqa: ANN401
    """Pin the count, so a route added later is walked, not silently skipped.

    76 HTTP method/path pairs, the four FastAPI docs pairs (GET and HEAD of
    ``/openapi.json``, ``/docs``, ``/docs/oauth2-redirect``, ``/redoc``) and
    the ``/inherent/ws`` socket.
    """
    _, _, routes = guard_client(tmp_path)
    assert len(routes) == 85, routes


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "wrong"}],
    ids=["no-key", "wrong-key", "not-bearer"],
)
def test_every_route_refuses_a_caller_without_the_key(
    tmp_path: Any,  # noqa: ANN401
    headers: dict[str, str],
) -> None:
    """401 on every HTTP route, a refused handshake on every socket; health stays open."""
    client, _, routes = guard_client(tmp_path)
    for route in routes:
        method, path = route.split(" ", 1)
        expected = 200 if path in KEYLESS else (1008 if method == "WS" else 401)
        assert status_of(client, route, headers) == expected, route


def test_every_route_refuses_a_rebound_host_even_with_the_key(tmp_path: Any) -> None:  # noqa: ANN401
    """A page whose own domain resolves to 127.0.0.1 sends that domain as Host."""
    client, key, routes = guard_client(tmp_path)
    for host in ("attacker.example:8006", "attacker.example", "127.0.0.1.attacker.example"):
        headers = {"Authorization": f"Bearer {key}", "Host": host}
        for route in routes:
            assert status_of(client, route, headers) == 400, (host, route)


def test_the_key_on_a_local_host_gets_through(tmp_path: Any) -> None:  # noqa: ANN401
    """The same routes answer the desktop, the CLI and the hooks, which carry the key."""
    client, key, routes = guard_client(tmp_path)
    auth = {"Authorization": f"Bearer {key}"}
    for route in routes:
        assert status_of(client, route, auth) not in {401, 1008}, route
    assert client.get("/inherent/conversation", headers=auth).json() == {"since": None, "rows": []}
    local = {**auth, "Host": "localhost"}
    assert client.get("/inherent/conversation", headers=local).status_code == 200
    with client.websocket_connect("ws://127.0.0.1:8006/inherent/ws", headers=auth) as ws:
        ws.close()


def test_other_accounts_cannot_read_the_runtime_root(tmp_path: Any) -> None:  # noqa: ANN401
    """An existing world-readable root is closed at boot; the key file is owner-only."""
    root = tmp_path / "jarvis"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    paths = bootstrap_runtime(root)
    local_key(paths.root)
    assert paths.root.stat().st_mode & 0o777 == 0o700
    assert (paths.root / "plugin-access.json").stat().st_mode & 0o777 == 0o600
