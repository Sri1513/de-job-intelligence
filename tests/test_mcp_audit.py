"""Tests for Sprint 2, Batch 3: error sanitization + structured JSON logs
+ audit rows, all sharing one correlation_id.

The contract under test:
- Every tools/call gets one UUID correlation_id, generated at the HTTP
  boundary. Error responses carry ONLY that id (no exception text, no
  traceback); the same id ties the JSON log line to the audit row.
- -32603 data is exactly {"correlation_id": ...}; -32001/-32602 data keep
  their existing fields and gain the correlation_id.
- One JSON log line per call on de-job-intelligence.mcp.audit, one row
  in scout.mcp_audit_log (fail-open: a dead DB never breaks the call).
- The audit row records the tool's required scope and the OAuth
  client_id (None for the static owner token).
"""

import base64
import hashlib
import io
import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.protocols import audit as audit_module
from src.protocols import token_store as ts_module
from src.protocols.app import create_app
from src.protocols.audit import (
    build_log_record,
    client_id_for_request,
    log_tool_call,
    new_correlation_id,
    record_audit_row,
    required_scope_for_tool,
)
from src.protocols.dispatcher import TOOL_HANDLERS

OWNER_TOKEN = "test-owner-token"
STATIC_BEARER = "test-bearer"

VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)
REDIRECT_URI = "https://gemini.example.com/oauth/callback"

LEAK_MARKER = "LEAK_MARKER_db_password_xyz"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._calls.append((sql, params))


class _FakeConn:
    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._calls)


class _FakePool:
    """Captures every execute; never touches a real database."""

    def __init__(self):
        self.calls = []

    def connection(self, timeout=None):
        return _FakeConn(self.calls)


def _audit_inserts(pool):
    return [c for c in pool.calls if "INSERT INTO scout.mcp_audit_log" in c[0]]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ts_module, "_store", None)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "MCP_AUTH_TOKEN", OWNER_TOKEN)
    monkeypatch.setattr(settings, "MCP_PUBLIC_URL", "https://mcp.example.com")
    return TestClient(create_app(auth_token=STATIC_BEARER))


@pytest.fixture
def fake_pool(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(audit_module, "get_pool", lambda: pool)
    return pool


def _rpc(client, token, tool, arguments, rpc_id=1):
    return client.post(
        "/rpc",
        json={
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def _oauth_token_with_client(client, scope):
    """Register + approve + exchange; returns (access_token, client_id)."""
    reg = client.post(
        "/oauth/register",
        json={"redirect_uris": [REDIRECT_URI], "client_name": "Audit Test Client"},
    )
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]
    appr = client.post(
        "/oauth/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": "xyz",
            "scope": scope,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "owner_token": OWNER_TOKEN,
            "decision": "approve",
        },
        follow_redirects=False,
    )
    assert appr.status_code == 303
    from urllib.parse import parse_qs, urlparse

    code = parse_qs(urlparse(appr.headers["location"]).query)["code"][0]
    tok = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": VERIFIER,
        },
    )
    assert tok.status_code == 200
    return tok.json()["access_token"], client_id


def _capture_audit_log_lines():
    """Attach a StringIO handler to the audit logger; returns (stream, handler)."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    audit_module._audit_logger.addHandler(handler)
    return stream, handler


def _parsed_log_lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Unit: correlation ids, scopes, log records
# ---------------------------------------------------------------------------


def test_new_correlation_id_is_uuid_and_unique():
    first = new_correlation_id()
    second = new_correlation_id()
    assert uuid.UUID(first)  # parses as a UUID
    assert uuid.UUID(second)
    assert first != second


def test_required_scope_for_tool():
    assert required_scope_for_tool("search_saved_jobs") == "mcp:read"
    assert required_scope_for_tool("run_batch_ingestion") == "mcp:ingest"
    assert required_scope_for_tool("queue_job_for_application") == "mcp:apply"
    assert required_scope_for_tool("no_such_tool") == "mcp"
    assert required_scope_for_tool(None) == "mcp"


def test_client_id_for_request():
    assert client_id_for_request(SimpleNamespace(scope={"mcp_client_id": "mcp-abc"})) == "mcp-abc"
    assert client_id_for_request(SimpleNamespace(scope={})) is None
    assert client_id_for_request(SimpleNamespace(scope={"mcp_client_id": None})) is None


def test_build_log_record_shape():
    record = build_log_record(
        correlation_id="corr-1",
        tool_name="search_saved_jobs",
        scope="mcp:read",
        client_id="mcp-abc",
        status="error",
        duration_ms=42,
        error_code="-32603",
        error_message="Internal error during tool execution",
        request_size_bytes=128,
    )
    assert record["event"] == "tool_call"
    assert record["level"] == "ERROR"
    assert record["correlation_id"] == "corr-1"
    assert record["tool_name"] == "search_saved_jobs"
    assert record["scope"] == "mcp:read"
    assert record["client_id"] == "mcp-abc"
    assert record["duration_ms"] == 42
    assert record["error_code"] == "-32603"
    assert record["request_size_bytes"] == 128
    json.dumps(record)  # serializable
    ok_record = build_log_record(
        correlation_id="corr-2",
        tool_name="search_saved_jobs",
        scope="mcp:read",
        client_id=None,
        status="ok",
        duration_ms=3,
    )
    assert ok_record["level"] == "INFO"
    assert ok_record["error_code"] is None


def test_log_tool_call_emits_one_json_line():
    stream, handler = _capture_audit_log_lines()
    try:
        log_tool_call(
            correlation_id="corr-9",
            tool_name="get_job_details",
            scope="mcp:read",
            client_id=None,
            status="ok",
            duration_ms=5,
            request_size_bytes=64,
        )
    finally:
        audit_module._audit_logger.removeHandler(handler)
    lines = _parsed_log_lines(stream)
    assert len(lines) == 1
    assert lines[0]["correlation_id"] == "corr-9"
    assert lines[0]["event"] == "tool_call"
    assert lines[0]["status"] == "ok"


# ---------------------------------------------------------------------------
# Unit: audit row writer (fail-open, kill switch)
# ---------------------------------------------------------------------------


def test_record_audit_row_writes_expected_columns(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(audit_module, "get_pool", lambda: pool)
    record_audit_row(
        correlation_id="corr-1",
        tool_name="search_saved_jobs",
        scope="mcp:read",
        client_id="mcp-abc",
        duration_ms=42,
        status="error",
        error_code="-32603",
        error_message="Internal error during tool execution",
        request_size_bytes=128,
    )
    inserts = _audit_inserts(pool)
    assert len(inserts) == 1
    _, params = inserts[0]
    assert params == (
        "corr-1",
        "mcp-abc",
        "search_saved_jobs",
        "mcp:read",
        42,
        "error",
        "-32603",
        "Internal error during tool execution",
        128,
    )


def test_record_audit_row_never_raises_when_db_down(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(audit_module, "get_pool", _boom)
    record_audit_row(correlation_id="corr-1", tool_name="search_saved_jobs", scope="mcp:read")


def test_record_audit_row_kill_switch_skips_write(monkeypatch):
    monkeypatch.setenv("MCP_AUDIT_ENABLED", "false")

    def _boom():
        raise AssertionError("pool must not be touched while disabled")

    monkeypatch.setattr(audit_module, "get_pool", _boom)
    record_audit_row(correlation_id="corr-1", tool_name="search_saved_jobs", scope="mcp:read")


# ---------------------------------------------------------------------------
# End-to-end: sanitized errors, shared correlation_id, audit rows
# ---------------------------------------------------------------------------


def test_internal_error_returns_only_correlation_id(client, fake_pool, monkeypatch):
    async def _boom(arguments):
        raise ValueError(f"connection failed: host=db password={LEAK_MARKER}")

    monkeypatch.setitem(TOOL_HANDLERS, "search_saved_jobs", _boom)
    stream, handler = _capture_audit_log_lines()
    try:
        response = _rpc(client, STATIC_BEARER, "search_saved_jobs", {})
    finally:
        audit_module._audit_logger.removeHandler(handler)

    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == -32603
    assert body["error"]["message"] == "Internal error during tool execution"
    # The leak: no exception text anywhere in the response.
    assert LEAK_MARKER not in response.text
    assert "connection failed" not in response.text
    data = body["error"]["data"]
    assert set(data) == {"correlation_id"}
    correlation_id = uuid.UUID(data["correlation_id"])

    # Same id in the JSON log line and the audit row.
    lines = _parsed_log_lines(stream)
    assert len(lines) == 1
    assert lines[0]["correlation_id"] == str(correlation_id)
    assert lines[0]["status"] == "error"
    assert lines[0]["error_code"] == "-32603"
    assert lines[0]["tool_name"] == "search_saved_jobs"
    assert lines[0]["scope"] == "mcp:read"
    assert lines[0]["client_id"] is None  # static owner token
    inserts = _audit_inserts(fake_pool)
    assert len(inserts) == 1
    _, params = inserts[0]
    assert params[0] == str(correlation_id)
    assert params[1] is None
    assert params[2] == "search_saved_jobs"
    assert params[3] == "mcp:read"
    assert params[5] == "error"
    assert params[6] == "-32603"
    assert params[7] == "Internal error during tool execution"
    assert LEAK_MARKER not in str(params)


def test_scope_denial_carries_correlation_id_and_client(client, fake_pool):
    token, client_id = _oauth_token_with_client(client, "mcp:read")
    response = _rpc(client, token, "queue_job_for_application", {"job_id": "li-1"})
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == -32001
    data = body["error"]["data"]
    assert data["tool"] == "queue_job_for_application"
    assert data["required_scope"] == "mcp:apply"
    correlation_id = uuid.UUID(data["correlation_id"])
    inserts = _audit_inserts(fake_pool)
    assert len(inserts) == 1
    _, params = inserts[0]
    assert params[0] == str(correlation_id)
    assert params[1] == client_id  # middleware stashed the OAuth client_id
    assert params[2] == "queue_job_for_application"
    assert params[3] == "mcp:apply"
    assert params[5] == "error"
    assert params[6] == "-32001"


def test_validation_error_carries_correlation_id(client, fake_pool):
    response = _rpc(client, STATIC_BEARER, "search_saved_jobs", {"limit": 999})
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == -32602
    correlation_id = uuid.UUID(body["error"]["data"]["correlation_id"])
    inserts = _audit_inserts(fake_pool)
    assert len(inserts) == 1
    _, params = inserts[0]
    assert params[0] == str(correlation_id)
    assert params[2] == "search_saved_jobs"
    assert params[5] == "error"
    assert params[6] == "-32602"


def test_success_call_writes_ok_row(client, fake_pool, monkeypatch):
    async def _ok(arguments):
        return [{"job_id": "li-1"}]

    monkeypatch.setitem(TOOL_HANDLERS, "search_saved_jobs", _ok)
    response = _rpc(client, STATIC_BEARER, "search_saved_jobs", {})
    assert response.status_code == 200
    assert "error" not in response.json()
    inserts = _audit_inserts(fake_pool)
    assert len(inserts) == 1
    _, params = inserts[0]
    uuid.UUID(params[0])
    assert params[1] is None
    assert params[2] == "search_saved_jobs"
    assert params[3] == "mcp:read"
    assert params[5] == "ok"
    assert params[6] is None


def test_missing_tool_name_audited_as_unknown(client, fake_pool):
    response = client.post(
        "/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
        headers={"Authorization": f"Bearer {STATIC_BEARER}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == -32602
    uuid.UUID(body["error"]["data"]["correlation_id"])
    inserts = _audit_inserts(fake_pool)
    assert len(inserts) == 1
    _, params = inserts[0]
    assert params[2] == "unknown"
    assert params[5] == "error"
    assert params[6] == "-32602"


def test_unknown_tool_result_audited_as_error(client, fake_pool):
    response = _rpc(client, STATIC_BEARER, "no_such_tool", {})
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["content"][0]["text"]
    assert "Unknown tool" in json.loads(body["result"]["content"][0]["text"])["error"]
    inserts = _audit_inserts(fake_pool)
    assert len(inserts) == 1
    _, params = inserts[0]
    assert params[2] == "no_such_tool"
    assert params[3] == "mcp"
    assert params[5] == "error"
    assert "Unknown tool" in params[7]
