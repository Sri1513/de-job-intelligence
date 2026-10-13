# tests/test_dashboard_auth.py
"""Tests for dashboard write-endpoint auth (reads public, writes need login).

Write endpoints require `Authorization: Bearer <ADMIN_API_TOKEN>`.
Fail-closed: when ADMIN_API_TOKEN is unset, all writes are denied.
"""
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.dashboard.app import app

WRITE_ENDPOINTS = [
    ("/api/applications/queue", {"job_id": "job-1"}),
    ("/api/applications/update-status", {"job_id": "job-1", "status": "QUEUED"}),
    ("/api/jobs/update-status", {"job_id": "job-1", "status": "applied"}),
    ("/api/jobs/update-notes", {"job_id": "job-1", "notes": "hi"}),
    ("/api/whatsapp-alerts/update-status", {"outreach_id": "wa-1", "status": "sent"}),
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", "dash-secret")
    return TestClient(app)


def _auth(token="dash-secret"):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("path,body", WRITE_ENDPOINTS)
def test_writes_reject_anonymous(client, path, body):
    assert client.post(path, json=body).status_code == 401


@pytest.mark.parametrize("path,body", WRITE_ENDPOINTS)
def test_writes_reject_wrong_token(client, path, body):
    r = client.post(path, json=body, headers=_auth("wrong"))
    assert r.status_code == 401
    assert r.json()["success"] is False


def test_writes_fail_closed_without_token(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", "")
    client = TestClient(app)
    # Even a "correct-looking" token cannot authorize when none is configured.
    r = client.post("/api/applications/queue", json={"job_id": "x"}, headers=_auth("x"))
    assert r.status_code == 401


def test_queue_rejects_get(client):
    # GET must not mutate: queueing is POST-only now.
    assert client.get("/api/applications/queue?job_id=x", headers=_auth()).status_code == 405


def test_queue_with_auth_reaches_handler(client, monkeypatch):
    # SUBMITTED row -> 409 proves the request passed auth and ran the handler.
    cur = MagicMock()
    cur.fetchone.return_value = {"apply_status": "SUBMITTED"}
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    monkeypatch.setattr("src.dashboard.app.get_db_connection", lambda: cm)
    r = client.post("/api/applications/queue", json={"job_id": "job-1"}, headers=_auth())
    assert r.status_code == 409


def test_login_accepts_correct_token(client):
    r = client.post("/api/login", json={"token": "dash-secret"})
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_login_rejects_wrong_token(client):
    assert client.post("/api/login", json={"token": "nope"}).status_code == 401


def test_login_page_is_public(client):
    assert client.get("/login").status_code == 200
