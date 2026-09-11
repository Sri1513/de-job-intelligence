# src/workers/backfill_worker.py
import logging
import time
from typing import Any

from src.engine.analyzer import evaluate_job_fit
from src.workers.pipeline_utils import get_unprocessed_jobs, mark_job_failed, update_job_evaluation

logger = logging.getLogger("de-job-intelligence.backfill")

def run_backfill_batch(batch_size: int = 10, delay_seconds: float = 2.0) -> dict[str, Any]:
    """
    Pulls pending/failed jobs from PostgreSQL, executes LLM evaluations,
    and commits the results with rate pacing.
    """
    jobs = get_unprocessed_jobs(limit=batch_size)
    if not jobs:
        return {
            "status": "idle",
            "processed_count": 0,
            "message": "No pending or failed jobs found in queue."
        }

    processed = 0
    failed = 0

    for idx, job in enumerate(jobs):
        job_id = job["job_id"]
        description = job.get("description", "")
        category = job.get("job_category", "data_engineering")

        try:
            analysis = evaluate_job_fit(description, category_slug=category)
            score = analysis.get("match_score", 0)
            notes = analysis.get("summary_rationale", "")

            # Persist evaluation result
            success = update_job_evaluation(
                job_id=job_id,
                match_score=score,
                ai_notes=notes,
                ai_status="PROCESSED"
            )

            if success:
                processed += 1
            else:
                failed += 1

        except Exception as exc:
            logger.error(f"Error evaluating job {job_id}: {exc}")
            mark_job_failed(job_id, str(exc))
            failed += 1

        # Rate pacing delay between individual API calls
        if idx < len(jobs) - 1:
            time.sleep(delay_seconds)

    return {
        "status": "completed",
        "total_attempted": len(jobs),
        "processed_count": processed,
        "failed_count": failed
    }

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_backfill_batch(batch_size=5, delay_seconds=2.0)
    print(f"Batch execution result: {result}")
