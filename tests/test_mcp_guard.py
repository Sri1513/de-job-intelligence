"""Tests for Sprint 2, Batch 4: HTTP guards.

- 1 MB request cap (413 before anything reads the body).
- Origin validation on the private JSON-RPC paths only when an Origin
  header is present (browsers); server-side clients send none and pass
  through. Loopback + MCP_ALLOWED_ORIGINS pass.
- Per-credential rate limiting (60/min default), bucketed by SHA-256 of
  the bearer token, or by client IP when no credential is presented.
- 30s tool timeout in handle_rpc, exempting run_application_worker and
  retry_job_evaluations.

The guard middleware is outermost, so rejections happen before auth:
an oversized or flooding request gets 413/429 even with no token at all.
"""

import asyncio
import time
import uuid

import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.protocols import audit as audit_module
from src.protocols import token_store as ts_module
from src.protocols.app import create_app
from src.protocols.dispatcher import TOOL_HANDLERS
from src.protocols.guard import _origin_allowed

OWNER_TOKEN = "test-owner-token"
STATIC_BEARER = "test-bearer"
OTHER_BEARER = "test-bearer-other"


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


def _rpc(client, token, tool, arguments, headers=None):
    all_headers = {}
    if token is not None:
        all_headers["Authorization"] = f"Bearer {token}"
    if headers:
        all_headers.update(headers)
    return client.post(
        "/rpc",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers=all_headers,
    )


def _ok_handler(monkeypatch, tool="search_saved_jobs"):
    async def _ok(arguments):
        return [{"job_id": "li-1"}]

    monkeypatch.setitem(TOOL_HANDLERS, tool, _ok)


# ---------------------------------------------------------------------------
# Settings defaults
# ---------------------------------------------------------------------------


def test_guard_defaults():
    assert settings.MCP_MAX_REQUEST_BYTES == 1_048_576
    assert settings.MCP_RATE_LIMIT_PER_MINUTE == 60
    assert settings.MCP_TOOL_TIMEOUT_SECONDS == 30.0
    assert "https://jobs.sriomkar.com" in settings.MCP_ALLOWED_ORIGINS


# ---------------------------------------------------------------------------
# Size cap
# ---------------------------------------------------------------------------


def test_oversized_body_rejected_before_auth(client, monkeypatch):
    # The minimal tools/call body is 99 bytes as httpx serializes it
    # (compact separators), so the cap must sit below that.
    monkeypatch.setattr(settings, "MCP_MAX_REQUEST_BYTES", 50)
    response = _rpc(client, None, "search_saved_jobs", {})
    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == -32000
    assert body["error"]["message"] == "Request too large"
    assert body["error"]["data"]["max_bytes"] == 50
    uuid.UUID(body["error"]["data"]["correlation_id"])


def test_body_under_cap_reaches_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "MCP_MAX_REQUEST_BYTES", 200)
    response = _rpc(client, None, "search_saved_jobs", {})
    # Under the cap: the guard passes and auth rejects (no token).
    assert response.status_code == 401


def test_size_cap_applies_to_non_rpc_paths_too(client, monkeypatch):
    monkeypatch.setattr(settings, "MCP_MAX_REQUEST_BYTES", 10)
    response = client.post("/oauth/register", json={"redirect_uris": ["x" * 100]})
    assert response.status_code == 413


# ---------------------------------------------------------------------------
# Origin validation
# ---------------------------------------------------------------------------


def test_foreign_origin_rejected(client, fake_pool, monkeypatch):
    _ok_handler(monkeypatch)
    response = _rpc(
        client,
        STATIC_BEARER,
        "search_saved_jobs",
        {},
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == -32001
    assert body["error"]["message"] == "Origin not allowed"
    assert body["error"]["data"]["origin"] == "https://evil.example.com"
    uuid.UUID(body["error"]["data"]["correlation_id"])
    # Rejected in middleware: no tool call, no audit row.
    assert _audit_inserts(fake_pool) == []


def test_allowed_origin_passes(client, fake_pool, monkeypatch):
    _ok_handler(monkeypatch)
    response = _rpc(
        client,
        STATIC_BEARER,
        "search_saved_jobs",
        {},
        headers={"Origin": "https://jobs.sriomkar.com"},
    )
    assert response.status_code == 200
    assert len(_audit_inserts(fake_pool)) == 1


def test_loopback_origin_passes(client, fake_pool, monkeypatch):
    _ok_handler(monkeypatch)
    for origin in ("http://localhost:5173", "http://127.0.0.1:6274", "http://[::1]:8000"):
        response = _rpc(client, STATIC_BEARER, "search_saved_jobs", {}, headers={"Origin": origin})
        assert response.status_code == 200, origin
    assert len(_audit_inserts(fake_pool)) == 3


def test_missing_origin_passes(client, fake_pool, monkeypatch):
    # Server-side clients send no Origin header at all.
    _ok_handler(monkeypatch)
    response = _rpc(client, STATIC_BEARER, "search_saved_jobs", {})
    assert response.status_code == 200


def test_origin_check_skipped_on_health(client):
    response = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 200


def test_origin_allowed_unit():
    assert _origin_allowed("https://jobs.sriomkar.com")
    assert _origin_allowed("https://sriomkar.com")
    assert _origin_allowed("http://localhost:3000")
    assert not _origin_allowed("https://evil.example.com")
    assert not _origin_allowed("http://[::1")  # unparseable: not allowed
    assert not _origin_allowed("")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limit_blocks_fourth_call_same_token(client, fake_pool, monkeypatch):
    monkeypatch.setattr(settings, "MCP_RATE_LIMIT_PER_MINUTE", 3)
    _ok_handler(monkeypatch)
    for _ in range(3):
        assert _rpc(client, STATIC_BEARER, "search_saved_jobs", {}).status_code == 200
    limited = _rpc(client, STATIC_BEARER, "search_saved_jobs", {})
    assert limited.status_code == 429
    body = limited.json()
    assert body["error"]["code"] == -32000
    assert body["error"]["message"] == "Rate limit exceeded"
    assert body["error"]["data"]["retry_after_seconds"] >= 1
    assert limited.headers["retry-after"] == str(body["error"]["data"]["retry_after_seconds"])
    uuid.UUID(body["error"]["data"]["correlation_id"])
    # Rejected in middleware: only the 3 allowed calls wrote audit rows.
    assert len(_audit_inserts(fake_pool)) == 3


def test_rate_limit_is_per_token(client, fake_pool, monkeypatch):
    monkeypatch.setattr(settings, "MCP_RATE_LIMIT_PER_MINUTE", 2)
    _ok_handler(monkeypatch)
    for _ in range(2):
        assert _rpc(client, STATIC_BEARER, "search_saved_jobs", {}).status_code == 200
    assert _rpc(client, STATIC_BEARER, "search_saved_jobs", {}).status_code == 429
    # A different credential has its own budget: passes the guard, then
    # auth rejects it (unknown token) with 401 — not 429.
    assert _rpc(client, OTHER_BEARER, "search_saved_jobs", {}).status_code == 401


def test_rate_limit_buckets_unauthenticated_by_ip(client, monkeypatch):
    monkeypatch.setattr(settings, "MCP_RATE_LIMIT_PER_MINUTE", 2)
    for _ in range(2):
        assert _rpc(client, None, "search_saved_jobs", {}).status_code == 401
    assert _rpc(client, None, "search_saved_jobs", {}).status_code == 429


def test_rate_limit_zero_disables(client, fake_pool, monkeypatch):
    monkeypatch.setattr(settings, "MCP_RATE_LIMIT_PER_MINUTE", 0)
    _ok_handler(monkeypatch)
    for _ in range(5):
        assert _rpc(client, STATIC_BEARER, "search_saved_jobs", {}).status_code == 200


# ---------------------------------------------------------------------------
# Tool timeout
# ---------------------------------------------------------------------------


def test_slow_tool_times_out(client, fake_pool, monkeypatch):
    monkeypatch.setattr(settings, "MCP_TOOL_TIMEOUT_SECONDS", 0.5)

    async def _slow(arguments):
        await asyncio.sleep(3)
        return [{"job_id": "li-1"}]

    monkeypatch.setitem(TOOL_HANDLERS, "search_saved_jobs", _slow)
    started = time.monotonic()
    response = _rpc(client, STATIC_BEARER, "search_saved_jobs", {})
    elapsed = time.monotonic() - started
    assert elapsed < 2.0  # returned on the timeout, not after the sleep
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == -32000
    assert body["error"]["message"] == "Tool call timed out after 0.5s"
    correlation_id = uuid.UUID(body["error"]["data"]["correlation_id"])
    inserts = _audit_inserts(fake_pool)
    assert len(inserts) == 1
    _, params = inserts[0]
    assert params[0] == str(correlation_id)
    assert params[5] == "error"
    assert params[6] == "-32000"


def test_exempt_tool_is_not_timed_out(client, fake_pool, monkeypatch):
    monkeypatch.setattr(settings, "MCP_TOOL_TIMEOUT_SECONDS", 0.5)

    async def _slow(arguments):
        await asyncio.sleep(1.0)
        return {"success": True, "jobs_processed": 0, "results": []}

    monkeypatch.setitem(TOOL_HANDLERS, "run_application_worker", _slow)
    started = time.monotonic()
    response = _rpc(client, STATIC_BEARER, "run_application_worker", {})
    elapsed = time.monotonic() - started
    assert elapsed >= 1.0  # ran to completion, past the 0.5s timeout
    assert response.status_code == 200
    assert "error" not in response.json()
    inserts = _audit_inserts(fake_pool)
    assert len(inserts) == 1
    assert inserts[0][1][5] == "ok"


def test_timeout_disabled_when_zero(client, fake_pool, monkeypatch):
    monkeypatch.setattr(settings, "MCP_TOOL_TIMEOUT_SECONDS", 0)

    async def _slow(arguments):
        await asyncio.sleep(1.0)
        return [{"job_id": "li-1"}]

    monkeypatch.setitem(TOOL_HANDLERS, "search_saved_jobs", _slow)
    response = _rpc(client, STATIC_BEARER, "search_saved_jobs", {})
    assert response.status_code == 200
    assert "error" not in response.json()
