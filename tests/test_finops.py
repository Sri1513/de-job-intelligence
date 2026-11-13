"""Tests for the FinOps widget endpoint (GET /api/finops).

The widget is LLM cost & failover telemetry, reframed from the Sprint 1
spec ("Cost per Application"): the apply engine is dormant, so there is
no per-application cost. The endpoint serves aggregates only — no per-call
rows, no job_ids, no error text — so it is safe for the public showcase
view. One SQL statement, one fetchone, so the mock pattern below mirrors
tests/test_engine_progress.py.
"""
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from src.core.config import settings
from src.dashboard.app import app

TOKEN = "test-finops-token"


def _mock_db(monkeypatch, fetchone=None, exc=None):
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    if exc is not None:
        monkeypatch.setattr(
            "src.dashboard.app.get_db_connection", lambda: (_ for _ in ()).throw(exc)
        )
    else:
        monkeypatch.setattr("src.dashboard.app.get_db_connection", lambda: cm)
    return cur


def _client(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_TOKEN", TOKEN)
    return TestClient(app)


def _ledger_row(**overrides):
    row = {
        "total_calls": 43,
        "total_cost_usd": 0,
        "fallbacks": 1,
        "errors": 0,
        "first_call_at": "2026-11-12T17:46:00",
        "last_call_at": "2026-11-13T05:04:00",
        "by_model": [
            {
                "provider": "gemini",
                "model": "gemini-3.5-flash-lite",
                "calls": 43,
                "cost_usd": 0,
                "fallbacks": 1,
            }
        ],
        "by_fallback_reason": [{"reason": "transient", "calls": 1}],
        "daily": [
            {"day": "2026-11-12", "calls": 20, "cost_usd": 0, "fallbacks": 1},
            {"day": "2026-11-13", "calls": 23, "cost_usd": 0, "fallbacks": 0},
        ],
    }
    row.update(overrides)
    return row


def test_finops_aggregates(monkeypatch):
    _mock_db(monkeypatch, fetchone=_ledger_row())
    client = _client(monkeypatch)
    r = client.get("/api/finops")
    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] == 43
    assert body["total_cost_usd"] == 0.0
    assert body["fallbacks"] == 1
    assert body["errors"] == 0
    assert body["first_call_at"] == "2026-11-12T17:46:00"
    assert body["last_call_at"] == "2026-11-13T05:04:00"
    assert body["by_model"][0]["model"] == "gemini-3.5-flash-lite"
    assert body["by_model"][0]["calls"] == 43
    assert body["by_fallback_reason"] == [{"reason": "transient", "calls": 1}]
    # Daily is oldest-first for the chart, even though SQL returns newest-first.
    assert [d["day"] for d in body["daily"]] == ["2026-11-12", "2026-11-13"]
    # Aggregates only: no per-call rows, no job_ids, no error text.
    assert "job_id" not in str(body)
    assert "error_message" not in str(body)


def test_finops_empty_ledger(monkeypatch):
    _mock_db(
        monkeypatch,
        fetchone=_ledger_row(
            total_calls=0,
            total_cost_usd=0,
            fallbacks=0,
            errors=0,
            first_call_at=None,
            last_call_at=None,
            by_model=[],
            by_fallback_reason=[],
            daily=[],
        ),
    )
    client = _client(monkeypatch)
    r = client.get("/api/finops")
    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] == 0
    assert body["total_cost_usd"] == 0.0
    assert body["by_model"] == []
    assert body["daily"] == []
    assert body["first_call_at"] is None


def test_finops_string_json_columns(monkeypatch):
    # Older drivers may return json_agg columns as strings, not lists.
    import json

    row = _ledger_row()
    row["by_model"] = json.dumps(row["by_model"])
    row["by_fallback_reason"] = json.dumps(row["by_fallback_reason"])
    row["daily"] = json.dumps(row["daily"])
    _mock_db(monkeypatch, fetchone=row)
    client = _client(monkeypatch)
    r = client.get("/api/finops")
    assert r.status_code == 200
    assert r.json()["by_model"][0]["model"] == "gemini-3.5-flash-lite"
    assert len(r.json()["daily"]) == 2


def test_finops_db_error_returns_empty_shape(monkeypatch):
    _mock_db(monkeypatch, exc=RuntimeError("db down"))
    client = _client(monkeypatch)
    r = client.get("/api/finops")
    assert r.status_code == 500
    body = r.json()
    assert body["total_calls"] == 0
    assert body["by_model"] == []
    assert body["daily"] == []
