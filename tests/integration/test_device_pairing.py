"""ADR 0196: a device pairs by claiming a one-time code the owner's screen shows.

Acceptance checks, each against the real app guarded the way the daemon guards a listening
brain (device tokens for remote peers, the local key on loopback, a Host allowlist):

- a code is minted with the local key, claimed by a remote peer that holds no token, and the
  token it returns opens an owner route; the same code never works twice, a wrong, used or
  expired code gets one identical refusal, a newer code voids the older, a restart voids all,
  and the code is nowhere on disk;
- a name paired by other means after the mint cannot be claimed over;
- minting is refused for a bad or taken name and on a brain that does not listen, and any
  paired device may mint, list and unpair, the unpair taking effect on the next request;
- a remote peer with no token still gets 401 on every route but the liveness probe and the
  claim, and the claim is closed to every other method and path, to an oversized body and to a
  wrong Host;
- the URLs a mint lists are exactly the ones the Host check accepts, addresses first.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from jarvis.state.device_tokens import (
    PAIRING_CODE_TTL_S,
    PairingCodes,
    device_token_matches,
    pair_device,
)
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.device_pairing import DevicePairing, brain_urls
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

REMOTE = "100.87.250.92"
PORT = 8006
MINT = "/inherent/devices/pairing"
CLAIM = "/inherent/devices/claim"
OWNER_ROUTE = "/inherent/conversation"
REFUSED = {"detail": "invalid pairing code"}


class _Clock:
    """A settable time source, so ten minutes pass without anyone waiting."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class _Brain:
    """The daemon's guard around the pairing routes, reached as different peers."""

    def __init__(
        self, root: Path, *, listens: bool = True, hosts: tuple[str, ...] = ("jarvis", REMOTE),
    ) -> None:
        self.root = root
        self.clock = _Clock()
        self.codes = PairingCodes(root, clock=self.clock)
        addresses = (REMOTE,) if listens else ()
        self.urls = brain_urls(addresses, hosts, PORT)
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                conversation_read=lambda _after, _limit, _before: {"since": None, "rows": []},
                pairing=DevicePairing(
                    root=root, codes=self.codes, listens=listens, brain_urls=self.urls,
                ),
            ),
        )
        self.key = local_key(root)
        require_local_key(
            app,
            functools.partial(local_key_matches, self.key),
            extra_hosts=hosts,
            device_token_matches=(
                functools.partial(device_token_matches, root) if listens else None
            ),
            open_claim=listens,
        )
        self.app = app

    def peer(self, address: str, host: str = f"jarvis:{PORT}") -> TestClient:
        return TestClient(self.app, base_url=f"http://{host}", client=(address, 50000))

    def owner(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(brain: _Brain, name: str = "phone") -> dict[str, Any]:
    reply = brain.peer("127.0.0.1").post(MINT, json={"name": name}, headers=brain.owner())
    assert reply.status_code == 200, reply.text
    body: dict[str, Any] = reply.json()
    return body


def _files_holding(root: Path, needle: str) -> list[str]:
    return [
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and needle.encode() in path.read_bytes()
    ]


def test_a_code_minted_with_the_local_key_pairs_a_device_that_has_no_token(
    tmp_path: Path,
) -> None:
    """Mint on loopback, claim from the tailnet with nothing, then use the token."""
    brain = _Brain(tmp_path)
    minted = _mint(brain)
    code = minted["code"]
    assert set(minted) == {"device", "code", "expires_at_ms", "brain"}
    assert minted["device"] == "phone"
    assert len(code) >= 43
    assert minted["expires_at_ms"] == int((brain.clock.now + PAIRING_CODE_TTL_S) * 1000)
    assert minted["brain"] == [f"http://{REMOTE}:{PORT}", f"http://jarvis:{PORT}"]

    phone = brain.peer(REMOTE)
    assert phone.get(OWNER_ROUTE).status_code == 401  # no token yet
    claimed = phone.post(CLAIM, json={"code": code})
    assert claimed.status_code == 200
    assert set(claimed.json()) == {"device", "token"}
    assert claimed.json()["device"] == "phone"
    token = claimed.json()["token"]
    assert len(token) >= 43
    assert token != code

    assert device_token_matches(tmp_path, token)
    opened = phone.get(OWNER_ROUTE, headers=_bearer(token))
    assert (opened.status_code, opened.json()) == (200, {"since": None, "rows": []})

    # Only the hash of the token is kept, and the code is never written anywhere.
    assert _files_holding(tmp_path, token) == []
    assert _files_holding(tmp_path, code) == []


def test_a_code_works_once_and_a_wrong_used_or_expired_one_is_refused_alike(
    tmp_path: Path,
) -> None:
    """One refusal body for a wrong, used or expired code; the second claim pairs nothing."""
    brain = _Brain(tmp_path)
    phone = brain.peer(REMOTE)

    code = _mint(brain)["code"]
    for wrong in ("", "wrong", code[:-1], code + "x", code.swapcase()):
        refused = phone.post(CLAIM, json={"code": wrong})
        assert (refused.status_code, refused.json()) == (401, REFUSED), wrong
    # A lone surrogate is valid JSON but no code, and must not be a 500.
    surrogate = phone.post(CLAIM, content=b'{"code": "\\ud800"}')
    assert (surrogate.status_code, surrogate.json()) == (401, REFUSED)
    assert phone.post(CLAIM, json={"code": code}).status_code == 200  # wrong tries burn nothing
    used = phone.post(CLAIM, json={"code": code})
    assert (used.status_code, used.json()) == (401, REFUSED)

    # Valid up to the last second, refused from the tenth minute on.
    last_second = _mint(brain, "tablet")["code"]
    brain.clock.now += PAIRING_CODE_TTL_S - 1
    assert phone.post(CLAIM, json={"code": last_second}).status_code == 200
    expired_code = _mint(brain, "watch")["code"]
    brain.clock.now += PAIRING_CODE_TTL_S
    expired = phone.post(CLAIM, json={"code": expired_code})
    assert (expired.status_code, expired.json()) == (401, REFUSED)
    brain.clock.now -= PAIRING_CODE_TTL_S  # voided, not merely late
    assert phone.post(CLAIM, json={"code": expired_code}).status_code == 401

    paired = brain.peer("127.0.0.1").get("/inherent/devices", headers=brain.owner()).json()
    assert [row["name"] for row in paired["devices"]] == ["phone", "tablet"]


def test_a_new_code_voids_the_old_and_a_restart_voids_all(tmp_path: Path) -> None:
    """At most one code is outstanding, and it lives only in the brain process's memory."""
    brain = _Brain(tmp_path)
    phone = brain.peer(REMOTE)
    first = _mint(brain, "phone")["code"]
    second = _mint(brain, "tablet")["code"]
    assert phone.post(CLAIM, json={"code": first}).status_code == 401
    assert phone.post(CLAIM, json={"code": second}).json()["device"] == "tablet"

    outstanding = _mint(brain, "watch")["code"]
    restarted = _Brain(tmp_path)  # a new process on the same runtime root has no codes
    assert restarted.peer(REMOTE).post(CLAIM, json={"code": outstanding}).status_code == 401
    assert phone.post(CLAIM, json={"code": outstanding}).status_code == 200


def test_a_name_paired_by_other_means_since_the_mint_cannot_be_claimed_over(
    tmp_path: Path,
) -> None:
    """The claim is refused the same way and the code is voided; the older token stays."""
    brain = _Brain(tmp_path)
    code = _mint(brain, "phone")["code"]
    earlier = pair_device(tmp_path, "phone")
    phone = brain.peer(REMOTE)
    refused = phone.post(CLAIM, json={"code": code})
    assert (refused.status_code, refused.json()) == (401, REFUSED)
    assert device_token_matches(tmp_path, earlier)

    # Voided: it stays refused once the name is free again.
    brain.peer("127.0.0.1").delete("/inherent/devices/phone", headers=brain.owner())
    assert phone.post(CLAIM, json={"code": code}).status_code == 401


def test_a_mint_is_refused_for_a_bad_name_a_taken_name_and_a_brain_that_does_not_listen(
    tmp_path: Path,
) -> None:
    """400 for a name that cannot be one, 409 for a taken name, 409 without listening."""
    brain = _Brain(tmp_path)
    local = brain.peer("127.0.0.1")
    for bad in ("", "../x", "-x", "a b", "x" * 65):
        reply = local.post(MINT, json={"name": bad}, headers=brain.owner())
        assert reply.status_code == 400, bad
    not_a_name: list[Any] = [{}, {"name": 5}, {"name": None}, []]
    for body in not_a_name:
        assert local.post(MINT, json=body, headers=brain.owner()).status_code == 400
    assert local.post(MINT, content=b"{nope", headers=brain.owner()).status_code == 400
    pair_device(tmp_path, "macbook")
    taken = local.post(MINT, json={"name": "macbook"}, headers=brain.owner())
    assert (taken.status_code, taken.json()) == (
        409, {"detail": "macbook is already paired; unpair it first"},
    )

    (tmp_path / "alone").mkdir()
    alone = _Brain(tmp_path / "alone", listens=False)
    refused = alone.peer("127.0.0.1").post(MINT, json={"name": "phone"}, headers=alone.owner())
    assert (refused.status_code, refused.json()) == (
        409, {"detail": "this Jarvis does not listen beyond this machine"},
    )
    # No code can exist there, and the claim is not opened to a caller without the key.
    unkeyed = alone.peer("127.0.0.1").post(CLAIM, json={"code": "x"})
    assert (unkeyed.status_code, unkeyed.json()) == (401, {"detail": "unauthorized"})
    keyed = alone.peer("127.0.0.1").post(CLAIM, json={"code": "x"}, headers=alone.owner())
    assert (keyed.status_code, keyed.json()) == (401, REFUSED)


def test_a_brain_none_of_whose_addresses_the_host_check_accepts_mints_nothing(
    tmp_path: Path,
) -> None:
    """A QR whose URLs would all be refused with 400 is not worth showing."""
    brain = _Brain(tmp_path, hosts=())
    assert brain.urls == ()
    reply = brain.peer("127.0.0.1", f"127.0.0.1:{PORT}").post(
        MINT, json={"name": "phone"}, headers=brain.owner(),
    )
    assert reply.status_code == 409
    assert "listen_hosts" in reply.json()["detail"]


def test_a_paired_device_can_mint_list_and_unpair_and_the_unpair_is_immediate(
    tmp_path: Path,
) -> None:
    """The same callers every owner route admits; a token or its hash is never listed."""
    brain = _Brain(tmp_path)
    first = brain.peer(REMOTE).post(CLAIM, json={"code": _mint(brain, "phone")["code"]}).json()
    token = first["token"]
    phone = brain.peer(REMOTE)

    # The phone mints a code for a second device, which claims it.
    second_code = phone.post(MINT, json={"name": "tablet"}, headers=_bearer(token)).json()["code"]
    second = brain.peer(REMOTE).post(CLAIM, json={"code": second_code}).json()
    assert second["device"] == "tablet"

    listed = phone.get("/inherent/devices", headers=_bearer(token))
    assert listed.status_code == 200
    rows = listed.json()["devices"]
    assert [row["name"] for row in rows] == ["phone", "tablet"]
    assert all(set(row) == {"name", "created_at"} for row in rows)
    for secret in (token, second["token"], second_code):
        assert secret not in listed.text
    assert brain.peer("127.0.0.1").get("/inherent/devices", headers=brain.owner()).json() == {
        "devices": rows,
    }

    # Revoking the tablet from the phone: its next request is refused, the phone's is not.
    gone = phone.delete("/inherent/devices/tablet", headers=_bearer(token))
    assert (gone.status_code, gone.json()) == (200, {"unpaired": "tablet"})
    tablet = brain.peer(REMOTE)
    assert tablet.get(OWNER_ROUTE, headers=_bearer(second["token"])).status_code == 401
    assert tablet.get(OWNER_ROUTE, headers=_bearer(token)).status_code == 200
    again = phone.delete("/inherent/devices/tablet", headers=_bearer(token))
    assert again.status_code == 404
    assert phone.delete("/inherent/devices/ghost", headers=_bearer(token)).status_code == 404

    # A device may unpair itself.
    assert phone.delete("/inherent/devices/phone", headers=_bearer(token)).status_code == 200
    assert phone.get(OWNER_ROUTE, headers=_bearer(token)).status_code == 401


def test_an_unauthenticated_remote_peer_reaches_only_the_probe_and_the_claim(
    tmp_path: Path,
) -> None:
    """No token, a wrong one and the local key are all 401 on the other routes."""
    brain = _Brain(tmp_path)
    token = pair_device(tmp_path, "macbook")
    phone = brain.peer(REMOTE)
    for headers in ({}, _bearer("wrong"), {"Authorization": token}, brain.owner()):
        for method, path in (
            ("GET", OWNER_ROUTE),
            ("GET", "/inherent/devices"),
            ("POST", MINT),
            ("DELETE", "/inherent/devices/macbook"),
            ("GET", CLAIM),  # only POST is open
            ("PUT", CLAIM),
            ("DELETE", CLAIM),
            ("POST", CLAIM + "/"),
            ("POST", "/inherent/devices/claim/x"),
        ):
            reply = phone.request(method, path, headers=headers, json={"name": "x", "code": "x"})
            assert reply.status_code == 401, (method, path, sorted(headers))
    assert phone.get("/api/health").status_code == 200
    assert device_token_matches(tmp_path, token)  # none of that unpaired anybody

    # The claim answers for itself, from loopback too, with or without a header.
    for peer in (REMOTE, "127.0.0.1"):
        for headers in ({}, _bearer("wrong")):
            reply = brain.peer(peer).post(CLAIM, json={"code": "x"}, headers=headers)
            assert (reply.status_code, reply.json()) == (401, REFUSED)
    code = _mint(brain)["code"]
    assert brain.peer("127.0.0.1").post(CLAIM, json={"code": code}).status_code == 200


def test_the_claim_caps_its_body_and_is_held_to_the_host_allowlist(tmp_path: Path) -> None:
    """A big body is 413 before it is parsed; a Host the brain does not answer to is 400."""
    brain = _Brain(tmp_path)
    phone = brain.peer(REMOTE)
    code = _mint(brain)["code"]
    assert phone.post(CLAIM, json={"code": "x" * 5000}).status_code == 413
    assert phone.post(CLAIM, content=b"x" * 5000).status_code == 413

    def chunks() -> Iterator[bytes]:  # no content-length header: the cap is on bytes read
        yield b'{"code": "'
        yield b"x" * 5000
        yield b'"}'

    assert phone.post(CLAIM, content=chunks()).status_code == 413
    assert phone.post(MINT, json={"name": "x" * 5000}, headers=brain.owner()).status_code == 401
    local = brain.peer("127.0.0.1")
    assert local.post(MINT, json={"name": "x" * 5000}, headers=brain.owner()).status_code == 413
    not_a_code: list[Any] = [[], "x", {"code": 5}, {"code": None}, {}]
    for body in not_a_code:
        assert phone.post(CLAIM, json=body).status_code == 400, body
    assert phone.post(CLAIM, content=b"{nope").status_code == 400

    for host in ("attacker.example", f"attacker.example:{PORT}"):
        refused = brain.peer(REMOTE, host).post(CLAIM, json={"code": code})
        assert refused.status_code == 400, host
    assert phone.post(CLAIM, json={"code": code}).status_code == 200  # nothing above burned it


def test_every_url_a_mint_lists_passes_the_host_check(tmp_path: Path) -> None:
    """Each listed URL's authority is accepted as a Host; the check still refuses others."""
    brain = _Brain(tmp_path)
    listed = _mint(brain)["brain"]
    assert listed
    for url in listed:
        authority = url.removeprefix("http://")
        reply = brain.peer(REMOTE, authority).get("/api/health")
        assert reply.status_code == 200, url
    assert brain.peer(REMOTE, "elsewhere:8006").get("/api/health").status_code == 400


@pytest.mark.parametrize(
    ("addresses", "hosts", "expected"),
    [
        # Addresses first, then names, each in the order written.
        (["100.1.1.1"], ["jarvis", "100.1.1.1"], ["100.1.1.1", "jarvis"]),
        (["100.1.1.1", "100.1.1.2"], ["b.ts.net", "100.1.1.2", "a", "100.1.1.1"],
         ["100.1.1.1", "100.1.1.2", "b.ts.net", "a"]),
        # A listen address the Host check would refuse is not listed.
        (["100.1.1.1"], ["jarvis"], ["jarvis"]),
        (["100.1.1.1"], [], []),
        # A listen_hosts address the brain does not listen on is not listed either.
        ([], ["100.1.1.9", "jarvis"], ["jarvis"]),
        # The check cuts a Host at its first colon, so an IPv6 literal can never match.
        (["fd7a::1"], ["fd7a::1", "[fd7a::1]", "jarvis"], ["jarvis"]),
        # Not a host name a URL can carry.
        (["100.1.1.1"], ["a b", "a/b", "a:80", "", "100.1.1.1"], ["100.1.1.1"]),
        # Repeats collapse.
        (["100.1.1.1", "100.1.1.1"], ["jarvis", "jarvis", "100.1.1.1"], ["100.1.1.1", "jarvis"]),
        ([], [], []),
    ],
)
def test_brain_urls_list_what_the_host_check_accepts_addresses_first(
    addresses: list[str], hosts: list[str], expected: list[str],
) -> None:
    """Table: listen addresses and hosts in, the base URLs a device can dial out."""
    assert brain_urls(addresses, hosts, 8006) == tuple(
        f"http://{host}:8006" for host in expected
    )
