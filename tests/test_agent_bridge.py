# tests/test_agent_bridge.py
"""Agent staging bridge: validation, dedupe mapping, evaluation, metadata.

The bridge must work for ANY MCP client, not just Friday: inputs are plain
job objects, outcomes are explicit per row, and run_batch_ingestion is
untouched (its own suites cover it).
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

from src.engine.agent_bridge import MAX_AGENT_JOBS, stage_agent_jobs_workflow
from src.engine.pipeline_utils import stage_raw_jobs
from src.protocols.dispatcher import TOOL_HANDLERS, dispatch_tool_call
from src.protocols.schemas import MCP_TOOLS

LINKEDIN_JOB = {
    "title": "Data Engineer",
    "company": "MeeBoss",
    "job_url": "https://www.linkedin.com/jobs/view/4485232781",
    "location": "Remote",
    "source": "linkedin",
    "description": "We are hiring a Data Engineer to build and operate our lakehouse platform. "
    "Requirements: 5+ years of data engineering, strong SQL and Python, "
    "experience with Airflow, dbt, and cloud data warehouses.",
    "workplace": "Remote",
    "duration": "Not stated",
}


def _mock_stage_with(staged_ids):
    mock_stage = MagicMock()
    mock_stage.return_value = {
        "staged_jobs": [{"job_id": jid, "title": "t", "company": "c"} for jid in staged_ids],
        "saved": len(staged_ids),
        "updated": 0,
        "skipped": 0,
    }
    return mock_stage


@patch("src.engine.agent_bridge.get_db_connection")
@patch("src.engine.agent_bridge.run_backfill_batch", return_value={"successful": 1})
@patch("src.engine.agent_bridge.stage_raw_jobs")
def test_workflow_stages_valid_reports_invalid_and_evaluates_touched_ids(
    mock_stage, mock_eval, mock_db
):
    mock_stage.side_effect = lambda conn, jobs, job_category, default_source: {
        "staged_jobs": [{"job_id": "li-4485232781", "title": "t", "company": "c"}],
        "saved": 1,
        "updated": 0,
        "skipped": 0,
    }
    mock_db.return_value.__enter__.return_value = MagicMock()
    jobs = [
        LINKEDIN_JOB,
        {"title": "Data Engineer", "company": "No URL Co"},
        {"company": "No Title Co", "job_url": "https://example.com/1"},
        {"title": "Data Engineer", "company": "Bad URL Co", "job_url": "not-a-url"},
    ]
    result = stage_agent_jobs_workflow(jobs)
    assert result["status"] == "success"
    assert result["staged"] == ["li-4485232781"]
    assert len(result["invalid"]) == 3
    assert result["evaluated"] == 1
    # Evaluation targets ONLY the newly staged ids (evaluator variant).
    _, kwargs = mock_eval.call_args
    assert kwargs["job_ids"] == ["li-4485232781"]
    # Extras survive into the staged row's metadata.
    staged_job = mock_stage.call_args.kwargs["jobs"][0]
    assert staged_job["metadata"]["workplace"] == "Remote"
    assert staged_job["metadata"]["duration"] == "Not stated"
    assert staged_job["metadata"]["found_via"] == "agent"
    assert staged_job["metadata"]["agent_portal"] == "linkedin"


@patch("src.engine.agent_bridge.get_db_connection")
@patch("src.engine.agent_bridge.run_backfill_batch")
@patch("src.engine.agent_bridge.stage_raw_jobs")
def test_workflow_skips_evaluation_when_disabled(mock_stage, mock_eval, mock_db):
    mock_stage.return_value = {
        "staged_jobs": [{"job_id": "li-4485232781", "title": "t", "company": "c"}],
        "saved": 1,
        "updated": 0,
        "skipped": 0,
    }
    mock_db.return_value.__enter__.return_value = MagicMock()
    result = stage_agent_jobs_workflow([LINKEDIN_JOB], run_evaluation=False)
    assert result["evaluated"] == 0
    mock_eval.assert_not_called()


@patch("src.engine.agent_bridge.stage_raw_jobs")
def test_workflow_rejects_oversize_batch(mock_stage):
    result = stage_agent_jobs_workflow([dict(LINKEDIN_JOB) for _ in range(MAX_AGENT_JOBS + 1)])
    assert result["status"] == "error"
    mock_stage.assert_not_called()


@patch("src.engine.agent_bridge.get_db_connection")
@patch("src.engine.agent_bridge.run_backfill_batch", side_effect=RuntimeError("groq down"))
@patch("src.engine.agent_bridge.stage_raw_jobs")
def test_workflow_evaluation_failure_keeps_staging(mock_stage, mock_eval, mock_db):
    mock_stage.return_value = {
        "staged_jobs": [{"job_id": "li-4485232781", "title": "t", "company": "c"}],
        "saved": 1,
        "updated": 0,
        "skipped": 0,
    }
    mock_db.return_value.__enter__.return_value = MagicMock()
    result = stage_agent_jobs_workflow([LINKEDIN_JOB])
    assert result["status"] == "success"
    assert result["staged"] == ["li-4485232781"]
    assert "groq down" in result["evaluation_error"]


def _mock_conn_new_row():
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


@patch("src.engine.pipeline_utils.calculate_local_fit_score")
@patch("src.engine.pipeline_utils.get_cached_resume", return_value="data engineer resume")
def test_stage_raw_jobs_preserves_agent_metadata_computed_keys_win(_resume, mock_fit):
    mock_fit.return_value = {
        "matched_skills": ["Python"],
        "missing_skills": [],
        "semantic_match": 0,
        "skill_match": 0,
        "score": 42,
    }
    conn, cur = _mock_conn_new_row()
    job = dict(LINKEDIN_JOB)
    job["metadata"] = {
        "workplace": "Remote",
        "duration": "12 months",
        "source": "bogus-should-lose",
    }
    stats = stage_raw_jobs(conn, [job])
    assert stats["saved"] == 1
    insert_calls = [c for c in cur.execute.call_args_list if "INSERT INTO saved_jobs" in str(c.args[0])]
    assert len(insert_calls) == 1
    stored = json.loads(insert_calls[0].args[1][12])
    assert stored["workplace"] == "Remote"
    assert stored["duration"] == "12 months"
    assert stored["source"] == "LinkedIn"  # computed mapping wins over caller metadata
    assert stored["locations"] == [
        {"city": "Remote", "url": "https://www.linkedin.com/jobs/view/4485232781"}
    ]
    assert stored["matched_skills"] == ["Python"]


def test_tool_registered_and_schema_requires_jobs():
    assert "stage_agent_jobs" in TOOL_HANDLERS
    entry = next(t for t in MCP_TOOLS if t["name"] == "stage_agent_jobs")
    assert entry["inputSchema"]["required"] == ["jobs"]
    item_required = entry["inputSchema"]["properties"]["jobs"]["items"]["required"]
    assert item_required == ["title", "company", "job_url"]


@patch("src.protocols.dispatcher.stage_agent_jobs_workflow")
def test_dispatch_routes_to_bridge(mock_workflow):
    mock_workflow.return_value = {"status": "success", "staged": ["li-4485232781"]}
    result = asyncio.run(
        dispatch_tool_call("stage_agent_jobs", {"jobs": [LINKEDIN_JOB], "run_evaluation": False})
    )
    assert result["staged"] == ["li-4485232781"]
    _, kwargs = mock_workflow.call_args
    assert kwargs["run_evaluation"] is False


def _mock_refresh_cursor(mock_db, row):
    conn = mock_db.return_value.__enter__.return_value
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = row
    return cur


@patch("src.engine.agent_bridge.get_db_connection")
@patch("src.engine.agent_bridge.run_backfill_batch", return_value={"successful": 1})
@patch("src.engine.agent_bridge.stage_raw_jobs")
def test_workflow_refreshes_already_present_rows_and_evaluates_them(
    mock_stage, mock_eval, mock_db
):
    mock_stage.return_value = {"staged_jobs": [], "saved": 0, "updated": 1, "skipped": 0}
    cur = _mock_refresh_cursor(
        mock_db,
        {
            "job_category": "friday_engine",
            "metadata": {
                "locations": [
                    {"city": "Remote", "url": "https://www.linkedin.com/jobs/view/4485232781"}
                ]
            },
            "sponsorship": None,
        },
    )
    job = dict(LINKEDIN_JOB)
    job["sponsorship"] = "No Sponsorship"
    result = stage_agent_jobs_workflow([job])
    assert result["staged"] == []
    assert result["already_present"] == ["li-4485232781"]
    assert result["refreshed"] == ["li-4485232781"]
    assert result["evaluated"] == 1
    # Evaluation covers staged AND refreshed ids.
    _, kwargs = mock_eval.call_args
    assert kwargs["job_ids"] == ["li-4485232781"]
    updates = [c for c in cur.execute.call_args_list if "UPDATE saved_jobs" in str(c.args[0])]
    assert len(updates) == 1
    sql, params = updates[0].args[0], updates[0].args[1]
    assert "job_category = %s" in sql
    assert "metadata = %s" in sql
    assert "sponsorship = %s" in sql
    assert params[0] == "data_engineering"
    stored = json.loads(params[1])
    assert stored["workplace"] == "Remote"  # caller detail merged
    assert stored["duration"] == "Not stated"
    assert stored["found_via"] == "agent"
    assert stored["locations"] == [  # pipeline-computed key never overwritten
        {"city": "Remote", "url": "https://www.linkedin.com/jobs/view/4485232781"}
    ]
    assert params[2] == job["sponsorship"]
    assert params[3] == "li-4485232781"


@patch("src.engine.agent_bridge.get_db_connection")
@patch("src.engine.agent_bridge.run_backfill_batch")
@patch("src.engine.agent_bridge.stage_raw_jobs")
def test_workflow_refresh_skips_unchanged_rows(mock_stage, mock_eval, mock_db):
    mock_stage.return_value = {"staged_jobs": [], "saved": 0, "updated": 0, "skipped": 1}
    cur = _mock_refresh_cursor(
        mock_db,
        {
            "job_category": "data_engineering",
            "metadata": {
                "workplace": "Remote",
                "duration": "Not stated",
                "found_via": "agent",
                "agent_portal": "linkedin",
            },
            "sponsorship": None,
        },
    )
    result = stage_agent_jobs_workflow([LINKEDIN_JOB])
    assert result["refreshed"] == []
    updates = [c for c in cur.execute.call_args_list if "UPDATE saved_jobs" in str(c.args[0])]
    assert updates == []
    mock_eval.assert_not_called()


@patch("src.engine.agent_bridge.get_db_connection")
@patch("src.engine.agent_bridge.run_backfill_batch", side_effect=RuntimeError("groq down"))
@patch("src.engine.agent_bridge.stage_raw_jobs")
def test_workflow_refresh_evaluation_failure_keeps_staging(mock_stage, mock_eval, mock_db):
    mock_stage.return_value = {"staged_jobs": [], "saved": 0, "updated": 1, "skipped": 0}
    _mock_refresh_cursor(
        mock_db,
        {"job_category": "friday_engine", "metadata": {}, "sponsorship": None},
    )
    result = stage_agent_jobs_workflow([LINKEDIN_JOB])
    assert result["status"] == "success"
    assert result["refreshed"] == ["li-4485232781"]
    assert "groq down" in result["evaluation_error"]


@patch("src.engine.agent_bridge.get_db_connection")
@patch("src.engine.agent_bridge.run_backfill_batch", return_value={"successful": 1})
@patch("src.engine.agent_bridge.stage_raw_jobs")
def test_workflow_refresh_keeps_long_sponsorship_out_of_varchar_column(
    mock_stage, mock_eval, mock_db
):
    mock_stage.return_value = {"staged_jobs": [], "saved": 0, "updated": 1, "skipped": 0}
    cur = _mock_refresh_cursor(
        mock_db,
        {"job_category": "friday_engine", "metadata": {}, "sponsorship": None},
    )
    job = dict(LINKEDIN_JOB)
    job["sponsorship"] = (
        "No C2C, 1099, or visa sponsorship/transfer candidates will be considered."
    )
    result = stage_agent_jobs_workflow([job])
    assert result["refreshed"] == ["li-4485232781"]
    updates = [c for c in cur.execute.call_args_list if "UPDATE saved_jobs" in str(c.args[0])]
    assert len(updates) == 1
    sql, params = updates[0].args[0], updates[0].args[1]
    assert "sponsorship = %s" not in sql  # 76 chars would not fit varchar(50)
    stored = json.loads(params[1])
    assert stored["sponsorship_note"] == job["sponsorship"]  # full wording preserved
    assert params[0] == "data_engineering"
