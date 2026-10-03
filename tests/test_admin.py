# tests/test_admin.py
"""Tests for the remote diagnostics API (src/dashboard/admin.py)."""
import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.dashboard.app import app


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", "admin-secret")
    monkeypatch.setattr(settings, "LOGS_DIR", tmp_path)
    return TestClient(app)


def _headers(token="admin-secret"):
    return {"Authorization": f"Bearer {token}"}


def _get(client, path, token):
    return client.get(path, headers=_headers(token))


def test_admin_disabled_returns_404(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", "")
    monkeypatch.setattr(settings, "LOGS_DIR", tmp_path)
    client = TestClient(app)
    assert client.get("/api/admin/health", headers=_headers()).status_code == 404
    assert client.get("/api/admin/logs?service=mcp", headers=_headers()).status_code == 404


def test_admin_wrong_token_returns_404(admin_client):
    assert _get(admin_client, "/api/admin/health", "wrong").status_code == 404


def test_admin_missing_token_returns_404(admin_client):
    assert admin_client.get("/api/admin/health").status_code == 404


def test_admin_health_structure(admin_client):
    response = admin_client.get("/api/admin/health", headers=_headers())
    assert response.status_code == 200
    data = response.json()
    assert data["dashboard"] == "ok"
    assert "status" in data["database"]
    assert "status" in data["mcp_server"]
    assert "timestamp" in data
    assert isinstance(data["queue"], dict)


def test_admin_logs_tail(admin_client, tmp_path):
    log = tmp_path / "mcp.log"
    log.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")
    response = admin_client.get("/api/admin/logs?service=mcp&tail=3", headers=_headers())
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "mcp"
    assert data["lines"] == ["line 7", "line 8", "line 9"]


def test_admin_logs_unknown_service(admin_client):
    response = admin_client.get("/api/admin/logs?service=nope", headers=_headers())
    assert response.status_code == 400


def test_admin_logs_missing_file(admin_client):
    response = admin_client.get("/api/admin/logs?service=dashboard", headers=_headers())
    assert response.status_code == 404
