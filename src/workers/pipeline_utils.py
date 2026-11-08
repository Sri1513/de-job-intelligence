# src/workers/pipeline_utils.py
import json
import logging
from typing import Any

from src.core.database import get_db_connection

logger = logging.getLogger("de-job-intelligence.pipeline_utils")


def get_unprocessed_jobs(limit: int = 10) -> list[dict[str, Any]]:
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
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, (limit,))
        return [dict(row) for row in cur.fetchall()]


def update_job_evaluation(
    job_id: str,
    match_score: int,
    ai_notes: str,
    ai_status: str = "PROCESSED",
    sponsorship: str | None = None,
    employment_type: str | None = None,
    metadata_updates: dict[str, Any] | None = None,
) -> bool:
    """
    Commits LLM evaluation scores and notes back to PostgreSQL.

    The optional params are backward-compatible: when omitted the
    function behaves exactly as before (fit_score, notes, ai_status
    only). Callers that need more — the local-LLM fallback queue
    persists sponsorship and merges metadata (ai_extracted_skills,
    tailoring_signals, score breakdown) through this same write path
    instead of duplicating SQL. metadata_updates is merged into the
    row's existing metadata with PostgreSQL jsonb || (new keys win).
    """
    set_clauses = ["fit_score = %s", "notes = %s", "ai_status = %s"]
    params: list[Any] = [str(match_score), ai_notes, ai_status]
    if sponsorship is not None:
        set_clauses.append("sponsorship = %s")
        params.append(sponsorship)
    if employment_type is not None:
        set_clauses.append("employment_type = %s")
        params.append(employment_type)
    if metadata_updates:
        set_clauses.append("metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb")
        params.append(json.dumps(metadata_updates))
    query = f"""
        UPDATE saved_jobs
        SET {", ".join(set_clauses)}
        WHERE job_id = %s;
    """
    params.append(job_id)
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
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
        ai_status="FAILED",
    )
