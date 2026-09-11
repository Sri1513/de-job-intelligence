# src/workers/batch_ingestion.py
import logging
from typing import List, Dict, Any
from src.core.database import get_db_connection
from src.engine.matcher import calculate_match_score
from src.ingestion.dice_client import DiceJobClient

logger = logging.getLogger("de-job-intelligence.batch_ingestion")

def persist_jobs(jobs: List[Dict[str, Any]], category_slug: str = "data_engineering") -> int:
    """
    Inserts raw job records into saved_jobs.
    Computes heuristic fit score upon ingestion and marks records as PENDING.
    """
    if not jobs:
        return 0

    insert_query = """
        INSERT INTO saved_jobs (
            job_id, title, company, location, is_remote, job_url, 
            description, job_category, fit_score, ai_status, saved_at
        ) VALUES (
            %(job_id)s, %(title)s, %(company)s, %(location)s, %(is_remote)s, %(job_url)s,
            %(description)s, %(job_category)s, %(fit_score)s, 'PENDING', NOW()
        )
        ON CONFLICT (job_id) DO NOTHING;
    """

    inserted_count = 0
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for job in jobs:
                description = job.get("description", "")
                scoring = calculate_match_score(description, category_slug=category_slug)
                
                params = {
                    "job_id": job["job_id"],
                    "title": job["title"],
                    "company": job["company"],
                    "location": job["location"],
                    "is_remote": job["is_remote"],
                    "job_url": job["job_url"],
                    "description": description,
                    "job_category": category_slug,
                    "fit_score": str(scoring["score"])
                }
                cur.execute(insert_query, params)
                inserted_count += 1
        conn.commit()

    return inserted_count

def run_dice_ingestion(query: str = "Data Engineer", pages: int = 1) -> Dict[str, Any]:
    """Runs a batch ingestion cycle from Dice."""
    client = DiceJobClient()
    total_found = 0
    total_persisted = 0

    for page in range(1, pages + 1):
        jobs = client.search_jobs(query=query, page=page)
        total_found += len(jobs)
        persisted = persist_jobs(jobs, category_slug="data_engineering")
        total_persisted += persisted

    return {
        "status": "completed",
        "query": query,
        "total_fetched": total_found,
        "total_persisted": total_persisted
    }

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    summary = run_dice_ingestion(query="Senior Data Engineer", pages=1)
    print(f"Ingestion finished: {summary}")