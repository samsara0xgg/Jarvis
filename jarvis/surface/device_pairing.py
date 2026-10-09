"""ADR 0196: pair a device by claiming a one-time code the owner's screen shows.

Four routes on the brain's server. ``POST /inherent/devices/pairing`` mints a code for a device
name, and the owner's screen shows it as a QR; the new device trades it for its own token at
``POST /inherent/devices/claim``, the one route a peer without a token reaches (the middleware in
:mod:`jarvis.surface.inherent_server` opens exactly that path). ``GET /inherent/devices`` and
``DELETE /inherent/devices/{name}`` list and unpair. The code logic is
:class:`jarvis.state.device_tokens.PairingCodes`; this module is only HTTP.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from fastapi import FastAPI, HTTPException, Request

from jarvis.state.device_tokens import (
    DeviceNameTakenError,
    DeviceTokenError,
    paired_devices,
    unpair_device,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from jarvis.state.device_tokens import PairingCodes

CLAIM_PATH: Final = "/inherent/devices/claim"
# A name is at most 64 characters and a code 43, so a request this size is already generous;
# the claim route is read before anyone has proved anything.
_MAX_BODY_BYTES: Final = 1024
_HOST_NAME: Final = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class DevicePairing:
    """What the pairing routes need, wired once by the runtime.

    ``listens`` is whether the brain listens on any private address beyond this machine.
    ``brain_urls`` are the base URLs a device on the tailnet can dial (see :func:`brain_urls`).
    """

    root: Path
    codes: PairingCodes
    listens: bool
    brain_urls: tuple[str, ...]


def brain_urls(addresses: Iterable[str], hosts: Iterable[str], port: int) -> tuple[str, ...]:
    """The base URLs that reach this brain and pass its Host check, addresses first.

    The Host check (:func:`jarvis.surface.inherent_server.require_local_key`) accepts a request
    only when the part of its ``Host`` header before the first ``:`` is a local name or one of
    ``hosts``. So a URL is listed only when its host is in ``hosts``: a listen address that is
    not also a listen host would be refused with 400, and an IPv6 literal can never match
    because the check cuts it at its first colon. A listed address must also be one the brain
    listens on.

    Args:
        addresses: ``runtime.listen_addresses``.
        hosts: ``runtime.listen_hosts``, the names the Host check accepts besides local ones.
        port: The port the daemon serves on.
    """
    accepted = tuple(hosts)
    ips = [address for address in addresses if address in accepted and _is_ipv4(address)]
    names = [name for name in accepted if _HOST_NAME.fullmatch(name) and not _is_ipv4(name)]
    return tuple(dict.fromkeys(f"http://{host}:{port}" for host in (*ips, *names)))


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def _unreachable(pairing: DevicePairing) -> str | None:
    """Why a code minted now could not be claimed, else ``None``."""
    if not pairing.listens:
        return "this Jarvis does not listen beyond this machine"
    if not pairing.brain_urls:
        return (
            "no address this Jarvis listens on is one its Host check accepts; "
            "list it in runtime.listen_hosts"
        )
    return None


async def _small_json(request: Request) -> dict[str, Any]:
    """The request's JSON object, read with a hard cap on the bytes taken."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="request too large")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request too large")
    try:
        body = json.loads(bytes(raw) or b"{}")
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="send a JSON object")
    return body


def register_pairing_routes(app: FastAPI, pairing: DevicePairing | None) -> None:
    """ADR 0196: mint a code, claim it, list the paired devices, unpair one; none without wiring."""
    if pairing is None:
        return
    _register_code_routes(app, pairing)
    _register_device_routes(app, pairing)


def _register_code_routes(app: FastAPI, paired: DevicePairing) -> None:
    @app.post("/inherent/devices/pairing", status_code=200)
    async def mint_code(request: Request) -> dict[str, Any]:
        """``{name}``: a code that pairs that device once, for ten minutes, voiding any earlier."""
        name = (await _small_json(request)).get("name")
        if not isinstance(name, str):
            raise HTTPException(status_code=400, detail="send a JSON object with a name")
        if (unreachable := _unreachable(paired)) is not None:
            raise HTTPException(status_code=409, detail=unreachable)
        try:
            code, expires_at_ms = await asyncio.to_thread(paired.codes.mint, name)
        except DeviceNameTakenError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except DeviceTokenError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {
            "device": name,
            "code": code,
            "expires_at_ms": expires_at_ms,
            "brain": list(paired.brain_urls),
        }

    @app.post(CLAIM_PATH, status_code=200)
    async def claim_code(request: Request) -> dict[str, str]:
        """``{code}``: the new device's token, once. Open to a peer that has no token yet."""
        code = (await _small_json(request)).get("code")
        if not isinstance(code, str):
            raise HTTPException(status_code=400, detail="send a JSON object with a code")
        claimed = await asyncio.to_thread(paired.codes.claim, code)
        if claimed is None:
            raise HTTPException(status_code=401, detail="invalid pairing code")
        return {"device": claimed[0], "token": claimed[1]}


def _register_device_routes(app: FastAPI, paired: DevicePairing) -> None:
    @app.get("/inherent/devices")
    async def list_devices() -> dict[str, list[dict[str, str]]]:
        """Each paired device's name and pairing time; never a token or its hash."""
        rows = await asyncio.to_thread(paired_devices, paired.root)
        return {"devices": [{"name": name, "created_at": at} for name, at in rows]}

    @app.delete("/inherent/devices/{name}")
    async def unpair(name: str) -> dict[str, str]:
        """Revoke a device's token at once; 404 when it is not paired."""
        try:
            await asyncio.to_thread(unpair_device, paired.root, name)
        except DeviceTokenError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {"unpaired": name}


__all__ = [
    "CLAIM_PATH",
    "DevicePairing",
    "brain_urls",
    "register_pairing_routes",
]
