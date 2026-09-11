# tests/test_workers.py
from unittest.mock import MagicMock, patch

from src.workers.backfill_worker import run_backfill_batch
from src.workers.pipeline_utils import get_unprocessed_jobs


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
        {"job_id": "job_101", "description": "Senior Data Engineer with Spark", "job_category": "data_engineering"}
    ]
    mock_eval.return_value = {
        "match_score": 90,
        "summary_rationale": "High relevance match."
    }
    mock_update.return_value = True

    summary = run_backfill_batch(batch_size=1, delay_seconds=0.0)

    assert summary["status"] == "completed"
    assert summary["processed_count"] == 1
    assert summary["failed_count"] == 0
    mock_update.assert_called_once_with(
        job_id="job_101",
        match_score=90,
        ai_notes="High relevance match.",
        ai_status="PROCESSED"
    )
