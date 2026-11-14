# tests/test_dashboard_public_gating.py
"""Tests for public-vs-token gating on dashboard reads.

The dashboard link is public. Unauthenticated readers see the showcase
fields only; personal notes, visa analysis, applied state, and WhatsApp
message bodies need the dashboard token. Writes were already gated
(see test_dashboard_auth.py); this file covers the read side.
"""
from pathlib import Path
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from src.core.config import settings
from src.dashboard.app import (
    app,
    redact_alert_for_public,
    redact_application_for_public,
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


def _application_row(**overrides):
    row = {
        "job_id": "li-1",
        "title": "Data Engineer",
        "company": "Acme",
        "location": "Remote",
        "job_url": "https://example.com/j",
        "fit_score": 64,
        "apply_status": "SUBMITTED",
        "applied_at": "2026-11-14T10:00:00",
        "review_screenshot_path": None,
        "apply_notes": "Submitted via Dice, confirmation pending.",
        "saved_at": None,
        "metadata": {
            "manual_application": {
                "portal": "Dice",
                "work_type": "C2C",
                "recruiter": "Rae",
                "rate": "$60/hr",
                "notes": "Call Rae first",
            },
            "engine_tracker": {"stage": "applied", "recruiter": "Rae"},
        },
    }
    row.update(overrides)
    return row


def _application_response_row(**overrides):
    """A row as api_applications builds it (post-query, pre-redaction)."""
    row = {
        "job_id": "li-1",
        "title": "Data Engineer",
        "company": "Acme",
        "location": "Remote",
        "job_url": "https://example.com/j",
        "fit_score": 64,
        "apply_status": "SUBMITTED",
        "applied_at": "2026-11-14T10:00:00",
        "screenshot_url": "/screenshots/li-1_review.png",
        "apply_notes": "Submitted via Dice, confirmation pending.",
        "manual_application": {
            "portal": "Dice",
            "work_type": "C2C",
            "recruiter": "Rae",
            "rate": "$60/hr",
            "notes": "Call Rae first",
        },
        "engine_tracker": {"stage": "applied", "recruiter": "Rae"},
    }
    row.update(overrides)
    return row


def _mock_applications_db(monkeypatch, rows, counts=None):
    cur = MagicMock()
    cur.fetchone.return_value = counts or {
        "pending": 1,
        "queued": 0,
        "submitted": 2,
        "failed": 0,
        "manual": 1,
    }
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    monkeypatch.setattr("src.dashboard.app.get_db_connection", lambda: cm)


def test_redact_application_strips_private_fields():
    redacted = redact_application_for_public(_application_response_row())
    assert redacted["apply_status"] is None
    assert redacted["applied_at"] is None
    assert redacted["apply_notes"] == ""
    assert redacted["screenshot_url"] is None
    assert redacted["manual_application"] is None
    assert redacted["engine_tracker"] is None
    # Showcase fields untouched.
    assert redacted["title"] == "Data Engineer"
    assert redacted["company"] == "Acme"
    assert redacted["location"] == "Remote"
    assert redacted["job_url"] == "https://example.com/j"
    assert redacted["fit_score"] == 64


def test_applications_public_response_is_redacted(monkeypatch, tmp_path):
    shot = tmp_path / "li-1_review.png"
    shot.write_bytes(b"fake-png")
    _mock_applications_db(
        monkeypatch, [_application_row(review_screenshot_path=str(shot))]
    )
    client = _client(monkeypatch)
    r = client.get("/api/applications")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"] == {
        "pending": 1,
        "queued": 0,
        "submitted": 2,
        "failed": 0,
        "manual": 1,
    }
    row = body["applications"][0]
    assert row["apply_status"] is None
    assert row["applied_at"] is None
    assert row["apply_notes"] == ""
    assert row["screenshot_url"] is None
    assert row["manual_application"] is None
    assert row["engine_tracker"] is None
    assert row["title"] == "Data Engineer"
    assert row["fit_score"] == 64


def test_applications_authed_response_is_full(monkeypatch, tmp_path):
    shot = tmp_path / "li-1_review.png"
    shot.write_bytes(b"fake-png")
    _mock_applications_db(
        monkeypatch, [_application_row(review_screenshot_path=str(shot))]
    )
    client = _client(monkeypatch)
    r = client.get("/api/applications", headers=_auth())
    assert r.status_code == 200
    row = r.json()["applications"][0]
    assert row["apply_status"] == "SUBMITTED"
    assert row["applied_at"] == "2026-11-14T10:00:00"
    assert row["apply_notes"] == "Submitted via Dice, confirmation pending."
    assert row["screenshot_url"] == "/screenshots/li-1_review.png"
    assert row["manual_application"]["recruiter"] == "Rae"
    assert row["manual_application"]["rate"] == "$60/hr"
    assert row["engine_tracker"] == {"stage": "applied", "recruiter": "Rae"}


def test_applications_template_renders_locked_badge_for_null_status():
    template = (
        Path(__file__).parent.parent
        / "src/dashboard/templates/applications.html"
    ).read_text()
    assert "${app.apply_status || '🔒 Private'}" in template
    # The no-screenshot sub-line must not claim "Awaiting worker
    # execution" for a public reader whose status was redacted.
    assert "app.apply_status ? 'Awaiting worker execution.' : ''" in template


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
