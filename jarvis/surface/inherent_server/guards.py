"""Who may reach the server: the Host check, the local key, a paired device's token, body caps.

:func:`require_local_key` puts every route except the liveness probe behind the local key and a
local Host. A peer that is not on loopback cannot read the key, so it carries a paired device's
token instead (ADR 0170); ``POST`` to the pairing claim path is open to any peer (ADR 0196). The
handlers read the same ``Authorization`` header for who is asking (:func:`paired_device`,
:func:`reader_of`) and cap a body while it is read (:func:`capped_body`).
"""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING, Any, Final

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.datastructures import Address, Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketClose

from jarvis.state.event_log import MAC_NODE
from jarvis.surface.device_pairing import CLAIM_PATH

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fastapi import FastAPI, Request, WebSocket
    from starlette.types import ASGIApp, Receive, Scope, Send

    from jarvis.surface.inherent_server.deps import InherentDeps


LOGGER = logging.getLogger(__name__)


# The two authenticated input routes, gated in middleware so the token is the
# first thing checked.
V2_INPUT_PATHS: Final[frozenset[str]] = frozenset(
    {"/inherent/submit/v2", "/inherent/asr-submit/v2"},
)

# A page that rebinds its own domain to 127.0.0.1 still sends that domain as
# Host, so only requests addressed to this machine reach a route.
_LOCAL_HOSTS: Final[tuple[str, ...]] = ("127.0.0.1", "localhost")

# Open without the local key: the liveness probe, and the v2 input routes, which
# check their own per-boot token.
_HEALTH_PATH: Final = "/api/health"
_KEYLESS_PATHS: Final[frozenset[str]] = frozenset(
    {_HEALTH_PATH, *V2_INPUT_PATHS},
)


def bearer_token(header: str | None) -> str | None:
    """Extract the Bearer token from an ``Authorization`` header (D5).

    Returns None for anything that is not exactly ``Bearer <token>``. The
    header value is never logged — a malformed one is indistinguishable
    from a wrong one to everything downstream, which is the point.
    """
    if header is None:
        return None
    scheme, separator, token = header.partition(" ")
    if scheme != "Bearer" or not separator or not token:
        return None
    return token


def is_loopback_peer(client: Address | None) -> bool:
    """Whether the TCP peer is this machine; no peer address (a unix socket) is too."""
    if client is None:
        return True
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def manager_authorize(
    authorize: Callable[[str | None], bool],
    device_token_matches: Callable[[str], bool] | None,
) -> Callable[[str | None], bool]:
    """The handler-level check of the plugin, language, Codex reset and balance routes (ADR 0202).

    ``authorize`` is the local key; with ``device_token_matches`` a paired device's token passes
    too. The middleware has already refused a remote peer without a device token.
    """
    if device_token_matches is None:
        return authorize

    def allowed(header: str | None) -> bool:
        token = bearer_token(header)
        return authorize(header) or (token is not None and device_token_matches(token))

    return allowed


class _LocalKeyMiddleware:
    """Refuse every HTTP request and socket upgrade that lacks the local key.

    Pure ASGI rather than ``app.middleware("http")`` so ``/inherent/ws`` is
    covered too: a socket without the key is closed before ``accept``, which
    the server answers with HTTP 403.

    With ``device_token_matches`` (ADR 0170), a peer that is not on loopback
    cannot read the key, so it must carry a paired device's token on every
    route but the liveness probe. A peer on loopback is checked as before.
    With ``open_claim`` (ADR 0196), ``POST`` to the claim path is open to any
    peer, loopback or not: a device that has no token yet trades its code there.
    """

    def __init__(
        self,
        app: ASGIApp,
        authorize: Callable[[str | None], bool],
        device_token_matches: Callable[[str], bool] | None = None,
        *,
        open_claim: bool = False,
    ) -> None:
        self.app = app
        self.authorize = authorize
        self.device_token_matches = device_token_matches
        self.open_claim = open_claim

    def _refusal(self, scope: Scope) -> str | None:
        """Why the request is refused, or None when it may pass."""
        if scope["path"] == _HEALTH_PATH:
            return None
        if (
            self.open_claim
            and scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] == CLAIM_PATH
        ):
            return None
        header = Headers(scope=scope).get("authorization")
        if self.device_token_matches is not None and not is_loopback_peer(
            Address(*scope["client"]) if scope.get("client") else None,
        ):
            token = bearer_token(header)
            if token is None:
                return "no Authorization header" if header is None else "not a Bearer token"
            return None if self.device_token_matches(token) else "no paired device holds the token"
        if scope["path"] in _KEYLESS_PATHS or self.authorize(header):
            return None
        return "no Authorization header" if header is None else "not the local key"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        refusal = (
            self._refusal(scope) if scope["type"] in {"http", "websocket"} else None
        )
        if refusal is not None:
            # Never the header's value: method, path, peer and why are enough to tell a stale
            # token from a request that went out without one, or to another daemon.
            client = scope.get("client")
            LOGGER.warning(
                "refused %s %r from %s: %s",
                scope.get("method", "WS"), scope["path"], client[0] if client else "a local socket",
                refusal,
            )
            if scope["type"] == "websocket":
                await WebSocketClose(code=1008)(scope, receive, send)
            else:
                await JSONResponse({"detail": "unauthorized"}, status_code=401)(
                    scope, receive, send
                )
            return
        await self.app(scope, receive, send)


def require_local_key(
    app: FastAPI,
    authorize: Callable[[str | None], bool],
    *,
    extra_hosts: Sequence[str] = (),
    device_token_matches: Callable[[str], bool] | None = None,
    open_claim: bool = False,
) -> None:
    """Serve only requests addressed to this machine that carry the local key.

    ``authorize`` checks an ``Authorization`` header value (``Bearer <key>``).
    The host check runs first, so a rebound page is refused before anything
    reads its headers. ``extra_hosts`` are the further names a brain answers to
    on its private addresses, and ``device_token_matches`` is what a peer on
    one of those addresses must present (ADR 0170); both default to nothing.
    ``open_claim`` lets any peer ``POST`` the pairing claim route without a
    token (ADR 0196), the only route besides the liveness probe that needs none;
    the app has to register it (``InherentDeps.pairing``).
    """
    app.add_middleware(
        _LocalKeyMiddleware,
        authorize=authorize,
        device_token_matches=device_token_matches,
        open_claim=open_claim,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*_LOCAL_HOSTS, *extra_hosts])


async def capped_body(request: Request, limit: int) -> bytes:
    """The request body, or a 413 as soon as it is over ``limit`` bytes (none of it kept)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="body too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="body too large")
    return bytes(body)


def paired_device(deps: InherentDeps, request: Request | WebSocket) -> str | None:
    """The paired device whose token this request carries (ADR 0212); ``None`` for the local key."""
    token = bearer_token(request.headers.get("authorization"))
    if deps.device_name is None or token is None:
        return None
    return deps.device_name(token)


def reader_of(deps: InherentDeps, request: Request | WebSocket) -> str:
    """The device a request comes from: a paired one by its token, else this host's own UI."""
    return paired_device(deps, request) or MAC_NODE


def for_reader(card: object, reader: str) -> dict[str, Any] | None:
    """``card`` if ``reader`` may be shown it (ADR 0218): it is that device's, or no device's."""
    if not isinstance(card, dict):
        return None
    device = card.get("device")
    return card if device is None or device == reader else None
