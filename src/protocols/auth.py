# src/protocols/auth.py
"""
Authentication for the MCP server.

Two credential types are accepted on the JSON-RPC surface:

1. The static ``MCP_AUTH_TOKEN`` (RFC 6750 bearer) -- for the owner's own
   agents, scripts, and machine-to-machine callers.
2. OAuth 2.1 access tokens issued by the built-in authorization server
   (src/protocols/oauth.py) -- for third-party MCP clients such as the
   Gemini app, which only speak OAuth.

Layout follows the production remote-MCP pattern:

    Client --> BearerAuthMiddleware --> Starlette app --> tool registry

* Tokens compared in constant time (hmac.compare_digest).
* Fail-closed: the server refuses to start when no static token is configured.
* GET /health, /.well-known/*, and /oauth/* stay unauthenticated by design
  (liveness probes and the OAuth dance itself).
* 401 responses carry a WWW-Authenticate header pointing at the RFC 9728
  protected-resource metadata, so spec-compliant clients auto-discover OAuth.
"""
import hmac
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("de-job-intelligence.auth")


def _is_exempt_path(path: str) -> bool:
    return (
        path == "/health"
        or path.startswith("/.well-known/")
        or path.startswith("/oauth/")
    )


class BearerAuthMiddleware:
    """ASGI bearer-token gate for the MCP JSON-RPC endpoint."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise RuntimeError(
                "MCP_AUTH_TOKEN is not set. Refusing to start an unauthenticated "
                "MCP server: set MCP_AUTH_TOKEN in the environment (or .env) to a "
                "strong random value, e.g. `openssl rand -hex 32`."
            )
        self.app = app
        self._token = token.encode("utf-8")

    def _is_authorized(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, credential = auth.partition(" ")
        if scheme.lower() != "bearer" or not credential:
            return False
        if hmac.compare_digest(credential.encode("utf-8"), self._token):
            return True
        # OAuth 2.1 access token issued by our own authorization server.
        from src.protocols.token_store import get_store

        return get_store().get_access_token(credential) is not None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        if _is_exempt_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        if not self._is_authorized(scope):
            logger.warning("Rejected unauthenticated request to %s", scope.get("path"))
            from src.core.config import settings

            metadata_url = (
                f"{settings.MCP_PUBLIC_URL.rstrip('/')}/.well-known/oauth-protected-resource"
            )
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32001,
                        "message": "Unauthorized: valid bearer token required",
                    },
                    "id": None,
                },
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{metadata_url}", '
                        'error="invalid_token", '
                        'error_description="Valid bearer token required"'
                    )
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
