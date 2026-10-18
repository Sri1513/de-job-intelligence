# tests/test_logs_viewer.py
"""Tests for the token-gated service log viewer page.

Security model: the /logs page shell is public (like other dashboard reads),
but all log data flows through /api/admin/logs, which requires the Bearer
dashboard token and 404s without it (covered in test_admin.py). These tests
pin the page's half of that contract.
"""

import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.dashboard.app import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", "dash-secret")
    return TestClient(app)


def test_logs_page_is_public_shell(client):
    """/logs renders for anonymous visitors — the shell carries no log data."""
    r = client.get("/logs")
    assert r.status_code == 200
    assert "Service Logs" in r.text


def test_logs_page_embeds_no_log_data(client):
    """The HTML must not inline any service log content; data comes only
    from the authed /api/admin/logs endpoint at runtime."""
    r = client.get("/logs")
    assert r.status_code == 200
    assert "/api/admin/logs" in r.text
    # No log lines are server-rendered: the pane ships empty and is
    # populated only by the authed API fetch at runtime.
    assert '<div id="log-pane"' in r.text
    assert r.text.split('<div id="log-pane"')[1].split("</div>")[0].strip().endswith(">")


def test_logs_page_redirects_to_login_without_token(client):
    """The page's JS sends visitors without a dashboard token to /login."""
    r = client.get("/logs")
    assert r.status_code == 200
    assert "/login?next=" in r.text
    assert "dj_token" in r.text


def test_logs_page_renders_log_lines_as_text(client):
    """Log lines must be inserted via textContent, never innerHTML —
    log content is untrusted and must not become executable markup."""
    r = client.get("/logs")
    assert r.status_code == 200
    assert "textContent" in r.text
    # No innerHTML assignment anywhere in the page script.
    assert "innerHTML" not in r.text


def test_dashboard_links_to_logs_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/logs"' in r.text
