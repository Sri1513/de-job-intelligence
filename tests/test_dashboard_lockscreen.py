# tests/test_dashboard_lockscreen.py
"""Tests for the dynamic countdown lock screen (/lockscreen).

Public GET: one saved job + an ISO deadline with a timezone offset renders
the countdown page. Unknown job -> 404; missing or naive deadline -> 400;
flag off -> 404. No schema change, no MCP tool: the deadline is a param.
"""
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.dashboard.app import app


def _mock_db(monkeypatch, row):
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    monkeypatch.setattr("src.dashboard.app.get_db_connection", lambda: cm)


def _job_row(**overrides):
    row = {
        "job_id": "li-4474501423",
        "title": "Senior Data Engineer II",
        "company": "Pax8",
        "location": "USA",
        "fit_score": 63,
    }
    row.update(overrides)
    return row


@pytest.fixture(autouse=True)
def _enable_lockscreen(monkeypatch):
    monkeypatch.setattr(settings, "LOCKSCREEN_ENABLED", True)


def test_lockscreen_renders_job_and_deadline(monkeypatch):
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get(
        "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00-05:00"
    )
    assert resp.status_code == 200
    assert "Pax8 — Senior Data Engineer II" in resp.text
    assert "2026-11-30T23:59:00-05:00" in resp.text
    assert "Nov 30, 2026" in resp.text
    # Milestone dates derive from the deadline (minus 13/12/10/6 days).
    assert "Nov 17" in resp.text
    assert "Nov 18" in resp.text
    assert "Nov 20" in resp.text
    assert "Nov 24" in resp.text


def test_lockscreen_unknown_job_404(monkeypatch):
    _mock_db(monkeypatch, None)
    client = TestClient(app)
    resp = client.get(
        "/lockscreen?job_id=li-nope&deadline=2026-11-30T23:59:00-05:00"
    )
    assert resp.status_code == 404


def test_lockscreen_missing_deadline_400(monkeypatch):
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get("/lockscreen?job_id=li-4474501423")
    assert resp.status_code == 400


def test_lockscreen_naive_deadline_400(monkeypatch):
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get(
        "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00"
    )
    assert resp.status_code == 400


def test_lockscreen_disabled_404(monkeypatch):
    monkeypatch.setattr(settings, "LOCKSCREEN_ENABLED", False)
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get(
        "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00-05:00"
    )
    assert resp.status_code == 404
