# src/workers/email_pipeline.py
import logging
from datetime import datetime
from dotenv import load_dotenv

from src.ingestion.email_scraper import fetch_jobs_from_email
from src.engine.scraper import fetch_scraped_jobs
from src.core.database import get_db_connection
from src.engine.pipeline_utils import stage_raw_jobs
from src.engine.evaluator import run_backfill_batch

logger = logging.getLogger(__name__)

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
            "new_jobs_saved": 0
        }
    
    enriched_jobs = []
    retrieval_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    logger.info(f"\n🌉 Step 2: Widening JobSpy search results and filtering sponsored/unqualified noise...")
    for item in email_jobs:
        title = item.get("title", "Data Engineer")
        company = item.get("company", "Unknown")
        
        if company == "Unknown":
            item["retrieved_at"] = retrieval_timestamp
            enriched_jobs.append(item)
            continue
            
        search_query = f"{title} {company}"
        logger.info(f"🔍 Searching wider pool for -> Query: '{search_query}'")
        
        # Uses fetch_scraped_jobs from your new scraper module which returns a list directly
        found_jobs = fetch_scraped_jobs(
            search_term=search_query,
            location="United States",
            results_wanted=15,
            hours_old=168,
            is_remote=True,
            site_name=["linkedin", "google"]
        )
        
        qualified_job = None
        target_company_lower = company.lower()
        
        for fj in found_jobs:
            fj_company = fj.get("company", "").lower()
            fj_title = fj.get("title", "").lower()
            
            is_company_match = (target_company_lower in fj_company) or (fj_company in target_company_lower)
            is_sponsored = any(bad in fj_title for bad in ["promoted", "sponsored", "ad"])
            is_relevant_role = any(kw in fj_title for kw in ["data", "etl", "engineer", "developer", "analytics"])
            
            if is_company_match and is_relevant_role and not is_sponsored:
                qualified_job = fj
                logger.info(f"   -> ✅ Qualified match found: '{fj.get('title')}' at '{fj.get('company')}'")
                break
                
        if qualified_job:
            qualified_job["retrieved_at"] = retrieval_timestamp
            enriched_jobs.append(qualified_job)
        else:
            logger.info(f"   -> ⚠️ No clean match in widened pool. Preserving email alert metadata & direct URL.")
            item["retrieved_at"] = retrieval_timestamp
            enriched_jobs.append(item)
            
    logger.info(f"\n💾 Step 3: Storing and staging {len(enriched_jobs)} jobs in PostgreSQL via stage_raw_jobs...")
    with get_db_connection() as conn:
        stats = stage_raw_jobs(
            conn=conn,
            jobs=enriched_jobs,
            job_category=job_category,
            default_source="Email-to-JobSpy Bridge"
        )

    # Trigger immediate AI evaluation for newly staged jobs, mirroring batch ingestion
    eval_summary = None
    if stats.get("saved", 0) > 0:
        logger.info(f"\n🤖 Step 4: Evaluating {stats['saved']} newly staged email jobs with AI...")
        eval_summary = run_backfill_batch(limit=stats["saved"], job_category=job_category)

    logger.info(f"\n🎉 Pipeline Complete! Inserted: {stats.get('saved', 0)} | Updated: {stats.get('updated', 0)} | Skipped: {stats.get('skipped', 0)}")
    return {
        "status": "success",
        "total_processed": len(enriched_jobs),
        "new_jobs_saved": stats.get("saved", 0),
        "ai_evaluated": eval_summary.get("successful", 0) if eval_summary else 0,
        "location_variants_added": stats.get("updated", 0),
        "duplicates_skipped": stats.get("skipped", 0),
        "message": f"Successfully processed email alerts. Saved {stats.get('saved', 0)} new jobs ({eval_summary.get('successful', 0) if eval_summary else 0} evaluated with AI)."
    }

if __name__ == "__main__":
    run_email_pipeline(limit=5)