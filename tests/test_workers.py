# tests/test_workers.py
from unittest.mock import MagicMock, patch

from src.workers.backfill_worker import run_backfill_batch
from src.workers.pipeline_utils import get_unprocessed_jobs, update_job_evaluation


def _mock_pipeline_db():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = False
    return mock_conn, mock_cursor


@patch("src.workers.pipeline_utils.get_db_connection")
def test_update_job_evaluation_defaults_unchanged(mock_get_db):
    # Omitted optional params -> exactly today's behavior: fit_score,
    # notes, ai_status only.
    mock_conn, mock_cursor = _mock_pipeline_db()
    mock_get_db.return_value = mock_conn

    assert update_job_evaluation(job_id="job_1", match_score=80, ai_notes="notes") is True
    query, params = mock_cursor.execute.call_args.args
    assert "fit_score = %s" in query
    assert "notes = %s" in query
    assert "ai_status = %s" in query
    assert "sponsorship" not in query
    assert "employment_type" not in query
    assert "metadata" not in query
    assert params == ("80", "notes", "PROCESSED", "job_1")
    mock_conn.commit.assert_called_once()


@patch("src.workers.pipeline_utils.get_db_connection")
def test_update_job_evaluation_with_sponsorship_and_metadata(mock_get_db):
    mock_conn, mock_cursor = _mock_pipeline_db()
    mock_get_db.return_value = mock_conn

    assert update_job_evaluation(
        job_id="job_2",
        match_score=82,
        ai_notes="Senior DE role.",
        ai_status="PROCESSED",
        sponsorship="Not Mentioned",
        employment_type="Contract",
        metadata_updates={"ai_extracted_skills": ["PySpark"], "ai_local_llm": True},
    ) is True
    query, params = mock_cursor.execute.call_args.args
    assert "sponsorship = %s" in query
    assert "employment_type = %s" in query
    assert "metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb" in query
    assert params[3] == "Not Mentioned"
    assert params[4] == "Contract"
    assert params[5] == '{"ai_extracted_skills": ["PySpark"], "ai_local_llm": true}'
    assert params[6] == "job_2"


@patch("src.workers.pipeline_utils.get_db_connection")
def test_update_job_evaluation_empty_metadata_is_noop(mock_get_db):
    mock_conn, mock_cursor = _mock_pipeline_db()
    mock_get_db.return_value = mock_conn

    assert update_job_evaluation(
        job_id="job_3", match_score=70, ai_notes="n", metadata_updates={}
    ) is True
    query, _ = mock_cursor.execute.call_args.args
    assert "metadata" not in query


@patch("src.workers.pipeline_utils.get_db_connection")
def test_update_job_evaluation_db_error_returns_false(mock_get_db):
    mock_conn, mock_cursor = _mock_pipeline_db()
    mock_get_db.return_value = mock_conn
    mock_cursor.execute.side_effect = RuntimeError("boom")

    assert update_job_evaluation(job_id="job_4", match_score=70, ai_notes="n") is False


@patch("src.workers.pipeline_utils.get_db_connection")
def test_get_unprocessed_jobs(mock_get_db):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    mock_cursor.fetchall.return_value = [
        {"job_id": "job_1", "title": "Data Engineer", "ai_status": "PENDING"}
    ]

    results = get_unprocessed_jobs(limit=1)
    assert len(results) == 1
    assert results[0]["job_id"] == "job_1"
    assert results[0]["ai_status"] == "PENDING"


@patch("src.workers.backfill_worker.get_unprocessed_jobs")
@patch("src.workers.backfill_worker.evaluate_job_fit")
@patch("src.workers.backfill_worker.update_job_evaluation")
def test_run_backfill_batch_flow(mock_update, mock_eval, mock_get_jobs):
    mock_get_jobs.return_value = [
        {
            "job_id": "job_101",
            "description": "Senior Data Engineer with Spark",
            "job_category": "data_engineering",
        }
    ]
    mock_eval.return_value = {"match_score": 90, "summary_rationale": "High relevance match."}
    mock_update.return_value = True

    summary = run_backfill_batch(batch_size=1, delay_seconds=0.0)

    assert summary["status"] == "completed"
    assert summary["processed_count"] == 1
    assert summary["failed_count"] == 0
    mock_update.assert_called_once_with(
        job_id="job_101", match_score=90, ai_notes="High relevance match.", ai_status="PROCESSED"
    )


def test_classify_failure_reason_billing():
    # browser_use is not installed in the test env; the classifier is a pure
    # function, so stub the heavy import.
    import sys
    from unittest.mock import MagicMock

    sys.modules.setdefault("browser_use", MagicMock())
    from src.workers.apply_worker import classify_failure_reason

    assert (
        classify_failure_reason(
            {"message": "402 RESOURCE_EXHAUSTED: Your prepayment credits are depleted."}
        )
        == "billing_error"
    )
    # Billing keywords win even when captcha-adjacent words are present.
    assert (
        classify_failure_reason(
            {"message": "Agent was blocked by auth walls or CAPTCHAs. 402 billing error."}
        )
        == "billing_error"
    )


def test_classify_failure_reason_captcha_still_works():
    import sys
    from unittest.mock import MagicMock

    sys.modules.setdefault("browser_use", MagicMock())
    from src.workers.apply_worker import classify_failure_reason

    assert classify_failure_reason({"message": "Blocked by a captcha challenge"}) == "captcha"
    assert classify_failure_reason({"message": "Something went wrong"}) == "agent_error"
