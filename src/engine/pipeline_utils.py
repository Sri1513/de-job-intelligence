# src/engine/pipeline_utils.py
import hashlib
import json
import re
from datetime import datetime
from typing import Optional

from src.core.utils import get_cached_resume
from src.engine.matcher import calculate_local_fit_score


def generate_domain_short_id(email: Optional[str], company_name: Optional[str] = None) -> str:
    """
    Extracts the domain after '@' from an email (e.g., 'hr@databricks.com' -> 'databricks'),
    and returns a clean 4-character code (e.g., 'data'). 
    Falls back to company name slug if no email is provided.
    """
    domain_part = ""
    if email and "@" in email:
        domain_part = email.split("@")[1].strip().lower()
    elif company_name:
        domain_part = re.sub(r'[^a-z0-9]', '', company_name.lower()) + ".com"
    else:
        domain_part = "unknown.com"

    # Get the root domain name (e.g., 'databricks' from 'databricks.com')
    parts = domain_part.split('.')
    root_name = parts[-2] if len(parts) >= 2 else parts[0]
    
    # Clean and take the first 4 alphanumeric characters (padded with 'x' if too short)
    clean_name = re.sub(r'[^a-z0-9]', '', root_name)
    return clean_name[:4].ljust(4, 'x')

def extract_linkedin_id(url: str) -> str:
    """Extracts numeric job ID from LinkedIn URLs."""
    if not url:
        return None
    match = re.search(r'/jobs/view/(\d+)', url)
    return match.group(1) if match else None

def generate_job_id(company: str, title: str, job_url: str = None) -> str:
    """Generates a deterministic unique job ID."""
    if job_url:
        numeric_id = extract_linkedin_id(job_url)
        if numeric_id:
            return f"li-{numeric_id}"
            
    def slugify(text):
        text = (text or "unknown").lower().strip()
        text = re.sub(r'[^\w\s-]', '', text)
        return re.sub(r'[\s_-]+', '-', text)[:30]

    c_slug = slugify(company)
    t_slug = slugify(title)
    raw_str = f"{(company or '').strip().lower()}:{(title or '').strip().lower()}"
    short_hash = hashlib.md5(raw_str.encode()).hexdigest()[:6]
    return f"{c_slug}-{t_slug}-{short_hash}"

def safe_float(val):
    try:
        return float(val) if val is not None and val != "N/A" else None
    except (ValueError, TypeError):
        return None

def stage_raw_jobs(conn, jobs: list, job_category: str = "data_engineering", default_source: str = "JobSpy") -> dict:
    """
    Ingests scraped jobs into PostgreSQL.
    - Prevents duplicate inserts
    - Merges new locations into existing job records
    - Calculates an immediate TF-IDF & heuristic fit score (0 Gemini API cost)
    - Stages new records as 'PENDING'
    """
    staged_jobs = []
    saved_count = 0
    updated_count = 0
    skipped_count = 0

    # Clean dictionary mapping for precise source tags
    SOURCE_MAP = {
        "linkedin": "LinkedIn",
        "indeed": "Indeed",
        "google": "Google Jobs",
        "google_jobs": "Google Jobs",
        "dice": "Dice",
        "email alert": "Email Alert",
        "email-to-jobspy bridge": "Email Alert"
    }

    resume_text = get_cached_resume(job_category)

    with conn.cursor() as cur:
        for job in jobs:
            title = str(job.get("title", "N/A")).strip()
            company = str(job.get("company", "N/A")).strip()
            job_url = str(job.get("job_url", "")).strip()
            city = str(job.get("location", "United States")).strip()

            raw_source = job.get("source") or job.get("site") or default_source
            job_source = SOURCE_MAP.get(str(raw_source).lower(), str(raw_source).capitalize())
            
            if not job_url or job_url == "N/A" or not title or not company:
                continue

            job_id = generate_job_id(company, title, job_url)
            job_desc = str(
                job.get("description") or job.get("job_description") or ""
            ).strip()

            if len(job_desc) < 20:
                job_desc = f"Role: {title} at {company}. Extracted via scraper."

            # 1. Check if record already exists
            cur.execute("SELECT metadata FROM saved_jobs WHERE job_id = %s;", (job_id,))
            existing_record = cur.fetchone()

            if existing_record:
                metadata = (existing_record["metadata"] if isinstance(existing_record, dict) else existing_record[0]) or {}
                locations_list = metadata.get("locations", [])
                location_exists = any(
                    loc.get("url") == job_url or loc.get("city", "").lower() == city.lower() 
                    for loc in locations_list
                )

                if not location_exists:
                    locations_list.append({"city": city, "url": job_url})
                    metadata["locations"] = locations_list
                    cur.execute(
                        "UPDATE saved_jobs SET metadata = %s WHERE job_id = %s;",
                        (json.dumps(metadata), job_id)
                    )
                    updated_count += 1
                else:
                    skipped_count += 1
                continue

            # 2. Compute immediate baseline score using local TF-IDF matcher (0 API tokens)
            local_eval = calculate_local_fit_score(
                resume_text=resume_text,
                job_description=job_desc,
                job_title=title,
                job_category=job_category
            )

            initial_metadata = {
                "locations": [{"city": city, "url": job_url}],
                "date_posted": str(job.get("date_posted") or "Unknown"),
                "retrieved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": job_source,
                "matched_skills": local_eval.get("matched_skills", []),
                "missing_skills": local_eval.get("missing_skills", []),
                "semantic_match": local_eval.get("semantic_match", 0),
                "skill_match": local_eval.get("skill_match", 0)
            }

            cur.execute(
                """
                INSERT INTO saved_jobs (
                    job_id, job_url, status, title, company, location, is_remote, 
                    salary_min, salary_max, fit_score, notes, description, metadata,
                    employment_type, sponsorship, job_category, ai_status, saved_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, 
                    %s, %s, %s, %s, %s, %s, 
                    %s, %s, %s, 'PENDING', CURRENT_TIMESTAMP
                )
                ON CONFLICT (job_id) DO NOTHING;
                """,
                (
                    job_id,
                    job_url,
                    "saved",
                    title,
                    company,
                    city,
                    True,
                    safe_float(job.get("min_amount")),
                    safe_float(job.get("max_amount")),
                    str(local_eval.get("score", 0)),
                    "Pending AI evaluation...",
                    job_desc,
                    json.dumps(initial_metadata),
                    str(job.get("employment_type", "Unknown")),
                    str(job.get("sponsorship", "Not Mentioned")),
                    job_category
                )
            )
            saved_count += 1
            staged_jobs.append({"job_id": job_id, "title": title, "company": company})

        conn.commit()

    return {
        "staged_jobs": staged_jobs,
        "saved": saved_count,
        "updated": updated_count,
        "skipped": skipped_count
    }
