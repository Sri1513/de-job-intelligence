# src/protocols/guard.py
"""HTTP guards for the MCP server (Sprint 2, Batch 4).

One Starlette (raw ASGI) middleware, outermost in the stack — it runs
before auth and before the router, so an oversized body, a foreign
browser origin, or a flooding client never reaches authentication or
any handler. Three guards live here; the 30s tool timeout lives in
handle_rpc instead, because only there is the tool name known (the
apply worker and the backfill retries are exempt — see app.py).

SCOPE — what each guard applies to, and why:
- SIZE CAP: every request, any path. Checked on the Content-Length
  header only; the body is never read here. A chunked body with no
  Content-Length slips past this check (documented limitation — MCP
  clients send Content-Length; uvicorn has its own connection limits
  as a backstop).
- ORIGIN: only POSTs to the private JSON-RPC paths (/, /rpc, /mcp).
  NOT applied to /oauth/* on purpose: the approval form POSTs from the
  server's own page, and the authorize GET arrives from the client's
  browser — Origin-checking the OAuth dance would break it. Only
  enforced when an Origin header is PRESENT: only browsers send one,
  so server-side clients (Gemini app, scripts, the Inspector's proxy,
  the dashboard backend) pass through untouched. A present Origin must
  be loopback (local Inspector / local dev) or in MCP_ALLOWED_ORIGINS.
- RATE LIMIT: only POSTs to the private JSON-RPC paths. Per credential
  (SHA-256 of the bearer token — the raw token is never stored or
  logged), sliding 60s window; a request with no credential is
  bucketed by client IP, so unauthenticated floods are throttled too.
  Buckets are in-memory on the middleware instance: one uvicorn worker
  means one budget per credential. If the server ever runs multiple
  workers, each keeps its own budget (documented, accepted).

Every rejection carries a fresh correlation_id (same idea as Batch 3's
audit trail) so the owner can find the rejection in the logs. The
rejection shape is a JSON-RPC error object — this server speaks
JSON-RPC on every POST path, so one shape fits all.
"""

import hashlib
import logging
import math
import time
from collections import deque
from urllib.parse import urlparse

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.core.config import settings
from src.protocols.audit import new_correlation_id

logger = logging.getLogger("de-job-intelligence.guard")

_RPC_PATHS = frozenset({"/", "/rpc", "/mcp"})
_RATE_WINDOW_SECONDS = 60.0

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


def _is_loopback_origin(origin: str) -> bool:
    """True for http(s)://localhost[:port], 127.0.0.1, [::1]."""
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return parsed.hostname in _LOOPBACK_HOSTS


def _allowed_origins() -> set[str]:
    """MCP_ALLOWED_ORIGINS, comma-separated, lowercased, no empties."""
    return {
        item.strip().lower() for item in settings.MCP_ALLOWED_ORIGINS.split(",") if item.strip()
    }


def _origin_allowed(origin: str) -> bool:
    try:
        if _is_loopback_origin(origin):
            return True
        # Compare scheme://host[:port] only — a path or query on the Origin
        # header is malformed input, not an allowlisted origin.
        parsed = urlparse(origin)
        candidate = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    except ValueError:
        return False  # Unparseable Origin (e.g. broken IPv6) is not allowed.
    return candidate in _allowed_origins()


def _bucket_key(scope: Scope) -> str:
    """Per-credential bucket: SHA-256 of the bearer token, else client IP.

    The raw token is never stored or logged — only its hash prefixes
    the bucket. An absent/invalid credential still gets throttled, by IP.
    """
    headers = dict(scope.get("headers", []))
    auth = headers.get(b"authorization", b"").decode("latin-1")
    scheme, _, credential = auth.partition(" ")
    if scheme.lower() == "bearer" and credential:
        digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]
        return f"token:{digest}"
    client = scope.get("client")
    host = client[0] if client else "unknown"
    return f"ip:{host}"


def _rejection(status_code: int, code: int, message: str, extra: dict | None = None):
    """A JSON-RPC-shaped rejection with a fresh correlation_id."""
    correlation_id = new_correlation_id()
    data = {"correlation_id": correlation_id}
    if extra:
        data.update(extra)
    response = JSONResponse(
        {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message, "data": data},
            "id": None,
        },
        status_code=status_code,
    )
    return response, correlation_id


class MCPGuardMiddleware:
    """Size cap + Origin validation + per-credential rate limiting."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # key -> deque of monotonic timestamps inside the window.
        # Instance state (not module state) so each app — and each test's
        # fresh TestClient — starts with empty budgets.
        self._buckets: dict[str, deque[float]] = {}

    def _check_size(self, scope: Scope) -> tuple | None:
        """(response, correlation_id) when the body is too large, else None."""
        headers = dict(scope.get("headers", []))
        raw_length = headers.get(b"content-length")
        if raw_length is None:
            return None
        try:
            length = int(raw_length.decode("latin-1"))
        except ValueError:
            return None
        max_bytes = settings.MCP_MAX_REQUEST_BYTES
        if length <= max_bytes:
            return None
        response, correlation_id = _rejection(
            413,
            -32000,
            "Request too large",
            {"max_bytes": max_bytes, "content_length": length},
        )
        logger.warning(
            "guard: rejected oversized request to %s (%s bytes > %s) (correlation_id=%s)",
            scope.get("path"),
            length,
            max_bytes,
            correlation_id,
        )
        return response, correlation_id

    def _check_origin(self, scope: Scope) -> tuple | None:
        """(response, correlation_id) for a foreign Origin, else None."""
        headers = dict(scope.get("headers", []))
        origin = headers.get(b"origin", b"").decode("latin-1")
        if not origin:
            return None  # No Origin: not a browser — pass through.
        if _origin_allowed(origin):
            return None
        response, correlation_id = _rejection(403, -32001, "Origin not allowed", {"origin": origin})
        logger.warning(
            "guard: rejected foreign origin %s on %s (correlation_id=%s)",
            origin,
            scope.get("path"),
            correlation_id,
        )
        return response, correlation_id

    def _check_rate(self, scope: Scope) -> tuple | None:
        """(response, correlation_id) when the bucket is empty, else None."""
        limit = settings.MCP_RATE_LIMIT_PER_MINUTE
        if limit <= 0:
            return None  # 0 disables rate limiting.
        key = _bucket_key(scope)
        now = time.monotonic()
        bucket = self._buckets.setdefault(key, deque())
        while bucket and bucket[0] <= now - _RATE_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) < limit:
            bucket.append(now)
            return None
        retry_after = max(1, math.ceil(bucket[0] + _RATE_WINDOW_SECONDS - now))
        response, correlation_id = _rejection(
            429,
            -32000,
            "Rate limit exceeded",
            {"retry_after_seconds": retry_after},
        )
        response.headers["Retry-After"] = str(retry_after)
        logger.warning(
            "guard: rate limit exceeded for %s on %s (correlation_id=%s)",
            key,
            scope.get("path"),
            correlation_id,
        )
        return response, correlation_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Size cap first: it applies to every request, before any body
        # is read anywhere downstream.
        size_result = self._check_size(scope)
        if size_result is not None:
            response, _ = size_result
            await response(scope, receive, send)
            return

        # Origin + rate apply only to the private JSON-RPC surface.
        # /health, /oauth/*, /.well-known/*, /public/mcp and GET / keep
        # their own auth posture (or none) and are untouched here.
        if scope.get("method") == "POST" and scope.get("path") in _RPC_PATHS:
            origin_result = self._check_origin(scope)
            if origin_result is not None:
                response, _ = origin_result
                await response(scope, receive, send)
                return
            rate_result = self._check_rate(scope)
            if rate_result is not None:
                response, _ = rate_result
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)
