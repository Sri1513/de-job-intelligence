# src/protocols/app.py
"""
MCP server transport layer (Starlette/ASGI).

Implements the MCP JSON-RPC 2.0 surface (protocol versions 2024-11-05,
2025-03-26, 2025-06-18): initialize handshake, notifications/initialized,
tools/list, tools/call.
Authentication is enforced by BearerAuthMiddleware (see auth.py); this module
only speaks protocol. Tool routing lives in dispatcher.py's TOOL_HANDLERS
registry; business logic lives in engine/, workers/, and synthesis/.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from src.core.config import settings
from src.protocols.audit import (
    arecord_audit_row,
    client_id_for_request,
    log_tool_call,
    new_correlation_id,
    required_scope_for_tool,
)
from src.protocols.auth import BearerAuthMiddleware
from src.protocols.dashboard_mcp import PUBLIC_MCP_PATH, handle_public_rpc
from src.protocols.dispatcher import dispatch_tool_call
from src.protocols.guard import MCPGuardMiddleware
from src.protocols.oauth import oauth_routes
from src.protocols.schemas import MCP_TOOLS
from src.protocols.scopes import InsufficientScopeError, scopes_for_request
from src.protocols.validation import ToolArgumentError

logger = logging.getLogger("de-job-intelligence.mcp")
# The MCP server process never configures a handler for this logger (no
# basicConfig here; uvicorn only configures its own loggers). Without one,
# Python's lastResort handler (WARNING) silently drops logger.info() calls
# even when the level allows them -- which is why "Tool executed:" never
# reached mcp.log. Attach a dedicated stderr handler (captured by tee into
# mcp.log) and stop propagation so records can't double-emit via root.
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setLevel(logging.INFO)
_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
)
if not logger.handlers:
    logger.addHandler(_handler)
logger.propagate = False

# Same black-box problem one level down: run_batch_ingestion's per-source
# counts ("Total combined raw jobs fetched", "Extracted N jobs from X") are
# logger.info() on these loggers, which also have no handler in this process
# -- so lastResort (WARNING) dropped every one of them and a run looked like
# it "just executed". Give each of them the same stderr handler (captured
# by tee into mcp.log) with propagate off, so nothing double-emits.
for _ingestion_logger_name in (
    "src.engine.ingestion",
    "src.engine.scraper",
    "de-job-intelligence.dice_client",
    "de-job-intelligence.dispatcher",
):
    _ingestion_logger = logging.getLogger(_ingestion_logger_name)
    _ingestion_logger.setLevel(logging.INFO)
    if not _ingestion_logger.handlers:
        _ingestion_logger.addHandler(_handler)
    _ingestion_logger.propagate = False

# Protocol versions this server speaks. The tools wire format (initialize,
# tools/list, tools/call, notifications) is identical across these versions,
# so negotiation is safe: echo the client's requested version when it is one
# of these, otherwise fall back to 2024-11-05. Some client gateways tag the
# runtime transport by the negotiated version, so answering a hardcoded
# 2024-11-05 to a client that asked for 2025-03-26 can break tool execution
# even though registration (initialize + tools/list) succeeds.
SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {"2024-11-05", "2025-03-26", "2025-06-18"}
)
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

# Sprint 2, Batch 4: tools exempt from the 30s call timeout — slow by
# nature. run_application_worker drives browser automation (minutes);
# retry_job_evaluations runs backfill batches (LLM calls per job). Every
# other tool must answer inside MCP_TOOL_TIMEOUT_SECONDS.
_TIMEOUT_EXEMPT_TOOLS = frozenset(
    {"run_application_worker", "retry_job_evaluations"}
)


async def _record_tool_call(
    *,
    request: Request,
    correlation_id: str,
    tool_name: str,
    scope: str,
    started: float,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    request_size_bytes: int | None = None,
) -> None:
    """One JSON log line + one audit row for a tools/call outcome.

    Sprint 2, Batch 3: the same correlation_id ties the client's error
    response to the server-side record. The JSON line always emits; the
    audit row is fail-open inside record_audit_row. ``error_message``
    must already be client-safe (the message the client saw) — never
    pass exception text here.
    """
    duration_ms = int((time.monotonic() - started) * 1000)
    client_id = client_id_for_request(request)
    log_tool_call(
        correlation_id=correlation_id,
        tool_name=tool_name,
        scope=scope,
        client_id=client_id,
        status=status,
        duration_ms=duration_ms,
        error_code=error_code,
        error_message=error_message,
        request_size_bytes=request_size_bytes,
    )
    await arecord_audit_row(
        correlation_id=correlation_id,
        tool_name=tool_name,
        scope=scope,
        client_id=client_id,
        duration_ms=duration_ms,
        status=status,
        error_code=error_code,
        error_message=error_message,
        request_size_bytes=request_size_bytes,
    )


async def health_check(request: Request) -> JSONResponse:
    """Service health and liveness probe (unauthenticated by design)."""
    return JSONResponse(
        {"status": "healthy", "service": "de-job-intelligence-mcp", "version": "1.0.0"}
    )


_FAVICON_PATH = Path(__file__).resolve().parent / "assets" / "favicon.ico"


async def favicon(request: Request) -> FileResponse:
    """Connector icon (unauthenticated by design: favicon crawlers only).

    The Agni Bolt mark, served as a real .ico so connector directories
    (e.g. Gemini's) can render it without an authenticated fetch.
    """
    return FileResponse(_FAVICON_PATH, media_type="image/x-icon")


async def landing_page(request: Request) -> HTMLResponse:
    """Public landing page (unauthenticated by design).

    Gives favicon crawlers an HTML page to discover the server icon from:
    without it, GET / answers 401 (the bearer-gated JSON-RPC surface), so
    no crawler ever sees a favicon link even though /favicon.ico itself
    is public. Content is public-safe only: server name, public endpoint
    path, and the public dashboard link. Nothing internal is named here.
    """
    return HTMLResponse(
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>de-job-intelligence MCP server</title>\n"
        '<meta name="description" content="Public Model Context Protocol server for de-job-intelligence job-market data.">\n'
        '<link rel="icon" type="image/x-icon" href="/favicon.ico">\n'
        "</head>\n"
        "<body>\n"
        "<h1>de-job-intelligence-public MCP server</h1>\n"
        "<p>Public MCP endpoint: POST /public/mcp</p>\n"
        '<p><a href="https://jobs.sriomkar.com">Job dashboard</a></p>\n'
        "</body>\n"
        "</html>\n"
    )


async def handle_rpc(request: Request) -> JSONResponse | Response:
    """
    JSON-RPC 2.0 & MCP protocol endpoint handling initialization,
    tool discovery, and tool execution.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None},
            status_code=400,
        )

    rpc_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params", {})

    # 1. MCP Handshake Initialization
    if method == "initialize":
        requested_version = params.get("protocolVersion")
        negotiated_version = (
            requested_version
            if requested_version in SUPPORTED_PROTOCOL_VERSIONS
            else DEFAULT_PROTOCOL_VERSION
        )
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "protocolVersion": negotiated_version,
                    "serverInfo": {"name": "de-job-intelligence", "version": "1.0.0"},
                    "capabilities": {"tools": {}},
                },
            }
        )

    # 1b. Initialized notification (JSON-RPC notification: no id, no response body).
    # Per the MCP spec the server answers 202 Accepted.
    if method == "notifications/initialized":
        return Response(status_code=202)

    # 2. Ping / Liveness Check
    elif method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {}})

    # 3. Tool Discovery
    elif method in ("tools/list", "list_tools"):
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": MCP_TOOLS}})

    # 4. Tool Execution (Protected with Try/Except)
    elif method in ("tools/call", "call_tool"):
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        # Sprint 2, Batch 3: one UUID per call, generated at the boundary.
        # It goes back to the client on every error response (never the
        # raw exception text) and ties the JSON log line to the audit row.
        correlation_id = new_correlation_id()
        started = time.monotonic()
        # request.json() above already read + cached the body, so this is
        # cheap; the size goes in the log line and the audit row.
        body = await request.body()
        request_size_bytes = len(body) if body else None
        scope = required_scope_for_tool(tool_name)

        if not tool_name:
            await _record_tool_call(
                request=request,
                correlation_id=correlation_id,
                tool_name="unknown",
                scope=scope,
                started=started,
                status="error",
                error_code="-32602",
                error_message="Missing 'name' in params",
                request_size_bytes=request_size_bytes,
            )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {
                        "code": -32602,
                        "message": "Missing 'name' in params",
                        "data": {"correlation_id": correlation_id},
                    },
                }
            )

        try:
            # Sprint 2, Batch 4: bound how long a tool call may hold the
            # request. Exempt are the two synchronous long-runners — the
            # apply worker (browser automation, minutes) and the backfill
            # retries — plus a non-positive timeout disables the guard.
            # The ingestion / email tools return immediately ("started"),
            # so the timeout never bites them. Best-effort for handlers
            # that block the event loop synchronously: wait_for can only
            # cancel at an await point; a to_thread handler keeps running
            # in its thread after the client gets the timeout.
            timeout = settings.MCP_TOOL_TIMEOUT_SECONDS
            if tool_name in _TIMEOUT_EXEMPT_TOOLS or timeout <= 0:
                execution_result = await dispatch_tool_call(
                    tool_name, arguments, scopes=scopes_for_request(request)
                )
            else:
                try:
                    execution_result = await asyncio.wait_for(
                        dispatch_tool_call(
                            tool_name, arguments, scopes=scopes_for_request(request)
                        ),
                        timeout=timeout,
                    )
                except TimeoutError:
                    await _record_tool_call(
                        request=request,
                        correlation_id=correlation_id,
                        tool_name=tool_name,
                        scope=scope,
                        started=started,
                        status="error",
                        error_code="-32000",
                        error_message=f"Tool call timed out after {timeout:g}s",
                        request_size_bytes=request_size_bytes,
                    )
                    return JSONResponse(
                        {
                            "jsonrpc": "2.0",
                            "id": rpc_id,
                            "error": {
                                "code": -32000,
                                "message": f"Tool call timed out after {timeout:g}s",
                                "data": {"correlation_id": correlation_id},
                            },
                        },
                        status_code=200,
                    )
            # Unknown tools (and some handlers) answer with a result dict
            # carrying an "error" key instead of raising — already
            # client-safe text, but it is still a failed call for the
            # audit trail.
            if isinstance(execution_result, dict) and "error" in execution_result:
                await _record_tool_call(
                    request=request,
                    correlation_id=correlation_id,
                    tool_name=tool_name,
                    scope=scope,
                    started=started,
                    status="error",
                    error_message=str(execution_result["error"]),
                    request_size_bytes=request_size_bytes,
                )
            else:
                logger.info("Tool executed: %s", tool_name)
                await _record_tool_call(
                    request=request,
                    correlation_id=correlation_id,
                    tool_name=tool_name,
                    scope=scope,
                    started=started,
                    status="ok",
                    request_size_bytes=request_size_bytes,
                )
            # Serialize as JSON (not str()) so agents receive parseable content;
            # default=str keeps datetimes and other scalars safe.
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(execution_result, default=str),
                            }
                        ]
                    },
                }
            )
        except InsufficientScopeError as exc:
            # Sprint 2, Batch 2: the token is valid but lacks the tool's
            # scope. -32001 (same code the middleware uses for auth
            # failures) with the required scope named, so the client knows
            # what to request. HTTP 200 like the other tool-call errors.
            # str(exc) is client-safe by construction (tool + scope names).
            message = str(exc)
            await _record_tool_call(
                request=request,
                correlation_id=correlation_id,
                tool_name=tool_name,
                scope=scope,
                started=started,
                status="error",
                error_code="-32001",
                error_message=message,
                request_size_bytes=request_size_bytes,
            )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {
                        "code": -32001,
                        "message": message,
                        "data": {
                            "tool": exc.tool_name,
                            "required_scope": exc.required_scope,
                            "correlation_id": correlation_id,
                        },
                    },
                },
                status_code=200,
            )
        except ToolArgumentError as exc:
            # Sprint 2, Batch 1: bad arguments are the client's fault, not
            # an internal error. Answer -32602 (Invalid params) with
            # field-level errors only — no exception text, no traceback.
            await _record_tool_call(
                request=request,
                correlation_id=correlation_id,
                tool_name=tool_name,
                scope=scope,
                started=started,
                status="error",
                error_code="-32602",
                error_message="Invalid tool arguments",
                request_size_bytes=request_size_bytes,
            )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {
                        "code": -32602,
                        "message": "Invalid tool arguments",
                        "data": {
                            "tool": exc.tool_name,
                            "errors": exc.errors,
                            "correlation_id": correlation_id,
                        },
                    },
                },
                status_code=200,
            )
        except Exception:
            # Sprint 2, Batch 3: the client gets the correlation_id and
            # nothing else — no str(exc), no traceback. The full traceback
            # stays server-side in mcp.log, tagged with the same id so the
            # owner can find the call it belongs to.
            logger.exception(
                "Tool execution failed: %s (correlation_id=%s)",
                tool_name,
                correlation_id,
            )
            await _record_tool_call(
                request=request,
                correlation_id=correlation_id,
                tool_name=tool_name,
                scope=scope,
                started=started,
                status="error",
                error_code="-32603",
                error_message="Internal error during tool execution",
                request_size_bytes=request_size_bytes,
            )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {
                        "code": -32603,
                        "message": "Internal error during tool execution",
                        "data": {"correlation_id": correlation_id},
                    },
                },
                status_code=200,
            )

    # 5. Unsupported RPC Method
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Method '{method}' not found"},
        }
    )


# Starlette Application Routes
routes = [
    Route("/health", health_check, methods=["GET"]),
    Route("/favicon.ico", favicon, methods=["GET"]),
    Route("/", landing_page, methods=["GET"]),
    Route("/", handle_rpc, methods=["POST"]),
    Route("/rpc", handle_rpc, methods=["POST"]),
    # /mcp is the conventional Streamable HTTP endpoint path (e.g. Dice's
    # public MCP server lives at /mcp). Same handler and same auth as /rpc;
    # BearerAuthMiddleware still requires a token or OAuth token here.
    Route("/mcp", handle_rpc, methods=["POST"]),
    # Public read-only MCP surface (resources, prompts, one tool). Exempt
    # from bearer auth by exact path in auth.py -- safe only because its
    # handler queries exclusively through the mcp_dashboard_reader pool.
    Route(PUBLIC_MCP_PATH, handle_public_rpc, methods=["POST"]),
    *oauth_routes,
]


def create_app(auth_token: str | None = None) -> Starlette:
    """
    Application factory. Resolves the bearer token from the explicit argument
    or MCP_AUTH_TOKEN, then wraps the app in BearerAuthMiddleware.

    Fails closed: without a token the process refuses to start rather than
    serving an open MCP endpoint.
    """
    token = auth_token or settings.MCP_AUTH_TOKEN
    if not token:
        raise RuntimeError(
            "MCP_AUTH_TOKEN is not set. Refusing to start an unauthenticated "
            "MCP server: set MCP_AUTH_TOKEN in the environment (or .env) to a "
            "strong random value, e.g. `openssl rand -hex 32`."
        )
    app = Starlette(debug=False, routes=routes)
    app.add_middleware(BearerAuthMiddleware, token=token)
    # Sprint 2, Batch 4: the guard is added AFTER auth, which makes it
    # OUTERMOST (Starlette wraps last-added first) — oversized bodies,
    # foreign browser origins, and flooding clients are rejected before
    # auth or any handler ever sees them.
    app.add_middleware(MCPGuardMiddleware)
    return app


# Uvicorn entrypoint: `uvicorn src.protocols.app:app`
app = create_app()
