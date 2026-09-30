# src/workers/apply_worker.py
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from src.core.database import get_db_connection
from src.engine.browser_agent import autofill_job_application

logger = logging.getLogger("de-job-intelligence.apply_worker")


def get_queued_apply_jobs(limit: int = 5) -> list[dict[str, Any]]:
    """Fetches jobs flagged as QUEUED for automated application."""
    query = """
    SELECT job_id, title, company, job_url 
    FROM saved_jobs
    WHERE apply_status = 'QUEUED'
    ORDER BY saved_at ASC NULLS LAST
    LIMIT %s;
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (limit,))
            rows = cur.fetchall()
            return [dict(r) for r in rows]


def update_apply_status(
    job_id: str,
    status: str,
    screenshot_path: str | None = None,
    notes: str | None = None,
):
    """Updates job application status, timestamp, and review artifacts in PostgreSQL."""
    query = """
    UPDATE saved_jobs
    SET 
        apply_status = %s,
        applied_at = %s,
        review_screenshot_path = %s,
        apply_notes = %s
    WHERE job_id = %s;
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                query,
                (
                    status,
                    datetime.now(timezone.utc)
                    if status in ("PENDING_REVIEW", "SUBMITTED")
                    else None,
                    screenshot_path,
                    notes,
                    job_id,
                ),
            )
        conn.commit()


async def process_apply_queue(limit: int = 3, headless: bool = True) -> list[dict[str, Any]]:
    """Polls and runs the browser autofill agent across queued applications."""
    jobs = get_queued_apply_jobs(limit=limit)
    if not jobs:
        logger.info("No applications in QUEUED status.")
        return []

    logger.info("Found %d queued application(s) to process.", len(jobs))
    processed_results = []

    for job in jobs:
        job_id = job["job_id"]
        job_url = job["job_url"]
        logger.info("Processing application: %s (%s) - %s", job["title"], job["company"], job_url)

        # Transition status to IN_PROGRESS so workers do not duplicate effort
        update_apply_status(job_id=job_id, status="IN_PROGRESS")

        # In src/workers/apply_worker.py inside process_application()

        result = await autofill_job_application(
            job_url=job["job_url"],
            job_id=job["job_id"],
            headless=headless,
        )

        new_status = result.get("status", "FAILED")
        notes = result.get("message") or result.get("error") or "Processed by apply worker."
        screenshot_path = result.get("screenshot_path")

        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE saved_jobs
                SET apply_status = %s,
                    apply_notes = %s,
                    review_screenshot_path = COALESCE(%s, review_screenshot_path),
                    applied_at = CURRENT_TIMESTAMP
                WHERE job_id = %s;
                """,
                (new_status, notes, screenshot_path, job["job_id"]),
            )
            conn.commit()

        logger.info("Application job_id=%s updated to status=%s", job["job_id"], new_status)

        update_apply_status(
            job_id=job_id,
            status=result["status"],
            screenshot_path=result.get("screenshot_path"),
            notes=result.get("message") or result.get("error"),
        )
        logger.info("Application job_id=%s updated to status=%s", job_id, result["status"])
        processed_results.append(result)

    return processed_results


async def run_worker_daemon(poll_interval: int = 30, limit: int = 5, headless: bool = True):
    """Continuously polls PostgreSQL for QUEUED applications and processes them."""
    logger.info(
        "Apply worker daemon started (poll_interval=%ds, limit=%d, headless=%s)",
        poll_interval,
        limit,
        headless,
    )

    while True:
        try:
            processed = await process_apply_queue(limit=limit, headless=headless)
            if processed:
                logger.info("Batch completed: processed %d job(s).", len(processed))
            else:
                logger.debug("Queue empty. Sleeping for %ds...", poll_interval)
        except Exception as exc:
            logger.error("Unhandled error in worker cycle: %s", exc, exc_info=True)

        await asyncio.sleep(poll_interval)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Read container environment variables
    is_headless = os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes")
    poll_delay = int(os.getenv("POLL_INTERVAL", "30"))
    batch_limit = int(os.getenv("BATCH_LIMIT", "5"))

    try:
        asyncio.run(
            run_worker_daemon(
                poll_interval=poll_delay,
                limit=batch_limit,
                headless=is_headless,
            )
        )
    except (KeyboardInterrupt, SystemExit):
        logger.info("Worker shutdown requested. Exiting cleanly.")
