# tests/test_protocol.py
import pytest
from starlette.testclient import TestClient

from src.protocols.app import app
from src.protocols.dispatcher import TOOL_HANDLERS, dispatch_tool_call
from src.protocols.schemas import MCP_TOOLS

# Must match tests/conftest.py
HEADERS = {"Authorization": "Bearer test-token"}

client = TestClient(app)


def test_health_endpoint():
    # /health is unauthenticated by design (load-balancer probes).
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "de-job-intelligence-mcp"


def test_rpc_rejects_missing_token():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    response = client.post("/rpc", json=payload)
    assert response.status_code == 401


def test_rpc_rejects_wrong_token():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    response = client.post(
        "/rpc", json=payload, headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401


def test_mcp_initialize():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    response = client.post("/rpc", json=payload, headers=HEADERS)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["serverInfo"]["name"] == "de-job-intelligence"
    assert "tools" in result["capabilities"]


def test_mcp_notifications_initialized():
    # JSON-RPC notification: no id; server answers 202 with no body.
    payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    response = client.post("/rpc", json=payload, headers=HEADERS)
    assert response.status_code == 202


def test_mcp_tools_list():
    payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    response = client.post("/rpc", json=payload, headers=HEADERS)
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "prepare_job_tailoring_prompt" in tool_names
    assert "export_tailored_resume" in tool_names


def test_tool_registry_covers_manifest():
    # Every tool advertised in schemas.py must have a registered handler.
    for tool in MCP_TOOLS:
        assert tool["name"] in TOOL_HANDLERS, f"missing handler for {tool['name']}"


@pytest.mark.asyncio
async def test_dispatcher_unknown_tool():
    result = await dispatch_tool_call("non_existent_tool", {})
    assert "error" in result
    assert "Unknown tool" in result["error"]
