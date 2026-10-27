"""Tests for /api/jobs keyset pagination (newest-first, 50 per page)."""
from datetime import datetime

from src.dashboard.app import (
    _decode_jobs_cursor,
    _encode_jobs_cursor,
    _jobs_keyset_clause,
    _jobs_page_params,
)


class _FakeRequest:
    def __init__(self, params):
        self.query_params = params


def test_cursor_round_trip():
    cursor = _encode_jobs_cursor("2026-10-27T19:38:53.594983", "li-123")
    saved_at, job_id = _decode_jobs_cursor(cursor)
    assert saved_at == datetime(2026, 10, 27, 19, 38, 53, 594983)
    assert job_id == "li-123"


def test_cursor_null_saved_at_round_trip():
    cursor = _encode_jobs_cursor(None, "li-123")
    assert _decode_jobs_cursor(cursor) == (None, "li-123")


def test_cursor_invalid_fails_open():
    assert _decode_jobs_cursor("not-a-cursor") == (None, None)
    assert _decode_jobs_cursor("") == (None, None)


def test_page_params_defaults_and_clamps():
    assert _jobs_page_params(_FakeRequest({}))[0] is None
    assert _jobs_page_params(_FakeRequest({"limit": "50"}))[0] == 50
    assert _jobs_page_params(_FakeRequest({"limit": "500"}))[0] == 100
    assert _jobs_page_params(_FakeRequest({"limit": "abc"}))[0] == 50
    assert _jobs_page_params(_FakeRequest({"limit": "0"}))[0] == 1


def test_page_params_passes_cursor_through():
    cursor = _encode_jobs_cursor("2026-10-27T19:38:53.594983", "li-123")
    limit, saved_at, job_id = _jobs_page_params(
        _FakeRequest({"limit": "50", "cursor": cursor})
    )
    assert limit == 50
    assert saved_at == datetime(2026, 10, 27, 19, 38, 53, 594983)
    assert job_id == "li-123"


def test_keyset_clause_no_cursor():
    assert _jobs_keyset_clause(None, None) == ("", ())


def test_keyset_clause_non_null_cursor():
    saved_at = datetime(2026, 10, 27, 19, 38, 53, 594983)
    sql, params = _jobs_keyset_clause(saved_at, "li-123")
    assert "saved_at < %s" in sql
    assert "saved_at IS NULL" in sql
    assert params == (saved_at, saved_at, "li-123")


def test_keyset_clause_null_cursor():
    sql, params = _jobs_keyset_clause(None, "li-123")
    assert sql == "AND saved_at IS NULL AND job_id < %s"
    assert params == ("li-123",)
