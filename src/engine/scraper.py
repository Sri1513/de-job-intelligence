# src/engine/scraper.py
import contextlib
import logging
import os
import random
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from jobspy import scrape_jobs

logger = logging.getLogger(__name__)

# Residential egress for scraping. LinkedIn/Indeed/ZipRecruiter all
# 429/403/challenge the VM's datacenter IP; a forward proxy on the user's
# laptop (reached over Tailscale, e.g. http://100.68.109.36:8888) gives
# scraper traffic a residential IP. Set SCRAPER_PROXY_URL in .env.
# When unset or unreachable this is a no-op and traffic goes direct --
# today's behavior, never worse.
_PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
_NO_PROXY_ENV_VARS = ("NO_PROXY", "no_proxy")
_PROXY_CHECK_TIMEOUT_S = 3.0


def _configured_proxy_url() -> str | None:
    url = (os.environ.get("SCRAPER_PROXY_URL") or "").strip()
    return url or None


def _proxy_reachable(proxy_url: str) -> bool:
    try:
        parts = urlparse(proxy_url)
        host, port = parts.hostname, parts.port or 8080
        if not host:
            return False
        with socket.create_connection((host, port), timeout=_PROXY_CHECK_TIMEOUT_S):
            return True
    except OSError:
        return False


@contextlib.contextmanager
def maybe_scraper_proxy():
    """Route scraper HTTP traffic via SCRAPER_PROXY_URL when reachable.

    requests.Session honors HTTP(S)_PROXY env vars, which is how JobSpy's
    internal session picks the proxy up. NO_PROXY keeps localhost/loopback
    direct. Env is restored afterwards.

    Threading note: os.environ is process-wide, so two overlapping scrapes
    race on these vars -- but they always write the same values, and the
    worst case is one scrape silently falling back to direct. Scrapes are
    user-triggered minutes apart, so this stays a benign, fail-open edge.
    """
    proxy_url = _configured_proxy_url()
    if not proxy_url or not _proxy_reachable(proxy_url):
        if proxy_url:
            logger.info("Scraper proxy %s unreachable; scraping direct.", proxy_url)
        yield
        return
    saved = {k: os.environ.get(k) for k in _PROXY_ENV_VARS + _NO_PROXY_ENV_VARS}
    for k in _PROXY_ENV_VARS:
        os.environ[k] = proxy_url
    for k in _NO_PROXY_ENV_VARS:
        os.environ[k] = "localhost,127.0.0.1"
    logger.info("Scraping via residential proxy %s.", proxy_url)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _env_float(key: str, default: float) -> float:
    """Read a float env var, falling back to default on missing/garbage."""
    try:
        return max(0.0, float(os.environ.get(key, default)))
    except (TypeError, ValueError):
        return default


def linkedin_throttle_sleep(env_key: str, default_s: float, jitter_s: float = 1.5) -> None:
    """Pause with jitter to keep LinkedIn traffic under the burst 429 window.

    LinkedIn 429s rapid sequential requests even from a residential IP (verified
    2026-10-16: single guest-API requests return 200 while pipeline bursts 429).
    A small randomized pause between requests smooths the pattern. The base delay
    is env-overridable per call site so it can be tuned without a code change;
    0 disables. Jitter avoids a metronomic request fingerprint.
    """
    base = _env_float(env_key, default_s)
    if base <= 0:
        return
    delay = base + random.uniform(0, jitter_s)
    logger.debug("LinkedIn throttle: sleeping %.1fs (%s).", delay, env_key)
    time.sleep(delay)


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
    # Throttle before the request: LinkedIn 429s burst patterns even via the
    # residential proxy, while spaced single requests return 200.
    linkedin_throttle_sleep("LINKEDIN_GUEST_DELAY_S", 5.0)
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{linkedin_id}"
    try:
        # maybe_scraper_proxy: residential egress when the laptop proxy is
        # up, direct otherwise (fail-open).
        with maybe_scraper_proxy():
            resp = requests.get(url, headers={"User-Agent": _LINKEDIN_GUEST_UA}, timeout=15)
    except requests.RequestException as req_err:
        logger.warning("LinkedIn guest API fetch failed for job %s: %s", linkedin_id, req_err)
        return None
    if resp.status_code != 200:
        logger.warning(
            "LinkedIn guest API returned %s for job %s.",
            resp.status_code,
            linkedin_id,
        )
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    desc_el = soup.select_one("div.show-more-less-html__markup")
    if not desc_el:
        logger.warning(
            "LinkedIn guest API: no description markup for job %s (auth wall or changed markup).",
            linkedin_id,
        )
        return None
    description = desc_el.get_text("\n", strip=True)
    if len(description) < 50:
        logger.warning(
            "LinkedIn guest API: description too short (%d chars) for job %s.",
            len(description),
            linkedin_id,
        )
        return None
    title_el = soup.select_one("h2.top-card-layout__title")
    company_el = soup.select_one("a.topcard__org-name-link")
    location_el = soup.select_one("span.topcard__flavor--bullet")
    return {
        "title": title_el.get_text(strip=True) if title_el else None,
        "company": company_el.get_text(strip=True) if company_el else None,
        "location": location_el.get_text(strip=True) if location_el else "United States",
        "description": description,
        "job_url": f"https://www.linkedin.com/jobs/view/{linkedin_id}/",
        "job_type": "N/A",
        "date_posted": "N/A",
        "site": "linkedin",
    }


def is_unwanted_job(title: str) -> bool:
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in BLACKLIST_KEYWORDS)


# Corp-to-Corp (C2C) arrangement detection. JobSpy has no native C2C
# filter: its job_type taxonomy stops at "contract", and a C2C engagement
# only ever appears in posting text (title/body) on Indeed/LinkedIn/Dice.
# So a c2c_only run narrows at the source with job_type="contract" and
# then keeps only rows whose title/description positively confirms C2C.
# Exclusions are checked first: a posting that says "no C2C" or "W-2
# only" is dropped even if it mentions C2C elsewhere (e.g. "No
# sponsorship or C2C"). 1099 alone is NOT C2C and is not a positive.
_C2C_POSITIVE_PATTERNS = (
    re.compile(r"\bc\s*@\s*c\b", re.IGNORECASE),  # C@C
    re.compile(r"\bc2c\b", re.IGNORECASE),
    re.compile(r"\bcorp[-\s]?to[-\s]?corp\b", re.IGNORECASE),
    re.compile(r"\bc\s+to\s+c\b", re.IGNORECASE),
)
_C2C_EXCLUSION_PATTERNS = (
    re.compile(r"\bno\b[^.\n]{0,40}?\bc2c\b", re.IGNORECASE),
    re.compile(r"\bno\b[^.\n]{0,40}?corp[-\s]?to[-\s]?corp\b", re.IGNORECASE),
    re.compile(r"not\s+open\s+to[^.\n]{0,40}?c2c", re.IGNORECASE),
    re.compile(r"\bw-?2\s+only\b", re.IGNORECASE),
    re.compile(r"\bw-?2\s+basis\b", re.IGNORECASE),
    re.compile(r"must\s+be\s+(a\s+)?w-?2\b", re.IGNORECASE),
    re.compile(r"\bw-?2\s+employee\b", re.IGNORECASE),
)


def is_c2c_arrangement(job: dict) -> bool:
    """True when a job record's title/description confirms a C2C arrangement.

    Strict by design: rows with no C2C confirmation (including rows whose
    description is missing or a placeholder) return False, so a c2c_only
    run never stages an unverified row as C2C.
    """
    text = f"{job.get('title') or ''}\n{job.get('description') or ''}"
    if not text.strip():
        return False
    if any(pattern.search(text) for pattern in _C2C_EXCLUSION_PATTERNS):
        return False
    return any(pattern.search(text) for pattern in _C2C_POSITIVE_PATTERNS)


def fetch_scraped_jobs(
    search_term: str = "Data Engineer",
    location: str = "United States",
    results_wanted: int = 15,
    hours_old: int = 48,
    is_remote: bool = True,
    site_name: list = None,
    c2c_only: bool = False,
) -> list:
    """Scrapes job listings strictly from LinkedIn and Indeed.

    When c2c_only is True, JobSpy is asked for contract roles only
    (job_type="contract" — the closest native filter; C2C itself is not
    a JobSpy taxonomy). The C2C text confirmation itself is NOT applied
    here: LinkedIn descriptions are often empty at scrape time and only
    get filled by the guest-API enrichment in run_batch_ingestion_workflow,
    so filtering here would drop rows before their text exists. Callers
    that need the text filter should apply is_c2c_arrangement after
    enrichment (run_batch_ingestion_workflow does this).
    """
    if site_name is None:
        site_name = ["linkedin", "indeed"]

    collected_jobs = []

    # Residential proxy (laptop over Tailscale) when reachable; direct
    # otherwise. Covers every site in the loop and whatever requests the
    # site scrapers make internally.
    with maybe_scraper_proxy():
        for idx, site in enumerate(site_name):
            if idx > 0:
                # Space out per-site scrapes; JobSpy fires its own internal
                # request burst per site, so back-to-back sites look automated.
                linkedin_throttle_sleep("LINKEDIN_SITE_DELAY_S", 3.0)
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
                    job_type="contract" if c2c_only else None,
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
                                hours_old,
                                dropped,
                                site,
                                parsed_dates.max(),
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
                                site,
                                hours_old,
                                len(ranked),
                            )
                            df = ranked
                        else:
                            df = fresh
                    except Exception as filter_err:
                        logger.warning(f"Could not apply date filter for '{site}': {filter_err}")

                records = df.fillna("N/A").to_dict(orient="records")
                clean_records = [j for j in records if not is_unwanted_job(str(j.get("title", "")))]
                collected_jobs.extend(clean_records)
                logger.info(f"✅ Extracted {len(clean_records)} jobs from '{site}'.")

            except Exception as site_err:
                logger.warning(
                    f"⚠️ Provider '{site}' failed: {site_err}. Proceeding to next source.",
                    exc_info=True,
                )

    return collected_jobs
