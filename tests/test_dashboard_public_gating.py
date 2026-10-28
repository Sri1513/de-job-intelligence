# tests/test_dashboard_public_gating.py
"""Tests for public-vs-token gating on dashboard reads.

The dashboard link is public. Unauthenticated readers see the showcase
fields only; personal notes, visa analysis, applied state, and WhatsApp
message bodies need the dashboard token. Writes were already gated
(see test_dashboard_auth.py); this file covers the read side.
"""
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from src.core.config import settings
from src.dashboard.app import (
    app,
    redact_alert_for_public,
    redact_job_for_public,
)

TOKEN = "dash-secret"


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _job_row(**overrides):
    row = {
        "job_id": "li-1",
        "title": "Data Engineer",
        "company": "Acme",
        "location": "Remote",
        "employment_type": "Full-time",
        "sponsorship": "Available",
        "fit_score": 64,
        "status": "applied",
        "metadata": {"personal_notes": "Call this recruiter first"},
        "job_url": "https://example.com/j",
        "saved_at": None,
        "notes": "AI notes",
        "applied_at": "2026-10-27T10:00:00",
        "apply_status": "SUBMITTED",
        "description": "<p>Build pipelines</p>",
    }
    row.update(overrides)
    return row


def _alert_row(**overrides):
    row = {
        "outreach_id": 7,
        "short_id": "abc1",
        "company_name": "Acme",
        "job_title": "Data Engineer",
        "recruiter_name": "Rae",
        "status": "Drafted",
        "created_at": None,
        "resume_doc_url": "https://docs.example.com/r",
        "gmail_draft_id": "d-1",
        "metadata": {"whatsapp_raw": "Hi, we have a role", "recipient_email": "rae@acme.com"},
        "helper_name": "Helper",
        "helper_email": "h@example.com",
    }
    row.update(overrides)
    return row


def _mock_db(monkeypatch, rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    monkeypatch.setattr("src.dashboard.app.get_db_connection", lambda: cm)


def _client(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", TOKEN)
    return TestClient(app)


def test_redact_job_strips_private_fields():
    redacted = redact_job_for_public(_job_row())
    assert redacted["personal_notes"] == ""
    assert redacted["sponsorship"] is None
    assert redacted["apply_status"] is None
    assert redacted["applied_at"] is None
    assert redacted["status"] is None
    # Public fields untouched (the redactor passes them through as-is;
    # HTML-stripping of descriptions happens in api_jobs, not here).
    assert redacted["title"] == "Data Engineer"
    assert redacted["company"] == "Acme"
    assert redacted["description"] == "<p>Build pipelines</p>"


def test_redact_job_keeps_saved_status():
    redacted = redact_job_for_public(_job_row(status="saved"))
    assert redacted["status"] == "saved"


def test_redact_alert_strips_body_only():
    alert = {
        "company_name": "Acme",
        "status": "Drafted",
        "whatsapp_raw": "Hi, we have a role",
        "recipient_email": "rae@acme.com",
    }
    redacted = redact_alert_for_public(alert)
    assert redacted["whatsapp_raw"] is None
    assert redacted["company_name"] == "Acme"
    assert redacted["status"] == "Drafted"
    assert redacted["recipient_email"] == "rae@acme.com"


def test_jobs_public_response_is_redacted(monkeypatch):
    _mock_db(monkeypatch, [_job_row()])
    client = _client(monkeypatch)
    r = client.get("/api/jobs?category=data_engineering&limit=50")
    assert r.status_code == 200
    job = r.json()["jobs"][0]
    assert job["personal_notes"] == ""
    assert job["sponsorship"] is None
    assert job["apply_status"] is None
    assert job["applied_at"] is None
    assert job["status"] is None


def test_jobs_authed_response_is_full(monkeypatch):
    _mock_db(monkeypatch, [_job_row()])
    client = _client(monkeypatch)
    r = client.get("/api/jobs?category=data_engineering&limit=50", headers=_auth())
    job = r.json()["jobs"][0]
    assert job["personal_notes"] == "Call this recruiter first"
    assert job["sponsorship"] == "Available"
    assert job["apply_status"] == "SUBMITTED"
    assert job["status"] == "applied"


def test_alerts_public_response_hides_bodies(monkeypatch):
    _mock_db(monkeypatch, [_alert_row()])
    client = _client(monkeypatch)
    r = client.get("/api/whatsapp-alerts")
    assert r.status_code == 200
    alert = r.json()[0]
    assert alert["whatsapp_raw"] is None
    assert alert["company_name"] == "Acme"
    assert alert["status"] == "Drafted"


def test_alerts_authed_response_has_bodies(monkeypatch):
    _mock_db(monkeypatch, [_alert_row()])
    client = _client(monkeypatch)
    r = client.get("/api/whatsapp-alerts", headers=_auth())
    assert r.json()[0]["whatsapp_raw"] == "Hi, we have a role"
