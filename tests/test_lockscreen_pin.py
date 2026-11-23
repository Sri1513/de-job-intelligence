# tests/test_lockscreen_pin.py
"""Pin + clear for the lock-screen dream job (branch friday/lockscreen-pin).

Gemini pins one saved job through the MCP server; the endpoint returns
it first while it still qualifies, then falls back to the auto pick.
Pin validation (unknown job, no posted date, expired deadline, not
IDLE, verified gone) names the reason and writes nothing. Clearing
removes the pin row, if any. The tools are registered in all four
places (schema manifest, handler registry, scope map, argument
models) and are write-side-effect tools, so they need mcp:apply.
"""
import asyncio
from datetime import date

import pytest

from src.protocols.dispatcher import TOOL_HANDLERS, dispatch_tool_call
from src.protocols.schemas import MCP_TOOLS
from src.protocols.scopes import SCOPE_APPLY, TOOL_SCOPES
from src.protocols.validation import ToolArgumentError


class FakeCursor:
    """Statements recorded; fetchone dispatches on SQL text."""

    def __init__(self, job_row=None, deleted_row=None):
        self.job_row = job_row
        self.deleted_row = deleted_row
        self.statements = []
        self._last_sql = ""

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self._last_sql = sql

    def fetchone(self):
        if "FROM scout.saved_jobs" in self._last_sql:
            return self.job_row
        if "DELETE FROM scout.lockscreen_pin" in self._last_sql:
            return self.deleted_row
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _job_row(**overrides):
    row = {
        "job_id": "li-1",
        "title": "Data Engineer",
        "company": "Acme",
        "apply_status": "IDLE",
        "posted_at": date(2026, 11, 21),
        "deadline_live": True,
        "verified_gone": False,
    }
    row.update(overrides)
    return row


def _patch_db(monkeypatch, cursor):
    conn = FakeConnection(cursor)
    monkeypatch.setattr("src.protocols.dispatcher.get_db_connection", lambda: conn)
    return conn


def _upserts(cursor):
    return [s for s in cursor.statements if "INSERT INTO scout.lockscreen_pin" in s[0]]


def test_tools_registered_in_all_four_places():
    for name in ("pin_lockscreen_job", "clear_lockscreen_pin"):
        assert name in TOOL_HANDLERS
        assert any(t["name"] == name for t in MCP_TOOLS)
        assert TOOL_SCOPES[name] == SCOPE_APPLY
    pin_entry = next(t for t in MCP_TOOLS if t["name"] == "pin_lockscreen_job")
    assert pin_entry["inputSchema"]["required"] == ["job_id"]
    assert "pinned_via" in pin_entry["inputSchema"]["properties"]
    clear_entry = next(t for t in MCP_TOOLS if t["name"] == "clear_lockscreen_pin")
    assert clear_entry["inputSchema"]["properties"] == {}


class TestPinLockscreenJob:
    def test_success_pins_and_returns_derived_deadline_and_url(self, monkeypatch):
        cur = FakeCursor(job_row=_job_row())
        conn = _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call("pin_lockscreen_job", {"job_id": "li-1"})
        )
        assert result["success"] is True
        assert result["job_id"] == "li-1"
        assert result["title"] == "Data Engineer"
        assert result["company"] == "Acme"
        # Posted Nov 21 + 7 days, end of day local — the same derivation
        # the endpoint uses, so the URL the app loads shows the same date.
        assert result["deadline"] == "2026-11-28T23:59:00-05:00"
        assert result["lockscreen_url"] == (
            "https://jobs.sriomkar.com/lockscreen?job_id=li-1"
        )
        upserts = _upserts(cur)
        assert len(upserts) == 1
        assert upserts[0][1] == ("li-1", "gemini")
        assert conn.commits == 1

    def test_pinned_via_passed_through(self, monkeypatch):
        cur = FakeCursor(job_row=_job_row())
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call(
                "pin_lockscreen_job", {"job_id": "li-1", "pinned_via": "mcp"}
            )
        )
        assert result["success"] is True
        assert _upserts(cur)[0][1] == ("li-1", "mcp")

    def test_unknown_job_writes_nothing(self, monkeypatch):
        cur = FakeCursor(job_row=None)
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call("pin_lockscreen_job", {"job_id": "li-nope"})
        )
        assert result["success"] is False
        assert "not found" in result["error"]
        assert _upserts(cur) == []

    def test_no_posted_date_writes_nothing(self, monkeypatch):
        cur = FakeCursor(job_row=_job_row(posted_at=None, deadline_live=None))
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call("pin_lockscreen_job", {"job_id": "li-1"})
        )
        assert result["success"] is False
        assert "no posted date" in result["error"]
        assert _upserts(cur) == []

    def test_expired_deadline_names_the_deadline(self, monkeypatch):
        cur = FakeCursor(
            job_row=_job_row(posted_at=date(2026, 11, 10), deadline_live=False)
        )
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call("pin_lockscreen_job", {"job_id": "li-1"})
        )
        assert result["success"] is False
        assert "deadline passed" in result["error"]
        assert "2026-11-17" in result["error"]
        assert _upserts(cur) == []

    def test_not_idle_writes_nothing(self, monkeypatch):
        cur = FakeCursor(job_row=_job_row(apply_status="SUBMITTED"))
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call("pin_lockscreen_job", {"job_id": "li-1"})
        )
        assert result["success"] is False
        assert "SUBMITTED" in result["error"]
        assert _upserts(cur) == []

    def test_verified_gone_writes_nothing(self, monkeypatch):
        cur = FakeCursor(job_row=_job_row(verified_gone=True))
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call("pin_lockscreen_job", {"job_id": "li-1"})
        )
        assert result["success"] is False
        assert "verified" in result["error"]
        assert _upserts(cur) == []

    def test_missing_job_id_rejected(self):
        with pytest.raises(ToolArgumentError):
            asyncio.run(dispatch_tool_call("pin_lockscreen_job", {}))


class TestClearLockscreenPin:
    def test_clears_existing_pin(self, monkeypatch):
        cur = FakeCursor(deleted_row={"job_id": "li-1"})
        _patch_db(monkeypatch, cur)
        result = asyncio.run(dispatch_tool_call("clear_lockscreen_pin", {}))
        assert result["success"] is True
        assert result["cleared"] is True
        assert result["cleared_job_id"] == "li-1"
        assert "li-1" in result["message"]

    def test_clearing_with_no_pin_is_not_an_error(self, monkeypatch):
        cur = FakeCursor(deleted_row=None)
        _patch_db(monkeypatch, cur)
        result = asyncio.run(dispatch_tool_call("clear_lockscreen_pin", {}))
        assert result["success"] is True
        assert result["cleared"] is False
        assert result["cleared_job_id"] is None
