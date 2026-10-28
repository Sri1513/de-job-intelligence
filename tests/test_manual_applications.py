# tests/test_manual_applications.py
"""Tests for manual application logging (dashboard Mark Applied path).

Marking a job applied from the dashboard is a HUMAN application, so it
records apply_status='MANUAL' (never 'SUBMITTED', which is reserved for
engine submissions) plus optional details in metadata JSON. The review
pool surfaces those details without leaking the rest of metadata
(personal notes stay out of the ungated applications response).
"""
import json
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from src.core.config import settings
from src.dashboard.app import app

TOKEN = "dash-secret"


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _mock_db(monkeypatch, fetchone=None, fetchall=None):
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall or []
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    monkeypatch.setattr("src.dashboard.app.get_db_connection", lambda: cm)
    return cur


def _client(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", TOKEN)
    return TestClient(app)


def test_mark_applied_records_manual_with_details(monkeypatch):
    cur = _mock_db(monkeypatch, fetchone={"metadata": {"personal_notes": "keep me"}})
    client = _client(monkeypatch)
    r = client.post(
        "/api/jobs/update-status",
        json={
            "job_id": "li-1",
            "status": "applied",
            "application": {"portal": "Indeed", "work_type": "C2C", "recruiter": "Rae"},
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    updates = [c for c in cur.execute.call_args_list if "UPDATE" in c.args[0]]
    assert len(updates) == 1
    sql, params = updates[0].args[0], updates[0].args[1]
    assert "MANUAL" in sql
    assert "SUBMITTED" not in sql
    meta = json.loads(params[1])
    assert meta["manual_application"]["portal"] == "Indeed"
    assert meta["manual_application"]["work_type"] == "C2C"
    assert meta["manual_application"]["recruiter"] == "Rae"
    assert meta["personal_notes"] == "keep me"


def test_mark_applied_without_row_still_manual(monkeypatch):
    cur = _mock_db(monkeypatch, fetchone=None)
    client = _client(monkeypatch)
    r = client.post(
        "/api/jobs/update-status",
        json={"job_id": "li-9", "status": "applied"},
        headers=_auth(),
    )
    assert r.status_code == 200
    updates = [c for c in cur.execute.call_args_list if "UPDATE" in c.args[0]]
    assert len(updates) == 1
    assert "MANUAL" in updates[0].args[0]
    assert len(updates[0].args[1]) == 2  # no metadata to write back


def test_other_status_unchanged(monkeypatch):
    cur = _mock_db(monkeypatch)
    client = _client(monkeypatch)
    r = client.post(
        "/api/jobs/update-status",
        json={"job_id": "li-1", "status": "saved"},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert cur.fetchone.call_count == 0
    updates = [c for c in cur.execute.call_args_list if "UPDATE" in c.args[0]]
    assert "MANUAL" not in updates[0].args[0]
    assert updates[0].args[1] == ("saved", "SAVED", "li-1")


def test_applications_surfaces_manual_details_without_metadata_leak(monkeypatch):
    counts = {"pending": 0, "queued": 0, "submitted": 1, "failed": 0, "manual": 2}
    row = {
        "job_id": "li-1",
        "title": "Data Engineer",
        "company": "Acme",
        "location": "Remote",
        "job_url": "https://example.com/j",
        "fit_score": 64,
        "apply_status": "MANUAL",
        "applied_at": None,
        "review_screenshot_path": None,
        "apply_notes": "n/a",
        "saved_at": None,
        "metadata": {
            "personal_notes": "SECRET-NOTE",
            "manual_application": {"portal": "Indeed", "work_type": "C2C"},
        },
    }
    _mock_db(monkeypatch, fetchone=counts, fetchall=[row])
    client = _client(monkeypatch)
    r = client.get("/api/applications?status=ALL")
    assert r.status_code == 200
    data = r.json()
    assert data["stats"]["manual"] == 2
    rec = data["applications"][0]
    assert rec["manual_application"]["portal"] == "Indeed"
    assert rec["manual_application"]["work_type"] == "C2C"
    assert "metadata" not in rec
    assert "SECRET-NOTE" not in r.text
