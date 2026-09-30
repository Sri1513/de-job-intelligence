# tests/test_protocol.py
import pytest
from starlette.testclient import TestClient

from src.protocols.app import app
from src.protocols.dispatcher import dispatch_tool_call

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "de-job-intelligence-mcp"


def test_mcp_initialize():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    response = client.post("/rpc", json=payload)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["serverInfo"]["name"] == "de-job-intelligence"
    assert "tools" in result["capabilities"]


def test_mcp_tools_list():
    payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    response = client.post("/rpc", json=payload)
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "prepare_job_tailoring_prompt" in tool_names
    assert "export_tailored_resume" in tool_names


@pytest.mark.asyncio
async def test_dispatcher_unknown_tool():
    result = await dispatch_tool_call("non_existent_tool", {})
    assert "error" in result
    assert "Unknown tool" in result["error"]
