# tests/test_local_llm_queue.py
"""Unit tests for the local-LLM fallback queue (src/engine/local_llm_queue.py).

The DB connection and the local-LLM tier are mocked: no live model calls
and no live Postgres in these tests. Assertions follow the module's real
shapes (evaluator-style summary dict; _process_one_job's keyword args).
"""
from unittest.mock import MagicMock, patch

from src.engine import local_llm_queue
from src.engine.local_llm import LocalLLMError, LocalLLMUnavailableError

JOB1 = {
    "job_id": "li-111",
    "title": "Senior Data Engineer",
    "company": "Acme",
    "description": "Build PySpark pipelines on Databricks.",
    "job_url": "https://example.test/jobs/111",
    "metadata": {},
}
JOB2 = {
    "job_id": "li-222",
    "title": "Data Engineer",
    "company": "Beta",
    "description": "Build Airflow DAGs on Snowflake.",
    "job_url": "https://example.test/jobs/222",
    "metadata": {},
}

SPONSORSHIP_NONE = {
    "mentioned": False,
    "sponsorship": "Not Mentioned",
    "quote": None,
}
SCORE_DATA = {
    "score": 82,
    "matched_skills": ["PySpark"],
    "missing_skills": ["Airflow"],
    "semantic_match": 0.9,
    "skill_match": 0.8,
}


def _mock_db(rows):
    """A get_db_connection mock whose cursor returns `rows` (dict rows,
    which _fetch_jobs handles) for SELECTs."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = rows
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = False
    return mock_conn, mock_cur


def _run_batch(rows, **kwargs):
    mock_conn, mock_cur = _mock_db(rows)
    with patch.object(local_llm_queue, "get_db_connection", return_value=mock_conn), \
         patch.object(local_llm_queue, "update_job_evaluation", return_value=True) as mock_update, \
         patch.object(local_llm_queue, "calculate_local_fit_score", return_value=dict(SCORE_DATA)) as mock_score, \
         patch.object(local_llm_queue, "get_cached_resume", return_value="resume text") as mock_resume, \
         patch.object(local_llm_queue, "notifier") as mock_notifier, \
         patch("time.sleep"):
        result = local_llm_queue.run_local_fallback_batch(**kwargs)
    return result, mock_conn, mock_cur, mock_update, mock_score, mock_resume, mock_notifier


def test_batch_success_writes_back_skills_sponsorship_notes_score():
    with patch.object(local_llm_queue.local_llm, "extract_skills", return_value=["PySpark", "Databricks"]) as mock_extract, \
         patch.object(local_llm_queue.local_llm, "sponsorship_check", return_value=dict(SPONSORSHIP_NONE)) as mock_spons, \
         patch.object(local_llm_queue.local_llm, "ai_notes", return_value="Senior DE role.") as mock_notes:
        result, mock_conn, mock_cur, mock_update, mock_score, mock_resume, mock_notifier = _run_batch([JOB1])

    assert result["status"] == "completed"
    assert result["successful"] == 1
    assert result["failed"] == 0
    assert result["attempted"] == 1
    assert result["results"][0]["job_id"] == "li-111"
    assert result["results"][0]["status"] == "PROCESSED"
    assert result["results"][0]["score"] == 82
    assert result["results"][0]["skills"] == ["PySpark", "Databricks"]
    assert result["results"][0]["sponsorship"] == "Not Mentioned"
    # extract_skills gets (description, vocabulary).
    mock_extract.assert_called_once()
    assert mock_extract.call_args.args[0] == JOB1["description"]
    mock_spons.assert_called_once_with(JOB1["description"])
    mock_notes.assert_called_once_with(JOB1["description"])
    # Deterministic score from the extracted skills + cached resume.
    mock_score.assert_called_once_with(
        resume_text="resume text",
        job_description=JOB1["description"],
        job_title="Senior Data Engineer",
        job_category="data_engineering",
        ai_extracted_skills=["PySpark", "Databricks"],
    )
    # One write-back carrying sponsorship + extracted skills + notes + score.
    mock_update.assert_called_once()
    kwargs = mock_update.call_args.kwargs
    assert kwargs["job_id"] == "li-111"
    assert kwargs["match_score"] == 82
    assert kwargs["ai_notes"] == "Senior DE role."
    assert kwargs["ai_status"] == "PROCESSED"
    assert kwargs["sponsorship"] == "Not Mentioned"
    assert kwargs["metadata_updates"]["ai_extracted_skills"] == ["PySpark", "Databricks"]
    assert kwargs["metadata_updates"]["tailoring_signals"] == ["PySpark", "Databricks"]
    assert kwargs["metadata_updates"]["matched_skills"] == ["PySpark"]
    # employment_type is NOT touched by the queue.
    assert "employment_type" not in kwargs
    mock_notifier.alert_llm_all_down.assert_not_called()


def test_batch_honors_explicit_job_ids():
    with patch.object(local_llm_queue.local_llm, "extract_skills", return_value=[]), \
         patch.object(local_llm_queue.local_llm, "sponsorship_check", return_value=dict(SPONSORSHIP_NONE)), \
         patch.object(local_llm_queue.local_llm, "ai_notes", return_value="notes"):
        result, mock_conn, mock_cur, *_ = _run_batch([JOB2], job_ids=["li-222"])
    assert result["successful"] == 1
    assert result["results"][0]["job_id"] == "li-222"
    # The pending-work query was replaced by the explicit-ids query.
    select_sql = mock_cur.execute.call_args_list[0].args[0]
    assert "job_id = ANY" in select_sql
    assert mock_cur.execute.call_args_list[0].args[1] == (["li-222"],)


def test_batch_one_failure_does_not_stop_batch():
    def extract(description, vocabulary=None):
        if "PySpark" in description:
            return ["PySpark"]
        raise LocalLLMError("checklist retry exhausted")

    with patch.object(local_llm_queue.local_llm, "extract_skills", side_effect=extract), \
         patch.object(local_llm_queue.local_llm, "sponsorship_check", return_value=dict(SPONSORSHIP_NONE)), \
         patch.object(local_llm_queue.local_llm, "ai_notes", return_value="notes"):
        result, mock_conn, mock_cur, mock_update, *_ = _run_batch([JOB1, JOB2])

    assert result["successful"] == 1
    assert result["failed"] == 1
    assert [r["job_id"] for r in result["results"]] == ["li-111", "li-222"]
    assert result["results"][1]["status"] == "FAILED"
    # The failing row was marked FAILED by a minimal UPDATE that touches
    # only ai_status + ai_error — notes and score are preserved (NOT
    # workers.mark_job_failed, which overwrites notes and zeroes score).
    failed_sql = [c.args[0] for c in mock_cur.execute.call_args_list if "ai_status = 'FAILED'" in c.args[0]]
    assert len(failed_sql) == 1
    assert "ai_error" in failed_sql[0]
    assert "ai_notes" not in failed_sql[0]
    assert "ai_match_score" not in failed_sql[0]
    # The successful row was still written back.
    assert mock_update.call_count == 1
    assert mock_update.call_args.kwargs["job_id"] == "li-111"


def test_batch_tier_down_stops_batch_and_alerts_once():
    def extract(description, vocabulary=None):
        raise LocalLLMUnavailableError("HTTP 524")

    with patch.object(local_llm_queue.local_llm, "extract_skills", side_effect=extract) as mock_extract, \
         patch.object(local_llm_queue.local_llm, "sponsorship_check") as mock_spons, \
         patch.object(local_llm_queue.local_llm, "ai_notes") as mock_notes:
        result, mock_conn, mock_cur, mock_update, mock_score, mock_resume, mock_notifier = _run_batch([JOB1, JOB2])

    assert result["status"] == "completed"
    assert result["successful"] == 0
    assert result["failed"] == 0
    assert result["attempted"] == 1
    assert result["local_llm_unavailable"] is True
    assert result["results"][0]["status"] == "TIER_DOWN"
    # Only the first job was attempted; the second row stays untouched.
    assert mock_extract.call_count == 1
    mock_spons.assert_not_called()
    mock_notes.assert_not_called()
    mock_update.assert_not_called()
    mock_score.assert_not_called()
    mock_notifier.alert_llm_all_down.assert_called_once_with(
        task="local-3b-fallback", last_error="HTTP 524"
    )
    # No FAILED marking on tier-down: the rows stay pending for next run.
    failed_sql = [c.args[0] for c in mock_cur.execute.call_args_list if "ai_status = 'FAILED'" in c.args[0]]
    assert failed_sql == []


def test_batch_empty_pending_returns_clean_summary():
    result, *_ = _run_batch([])
    assert result == {
        "status": "success",
        "message": "No pending jobs found.",
        "attempted": 0,
        "successful": 0,
        "failed": 0,
        "results": [],
    }


def test_batch_serial_order_matches_fetch_order():
    seen = []

    def extract(description, vocabulary=None):
        seen.append(description)
        return []

    with patch.object(local_llm_queue.local_llm, "extract_skills", side_effect=extract), \
         patch.object(local_llm_queue.local_llm, "sponsorship_check", return_value=dict(SPONSORSHIP_NONE)), \
         patch.object(local_llm_queue.local_llm, "ai_notes", return_value="notes"):
        result, *_ = _run_batch([JOB2, JOB1])
    # Serial: rows processed in the order the query returned them.
    assert seen == [JOB2["description"], JOB1["description"]]
    assert [r["job_id"] for r in result["results"]] == ["li-222", "li-111"]


def test_batch_dry_run_does_not_write():
    with patch.object(local_llm_queue.local_llm, "extract_skills", return_value=["PySpark"]), \
         patch.object(local_llm_queue.local_llm, "sponsorship_check", return_value=dict(SPONSORSHIP_NONE)), \
         patch.object(local_llm_queue.local_llm, "ai_notes", return_value="notes"):
        result, mock_conn, mock_cur, mock_update, *_ = _run_batch([JOB1], dry_run=True)
    assert result["successful"] == 1
    assert result["results"][0]["status"] == "PROCESSED"
    mock_update.assert_not_called()
    # No FAILED marking either.
    failed_sql = [c.args[0] for c in mock_cur.execute.call_args_list if "ai_status = 'FAILED'" in c.args[0]]
    assert failed_sql == []
