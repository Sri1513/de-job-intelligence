# src/engine/scraper.py
import logging

from jobspy import scrape_jobs

logger = logging.getLogger(__name__)

BLACKLIST_KEYWORDS = [
    "technician",
    "electrician",
    "hvac",
    "facility",
    "facilities",
    "building engineer",
    "critical environments",
    "data center",
    "hardware",
    "operator",
    "chiller",
    "plumbing",
    "carpentry",
]


def is_unwanted_job(title: str) -> bool:
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in BLACKLIST_KEYWORDS)


def fetch_scraped_jobs(
    search_term: str = "Data Engineer",
    location: str = "United States",
    results_wanted: int = 10,
    hours_old: int = 48,
    is_remote: bool = True,
    site_name: list = None,
) -> list:
    """Scrapes job listings from LinkedIn, Indeed, and Google Jobs."""
    if site_name is None:
        site_name = ["linkedin", "indeed"]

    logger.info(f"🌐 Scraping {results_wanted} listings for '{search_term}' in '{location}'...")

    try:
        df = scrape_jobs(
            site_name=site_name,
            search_term=search_term,
            location=location,
            results_wanted=results_wanted,
            hours_old=hours_old,
            is_remote=is_remote,
            linkedin_fetch_description=True,
        )
        if df is not None and not df.empty:
            records = df.fillna("N/A").to_dict(orient="records")
            # Filter out blacklisted non-engineering roles
            clean_records = [j for j in records if not is_unwanted_job(str(j.get("title", "")))]
            return clean_records
    except Exception as e:
        logger.error(f"Scraper encountered an error: {e}")

    return []
