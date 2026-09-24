"""A real OAuth-protected MCP server over Streamable HTTP, run as a subprocess by the tests.

It is its own authorization server: dynamic client registration, an
auto-approving ``/authorize``, PKCE checked by the SDK, refresh tokens that
rotate. It also accepts the fixed bearer ``static-secret`` so the same
process exercises the ``headers`` path. Argv: the port to bind.
"""

from __future__ import annotations

import base64
import secrets
import sys
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlencode

import uvicorn
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    IdentityAssertionParams,
    RefreshToken,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.types import Message

STATIC_BEARER = "static-secret"


class Provider:
    """In-memory authorization server; every call is answered without a human."""

    def __init__(self, auth_method: str) -> None:
        """Start with the fixed bearer already valid."""
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {
            STATIC_BEARER: AccessToken(token=STATIC_BEARER, client_id="static", scopes=[])
        }
        self.refresh: dict[str, RefreshToken] = {}
        self.issued = 0
        self.refreshes = 0
        self.auth_method = auth_method

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """A registered client, or None."""
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Select the authentication method as a third-party authorization server may."""
        client_info.token_endpoint_auth_method = self.auth_method
        if self.auth_method == "none":
            client_info.client_secret = None
            client_info.client_secret_expires_at = None
        self.clients[client_info.client_id] = client_info

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Approve at once: redirect straight back with a code."""
        code = secrets.token_urlsafe(16)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        query = {"code": code, **({"state": params.state} if params.state else {})}
        return f"{params.redirect_uri}?{urlencode(query)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """The pending code, if this client owns it."""
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """One code, one token set."""
        del self.codes[authorization_code.code]
        return self._issue(client.client_id, authorization_code.scopes)

    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        self.issued += 1
        access, refresh = f"at-{self.issued}", f"rt-{self.issued}"
        self.access[access] = AccessToken(
            token=access, client_id=client_id, scopes=scopes, expires_at=int(time.time()) + 3600
        )
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",  # noqa: S106 — the OAuth token type, not a secret.
            expires_in=3600,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """The refresh token, if this client owns it."""
        token = self.refresh.get(refresh_token)
        return token if token and token.client_id == client.client_id else None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Rotate: the old refresh token dies with the exchange."""
        self.refreshes += 1
        del self.refresh[refresh_token.token]
        return self._issue(client.client_id, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Bearer check for every MCP request."""
        return self.access.get(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Forget a token of either kind."""
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        """Not offered by this server."""
        raise NotImplementedError


def strict_token_endpoint(provider: Provider) -> Callable[[Request], Awaitable[Response]]:
    """Reject duplicate client authentication at the HTTP boundary, as Linear does."""
    handler = TokenHandler(provider, ClientAuthenticator(provider))

    async def token(request: Request) -> Response:
        data = dict(await request.form())
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Basic "):
            if "client_id" in data or "client_secret" in data:
                return JSONResponse(
                    {
                        "error": "invalid_request",
                        "error_description": "Client must not use multiple authentication methods",
                    },
                    status_code=400,
                )
            # The SDK server requires a body client_id internally. Supply it only
            # after checking the wire request; its handler still validates the
            # Basic secret, PKCE, code ownership and refresh token normally.
            credentials = base64.b64decode(authorization[6:]).decode()
            data["client_id"] = unquote(credentials.split(":", 1)[0])
        body = urlencode(data).encode()

        async def receive() -> Message:
            return {"type": "http.request", "body": body}

        response = await handler.handle(Request(request.scope, receive))
        assert isinstance(response, Response)
        return response

    return token


def main(port: int, auth_method: str = "client_secret_post") -> None:
    """Serve the protected MCP endpoint and its authorization server on one port."""
    provider = Provider(auth_method)
    server = MCPServer(
        "oauth-echo",
        auth_server_provider=provider,
        auth=AuthSettings.model_validate(
            {
                "issuer_url": f"http://127.0.0.1:{port}",
                "resource_server_url": f"http://127.0.0.1:{port}/mcp",
                "client_registration_options": ClientRegistrationOptions(enabled=True),
            }
        ),
    )

    @server.tool()
    def whoami() -> dict[str, Any]:
        """How many token sets this server issued and how many came from a refresh."""
        return {"tokens_issued": provider.issued, "refreshes": provider.refreshes}

    app = server.streamable_http_app()
    app.router.routes = [route for route in app.routes if getattr(route, "path", None) != "/token"]
    app.router.routes.append(Route("/token", strict_token_endpoint(provider), methods=["POST"]))
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "client_secret_post")
