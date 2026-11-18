"""Sprint 4, Lesson 4: run_tailoring_graph as an MCP tool.

The graph becomes callable through the dispatcher behind the
TAILORING_GRAPH_ENABLED flag (default off). The old
prepare_job_tailoring_prompt tool is untouched.
"""

import asyncio
from unittest.mock import patch

import pytest

from src.protocols.app import _TIMEOUT_EXEMPT_TOOLS
from src.protocols.dispatcher import TOOL_HANDLERS, dispatch_tool_call
from src.protocols.schemas import MCP_TOOLS
from src.protocols.scopes import SCOPE_READ, TOOL_SCOPES
from src.protocols.validation import ToolArgumentError

FAKE_STATE = {
    "job_id": "x",
    "bundle": {},
    "selected_bullets": {"job1": ["Built pipelines with PySpark"]},
    "draft": "Sri Omkar D\n- Built pipelines with PySpark",
    "draft_attempts": 1,
    "verification": {"passed": True, "failures": []},
    "error": None,
}


def test_tool_registered_schema_scope_and_timeout_exemption():
    assert "run_tailoring_graph" in TOOL_HANDLERS
    entry = next(t for t in MCP_TOOLS if t["name"] == "run_tailoring_graph")
    assert entry["inputSchema"]["required"] == ["job_id"]
    assert TOOL_SCOPES["run_tailoring_graph"] == SCOPE_READ
    assert "run_tailoring_graph" in _TIMEOUT_EXEMPT_TOOLS


def test_flag_off_returns_disabled_without_running_graph(monkeypatch):
    monkeypatch.delenv("TAILORING_GRAPH_ENABLED", raising=False)
    with patch("src.protocols.dispatcher.run_tailoring_graph") as mock_graph:
        result = asyncio.run(dispatch_tool_call("run_tailoring_graph", {"job_id": "x"}))
    mock_graph.assert_not_called()
    assert result["success"] is False
    assert "disabled" in result["error"]


def test_flag_on_returns_draft_and_verification(monkeypatch):
    monkeypatch.setenv("TAILORING_GRAPH_ENABLED", "1")
    with patch("src.protocols.dispatcher.run_tailoring_graph", return_value=FAKE_STATE):
        result = asyncio.run(dispatch_tool_call("run_tailoring_graph", {"job_id": "x"}))
    assert result["success"] is True
    assert result["draft"] == FAKE_STATE["draft"]
    assert result["selected_bullets"] == FAKE_STATE["selected_bullets"]
    assert result["verification"] == {"passed": True, "failures": []}
    assert result["draft_attempts"] == 1


def test_graph_error_maps_to_failure(monkeypatch):
    monkeypatch.setenv("TAILORING_GRAPH_ENABLED", "true")
    failed = {**FAKE_STATE, "error": "retrieve failed", "draft": None}
    with patch("src.protocols.dispatcher.run_tailoring_graph", return_value=failed):
        result = asyncio.run(dispatch_tool_call("run_tailoring_graph", {"job_id": "x"}))
    assert result["success"] is False
    assert result["error"] == "retrieve failed"


def test_missing_job_id_rejected():
    with pytest.raises(ToolArgumentError):
        asyncio.run(dispatch_tool_call("run_tailoring_graph", {}))
