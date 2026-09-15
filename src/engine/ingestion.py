from src.engine.evaluator import run_backfill_batch
# src/engine/ingestion.py
import logging
from src.core.database import get_db_connection
from src.engine.scraper import fetch_scraped_jobs
from src.engine.pipeline_utils import stage_raw_jobs

logger = logging.getLogger(__name__)

def run_batch_ingestion_workflow(
    search_term: str = "Data Engineer",
    location: str = "Remote",
    results_wanted: int = 10,
    hours_old: int = 48,
    job_category: str = "data_engineering"
) -> dict:
    """Executes scraping, deduplication, and initial local match scoring."""
    raw_jobs = fetch_scraped_jobs(
        search_term=search_term,
        location=location,
        results_wanted=results_wanted,
        hours_old=hours_old,
        is_remote=True
    )

    if not raw_jobs:
        return {
            "status": "warning",
            "message": f"No jobs found for '{search_term}'.",
            "new_jobs_saved": 0
        }

    with get_db_connection() as conn:
        stats = stage_raw_jobs(
            conn=conn,
            jobs=raw_jobs,
            job_category=job_category,
            default_source="JobSpy Scraper"
        )

    # Trigger immediate AI evaluation for newly staged jobs
    eval_summary = None
    if stats.get("saved", 0) > 0:
        logger.info(f"Evaluating {stats['saved']} newly ingested jobs with AI...")
        eval_summary = run_backfill_batch(limit=stats["saved"], job_category=job_category)

    return {
        "status": "success",
        "search_term": search_term,
        "total_scraped": len(raw_jobs),
        "new_jobs_saved": stats.get("saved", 0),
        "ai_evaluated": eval_summary.get("successful", 0) if eval_summary else 0,
        "location_variants_added": stats.get("updated", 0),
        "duplicates_skipped": stats.get("skipped", 0),
        "message": f"Ingested {stats.get('saved', 0)} new jobs ({eval_summary.get('successful', 0) if eval_summary else 0} evaluated with AI)."
    }
