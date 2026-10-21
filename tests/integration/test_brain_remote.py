"""ADR 0170 step 3: a terminal reaches the brain over the private network with its own token.

Four acceptance checks, each against the real code: the `pair`, `unpair` and `devices`
commands and the file they keep; the whole route table guarded the way the daemon guards it
when the brain listens beyond loopback (no token, a wrong one, a revoked one, the local key
and a wrong Host from a remote peer are refused, a paired device's token is accepted, a
peer on loopback is checked as before); the v2 HTTP input route taking the
device token from a remote peer and the boot token from loopback only; `runtime.listen_*`
open to a brain and to a Mac running alone (ADR 0207); and the one-shot CLI as a remote client
of a real server, exiting non-zero with a reason when the turn fails.
"""

from __future__ import annotations

import asyncio
import functools
import json
import socket
import stat
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Self

import pytest
import uvicorn
from fastapi.testclient import TestClient

from jarvis.cli import main
from jarvis.runtime.config import RuntimeBootstrapError
from jarvis.state.device_tokens import device_token_matches
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import (
    InherentDeps,
    InherentV2Deps,
    create_app,
    require_local_key,
)
from tests.support.brain_role import brain_runtime
from tests.support.local_key_guard import KEYLESS, guard_client, status_of

if TYPE_CHECKING:
    from pathlib import Path

REMOTE = "100.87.250.92"


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# --- pair / unpair / devices -------------------------------------------------


def test_pair_prints_only_the_token_and_keeps_only_its_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The token is stdout and nothing else; the 0600 file holds a name, a date and a hash."""
    code, out, err = _run(capsys, "pair", "macbook", "--runtime-root", str(tmp_path))
    token = out.rstrip("\n")
    assert (code, err) == (0, "")
    assert out == token + "\n"
    assert len(token) >= 43
    assert token.replace("-", "").replace("_", "").isalnum()

    stored = tmp_path / "devices.json"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert token not in stored.read_text(encoding="utf-8")
    row = json.loads(stored.read_text(encoding="utf-8"))["devices"]["macbook"]
    assert set(row) == {"sha256", "created_at"}

    code, out, err = _run(capsys, "devices", "--runtime-root", str(tmp_path))
    assert code == 0
    assert out.startswith("macbook\t")
    assert row["created_at"] in out
    assert token not in out + err
    assert row["sha256"] not in out + err


def test_each_device_has_its_own_token_and_unpair_revokes_it_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Two devices, two tokens; unpairing one needs no restart and leaves the other."""
    laptop = _run(capsys, "pair", "macbook", "--runtime-root", str(tmp_path))[1].strip()
    phone = _run(capsys, "pair", "phone", "--runtime-root", str(tmp_path))[1].strip()
    assert laptop != phone
    assert device_token_matches(tmp_path, laptop)
    assert device_token_matches(tmp_path, phone)
    assert not device_token_matches(tmp_path, "wrong")
    assert not device_token_matches(tmp_path, "")

    assert _run(capsys, "unpair", "macbook", "--runtime-root", str(tmp_path))[0] == 0
    assert not device_token_matches(tmp_path, laptop)
    assert device_token_matches(tmp_path, phone)
    names = _run(capsys, "devices", "--runtime-root", str(tmp_path))[1]
    assert [line.split("\t")[0] for line in names.splitlines()] == ["phone"]


def test_pair_and_unpair_refuse_what_cannot_be_done(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name taken, unknown or malformed, and a terminal for stdout, each give exit 1."""
    root = ("--runtime-root", str(tmp_path))
    token = _run(capsys, "pair", "macbook", *root)[1].strip()
    for argv in (("pair", "macbook"), ("unpair", "ghost"), ("pair", "../x"), ("pair", "")):
        code, out, err = _run(capsys, *argv, *root)
        assert (code, out) == (1, "")
        assert err.startswith(f"jarvis {argv[0]}:")
        assert token not in err

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    code, out, err = _run(capsys, "pair", "tty", *root)
    assert (code, out) == (1, "")
    assert "redirect" in err
    assert [name for name, _ in json.loads(
        (tmp_path / "devices.json").read_text(encoding="utf-8"))["devices"].items()
    ] == ["macbook"]


# --- the guard on every route -----------------------------------------------


def _remote(tmp_path: Path, **guard: Any) -> tuple[TestClient, str, list[str]]:  # noqa: ANN401
    return guard_client(
        tmp_path,
        peer=REMOTE,
        extra_hosts=("jarvis", REMOTE),
        device_token_matches=functools.partial(device_token_matches, tmp_path),
        **guard,
    )


def _pair(root: Path, name: str, capsys: pytest.CaptureFixture[str]) -> str:
    return _run(capsys, "pair", name, "--runtime-root", str(root))[1].strip()


def _refused(method_path: str) -> int:
    return 1008 if method_path.startswith("WS ") else 401


def test_a_remote_peer_needs_a_device_token_on_every_route(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """No token, a wrong one and the local key are refused on every route but health.

    The plugin, language, Codex reset and balance routes check again in their handler (ADR 0202):
    a device token opens them as it opens every other route.
    """
    client, key, routes = _remote(tmp_path)
    token = _pair(tmp_path, "macbook", capsys)
    host = {"Host": "jarvis:8006"}
    for headers in (
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": token},
        {"Authorization": f"Bearer {key}"},  # the local key is not for remote peers
    ):
        for route in routes:
            path = route.split(" ", 1)[1]
            expected = 200 if path in KEYLESS else _refused(route)
            assert status_of(client, route, {**headers, **host}) == expected, (
                route,
                sorted(headers),
            )

    good = {"Authorization": f"Bearer {token}", **host}
    for route in routes:
        assert status_of(client, route, good) not in {401, 1008}, route
    assert client.get("/inherent/conversation", headers=good).json() == {
        "since": None,
        "rows": [],
    }


def test_a_paired_device_manages_plugins_and_a_wrong_token_does_not(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ADR 0202: the plugin, language and usage-write routes answer 200 to a device token only."""
    client, key, _ = _remote(tmp_path)
    token = _pair(tmp_path, "phone", capsys)
    plugin_action = {"operation": "open", "data": {"plugin_id": "x"}}
    reset = {"request_id": "00000000-0000-0000-0000-000000000000"}
    requests: tuple[tuple[str, str, dict[str, Any]], ...] = (
        ("GET", "/inherent/plugins", {}),
        ("GET", "/inherent/plugins/x/icon", {}),
        ("POST", "/inherent/plugins/action", plugin_action),
        ("POST", "/inherent/language", {"language": "en"}),
        ("POST", "/inherent/usage/codex/reset", reset),
        ("POST", "/inherent/usage/balance", {"service": "openai", "usd": 1}),
    )
    for bearer, expected in ((token, 200), ("wrong", 401), (key, 401)):
        headers = {"Authorization": f"Bearer {bearer}", "Host": "jarvis"}
        for method, path, body in requests:
            sent = client.request(method, path, headers=headers, json=body or None)
            status = sent.status_code
            assert status == expected, (method, path, bearer == token)


def test_revoking_a_device_takes_effect_without_a_restart(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same running app answers 200, then 401 once the device is unpaired."""
    client, _, _ = _remote(tmp_path)
    token = _pair(tmp_path, "macbook", capsys)
    headers = {"Authorization": f"Bearer {token}", "Host": "jarvis"}
    assert client.get("/inherent/conversation", headers=headers).status_code == 200
    assert _run(capsys, "unpair", "macbook", "--runtime-root", str(tmp_path))[0] == 0
    assert client.get("/inherent/conversation", headers=headers).status_code == 401


def test_every_refusal_is_logged_with_why_and_never_the_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A phone that loses its pairing over a 401 can be traced from the brain's log alone."""
    client, key, _ = _remote(tmp_path)
    token = _pair(tmp_path, "phone", capsys)
    host = {"Host": "jarvis"}
    with caplog.at_level("WARNING", logger="jarvis.surface.inherent_server"):
        for headers in (
            {},
            {"Authorization": token},
            {"Authorization": f"Bearer {key}"},
            {"Authorization": f"Bearer {token}"},
        ):
            client.get("/inherent/conversation", headers={**headers, **host})
        assert _run(capsys, "unpair", "phone", "--runtime-root", str(tmp_path))[0] == 0
        stale = {"Authorization": f"Bearer {token}", **host}
        client.post("/inherent/plugins/action", headers=stale)
    assert [r.getMessage() for r in caplog.records] == [
        f"refused GET '/inherent/conversation' from {REMOTE}: no Authorization header",
        f"refused GET '/inherent/conversation' from {REMOTE}: not a Bearer token",
        f"refused GET '/inherent/conversation' from {REMOTE}: no paired device holds the token",
        f"refused POST '/inherent/plugins/action' from {REMOTE}: no paired device holds the token",
    ]
    assert token not in caplog.text
    assert key not in caplog.text


def test_a_remote_peer_is_held_to_the_host_allowlist(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid token with a Host the brain was not told to answer to is a 400, on every route."""
    client, _, routes = _remote(tmp_path)
    token = _pair(tmp_path, "macbook", capsys)
    for host in ("attacker.example", "attacker.example:8006", "jarvis.attacker.example"):
        headers = {"Authorization": f"Bearer {token}", "Host": host}
        for route in routes:
            assert status_of(client, route, headers) == 400, (host, route)
    for host in ("jarvis", f"{REMOTE}:8006", "127.0.0.1", "localhost"):
        headers = {"Authorization": f"Bearer {token}", "Host": host}
        assert client.get("/inherent/conversation", headers=headers).status_code == 200, host


def test_loopback_is_checked_exactly_as_before(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On loopback the local key opens everything and a device token opens nothing."""
    client, key, routes = guard_client(
        tmp_path,
        peer="127.0.0.1",
        extra_hosts=("jarvis",),
        device_token_matches=functools.partial(device_token_matches, tmp_path),
    )
    token = _pair(tmp_path, "macbook", capsys)
    for route in routes:
        path = route.split(" ", 1)[1]
        expected = 200 if path in KEYLESS else _refused(route)
        assert status_of(client, route, {}) == expected, route
        assert status_of(client, route, {"Authorization": f"Bearer {token}"}) == expected, route
        assert status_of(client, route, {"Authorization": f"Bearer {key}"}) not in {401, 1008}, (
            route
        )


def test_without_listen_settings_nothing_about_the_guard_changes(tmp_path: Path) -> None:
    """The default guard allows no other Host, and a peer's address changes nothing."""
    client, key, routes = guard_client(tmp_path, peer=REMOTE)
    for route in routes:
        path = route.split(" ", 1)[1]
        assert status_of(client, route, {"Host": "jarvis"}) == 400, route
        expected = 200 if path in KEYLESS else _refused(route)
        assert status_of(client, route, {}) == expected, route
        assert status_of(client, route, {"Authorization": f"Bearer {key}"}) not in {401, 1008}, (
            route
        )


# --- the v2 input route --------------------------------------------------------


def _v2_client(tmp_path: Path, peer: str) -> tuple[TestClient, str]:
    boot_token = "boot-" + "b" * 40
    deps = InherentDeps(
        submit_callable=lambda _text: "T1",
        broadcaster=InherentBroadcaster(),
        v2=InherentV2Deps(
            token_matches=lambda presented: presented == boot_token,
            device_token_matches=functools.partial(device_token_matches, tmp_path),
        ),
    )
    app = create_app(deps)
    matches = functools.partial(local_key_matches, local_key(tmp_path))
    require_local_key(
        app,
        matches,
        extra_hosts=("jarvis",),
        device_token_matches=functools.partial(device_token_matches, tmp_path),
    )
    return TestClient(app, base_url="http://jarvis:8006", client=(peer, 50000)), boot_token


def test_the_v2_route_takes_the_device_token_from_a_remote_peer_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A remote peer cannot read the boot token, so it presents its device token instead."""
    token = _pair(tmp_path, "macbook", capsys)
    body = {"request_id": "R1", "client_instance_id": "I1", "text": "hi",
            "client_created_at_ms": 1}
    submit = "/inherent/submit/v2"

    def status(client: TestClient, presented: str | None) -> int:
        headers = {} if presented is None else {"Authorization": f"Bearer {presented}"}
        return client.post(submit, json=body, headers=headers).status_code

    # Past the guard the route answers for itself: this rig wires no input inbox.
    past_the_guard = 501
    remote, boot = _v2_client(tmp_path, REMOTE)
    assert status(remote, token) == past_the_guard
    assert status(remote, boot) == 401
    assert status(remote, "wrong") == 401
    assert status(remote, None) == 401

    loopback, boot = _v2_client(tmp_path, "127.0.0.1")
    assert status(loopback, boot) == past_the_guard
    assert status(loopback, token) == 403


# --- runtime.listen_* ---------------------------------------------------------


def test_listen_settings_are_empty_by_default_and_either_role_may_set_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default: loopback only. A brain and a Mac running alone (ADR 0207) may listen further.

    Only a brain has a terminal hub; any daemon that listens can take a phone's events.
    """
    for i in range(3):
        (tmp_path / str(i)).mkdir()
    listen = f"  listen_addresses: ['{REMOTE}']\n  listen_hosts: [jarvis]\n"
    default = brain_runtime(tmp_path / "0", "assistant_name: Jarvis\n", monkeypatch)
    brain = brain_runtime(tmp_path / "1", f"runtime:\n  role: brain\n{listen}", monkeypatch)
    alone = brain_runtime(tmp_path / "2", f"runtime:\n  role: all\n{listen}", monkeypatch)
    try:
        assert (default.config.listen_addresses, default.config.listen_hosts) == ((), ())
        assert default.phone_events is None
        for listening in (brain, alone):
            assert listening.config.listen_addresses == (REMOTE,)
            assert listening.config.listen_hosts == ("jarvis",)
            assert listening.phone_events is not None
        assert brain.terminal_hub is not None
        assert alone.terminal_hub is None
    finally:
        default.conn.close()
        brain.conn.close()
        alone.conn.close()


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("listen_addresses: ['0.0.0.0']", "not a private address"),
        ("listen_addresses: ['::']", "not a private address"),
        ("listen_addresses: ['8.8.8.8']", "not a private address"),
        ("listen_addresses: [jarvis]", "not an IP address"),
        ("listen_addresses: '100.87.250.92'", "must be a list"),
        ("listen_hosts: ['*']", "no wildcards"),
        ("listen_hosts: ['*.example']", "no wildcards"),
        ("listen_hosts: ['']", "no wildcards"),
    ],
)
def test_a_daemon_never_listens_on_a_wildcard_or_public_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    message: str,
) -> None:
    """The settings cannot open the daemon to the internet."""
    with pytest.raises(RuntimeBootstrapError, match=message):
        brain_runtime(tmp_path, f"runtime:\n  role: brain\n  {setting}\n", monkeypatch)


# --- the CLI as a remote client ----------------------------------------------


class _Server:
    """A real uvicorn server on loopback whose submit fails or answers as the test says."""

    def __init__(self, tmp_path: Path, reply: str | None) -> None:
        self.key = local_key(tmp_path)
        self.broadcaster = InherentBroadcaster()
        self.reply = reply
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        app = create_app(
            InherentDeps(submit_callable=self._submit, broadcaster=self.broadcaster),
        )
        require_local_key(app, functools.partial(local_key_matches, self.key))
        self.server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"
            ),
        )
        self.loop = asyncio.new_event_loop()
        self.broadcaster.attach_loop(self.loop)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _submit(self, text: str) -> str:
        if self.reply is None:
            self.broadcaster.broadcast_op_sync(
                "failed",
                turn_id="T1",
                reason="missing_key",
                message="no model key",
            )
        else:
            for op, payload in (
                ("open", {"q": text, "turn_id": "T1"}),
                ("append", {"token": self.reply, "turn_id": "T1"}),
                ("done", {"turn_id": "T1"}),
            ):
                self.broadcaster.broadcast_op_sync(op, **payload)
        return "T1"

    def _serve(self) -> None:
        self.loop.run_until_complete(self.server.serve())

    def __enter__(self) -> Self:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server never started"
            time.sleep(0.05)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _token_file(path: Path, value: str, mode: int = 0o600) -> Path:
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_a_failed_turn_exits_nonzero_with_a_reason_and_prints_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`turn.failed` reaches the CLI as `failed`: exit 5, the reason on stderr, stdout empty."""
    with _Server(tmp_path, reply=None) as brain:
        token = _token_file(tmp_path / "brain-token", brain.key)
        code, out, err = _run(
            capsys,
            "--brain",
            brain.url,
            "--brain-token-file",
            str(token),
            "你好",
        )
    assert (code, out) == (5, "")
    assert err == "jarvis: the turn failed: no model key (missing_key)\n"


def test_the_cli_prints_the_brains_answer_using_the_token_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default token file is `<runtime root>/brain-token`; the answer is stdout."""
    with _Server(tmp_path, reply="你好 Allen") as brain:
        _token_file(tmp_path / "brain-token", brain.key)
        code, out, err = _run(
            capsys,
            "--brain",
            brain.url + "/",
            "--runtime-root",
            str(tmp_path),
            "你好",
        )
    assert (code, out, err) == (0, "你好 Allen\n", "")


def test_the_cli_refuses_an_unpaired_device_and_a_token_file_others_can_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A wrong token is refused by the brain; a loose, missing or empty file never reaches it."""
    with _Server(tmp_path, reply="never sent") as brain:
        wrong = _token_file(tmp_path / "wrong", "not-a-paired-token")
        code, out, err = _run(capsys, "--brain", brain.url, "--brain-token-file", str(wrong), "hi")
        assert (code, out) == (1, "")
        assert "daemon refused the connection: HTTP 403" in err

        loose = _token_file(tmp_path / "loose", brain.key, mode=0o644)
        empty = _token_file(tmp_path / "empty", "")
        for token_file, reason in (
            (loose, "readable by others"),
            (empty, "is empty"),
            (tmp_path / "missing", "pair this device first"),
        ):
            code, out, err = _run(
                capsys,
                "--brain",
                brain.url,
                "--brain-token-file",
                str(token_file),
                "hi",
            )
            assert (code, out) == (1, "")
            assert reason in err
            assert brain.key not in err

    code, _, err = _run(capsys, "--brain", "jarvis:8006", "--runtime-root", str(tmp_path), "hi")
    assert code == 1
    assert "--brain must look like" in err
