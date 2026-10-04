# src/protocols/app.py
"""
MCP server transport layer (Starlette/ASGI).

Implements the MCP JSON-RPC 2.0 surface (protocolVersion 2024-11-05):
initialize handshake, notifications/initialized, tools/list, tools/call.
Authentication is enforced by BearerAuthMiddleware (see auth.py); this module
only speaks protocol. Tool routing lives in dispatcher.py's TOOL_HANDLERS
registry; business logic lives in engine/, workers/, and synthesis/.
"""
import json
import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from src.core.config import settings
from src.protocols.auth import BearerAuthMiddleware
from src.protocols.dispatcher import dispatch_tool_call
from src.protocols.oauth import oauth_routes
from src.protocols.schemas import MCP_TOOLS

logger = logging.getLogger("de-job-intelligence.mcp")


async def health_check(request: Request) -> JSONResponse:
    """Service health and liveness probe (unauthenticated by design)."""
    return JSONResponse(
        {"status": "healthy", "service": "de-job-intelligence-mcp", "version": "1.0.0"}
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
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "protocolVersion": "2024-11-05",
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

        if not tool_name:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32602, "message": "Missing 'name' in params"},
                }
            )

        try:
            execution_result = await dispatch_tool_call(tool_name, arguments)
            logger.info("Tool executed: %s", tool_name)
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
        except Exception as exc:
            logger.exception("Tool execution failed: %s", tool_name)
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {
                        "code": -32603,
                        "message": "Internal error during tool execution",
                        "data": str(exc),
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
    Route("/", handle_rpc, methods=["POST"]),
    Route("/rpc", handle_rpc, methods=["POST"]),
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
    return app


# Uvicorn entrypoint: `uvicorn src.protocols.app:app`
app = create_app()
