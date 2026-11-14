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
  (liveness probes and the OAuth dance itself), plus GET /favicon.ico so
  connector directories and favicon crawlers can fetch the server icon,
  plus GET / (the public landing page, so favicon crawlers can discover
  the server icon link),
  plus POST /public/mcp -- the read-only public MCP surface. That last
  exemption is safe ONLY because its handler (dashboard_mcp.py) queries
  exclusively through the mcp_dashboard_reader pool, which holds SELECT
  on sanitized views and nothing else. Never exempt another JSON-RPC
  path here: the private tools behind /rpc and /mcp stay bearer-gated.
* 401 responses carry a WWW-Authenticate header pointing at the RFC 9728
  protected-resource metadata, so spec-compliant clients auto-discover OAuth.
"""
import hmac
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.protocols.scopes import ALL_SCOPES, parse_scopes

logger = logging.getLogger("de-job-intelligence.auth")


def _is_exempt_path(path: str, method: str) -> bool:
    # Method-scoped on purpose: "/" must exempt ONLY the GET landing page.
    # A path-only exemption here would also open POST /, the private
    # JSON-RPC endpoint, so the method is part of the exemption check.
    return (
        path == "/health"
        or path == "/favicon.ico"
        or path == "/public/mcp"
        or path.startswith("/.well-known/")
        or path.startswith("/oauth/")
        or (path == "/" and method == "GET")
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

    def _granted_scopes(self, scope: Scope) -> frozenset | None:
        """Granted scopes for the presented credential, or None if unknown.

        The static token is the owner's token: all scopes. An OAuth access
        token resolves to the scopes stored on its record (legacy ``mcp``
        expands to all three). This is the only place that resolves scopes:
        the static token can be overridden per app instance (create_app),
        so no other layer may re-derive them from settings.
        """
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, credential = auth.partition(" ")
        if scheme.lower() != "bearer" or not credential:
            return None
        if hmac.compare_digest(credential.encode("utf-8"), self._token):
            return ALL_SCOPES
        # OAuth 2.1 access token issued by our own authorization server.
        from src.protocols.token_store import get_store

        record = get_store().get_access_token(credential)
        if record is None:
            return None
        return parse_scopes(record.get("scope", ""))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        if _is_exempt_path(scope.get("path", ""), scope.get("method", "")):
            await self.app(scope, receive, send)
            return

        granted = self._granted_scopes(scope)
        if granted is None:
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

        # Stash the granted scopes for the tool layer (scopes_for_request
        # reads them back in handle_rpc). The middleware is the only layer
        # that knows which credential was presented.
        scope["mcp_scopes"] = granted
        await self.app(scope, receive, send)
