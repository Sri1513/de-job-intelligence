"""Tests for the Friday-engine progress tracker.

Tracker state lives in metadata JSON as `engine_tracker` (same no-migration
precedent as the MANUAL lane's `manual_application`). Writes need the
Bearer admin token; reads strip tracker detail for public viewers (PR #50
gating): /api/engine-progress returns stage counts only when logged out,
and /api/applications carries engine_tracker only when logged in.
"""
import json
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from src.core.config import settings
from src.dashboard.app import app

TOKEN = "test-engine-progress-token"


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


def test_update_tracker_requires_auth(monkeypatch):
    _mock_db(monkeypatch)
    client = _client(monkeypatch)
    r = client.post(
        "/api/jobs/update-tracker",
        json={"job_id": "li-1", "tracker": {"stage": "picked"}},
    )
    assert r.status_code == 401


def test_update_tracker_merges_preserving_other_metadata(monkeypatch):
    cur = _mock_db(
        monkeypatch,
        fetchone={
            "metadata": {
                "personal_notes": "keep me",
                "manual_application": {"portal": "Indeed"},
                "engine_tracker": {"stage": "picked", "source_portal": "Indeed"},
            }
        },
    )
    client = _client(monkeypatch)
    r = client.post(
        "/api/jobs/update-tracker",
        json={
            "job_id": "li-1",
            "tracker": {
                "stage": "tailored",
                "resume_version": "SoFi-tailored-resume.md",
                "cover_letter": False,
                "recruiter": "",
            },
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["engine_tracker"]["stage"] == "tailored"
    assert body["engine_tracker"]["source_portal"] == "Indeed"
    assert body["engine_tracker"]["resume_version"] == "SoFi-tailored-resume.md"
    # False is meaningful ("no cover letter"), not dropped; "" clears.
    assert body["engine_tracker"]["cover_letter"] is False
    assert "recruiter" not in body["engine_tracker"]
    # Outcome derives from stage when not set.
    assert body["engine_tracker"]["outcome"] == "open"
    updates = [c for c in cur.execute.call_args_list if "UPDATE" in c.args[0]]
    assert len(updates) == 1
    meta = json.loads(updates[0].args[1][0])
    assert meta["personal_notes"] == "keep me"
    assert meta["manual_application"] == {"portal": "Indeed"}
    assert meta["engine_tracker"]["stage"] == "tailored"


def test_update_tracker_rejects_bad_stage_and_portal(monkeypatch):
    _mock_db(monkeypatch, fetchone={"metadata": {}})
    client = _client(monkeypatch)
    r = client.post(
        "/api/jobs/update-tracker",
        json={"job_id": "li-1", "tracker": {"stage": "hired-ish"}},
        headers=_auth(),
    )
    assert r.status_code == 400
    r = client.post(
        "/api/jobs/update-tracker",
        json={"job_id": "li-1", "tracker": {"source_portal": "Craigslist"}},
        headers=_auth(),
    )
    assert r.status_code == 400


def test_update_tracker_upserts_browser_found_role(monkeypatch):
    cur = _mock_db(monkeypatch, fetchone=None)
    client = _client(monkeypatch)
    r = client.post(
        "/api/jobs/update-tracker",
        json={
            "job_id": "friday-acme-senior-data-engineer",
            "title": "Senior Data Engineer",
            "company": "Acme",
            "tracker": {"stage": "picked", "source_portal": "LinkedIn-discovery"},
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    inserts = [c for c in cur.execute.call_args_list if "INSERT" in c.args[0]]
    assert len(inserts) == 1
    sql, params = inserts[0].args[0], inserts[0].args[1]
    assert "friday_engine" in sql
    meta = json.loads(params[5])
    assert meta["engine_tracker"]["stage"] == "picked"
    assert meta["engine_tracker"]["source_portal"] == "LinkedIn-discovery"


def test_update_tracker_missing_row_without_details_is_404(monkeypatch):
    _mock_db(monkeypatch, fetchone=None)
    client = _client(monkeypatch)
    r = client.post(
        "/api/jobs/update-tracker",
        json={"job_id": "li-9", "tracker": {"stage": "picked"}},
        headers=_auth(),
    )
    assert r.status_code == 404


def test_engine_progress_public_gets_counts_only(monkeypatch):
    rows = [
        {
            "job_id": "li-1",
            "title": "Senior Data Engineer",
            "company": "Acme",
            "location": "Remote",
            "job_url": "https://example.com/j1",
            "fit_score": 64,
            "apply_status": "IDLE",
            "applied_at": None,
            "saved_at": None,
            "metadata": {
                "engine_tracker": {
                    "stage": "responded",
                    "source_portal": "Indeed",
                    "recruiter": "Rae",
                    "response": "Interview invite",
                    "rejection_reason": "",
                }
            },
        },
        {
            "job_id": "li-2",
            "title": "Data Engineer",
            "company": "Beta",
            "location": "Remote",
            "job_url": "https://example.com/j2",
            "fit_score": 50,
            "apply_status": "IDLE",
            "applied_at": None,
            "saved_at": None,
            "metadata": {"personal_notes": "no tracker here"},
        },
    ]
    _mock_db(monkeypatch, fetchall=rows)
    client = _client(monkeypatch)
    r = client.get("/api/engine-progress")
    assert r.status_code == 200
    body = r.json()
    assert body["stage_counts"]["responded"] == 1
    assert body["stage_counts"]["picked"] == 0
    # No per-role rows for public readers: recruiter/response never leak.
    assert body["roles"] == []


def test_engine_progress_authed_gets_full_tracker(monkeypatch):
    rows = [
        {
            "job_id": "li-1",
            "title": "Senior Data Engineer",
            "company": "Acme",
            "location": "Remote",
            "job_url": "https://example.com/j1",
            "fit_score": 64,
            "apply_status": "IDLE",
            "applied_at": None,
            "saved_at": None,
            "metadata": {
                "engine_tracker": {
                    "stage": "rejected",
                    "source_portal": "Dice",
                    "recruiter": "Rae",
                    "response": "Not moving forward",
                    "rejection_reason": "min quals",
                    "updated_at": "2026-10-29T10:00:00",
                }
            },
        }
    ]
    _mock_db(monkeypatch, fetchall=rows)
    client = _client(monkeypatch)
    r = client.get("/api/engine-progress", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert len(body["roles"]) == 1
    tracker = body["roles"][0]["engine_tracker"]
    assert tracker["recruiter"] == "Rae"
    assert tracker["rejection_reason"] == "min quals"
    # Stage filter applies to authed rows.
    r = client.get("/api/engine-progress?stage=picked", headers=_auth())
    assert r.json()["roles"] == []
    assert r.json()["stage_counts"]["rejected"] == 1


def test_applications_tracker_gated_by_auth(monkeypatch):
    rows = [
        {
            "job_id": "li-1",
            "title": "Senior Data Engineer",
            "company": "Acme",
            "location": "Remote",
            "job_url": "https://example.com/j1",
            "fit_score": 64,
            "apply_status": "MANUAL",
            "applied_at": None,
            "review_screenshot_path": None,
            "apply_notes": "n",
            "saved_at": None,
            "metadata": {"engine_tracker": {"stage": "applied", "source_portal": "Indeed"}},
        }
    ]
    counts = {"pending": 0, "queued": 0, "submitted": 0, "failed": 0, "manual": 1}
    cur = _mock_db(monkeypatch, fetchone=counts, fetchall=rows)
    client = _client(monkeypatch)
    r = client.get("/api/applications")
    assert r.status_code == 200
    assert r.json()["applications"][0]["engine_tracker"] is None
    r = client.get("/api/applications", headers=_auth())
    assert r.json()["applications"][0]["engine_tracker"]["stage"] == "applied"
