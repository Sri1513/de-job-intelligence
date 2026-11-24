# tests/test_job_milestones.py
"""Mark + list for the lock-screen milestone table (branch
friday/lockscreen-milestone-table).

Gemini (or the owner) marks one of the seven milestones done for a saved
job; the lock-screen page reads scout.job_milestones, so this is how the
check-offs get driven by real events instead of the calendar. Mark
validation (unknown job, unknown key) names the reason and writes
nothing. List returns the full board in page order; missing rows read as
not done except 'saved', which is done by definition. The tools are
registered in all four places (schema manifest, handler registry, scope
map, argument models); mark is write-side-effect (mcp:apply), list is
read-only (mcp:read).
"""
import asyncio
from datetime import datetime

import pytest

from src.protocols.dispatcher import TOOL_HANDLERS, dispatch_tool_call
from src.protocols.schemas import MCP_TOOLS
from src.protocols.scopes import SCOPE_APPLY, SCOPE_READ, TOOL_SCOPES
from src.protocols.validation import ToolArgumentError


class FakeCursor:
    """Statements recorded; fetchone/fetchall dispatch on SQL text."""

    def __init__(self, job_row=None, milestone_rows=None):
        self.job_row = job_row
        self.milestone_rows = list(milestone_rows or [])
        self.statements = []
        self._last_sql = ""

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self._last_sql = sql

    def fetchone(self):
        if "FROM scout.saved_jobs" in self._last_sql:
            return self.job_row
        return None

    def fetchall(self):
        if "FROM scout.job_milestones" in self._last_sql:
            return self.milestone_rows
        return []

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
    row = {"job_id": "li-1", "title": "Data Engineer", "company": "Acme"}
    row.update(overrides)
    return row


def _milestone_row(key, done=True, done_at=None, marked_by="gemini"):
    return {
        "milestone_key": key,
        "done": done,
        "done_at": done_at,
        "marked_by": marked_by,
    }


def _patch_db(monkeypatch, cursor):
    conn = FakeConnection(cursor)
    monkeypatch.setattr("src.protocols.dispatcher.get_db_connection", lambda: conn)
    return conn


def _upserts(cursor):
    return [
        s for s in cursor.statements if "INSERT INTO scout.job_milestones" in s[0]
    ]


def test_tools_registered_in_all_four_places():
    for name, scope in (
        ("mark_job_milestone", SCOPE_APPLY),
        ("list_job_milestones", SCOPE_READ),
    ):
        assert name in TOOL_HANDLERS
        assert any(t["name"] == name for t in MCP_TOOLS)
        assert TOOL_SCOPES[name] == scope
    mark_entry = next(t for t in MCP_TOOLS if t["name"] == "mark_job_milestone")
    assert mark_entry["inputSchema"]["required"] == ["job_id", "milestone"]
    assert mark_entry["inputSchema"]["properties"]["done"]["default"] is True
    list_entry = next(t for t in MCP_TOOLS if t["name"] == "list_job_milestones")
    assert list_entry["inputSchema"]["required"] == ["job_id"]


class TestMarkJobMilestone:
    def test_success_marks_and_returns_board(self, monkeypatch):
        # The list call inside mark reads the table after the upsert:
        # the fake returns the post-upsert rows, so the board shows
        # 'submitted' done.
        cur = FakeCursor(
            job_row=_job_row(),
            milestone_rows=[_milestone_row("saved"), _milestone_row("submitted")],
        )
        conn = _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call(
                "mark_job_milestone", {"job_id": "li-1", "milestone": "submitted"}
            )
        )
        assert result["success"] is True
        assert result["job_id"] == "li-1"
        assert result["title"] == "Data Engineer"
        assert result["company"] == "Acme"
        assert result["milestone"] == "submitted"
        assert result["done"] is True
        assert result["marked_by"] == "gemini"
        assert "Application submitted" in result["message"]
        assert result["lockscreen_url"] == (
            "https://jobs.sriomkar.com/lockscreen?job_id=li-1"
        )
        assert len(result["milestones"]) == 7
        board = {m["key"]: m for m in result["milestones"]}
        assert board["submitted"]["done"] is True
        assert board["saved"]["done"] is True
        assert board["fit_check"]["done"] is False
        upserts = _upserts(cur)
        assert len(upserts) == 1
        params = upserts[0][1]
        assert params[0] == "li-1"
        assert params[1] == "submitted"
        assert params[2] is True
        assert isinstance(params[3], datetime)
        assert params[4] == "gemini"
        assert conn.commits == 1

    def test_reopen_clears_done_at(self, monkeypatch):
        cur = FakeCursor(job_row=_job_row())
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call(
                "mark_job_milestone",
                {"job_id": "li-1", "milestone": "submitted", "done": False},
            )
        )
        assert result["success"] is True
        assert result["done"] is False
        params = _upserts(cur)[0][1]
        assert params[2] is False
        assert params[3] is None
        assert "not done" in result["message"]

    def test_marked_by_passed_through(self, monkeypatch):
        cur = FakeCursor(job_row=_job_row())
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call(
                "mark_job_milestone",
                {"job_id": "li-1", "milestone": "fit_check", "marked_by": "owner"},
            )
        )
        assert result["success"] is True
        assert _upserts(cur)[0][1][4] == "owner"
        assert result["marked_by"] == "owner"

    def test_unknown_milestone_writes_nothing(self, monkeypatch):
        cur = FakeCursor(job_row=_job_row())
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call(
                "mark_job_milestone", {"job_id": "li-1", "milestone": "nope"}
            )
        )
        assert result["success"] is False
        assert "Unknown milestone" in result["error"]
        assert "submitted" in result["error"]
        assert _upserts(cur) == []

    def test_unknown_job_writes_nothing(self, monkeypatch):
        cur = FakeCursor(job_row=None)
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call(
                "mark_job_milestone", {"job_id": "li-nope", "milestone": "saved"}
            )
        )
        assert result["success"] is False
        assert "not found" in result["error"]
        assert _upserts(cur) == []

    def test_missing_job_id_rejected(self):
        with pytest.raises(ToolArgumentError):
            asyncio.run(
                dispatch_tool_call("mark_job_milestone", {"milestone": "saved"})
            )

    def test_missing_milestone_rejected(self):
        with pytest.raises(ToolArgumentError):
            asyncio.run(dispatch_tool_call("mark_job_milestone", {"job_id": "li-1"}))


class TestListJobMilestones:
    def test_returns_board_in_page_order(self, monkeypatch):
        cur = FakeCursor(
            job_row=_job_row(),
            milestone_rows=[
                _milestone_row(
                    "fit_check", done_at=datetime(2026, 11, 23, 12, 0, 0)
                ),
                _milestone_row("submitted", done=False),
            ],
        )
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call("list_job_milestones", {"job_id": "li-1"})
        )
        assert result["success"] is True
        assert [m["key"] for m in result["milestones"]] == [
            "saved",
            "fit_check",
            "package_built",
            "package_assets",
            "submitted",
            "follow_up",
            "reply_expected",
        ]
        board = {m["key"]: m for m in result["milestones"]}
        assert board["saved"]["done"] is True  # done by definition
        assert board["fit_check"]["done"] is True
        assert board["fit_check"]["label"] == "Fit Check Passed"
        assert board["fit_check"]["done_at"] == "2026-11-23T12:00:00"
        assert board["fit_check"]["marked_by"] == "gemini"
        assert board["submitted"]["done"] is False
        assert board["package_built"]["done"] is False

    def test_tuple_rows_read_the_same(self, monkeypatch):
        # psycopg returns dict rows in prod, but the reader also accepts
        # plain tuples — cover that branch.
        cur = FakeCursor(
            job_row=_job_row(),
            milestone_rows=[("saved", True, None, "ingest")],
        )
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call("list_job_milestones", {"job_id": "li-1"})
        )
        assert result["success"] is True
        board = {m["key"]: m for m in result["milestones"]}
        assert board["saved"]["done"] is True
        assert board["saved"]["marked_by"] == "ingest"

    def test_unknown_job(self, monkeypatch):
        cur = FakeCursor(job_row=None)
        _patch_db(monkeypatch, cur)
        result = asyncio.run(
            dispatch_tool_call("list_job_milestones", {"job_id": "li-nope"})
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_missing_job_id_rejected(self):
        with pytest.raises(ToolArgumentError):
            asyncio.run(dispatch_tool_call("list_job_milestones", {}))
