"""Serial drain for the local-LLM (qwen2.5:3b) fallback queue.

Processes only rows the free Gemini -> Groq chain already failed
(ai_status PENDING/FAILED, or missing ai_extracted_skills) — so the
local 3B only ever runs after both free tiers are exhausted for a given
row, by construction. One job at a time, no parallelism: the box is
2 CPUs and the model needs ~1-2 min per call.

Per job, in order:
  1. skill extraction via local_llm.extract_skills (checklist pass 1 ->
     verbatim safety net -> checklist pass 2, union of trues, normalized),
  2. sponsorship detection via local_llm.sponsorship_check,
  3. AI Notes via local_llm.ai_notes,
  4. deterministic fit score via matcher.calculate_local_fit_score
     (the LLM never scores),
  5. one UPDATE per job (same columns as evaluator.run_backfill_batch),
     committed per job.

Failure handling (never raises out of run_local_fallback_batch):
  - Per-job data failure (bad JSON after retry, empty reply, missing
    keys): mark the row FAILED with ai_error, continue to the next job.
    The row stays in the queue for a later run.
  - Transport failure (the tier itself is down: connection refused,
    Cloudflare 1010/524, auth failure, timeout): stop the batch, call
    notifier.alert_llm_all_down(task="local-3b-fallback") ONCE, return
    the summary with the rows attempted so far. Rows not yet attempted
    stay untouched.
  - Empty extraction (the JD names no taxonomy skills at all, e.g. a
    non-DE role): still PROCESSED — the extraction succeeded, there is
    just nothing to extract. The row may be re-selected by the
    evaluator's pending query on a later run (its WHERE clause matches
    empty ai_extracted_skills); re-running the queue on it just repeats
    the same deterministic result.

Trigger: manual CLI only (src/workers/local_llm_backfill.py). No
scheduler exists yet; scheduling is a follow-up decision. Ingestion's
inline evaluator.run_backfill_batch is untouched — this queue is a later
drain, never a change to the ingestion path.
"""

import logging
import time
from typing import Any, Dict, List, Optional

from src.core.database import get_db_connection
from src.engine import local_llm, notifier
from src.engine.local_llm import LocalLLMError, LocalLLMUnavailableError
from src.engine.matcher import calculate_local_fit_score, get_cached_resume
from src.workers.pipeline_utils import update_job_evaluation

logger = logging.getLogger(__name__)

# Same WHERE clause as evaluator.run_backfill_batch, verbatim, so the
# queue and the primary backfill agree on what "pending" means.
_PENDING_JOBS_SQL = """
    SELECT job_id, title, company, description, metadata
    FROM saved_jobs
    WHERE (
        ai_status IN ('PENDING', 'FAILED')
        OR LEFT(notes, 18) = 'AI analysis failed'
        OR metadata->'ai_extracted_skills' IS NULL
        OR metadata->>'ai_extracted_skills' IN ('[]', 'null', '')
    )
    ORDER BY saved_at DESC NULLS LAST
    LIMIT %s;
"""

_JOB_IDS_SQL = """
    SELECT job_id, title, company, description, metadata
    FROM saved_jobs
    WHERE job_id = ANY(%s);
"""

_PACING_SLEEP_S = 1.5  # same pacing as evaluator.run_backfill_batch


def _fetch_jobs(limit: int, job_ids: Optional[List[str]]) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if job_ids:
                cur.execute(_JOB_IDS_SQL, (job_ids,))
            else:
                cur.execute(_PENDING_JOBS_SQL, (limit,))
            for row in cur.fetchall():
                jobs.append(
                    {
                        "job_id": row["job_id"] if isinstance(row, dict) else row[0],
                        "title": row["title"] if isinstance(row, dict) else row[1],
                        "company": row["company"] if isinstance(row, dict) else row[2],
                        "description": row["description"] if isinstance(row, dict) else row[3],
                        "metadata": (row["metadata"] if isinstance(row, dict) else row[4]) or {},
                    }
                )
    return jobs


def _mark_job_failed(job_id: str, error: str) -> None:
    """Mark a row FAILED, preserving its notes/fit_score (same as the
    evaluator's failure path — NOT workers.mark_job_failed, which
    overwrites notes and zeroes the score)."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE saved_jobs SET ai_status = 'FAILED', ai_error = %s WHERE job_id = %s;",
                    (str(error), job_id),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - failure marking must not crash the batch
        logger.error("local-llm: could not mark %s FAILED: %s", job_id, exc)


def _process_one_job(
    job: Dict[str, Any],
    resume_text: str,
    job_category: str,
    dry_run: bool,
) -> Dict[str, Any]:
    """Run the three local-LLM tasks for one job and persist the result.

    Returns the per-job result dict. Raises LocalLLMUnavailableError
    when the tier itself is down (caller stops the batch); raises
    LocalLLMError for per-job data failures (caller marks FAILED).
    """
    job_id = job["job_id"]
    description = job.get("description", "") or ""
    logger.info(
        "local-llm: job %s served via qwen2.5:3b (title=%r company=%r)",
        job_id, job.get("title"), job.get("company"),
    )

    vocabulary = local_llm.load_checklist_vocabulary()
    extracted = local_llm.extract_skills(description, vocabulary)
    if not extracted:
        logger.warning(
            "local-llm: job %s extracted no taxonomy skills (non-DE role or "
            "empty description); proceeding with an empty skill list",
            job_id,
        )
    sponsorship = local_llm.sponsorship_check(description)
    notes = local_llm.ai_notes(description)

    score_data = calculate_local_fit_score(
        resume_text=resume_text,
        job_description=description,
        job_title=job.get("title", ""),
        job_category=job_category,
        ai_extracted_skills=extracted,
    )

    result = {
        "job_id": job_id,
        "status": "PROCESSED",
        "score": score_data["score"],
        "skills": extracted,
        "sponsorship": sponsorship["sponsorship"],
        "notes": notes,
    }
    if dry_run:
        logger.info("local-llm: dry run for %s — nothing written: %s", job_id, result)
        return result

    metadata_updates = {
        "matched_skills": score_data.get("matched_skills", []),
        "missing_skills": score_data.get("missing_skills", []),
        "semantic_match": score_data.get("semantic_match", 0),
        "skill_match": score_data.get("skill_match", 0),
        # The extracted skills double as the tailoring-signal input the
        # primary path produces; do NOT invent separate signals.
        "ai_extracted_skills": extracted,
        "tailoring_signals": extracted,
    }
    ok = update_job_evaluation(
        job_id=job_id,
        match_score=score_data["score"],
        ai_notes=notes,
        ai_status="PROCESSED",
        sponsorship=sponsorship["sponsorship"],
        metadata_updates=metadata_updates,
    )
    if not ok:
        raise LocalLLMError(f"failed to persist evaluation for {job_id}")
    logger.info("local-llm: job %s PROCESSED, score %s", job_id, score_data["score"])
    return result


def run_local_fallback_batch(
    limit: int = 5,
    job_ids: Optional[List[str]] = None,
    dry_run: bool = False,
    job_category: str = "data_engineering",
) -> Dict[str, Any]:
    """Drain AI-pending/FAILED rows through the local 3B, serially.

    Never raises. Returns a summary dict in the evaluator's shape.
    """
    resume_text = get_cached_resume(job_category)
    try:
        jobs = _fetch_jobs(limit, job_ids)
    except Exception as exc:  # noqa: BLE001 - DB read failure: nothing to drain
        logger.error("local-llm: could not fetch pending jobs: %s", exc)
        return {"status": "failed", "message": f"could not fetch jobs: {exc}",
                "attempted": 0, "successful": 0, "failed": 0, "results": []}

    if not jobs:
        return {"status": "success", "message": "No pending jobs found.",
                "attempted": 0, "successful": 0, "failed": 0, "results": []}

    successful = 0
    failed = 0
    results: List[Dict[str, Any]] = []
    tier_down: Optional[str] = None

    for idx, job in enumerate(jobs):
        job_id = job["job_id"]
        try:
            result = _process_one_job(job, resume_text, job_category, dry_run)
            successful += 1
            results.append(result)
        except LocalLLMUnavailableError as exc:
            # The tier itself is down: stop the batch, alert once, leave
            # the remaining rows untouched.
            tier_down = str(exc)
            logger.error("local-llm: tier unavailable during job %s: %s", job_id, exc)
            results.append({"job_id": job_id, "status": "TIER_DOWN", "error": str(exc)})
            break
        except Exception as exc:  # noqa: BLE001 - per-job failure: mark and continue
            failed += 1
            logger.error("local-llm: failed %s: %s", job_id, exc)
            if not dry_run:
                _mark_job_failed(job_id, str(exc))
            results.append({"job_id": job_id, "status": "FAILED", "error": str(exc)})

        if idx + 1 < len(jobs):
            time.sleep(_PACING_SLEEP_S)

    summary: Dict[str, Any] = {
        "status": "completed",
        "attempted": len(results),
        "successful": successful,
        "failed": failed,
        "results": results,
    }
    if tier_down is not None:
        summary["local_llm_unavailable"] = True
        summary["error"] = tier_down
        notifier.alert_llm_all_down(task="local-3b-fallback", last_error=tier_down)
        logger.error("local-llm: batch stopped, tier down: %s", tier_down)
    return summary
