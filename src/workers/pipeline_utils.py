# src/workers/pipeline_utils.py
import logging
from typing import List, Dict, Any
from src.core.database import get_db_connection

logger = logging.getLogger("de-job-intelligence.pipeline_utils")

def get_unprocessed_jobs(limit: int = 10) -> List[Dict[str, Any]]:
    """
    Fetches records needing evaluation (PENDING or FAILED status).
    Orders by saved_at descending.
    """
    query = """
        SELECT 
            job_id, title, company, location, is_remote, job_url, 
            description, job_category, 
            fit_score AS match_score, 
            notes AS ai_notes, 
            ai_status
        FROM saved_jobs
        WHERE ai_status IN ('PENDING', 'FAILED') OR ai_status IS NULL
        ORDER BY saved_at DESC
        LIMIT %s;
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (limit,))
            return [dict(row) for row in cur.fetchall()]

def update_job_evaluation(
    job_id: str,
    match_score: int,
    ai_notes: str,
    ai_status: str = "PROCESSED"
) -> bool:
    """
    Commits LLM evaluation scores and notes back to PostgreSQL.
    """
    query = """
        UPDATE saved_jobs
        SET fit_score = %s,
            notes = %s,
            ai_status = %s
        WHERE job_id = %s;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (str(match_score), ai_notes, ai_status, job_id))
            conn.commit()
        return True
    except Exception as exc:
        logger.error(f"Failed to update job {job_id}: {exc}")
        return False

def mark_job_failed(job_id: str, error_message: str) -> None:
    """Marks a job as FAILED to prevent worker deadlocks."""
    update_job_evaluation(
        job_id=job_id,
        match_score=0,
        ai_notes=f"Evaluation Error: {error_message}",
        ai_status="FAILED"
    )