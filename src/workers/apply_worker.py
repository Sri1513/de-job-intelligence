# src/workers/apply_worker.py
"""Apply queue worker: claims QUEUED jobs and runs the browser autofill agent.

Claiming is atomic (SELECT ... FOR UPDATE SKIP LOCKED + UPDATE in one
transaction) so multiple workers never process the same job. Stale claims
(IN_PROGRESS older than STALE_CLAIM_MINUTES) are automatically reclaimed.
Failed jobs are retried with backoff up to MAX_ATTEMPTS, then parked as
FAILED with a failure_reason taxonomy for the dashboard.
"""

import asyncio
import logging
import os
import socket
from typing import Any

from psycopg.errors import UndefinedColumn

from src.core.database import get_db_connection
from src.engine.browser_agent import (
    STATUS_FAILED,
    STATUS_NEEDS_HUMAN,
    STATUS_PENDING_REVIEW,
    STATUS_SUBMITTED,
    autofill_job_application,
)

logger = logging.getLogger("de-job-intelligence.apply_worker")

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
MAX_ATTEMPTS = 3
STALE_CLAIM_MINUTES = 30
RETRY_BASE_MINUTES = 5

# Failure taxonomy surfaced to the dashboard (failure_reason column).
FAILURE_REASONS = (
    "login_wall",
    "captcha",
    "session_expired",
    "field_unmapped",
    "upload_failed",
    "rate_limited",
    "billing_error",
    "agent_error",
    "unknown",
)

# Keywords identifying a depleted/denied LLM billing account. Checked first:
# a 402 must never be misread as a captcha or retried on a 5-minute backoff.
_BILLING_KEYWORDS = (
    "402",
    "resource_exhausted",
    "prepayment",
    "billing",
    "quota",
    "credits are depleted",
    "insufficient_quota",
)


def classify_failure_reason(
    result: dict[str, Any] | None, exc: BaseException | None = None
) -> str:
    """Maps a result dict / exception to the failure_reason taxonomy."""
    text = ""
    if result:
        text += " ".join(str(result.get(k) or "") for k in ("message", "error", "failure_reason"))
    if exc is not None:
        text += f" {type(exc).__name__}: {exc}"
    text = text.lower()
    if any(k in text for k in _BILLING_KEYWORDS):
        return "billing_error"
    if any(k in text for k in ("captcha", "turnstile", "challenge")):
        return "captcha"
    if "session_expired" in text or ("session" in text and "expir" in text):
        return "session_expired"
    if any(k in text for k in ("login", "sign in", "signin", "authwall", "unauthorized")):
        return "login_wall"
    if "rate" in text or "429" in text or "too many requests" in text:
        return "rate_limited"
    if "upload" in text:
        return "upload_failed"
    if "field" in text and ("unmapped" in text or "not found" in text):
        return "field_unmapped"
    if text.strip():
        return "agent_error"
    return "unknown"


def claim_queued_jobs(
    limit: int = 5,
    worker_id: str = WORKER_ID,
    stale_after_minutes: int = STALE_CLAIM_MINUTES,
) -> list[dict[str, Any]]:
    """Atomically claims QUEUED jobs (plus stale IN_PROGRESS) for this worker.

    Uses SELECT ... FOR UPDATE SKIP LOCKED inside a single transaction, then
    marks the rows IN_PROGRESS with this worker's id and claim timestamp.
    Also reclaims jobs whose claim went stale (crashed worker) and picks up
    jobs whose next_retry_at backoff has elapsed.

    Requires columns claimed_at, claimed_by, attempts, next_retry_at
    (see MIGRATION_NOTES.md); falls back to a simpler atomic claim when the
    migration has not been applied yet.
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT job_id, title, company, job_url,
                           COALESCE(attempts, 0) AS attempts
                    FROM saved_jobs
                    WHERE (apply_status = 'QUEUED'
                           AND (next_retry_at IS NULL OR next_retry_at <= NOW()))
                       OR (apply_status = 'IN_PROGRESS'
                           AND (claimed_at IS NULL
                                OR claimed_at < NOW() - (%s * INTERVAL '1 minute')))
                    ORDER BY saved_at ASC NULLS LAST
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED;
                    """,
                    (stale_after_minutes, limit),
                )
                rows = [dict(r) for r in cur.fetchall()]
                if rows:
                    cur.execute(
                        """
                        UPDATE saved_jobs
                        SET apply_status = 'IN_PROGRESS',
                            claimed_at = NOW(),
                            claimed_by = %s
                        WHERE job_id = ANY(%s);
                        """,
                        (worker_id, [r["job_id"] for r in rows]),
                    )
            conn.commit()
        return rows
    except UndefinedColumn:
        logger.warning(
            "Lease/retry columns missing on saved_jobs; using legacy claim. "
            "Apply the SQL in MIGRATION_NOTES.md to enable leases and backoff."
        )
        return _claim_legacy(limit)


def _claim_legacy(limit: int) -> list[dict[str, Any]]:
    """Pre-migration atomic claim: no leases, no retry backoff."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, title, company, job_url
                FROM saved_jobs
                WHERE apply_status = 'QUEUED'
                ORDER BY saved_at ASC NULLS LAST
                LIMIT %s
                FOR UPDATE SKIP LOCKED;
                """,
                (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            if rows:
                cur.execute(
                    "UPDATE saved_jobs SET apply_status = 'IN_PROGRESS' "
                    "WHERE job_id = ANY(%s);",
                    ([r["job_id"] for r in rows],),
                )
        conn.commit()
    return rows


def finalize_job(
    job_id: str,
    result: dict[str, Any] | None,
    attempts: int = 0,
) -> None:
    """Writes the single, authoritative post-run update for a job.

    - SUBMITTED       -> terminal success; applied_at set (only here).
    - PENDING_REVIEW  -> waiting on human; screenshot preserved.
    - NEEDS_HUMAN     -> parked with failure_reason (e.g. session_expired).
    - FAILED          -> retried with backoff up to MAX_ATTEMPTS, then parked.
    applied_at is set ONLY on SUBMITTED.
    """
    result = result or {}
    status = result.get("status", STATUS_FAILED)
    notes = result.get("message") or result.get("error") or "Processed by apply worker."
    screenshot_path = result.get("screenshot_path")
    failure_reason = result.get("failure_reason") or classify_failure_reason(result)

    # (apply_status, set_applied_now, failure_reason, attempts, retry_in_minutes|None)
    if status == STATUS_SUBMITTED:
        plan = (STATUS_SUBMITTED, True, None, attempts, None)
    elif status == STATUS_PENDING_REVIEW:
        plan = (STATUS_PENDING_REVIEW, False, None, attempts, None)
    elif status == STATUS_NEEDS_HUMAN:
        plan = (STATUS_NEEDS_HUMAN, False, failure_reason, attempts, None)
    else:  # FAILED -> retry with backoff, then park
        attempts = attempts + 1
        if failure_reason == "billing_error":
            # A depleted billing account will not recover on a 5-minute
            # backoff; park immediately so the dashboard shows the real cause
            # instead of churning through pointless retries.
            logger.warning(
                "job_id=%s parked immediately: LLM billing exhausted (%s). "
                "Top up billing or switch LLM_PROVIDER and re-queue from the dashboard.",
                job_id,
                failure_reason,
            )
            plan = (STATUS_FAILED, False, failure_reason, attempts, None)
        elif attempts >= MAX_ATTEMPTS:
            logger.warning(
                "job_id=%s exhausted %d attempts; parking as FAILED (%s)",
                job_id,
                attempts,
                failure_reason,
            )
            plan = (STATUS_FAILED, False, failure_reason, attempts, None)
        else:
            backoff = RETRY_BASE_MINUTES * attempts
            logger.info(
                "job_id=%s failed (%s); requeueing in ~%d min (attempt %d/%d)",
                job_id,
                failure_reason,
                backoff,
                attempts,
                MAX_ATTEMPTS,
            )
            plan = ("QUEUED", False, failure_reason, attempts, backoff)

    _update_full(job_id, notes, screenshot_path, *plan)


def _update_full(
    job_id: str,
    notes: str,
    screenshot_path: str | None,
    apply_status: str,
    set_applied_now: bool,
    failure_reason: str | None,
    attempts: int,
    retry_in_minutes: int | None,
) -> None:
    """Single update path. Falls back to legacy columns pre-migration."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE saved_jobs
                    SET apply_status = %s,
                        apply_notes = %s,
                        review_screenshot_path = COALESCE(%s, review_screenshot_path),
                        applied_at = CASE WHEN %s THEN NOW() ELSE applied_at END,
                        failure_reason = %s,
                        attempts = %s,
                        next_retry_at = CASE
                            WHEN %s THEN NULL
                            ELSE NOW() + (%s * INTERVAL '1 minute')
                        END
                    WHERE job_id = %s;
                    """,
                    (
                        apply_status,
                        notes,
                        screenshot_path,
                        set_applied_now,
                        failure_reason,
                        attempts,
                        retry_in_minutes is None,
                        retry_in_minutes or 0,
                        job_id,
                    ),
                )
            conn.commit()
    except UndefinedColumn:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE saved_jobs
                    SET apply_status = %s,
                        apply_notes = %s,
                        review_screenshot_path = COALESCE(%s, review_screenshot_path),
                        applied_at = CASE WHEN %s THEN NOW() ELSE applied_at END
                    WHERE job_id = %s;
                    """,
                    (apply_status, notes, screenshot_path, set_applied_now, job_id),
                )
            conn.commit()


async def process_apply_queue(limit: int = 5, headless: bool = True) -> list[dict[str, Any]]:
    """Claims and runs the browser autofill agent across queued applications."""
    jobs = claim_queued_jobs(limit=limit)
    if not jobs:
        logger.info("No applications in QUEUED status.")
        return []

    logger.info("Claimed %d queued application(s) to process.", len(jobs))
    processed_results = []

    for job in jobs:
        job_id = job["job_id"]
        logger.info(
            "Processing application: %s (%s) - %s", job["title"], job["company"], job["job_url"]
        )
        try:
            result = await autofill_job_application(
                job_url=job["job_url"],
                job_id=job_id,
                headless=headless,
            )
            result_status = (result or {}).get("status", STATUS_FAILED)
        except Exception as exc:
            # One job's crash must never kill the batch or strand IN_PROGRESS.
            logger.error("Worker crashed on job_id=%s: %s", job_id, exc, exc_info=True)
            result = {
                "status": STATUS_FAILED,
                "job_id": job_id,
                "error": f"worker exception: {exc}",
            }
            result_status = STATUS_FAILED

        try:
            finalize_job(job_id=job_id, result=result, attempts=job.get("attempts", 0))
        except Exception as exc:
            logger.error("Failed to finalize job_id=%s: %s", job_id, exc, exc_info=True)

        logger.info("Application job_id=%s finished with status=%s", job_id, result_status)
        processed_results.append(result)

    return processed_results


async def run_worker_daemon(poll_interval: int = 30, limit: int = 5, headless: bool = True):
    """Continuously polls PostgreSQL for QUEUED applications and processes them."""
    logger.info(
        "Apply worker daemon started (poll_interval=%ds, limit=%d, headless=%s, worker_id=%s)",
        poll_interval,
        limit,
        headless,
        WORKER_ID,
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
