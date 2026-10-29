# src/engine/ingestion.py
import logging

from src.core.database import get_db_connection
from src.engine.evaluator import run_backfill_batch
from src.engine.pipeline_utils import (
    _is_real_description,
    extract_linkedin_id,
    stage_raw_jobs,
)
from src.engine.scraper import (
    fetch_linkedin_guest_job,
    fetch_scraped_jobs,
    is_c2c_arrangement,
)
from src.ingestion.dice_client import DiceJobClient

logger = logging.getLogger(__name__)

# Guest-API enrichment tuning. LinkedIn has 429/signup-walled JobSpy's
# per-job description fetch from datacenter IPs since ~2026-10-02 (Indeed
# descriptions still flow fine, so this is LinkedIn-specific, not a staging
# bug). One guest request per job needing it; stop after consecutive misses
# so an IP-level block doesn't burn the run. The polite delay between attempts
# now lives inside fetch_linkedin_guest_job (LINKEDIN_GUEST_DELAY_S) so every
# caller -- batch, email, JIT -- gets throttled consistently.
GUEST_ENRICH_MAX_CONSECUTIVE_MISSES = 3
# Mirrors stage_raw_jobs: shorter than this gets the "Role: ..." placeholder.
_MISSING_DESC_THRESHOLD = 20


def _enrich_missing_linkedin_descriptions(jobs: list) -> int:
    """Fill empty LinkedIn descriptions via the guest API before staging.

    JobSpy scrapes each LinkedIn description from the full /jobs/view/ page,
    which LinkedIn 429s or redirects to the signup wall from datacenter IPs
    (JobSpy then silently returns an empty description). The lightweight
    guest endpoint (/jobs-guest/jobs/api/jobPosting/<id>) usually still
    answers. Only LinkedIn jobs with a missing/short description are
    attempted; non-LinkedIn jobs and jobs that already have descriptions are
    skipped. Fail-open throughout: any miss or error leaves the job for the
    placeholder path, exactly as before.
    Returns the number of jobs enriched.
    """
    enriched = 0
    consecutive_misses = 0
    for job in jobs:
        if consecutive_misses >= GUEST_ENRICH_MAX_CONSECUTIVE_MISSES:
            logger.info(
                "Guest-API enrichment stopping after %d consecutive misses "
                "(likely IP-level block); remaining jobs keep placeholders.",
                consecutive_misses,
            )
            break
        if len(job.get("description") or "") >= _MISSING_DESC_THRESHOLD:
            continue
        linkedin_id = extract_linkedin_id(job.get("job_url"))
        if not linkedin_id:
            continue
        try:
            guest_job = fetch_linkedin_guest_job(linkedin_id)
        except Exception as exc:
            logger.warning(
                "Guest-API enrichment failed for job '%s': %s",
                job.get("job_id"), exc,
            )
            guest_job = None
        if guest_job and _is_real_description(guest_job.get("description")):
            job["description"] = guest_job["description"]
            enriched += 1
            consecutive_misses = 0
            logger.info(
                "   -> Guest-API enriched description for '%s'.", job.get("job_id")
            )
        else:
            consecutive_misses += 1
    return enriched


def run_batch_ingestion_workflow(
    search_term: str = "Data Engineer",
    location: str = "United States",
    results_wanted: int = 10,
    hours_old: int = 48,
    job_category: str = "data_engineering",
    c2c_only: bool = False,
) -> dict:
    """Executes multi-source scraping (JobSpy + Dice), deduplication, and initial local match scoring.

    When c2c_only is True, JobSpy is asked for contract roles only and,
    after description enrichment, only rows whose title/description
    confirms a Corp-to-Corp arrangement (is_c2c_arrangement) are staged.
    Rows with no C2C confirmation — including rows whose description
    could not be fetched — are dropped and counted, never staged as C2C.
    """

    # 1. Fetch from JobSpy (LinkedIn, Indeed, Google Jobs)
    logger.info(f"🌐 Fetching JobSpy listings for '{search_term}'...")
    jobspy_jobs = fetch_scraped_jobs(
        search_term=search_term,
        location=location,
        results_wanted=results_wanted,
        hours_old=hours_old,
        is_remote=True,
        c2c_only=c2c_only,
    )

    # 2. Fetch from Dice using your custom REST client
    logger.info(f"🎲 Fetching Dice listings for '{search_term}'...")
    dice_client = DiceJobClient()
    dice_jobs = dice_client.search_jobs(
        query=search_term, location=location, page=1, page_size=results_wanted
    )

    # Combine listings from all platforms
    raw_jobs = jobspy_jobs + dice_jobs
    logger.info(
        f"📦 Total combined raw jobs fetched: {len(raw_jobs)} (JobSpy: {len(jobspy_jobs)}, Dice: {len(dice_jobs)})"
    )

    # LinkedIn descriptions come back empty when LinkedIn walls JobSpy's
    # per-job page fetch (429/signup redirect from datacenter IPs). Fill
    # them via the guest API before staging so they don't become
    # placeholders — stage_raw_jobs then scores them on the real text.
    enriched = _enrich_missing_linkedin_descriptions(raw_jobs)
    if enriched:
        logger.info(f"✨ Guest-API enriched {enriched} LinkedIn description(s).")

    # C2C text confirmation runs here, not at scrape time: LinkedIn
    # descriptions are only reliably present after the enrichment above,
    # and Dice descriptions arrive with the MCP payload. Filtering
    # earlier would drop LinkedIn rows before their text exists.
    c2c_dropped = 0
    if c2c_only:
        pre_filter = len(raw_jobs)
        raw_jobs = [job for job in raw_jobs if is_c2c_arrangement(job)]
        c2c_dropped = pre_filter - len(raw_jobs)
        logger.info(
            "C2C filter kept %d of %d jobs (%d dropped: no C2C confirmation in title/description).",
            len(raw_jobs),
            pre_filter,
            c2c_dropped,
        )

    if not raw_jobs:
        return {
            "status": "warning",
            "message": f"No jobs found for '{search_term}' across any platform.",
            "new_jobs_saved": 0,
        }

    with get_db_connection() as conn:
        stats = stage_raw_jobs(
            conn=conn, jobs=raw_jobs, job_category=job_category, default_source="JobSpy"
        )

    # Trigger immediate AI evaluation for newly staged jobs
    eval_summary = None
    if stats.get("saved", 0) > 0:
        logger.info(f"🤖 Evaluating {stats['saved']} newly ingested jobs with AI...")
        eval_summary = run_backfill_batch(limit=stats["saved"], job_category=job_category)

    # Counts are computed from the post-filter list so a c2c_only run
    # reports what was actually staged, not what was scraped.
    dice_count = sum(1 for job in raw_jobs if job.get("source") == "dice")
    return {
        "status": "success",
        "search_term": search_term,
        "total_scraped": len(raw_jobs),
        "jobspy_count": len(raw_jobs) - dice_count,
        "dice_count": dice_count,
        "c2c_only": c2c_only,
        "c2c_dropped": c2c_dropped,
        "new_jobs_saved": stats.get("saved", 0),
        "ai_evaluated": eval_summary.get("successful", 0) if eval_summary else 0,
        "location_variants_added": stats.get("updated", 0),
        "duplicates_skipped": stats.get("skipped", 0),
        "message": f"Ingested {stats.get('saved', 0)} new jobs ({eval_summary.get('successful', 0) if eval_summary else 0} evaluated with AI).",
    }
