# src/engine/scraper.py
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from bs4 import BeautifulSoup
from jobspy import scrape_jobs

logger = logging.getLogger(__name__)

BLACKLIST_KEYWORDS = [
    "technician", "electrician", "hvac", "facility", "facilities",
    "building engineer", "critical environments", "data center",
    "hardware", "operator", "chiller", "plumbing", "carpentry",
]

_LINKEDIN_GUEST_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def fetch_linkedin_guest_job(linkedin_id: str) -> dict | None:
    """
    Fetch one LinkedIn posting via the public guest API, job-shaped dict or None.

    Single targeted request to /jobs-guest/jobs/api/jobPosting/<id>. Honest
    caveat: this is the SAME guest-endpoint family JobSpy's LinkedIn search
    uses, so it hits the same 429 wall from the VM's datacenter IP — it is
    not a bypass. It earns its place as the last resort because one request
    sometimes slips past the rate limit that blocks a paged search, and it
    fetches the exact posting by ID instead of a fuzzy search.

    Fail-open by design: any network error, non-200 status, auth-wall HTML,
    or missing description markup returns None and the caller falls through
    to the placeholder path. Title/company parse best-effort — the caller
    fills gaps from the email alert, which already knows them.
    """
    if not linkedin_id:
        return None
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{linkedin_id}"
    try:
        resp = requests.get(
            url, headers={"User-Agent": _LINKEDIN_GUEST_UA}, timeout=15
        )
    except requests.RequestException as req_err:
        logger.warning(
            "LinkedIn guest API fetch failed for job %s: %s", linkedin_id, req_err
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "LinkedIn guest API returned %s for job %s.",
            resp.status_code, linkedin_id,
        )
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    desc_el = soup.select_one("div.show-more-less-html__markup")
    if not desc_el:
        logger.warning(
            "LinkedIn guest API: no description markup for job %s "
            "(auth wall or changed markup).",
            linkedin_id,
        )
        return None
    description = desc_el.get_text("\n", strip=True)
    if len(description) < 50:
        logger.warning(
            "LinkedIn guest API: description too short (%d chars) for job %s.",
            len(description), linkedin_id,
        )
        return None
    title_el = soup.select_one("h2.top-card-layout__title")
    company_el = soup.select_one("a.topcard__org-name-link")
    location_el = soup.select_one("span.topcard__flavor--bullet")
    return {
        "title": title_el.get_text(strip=True) if title_el else None,
        "company": company_el.get_text(strip=True) if company_el else None,
        "location": location_el.get_text(strip=True)
        if location_el
        else "United States",
        "description": description,
        "job_url": f"https://www.linkedin.com/jobs/view/{linkedin_id}/",
        "job_type": "N/A",
        "date_posted": "N/A",
        "site": "linkedin",
    }


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
