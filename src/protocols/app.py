# src/protocols/app.py
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.protocols.dispatcher import dispatch_tool_call
from src.protocols.schemas import MCP_TOOLS


async def health_check(request: Request) -> JSONResponse:
    """Service health and liveness probe."""
    return JSONResponse({
        "status": "healthy",
        "service": "de-job-intelligence-mcp",
        "version": "1.0.0"
    })

async def handle_rpc(request: Request) -> JSONResponse:
    """
    JSON-RPC 2.0 & MCP protocol endpoint handling initialization,
    tool discovery, and tool execution.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({
            "jsonrpc": "2.0",
            "error": {"code": -32700, "message": "Parse error"},
            "id": None
        }, status_code=400)

    rpc_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params", {})

    # 1. MCP Handshake Initialization
    if method == "initialize":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {
                    "name": "de-job-intelligence",
                    "version": "1.0.0"
                },
                "capabilities": {
                    "tools": {}
                }
            }
        })

    # 2. Ping / Liveness Check
    elif method == "ping":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {}
        })

    # 3. Tool Discovery
    elif method in ("tools/list", "list_tools"):
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "tools": MCP_TOOLS
            }
        })

    # 4. Tool Execution
    elif method in ("tools/call", "call_tool"):
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if not tool_name:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32602, "message": "Missing 'name' in params"}
            })

        execution_result = await dispatch_tool_call(tool_name, arguments)
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": str(execution_result)
                    }
                ]
            }
        })

    # Inside src/protocols/app.py -> handle_rpc:
    elif method in ("tools/call", "call_tool"):
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if not tool_name:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32602, "message": "Missing 'name' in params"}
            })

        try:
            execution_result = await dispatch_tool_call(tool_name, arguments)
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": str(execution_result)
                        }
                    ]
                }
            })
        except Exception as exc:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {
                    "code": -32603,
                    "message": "Internal error during tool execution",
                    "data": str(exc)
                }
            }, status_code=200)

    # 5. Unsupported RPC Method
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": f"Method '{method}' not found"}
    })

# Starlette Application Routes
routes = [
    Route("/health", health_check, methods=["GET"]),
    Route("/", handle_rpc, methods=["POST"]),
    Route("/rpc", handle_rpc, methods=["POST"]),
]

app = Starlette(debug=False, routes=routes)
