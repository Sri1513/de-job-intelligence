# src/workers/email_pipeline.py
import logging
import re
from datetime import datetime

from dotenv import load_dotenv

from src.core.database import get_db_connection
from src.engine.evaluator import run_backfill_batch
from src.engine.pipeline_utils import stage_raw_jobs
from src.engine.scraper import fetch_scraped_jobs
from src.ingestion.email_scraper import fetch_jobs_from_email

logger = logging.getLogger(__name__)

_US_STATE_ABBR = frozenset(
    s.lower()
    for s in (
        "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
        "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
    ).split()
)
_US_STATE_NAMES = frozenset(
    (
        "alabama alaska arizona arkansas california colorado connecticut delaware "
        "florida georgia hawaii idaho illinois indiana iowa kansas kentucky louisiana "
        "maine maryland massachusetts michigan minnesota mississippi missouri montana "
        "nebraska nevada hampshire jersey mexico york carolina dakota ohio oklahoma "
        "oregon pennsylvania rhode island carolina dakota tennessee texas utah vermont "
        "virginia washington wisconsin wyoming district of columbia"
    ).split()
)
_US_MARKERS = ("united states", "usa", "u.s.a", "u.s.")
_NON_US_MARKERS = (
    "united kingdom",
    "canada",
    "india",
    "australia",
    "ireland",
    "germany",
    "france",
    "netherlands",
    "spain",
    "mexico",
    "brazil",
    "singapore",
    "philippines",
    "pakistan",
)
_REMOTE_US_LABELS = frozenset(
    {"remote", "remote us", "remote usa", "remote united states", "us remote", "usa remote"}
)


def is_us_location(job: dict) -> bool:
    """
    Best-effort check that a JobSpy result is a US posting.

    JobSpy's location filter is fuzzy, and the same role is often reposted from
    several offices — when duplicates match, this picks the US posting. Explicit
    non-US markers reject first (guards ambiguous abbreviations like IN), then
    positive US evidence (state names/abbreviations, USA markers, US remote
    labels) accepts. Remote results with no parsable location are accepted
    because the JobSpy query itself is US-scoped.
    """
    loc = (job.get("location") or "").strip()
    low = loc.lower()

    if any(m in low for m in _NON_US_MARKERS):
        return False
    if any(m in low for m in _US_MARKERS):
        return True
    if low in _REMOTE_US_LABELS:
        return True
    tokens = set(re.split(r"[^a-z0-9]+", low))
    if tokens & _US_STATE_ABBR:
        return True
    if any(name in low for name in _US_STATE_NAMES):
        return True
    if not loc and job.get("is_remote"):
        return True
    return False


def run_email_pipeline(limit: int = 5, job_category: str = "data_engineering") -> dict:
    """
    Pulls LinkedIn job alerts from Gmail, widens metadata via JobSpy scraper,
    filters out sponsored/unqualified noise with robust role and company matching,
    stages raw jobs using native deduplication/location tracking, and triggers AI evaluation.
    """
    load_dotenv()

    logger.info("📧 Step 1: Pulling recent job alerts from email inbox...")
    email_jobs = fetch_jobs_from_email(limit=limit)

    if not email_jobs:
        logger.warning("⚠️ No email jobs found to process.")
        return {
            "status": "warning",
            "message": "No email jobs found to process.",
            "new_jobs_saved": 0,
        }

    enriched_jobs = []
    retrieval_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.info(
        "\n🌉 Step 2: Widening JobSpy search results and filtering sponsored/unqualified noise..."
    )
    for item in email_jobs:
        title = item.get("title", "Data Engineer")
        company = item.get("company", "Unknown")

        if company == "Unknown":
            item["retrieved_at"] = retrieval_timestamp
            enriched_jobs.append(item)
            continue

        search_query = f"{title} {company}"
        logger.info(f"🔍 Searching wider pool for -> Query: '{search_query}'")

        # Uses fetch_scraped_jobs from your new scraper module which returns a list directly.
        # Google is excluded: it returns no data from datacenter IPs (every call
        # errors), so it only adds noise to the widening step.
        found_jobs = fetch_scraped_jobs(
            search_term=search_query,
            location="United States",
            results_wanted=15,
            hours_old=168,
            is_remote=True,
            site_name=["linkedin"],
        )

        qualified_job = None
        target_company_lower = company.lower()

        for fj in found_jobs:
            fj_company = fj.get("company", "").lower()
            fj_title = fj.get("title", "").lower()

            is_company_match = (target_company_lower in fj_company) or (
                fj_company in target_company_lower
            )
            is_sponsored = any(bad in fj_title for bad in ["promoted", "sponsored", "ad"])
            is_relevant_role = any(
                kw in fj_title for kw in ["data", "etl", "engineer", "developer", "analytics"]
            )
            # The same role is often reposted from multiple offices; only accept
            # the US posting so the pipeline never points at a foreign listing.
            if (
                is_company_match
                and is_relevant_role
                and not is_sponsored
                and is_us_location(fj)
            ):
                qualified_job = fj
                logger.info(
                    f"   -> ✅ Qualified match found: '{fj.get('title')}' at '{fj.get('company')}'"
                )
                break

        if qualified_job:
            qualified_job["retrieved_at"] = retrieval_timestamp
            enriched_jobs.append(qualified_job)
        else:
            logger.info(
                "   -> ⚠️ No clean match in widened pool. Preserving email alert metadata & direct URL."
            )
            item["retrieved_at"] = retrieval_timestamp
            enriched_jobs.append(item)

    logger.info(
        f"\n💾 Step 3: Storing and staging {len(enriched_jobs)} jobs in PostgreSQL via stage_raw_jobs..."
    )
    with get_db_connection() as conn:
        stats = stage_raw_jobs(
            conn=conn,
            jobs=enriched_jobs,
            job_category=job_category,
            default_source="Email-to-JobSpy Bridge",
        )

    # Trigger immediate AI evaluation for newly staged jobs, mirroring batch ingestion
    eval_summary = None
    if stats.get("saved", 0) > 0:
        logger.info(f"\n🤖 Step 4: Evaluating {stats['saved']} newly staged email jobs with AI...")
        eval_summary = run_backfill_batch(limit=stats["saved"], job_category=job_category)

    logger.info(
        f"\n🎉 Pipeline Complete! Inserted: {stats.get('saved', 0)} | Updated: {stats.get('updated', 0)} | Skipped: {stats.get('skipped', 0)}"
    )
    return {
        "status": "success",
        "total_processed": len(enriched_jobs),
        "new_jobs_saved": stats.get("saved", 0),
        "ai_evaluated": eval_summary.get("successful", 0) if eval_summary else 0,
        "location_variants_added": stats.get("updated", 0),
        "duplicates_skipped": stats.get("skipped", 0),
        "message": f"Successfully processed email alerts. Saved {stats.get('saved', 0)} new jobs ({eval_summary.get('successful', 0) if eval_summary else 0} evaluated with AI).",
    }


if __name__ == "__main__":
    run_email_pipeline(limit=5)
