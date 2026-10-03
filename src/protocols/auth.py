# src/protocols/auth.py
"""
Authentication for the MCP server.

Remote MCP servers must not run open: the JSON-RPC surface exposes tools that
queue job applications, run browser automation, and spend LLM budget. This
module implements the industry-standard pattern for securing a remote MCP
endpoint:

    Client --> BearerAuthMiddleware --> Starlette app --> tool registry

* RFC 6750 bearer tokens, compared in constant time (hmac.compare_digest).
* Fail-closed: the server refuses to start when no token is configured.
* GET /health stays unauthenticated so load-balancer / uptime probes keep working.
"""
import hmac
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("de-job-intelligence.auth")

# Paths that never require authentication (liveness probes).
EXEMPT_PATHS = frozenset({"/health"})


class BearerAuthMiddleware:
    """Pure-ASGI bearer-token gate for the MCP JSON-RPC endpoint."""

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
        return hmac.compare_digest(credential.encode("utf-8"), self._token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        if scope.get("path") in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        if not self._is_authorized(scope):
            logger.warning("Rejected unauthenticated request to %s", scope.get("path"))
            response = JSONResponse(
                {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Unauthorized: valid bearer token required"}, "id": None},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
