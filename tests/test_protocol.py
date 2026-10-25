# tests/test_protocol.py
import pytest
from starlette.testclient import TestClient

import src.protocols.dispatcher as dispatcher
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


def test_favicon_is_public():
    # Connector directories and favicon crawlers fetch without credentials.
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert "image/x-icon" in response.headers["content-type"]
    assert response.content[:4] == b"\x00\x00\x01\x00"  # ICO magic
    assert len(response.content) > 100


def test_favicon_ignores_bad_token():
    # The exemption is exact-path: a wrong token still gets the icon.
    response = client.get(
        "/favicon.ico", headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 200


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


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18"])
def test_mcp_initialize_negotiates_requested_version(version):
    # The server echoes a requested version it speaks, so clients that tag
    # the runtime transport by negotiated version (e.g. Gemini's gateway)
    # are not stuck on a hardcoded 2024-11-05.
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": version},
    }
    response = client.post("/rpc", json=payload, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == version


def test_mcp_initialize_falls_back_on_unknown_version():
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2099-01-01"},
    }
    response = client.post("/rpc", json=payload, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == "2024-11-05"


def test_mcp_route_alias_serves_rpc():
    # /mcp is the conventional endpoint path; same handler and auth as /rpc.
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    response = client.post("/mcp", json=payload, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "de-job-intelligence"
    # And it is NOT an auth exemption: no token still gets 401.
    response = client.post("/mcp", json=payload)
    assert response.status_code == 401


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


@pytest.mark.asyncio
async def test_pipeline_tools_run_in_background_with_guard(monkeypatch):
    # Long pipeline tools must return immediately (client proxies drop the
    # stream after ~30-60s) while the work continues behind them, and a
    # second pipeline call while one runs must not start a duplicate.
    import asyncio

    started = asyncio.Event()
    finished = asyncio.Event()

    async def fake_pipeline(arguments):
        started.set()
        await asyncio.sleep(0.05)
        finished.set()
        return {"jobs_staged": 3}

    monkeypatch.setitem(TOOL_HANDLERS, "run_email_pipeline", fake_pipeline)
    monkeypatch.setitem(TOOL_HANDLERS, "run_batch_ingestion", fake_pipeline)
    try:
        first = await dispatch_tool_call("run_email_pipeline", {"limit": 5})
        assert first["status"] == "started"
        assert first["tool"] == "run_email_pipeline"
        # Either pipeline tool is refused while one runs (shared guard:
        # concurrent ingestion/email runs double the LinkedIn rate).
        second = await dispatch_tool_call("run_batch_ingestion", {})
        assert second["status"] == "already_running"
        assert second["tool"] == "run_email_pipeline"
        # The background run actually completes, then the slot frees up.
        await asyncio.wait_for(finished.wait(), timeout=5)
        for _ in range(100):
            if dispatcher._pipeline_task is None:
                break
            await asyncio.sleep(0.01)
        assert dispatcher._pipeline_task is None
        third = await dispatch_tool_call("run_batch_ingestion", {})
        assert third["status"] == "started"
        task = dispatcher._pipeline_task
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
    finally:
        dispatcher._pipeline_task = None
        dispatcher._pipeline_tool = None


@pytest.mark.asyncio
async def test_failed_pipeline_run_frees_slot(monkeypatch):
    # A background failure is logged inside the task, never raised to the
    # caller — and must not poison the slot for the next call.
    import asyncio

    async def boom(arguments):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(TOOL_HANDLERS, "run_email_pipeline", boom)
    try:
        first = await dispatch_tool_call("run_email_pipeline", {})
        assert first["status"] == "started"
        for _ in range(100):
            if dispatcher._pipeline_task is None:
                break
            await asyncio.sleep(0.01)
        assert dispatcher._pipeline_task is None
        second = await dispatch_tool_call("run_email_pipeline", {})
        assert second["status"] == "started"
        task = dispatcher._pipeline_task
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
    finally:
        dispatcher._pipeline_task = None
        dispatcher._pipeline_tool = None


def test_mcp_logger_emits_info():
    # Regression: "Tool executed:" (logger.info) never reached mcp.log.
    # Root cause, in two parts:
    #   1. the logger had no level set -> inherited WARNING from root;
    #   2. the MCP server process configures no handler for this logger,
    #      so even with level fixed, Python's lastResort (WARNING) dropped
    #      INFO records.
    # The logger must therefore own an INFO-capable handler of its own.
    import logging

    from src.protocols.app import logger

    assert logger.getEffectiveLevel() <= logging.INFO
    assert any(
        h.level <= logging.INFO for h in logger.handlers
    ), "mcp logger needs its own INFO handler (process configures no root handler)"
    assert logger.propagate is False  # our handler emits; don't double-emit via root
