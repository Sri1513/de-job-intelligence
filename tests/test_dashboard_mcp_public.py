# tests/test_dashboard_mcp_public.py
"""Public read-only MCP surface (src/protocols/dashboard_mcp.py).

Covers the security boundary, not just the happy path:
- /public/mcp answers WITHOUT a bearer token (exact-path exemption),
  while /rpc still demands one (the exemption did not leak).
- Every data method runs through the reader pool only; the module never
  touches the write pool (source guardrail test).
- No password configured -> 503 fail-closed, never an open endpoint.
- Tool arguments are clamped server-side and passed as bound parameters.
"""
from contextlib import contextmanager
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.protocols import dashboard_mcp
from src.protocols.app import app

OVERVIEW = [
    {
        "job_category": "data_engineering",
        "total_jobs_tracked": 1748,
        "average_fit_score": 49.6,
        "high_match_jobs": 120,
        "unique_hiring_companies": 300,
        "last_ingested_at": "2026-11-01T17:18:26",
    }
]
TOP_SKILLS = [
    {"skill": "python", "job_count": 900},
    {"skill": "sql", "job_count": 850},
]
JOB_ROWS = [
    {
        "job_id": "li-1",
        "title": "Senior Data Engineer",
        "company": "Example Corp",
        "location": "Remote",
        "job_category": "data_engineering",
        "fit_score": "82",
        "saved_at": "2026-11-01T10:00:00",
        "job_url": "https://example.com/jobs/1",
        "description_preview": "Example posting...",
        "match_score": 82,
    }
]


class FakeCursor:
    def __init__(self, calls):
        self._calls = calls
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self._calls.append((sql, params))
        self._sql = sql

    def fetchall(self):
        if "v_dashboard_market_metrics" in self._sql:
            return OVERVIEW
        if "v_dashboard_job_skills" in self._sql and "GROUP BY" in self._sql:
            return TOP_SKILLS
        if "v_dashboard_job_analytics" in self._sql:
            return JOB_ROWS
        return []

    def fetchone(self):
        if "v_dashboard_job_skills" in self._sql:
            return {"job_count": 42}
        return None


class FakeConn:
    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return FakeCursor(self._calls)


class FakePool:
    def __init__(self):
        self.calls = []

    @contextmanager
    def connection(self):
        yield FakeConn(self.calls)


@pytest.fixture()
def public_client(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(dashboard_mcp, "get_reader_pool", lambda: pool)
    monkeypatch.setattr(
        dashboard_mcp.settings, "MCP_DASHBOARD_READER_PASSWORD", "test-password"
    )
    return TestClient(app), pool


def rpc(client, method, params=None, rpc_id=1, headers=None):
    payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}
    return client.post("/public/mcp", json=payload, headers=headers)


def test_public_initialize_advertises_all_three_primitives(public_client):
    client, _ = public_client
    response = rpc(client, "initialize", {"protocolVersion": "2025-06-18"})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"resources": {}, "prompts": {}, "tools": {}}


def test_public_route_ignores_bad_token(public_client):
    client, _ = public_client
    response = rpc(
        client, "ping", headers={"Authorization": "Bearer <redacted>"}
    )
    assert response.status_code == 200
    assert response.json()["result"] == {}


def test_private_rpc_still_requires_token(public_client):
    client, _ = public_client
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert client.post("/rpc", json=payload).status_code == 401


def test_resources_list(public_client):
    client, _ = public_client
    result = rpc(client, "resources/list").json()["result"]
    uris = [r["uri"] for r in result["resources"]]
    assert uris == [
        "jobs://metrics/overview",
        "jobs://metrics/top-skills",
        "profile://frameworks",
    ]


def test_resources_read_overview(public_client):
    client, _ = public_client
    result = rpc(client, "resources/read", {"uri": "jobs://metrics/overview"}).json()[
        "result"
    ]
    contents = result["contents"]
    assert contents[0]["uri"] == "jobs://metrics/overview"
    assert contents[0]["mimeType"] == "application/json"
    import json as _json

    data = _json.loads(contents[0]["text"])
    assert data[0]["job_category"] == "data_engineering"
    assert data[0]["total_jobs_tracked"] == 1748


def test_resources_read_top_skills(public_client):
    client, _ = public_client
    result = rpc(client, "resources/read", {"uri": "jobs://metrics/top-skills"}).json()[
        "result"
    ]
    import json as _json

    data = _json.loads(result["contents"][0]["text"])
    assert data[0] == {"skill": "python", "job_count": 900}


def test_resources_read_unknown_uri(public_client):
    client, _ = public_client
    error = rpc(client, "resources/read", {"uri": "jobs://nope"}).json()["error"]
    assert error["code"] == -32602


def test_prompts_list(public_client):
    client, _ = public_client
    result = rpc(client, "prompts/list").json()["result"]
    names = [p["name"] for p in result["prompts"]]
    assert names == ["market_skill_demand", "explain_fit_score"]


def test_prompts_get_skill_demand_grounds_in_live_data(public_client):
    client, _ = public_client
    result = rpc(
        client, "prompts/get", {"name": "market_skill_demand", "arguments": {"skill": "Python"}}
    ).json()["result"]
    text = result["messages"][0]["content"]["text"]
    assert "python" in text
    assert "42" in text  # the focus-skill count from the fake cursor


def test_prompts_get_fit_score_explainer(public_client):
    client, _ = public_client
    result = rpc(client, "prompts/get", {"name": "explain_fit_score"}).json()["result"]
    text = result["messages"][0]["content"]["text"]
    assert "data_engineering" in text
    assert "rapidfuzz" in text


def test_prompts_get_unknown(public_client):
    client, _ = public_client
    error = rpc(client, "prompts/get", {"name": "nope"}).json()["error"]
    assert error["code"] == -32602


def test_tools_list(public_client):
    client, _ = public_client
    result = rpc(client, "tools/list").json()["result"]
    tools = result["tools"]
    assert [t["name"] for t in tools] == ["query_jobs_by_criteria"]
    assert "min_score" in tools[0]["inputSchema"]["properties"]


def test_tools_call_passes_bound_params_and_clamps(public_client):
    client, pool = public_client
    result = rpc(
        client,
        "tools/call",
        {
            "name": "query_jobs_by_criteria",
            "arguments": {"skill": "  Databricks ", "min_score": 50, "limit": 500},
        },
    ).json()["result"]
    import json as _json

    rows = _json.loads(result["content"][0]["text"])
    assert rows[0]["job_id"] == "li-1"
    # Skill cleaned (trimmed + lowered), limit clamped to 50, all bound.
    sql, params = next(
        (s, p) for s, p in pool.calls if "v_dashboard_job_analytics" in s
    )
    assert params == ("databricks", 50, 50)
    assert "Databricks" not in sql  # never interpolated into SQL text


def test_tools_call_unknown_tool(public_client):
    client, _ = public_client
    error = rpc(client, "tools/call", {"name": "drop_table"}).json()["error"]
    assert error["code"] == -32602


def test_tools_call_rejects_non_numeric_score(public_client):
    client, _ = public_client
    error = rpc(
        client,
        "tools/call",
        {"name": "query_jobs_by_criteria", "arguments": {"min_score": "high"}},
    ).json()["error"]
    assert error["code"] == -32602


def test_unknown_method(public_client):
    client, _ = public_client
    error = rpc(client, "resources/subscribe").json()["error"]
    assert error["code"] == -32601


def test_notifications_initialized_returns_202(public_client):
    client, _ = public_client
    payload = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    assert client.post("/public/mcp", json=payload).status_code == 202


def test_reader_unconfigured_fails_closed(monkeypatch):
    monkeypatch.setattr(
        dashboard_mcp.settings, "MCP_DASHBOARD_READER_PASSWORD", ""
    )
    client = TestClient(app)
    response = rpc(client, "resources/read", {"uri": "jobs://metrics/overview"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == -32002


def test_module_never_touches_write_pool():
    source = Path(dashboard_mcp.__file__).read_text()
    without_reader = source.replace("get_reader_pool", "")
    assert "get_pool(" not in without_reader
    assert "get_db_connection" not in source
