# src/engine/scraper.py
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from jobspy import scrape_jobs

logger = logging.getLogger(__name__)

BLACKLIST_KEYWORDS = [
    "technician", "electrician", "hvac", "facility", "facilities",
    "building engineer", "critical environments", "data center",
    "hardware", "operator", "chiller", "plumbing", "carpentry",
]


def is_unwanted_job(title: str) -> bool:
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in BLACKLIST_KEYWORDS)


def fetch_scraped_jobs(
    search_term: str = "Data Engineer",
    location: str = "United States",
    results_wanted: int = 15,
    hours_old: int = 48,
    is_remote: bool = True,
    site_name: list = None,
) -> list:
    """Scrapes job listings strictly from LinkedIn and Indeed."""
    if site_name is None:
        site_name = ["linkedin", "indeed"]

    collected_jobs = []

    for site in site_name:
        logger.info(
            f"🌐 Scraping {results_wanted} listings from '{site}' for '{search_term}'..."
        )
        try:
            df = scrape_jobs(
                site_name=[site],
                search_term=search_term,
                location=location,
                results_wanted=results_wanted,
                is_remote=is_remote,
                country_indeed="usa",  # Always pass 'usa' so JobSpy never encounters None.strip()
            )

            if df is None or df.empty:
                logger.info(f"No listings returned from '{site}'.")
                continue

            # Recency post-filter. If every row is older than the cutoff (LinkedIn
            # frequently returns stale-dated rows), fall back to the most recent
            # rows instead of silently returning zero — a stale job the user can
            # still apply to beats an empty pipeline nobody notices.
            if hours_old and "date_posted" in df.columns:
                try:
                    cutoff_date = (
                        datetime.now(timezone.utc) - timedelta(hours=hours_old)
                    ).date()
                    parsed_dates = pd.to_datetime(
                        df["date_posted"], errors="coerce", utc=True
                    ).dt.date
                    fresh = df[(parsed_dates >= cutoff_date) | parsed_dates.isna()]
                    dropped = len(df) - len(fresh)
                    if dropped:
                        logger.info(
                            "Recency filter (%dh) dropped %d stale row(s) from '%s' "
                            "(newest seen: %s).",
                            hours_old, dropped, site, parsed_dates.max(),
                        )
                    if fresh.empty and not df.empty:
                        ranked = (
                            df.assign(_parsed_date=parsed_dates)
                            .sort_values("_parsed_date", ascending=False, na_position="first")
                            .drop(columns=["_parsed_date"])
                            .head(results_wanted)
                        )
                        logger.warning(
                            "⚠️ All '%s' rows were older than %dh; keeping %d most "
                            "recent instead of returning zero.",
                            site, hours_old, len(ranked),
                        )
                        df = ranked
                    else:
                        df = fresh
                except Exception as filter_err:
                    logger.warning(
                        f"Could not apply date filter for '{site}': {filter_err}"
                    )

            records = df.fillna("N/A").to_dict(orient="records")
            clean_records = [
                j
                for j in records
                if not is_unwanted_job(str(j.get("title", "")))
            ]
            collected_jobs.extend(clean_records)
            logger.info(
                f"✅ Extracted {len(clean_records)} jobs from '{site}'."
            )

        except Exception as site_err:
            logger.warning(
                f"⚠️ Provider '{site}' failed: {site_err}. Proceeding to next source.",
                exc_info=True,
            )

    return collected_jobs
