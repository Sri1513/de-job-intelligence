"""Bearer-token proxy in front of the Windows Ollama (Sprint 2, Batch 6).

The Windows Ollama on the tailnet (settings.WINDOWS_LLM_BASE_URL) has
no auth: anyone on the Tailscale network can call it directly. This
proxy puts the MCP server's bearer check in front of the two Ollama
paths the LLM router actually uses:

- GET  /ollama/api/tags             (liveness probe + model list)
- POST /ollama/v1/chat/completions  (OpenAI-compatible chat, streamed)

Everything else under /ollama/* is a 404. The router sends its Windows
calls through here when OLLAMA_PROXY_URL is set (see
src/core/llm_routing.py); the callers (portfolio/market chat, 3B queue)
need no changes -- they already send endpoint.auth_headers.

Auth: BearerAuthMiddleware already requires a valid bearer token on
this route (it is not in the exempt list in auth.py). The handler
additionally requires full access -- all three granular scopes, which
is what the legacy "mcp" scope expands to: the static owner token
has them, a read-only OAuth token (mcp:read) does not -- a scoped
read-only client must not be able to burn LLM calls through the
proxy. Denials are a plain 403 JSON body (this route speaks the
Ollama API shape, not JSON-RPC).

Upstream errors: an unreachable Ollama becomes a 502 with a generic
message (no upstream URL or exception text leaks). A non-2xx upstream
status is passed through with its body. Chat responses stream back
in 8 KiB chunks so long generations are not buffered in memory.

What this does NOT do: closing the direct :11434 port is a firewall
step, not a code step. Until the Windows Firewall drops direct
tailnet traffic to Ollama except from the jobs VM, the proxy is a
gate for our own traffic, not a wall. That step is Omkar's, on the
MSI -- see the PR description. The llm-prod VM's raw :11434 is the
same story: nothing in the repo calls it directly (the VM path goes
through Open WebUI with a key), so it is out of scope here.

No audit row is written for proxy calls: the Batch 3 audit trail
covers MCP tool calls, and this route is plain HTTP, not a tool.
Calls are logged at DEBUG (method, path, status, ms).
"""
import asyncio
import logging
import time
import urllib.error
import urllib.request

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from src.core.config import settings
from src.protocols.scopes import ALL_SCOPES, scopes_for_request

logger = logging.getLogger(__name__)

# The router's browser-like User-Agent (mandatory on the VM path for
# Cloudflare; harmless here, kept for consistency).
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Only these Ollama paths are proxied. The router uses exactly these
# two; anything else is not part of the LLM path and stays closed.
_ALLOWED = {
    ("GET", "/api/tags"),
    ("POST", "/v1/chat/completions"),
}

_TAGS_TIMEOUT_S = 10
_CHAT_TIMEOUT_S = 300
_STREAM_CHUNK_SIZE = 8192


def _upstream_url(path: str) -> str:
    return settings.WINDOWS_LLM_BASE_URL.rstrip("/") + path


def _proxy_path(request: Request) -> str:
    """The Ollama path for this request (/api/tags or
    /v1/chat/completions), derived from the route's path param."""
    return "/" + (request.path_params.get("rest") or "").strip("/")


def _denied(request: Request) -> Response | None:
    """403 unless the caller holds full access (all three granular
    scopes -- what the legacy "mcp" scope expands to). The static
    owner token has them; a scoped OAuth token (e.g. mcp:read) does
    not."""
    if not ALL_SCOPES <= scopes_for_request(request):
        return JSONResponse(
            {
                "error": (
                    "Insufficient scope: the Ollama proxy requires "
                    "the full 'mcp' scope"
                )
            },
            status_code=403,
        )
    return None


def _open_upstream(
    method: str, path: str, body: bytes | None, content_type: str | None
):
    """Blocking upstream call (runs in a thread). Returns the urllib
    response object; raises URLError/HTTPError on failure."""
    headers = {"User-Agent": BROWSER_USER_AGENT}
    if content_type:
        headers["Content-Type"] = content_type
    timeout = _TAGS_TIMEOUT_S if path == "/api/tags" else _CHAT_TIMEOUT_S
    req = urllib.request.Request(
        _upstream_url(path), data=body, headers=headers, method=method
    )
    return urllib.request.urlopen(req, timeout=timeout)


async def ollama_proxy(request: Request) -> Response:
    """Forward one allowlisted Ollama call to the Windows box."""
    path = _proxy_path(request)
    method = request.method
    started = time.monotonic()

    if (method, path) not in _ALLOWED:
        return JSONResponse({"error": "Not found"}, status_code=404)

    denied = _denied(request)
    if denied is not None:
        return denied

    body = await request.body() if method == "POST" else None
    content_type = request.headers.get("content-type")
    try:
        resp = await asyncio.to_thread(
            _open_upstream, method, path, body, content_type
        )
    except urllib.error.HTTPError as exc:
        # Upstream answered with an error status: pass it through.
        payload = exc.read()
        logger.debug(
            "Ollama proxy %s %s -> upstream %s in %.0fms",
            method,
            path,
            exc.code,
            (time.monotonic() - started) * 1000,
        )
        return Response(
            content=payload,
            status_code=exc.code,
            media_type=exc.headers.get_content_type() if exc.headers else None,
        )
    except Exception as exc:  # noqa: BLE001 - upstream down is a 502
        logger.debug("Ollama proxy %s %s upstream unavailable: %s", method, path, exc)
        return JSONResponse(
            {"error": "Ollama upstream unavailable"}, status_code=502
        )

    status = resp.status
    logger.debug(
        "Ollama proxy %s %s -> %s in %.0fms",
        method,
        path,
        status,
        (time.monotonic() - started) * 1000,
    )
    if method == "GET":
        payload = await asyncio.to_thread(resp.read)
        return Response(
            content=payload,
            status_code=status,
            media_type=resp.headers.get_content_type(),
        )

    async def _stream():
        try:
            while True:
                chunk = await asyncio.to_thread(resp.read, _STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            resp.close()

    return StreamingResponse(
        _stream(),
        status_code=status,
        media_type=resp.headers.get_content_type(),
    )
