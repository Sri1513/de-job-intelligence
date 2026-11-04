# tests/test_mcp_auth.py
"""Dedicated tests for the MCP bearer-token gate (src/protocols/auth.py)."""
import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.protocols.app import create_app


def _client_for(token: str) -> TestClient:
    return TestClient(create_app(auth_token=token))


def test_health_bypasses_auth():
    client = _client_for("some-token")
    assert client.get("/health").status_code == 200


def test_missing_token_rejected():
    client = _client_for("secret")
    response = client.post("/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401


def test_wrong_token_rejected():
    client = _client_for("secret")
    response = client.post(
        "/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_malformed_scheme_rejected():
    client = _client_for("secret")
    response = client.post(
        "/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": "Token secret"},
    )
    assert response.status_code == 401


def test_correct_token_accepted():
    client = _client_for("secret")
    response = client.post(
        "/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    assert response.json()["result"] == {}


def test_fail_closed_without_token(monkeypatch):
    monkeypatch.setattr(settings, "MCP_AUTH_TOKEN", "")
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN"):
        create_app()


def test_root_landing_page_public():
    client = _client_for("secret")
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert '<link rel="icon" type="image/x-icon" href="/favicon.ico">' in response.text
    assert "de-job-intelligence-public" in response.text
    for sensitive in ("MCP_AUTH_TOKEN", "mcp_dashboard_reader", "mcp-server:8000", "150.136"):
        assert sensitive not in response.text


def test_root_post_still_requires_auth():
    client = _client_for("secret")
    response = client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401


def test_favicon_still_public():
    client = _client_for("secret")
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert "image/x-icon" in response.headers["content-type"]
